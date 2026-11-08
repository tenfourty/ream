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
    for _ in range(n):
        sinks.breaker_record(name, False, at)


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
    assert parked == 0


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
