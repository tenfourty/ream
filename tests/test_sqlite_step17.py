"""SQLite schema step 17 adds the four check columns to `artifact` (generated pages, slice 1)."""

from ream import db as dbmod
from ream.db import Database


def test_step_17_adds_the_check_columns(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'v17.db'}")
    database.migrate()
    with database.connect() as conn:
        cols = {r[1] for r in conn.execute("pragma table_info(artifact)")}
        assert conn.execute("pragma user_version").fetchone()[0] == (
            dbmod.SQLITE_SCHEMA_VERSION
        )
    assert dbmod.SQLITE_SCHEMA_VERSION >= 17
    assert {"refresh_every", "checked_at", "check_status", "check_note"} <= cols
