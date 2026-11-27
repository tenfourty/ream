"""The machines page's sessions query is served by a covering index on `version`, so it never
reads the wide body/thumbnail rows. Both backends: SQLite runs schema step 43 once; Postgres runs
the end of `schema.sql` on every start."""

import pytest


def _index_exists(db) -> bool:
    with db.connect() as conn:
        if db.sqlite:
            row = conn.execute(
                "select 1 from sqlite_master where name = 'version_session_idx'"
            ).fetchone()
        else:
            row = conn.execute(
                "select 1 from pg_indexes where indexname = 'version_session_idx'"
            ).fetchone()
    return row is not None


@pytest.mark.usefixtures("fresh_schema")
def test_an_older_database_gets_the_index_and_migrating_twice_is_harmless(db):
    with db.connect() as conn:
        conn.execute("drop index version_session_idx")
        if db.sqlite:
            conn.execute("pragma user_version = 42")
    assert not _index_exists(db)
    db.migrate()
    assert _index_exists(db)
    db.migrate()
    assert _index_exists(db)


@pytest.mark.usefixtures("fresh_schema")
def test_sqlite_answers_the_sessions_query_from_the_index_alone(db):
    if not db.sqlite:
        pytest.skip("EXPLAIN QUERY PLAN is SQLite's")
    with db.connect() as conn:
        plan = " ".join(
            str(r[3])
            for r in conn.execute(
                "explain query plan select a.agent, v.session, max(v.created_at)"
                " from version v join artifact a on a.id = v.artifact_id"
                " where a.agent in (%s) and v.session is not null"
                " group by a.agent, v.session",
                ("x",),
            ).fetchall()
        )
    assert "COVERING INDEX version_session_idx" in plan
    assert "SCAN v" not in plan
