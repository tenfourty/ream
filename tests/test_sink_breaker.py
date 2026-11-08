"""The per-sink circuit breaker (core-and-plugins design, 4.5; slice 7b part 1)."""

from datetime import UTC, datetime, timedelta

import pytest

from ream import events, sinks, store
from ream.sinks import Sink

PAGE = "<!doctype html><html><head><title>t</title></head><body>hi</body></html>"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture()
def clean():
    with sinks.isolated():
        yield


def _fail(name, n, at=T0):
    for i in range(n):
        sinks.breaker_record(name, False, at, key=f"row{i}")


def _event(artifact_id="a1"):
    return events.Event(
        kind="decision_answered", artifact_id=artifact_id, agent="seat-a", actor="owner"
    )


def test_the_breaker_opens_at_the_threshold_and_not_before(clean):
    _fail("s", sinks.BREAKER_THRESHOLD - 1)
    assert sinks.breaker_allows("s", T0)
    _fail("s", 1)
    soon = T0 + timedelta(seconds=1)
    assert not sinks.breaker_allows("s", soon)
    assert "s" in sinks.open_breakers(soon)


def test_a_success_resets_the_count(clean):
    _fail("s", sinks.BREAKER_THRESHOLD - 1)
    sinks.breaker_record("s", True, T0)
    _fail("s", sinks.BREAKER_THRESHOLD - 1)
    assert sinks.breaker_allows("s", T0)


def test_after_the_cooldown_one_trial_goes_through(clean):
    _fail("s", sinks.BREAKER_THRESHOLD)
    later = T0 + timedelta(seconds=sinks.BREAKER_COOLDOWN_SECONDS + 1)
    assert sinks.breaker_allows("s", later)  # the trial
    assert not sinks.breaker_allows("s", later)  # only one at a time
    sinks.breaker_record("s", False, later)  # it failed: open again
    assert not sinks.breaker_allows("s", later + timedelta(seconds=1))
    again = later + timedelta(seconds=sinks.BREAKER_COOLDOWN_SECONDS + 1)
    assert sinks.breaker_allows("s", again)
    sinks.breaker_record("s", True, again)  # it worked: closed
    assert sinks.breaker_allows("s", again)
    assert sinks.breaker_allows("s", again)
    assert sinks.open_breakers(again) == {}


def test_an_after_commit_sink_is_skipped_while_open(clean):
    calls = []

    def bad(event):
        calls.append(1)
        raise OSError("down")

    sink = Sink("late", ("decision_answered",), deliver=bad, delivery="after_commit")
    for _ in range(sinks.BREAKER_THRESHOLD + 3):
        sinks.deliver_late([sink], _event())
    assert len(calls) == sinks.BREAKER_THRESHOLD


def test_a_sink_with_an_open_breaker_keeps_its_rows_uncounted(db):
    page = store.publish(db, "seat-a", "Plan", PAGE)
    now = datetime.now(UTC) + timedelta(minutes=1)
    calls = []

    def bad(event):
        calls.append(1)
        raise OSError("down")

    with sinks.isolated(Sink("bad", ("decision_answered",), deliver=bad)):
        for _ in range(sinks.BREAKER_THRESHOLD + 5):
            with db.connect() as conn, conn.transaction():
                events.emit(conn, _event(page.id))
        # One pass: the rows fail one by one and the breaker opens on the threshold.
        sinks.deliver_due(db, lambda: now)
        assert len(calls) == sinks.BREAKER_THRESHOLD
        assert "bad" in sinks.status(db)["open_breakers"]
        # Past the failed rows' backoff but inside the cooldown: nothing is tried.
        soon = now + timedelta(seconds=sinks.BREAKER_COOLDOWN_SECONDS - 1)
        assert sinks.deliver_due(db, lambda: soon) == (0, 0)
        assert len(calls) == sinks.BREAKER_THRESHOLD
        # After the cooldown one trial runs; it fails and the breaker re-opens.
        after = now + timedelta(seconds=sinks.BREAKER_COOLDOWN_SECONDS + 1)
        assert sinks.deliver_due(db, lambda: after) == (0, 1)
        assert len(calls) == sinks.BREAKER_THRESHOLD + 1
    with db.connect() as conn:
        (parked,) = conn.execute(
            "select count(*) from outbox where parked_at is not null"
        ).fetchone()
        untried = conn.execute(
            "select attempts from outbox where attempts = 0"
        ).fetchall()
    assert parked == 0
    # The five rows that failed were counted; the other five were never touched.
    assert len(untried) == 10 - sinks.BREAKER_THRESHOLD


def test_a_hung_sink_is_a_timeout_failure_and_trips_the_breaker(db, monkeypatch):
    import threading

    monkeypatch.setattr(sinks, "DELIVER_TIMEOUT_SECONDS", 0.05)
    release = threading.Event()
    page = store.publish(db, "seat-a", "Plan", PAGE)
    now = datetime.now(UTC) + timedelta(minutes=1)
    with sinks.isolated(
        Sink("hung", ("decision_answered",), deliver=lambda e: release.wait(5))
    ):
        for _ in range(sinks.BREAKER_THRESHOLD + 2):
            with db.connect() as conn, conn.transaction():
                events.emit(conn, _event(page.id))
        try:
            assert sinks.deliver_due(db, lambda: now) == (0, sinks.BREAKER_THRESHOLD)
        finally:
            release.set()
        assert "hung" in sinks.status(db)["open_breakers"]
    with db.connect() as conn:
        errors = conn.execute(
            "select distinct last_error from outbox where last_error is not null"
        ).fetchall()
    assert [e[0] for e in errors] == ["TimeoutError"]


def test_a_peek_spends_no_trial(clean):
    _fail("s", sinks.BREAKER_THRESHOLD)
    later = T0 + timedelta(seconds=sinks.BREAKER_COOLDOWN_SECONDS + 1)
    assert sinks.breaker_peek("s", later)
    assert sinks.breaker_peek("s", later)
    assert sinks.breaker_reserve("s", later)  # the trial is still there
    assert not sinks.breaker_reserve("s", later)


def test_only_one_of_many_concurrent_callers_gets_the_trial(clean):
    import threading

    _fail("s", sinks.BREAKER_THRESHOLD)
    later = T0 + timedelta(seconds=sinks.BREAKER_COOLDOWN_SECONDS + 1)
    got = []
    start = threading.Barrier(8)

    def go():
        start.wait()
        got.append(sinks.breaker_reserve("s", later))

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got.count(True) == 1 and len(got) == 8


def test_a_sink_with_nothing_due_keeps_its_trial_while_another_delivers(db):
    page = store.publish(db, "seat-a", "Plan", PAGE)
    now = datetime.now(UTC) + timedelta(minutes=1)
    x_calls, y_calls = [], []

    def x_bad(event):
        x_calls.append(1)
        raise OSError("down")

    sx = Sink("x", ("decision_answered",), deliver=x_bad)
    sy = Sink("y", ("comment_added",), deliver=lambda e: y_calls.append(1))
    with sinks.isolated(sx, sy):
        for _ in range(sinks.BREAKER_THRESHOLD):
            with db.connect() as conn, conn.transaction():
                events.emit(conn, _event(page.id))
        sinks.deliver_due(db, lambda: now)
        assert "x" in sinks.status(db)["open_breakers"]
        opened_until = sinks.open_breakers(now)["x"]
        # Past the cooldown, but X's rows are still in backoff (cap 40 s < 60 s, so
        # push their next_at out): nothing of X is due, Y has a row.
        with db.connect() as conn:
            conn.execute(
                "update outbox set next_at = %s where sink = 'x'",
                (now + timedelta(hours=1),),
            )
        with db.connect() as conn, conn.transaction():
            events.emit(
                conn,
                events.Event(kind="comment_added", artifact_id=page.id, agent="seat-a"),
            )
        later = now + timedelta(seconds=sinks.BREAKER_COOLDOWN_SECONDS + 1)
        assert sinks.deliver_due(db, lambda: later) == (1, 0)
        assert len(y_calls) == 1
        # X did not re-arm: its trial is still available to the next due row.
        assert sinks.breaker_peek("x", later)
        assert sinks.open_breakers(now)["x"] == opened_until


def test_one_poison_row_does_not_open_the_breaker(clean):
    for _ in range(sinks.BREAKER_THRESHOLD + 3):
        sinks.breaker_record("s", False, T0, key="poison")
    assert sinks.breaker_peek("s", T0)
    assert sinks.open_breakers(T0) == {}
    # A second, different row failing in the same run does count.
    sinks.breaker_record("s", False, T0, key="other")
    assert not sinks.breaker_peek("s", T0 + timedelta(seconds=1))


def test_a_success_from_a_call_begun_before_the_breaker_opened_is_ignored(clean):
    started = T0
    _fail("s", sinks.BREAKER_THRESHOLD, T0 + timedelta(seconds=5))
    sinks.breaker_record("s", True, T0 + timedelta(seconds=6), started=started)
    assert not sinks.breaker_peek("s", T0 + timedelta(seconds=7))
    # A trial begun after it opened does close it.
    trial = T0 + timedelta(seconds=sinks.BREAKER_COOLDOWN_SECONDS + 10)
    assert sinks.breaker_reserve("s", trial)
    sinks.breaker_record("s", True, trial, started=trial)
    assert sinks.open_breakers(trial) == {}
