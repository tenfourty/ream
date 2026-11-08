"""SQLite schema step 14 adds folders (`folder`, `folder_alias`, `artifact.folder_id`) in place."""

from importlib import resources

import pytest

from ream import db as dbmod
from ream.db import Database

_WIDE = ",\n                'decision_set_aside', 'decision_reopened')"


def _build_v13(path) -> Database:
    """A database exactly as steps 1 to 13 leave it."""
    database = Database(f"sqlite:///{path}")
    script = resources.files("ream").joinpath("schema_sqlite.sql").read_text()
    assert _WIDE in script
    script = script.replace(_WIDE, ")")
    with database.connect() as conn, conn.transaction():
        for statement in dbmod._statements(script):
            conn.execute(statement)
        for number in range(2, 14):
            for statement in dbmod._SQLITE_STEPS[number]:
                if callable(statement):
                    statement(conn)
                else:
                    conn.execute(statement)
        conn.execute("pragma user_version = 13")
    return database


def _tables(database) -> set[str]:
    with database.connect() as conn:
        return {r[0] for r in conn.execute("select name from sqlite_master")}


def _columns(database, table: str) -> set[str]:
    with database.connect() as conn:
        return {r[1] for r in conn.execute(f"pragma table_info({table})")}


def test_step_14_adds_the_folder_tables_and_column_to_a_v13_database(tmp_path):
    database = _build_v13(tmp_path / "v13.db")
    assert "folder" not in _tables(database)
    assert "folder_id" not in _columns(database, "artifact")
    assert "answer_record" in _columns(database, "decision")
    database.migrate()
    with database.connect() as conn:
        assert conn.execute("pragma user_version").fetchone()[0] == (
            dbmod.SQLITE_SCHEMA_VERSION
        )
    assert dbmod.SQLITE_SCHEMA_VERSION >= 14
    assert {"folder", "folder_alias", "artifact_folder_idx"} <= _tables(database)
    assert "folder_id" in _columns(database, "artifact")
    assert _columns(database, "folder") == {
        "id",
        "owner",
        "slug",
        "title",
        "archived_at",
        "created_at",
    }


def test_existing_pages_stay_loose_and_a_folder_round_trips(tmp_path):
    database = _build_v13(tmp_path / "v13.db")
    with database.connect() as conn, conn.transaction():
        conn.execute(
            "insert into artifact (id, title, agent, read_token, created_at,"
            " updated_at) values ('a', 't', 'x', 'r', datetime('now'), datetime('now'))"
        )
    database.migrate()
    with database.connect() as conn, conn.transaction():
        assert conn.execute("select folder_id from artifact").fetchone()[0] is None
        conn.execute(
            "insert into folder (id, owner, slug, title) values ('f', 'x', 's', 'T')"
        )
        conn.execute("update artifact set folder_id = 'f'")
        got = conn.execute(
            "select f.slug, f.created_at from artifact a join folder f"
            " on f.id = a.folder_id"
        ).fetchone()
    assert got[0] == "s"
    assert got[1] is not None, "created_at defaults on its own"


def test_the_owner_and_slug_pair_is_unique(tmp_path):
    database = _build_v13(tmp_path / "v13.db")
    database.migrate()
    with database.connect() as conn, conn.transaction():
        conn.execute(
            "insert into folder (id, owner, slug, title) values ('f', 'x', 's', 'T')"
        )
        conn.execute(
            "insert into folder (id, owner, slug, title) values ('g', 'y', 's', 'T')"
        )
        with pytest.raises(Exception, match="(?i)unique"):
            conn.execute(
                "insert into folder (id, owner, slug, title) values ('h', 'x', 's', 'T')"
            )
