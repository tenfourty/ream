"""A database from before people slice 2 comes up with its per-person state under the owner
(docs/agent/specs/2026-11-24-people-design.md). Both backends: SQLite runs schema step 37 once,
Postgres runs the block at the end of `schema.sql` on every start and must not redo it."""

from datetime import UTC, datetime

import pytest

from ream import frame_pass, store

PAGE = "<!doctype html><html><head><title>t</title></head><body>{}</body></html>"
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def _old_shape(db) -> None:
    """Put the tables back as they were before the slice."""
    with db.connect() as conn:
        conn.execute("drop table page_pin")
        conn.execute("drop table page_read")
        conn.execute("drop table human_pref")
        conn.execute("alter table frame_pass drop column person")
        conn.execute(
            "create table page_read (artifact_id text primary key"
            " references artifact(id) on delete cascade,"
            " read_version integer not null, read_at timestamptz not null)"
        )
        conn.execute(
            "create table human_pref (key text primary key, value text not null,"
            " updated_at timestamptz not null)"
        )
        if db.sqlite:
            conn.execute("pragma user_version = 36")


def _seed(db, *, owner: str | None = "jeremy"):
    page = store.publish(db, "seat", "One", PAGE.format("one"))
    bare = store.publish(db, "seat", "Two", PAGE.format("two"))
    token = frame_pass.mint(db, page.id)  # a pass in flight when the service stopped
    with db.connect() as conn:
        conn.execute("delete from frame_pass")
    _old_shape(db)
    with db.connect() as conn:
        conn.execute(
            "insert into page_read (artifact_id, read_version, read_at)"
            " values (%s, 1, %s)",
            (page.id, T0),
        )
        conn.execute(
            "insert into human_pref (key, value, updated_at) values ('review_mode', 'live', %s)",
            (T0,),
        )
        conn.execute("update artifact set pinned_at = %s where id = %s", (T0, page.id))
        conn.execute(
            "insert into frame_pass (token_sha256, artifact_id, expires_at)"
            " values ('t', %s, %s)",
            (page.id, T0),
        )
        conn.execute("delete from service_meta where key = 'owner_id'")
        if owner:
            conn.execute(
                "insert into service_meta (key, value) values ('owner_id', %s)",
                (owner,),
            )
    assert token
    return page, bare


def _all(db, sql):
    with db.connect() as conn:
        return [tuple(r) for r in conn.execute(sql).fetchall()]


@pytest.mark.usefixtures("fresh_schema")
def test_a_database_from_before_the_slice_takes_the_owner_id(db):
    page, _ = _seed(db)
    db.migrate()
    assert _all(db, "select person, artifact_id, read_version from page_read") == [
        ("jeremy", page.id, 1)
    ]
    assert _all(db, "select person, key, value from human_pref") == [
        ("jeremy", "review_mode", "live")
    ]
    assert _all(db, "select person, artifact_id from page_pin") == [("jeremy", page.id)]
    assert _all(db, "select person from frame_pass") == [("jeremy",)]
    # the old column is kept, and still says what it did
    assert _all(db, "select count(*) from artifact where pinned_at is not null") == [
        (1,)
    ]


@pytest.mark.usefixtures("fresh_schema")
def test_a_second_migration_changes_nothing_and_refills_no_pin(db):
    page, _ = _seed(db)
    db.migrate()
    with db.connect() as conn:
        conn.execute("delete from page_pin")  # the owner unpinned it afterwards
    db.migrate()
    assert _all(db, "select * from page_pin") == []
    assert _all(db, "select person, artifact_id from page_read") == [
        ("jeremy", page.id)
    ]


@pytest.mark.usefixtures("fresh_schema")
def test_the_rebuilt_keys_take_a_second_person(db):
    page, _ = _seed(db)
    db.migrate()
    store.mark_read(db, page.id, "ann")
    store.set_pinned(db, page.id, True, agent=None, person="ann")
    with db.connect() as conn:
        conn.execute(
            "insert into human_pref (person, key, value) values ('ann', 'review_mode', 'held')"
        )
    assert len(_all(db, "select * from page_read")) == 2
    assert len(_all(db, "select * from page_pin")) == 2
    assert len(_all(db, "select * from human_pref")) == 2


@pytest.mark.usefixtures("fresh_schema")
def test_rows_with_no_recorded_owner_are_refused_and_nothing_changes(db):
    _seed(db, owner=None)
    with pytest.raises(Exception):  # noqa: B017 -- a RuntimeError or the driver's own
        db.migrate()
    # the old shapes are all still there
    assert _all(db, "select count(*) from page_read") == [(1,)]
    with db.connect() as conn:
        conn.execute("select artifact_id from page_read")  # the old shape is intact
        conn.execute("select key from human_pref")
    with pytest.raises(Exception), db.connect() as conn:  # noqa: B017 -- "no such column"
        conn.execute("select person from page_read")


@pytest.mark.usefixtures("fresh_schema")
def test_an_empty_old_database_needs_no_owner(db):
    _old_shape(db)
    with db.connect() as conn:
        conn.execute("delete from service_meta where key = 'owner_id'")
    db.migrate()
    assert _all(db, "select * from page_pin") == []
