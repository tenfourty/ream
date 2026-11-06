"""`events.emit` and the core outbox (core-and-plugins design, slice 4 part 1):
the journal row, the outbox row per sink, the after-commit fan-out, the sender,
and the migration that must leave the partner's old rows alone."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from artifacts import db as dbmod
from artifacts import events, ids, partner, sinks, store
from artifacts.sinks import Sink

PAGE = "<!doctype html><html><head><title>t</title></head><body>hi</body></html>"


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC) + timedelta(minutes=1)  # after the row is written

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def _event(kind="decision_answered", artifact_id="art1", **kwargs):
    return events.Event(
        kind=kind,
        artifact_id=artifact_id,
        agent="seat-a",
        actor="owner",
        summary="s",
        **kwargs,
    )


@pytest.fixture()
def page(db):
    return store.publish(db, "seat-a", "Plan", PAGE)


def _emit(db, event):
    with db.connect() as conn, conn.transaction():
        return events.emit(conn, event)


def _outbox(db):
    with db.connect() as conn:
        return conn.execute(
            "select sink, event_json, attempts, next_at, delivered_at, parked_at"
            " from outbox order by created_at, id"
        ).fetchall()


def _journal(db):
    with db.connect() as conn:
        return conn.execute("select seq, kind from event order by seq").fetchall()


def test_kind_lists_are_distinct_and_the_partner_uses_the_bridge_list():
    assert not set(events.KINDS) & set(events.BRIDGE_KINDS)
    assert partner.KINDS == events.BRIDGE_KINDS
    assert "answered" in events.BRIDGE_KINDS


def test_core_registers_the_nudge_and_the_partner_as_sinks():
    by_name = {s.name: s for s in sinks.registered()}
    assert by_name["nudge"].delivery == "after_commit"
    assert "decision_answered" in by_name["nudge"].kinds
    assert by_name["partner"].delivery == "outbox"
    assert by_name["partner"].stage is not None
    assert by_name["partner"].kinds == events.BRIDGE_KINDS


def test_a_journal_kind_writes_the_journal_row_and_stamps_the_seq(db, page):
    with sinks.isolated():
        out = _emit(db, _event(artifact_id=page.id))
    assert out.seq is not None
    assert out.event.id == str(out.seq)
    assert _journal(db) == [(out.seq, "decision_answered")]
    assert _outbox(db) == []


def test_a_bridge_kind_is_not_journaled_and_gets_a_fresh_id(db, page):
    with sinks.isolated():
        out = _emit(db, _event("asked", artifact_id=page.id))
    assert out.seq is None
    assert out.event.id
    assert _journal(db) == []


def test_an_unknown_kind_is_refused(db):
    with sinks.isolated(), pytest.raises(ValueError):
        _emit(db, _event("nonsense"))


def test_each_matching_sink_gets_its_own_outbox_row_and_others_none(db, page):
    seen = []
    with sinks.isolated(
        Sink("one", ("decision_answered",), deliver=seen.append),
        Sink("two", ("decision_answered", "comment_added"), deliver=seen.append),
        Sink("other", ("comment_added",), deliver=seen.append),
    ):
        out = _emit(db, _event(artifact_id=page.id, data={"k": 1}))
    rows = _outbox(db)
    assert [r[0] for r in rows] == ["one", "two"]
    stored = events.Event.from_json(rows[0][1])
    assert stored == out.event
    assert stored.id == str(out.seq)
    assert seen == []  # nothing is delivered inside the transaction


def test_the_outbox_rows_roll_back_with_the_action(db, page):
    sink = Sink("one", ("decision_answered",), deliver=lambda e: None)

    def action():
        with db.connect() as conn, conn.transaction():
            events.emit(conn, _event(artifact_id=page.id))
            raise RuntimeError("the action failed")

    with sinks.isolated(sink), pytest.raises(RuntimeError):
        action()
    assert _outbox(db) == []
    assert _journal(db) == []


def test_the_sender_delivers_once_and_marks_the_row(db, page):
    seen = []
    with sinks.isolated(Sink("one", ("decision_answered",), deliver=seen.append)):
        out = _emit(db, _event(artifact_id=page.id))
        assert sinks.deliver_due(db) == (1, 0)
        assert sinks.deliver_due(db) == (0, 0)
    assert seen == [out.event]
    assert _outbox(db)[0][4] is not None
    assert sinks.status(db)["pending"] == {}


def test_a_failing_sink_backs_off_then_delivers_and_never_loses_the_row(db, page):
    clock = Clock()
    calls = []

    def flaky(event):
        calls.append(event.id)
        if len(calls) < 3:
            raise OSError("no route")

    with sinks.isolated(Sink("one", ("decision_answered",), deliver=flaky)):
        _emit(db, _event(artifact_id=page.id))
        assert sinks.deliver_due(db, clock) == (0, 1)
        assert sinks.deliver_due(db, clock) == (0, 0)  # waiting out the backoff
        clock.advance(seconds=sinks.BACKOFF_BASE_SECONDS + 1)
        assert sinks.deliver_due(db, clock) == (0, 1)
        clock.advance(minutes=1)
        assert sinks.deliver_due(db, clock) == (1, 0)
    assert len(set(calls)) == 1  # the same event id every time: the idempotency key
    row = _outbox(db)[0]
    assert row[2] == 3 and row[4] is not None


def test_a_row_is_parked_not_dropped_after_too_many_failures(db, page, monkeypatch):
    monkeypatch.setattr(sinks, "MAX_ATTEMPTS", 2)
    clock = Clock()

    def broken(event):
        raise ValueError("secret detail")

    with sinks.isolated(Sink("one", ("decision_answered",), deliver=broken)):
        _emit(db, _event(artifact_id=page.id))
        sinks.deliver_due(db, clock)
        clock.advance(hours=1)
        sinks.deliver_due(db, clock)
        clock.advance(hours=1)
        assert sinks.deliver_due(db, clock) == (0, 0)
        assert sinks.status(db)["parked"] == {"one": 1}
    with db.connect() as conn:
        (error,) = conn.execute("select last_error from outbox").fetchone()
    assert error == "ValueError"  # the type, never the message


def test_rows_of_an_unregistered_sink_wait_untouched(db, page):
    with sinks.isolated(Sink("one", ("decision_answered",), deliver=lambda e: None)):
        _emit(db, _event(artifact_id=page.id))
    seen = []
    with sinks.isolated(Sink("elsewhere", ("decision_answered",), deliver=seen.append)):
        assert sinks.deliver_due(db) == (0, 0)
    assert seen == []
    assert _outbox(db)[0][4] is None and _outbox(db)[0][5] is None
    # Registered again, it drains.
    with sinks.isolated(Sink("one", ("decision_answered",), deliver=seen.append)):
        assert sinks.deliver_due(db) == (1, 0)


def test_after_commit_sinks_run_on_fire_only_and_never_raise(db, page):
    seen = []

    def boom(event):
        raise RuntimeError("down")

    with sinks.isolated(
        Sink(
            "late", ("decision_answered",), deliver=seen.append, delivery="after_commit"
        ),
        Sink("bad", ("decision_answered",), deliver=boom, delivery="after_commit"),
    ):
        out = _emit(db, _event(artifact_id=page.id))
        assert seen == []
        assert _outbox(db) == []
        before = sinks.late_failures()
        out.fire()  # must not raise
        assert seen == [out.event]
        assert sinks.late_failures() == before + 1


def test_a_staging_sink_writes_in_the_transaction_and_not_to_the_core_outbox(db, page):
    staged = []
    with sinks.isolated(
        Sink("own", ("asked",), stage=lambda conn, e: staged.append((conn, e.id)))
    ):
        out = _emit(db, _event("asked", artifact_id=page.id))
    assert staged and staged[0][1] == out.event.id
    assert _outbox(db) == []


def test_event_from_json_ignores_fields_it_does_not_know():
    raw = _event(data={"a": 1}).as_json() | {"added_later": True}
    assert events.Event.from_json(json.dumps(raw)).data == {"a": 1}


def test_the_nudge_sink_passes_the_fields_nudge_emit_takes(db, page, monkeypatch):
    from artifacts import nudge

    calls = []
    monkeypatch.setattr(nudge, "emit", lambda *a, **k: calls.append((a, k)))
    out = _emit(
        db,
        _event(
            artifact_id=page.id,
            note="his words",
            data={"title": "Plan", "letter": "a", "recommended": "b"},
        ),
    )
    out.fire()
    ((args, kwargs),) = calls
    assert args == ("decision_answered",)
    assert kwargs["event_id"] == str(out.seq)
    assert kwargs["title"] == "Plan" and kwargs["note"] == "his words"
    assert kwargs["letter"] == "a" and kwargs["recommended"] == "b"


@pytest.mark.usefixtures("fresh_schema")
def test_migration_adds_the_outbox_and_leaves_partner_rows_delivering(db):
    when = datetime.now(UTC)
    event_id = ids.new_id()
    with db.connect() as conn:
        conn.execute(
            "insert into partner_outbox (event_id, decision_id, version, kind, body,"
            " created_at, next_attempt_at) values (%s, %s, %s, %s, %s, %s, %s)",
            (event_id, "d1", 1, "asked", dbmod.jsonb({"title": "t"}), when, when),
        )
        conn.execute("drop table outbox")
        if db.sqlite:
            conn.execute("pragma user_version = 19")  # the version before this table
    db.migrate()
    db.migrate()  # idempotent
    with db.connect() as conn:
        cols = {
            r[0]
            for r in conn.execute(
                "select name from pragma_table_info('outbox')"
                if db.sqlite
                else "select column_name from information_schema.columns"
                " where table_name = 'outbox'"
            ).fetchall()
        }
        assert {
            "id",
            "sink",
            "event_json",
            "created_at",
            "attempts",
            "next_at",
            "delivered_at",
        } <= cols
        (kept,) = conn.execute(
            "select count(*) from partner_outbox where event_id = %s", (event_id,)
        ).fetchone()
        assert kept == 1
    # The row written before the migration is still the partner sender's to deliver.
    rows = partner._claim(db, datetime.now(UTC) + timedelta(seconds=1), 5)
    assert [r["event_id"] for r in rows] == [event_id]
