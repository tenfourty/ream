from collections.abc import Iterator
from contextlib import contextmanager
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
TRANSIENT: tuple[type[Exception], ...] = (psycopg.OperationalError,)
DUPLICATE_KEY: tuple[type[Exception], ...] = (UniqueViolation,)


class Rollback(Exception):
    """Raised inside `conn.transaction()` to undo it: the innermost transaction
    rolls back and the exception goes no further."""


def jsonb(value) -> object:
    """A JSON parameter: a dict or list stored in a JSON column."""
    return Jsonb(value)


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


def in_list(values) -> str:
    """The `(%s, %s, …)` of an expanded `in`, one placeholder per value, for SQL
    that both Postgres and SQLite read (SQLite has no arrays, so no `= any(%s)`).
    An empty list gives `(null)`, which matches nothing, as `= any('{}')` did."""
    n = len(values)
    return "(" + ", ".join(["%s"] * n) + ")" if n else "(null)"


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = _with_connect_timeout(dsn)

    @contextmanager
    def connect(self) -> Iterator[Connection]:
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
        sql = resources.files("artifacts").joinpath("schema.sql").read_text()
        with self.connect() as conn, conn.transaction():
            conn.advisory_lock(_MIGRATE_LOCK_KEY)
            conn.execute(sql)

    def reset(self) -> None:
        with self.connect() as conn:
            conn.execute("drop schema public cascade; create schema public;")
        self.migrate()
