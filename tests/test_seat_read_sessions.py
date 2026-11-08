"""An unlabelled reader's session is stored as '', not NULL (the SQLite switch,
slice 2): SQLite has no NULLS NOT DISTINCT, so one row per unlabelled seat needs
a real shared value. The migration folds NULL rows left by an older pod into
the '' row, keeping the later read."""

import pytest

from ream import events


def test_an_unlabelled_read_is_stored_with_an_empty_session(db):
    events.note_read(db, "seat-a", None)
    events.note_read(db, "seat-a", None)
    with db.connect() as conn:
        rows = conn.execute(
            "select session from seat_read where agent = 'seat-a'"
        ).fetchall()
    assert rows == [("",)]
    assert events.last_read(db, "seat-a") is not None


@pytest.mark.postgres_only  # migrates the Postgres schema
def test_the_migration_folds_a_null_session_into_the_empty_one_keeping_the_later(
    db,
):
    with db.connect() as conn:
        conn.execute("alter table seat_read alter column session drop not null")
        conn.execute(
            "insert into seat_read (agent, session, last_read_at) values"
            " ('seat-b', '', now() - interval '1 hour'),"
            " ('seat-b', null, now()),"
            " ('seat-c', null, now() - interval '2 hours')"
        )
    db.migrate()
    with db.connect() as conn:
        rows = conn.execute(
            "select agent, session, last_read_at > now() - interval '1 minute'"
            " from seat_read where agent in ('seat-b', 'seat-c') order by agent"
        ).fetchall()
        nullable = conn.execute(
            "select is_nullable from information_schema.columns"
            " where table_name = 'seat_read' and column_name = 'session'"
        ).fetchone()[0]
    assert rows == [("seat-b", "", True), ("seat-c", "", False)]
    assert nullable == "NO"
