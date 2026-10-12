import json
import re
import sqlite3
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


SQLITE_SCHEMA_VERSION = 1


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

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        if self.sqlite:
            raw = sqlite3.connect(
                _sqlite_path(self.dsn),
                isolation_level=None,
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
                check_same_thread=False,
            )
            try:
                raw.create_function(
                    "artifacts_now", 0, lambda: _time_text(datetime.now(UTC))
                )
                raw.execute("pragma foreign_keys = on")
                raw.execute("pragma busy_timeout = 5000")
                raw.execute("pragma synchronous = full")
                yield SqliteConnection(raw)
            finally:
                raw.close()
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
                script = (
                    resources.files("artifacts")
                    .joinpath("schema_sqlite.sql")
                    .read_text()
                )
                for statement in _statements(script):
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
