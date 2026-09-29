"""A real Postgres for every test, one database per xdist worker.

`uv run pytest -q -n auto` runs the suite in parallel; plain `uv run pytest -q`
still runs it serially (xdist's `worker_id` is then "master"). Each worker gets
its own database, `artifacts_test_<worker_id>`, built once per session. Between
tests the `db` fixture empties every table with one TRUNCATE instead of
rebuilding the schema -- ~24 ms instead of ~105 ms, and the result is the same:
every table empty, every owned sequence restarted.

A test that changes the schema itself (drops a column, drops the schema, calls
`reset()`) must request `fresh_schema`, which rebuilds the schema after it and
re-reads the table list, so the next test on that worker gets the real schema.
"""

import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pgserver
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from artifacts.db import Database


def _with_dbname(dsn: str, dbname: str) -> str:
    """The same server, another database. Keeps URI DSNs as URIs, because
    `Database` appends `?connect_timeout=` to anything without one."""
    if "://" in dsn:
        parts = urlsplit(dsn)
        return urlunsplit(parts._replace(path=f"/{dbname}"))
    return make_conninfo(dsn, dbname=dbname, connect_timeout="5")


@pytest.fixture(scope="session")
def _pg_server(testrun_uid: str) -> str:
    """The Postgres server this run uses: PG_TEST_DSN (CI's one service), else
    one throwaway pgserver SHARED by every worker of this run.

    Shared, not one per worker: pgserver serialises server start-up behind one
    machine-wide lock, so eight servers start one after another (~1.5 s each)
    and cost eight ~59 MB data directories in a RAM-backed /tmp. One server plus
    a CREATE DATABASE per worker (~0.06 s) costs one start-up. pgserver counts
    the processes holding it and stops the server when the last one lets go."""
    existing = os.environ.get("PG_TEST_DSN")
    if existing:
        yield existing
        return
    pgdata = Path(tempfile.gettempdir()) / f"artifacts-pgtest-{testrun_uid}"
    pgdata.mkdir(exist_ok=True)
    # cleanup_mode="delete" (not the default "stop"): "stop" only stops the
    # server and leaves the ~59 MB pgdata directory behind. /tmp here is
    # RAM-backed, and 116 leftover directories once filled it to its quota,
    # failing later suites at initdb with "Disk quota exceeded".
    server = pgserver.get_server(pgdata, cleanup_mode="delete")
    try:
        yield server.get_uri()
    finally:
        server.cleanup()
        # Belt and braces: the last worker's cleanup already removed `pgdata`.
        # Never remove it while a server still runs in it for another worker.
        if not (pgdata / "postmaster.pid").exists():
            shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="session")
def dsn(_pg_server: str, worker_id: str) -> str:
    """This worker's own database, created fresh (dropped first if a crashed run
    left it behind) and dropped again at the end of the session."""
    name = f"artifacts_test_{worker_id}"
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
def db(_schema: _Schema, dsn: str) -> Database:
    _schema.empty()  # every table empty, every sequence restarted
    return Database(dsn)
