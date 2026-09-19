"""An answer that Jeremy gave and nobody took -- and, crucially, WHY.

The alert this feeds exists because of what happened on 19 September 2026: every
deploy silently cut off every agent waiting to hear from him, and each of them
reported the silence as his. An alert that said only "an answer is uncollected"
would have been true and nearly useless, because the two causes need opposite
responses -- an agent that is gone needs someone else to collect the answer; an
agent that is alive and not acting is a bug to go and look at. An alert that
mixes them gets muted, and a muted alert misses the real one.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, events, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"
_OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture()
def client(db):
    return TestClient(create_gallery_app(db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _answered(db, agent: str = "seat-one"):
    published = store.publish(db, agent, "Board", PAGE)
    row = decisions.ask(
        db,
        agent=agent,
        artifact_id=published.id,
        question="ship it?",
        options=_OPTIONS,
    )
    decisions.answer(db, row.id, letter="a", by="jeremy")
    return published, row


def test_an_answer_nobody_has_read_the_feed_for_says_nobody_is_listening(db):
    _published, row = _answered(db)

    found = decisions.uncollected(db, stuck_after_seconds=0)

    assert [(r.decision.id, r.state) for r in found] == [(row.id, "no_reader")]
    assert found[0].last_read_at is None


def test_a_seat_that_has_read_since_the_answer_is_stuck_not_absent(db):
    _published, row = _answered(db)
    events.note_read(db, "seat-one")

    found = decisions.uncollected(db, stuck_after_seconds=0)

    assert [(r.decision.id, r.state) for r in found] == [(row.id, "stuck")]
    assert found[0].last_read_at is not None


def test_a_seat_last_read_BEFORE_the_answer_is_still_nobody_listening(db):
    """The comparison is "since the answer", not "recently". A seat read an hour
    before Jeremy answered and never again is exactly as absent as one that has
    never been read at all, and calling it live would send someone looking for an
    agent that is not there."""
    events.note_read(db, "seat-one")
    _published, row = _answered(db)

    found = decisions.uncollected(db, stuck_after_seconds=0)

    assert [(r.decision.id, r.state) for r in found] == [(row.id, "no_reader")]
    assert found[0].last_read_at is not None  # it HAS read, just not since


def test_an_answer_given_a_moment_ago_is_in_flight_not_lost(db):
    """A held request returns within a second of an answer. Reporting one that is
    seconds old would make the alert fire on the system working perfectly."""
    _answered(db)

    assert decisions.uncollected(db) == []


def test_a_collected_answer_is_not_reported(db):
    _published, row = _answered(db)
    decisions.acknowledge(db, "seat-one", row.id)

    assert decisions.uncollected(db, stuck_after_seconds=0) == []


def test_closing_collects_it_too(db):
    """`close` acknowledges if nothing has, so an agent that just did the work
    never shows up as having ignored the answer."""
    _published, row = _answered(db)
    decisions.close(db, "seat-one", row.id)

    assert decisions.uncollected(db, stuck_after_seconds=0) == []


def test_an_unanswered_decision_is_not_uncollected(db):
    published = store.publish(db, "seat-one", "Board", PAGE)
    decisions.ask(
        db,
        agent="seat-one",
        artifact_id=published.id,
        question="ship it?",
        options=_OPTIONS,
    )

    assert decisions.uncollected(db, stuck_after_seconds=0) == []


def test_one_seat_never_sees_another_seats_uncollected_answer(db):
    _published, mine = _answered(db, agent="seat-one")
    _other_published, theirs = _answered(db, agent="seat-two")

    mine_only = decisions.uncollected(db, agent="seat-one", stuck_after_seconds=0)
    everyones = decisions.uncollected(db, stuck_after_seconds=0)

    assert [r.decision.id for r in mine_only] == [mine.id]
    assert {r.decision.id for r in everyones} == {mine.id, theirs.id}


def test_the_route_is_scoped_to_the_caller_and_needs_a_key(client, db):
    token, _ = keys.mint(db, agent="seat-one")
    _published, mine = _answered(db, agent="seat-one")
    _answered(db, agent="seat-two")

    assert client.get("/api/v1/decisions/uncollected").status_code == 401

    response = client.get(
        "/api/v1/decisions/uncollected?stuck_after_seconds=0", headers=auth(token)
    )

    assert response.status_code == 200
    body = response.json()
    assert [row["decision"]["id"] for row in body] == [mine.id]
    assert body[0]["state"] == "no_reader"


def test_uncollected_is_a_route_of_its_own_not_a_decision_id(client, db):
    """`/decisions/uncollected` must never be matched as a decision called
    "uncollected" by some later path-parameter route declared above it."""
    token, _ = keys.mint(db, agent="seat-one")

    response = client.get("/api/v1/decisions/uncollected", headers=auth(token))

    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_reading_the_feed_records_the_read(client, db):
    token, _ = keys.mint(db, agent="seat-one")
    assert events.last_read(db, "seat-one") is None

    client.get("/api/v1/events?since=0", headers=auth(token))

    assert events.last_read(db, "seat-one") is not None


def test_bookkeeping_that_fails_never_fails_the_read(db, monkeypatch):
    """The feed is the feature; this is the safety net that watches it. An agent
    must still get its answers when the statistics table is unhappy."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("seat_read is having a bad day")

    monkeypatch.setattr(db, "connect", _boom)

    events.note_read(db, "seat-one")  # must not raise
