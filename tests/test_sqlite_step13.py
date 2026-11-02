"""SQLite schema step 13 adds `decision.answer_record` (a form's submitted answers) in place."""

from importlib import resources

from artifacts import db as dbmod
from artifacts.db import Database

_WIDE = ",\n                'decision_set_aside', 'decision_reopened')"


def _build_v12(path) -> Database:
    """A database exactly as steps 1 to 12 leave it."""
    database = Database(f"sqlite:///{path}")
    script = resources.files("artifacts").joinpath("schema_sqlite.sql").read_text()
    assert _WIDE in script
    script = script.replace(_WIDE, ")")
    with database.connect() as conn, conn.transaction():
        for statement in dbmod._statements(script):
            conn.execute(statement)
        for number in range(2, 13):
            for statement in dbmod._SQLITE_STEPS[number]:
                if callable(statement):
                    statement(conn)
                else:
                    conn.execute(statement)
        conn.execute("pragma user_version = 12")
    return database


def _columns(database) -> set[str]:
    with database.connect() as conn:
        return {r[1] for r in conn.execute("pragma table_info(decision)")}


def test_step_13_adds_the_answer_record_column_to_a_v12_database(tmp_path):
    database = _build_v12(tmp_path / "v12.db")
    assert "answer_record" not in _columns(database)
    assert "reopened_at" in _columns(database)
    database.migrate()
    with database.connect() as conn:
        assert conn.execute("pragma user_version").fetchone()[0] == (
            dbmod.SQLITE_SCHEMA_VERSION
        )
    assert dbmod.SQLITE_SCHEMA_VERSION >= 13
    assert "answer_record" in _columns(database)


def test_a_record_round_trips_through_the_new_column(tmp_path):
    database = _build_v12(tmp_path / "v12.db")
    database.migrate()
    record = {"kind": "form", "answers": {"a": ["x", "y"], "b": False}, "blank": []}
    with database.connect() as conn, conn.transaction():
        conn.execute(
            "insert into artifact (id, title, agent, read_token, created_at,"
            " updated_at) values ('a', 't', 'x', 'r', datetime('now'), datetime('now'))"
        )
        conn.execute(
            "insert into decision (id, artifact_id, version_n, question, options,"
            " kind, form_name, answer_record) values"
            " ('d', 'a', 1, 'q', '[]', 'form', 'f', %s)",
            (dbmod.jsonb(record),),
        )
        got = conn.execute(
            "select answer_record from decision where id = 'd'"
        ).fetchone()[0]
    assert got == record
