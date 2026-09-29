"""A real Postgres for every test, one database per xdist worker.

`uv run pytest -q -n auto` runs the suite in parallel; plain `uv run pytest -q`
still runs it serially (the worker id is then "master"). Each worker gets
its own database, `artifacts_test_<worker_id>`, built once per session. Between
tests the `db` fixture empties every table with one TRUNCATE instead of
rebuilding the schema -- ~24 ms instead of ~105 ms, and the result is the same:
every table empty, every owned sequence restarted.

A test that changes the schema itself (drops a column, drops the schema, calls
`reset()`) must request `fresh_schema`, which rebuilds the schema after it and
re-reads the table list, so the next test on that worker gets the real schema.
Any other test that changes the schema's column or table count fails with a
message saying so, and the schema is rebuilt behind it.

The throwaway server belongs to the xdist controller (or the only process,
when serial), never to a worker, so a worker killed mid-run cannot orphan it.
Servers orphaned by a killed controller are reaped at the next start.
"""

import json
import os
import re
import shutil
import signal
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import pgserver
import psutil
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from artifacts import events
from artifacts.db import Database


def _share_fastapi_type_adapters() -> None:
    """Build each FastAPI parameter validator once per process, not once per app.

    Almost every test builds its own app, and building one is ~0.13 s of CPU:
    FastAPI makes a fresh pydantic `TypeAdapter` for every parameter of every
    route (~200 per app), although the same `str`/`int`/`Query(...)` shapes
    recur in every build. Sharing an adapter across equal fields halves the
    build and takes about a minute of CPU off a full run. An adapter is a pure
    function of what is keyed here -- the annotation, its metadata, the field's
    attributes and the config, exactly what `ModelField.__post_init__` reads --
    so a shared one validates identically. Bounded, because models defined
    inside a router are new classes on every build and never hit.

    Tests only; the service builds its one app once and gains nothing. If
    FastAPI's internals move, this quietly stands down and tests are just
    slower."""
    try:
        from fastapi._compat import v2
    except ImportError:
        return
    if not (hasattr(v2, "asdict") and hasattr(v2.ModelField, "__post_init__")):
        return
    build = v2.ModelField.__post_init__
    cache: OrderedDict = OrderedDict()

    def frozen(value):
        try:
            hash(value)
            return value
        except TypeError:
            return ("unhashable", type(value), repr(value))

    def post_init(self) -> None:
        parts = v2.asdict(self.field_info)
        try:
            key = (
                parts["annotation"],
                tuple(frozen(m) for m in parts["metadata"]),
                tuple(sorted((k, frozen(v)) for k, v in parts["attributes"].items())),
                repr(self.config),
            )
            hash(key)
        except TypeError:
            build(self)
            return
        adapter = cache.get(key)
        if adapter is None:
            build(self)
            cache[key] = self._type_adapter
            if len(cache) > 2048:
                cache.popitem(last=False)
        else:
            cache.move_to_end(key)
            self._type_adapter = adapter

    v2.ModelField.__post_init__ = post_init


_share_fastapi_type_adapters()

# How the controller hands its server to the xdist workers it spawns.
_URI_ENV = "ARTIFACTS_TEST_PG_URI"
_PGDATA_PREFIX = "artifacts-pgtest-"


def _with_dbname(dsn: str, dbname: str) -> str:
    """The same server, another database. Keeps URI DSNs as URIs, because
    `Database` appends `?connect_timeout=` to anything without one. A `dbname`
    in the query string would override the path, so it is dropped."""
    if "://" in dsn:
        parts = urlsplit(dsn)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "dbname"]
        return urlunsplit(
            parts._replace(
                path=f"/{dbname}", query=urlencode(query, safe="/", quote_via=quote)
            )
        )
    return make_conninfo(dsn, dbname=dbname, connect_timeout="5")


def _stop_postmaster(pgdata: Path) -> None:
    """Stop the server running in `pgdata`, by the pid in its postmaster.pid,
    and only if that pid really is a postgres for this directory."""
    try:
        pid = int((pgdata / "postmaster.pid").read_text().splitlines()[0])
        proc = psutil.Process(pid)
        if "postgres" not in proc.name() or str(pgdata) not in proc.cmdline():
            return
        proc.send_signal(signal.SIGINT)  # postgres: fast shutdown
        proc.wait(timeout=10)
    except psutil.TimeoutExpired:
        proc.kill()
    except (OSError, ValueError, IndexError, psutil.Error):
        return


def _reap_orphans() -> None:
    """Stop and remove every throwaway test server whose clients are all dead:
    a run killed with SIGKILL never reached its own cleanup. pgserver lists its
    client pids in .handle_pids.json and never prunes a dead one."""
    for pgdata in Path(tempfile.gettempdir()).glob(f"{_PGDATA_PREFIX}*"):
        try:
            pids = json.loads((pgdata / ".handle_pids.json").read_text())
        except FileNotFoundError:
            # No client ever registered: a start-up in progress, or one that
            # died before registering. Leave a young one alone.
            try:
                if time.time() - pgdata.stat().st_mtime < 300:
                    continue
            except OSError:
                continue
            pids = []
        except (OSError, ValueError):
            continue
        if any(psutil.pid_exists(pid) for pid in pids):
            continue
        _stop_postmaster(pgdata)
        shutil.rmtree(pgdata, ignore_errors=True)


class _Server:
    """The throwaway pgserver, owned by exactly one process: the xdist
    controller (or the only process, when serial). Workers never hold it, so
    a worker that crashes cannot leave it running.

    One shared server, not one per worker: pgserver serialises start-up behind
    one machine-wide lock, so eight servers start one after another (~1.5 s
    each) and cost eight ~59 MB data directories in a RAM-backed /tmp. One
    server plus a CREATE DATABASE per worker (~0.06 s) costs one start-up."""

    server = None
    pgdata: Path | None = None

    @classmethod
    def ensure(cls) -> str:
        if cls.server is None:
            _reap_orphans()
            cls.pgdata = Path(tempfile.gettempdir()) / (
                f"{_PGDATA_PREFIX}{uuid.uuid4().hex}"
            )
            cls.pgdata.mkdir()
            # cleanup_mode="delete" (not the default "stop"): "stop" leaves the
            # ~59 MB pgdata directory behind. /tmp here is RAM-backed, and 116
            # leftover directories once filled it to its quota, failing later
            # suites at initdb with "Disk quota exceeded".
            cls.server = pgserver.get_server(cls.pgdata, cleanup_mode="delete")
        return cls.server.get_uri()

    @classmethod
    def stop(cls) -> None:
        if cls.server is None:
            return
        cls.server.cleanup()
        # Belt and braces, in case cleanup ever stops short of both.
        _stop_postmaster(cls.pgdata)
        shutil.rmtree(cls.pgdata, ignore_errors=True)
        cls.server = None


def _is_worker(config) -> bool:
    return hasattr(config, "workerinput")


def pytest_configure(config) -> None:
    """Under -n the controller starts the server before it spawns workers,
    and they inherit its address. Serially it starts on first use instead."""
    parallel = config.getoption("dist", "no") != "no"
    if _is_worker(config) or not parallel or os.environ.get("PG_TEST_DSN"):
        return
    if config.getoption("collectonly") or config.getoption("help"):
        return
    os.environ[_URI_ENV] = _Server.ensure()


def pytest_unconfigure(config) -> None:
    if not _is_worker(config):
        _Server.stop()
        os.environ.pop(_URI_ENV, None)


@pytest.fixture(autouse=True)
def _prompt_event_looks(monkeypatch):
    """A held wait looks for new events every half second in production, so a
    test that waits on one idled up to 0.5 s per wake. Tests look every 20 ms;
    a test that needs a particular interval still sets its own."""
    monkeypatch.setattr(events, "_LOOK_EVERY", 0.02)


@pytest.fixture()
def brief_holds(monkeypatch):
    """Cap every held `/events` read the server makes at 0.2 s.

    The CLI waiters ask the server to hold for whole seconds (at least one),
    so a test that expects "nothing before the timeout" used to sit through
    one or more full seconds of an empty hold. With the server's side capped,
    a short timeout (`wait --timeout 0.1s`, `wait --review --timeout 1.1s`)
    still runs the client's real loop -- look, listen once, give up at the
    deadline -- in about a quarter of a second."""
    real = events.wait_for_agent

    def brief(db, agent, *, seconds, **kwargs):
        return real(db, agent, seconds=min(seconds, 0.2), **kwargs)

    monkeypatch.setattr(events, "wait_for_agent", brief)


@pytest.fixture()
def on_first_listen(monkeypatch):
    """`arm(action)`: run `action` in a thread the moment an `artifacts-decide`
    waiter first asks the server to HOLD -- i.e. while it is listening, which is
    what these tests are about -- instead of after a guessed sleep. Returns the
    thread, to join."""
    from artifacts import decide

    def arm(action):
        real = decide._request
        fired = threading.Event()

        def go():
            time.sleep(0.05)  # let the held request reach the server first
            action()

        thread = threading.Thread(target=go, daemon=True)

        def listening(method, url, *args, **kwargs):
            if "&wait=" in url and not fired.is_set():
                fired.set()
                thread.start()
            return real(method, url, *args, **kwargs)

        monkeypatch.setattr(decide, "_request", listening)
        return thread

    return arm


@pytest.fixture()
def first_look(monkeypatch):
    """Set once a held wait has made its first look and found nothing, i.e.
    it has settled into waiting. Waiting on this instead of a fixed sleep
    means the test proceeds the moment the condition is true, under any load."""
    real = events.list_for_agent
    looked = threading.Event()

    def look(*args, **kwargs):
        rows = real(*args, **kwargs)
        if not rows:
            looked.set()
        return rows

    monkeypatch.setattr(events, "list_for_agent", look)
    return looked


@pytest.fixture(scope="session")
def _pg_server() -> str:
    """PG_TEST_DSN (CI's one service), else the controller's throwaway server."""
    return os.environ.get("PG_TEST_DSN") or os.environ.get(_URI_ENV) or _Server.ensure()


def _worker_id() -> str:
    """xdist's worker id ("gw0", ...), or "master" when serial or without xdist."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    assert re.fullmatch(r"\w+", worker), worker
    return worker


@pytest.fixture(scope="session")
def dsn(_pg_server: str) -> str:
    """This worker's own database, created fresh (dropped first if a crashed run
    left it behind) and dropped again at the end of the session."""
    name = f"artifacts_test_{_worker_id()}"
    drop = sql.SQL("drop database if exists {} with (force)").format(
        sql.Identifier(name)
    )
    with psycopg.connect(_pg_server, autocommit=True) as conn:
        conn.execute(drop)
        conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    try:
        yield _with_dbname(_pg_server, name)
    finally:
        with psycopg.connect(_pg_server, autocommit=True) as conn:
            conn.execute(drop)


class _Schema:
    """The worker's schema, built once, and the table list TRUNCATE empties."""

    def __init__(self, dsn: str) -> None:
        self.database = Database(dsn)
        self.tables: list[str] = []
        self.rebuild()

    def rebuild(self) -> None:
        self.database.reset()
        with self.database.connect() as conn:
            rows = conn.execute(
                "select tablename from pg_tables where schemaname = current_schema()"
                " order by tablename"
            ).fetchall()
        self.tables = [row[0] for row in rows]
        assert self.tables, "the schema built no tables"
        self.built = self.fingerprint()

    def fingerprint(self) -> tuple[int, int]:
        """Cheap evidence the schema is still the one built: its column and
        table counts. Catches a test that altered the schema without asking
        for `fresh_schema`, before it poisons the next test on this worker."""
        with self.database.connect() as conn:
            # pg_catalog rather than information_schema.columns: the same
            # counts for tables, in ~0.7 ms instead of ~2.4 ms per test.
            return conn.execute(
                "select count(*) filter (where a.attnum > 0), count(distinct c.oid)"
                " from pg_class c join pg_attribute a"
                "   on a.attrelid = c.oid and not a.attisdropped"
                " where c.relnamespace = current_schema()::regnamespace"
                "   and c.relkind in ('r', 'p')"
            ).fetchone()

    def empty(self) -> None:
        truncate = sql.SQL("truncate {} restart identity cascade").format(
            sql.SQL(", ").join(sql.Identifier(t) for t in self.tables)
        )
        with self.database.connect() as conn:
            # A connection a previous test leaked with a lock held would block
            # this forever; fail loudly instead.
            conn.execute("set lock_timeout = '10s'")
            conn.execute(truncate)


@pytest.fixture(scope="session")
def _schema(dsn: str) -> _Schema:
    return _Schema(dsn)


@pytest.fixture()
def fresh_schema(_schema: _Schema):
    """For a test that alters the schema: rebuild it afterwards, so the next
    test on this worker is not poisoned by what this one changed."""
    yield
    _schema.rebuild()


@pytest.fixture()
def db(_schema: _Schema, dsn: str, request):
    _schema.empty()  # every table empty, every sequence restarted
    yield Database(dsn)
    if "fresh_schema" in request.fixturenames:
        return  # it rebuilds anyway
    now = _schema.fingerprint()
    if now != _schema.built:
        _schema.rebuild()  # so the tests after it are not poisoned
        pytest.fail(
            f"{request.node.nodeid} changed the schema (columns, tables:"
            f" {_schema.built} -> {now}) without requesting `fresh_schema`."
            " Add @pytest.mark.usefixtures('fresh_schema') to it.",
            pytrace=False,
        )
