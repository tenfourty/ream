"""A database from before people slice 3 comes up with each form's answer record filed under the
owner and its receipts marked as about the owner's answers (docs/agent/specs/2026-11-24-people-design.md).
Both backends: SQLite runs schema step 38 once, Postgres runs the block at the end of `schema.sql`
on every start and must not redo it."""

from datetime import UTC, datetime

import pytest

from ream import store
from ream.db import jsonb

PAGE = "<!doctype html><html><head><title>t</title></head><body>{}</body></html>"
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
RECORD = {"kind": "form", "answers": {"region": "a"}, "answered_by": "jeremy"}


def _old_shape(db) -> None:
    """Take the slice's objects away again."""
    with db.connect() as conn:
        conn.execute("drop table form_answer")
        conn.execute("alter table decision drop column addressed_to")
        conn.execute("alter table receipt drop column person")
        if db.sqlite:
            conn.execute("pragma user_version = 37")


def _seed(db, *, owner: str | None = "jeremy"):
    page = store.publish(db, "seat", "One", PAGE.format("one"))
    _old_shape(db)
    with db.connect() as conn:
        conn.execute(
            "insert into decision (id, artifact_id, version_n, question, options,"
            " created_at, kind, form_name, answer_record, answered_at, answered_by)"
            " values ('d1', %s, 1, 'q', %s, %s, 'form', 'rollout', %s, %s, 'jeremy')",
            (page.id, jsonb([]), T0, jsonb(RECORD), T1),
        )
        conn.execute(
            "insert into decision (id, artifact_id, version_n, question, options,"
            " created_at, kind, form_name) values ('d2', %s, 1, 'q', %s, %s, 'form', 'other')",
            (page.id, jsonb([]), T0),
        )
        conn.execute(
            "insert into receipt (id, subject_kind, subject_id, state, agent, created_at)"
            " values ('r1', 'decision', 'd1', 'working', 'seat', %s)",
            (T1,),
        )
        conn.execute("delete from service_meta where key = 'owner_id'")
        if owner:
            conn.execute(
                "insert into service_meta (key, value) values ('owner_id', %s)",
                (owner,),
            )
    return page


def _all(db, sql):
    with db.connect() as conn:
        return [tuple(r) for r in conn.execute(sql).fetchall()]


@pytest.mark.usefixtures("fresh_schema")
def test_a_database_from_before_the_slice_files_its_records_under_the_owner(db):
    _seed(db)
    db.migrate()
    got = _all(
        db, "select decision_id, person, answer_record, answered_at from form_answer"
    )
    assert [(g[0], g[1], g[2]) for g in got] == [("d1", "jeremy", RECORD)]
    assert got[0][3] == T1
    assert _all(db, "select person from receipt") == [("jeremy",)]
    assert _all(db, "select count(*) from decision where addressed_to is not null") == [
        (0,)
    ]
    # the old column is kept, and still says what it did
    assert _all(
        db, "select count(*) from decision where answer_record is not null"
    ) == [(1,)]


@pytest.mark.usefixtures("fresh_schema")
def test_a_second_migration_changes_nothing_and_refiles_nothing(db):
    _seed(db)
    db.migrate()
    with db.connect() as conn:
        conn.execute("delete from form_answer")  # changed afterwards
    db.migrate()
    assert _all(db, "select * from form_answer") == []
    assert _all(db, "select person from receipt") == [("jeremy",)]


@pytest.mark.usefixtures("fresh_schema")
def test_the_new_table_takes_a_second_person(db):
    _seed(db)
    db.migrate()
    with db.connect() as conn:
        conn.execute(
            "insert into form_answer (decision_id, person, answer_record, answered_at)"
            " values ('d1', 'ann', %s, %s)",
            (jsonb({"answers": {}}), T1),
        )
    assert len(_all(db, "select * from form_answer")) == 2


@pytest.mark.usefixtures("fresh_schema")
def test_rows_with_no_recorded_owner_are_refused_and_nothing_changes(db):
    _seed(db, owner=None)
    with pytest.raises(Exception):  # noqa: B017 -- a RuntimeError or the driver's own
        db.migrate()
    with pytest.raises(Exception), db.connect() as conn:  # noqa: B017 -- "no such column"
        conn.execute("select person from receipt")
    assert _all(
        db, "select count(*) from decision where answer_record is not null"
    ) == [(1,)]


@pytest.mark.usefixtures("fresh_schema")
def test_an_empty_old_database_needs_no_owner(db):
    _old_shape(db)
    with db.connect() as conn:
        conn.execute("delete from service_meta where key = 'owner_id'")
    db.migrate()
    assert _all(db, "select * from form_answer") == []
