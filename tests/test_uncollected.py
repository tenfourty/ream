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


# --- the operator's view, which sat unbuilt behind a stale comment -----------


def test_the_operator_sees_every_seat_s_stranded_answers(client, db):
    """homelab-ij8.64's second half. It was blocked on "a key that does not
    exist yet"; decisions:read-all was added for /decisions?scope=all a day
    later and nobody came back here. The blocker had been gone for a day and
    the comment saying otherwise was the only thing still enforcing it.
    """
    from artifacts import grants

    ops_token, _ = keys.mint(db, agent="ops")
    grants.grant(db, "ops", grants.READ_ALL_DECISIONS)
    mine = _stranded(db, agent="seat-a", title="Seat A's page")
    theirs = _stranded(db, agent="seat-b", title="Seat B's page")

    got = client.get(
        "/api/v1/decisions/uncollected?scope=all&stuck_after_seconds=0",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert got.status_code == 200
    ids = {row["decision"]["id"] for row in got.json()}
    assert ids == {mine.id, theirs.id}


def test_each_row_says_whose_it_is_and_on_what(client, db):
    """An alert that says "one answer is stranded" without saying whose or on
    what page sends its reader back to the database, which is how an alert
    stops being read."""
    from artifacts import grants

    ops_token, _ = keys.mint(db, agent="ops")
    grants.grant(db, "ops", grants.READ_ALL_DECISIONS)
    _stranded(db, agent="seat-a", title="The page it is on")

    row = client.get(
        "/api/v1/decisions/uncollected?scope=all&stuck_after_seconds=0",
        headers={"Authorization": f"Bearer {ops_token}"},
    ).json()[0]
    assert row["agent"] == "seat-a"
    assert row["artifact_title"] == "The page it is on"
    assert row["state"] in ("no_reader", "stuck")


def test_without_the_grant_it_is_refused_and_not_quietly_narrowed(client, db):
    """A caller that asked about every seat and got its own back would believe
    the short list -- the same lie as an empty feed from a broken wait. And the
    403 names the seat, because every agent on baba shares one key file and the
    seat a key authenticates as is not the seat its holder assumes."""
    plain_token, _ = keys.mint(db, agent="no-grant-seat")
    _stranded(db, agent="seat-a", title="Somebody else's")

    got = client.get(
        "/api/v1/decisions/uncollected?scope=all&stuck_after_seconds=0",
        headers={"Authorization": f"Bearer {plain_token}"},
    )
    assert got.status_code == 403
    assert "no-grant-seat" in got.json()["detail"]
    assert "decisions:read-all" in got.json()["detail"]


def test_the_default_is_still_only_your_own(client, db):
    """`scope` defaults to mine, so nothing a seat already runs starts seeing
    other seats' answers because this shipped."""
    from artifacts import grants

    ops_token, _ = keys.mint(db, agent="ops")
    grants.grant(db, "ops", grants.READ_ALL_DECISIONS)
    _stranded(db, agent="seat-a", title="Not ops'")

    assert (
        client.get(
            "/api/v1/decisions/uncollected?stuck_after_seconds=0",
            headers={"Authorization": f"Bearer {ops_token}"},
        ).json()
        == []
    )


def _stranded(db, *, agent: str, title: str):
    """One answered-and-never-acknowledged decision on `agent`'s page."""
    keys.mint(db, agent=agent)
    page = store.publish(db, agent, title, PAGE)
    asked = decisions.ask(
        db,
        agent=agent,
        artifact_id=page.id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    decisions.answer(db, asked.id, letter="a", note="", by="jeremy")
    return asked


# --- per session, not per machine -------------------------------------------


def test_another_session_reading_does_not_make_the_asker_look_alive(client, db):
    """THE CASE THIS FEATURE EXISTS FOR, and the one it used to get wrong.

    Three sessions on baba share one key. seat_read was keyed on the agent, so
    ANY of them reading the feed marked the seat as listening -- and a stranded
    answer would report `stuck` ("a live agent is not acting; go and look")
    when the truth was `no_reader` ("the asker is gone; collect it for them").
    Those need opposite responses, which is the whole reason the two states
    exist, so getting it backwards is worse than not labelling at all.
    """
    from artifacts import events

    keys.mint(db, agent="seat-a")
    page = store.publish(db, "seat-a", "Asks something", PAGE)
    asked = decisions.ask(
        db,
        agent="seat-a",
        artifact_id=page.id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
        session="asker/aaa",
    )
    decisions.answer(db, asked.id, letter="a", note="", by="jeremy")

    # A DIFFERENT session on the same key reads the feed, after the answer.
    events.note_read(db, "seat-a", "someone-else/bbb")

    rows = decisions.uncollected(db, agent="seat-a", stuck_after_seconds=0)
    assert len(rows) == 1
    assert rows[0].state == "no_reader", (
        "another session's read made the absent asker look alive -- this is "
        "the per-machine answer the session column was added to end"
    )

    # Now the ASKER reads. Only that turns it into a live agent not acting.
    events.note_read(db, "seat-a", "asker/aaa")
    rows = decisions.uncollected(db, agent="seat-a", stuck_after_seconds=0)
    assert rows[0].state == "stuck"


def test_a_decision_from_before_sessions_matches_the_unlabelled_reader(client, db):
    """A decision asked before sessions existed has NULL, and so does the row
    for a caller that declares nothing. `is not distinct from` matches them; a
    plain `=` is never true for NULL, which would have made every pre-session
    decision read as no_reader for ever, including ones being actively worked.
    """
    from artifacts import events

    keys.mint(db, agent="seat-a")
    page = store.publish(db, "seat-a", "Asks something", PAGE)
    asked = decisions.ask(
        db,
        agent="seat-a",
        artifact_id=page.id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    decisions.answer(db, asked.id, letter="a", note="", by="jeremy")

    events.note_read(db, "seat-a")  # no session declared, as before today
    rows = decisions.uncollected(db, agent="seat-a", stuck_after_seconds=0)
    assert rows[0].state == "stuck", "the unlabelled reader stopped counting"


def test_an_unlabelled_reader_keeps_one_row_however_often_it_reads(db):
    """NULLS NOT DISTINCT on the unique index. Without it `on conflict` never
    fires for a caller that declares nothing -- two NULLs are normally distinct
    to a unique index -- and this table grows a row per request for ever."""
    from artifacts import events

    keys.mint(db, agent="seat-a")
    for _ in range(4):
        events.note_read(db, "seat-a")
    events.note_read(db, "seat-a", "labelled/xyz")
    with db.connect() as conn:
        total = conn.execute(
            "select count(*) from seat_read where agent = %s", ("seat-a",)
        ).fetchone()[0]
    assert total == 2, "one row for the unlabelled reader, one for the labelled"
