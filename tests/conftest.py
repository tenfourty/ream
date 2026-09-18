import os
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
    server = pgserver.get_server(tempfile.mkdtemp(prefix="artifacts-pgtest-"))
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture()
def db(dsn: str) -> Database:
    database = Database(dsn)
    database.reset()  # drops and recreates the schema; tests never share state
    return database
