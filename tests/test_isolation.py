"""Every test gets a clean database, whether it ran first, last or after a test
that altered the schema.

The `db` fixture empties the tables between tests instead of rebuilding the
schema, so these tests guard that path. The emptiness check runs both before
and after the writer, so either order is covered in one serial run:

    uv run pytest -q -p no:randomly tests/test_isolation.py -k isolation
"""

import pytest

from artifacts import comments, decisions, events, holds, keys, store

PAGE = "<!doctype html><html><head><title>t</title></head><body>{}</body></html>"


def _tables(conn) -> list[str]:
    rows = conn.execute(
        "select tablename from pg_tables where schemaname = current_schema()"
        " order by tablename"
    ).fetchall()
    return [row[0] for row in rows]


def _row_counts(db) -> dict[str, int]:
    with db.connect() as conn:
        tables = _tables(conn)
        assert tables, "no tables at all: the schema was never built"
        return {
            table: conn.execute(f'select count(*) from "{table}"').fetchone()[0]  # noqa: S608 -- names come from pg_tables
            for table in tables
        }


def _assert_clean(db) -> None:
    counts = _row_counts(db)
    assert {t: n for t, n in counts.items() if n} == {}
    with db.connect() as conn:
        sequences = conn.execute(
            "select sequencename, last_value from pg_sequences"
            " where schemaname = current_schema()"
        ).fetchall()
        assert sequences, "expected at least event.seq's sequence"
        # last_value is NULL until nextval() has been called since the last restart.
        assert {name: value for name, value in sequences if value is not None} == {}
    # ...and the next event number is back at its start.
    published = store.publish(db, "probe", "Probe", PAGE.format("p"))
    with db.connect() as conn:
        seq = events.append(
            conn,
            artifact_id=published.id,
            agent="probe",
            actor="probe",
            kind="comment_added",
            summary="s",
        )
    assert seq == 1


def _write_every_family(db) -> None:
    keys.mint(db, agent="writer")
    page = store.publish(db, "writer", "Board", PAGE.format("b"))
    asked = decisions.ask(
        db,
        agent="writer",
        artifact_id=page.id,
        question="ship?",
        options=[{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}],
    )
    decisions.answer(db, asked.id, letter="a", note="go", by="jeremy")
    comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="hi"
    )
    with db.connect() as conn:
        events.append(
            conn,
            artifact_id=page.id,
            agent="writer",
            actor="jeremy",
            kind="comment_added",
            summary="extra",
        )
        holds.set_mode(conn, "live")
    counts = _row_counts(db)
    for table in ("agent_key", "artifact", "version", "decision", "comment", "event"):
        assert counts[table] > 0, table
    assert counts["human_pref"] == 1
    with db.connect() as conn:
        seq = conn.execute(
            "select last_value from pg_sequences where sequencename = 'event_seq_seq'"
        ).fetchone()[0]
    assert seq is not None and seq >= 2


def test_every_table_is_empty_at_the_start(db):
    assert set(_row_counts(db).values()) == {0}


def test_isolation_empty_before_the_writer(db):
    _assert_clean(db)


def test_isolation_writes_every_table_family(db):
    _write_every_family(db)


def test_isolation_empty_after_the_writer(db):
    _assert_clean(db)


# --- a test that alters the schema must not poison the next one --------------


@pytest.mark.usefixtures("fresh_schema")
def test_isolation_alters_the_schema(db):
    with db.connect() as conn:
        conn.execute("alter table artifact drop column agents_can_read")
        conn.execute("drop table human_pref")
        conn.execute("create table leftover_from_a_test (x int)")
        conn.execute("insert into leftover_from_a_test values (1)")


def test_isolation_the_next_test_sees_the_real_schema(db):
    page = store.publish(db, "owner", "After", PAGE.format("a"))
    with db.connect() as conn:
        can_read = conn.execute(
            "select agents_can_read from artifact where id = %s", (page.id,)
        ).fetchone()[0]
        assert can_read is True
        holds.set_mode(conn, "live")
        assert holds.get_mode(conn) == "live"
        assert "leftover_from_a_test" not in _tables(conn)
    _write_every_family(db)
