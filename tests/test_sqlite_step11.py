"""SQLite schema step 11 (the 'form' decision kind) widens the stored CHECK in
place, proves it did, and fails loudly (rolling back) when it cannot."""

from importlib import resources

import pytest

from ream import db as dbmod
from ream.db import Database


def _build_v10(path, *, mangle: bool = False) -> Database:
    """A database exactly as steps 1 to 10 leave it."""
    database = Database(f"sqlite:///{path}")
    script = resources.files("ream").joinpath("schema_sqlite.sql").read_text()
    if mangle:
        assert "'question', 'review')" in script
        script = script.replace("'question', 'review')", "'question','review')")
    with database.connect() as conn, conn.transaction():
        for statement in dbmod._statements(script):
            conn.execute(statement)
        for number in range(2, 11):
            for statement in dbmod._SQLITE_STEPS[number]:
                if callable(statement):
                    statement(conn)
                else:
                    conn.execute(statement)
        conn.execute("pragma user_version = 10")
    return database


def _version(database) -> int:
    with database.connect() as conn:
        return conn.execute("pragma user_version").fetchone()[0]


def test_step_11_widens_a_v10_database(tmp_path):
    database = _build_v10(tmp_path / "v10.db")
    database.migrate()
    assert _version(database) == dbmod.SQLITE_SCHEMA_VERSION
    with database.connect() as conn:
        (sql,) = conn.execute(
            "select sql from sqlite_master where name = 'decision'"
        ).fetchone()
        assert conn.execute("pragma writable_schema").fetchone()[0] == 0
        tables = {r[0] for r in conn.execute("select name from sqlite_master")}
    assert "'question', 'review', 'form')" in sql
    assert "form_name" in sql
    assert {"decision_form", "decision_field"} <= tables


def test_a_form_decision_can_be_stored_after_step_11(tmp_path):
    database = _build_v10(tmp_path / "v10.db")
    database.migrate()
    with database.connect() as conn, conn.transaction():
        conn.execute(
            "insert into artifact (id, title, agent, read_token, created_at,"
            " updated_at) values ('a', 't', 'x', 'r', datetime('now'), datetime('now'))"
        )
        conn.execute(
            "insert into decision (id, artifact_id, version_n, question, options,"
            " kind, form_name) values ('d', 'a', 1, 'q', '[]', 'form', 'f')"
        )
        with pytest.raises(Exception, match="(?i)check"):
            conn.execute(
                "insert into decision (id, artifact_id, version_n, question,"
                " options, kind) values ('e', 'a', 1, 'q', '[]', 'bogus')"
            )


def test_a_different_stored_shape_fails_loudly_and_stays_at_v10(tmp_path):
    database = _build_v10(tmp_path / "odd.db", mangle=True)
    with pytest.raises(RuntimeError, match="step 11"):
        database.migrate()
    assert _version(database) == 10
    with database.connect() as conn:
        assert conn.execute("pragma writable_schema").fetchone()[0] == 0
        tables = {r[0] for r in conn.execute("select name from sqlite_master")}
    assert "decision_form" not in tables
