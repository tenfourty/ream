import os
import shutil
import tempfile

import pgserver
import pytest

from artifacts.db import Database


@pytest.fixture(scope="session")
def dsn() -> str:
    """A real Postgres for the test session: an existing one via PG_TEST_DSN, else ephemeral."""
    existing = os.environ.get("PG_TEST_DSN")
    if existing:
        yield existing
        return
    pgdata = tempfile.mkdtemp(prefix="artifacts-pgtest-")
    # cleanup_mode="delete" (not the default "stop"): "stop" only stops the
    # server and leaves the ~59 MB pgdata directory behind. /tmp here is
    # RAM-backed, and 116 leftover directories once filled it to its quota,
    # failing later suites at initdb with "Disk quota exceeded".
    server = pgserver.get_server(pgdata, cleanup_mode="delete")
    try:
        yield server.get_uri()
    finally:
        server.cleanup()
        # Belt and braces: cleanup_mode="delete" already removes `pgdata`, but
        # an ephemeral data directory must never survive a test session even
        # if that ever fails partway (or a future pgserver version stops
        # doing it) -- ignore_errors covers the now-common case where it is
        # already gone.
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture()
def db(dsn: str) -> Database:
    database = Database(dsn)
    database.reset()  # drops and recreates the schema; tests never share state
    return database
