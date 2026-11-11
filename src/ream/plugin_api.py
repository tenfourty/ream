"""The plug-in API and its loader (core-and-plugins design, 4.1, 4.2, 4.5, 4.6).

A plug-in is a Python package with an entry point in the group `ream.plugins` (or the old
`artifacts.plugins`, kept for good)
naming a function `register(api: PluginAPI) -> None`. The server calls each once at
start-up, in name order. Registrations: `sink` (with an optional in-transaction `stage`), `answer_source`, `on_answer`,
`scanner`, `mail_backend`, `schema`, `on_start`, `on_stop`, `cli`, `client_defaults` (the last
two matter to the command-line tools: `plugin_cli.py`).

A plug-in that raises while registering, names a major API version this core does not
speak, or is named in `REAM_PLUGINS` but not installed, stops start-up
(`PluginError`, naming it): a source or sink silently absent is worse than a pod
that does not start.

A sink a plug-in registers is wrapped: when `deliver` raises, the failure is logged with the
plug-in and interface and counted per plug-in (`failure_counts()`; also under
`plugin_failures` in `sinks.status()`), and then raised on, so the outbox retries and an
after-commit failure is dropped by the machinery that already does both. A timeout is
not counted there: the outbox gives up waiting while the plug-in's `deliver` is still
running, so the wrapper sees no failure; the outbox row's `last_error` says
`TimeoutError`, and the circuit breaker counts it.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from importlib import metadata, resources

from ream import (
    db as dbmod,
)
from ream import (
    decisions,
    events,
    hosts,
    mail,
    plugin_cli,
    scanners,
    settings,
    sinks,
    store,
    textvars,
)

# Shared with the command-line side, which cannot import this module (plugin_cli.py).
API_VERSION = plugin_cli.API_VERSION
GROUP = plugin_cli.GROUP
OLD_GROUP = plugin_cli.OLD_GROUP
PluginError = plugin_cli.PluginError

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_failures: dict[tuple[str, str], int] = {}
_loaded: list[str] = []
# Inbound routes plug-ins mounted, by normalised host: (plug-in, guarded ASGI app).
_ROUTERS: dict[str, tuple[str, Callable]] = {}
# API 1.2: plug-in tables (plug-in, one checked statement), and start / stop hooks
# (plug-in, fn), in registration order.
_SCHEMA: list[tuple[str, str]] = []
_STARTS: list[tuple[str, Callable]] = []
_STOPS: list[tuple[str, Callable]] = []


def failure_counts() -> dict[str, int]:
    """Failures caught, keyed `<plug-in>:<interface>`. With the sink sender's database
    bound (`sinks.bind`) this is the total over every process using it; otherwise, or
    if it cannot be read, the count since this process started.

    Bound, a key reports the stored total PLUS this process's still-unstored increments
    (`_pending`). That is right for several processes: the stored total holds what every
    process has flushed, and the pending ones are exactly what this process has counted
    and not flushed (a write that failed earlier is retried, never lost), so nothing is
    counted twice. A max of memory and the stored total would be wrong across
    processes, since memory knows only this process's share."""
    db = sinks.shared()
    if db is not None:
        _flush()
        try:
            with db.connect() as conn:
                rows = conn.execute(
                    "select plugin, interface, failures from plugin_failure"
                    " order by plugin, interface"
                ).fetchall()
            total = {(p, i): n for p, i, n in rows}
            with _lock:
                for (d, p, i), n in _pending.items():
                    if d is db:
                        total[(p, i)] = total.get((p, i), 0) + n
            return {f"{p}:{i}": n for (p, i), n in sorted(total.items())}
        except Exception:  # noqa: BLE001 -- a counter must not break a status call
            _log.warning("plug-in failure counts: shared copy unreachable")
    with _lock:
        return {f"{p}:{i}": n for (p, i), n in sorted(_failures.items())}


def loaded() -> list[str]:
    """The plug-ins `load` registered, in the order it called them."""
    return list(_loaded)


def count_failure(plugin: str, interface: str) -> None:
    """Count one caught failure of `plugin`'s `interface` (see `failure_counts`)."""
    _count(plugin, interface)


# --- the one writer of the shared failure counts ---------------------------------
# A caught failure is counted in memory at once and queued here; ONE daemon thread
# (started on the first count with a database bound, stopped by `sinks.stop`) stores
# the queue, one upsert per key and one connection per flush, so a plug-in route that
# faults on every request costs a dict update, not a thread and a connection each.
# Off the caller's thread because the caller may be inside a transaction of its own
# (the answer hook is), and on SQLite a second writer would wait for that commit.
_MAX_PENDING_KEYS = 1000
_RETRY_SECONDS = 2.0
_pending: dict[tuple[object, str, str], int] = {}
_cond = threading.Condition(_lock)
_wake = threading.Event()
_writer: threading.Thread | None = None
_writer_stop: threading.Event | None = None
_writing = False
_store_failing = False


def _count(plugin: str, interface: str) -> None:
    global _writer, _writer_stop
    db = sinks.shared()
    with _lock:
        _failures[(plugin, interface)] = _failures.get((plugin, interface), 0) + 1
        if db is None:
            return
        key = (db, plugin, interface)
        if key in _pending or len(_pending) < _MAX_PENDING_KEYS:
            _pending[key] = _pending.get(key, 0) + 1
        if _writer is None or not _writer.is_alive():
            _writer_stop = threading.Event()
            _writer = threading.Thread(
                target=_write_loop,
                args=(_writer_stop,),
                name="plugin-failure-writer",
                daemon=True,
            )
            _writer.start()
    _wake.set()


def _store(batch: dict[tuple[object, str, str], int]) -> None:
    """Store `batch`, one connection per database, one upsert per key."""
    by_db: dict[object, list[tuple[str, str, int]]] = {}
    for (db, plugin, interface), n in batch.items():
        by_db.setdefault(db, []).append((plugin, interface, n))
    for db, items in by_db.items():
        with db.connect() as conn, conn.transaction():
            for plugin, interface, n in items:
                conn.execute(
                    "insert into plugin_failure (plugin, interface, failures)"
                    " values (%s, %s, %s) on conflict (plugin, interface)"
                    " do update set failures = plugin_failure.failures + %s",
                    (plugin, interface, n, n),
                )


def _flush_once() -> bool:
    """Store what is pending. On a failure the increments go back (bounded) and the
    failure is logged once per streak. Returns whether it all went in."""
    global _writing, _store_failing
    with _cond:
        if not _pending:
            return True
        batch = dict(_pending)
        _pending.clear()
        _writing = True
    ok = True
    try:
        _store(batch)
    except Exception:  # noqa: BLE001 -- the in-memory count stands
        ok = False
    with _cond:
        if ok:
            _store_failing = False
        else:
            for key, n in batch.items():
                if key in _pending or len(_pending) < _MAX_PENDING_KEYS:
                    _pending[key] = _pending.get(key, 0) + n
            if not _store_failing:
                _log.warning("plug-in failure counts not stored", exc_info=True)
            _store_failing = True
        _writing = False
        _cond.notify_all()
    return ok


def _write_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        _wake.wait()
        _wake.clear()
        if stop.is_set():
            break
        while not stop.is_set() and not _flush_once():
            if stop.wait(_RETRY_SECONDS):  # down: try again shortly, not in a spin
                break
    _flush_once()  # last try on the way out


def stop_writer(timeout: float = 2.0) -> None:
    """Stop the writer (after one last try to store) and forget what is queued.
    `sinks.stop` calls it; the next count starts a new one."""
    global _writer, _writer_stop, _store_failing
    with _lock:
        thread, flag = _writer, _writer_stop
        _writer = _writer_stop = None
    if flag is not None:
        flag.set()
        _wake.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout)
    with _cond:
        _pending.clear()
        _store_failing = False
        _cond.notify_all()


def _flush(timeout: float = 5.0) -> None:
    """Wait for the writer to go idle (a status call wants the counts in). Gives up
    at once while the database is failing: the pending ones are added in memory."""
    _wake.set()
    with _cond:
        _cond.wait_for(
            lambda: _store_failing or (not _pending and not _writing), timeout
        )


class PluginAPI:
    """What `register` is handed. One per plug-in, so a registration knows whose it is."""

    version = API_VERSION

    def __init__(self, plugin: str) -> None:
        self._plugin = plugin
        # The answer sources this plug-in registered: `on_answer` needs one, and
        # runs only for answers stored with one of them.
        self._sources: set[str] = set()

    def sink(
        self,
        name: str,
        kinds: tuple[str, ...] | list[str],
        deliver: Callable | None = None,
        delivery: str = "outbox",
        *,
        label: str | None = None,
        stage: Callable | None = None,
    ) -> None:
        """Register an event sink (4.2, 1). `deliver(event)` returns on success and
        raises to retry (outbox) or be logged (after commit). A kind that is not one of
        `events.KINDS` or `events.BRIDGE_KINDS` is refused (`PluginError`, naming the
        plug-in and the kind): the sink would otherwise never match. Plug-ins cannot
        declare kinds of their own, so these two lists are the whole set.

        API 1.2: `stage(conn, event)` (outbox delivery only) runs in the action's own
        transaction, where the core's own staging sinks run, so the plug-in can write
        its own queue in the plug-in's tables (`schema`). It runs inside a savepoint: a
        raise undoes only its own writes, is logged and counted (`<plug-in>:stage`),
        and the action, its journal row and the other sinks go on; the event is simply
        not staged for this sink. With `stage` the core writes no outbox row and calls
        no `deliver`: the plug-in's own sender (`on_start`) delivers. A sink needs
        exactly one of `deliver` and `stage`. A database error the stage catches itself
        still rolls its savepoint back and counts on Postgres (the transaction is aborted
        there). A stage that ends the transaction (`commit`, `rollback`, SQLite's `or
        rollback`) fails the whole action with `db.TransactionLost`."""
        plugin = self._plugin
        if (deliver is None) == (stage is None):
            raise PluginError(
                f"plug-in {plugin!r}: sink {name!r} needs exactly one of deliver"
                " and stage"
            )
        if stage is not None and delivery != "outbox":
            raise PluginError(
                f"plug-in {plugin!r}: sink {name!r}: stage is for delivery='outbox'"
            )
        known = (*events.KINDS, *events.BRIDGE_KINDS)
        for kind in kinds:
            if kind not in known:
                raise PluginError(
                    f"plug-in {plugin!r}: sink {name!r} names unknown event kind"
                    f" {str(kind)[:60]!r}"
                )

        if stage is not None:

            def guarded_stage(conn, event):
                # A savepoint, so a database error inside the plug-in's writes (which
                # on Postgres would poison the whole transaction) undoes only them.
                try:
                    with conn.transaction():
                        stage(conn, event)
                        if not conn.in_transaction():
                            raise dbmod.TransactionLost("a stage ended the transaction")
                        # A stage that caught its own database error (a unique
                        # violation, say) leaves Postgres's transaction aborted; this
                        # raises here, inside the savepoint, so it is rolled back to
                        # instead of RELEASE failing and losing the whole action.
                        conn.execute("select 1")
                except dbmod.TransactionLost:
                    _count(plugin, "stage")
                    raise  # the action cannot be saved: fail it whole, never half
                except Exception as exc:  # noqa: BLE001 - a plug-in's fault is isolated
                    if not conn.in_transaction():
                        _count(plugin, "stage")
                        raise dbmod.TransactionLost(
                            "a stage ended the transaction"
                        ) from None
                    _count(plugin, "stage")
                    _log.warning(
                        "plug-in %s: sink %s stage failed: %s",
                        plugin,
                        name,
                        type(exc).__name__,
                    )

            sinks.register(
                sinks.Sink(
                    name,
                    tuple(kinds),
                    stage=guarded_stage,
                    delivery=delivery,
                    label=label,
                )
            )
            return

        def guarded(event):
            try:
                deliver(event)
            except Exception as exc:
                _count(plugin, "sink")
                _log.warning(
                    "plug-in %s: sink %s failed: %s", plugin, name, type(exc).__name__
                )
                raise

        sinks.register(
            sinks.Sink(
                name, tuple(kinds), deliver=guarded, delivery=delivery, label=label
            )
        )

    def answer_source(
        self,
        name: str,
        label: str,
        *,
        relay: bool = False,
        verified_label: str | None = None,
        route_label: str | None = None,
    ) -> None:
        """Declare a stored `answer_source` value and its wording (4.2, 2)."""
        self._sources.add(name)
        decisions.register_source(
            name,
            label,
            relay=relay,
            verified_label=verified_label,
            route_label=route_label,
        )

    def on_answer(self, name: str, fn: Callable) -> None:
        """Register an answer hook (4.2, 3). `fn(conn, decision, answer)` runs inside the
        answer's transaction, after the row is written and before the commit, for answers
        stored with a source THIS plug-in registered (`answer_source`, called first: a
        plug-in with none is refused, so the hook stays with the plug-in that vouches
        for its own inbound answers). It is for answers the person gave through the
        plug-in's own inbound path (`decisions.answer`), NOT for relays: an agent's
        `record_elsewhere` never runs it. `answer` is a `decisions.Answer`. A raise aborts
        the whole answer (`decisions.AnswerHookFailed`, naming the plug-in)."""
        if not self._sources:
            raise ValueError(
                f"plug-in {self._plugin!r}: on_answer needs an answer_source"
                " registered first"
            )
        # The live set, so a source registered after the hook counts too.
        decisions.register_answer_hook(self._plugin, name, fn, self._sources)

    def scanner(
        self,
        name: str,
        scan: Callable,
        on_error: str = "skip",
        timeout: float | None = None,
    ) -> None:
        """Register a scanner (4.2, 4). `scan(text) -> list[scan.Finding]` is run where
        the core takes its second opinion (after a clean pattern scan: bodies, section
        notes, agent comments' second scan). `on_error="skip"`: a raise is logged and
        counted and the text is judged on the other scanners; `"block"`: it fails the
        action (`scanners.ScannerBlocked`). Under `block` an agent comment not yet
        cleared is hidden (no event), so replies to it fail as for a withdrawn comment
        until a clean scan shows it again. Each call is bounded by `timeout` seconds
        (API 1.1; a positive number, capped at `scanners.MAX_SCAN_TIMEOUT_SECONDS`, 20),
        else by `scanners.SCAN_TIMEOUT_SECONDS` (10); a scanner that hangs costs one
        daemon thread per call, never reclaimed. The scanner named `gitguardian` is the
        second opinion on an agent comment (`scanners.SECOND_OPINION`): while it is
        registered, only its clean answer stamps a comment scanned."""
        scanners.register(self._plugin, name, scan, on_error, timeout)

    def mail_backend(self, name: str, send: Callable, enabled: Callable) -> None:
        """Register the mail backend (4.2, 5). `send(to, subject, text)` raises to fail;
        `enabled()` says whether mail is on. One backend at most; with none, mail is
        off. `name` is only for the log."""
        mail.register_backend(self._plugin, send, enabled)

    def schema(self, statements: list[str] | tuple[str, ...]) -> None:
        """Declare this plug-in's own tables (API 1.2). Each statement is created at
        start-up, after the core's migration and before any sink or hook runs, on
        Postgres and on SQLite, idempotently. Only `create table if not exists
        <prefix>_...` and `create [unique] index if not exists <prefix>_... on
        <prefix>_...` are accepted, where `<prefix>` is the plug-in's name in lower
        case with every run of other characters as `_`; no `;`, no `%`, no comment, no
        `rollback`, nothing after a table's column list, and (checked against the
        catalog at `apply_schema`) no core name, and an index or a `references` only on
        a table this plug-in's own statements create. Write
        the dialect-neutral subset: `text`, `integer`, `bigint`, `boolean`
        (default `true`/`false`), `timestamptz`, `jsonb`, plain constant defaults and
        primary key / unique / not null. There is no migration after the first
        creation: a later change is a new table or index, never an edit."""
        if isinstance(statements, str):
            raise PluginError(f"plug-in {self._plugin!r}: schema takes a list")
        checked = [_check_ddl(self._plugin, st) for st in statements]
        _SCHEMA.extend((self._plugin, st) for st in checked)

    def on_start(self, fn: Callable) -> None:
        """Run `fn(db)` once when the server has started (API 1.2): after the plug-in
        tables exist and the core's outbox sender is up, in name order of plug-in. For a
        sender thread of the plug-in's own; `fn` must return promptly. A raise is
        logged, counted (`<plug-in>:start`) and does not stop the server."""
        _STARTS.append((self._plugin, fn))

    def on_stop(self, fn: Callable) -> None:
        """Run `fn()` once when the server shuts down (API 1.2), in the reverse order of
        start, each isolated: a raise is logged, counted (`<plug-in>:stop`) and the
        rest still run. It must return quickly: each hook gets `STOP_SECONDS` (2) in a
        thread of its own, and one still running then is left behind and counted.
        Best effort: a killed process runs none."""
        _STOPS.append((self._plugin, fn))

    def text_vars(self, values: dict) -> None:
        """Add `{{name}}` template variables (4.2, text). Each name must start with this
        plug-in's own prefix (its name, lower-case, `_` for anything but letters and
        digits, then `_`: plug-in `beads-page` -> `beads_page_...`), so a plug-in can never
        take a core name or another's. A value is a string or a function of no arguments
        read each time a text is served; one that raises or returns a non-string renders
        as nothing and is counted as `<plug-in>:text`. Core served texts name none of
        them: they are for a plug-in's own `text_section` words and templates."""
        for name, value in dict(values).items():
            textvars.register_var(self._plugin, name, value)

    def text_section(self, slot: str, fn: Callable[[str], str]) -> None:
        """Add words to a named place in the served documents (4.2, text). `slot` is one of
        `textvars.SLOTS`: `proxy` and `tools` (after the tools paragraph and the proxy note
        in `agents.txt` only) and `integrations` (the end of `agents.txt` and of `skill.md`).
        `fn(doc)` gets `"agents.txt"` or `"skill.md"` and returns the paragraph for it, or
        "" for none. Several plug-ins' sections follow in registration order, each its own
        paragraph. One section per slot per plug-in."""
        textvars.register_section(self._plugin, slot, fn)

    def router(self, router, host: str) -> None:
        """Mount an inbound route on a dedicated host (4.2, routes). `router` is a FastAPI
        `APIRouter`; it is served only for requests whose Host is exactly `host`, never on
        the gallery, API, partner or content hosts, which a plug-in may not take (nor one
        another's): such a registration stops start-up. The route has no gallery or key
        authentication: it checks its own caller. A fault inside it answers a bare 500,
        counted as `<plug-in>:router` and logged by exception type only. Every response
        carries `x-content-type-options: nosniff`. The core applies NO body-size cap and no
        authentication to the route: it must bound what it reads (as the partner inbound
        route does with MAX_BODY) and check its caller itself. The host must be a plain
        dotted name (a non-ASCII one is IDNA-encoded first); anything else stops start-up.
        It may be a sibling of the gallery host: core cookies are host-only and gallery
        writes need the same origin, but a plug-in must not set `Domain=` cookies on the
        parent domain."""
        from fastapi import APIRouter, FastAPI
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse

        if not isinstance(router, APIRouter):
            raise TypeError(
                f"plug-in {self._plugin!r}: router needs a FastAPI APIRouter"
            )
        name = hosts.normalise(host) if isinstance(host, str) else None
        if name is None or not name.strip("."):
            raise ValueError(f"plug-in {self._plugin!r}: router needs a host")
        if not _HOST_NAME.fullmatch(name):
            try:
                name = name.encode("idna").decode("ascii")
            except UnicodeError:
                name = ""
            if not _HOST_NAME.fullmatch(name):
                raise ValueError(
                    f"plug-in {self._plugin!r}: router host is not a plain host name"
                )
        why = _reserved_host(name)
        if why is not None:
            raise ValueError(f"plug-in {self._plugin!r}: host {name!r} is {why}")
        if name in _ROUTERS:
            raise ValueError(
                f"plug-in {self._plugin!r}: host {name!r} is taken by {_ROUTERS[name][0]!r}"
            )
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.exception_handler(RequestValidationError)
        async def _no_echo(_request, exc):
            # Where a field went wrong, never what was sent (as the core's own apps do).
            errors = [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
            return JSONResponse(status_code=422, content={"detail": errors})

        app.include_router(router)
        _ROUTERS[name] = (self._plugin, _guard_app(self._plugin, app))

    def cli(self, name: str, main: Callable, help: str) -> None:
        """Register `ream <name>` (4.2, 6). `main()` is called as a console script is: it
        reads `sys.argv` (argv[0] is `ream <name>`) and returns the exit status. `help` is
        one printable line for `ream --help`. A verb the core owns, or one already taken, is
        refused (start-up stops). Used by the command-line process; the server only checks
        the registration."""
        plugin_cli.register_cli(self._plugin, name, main, help)

    def client_defaults(
        self,
        api_url: str | None = None,
        session_envs: list[str] | tuple[str, ...] = (),
        news: list[tuple[str, str]] | tuple[tuple[str, str], ...] = (),
    ) -> None:
        """The command-line tools' defaults (4.2, 6): `api_url` is the endpoint when
        `REAM_API` is unset (one plug-in at most); `session_envs` are environment variables
        holding the office part of the default session label, asked before the core's own
        `QD_OFFICE` and seat; `news` is `(YYYY-MM-DD[a-z], text)` lines printed once per
        machine with the core's. Used by the command-line process; the server only checks
        the registration.

        A plug-in runs in-process with the caller's rights and can read the key file;
        `api_url` only applies when `REAM_API`/`ARTIFACTS_API` is unset, and a key is not
        bound to an endpoint, so install only plug-ins you trust. `api_url` must be https
        (or http to a loopback host), with a valid port, no userinfo, query or fragment; a
        trailing "/" is dropped. A `session_envs` value is used only if it matches
        `^[a-z0-9][a-z0-9._-]{0,63}$` (lower-cased first). `news` ids are real dates not
        later than today (UTC), the text one printable line; the core's "seen" marker is
        never moved by them. `register` runs on every start of every command-line tool: keep
        it cheap and free of side effects (no network, files or subprocesses)."""
        plugin_cli.register_client_defaults(self._plugin, api_url, session_envs, news)


def _prefix(plugin: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", plugin.lower()).strip("_") + "_"


_DDL_TABLE = re.compile(
    r"\s*create\s+table\s+if\s+not\s+exists\s+([a-z][a-z0-9_]*)\s*\(", re.IGNORECASE
)
_DDL_INDEX = re.compile(
    r"\s*create\s+(?:unique\s+)?index\s+if\s+not\s+exists\s+([a-z][a-z0-9_]*)"
    r"\s+on\s+([a-z][a-z0-9_]*)\s*\(",
    re.IGNORECASE,
)
_DDL_REFERENCES = re.compile(r"\breferences\s+([a-z0-9_\"]+)", re.IGNORECASE)


def _close_of_list(statement: str, start: int) -> int | None:
    """The index of the `)` that closes the parenthesis opened just before `start`,
    skipping quoted text and quoted names, or None if it never closes."""
    depth = 1
    quote = None
    for i in range(start, len(statement)):
        ch = statement[i]
        if quote:
            if ch == quote:
                quote = None  # a doubled quote reopens at once: same result
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _check_ddl(plugin: str, statement: str) -> str:
    """One schema statement, or PluginError (naming the plug-in and never the text)."""
    prefix = _prefix(plugin)

    def refuse(why: str):
        raise PluginError(f"plug-in {plugin!r}: schema statement refused: {why}")

    if not isinstance(statement, str) or not statement.strip():
        refuse("not a statement")
    if re.search(r";|%|--|/\*", statement):
        refuse("no ';', '%' or comment allowed")
    # SQLite's `on conflict rollback` (or `or rollback`) would end the whole
    # transaction it runs in; no schema statement needs the word.
    if re.search(r"\brollback\b", statement, re.IGNORECASE):
        refuse("no rollback")
    table = _DDL_TABLE.match(statement)
    index = None if table else _DDL_INDEX.match(statement)
    if table is None and index is None:
        refuse("only create table / index if not exists")
    if table is not None:
        # The statement ends at the `)` closing its column list: nothing after it,
        # so no `as select`, `as values`, `as table`, `inherits (...)` and the like.
        close = _close_of_list(statement, table.end())
        if close is None or statement[close + 1 :].strip():
            refuse("a table statement ends at its column list")
    names = [table.group(1)] if table else list(index.groups())
    for name in names:
        if not name.lower().startswith(prefix):
            refuse(f"names must start with {prefix!r}")
    for ref in _DDL_REFERENCES.findall(statement):
        if not ref.lower().startswith(prefix):
            refuse("references only the plug-in's own tables")
    if re.search(r"\bas\s+select\b", statement, re.IGNORECASE):
        refuse("no as select")
    return statement.strip()


def _ddl_parts(statement: str) -> tuple[str, str, str | None, list[str]]:
    """A checked statement's kind (`table` or `index`), its name, the table an index
    is on, and the tables it references, all lower-case."""
    refs = [r.lower() for r in _DDL_REFERENCES.findall(statement)]
    table = _DDL_TABLE.match(statement)
    if table is not None:
        return "table", table.group(1).lower(), None, refs
    index = _DDL_INDEX.match(statement)
    return "index", index.group(1).lower(), index.group(2).lower(), refs


_CORE_TABLE = re.compile(
    r"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?([a-z_][a-z0-9_]*)", re.IGNORECASE
)
_CORE_INDEX = re.compile(
    r"\bcreate\s+(?:unique\s+)?index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?"
    r"([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)


def _core_names() -> tuple[set[str], set[str]]:
    """The core's own table and index names, read from its schema files (the
    Postgres one holds every table; the SQLite one adds nothing new, but is read
    too so a name only it has is still covered)."""
    tables: set[str] = set()
    indexes: set[str] = set()
    for name in ("schema.sql", "schema_sqlite.sql"):
        text = resources.files("ream").joinpath(name).read_text()
        tables.update(m.lower() for m in _CORE_TABLE.findall(text))
        indexes.update(m.lower() for m in _CORE_INDEX.findall(text))
    return tables, indexes


def _check_against(catalog_tables: set[str], catalog_indexes: set[str]) -> None:
    """Refuse a plug-in statement that would touch something not its own: a table or
    index name that is a core one (from the schema files, or anything in the
    catalog that no loaded plug-in declares), or an index `on` or a `references`
    naming a table that the same plug-in's own statements do not create. A prefix
    alone cannot promise this: a plug-in named `agent` has the prefix of `agent_key`."""
    own: dict[str, set[str]] = {}
    declared_tables: set[str] = set()
    declared_indexes: set[str] = set()
    for plugin, statement in _SCHEMA:
        kind, name, _on, _refs = _ddl_parts(statement)
        if kind == "table":
            own.setdefault(plugin, set()).add(name)
            declared_tables.add(name)
        else:
            declared_indexes.add(name)
    core_tables, core_indexes = _core_names()
    foreign_tables = core_tables | (catalog_tables - declared_tables)
    foreign_names = foreign_tables | core_indexes | (catalog_indexes - declared_indexes)
    for plugin, statement in _SCHEMA:
        kind, name, on, refs = _ddl_parts(statement)

        def refuse(why: str, plugin=plugin):
            raise PluginError(f"plug-in {plugin!r}: schema statement refused: {why}")

        if name in foreign_names:
            refuse("a name the core or another owner already uses")
        mine = own.get(plugin, set())
        if on is not None and on not in mine:
            refuse("an index only on the plug-in's own tables")
        if any(ref not in mine for ref in refs):
            refuse("references only the plug-in's own tables")


def apply_schema(db) -> None:
    """Create every plug-in's tables (idempotent). `serve` calls it after `load()`; a
    failure stops start-up, like any other registration fault, naming the plug-in and
    never the statement's text."""
    if not _SCHEMA:
        return
    try:
        db.ensure_plugin_tables(list(_SCHEMA), _check_against)
    except PluginError:
        raise
    except dbmod.PluginTableError as exc:
        raise PluginError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - the database could not be reached
        raise PluginError(
            f"plug-in schema could not be created: {type(exc).__name__}"
        ) from None


def start_all(db) -> None:
    """Call every plug-in's start hook, each isolated."""
    for plugin, fn in tuple(_STARTS):
        try:
            fn(db)
        except Exception as exc:  # noqa: BLE001 - a plug-in's fault is isolated
            _count(plugin, "start")
            _log.warning("plug-in %s: start failed: %s", plugin, type(exc).__name__)


STOP_SECONDS = 2.0  # each stop hook's limit, as the core's own sender gets


def stop_all(timeout: float = STOP_SECONDS) -> None:
    """Call every plug-in's stop hook, last registered first, each isolated and each
    given `timeout` seconds: it runs in a daemon thread, and one still running after
    that is left behind, logged and counted as a failure, so a stuck plug-in cannot
    keep the core's own sender from stopping."""
    for plugin, fn in reversed(list(_STOPS)):
        failed: list[str] = []

        def run(fn=fn, failed=failed):
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - a plug-in's fault is isolated
                failed.append(type(exc).__name__)

        worker = threading.Thread(target=run, name=f"plugin-stop-{plugin}", daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            _count(plugin, "stop")
            _log.warning("plug-in %s: stop did not return in %ss", plugin, timeout)
        elif failed:
            _count(plugin, "stop")
            _log.warning("plug-in %s: stop failed: %s", plugin, failed[0])


_HOST_NAME = re.compile(
    r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+"
)


def routers() -> dict[str, Callable]:
    """The ASGI app each plug-in route is served by, keyed by its normalised host. `create_app`
    reads this when it builds the host router; empty with no plug-in."""
    return {host: app for host, (_p, app) in _ROUTERS.items()}


def _reserved_host(host: str) -> str | None:
    """Why a plug-in may not serve `host`, or None. Read at call time, from the settings."""
    if hosts.is_gallery(host):
        return "the gallery host"
    if hosts.is_api(host):
        return "the API host"
    if host == hosts.normalise(settings.current().partner_inbound_host):
        return "the partner inbound host"
    content = store.content_host()
    if host == content or host.endswith("." + content):
        return "the content domain"
    return None


def _guard_app(plugin: str, app: Callable) -> Callable:
    """Wrap a plug-in's ASGI app so a fault in it answers a bare 500 and is counted and
    logged by exception type only (the message may hold a request's content), instead of
    reaching the server's traceback log."""
    import anyio
    from starlette.requests import ClientDisconnect
    from starlette.responses import PlainTextResponse

    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                # No plug-in websockets: refuse the handshake cleanly.
                await receive()
                await send({"type": "websocket.close", "code": 1008})
            else:
                await app(scope, receive, send)
            return
        started = False
        disconnected = False

        async def tracking(message):
            nonlocal started, disconnected
            if message["type"] == "http.response.start":
                started = True
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"x-content-type-options"
                ]
                headers.append((b"x-content-type-options", b"nosniff"))
                message = {**message, "headers": headers}
            try:
                await send(message)
            except OSError:
                disconnected = True
                raise

        try:
            await app(scope, receive, tracking)
        except Exception as exc:  # noqa: BLE001 - a plug-in's fault is isolated
            if disconnected or isinstance(
                exc,
                (
                    ClientDisconnect,
                    anyio.ClosedResourceError,
                    anyio.BrokenResourceError,
                ),
            ):
                return  # the client went away: not the plug-in's fault
            _count(plugin, "router")
            _log.warning("plug-in %s: route failed: %s", plugin, type(exc).__name__)
            if not started:
                await PlainTextResponse(
                    "plug-in error",
                    status_code=500,
                    headers={"x-content-type-options": "nosniff"},
                )(scope, receive, send)

    return guarded


def _entry_points() -> list:
    """The installed `ream.plugins` and (for good) `artifacts.plugins` entry points. Where one
    name is in both groups the `ream.plugins` one wins. Tests replace this."""
    found = {ep.name: ep for ep in metadata.entry_points(group=OLD_GROUP)}
    found.update({ep.name: ep for ep in metadata.entry_points(group=GROUP)})
    return list(found.values())


_requires_major = plugin_cli.requires_major


def load() -> list[str]:
    """Register every plug-in `REAM_PLUGINS` selects, once, in name order.
    Raises PluginError (naming the plug-in) and loads nothing further on a failure."""
    done = plugin_cli.register_all(
        _entry_points(), settings.current().plugins, PluginAPI, _log
    )
    # One plug-in's names (tables, variables) must never fall under another's: `acme`
    # would otherwise own every `acme_chat_...` name of `acme-chat`.
    for one in done:
        for other in done:
            if one != other and _prefix(other).startswith(_prefix(one)):
                raise PluginError(
                    f"plug-in {other!r}: its name prefix {_prefix(other)!r} falls under"
                    f" plug-in {one!r}'s {_prefix(one)!r}; rename one of them"
                )
    _loaded[:] = done
    return done
