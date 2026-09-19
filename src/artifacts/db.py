from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources

import psycopg

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


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = _with_connect_timeout(dsn)

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            yield conn

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
            conn.execute("select pg_advisory_xact_lock(%s)", (_MIGRATE_LOCK_KEY,))
            conn.execute(sql)

    def reset(self) -> None:
        with self.connect() as conn:
            conn.execute("drop schema public cascade; create schema public;")
        self.migrate()
