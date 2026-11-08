"""SQLite schema step 6 (the 'slack' answer source) widens the stored CHECK in
place, proves it did, and fails loudly (rolling back) when it cannot."""

from importlib import resources

import pytest

from ream import db as dbmod
from ream.db import Database


def _build_v5(path, *, mangle: bool = False) -> Database:
    """A database exactly as steps 1 to 5 leave it."""
    database = Database(f"sqlite:///{path}")
    script = resources.files("ream").joinpath("schema_sqlite.sql").read_text()
    if mangle:
        assert "'in-person', 'other'" in script
        script = script.replace("'in-person', 'other'", "'in-person','other'")
    with database.connect() as conn, conn.transaction():
        for statement in dbmod._statements(script):
            conn.execute(statement)
        for number in range(2, 6):
            for statement in dbmod._SQLITE_STEPS[number]:
                conn.execute(statement)
        conn.execute("pragma user_version = 5")
    return database


def _version(database) -> int:
    with database.connect() as conn:
        return conn.execute("pragma user_version").fetchone()[0]


def test_step_6_widens_a_v5_database(tmp_path):
    database = _build_v5(tmp_path / "v5.db")
    with database.connect() as conn:
        (sql,) = conn.execute(
            "select sql from sqlite_master where name = 'decision'"
        ).fetchone()
    assert "'slack'" not in sql
    database.migrate()
    assert _version(database) == dbmod.SQLITE_SCHEMA_VERSION
    with database.connect() as conn:
        (sql,) = conn.execute(
            "select sql from sqlite_master where name = 'decision'"
        ).fetchone()
        conn.execute("pragma writable_schema")  # readable afterwards
        assert conn.execute("pragma writable_schema").fetchone()[0] == 0
    # step 6 widened the CHECK and step 21 then took it out; neither leaves it behind
    assert "answer_source" in sql and "'in-person'" not in sql


def test_a_different_stored_shape_fails_loudly_and_stays_at_v5(tmp_path):
    database = _build_v5(tmp_path / "odd.db", mangle=True)
    with pytest.raises(RuntimeError, match="step 6"):
        database.migrate()
    assert _version(database) == 5
    with database.connect() as conn:
        assert conn.execute("pragma writable_schema").fetchone()[0] == 0
        tables = {
            r[0] for r in conn.execute("select name from sqlite_master").fetchall()
        }
        assert "partner_question" not in tables  # the whole step rolled back
        assert conn.execute("select count(*) from decision").fetchone()[0] == 0
