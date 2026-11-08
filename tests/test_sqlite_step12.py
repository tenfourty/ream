"""SQLite schema step 12 (a page's questions are set aside, not closed) adds
`decision.reopened_at` and widens the stored event CHECK in place, proves it did,
and is safe to run again."""

from importlib import resources

from ream import db as dbmod
from ream.db import Database

_WIDE = ",\n                'decision_set_aside', 'decision_reopened')"


def _build_v11(path) -> Database:
    """A database as steps 1 to 11 leave it: no `reopened_at`, and an event CHECK
    that does not know the two new kinds."""
    database = Database(f"sqlite:///{path}")
    script = resources.files("ream").joinpath("schema_sqlite.sql").read_text()
    assert _WIDE in script
    script = script.replace(_WIDE, ")")
    with database.connect() as conn, conn.transaction():
        for statement in dbmod._statements(script):
            conn.execute(statement)
        for number in range(2, 12):
            for statement in dbmod._SQLITE_STEPS[number]:
                if callable(statement):
                    statement(conn)
                else:
                    conn.execute(statement)
        conn.execute("pragma user_version = 11")
    return database


def _version(database) -> int:
    with database.connect() as conn:
        return conn.execute("pragma user_version").fetchone()[0]


def _shape(database):
    with database.connect() as conn:
        (event_sql,) = conn.execute(
            "select sql from sqlite_master where name = 'event'"
        ).fetchone()
        columns = {r[1] for r in conn.execute("pragma table_info(decision)")}
    return event_sql, columns


def test_step_12_adds_the_column_and_widens_the_check(tmp_path):
    database = _build_v11(tmp_path / "v11.db")
    sql, columns = _shape(database)
    assert "reopened_at" not in columns and "'decision_set_aside'" not in sql

    database.migrate()

    assert _version(database) == dbmod.SQLITE_SCHEMA_VERSION >= 12
    sql, columns = _shape(database)
    assert "reopened_at" in columns
    assert "kind" in sql  # step 21 later removed the CHECK itself
    with database.connect() as conn:
        assert conn.execute("pragma writable_schema").fetchone()[0] == 0


def test_the_widened_check_accepts_the_new_kinds(tmp_path):
    database = _build_v11(tmp_path / "v11.db")
    database.migrate()
    with database.connect() as conn, conn.transaction():
        conn.execute(
            "insert into artifact (id, title, agent, read_token, created_at,"
            " updated_at) values ('a', 't', 'x', 'r', datetime('now'),"
            " datetime('now'))"
        )
        for kind in ("decision_set_aside", "decision_reopened"):
            conn.execute(
                "insert into event (artifact_id, agent, actor, kind, created_at)"
                " values ('a', 'x', 'y', %s, datetime('now'))",
                (kind,),
            )
        # Step 21 took the CHECK out: the kind is validated in code (events.append).
        conn.execute(
            "insert into event (artifact_id, agent, actor, kind, created_at)"
            " values ('a', 'x', 'y', 'bogus', datetime('now'))"
        )


def test_step_12_run_again_changes_nothing(tmp_path):
    database = _build_v11(tmp_path / "v11.db")
    database.migrate()
    before = _shape(database)
    with database.connect() as conn, conn.transaction():
        dbmod._drop_house_checks(conn)
    assert _shape(database) == before
    database.migrate()
    assert _shape(database) == before
