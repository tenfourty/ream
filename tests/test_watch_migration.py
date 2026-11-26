"""A step-40 database comes up with `page_watch`, empty and unread (agents design, slice 3;
docs/agent/specs/2026-11-26-agents-and-watching-design.md). Both backends: SQLite runs schema step 41
once, Postgres runs the end of `schema.sql` on every start and must not redo or disturb it."""

from datetime import UTC, datetime

import pytest

from ream import db as dbmod
from ream import store

T0 = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
PAGE = "<!doctype html><html><head><title>t</title></head><body>x</body></html>"


def _old_shape(db) -> None:
    with db.connect() as conn:
        conn.execute("drop table page_watch")
        if db.sqlite:
            conn.execute("pragma user_version = 40")


def _all(db, sql):
    with db.connect() as conn:
        return [tuple(r) for r in conn.execute(sql).fetchall()]


def test_step_41_is_the_highest_and_is_this_one():
    assert dbmod.SQLITE_SCHEMA_VERSION >= 41
    assert any("page_watch" in s for s in dbmod._SQLITE_STEPS[41] if isinstance(s, str))


@pytest.mark.usefixtures("fresh_schema")
def test_a_database_from_before_the_slice_gets_the_table_empty(db):
    _old_shape(db)
    with pytest.raises(Exception), db.connect() as conn:  # noqa: B017 -- the driver's own
        conn.execute("select 1 from page_watch")
    db.migrate()
    assert _all(db, "select * from page_watch") == []


@pytest.mark.usefixtures("fresh_schema")
def test_the_table_takes_rows_with_the_defaults_and_the_key(db):
    page = store.publish(db, "owner", "T", PAGE).id
    _old_shape(db)
    db.migrate()
    with db.connect() as conn:
        conn.execute(
            "insert into page_watch (agent, artifact_id) values ('reader', %s)", (page,)
        )
    row = _all(db, "select created_at, session, changed_notified_at from page_watch")[0]
    assert row[0] is not None and row[1] is None and row[2] is None
    with pytest.raises(Exception), db.connect() as conn:  # noqa: B017 -- primary key
        conn.execute(
            "insert into page_watch (agent, artifact_id) values ('reader', %s)", (page,)
        )


@pytest.mark.usefixtures("fresh_schema")
def test_the_foreign_key_refuses_a_page_that_is_not_there_and_cascades(db):
    _old_shape(db)
    db.migrate()
    with pytest.raises(Exception), db.connect() as conn:  # noqa: B017 -- foreign key
        conn.execute(
            "insert into page_watch (agent, artifact_id) values ('reader', 'nope')"
        )
    page = store.publish(db, "owner", "T", PAGE).id
    with db.connect() as conn:
        conn.execute(
            "insert into page_watch (agent, artifact_id) values ('reader', %s)", (page,)
        )
    assert store.destroy(db, page, agent="owner")
    assert _all(db, "select * from page_watch") == []


@pytest.mark.usefixtures("fresh_schema")
def test_a_second_migration_keeps_what_was_written(db):
    page = store.publish(db, "owner", "T", PAGE).id
    _old_shape(db)
    db.migrate()
    with db.connect() as conn:
        conn.execute(
            "insert into page_watch (agent, artifact_id, changed_notified_at)"
            " values ('reader', %s, %s)",
            (page, T0),
        )
    db.migrate()
    assert _all(db, "select agent, artifact_id from page_watch") == [("reader", page)]


def test_the_index_on_the_page_exists(db):
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
                    "select indexname from pg_indexes where tablename = 'page_watch'"
                ).fetchall()
            ]
    assert "page_watch_artifact" in names
