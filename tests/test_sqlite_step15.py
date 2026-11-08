"""SQLite schema step 15 adds `artifact.archived_via_folder` (folders slice 2) in place."""

from ream import db as dbmod
from ream.db import Database


def test_step_15_adds_the_marker_column_and_index(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'v15.db'}")
    database.migrate()
    with database.connect() as conn:
        cols = {r[1] for r in conn.execute("pragma table_info(artifact)")}
        names = {r[0] for r in conn.execute("select name from sqlite_master")}
        assert conn.execute("pragma user_version").fetchone()[0] == (
            dbmod.SQLITE_SCHEMA_VERSION
        )
    assert dbmod.SQLITE_SCHEMA_VERSION >= 15
    assert "archived_via_folder" in cols
    assert "artifact_archived_via_folder_idx" in names
