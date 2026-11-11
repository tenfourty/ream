"""`events.emit` and the core outbox (core-and-plugins design, slice 4 part 1):
the journal row, the outbox row per sink, the after-commit fan-out, the sender,
and the migration that must leave the partner's old rows alone."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from ream import db as dbmod
from ream import events, ids, partner, sinks, store
from ream.sinks import Sink

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


def test_an_exception_that_opts_in_stores_its_fixed_message(db, page, monkeypatch):
    class Fixed(Exception):
        stored_message = True

    clock = Clock()

    def broken(event):
        raise Fixed("HTTP 503")

    with sinks.isolated(Sink("one", ("decision_answered",), deliver=broken)):
        _emit(db, _event(artifact_id=page.id))
        sinks.deliver_due(db, clock)
    with db.connect() as conn:
        (error,) = conn.execute("select last_error from outbox").fetchone()
    assert error == "Fixed: HTTP 503"


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
    from ream import nudge

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
    assert args == () and kwargs["kind"] == "decision_answered"
    assert kwargs["event_id"] == str(out.seq)
    assert kwargs["title"] == "Plan" and kwargs["note"] == "his words"
    assert kwargs["letter"] == "a" and kwargs["recommended"] == "b"


def _nudge_calls(db, page, monkeypatch, *extra):
    from ream import nudge

    calls = []
    monkeypatch.setattr(nudge, "emit", lambda *a, **k: calls.append(k))
    with sinks.isolated(*sinks._builtin_sinks(), *extra):
        _emit(db, _event(artifact_id=page.id, data={"title": "Plan"})).fire()
    return calls


def test_core_doorbell_stands_down_when_a_plugin_sink_named_doorbell_exists(
    db, page, monkeypatch
):
    doorbell = Sink(
        "doorbell",
        ("decision_answered",),
        deliver=lambda event: None,
        delivery="after_commit",
    )
    assert _nudge_calls(db, page, monkeypatch, doorbell) == []


def test_core_doorbell_rings_with_no_doorbell_sink(db, page, monkeypatch):
    assert len(_nudge_calls(db, page, monkeypatch)) == 1


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
            # The tables of the steps after it go too, or their steps would find them.
            conn.execute("drop table sink_breaker")
            conn.execute("drop table plugin_failure")
            conn.execute("drop table mcp_events_seen")
            conn.execute("drop table mcp_client")
            conn.execute("drop table mcp_agent")
            conn.execute("drop table mcp_token")
            conn.execute("drop table mcp_signin")
            conn.execute("drop table mcp_app")
            conn.execute("alter table decision drop column asked_by")
            conn.execute("alter table artifact drop column comments_default")
            conn.execute("alter table comment drop column deleted_at")
            conn.execute("alter table comment drop column deleted_by")
            conn.execute("alter table connect_request drop column move")
            conn.execute("alter table connect_request drop column origin_key")
            conn.execute("alter table agent_key drop column retire_after")
            conn.execute("alter table agent_key drop column revoked_by")
            conn.execute("alter table version drop column render_verdict")
            conn.execute("alter table version drop column render_findings")
            conn.execute("alter table version drop column part_changes")
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


# --- part 2: the remaining call sites, and the part 1 review fixes ------------------


def test_builtins_register_under_the_lock_so_a_concurrent_emit_sees_both(monkeypatch):
    import threading
    import time

    real = sinks._builtin_sinks

    def slow():
        time.sleep(0.3)
        return real()

    monkeypatch.setattr(sinks, "_builtin_sinks", slow)
    monkeypatch.setattr(sinks, "_sinks", {})
    monkeypatch.setattr(sinks, "_builtins_loaded", False)
    first = threading.Thread(target=sinks.registered)
    first.start()
    time.sleep(0.05)  # the first call is inside the slow registration
    names = {s.name for s in sinks.matching("asked")}
    first.join()
    assert names == {"partner"}


def test_a_lost_lease_neither_completes_nor_fails_the_row(db, page):
    def steals_the_lease(event):
        with db.connect() as conn:
            conn.execute(
                "update outbox set claimed_until = %s",
                (datetime.now(UTC) + timedelta(hours=1),),
            )

    with sinks.isolated(Sink("one", ("decision_answered",), deliver=steals_the_lease)):
        _emit(db, _event(artifact_id=page.id))
        assert sinks.deliver_due(db) == (0, 0)
    assert (
        _outbox(db)[0][4] is None
    )  # not marked delivered by a sender without the lease


def test_a_lost_lease_does_not_fail_the_row_either(db, page):
    def broken_after_losing_the_lease(event):
        with db.connect() as conn:
            conn.execute("update outbox set delivered_at = %s", (datetime.now(UTC),))
        raise ValueError("x")

    with sinks.isolated(
        Sink("one", ("decision_answered",), deliver=broken_after_losing_the_lease)
    ):
        _emit(db, _event(artifact_id=page.id))
        assert sinks.deliver_due(db) == (0, 0)
    row = _outbox(db)[0]
    assert row[2] == 0 and row[4] is not None  # attempts untouched


def test_a_deliver_that_hangs_is_a_timeout_failure_not_a_stuck_sender(
    db, page, monkeypatch
):
    import threading

    monkeypatch.setattr(sinks, "DELIVER_TIMEOUT_SECONDS", 0.2)
    release = threading.Event()
    with sinks.isolated(
        Sink("one", ("decision_answered",), deliver=lambda e: release.wait(10))
    ):
        _emit(db, _event(artifact_id=page.id))
        assert sinks.deliver_due(db) == (0, 1)
    release.set()
    with db.connect() as conn:
        error = conn.execute("select last_error from outbox").fetchone()[0]
    assert error == "TimeoutError"


def test_start_is_idempotent_and_stop_ends_the_sender(db, monkeypatch):
    import threading

    monkeypatch.setattr(sinks, "POLL_SECONDS", 0.05)
    first = sinks.start(db)
    try:
        assert sinks.start(db) is first
        senders = [t for t in threading.enumerate() if t.name == "outbox-sender"]
        assert senders == [first]
    finally:
        sinks.stop()
    assert not first.is_alive()
    second = sinks.start(db)
    try:
        assert second is not first
    finally:
        sinks.stop()
    assert not second.is_alive()


def test_the_app_shutdown_stops_the_sender(monkeypatch):
    import asyncio

    from ream import serve

    stopped = []
    monkeypatch.setattr(sinks, "stop", lambda timeout=5.0: stopped.append(timeout))

    async def app(scope, receive, send):
        await send({"type": "lifespan.shutdown.complete"})

    sent = []

    async def send(message):
        sent.append(message["type"])

    asyncio.run(serve._arm_shutdown_signals(app)({"type": "lifespan"}, None, send))
    assert stopped and sent == ["lifespan.shutdown.complete"]


def _open_question(db, page, **kwargs):
    from ream import decisions

    return decisions.ask(
        db,
        agent="seat-a",
        artifact_id=page.id,
        question="ship it?",
        options=[{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}],
        **kwargs,
    )


def test_set_aside_and_reopen_emit_even_with_the_bridge_off(db, page):
    seen = []
    sink = Sink(
        "spy", ("set_aside", "reopened", "asked"), deliver=lambda e: seen.append(e.kind)
    )
    with sinks.isolated(sink):
        decision = _open_question(db, page)
        assert store.set_archived(db, page.id, True, agent="seat-a")
        assert store.set_archived(db, page.id, False, agent="seat-a")
        assert sinks.deliver_due(db) == (3, 0)
    assert seen == ["asked", "set_aside", "reopened"]
    with db.connect() as conn:
        rows = conn.execute("select event_json from outbox").fetchall()
    refs = {Event_ref(r[0]) for r in rows}
    assert refs == {decision.id}


def Event_ref(raw):
    return events.Event.from_json(raw).ref_id


def test_an_answer_fires_the_nudge_with_the_note_and_recommendation(
    db, page, monkeypatch
):
    from ream import decisions, nudge

    calls = []
    monkeypatch.setattr(nudge, "emit", lambda *a, **k: calls.append(k))
    decision = _open_question(db, page, recommended="a")
    decisions.answer(
        db, decision.id, letter="b", note="not yet", by="jeremy", source="page"
    )
    (call,) = calls
    assert call["kind"] == "decision_answered" and call["agent"] == "seat-a"
    assert call["note"] == "not yet" and call["letter"] == "b"
    assert call["recommended"] == "a" and call["title"] == "Plan"
    with db.connect() as conn:
        seq, note = conn.execute(
            "select seq, note from event where kind = 'decision_answered'"
        ).fetchone()
    assert call["event_id"] == str(seq) and note == "not yet"


def test_a_comment_nudge_carries_no_note_though_the_journal_has_his_words(
    db, page, monkeypatch
):
    from ream import comments, nudge

    calls = []
    monkeypatch.setattr(nudge, "emit", lambda *a, **k: calls.append(k))
    comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="hello"
    )
    (call,) = calls
    assert call["kind"] == "comment_added" and not call.get("note")
    with db.connect() as conn:
        note = conn.execute(
            "select note from event where kind = 'comment_added'"
        ).fetchone()[0]
    assert note == "hello"


def test_admin_outbox_reports_counts_by_sink(db, page, capsys, monkeypatch):
    from ream import admin

    with sinks.isolated(Sink("one", ("decision_answered",), deliver=lambda e: None)):
        _emit(db, _event(artifact_id=page.id))
        _emit(db, _event(artifact_id=page.id))
        sinks.deliver_due(db)
        _emit(db, _event(artifact_id=page.id))
    monkeypatch.setattr(admin, "_open_db", lambda: db)
    admin.cmd_outbox(None)
    assert "one\tpending=1\tparked=0\tdelivered=2" in capsys.readouterr().out
