import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

SCHEMA_VERSION = 1

# An arbitrary, fixed bigint naming this migration's advisory lock -- distinct from any
# other advisory lock this service might one day take. pg_advisory_xact_lock is
# per-database and released automatically when the transaction ends (commit or
# rollback), so a crash mid-migration can never leave it held.
_MIGRATE_LOCK_KEY = 0x61727466_61637473  # "artfacts" in hex-ish, just needs to be fixed


# A connect that can block forever turns a bounded wait into an unbounded one.
# `events.wait_for_agent` opens one of these per look, twice a second, per
# waiter; with no timeout a single slow connect -- a Postgres failover, a
# cluster switchover, a deploy -- parks the loop past a deadline it has already
# agreed with the caller. Five seconds is far longer than a LAN connect and far
# shorter than any hold, so it bounds the pathological case without touching the
# ordinary one. Only applied when the DSN does not already say otherwise.
_CONNECT_TIMEOUT_SECONDS = 5


def _with_connect_timeout(dsn: str) -> str:
    if "connect_timeout" in dsn:
        return dsn
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}connect_timeout={_CONNECT_TIMEOUT_SECONDS}"


# The errors callers catch, named here so no module outside this one imports a
# driver (the SQLite switch, slice 3). Tuples, so slice 5 can add sqlite3's.
#   TRANSIENT: the database could not be reached or dropped the connection.
#   DUPLICATE_KEY: an insert collided with a primary key or unique index.
#   INTEGRITY: any constraint refused a write (tests name it; code catches
#   DUPLICATE_KEY).


class DuplicateKey(Exception):
    """SQLite's primary-key or unique clash, told apart from its other
    constraint errors by the error name (a foreign key or check is not one)."""


TRANSIENT: tuple[type[Exception], ...] = (
    psycopg.OperationalError,
    sqlite3.OperationalError,
)
DUPLICATE_KEY: tuple[type[Exception], ...] = (UniqueViolation, DuplicateKey)
INTEGRITY: tuple[type[Exception], ...] = (
    psycopg.IntegrityError,
    sqlite3.IntegrityError,
    DuplicateKey,
)


class DataError(ValueError):
    """A parameter no database column can hold: a NUL character, or a lone
    surrogate (half of a UTF-16 pair, which JSON can deliver). Refused here,
    before the statement is sent, for every string and every JSON value, so
    the refusal is the same whichever database is underneath. Callers that
    let it through answer 500, as they did when Postgres raised it."""


def refuse_unstorable(value: str) -> None:
    if "\x00" in value:
        raise DataError("a NUL character cannot be stored")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise DataError("a lone surrogate cannot be stored") from None


def _refuse_in_json(value) -> None:
    if isinstance(value, str):
        refuse_unstorable(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                refuse_unstorable(key)
            _refuse_in_json(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _refuse_in_json(item)


class Rollback(Exception):
    """Raised inside `conn.transaction()` to undo it: the innermost transaction
    rolls back and the exception goes no further."""


class Json:
    """A JSON parameter, encoded by whichever backend runs the statement."""

    __slots__ = ("value",)

    def __init__(self, value) -> None:
        self.value = value


def jsonb(value) -> Json:
    """A JSON parameter: a dict or list stored in a JSON column. Every string
    in it, keys included, is checked as a plain string parameter is."""
    _refuse_in_json(value)
    return Json(value)


class Result:
    """A statement's rows, fetched before `execute` returns, and its row count.
    Eager, so no open cursor ever outlives its statement (on SQLite an
    unfinished `update ... returning` would hold the write lock)."""

    __slots__ = ("_next", "_rows", "rowcount")

    def __init__(self, rows: list, rowcount: int) -> None:
        self._rows = rows
        self._next = 0
        self.rowcount = rowcount

    def fetchone(self):
        if self._next >= len(self._rows):
            return None
        self._next += 1
        return self._rows[self._next - 1]

    def fetchall(self) -> list:
        rest = self._rows[self._next :]
        self._next = len(self._rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    """One connection, in autocommit until `transaction()`. The only object
    the rest of the service talks to the database through."""

    def __init__(self, raw: psycopg.Connection) -> None:
        self._raw = raw

    def execute(self, query: str, params=None) -> Result:
        for value in params or ():
            if isinstance(value, str):
                refuse_unstorable(value)
        if params:
            params = [Jsonb(v.value) if isinstance(v, Json) else v for v in params]
        cur = self._raw.execute(query, params)
        rows = cur.fetchall() if cur.description is not None else []
        return Result(rows, cur.rowcount)

    @contextmanager
    def transaction(self) -> Iterator["Connection"]:
        """A transaction, or a savepoint when one is already open. `Rollback`
        raised inside undoes it and is swallowed; any other exception undoes
        it and propagates."""
        with self._raw.transaction() as tx:
            try:
                yield self
            except Rollback:
                raise psycopg.Rollback(tx) from None

    def for_update(self, suffix: str = "") -> str:
        """A row-lock clause for a select inside a transaction, such as
        `for update of decision`. SQLite will need none: its transactions
        take the write lock up front."""
        return f" for update{suffix}"

    def for_share(self) -> str:
        """A shared row lock: other readers proceed, a writer of the row waits
        for this transaction. None on SQLite, as with `for_update`."""
        return " for share"

    def advisory_lock(self, key: int) -> None:
        """Serialise this transaction against any other taking the same key;
        released when the transaction ends."""
        self._raw.execute("select pg_advisory_xact_lock(%s)", (key,))

    def byte_length(self, column: str) -> str:
        """The size in bytes of a text column, not its length in characters."""
        return f"octet_length({column})"


# --- SQLite -----------------------------------------------------------------
#
# One format for every stored time, so comparing and ordering them as text is
# correct: UTC, six fractional digits even when zero. A naive datetime is UTC
# (`said_at` arrives from callers that way).
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f+00:00"


def _time_text(value: datetime) -> str:
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.strftime(_TIME_FORMAT)


sqlite3.register_adapter(datetime, _time_text)
sqlite3.register_adapter(Json, lambda j: json.dumps(j.value, ensure_ascii=False))
sqlite3.register_converter("TIMESTAMPTZ", lambda b: datetime.fromisoformat(b.decode()))
sqlite3.register_converter("BOOLEAN", lambda b: b not in (b"0", b""))
sqlite3.register_converter("JSONB", lambda b: json.loads(b))

_DUPLICATE_NAMES = {"SQLITE_CONSTRAINT_PRIMARYKEY", "SQLITE_CONSTRAINT_UNIQUE"}


class SqliteConnection(Connection):
    """The same contract on SQLite: autocommit until `transaction()`, which
    opens BEGIN IMMEDIATE (one writer at a time, taken before the first read,
    so every read-then-write path is serialised); a nested one is a
    savepoint."""

    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw
        self._depth = 0

    def execute(self, query: str, params=None) -> Result:
        for value in params or ():
            if isinstance(value, str):
                refuse_unstorable(value)
        try:
            cur = self._raw.execute(query.replace("%s", "?"), tuple(params or ()))
        except sqlite3.IntegrityError as exc:
            if exc.sqlite_errorname in _DUPLICATE_NAMES:
                raise DuplicateKey(str(exc)) from exc
            raise
        rows = cur.fetchall() if cur.description is not None else []
        return Result(rows, cur.rowcount)

    @contextmanager
    def transaction(self) -> Iterator["Connection"]:
        depth = self._depth
        savepoint = f"sp{depth}"
        self._raw.execute("begin immediate" if depth == 0 else f"savepoint {savepoint}")
        self._depth += 1
        try:
            yield self
        except BaseException as exc:
            self._depth = depth
            if depth == 0:
                self._raw.execute("rollback")
            else:
                self._raw.execute(f"rollback to {savepoint}")
                self._raw.execute(f"release {savepoint}")
            if isinstance(exc, Rollback):
                return
            raise
        self._depth = depth
        self._raw.execute("commit" if depth == 0 else f"release {savepoint}")

    def for_update(self, suffix: str = "") -> str:
        return ""

    def for_share(self) -> str:
        return ""

    def advisory_lock(self, key: int) -> None:
        return None

    def byte_length(self, column: str) -> str:
        return f"length(cast({column} as blob))"


def _sqlite_path(dsn: str) -> str:
    """`sqlite:///relative.db` or `sqlite:////absolute/path.db`."""
    return dsn.removeprefix("sqlite:///")


def _statements(script: str) -> list[str]:
    """A schema file's statements, one by one: comments dropped, split at a
    semicolon that ends a line (no statement in our schema has one inside)."""
    lines = [line for line in script.splitlines() if not line.lstrip().startswith("--")]
    return [
        part.strip()
        for part in re.split(r";\s*$", "\n".join(lines), flags=re.MULTILINE)
        if part.strip()
    ]


def _widen_answer_sources(conn) -> None:
    """Step 6: add 'slack' to the answer_source CHECK by editing the stored
    table text (SQLite cannot alter a CHECK). Runs inside the migration's
    transaction and checks its own work: if the stored SQL does not contain the
    widened list afterwards (different whitespace, a hand-edited schema) it
    raises, the transaction rolls back and user_version stays where it was, so
    start-up fails loudly instead of marking a no-op as done. The pragma is
    always switched off again. No integrity_check: the database is large."""
    old = "'in-person', 'other')"
    new = "'in-person', 'other', 'slack')"
    conn.execute("pragma writable_schema = on")
    try:
        conn.execute(
            "update sqlite_master set sql = replace(sql, "  # noqa: S608 -- fixed text
            + _sql_text(old)
            + ", "
            + _sql_text(new)
            + ") where type = 'table' and name = 'decision'"
        )
    finally:
        conn.execute("pragma writable_schema = off")
    row = conn.execute(
        "select sql from sqlite_master where type = 'table' and name = 'decision'"
    ).fetchone()
    if row is None or new not in row[0]:
        raise RuntimeError(
            "schema step 6 could not widen decision.answer_source's CHECK to "
            "include 'slack': the stored table text is not the expected shape"
        )


def _sql_text(text: str) -> str:
    """`text` as an SQL string literal (fixed text only, never caller input)."""
    return "'" + text.replace("'", "''") + "'"


# Step 1 is `schema_sqlite.sql`, run whole on a fresh file. Every later change is
# a numbered step here, run statement by statement, never an edit to that file.
_SQLITE_STEPS: dict[int, tuple[str, ...]] = {
    # 21 Oct 2026: the session label an agent's comment was written under.
    2: ("alter table comment add column session text",),
    # 21 Oct 2026: what a question carries (context, topic, depends_on,
    # replaces, urgency, the offices that also need the answer).
    3: (
        "alter table decision add column context text not null default ''",
        "alter table decision add column topic_id text",
        "alter table decision add column topic_title text",
        "alter table decision add column depends_on jsonb not null default '[]'",
        "alter table decision add column replaces text",
        "alter table decision add column replaced_by text",
        "alter table decision add column urgency text not null default 'when_you_can'",
        "alter table decision add column blocked_on text",
        "alter table decision add column needed_by timestamptz",
        "alter table decision add column for_offices jsonb not null default '[]'",
    ),
    # 21 Oct 2026: the partner outbox and the per-question event counter.
    4: (
        "alter table decision add column partner_version integer not null default 0",
        (
            "create table partner_outbox ("
            " event_id text primary key,"
            " decision_id text not null,"
            " version integer not null,"
            " kind text not null,"
            " body jsonb not null check (json_valid(body)),"
            " created_at timestamptz not null,"
            " attempts integer not null default 0,"
            " next_attempt_at timestamptz not null,"
            " claimed_until timestamptz,"
            " delivered_at timestamptz,"
            " last_error text,"
            " last_error_at timestamptz,"
            " unique (decision_id, version))"
        ),
        (
            "create index partner_outbox_due_idx on partner_outbox (next_attempt_at)"
            " where delivered_at is null"
        ),
    ),
    # 21 Oct 2026: a refusal retrying cannot change parks the outbox row.
    5: ("alter table partner_outbox add column parked_at timestamptz",),
    # 21 Oct 2026: the partner bridge, inbound. 'slack' joins the answer
    # sources (a CHECK cannot be altered in SQLite, so the stored table text is
    # widened in place, the documented way to relax a constraint), and the copy
    # of a partner-home question with the dedupe records.
    6: (
        _widen_answer_sources,
        (
            "create table partner_question ("
            " source text primary key,"
            " version integer not null,"
            " asking_office text not null,"
            " title text not null,"
            " options jsonb not null check (json_valid(options)),"
            " recommended text,"
            " context text not null default '',"
            " link text,"
            " topic_id text,"
            " topic_title text,"
            " depends_on jsonb not null default '[]',"
            " replaces text,"
            " replaced_by text,"
            " urgency text not null default 'when_you_can',"
            " blocked_on text,"
            " needed_by timestamptz,"
            " for_offices jsonb not null default '[]',"
            " state text not null default 'open'"
            " check (state in ('open', 'answered', 'withdrawn', 'replaced')),"
            " answer text,"
            " answer_note text not null default '',"
            " answered_by text,"
            " answered_at timestamptz,"
            " pending_answer text,"
            " pending_note text not null default '',"
            " pending_at timestamptz,"
            " out_version integer not null default 0,"
            " created_at timestamptz not null,"
            " updated_at timestamptz not null)"
        ),
        (
            "create table partner_version_seen ("
            " source text primary key,"
            " version integer not null,"
            " updated_at timestamptz not null)"
        ),
        (
            "create table partner_event_seen ("
            " event_id text primary key,"
            " source text not null,"
            " version integer not null,"
            " kind text not null,"
            " outcome text not null,"
            " detail jsonb,"
            " seen_at timestamptz not null)"
        ),
        "create index partner_event_seen_at_idx on partner_event_seen (seen_at)",
    ),
    # 26 Oct 2026: every version kept, stored as zstd deltas (see schema.sql).
    7: (
        "alter table version add column packed blob",
        "alter table version add column codec text",
        "alter table version add column base_n integer",
        "alter table version add column body_bytes integer",
        "alter table version add column body_sha256 text",
    ),
    # 29 Oct 2026: a session holding an /events wait right now (see schema.sql).
    8: ("alter table seat_read add column held_until timestamptz",),
    # 29 Oct 2026: an agent's own pickup line (see schema.sql).
    9: (
        (
            "create table agent_pickup ("
            " agent text primary key,"
            " pickup text not null,"
            " updated_at timestamptz not null)"
        ),
    ),
}
SQLITE_SCHEMA_VERSION = max(_SQLITE_STEPS)

# NORMAL in service since 20 Oct 2026 (Jeremy's choice, runcove-5t3oj.34): in
# WAL mode a commit no longer waits for the disk. On this volume (DRBD protocol
# C, the pod's node diskless) FULL cost about 7 ms a commit, a network round
# trip each; NORMAL about 0.01 ms. The trade: a power cut or kernel panic on the
# node can lose the last few seconds of commits not yet checkpointed. A pod
# crash loses nothing, and the database is never corrupted (checkpoints still
# sync). The test suite turns it off for its throwaway files, as it turns fsync
# off for its throwaway Postgres (tests/conftest.py).
SQLITE_SYNCHRONOUS = "normal"
# Per connection: a 16 MiB page cache (SQLite's default is about 2 MiB) and up
# to 256 MiB of the file read through memory-mapping, which is the kernel's page
# cache, reclaimable, not the process's own memory. The file is 3.2 GB; the hot
# part (page lists, decisions, current versions) is far smaller. Sized against
# the pod's 1 GiB, which the thumbnail browser shares: the idle list is capped,
# but every in-flight request holds its own connection (the threadpool allows
# about 40), so 8 MiB keeps even that worst case near 320 MiB, and a cache only
# fills as pages are read.
SQLITE_CACHE_KIB = 8 * 1024
SQLITE_MMAP_BYTES = 256 * 1024 * 1024
# Idle connections kept for reuse. Opening one per request threw its cache
# away every time and re-ran its setup; reuse keeps both.
SQLITE_POOL_MAX = 4


def in_list(values) -> str:
    """The `(%s, %s, …)` of an expanded `in`, one placeholder per value, for SQL
    that both Postgres and SQLite read (SQLite has no arrays, so no `= any(%s)`).
    An empty list gives `(null)`, which matches nothing, as `= any('{}')` did."""
    n = len(values)
    return "(" + ", ".join(["%s"] * n) + ")" if n else "(null)"


class Database:
    def __init__(self, dsn: str) -> None:
        self.sqlite = dsn.startswith("sqlite:")
        self.dsn = dsn if self.sqlite else _with_connect_timeout(dsn)
        # SQLite only: one idle connection held for the life of this object
        # (design doc, "Checkpoints"). When the LAST connection to a WAL database
        # closes, SQLite checkpoints the WAL into the file and deletes it, two
        # fsyncs; every request opens and closes its own short connections, so
        # without this every request paid them. Under disk pressure each took
        # seconds (runcove-5t3oj.20.10: a claim and a 1 s held poll both took
        # over 6 s in CI). Opened once the database is in WAL mode, and it reads
        # once so it holds the WAL index; it never runs a query after that.
        self._keeper: sqlite3.Connection | None = None
        self._keeper_lock = threading.Lock()
        # SQLite only: idle request connections, reused (runcove-5t3oj.34).
        self._idle: list[sqlite3.Connection] = []
        self._idle_lock = threading.Lock()
        self._closed = False

    def _keep_open(self, raw: sqlite3.Connection) -> None:
        if self._keeper is not None:
            return
        if raw.execute("pragma journal_mode").fetchone()[0] != "wal":
            return
        with self._keeper_lock:
            if self._keeper is None:
                keeper = sqlite3.connect(
                    _sqlite_path(self.dsn), check_same_thread=False
                )
                keeper.execute("pragma busy_timeout = 5000")
                keeper.execute("select 1 from sqlite_master limit 1").fetchall()
                self._keeper = keeper

    def _open_sqlite(self) -> sqlite3.Connection:
        raw = sqlite3.connect(
            _sqlite_path(self.dsn),
            isolation_level=None,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            check_same_thread=False,
        )
        raw.create_function("artifacts_now", 0, lambda: _time_text(datetime.now(UTC)))
        raw.execute("pragma busy_timeout = 5000")
        raw.execute(f"pragma synchronous = {SQLITE_SYNCHRONOUS}")
        raw.execute(f"pragma cache_size = -{SQLITE_CACHE_KIB}")
        raw.execute(f"pragma mmap_size = {SQLITE_MMAP_BYTES}")
        return raw

    def _checkout(self) -> sqlite3.Connection:
        with self._idle_lock:
            raw = self._idle.pop() if self._idle else None
        if raw is None:
            raw = self._open_sqlite()
        try:
            # Every checkout, not only the first: reset() and the migration
            # turn foreign keys off on the connection they use, and a reused
            # connection must not carry that into the next request.
            raw.execute("pragma foreign_keys = on")
            self._keep_open(raw)
        except BaseException:
            raw.close()
            raise
        return raw

    def _checkin(self, raw: sqlite3.Connection, broken: bool) -> None:
        """Back to the idle list, or closed: a connection that raised a database
        error, or is still inside a transaction, is never reused. An ordinary
        refusal (a 404 raised inside the `with`) keeps its connection."""
        if not broken and not raw.in_transaction:
            with self._idle_lock:
                if not self._closed and len(self._idle) < SQLITE_POOL_MAX:
                    self._idle.append(raw)
                    return
        try:
            if raw.in_transaction:
                raw.rollback()
        finally:
            raw.close()

    def close(self) -> None:
        """Close every idle connection (and the keeper). For tests and shutdown;
        a connection in use is closed when it is handed back."""
        with self._idle_lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for raw in idle:
            raw.close()
        with self._keeper_lock:
            if self._keeper is not None:
                self._keeper.close()
                self._keeper = None

    def optimize(self) -> None:
        """Refresh the query planner's statistics (SQLite's own advice: run
        `pragma optimize` periodically). Cheap; a no-op on Postgres, whose
        autovacuum does this."""
        if self.sqlite:
            with self.connect() as conn:
                # Bounded: on a 3.2 GB file an unbounded ANALYZE could hold a
                # read snapshot for a long time. 400 rows a table is SQLite's
                # own suggestion; put back after, as the connection is reused.
                conn.execute("pragma analysis_limit = 400")
                try:
                    conn.execute("pragma optimize")
                finally:
                    conn.execute("pragma analysis_limit = 0")

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        if self.sqlite:
            raw = self._checkout()
            broken = False
            try:
                yield SqliteConnection(raw)
            except sqlite3.Error:
                broken = True
                raise
            except GeneratorExit:
                broken = True  # abandoned mid-use: its state is unknown
                raise
            finally:
                self._checkin(raw, broken)
            return
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            yield Connection(conn)

    def migrate(self) -> None:
        """Idempotent (re-running is a no-op) AND race-safe: two pods starting together
        both call this against a schema that has never been migrated. Without the
        advisory lock, `create table if not exists` and the `do $$ ... end $$` block's
        check-then-act (see schema.sql) are not atomic across two concurrent
        transactions -- both can pass their own check before either commits, and the
        loser then fails with a duplicate-object error instead of finding the object
        already there. Taking the lock inside one transaction, before touching the
        schema, serialises the two: the second migrate() blocks until the first
        commits and releases the lock, then runs against a schema that already has
        everything it was about to create."""
        if self.sqlite:
            self._migrate_sqlite()
            return
        sql = resources.files("artifacts").joinpath("schema.sql").read_text()
        with self.connect() as conn, conn.transaction():
            conn.advisory_lock(_MIGRATE_LOCK_KEY)
            conn.execute(sql)

    def _migrate_sqlite(self) -> None:
        """Numbered steps under PRAGMA user_version, each run statement by
        statement inside one BEGIN IMMEDIATE (never executescript, which
        commits on its own). Two starting processes serialise on the write
        lock, and the second finds the version already set."""
        with self.connect() as conn:
            conn._raw.execute("pragma journal_mode = wal")
            with conn.transaction():
                (version,) = conn.execute("pragma user_version").fetchone()
                if version >= SQLITE_SCHEMA_VERSION:
                    return
                if version < 1:
                    script = (
                        resources.files("artifacts")
                        .joinpath("schema_sqlite.sql")
                        .read_text()
                    )
                    for statement in _statements(script):
                        conn.execute(statement)
                    version = 1
                for number in sorted(_SQLITE_STEPS):
                    if number > version:
                        for statement in _SQLITE_STEPS[number]:
                            if callable(statement):
                                statement(conn)
                            else:
                                conn.execute(statement)
                conn.execute(f"pragma user_version = {SQLITE_SCHEMA_VERSION}")

    def reset(self) -> None:
        if self.sqlite:
            with self.connect() as conn:
                conn.execute("pragma foreign_keys = off")
                for (name,) in conn.execute(
                    "select name from sqlite_master where type = 'table'"
                    " and name not like 'sqlite_%'"
                ).fetchall():
                    conn.execute(f'drop table "{name}"')
                conn.execute("pragma user_version = 0")
            return
        with self.connect() as conn:
            conn.execute("drop schema public cascade; create schema public;")
        self.migrate()
