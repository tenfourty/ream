"""A step-39 database comes up with the gallery sign-in tables, empty and unread (people design,
slice 4; docs/agent/specs/2026-11-24-people-design.md). Both backends: SQLite runs schema step 40
once, Postgres runs the end of `schema.sql` on every start and must not redo or disturb it."""

from datetime import UTC, datetime, timedelta

import pytest

T0 = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)


def _old_shape(db) -> None:
    with db.connect() as conn:
        conn.execute("drop table person_signin")
        conn.execute("drop table person_session")
        if db.sqlite:
            conn.execute("drop table page_watch")  # agents slice 3, step 41
            conn.execute("drop table agent_person")  # agents slice 4, step 42
            conn.execute("pragma user_version = 39")


def _all(db, sql):
    with db.connect() as conn:
        return [tuple(r) for r in conn.execute(sql).fetchall()]


@pytest.mark.usefixtures("fresh_schema")
def test_a_database_from_before_the_slice_gets_both_tables_empty(db):
    _old_shape(db)
    with pytest.raises(Exception), db.connect() as conn:  # noqa: B017 -- the driver's own
        conn.execute("select 1 from person_session")
    db.migrate()
    assert _all(db, "select * from person_signin") == []
    assert _all(db, "select * from person_session") == []


@pytest.mark.usefixtures("fresh_schema")
def test_the_tables_take_rows_with_the_defaults_and_the_keys(db):
    _old_shape(db)
    db.migrate()
    with db.connect() as conn:
        conn.execute(
            "insert into person_signin (id, state_sha256, browser_sha256, oidc_nonce,"
            " oidc_verifier, expires_at) values ('s1', 'st', 'br', 'n', 'v', %s)",
            (T0 + timedelta(minutes=10),),
        )
        conn.execute(
            "insert into person_session (token_sha256, person, expires_at)"
            " values ('t1', 'jeremy', %s)",
            (T0 + timedelta(hours=12),),
        )
    assert _all(db, "select next_path from person_signin") == [("/",)]
    row = _all(db, "select created_at, last_seen_at from person_session")[0]
    assert row[0] is not None and row[1] is None
    with pytest.raises(Exception), db.connect() as conn:  # noqa: B017 -- unique state
        conn.execute(
            "insert into person_signin (id, state_sha256, browser_sha256, oidc_nonce,"
            " oidc_verifier, expires_at) values ('s2', 'st', 'br', 'n', 'v', %s)",
            (T0,),
        )


@pytest.mark.usefixtures("fresh_schema")
def test_a_second_migration_keeps_what_was_written(db):
    _old_shape(db)
    db.migrate()
    with db.connect() as conn:
        conn.execute(
            "insert into person_session (token_sha256, person, expires_at)"
            " values ('t1', 'jeremy', %s)",
            (T0,),
        )
    db.migrate()
    assert _all(db, "select token_sha256, person from person_session") == [
        ("t1", "jeremy")
    ]


def test_the_index_on_person_exists(db):
    with db.connect() as conn:
        if db.sqlite:
            names = [
                r[0]
                for r in conn.execute(
                    "select name from sqlite_master where type = 'index'"
                ).fetchall()
            ]
        else:
            names = [
                r[0]
                for r in conn.execute(
                    "select indexname from pg_indexes where tablename = 'person_session'"
                ).fetchall()
            ]
    assert "person_session_person" in names
