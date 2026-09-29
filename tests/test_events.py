"""The nudge journal: `append` (always inside a caller's own transaction) and
`list_for_agent` (the `?since=` cursor an agent anywhere -- not just one a
nudge can reach -- polls with). `decisions.answer` is the one live caller of
`append` this slice wires up (see `test_decisions.py` for its own behaviour);
the tests here exercise `events.py` itself, directly, across every kind the
schema names, so the journal's own contract is proven independently of which
store happens to call it today.
"""

import threading
import time

import psycopg
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


def _an_artifact(client, token: str) -> str:
    return client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()["id"]


def _append_event(db, *, agent: str, artifact_id: str) -> int:
    """One journal row, written the way a real interaction writes it."""
    with db.connect() as conn:
        return events.append(
            conn,
            artifact_id=artifact_id,
            agent=agent,
            actor="jeremy",
            kind="decision_answered",
            summary="Jeremy answered",
            ref_id="ref1",
        )


# ---------------------------------------------------------------------------
# append() -- one row per call, the owning seat in `agent`, across every kind
# the schema names.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", events.KINDS)
def test_appending_each_kind_records_exactly_one_row_with_the_owning_seat(db, kind):
    published = store.publish(db, "ops-seat", "Board", PAGE)
    with db.connect() as conn:
        seq = events.append(
            conn,
            artifact_id=published.id,
            agent="ops-seat",
            actor="jeremy",
            kind=kind,
            summary="s",
            ref_id="ref1",
        )
    rows = events.list_for_agent(db, "ops-seat")
    assert len(rows) == 1
    assert rows[0].seq == seq
    assert rows[0].agent == "ops-seat"
    assert rows[0].actor == "jeremy"
    assert rows[0].kind == kind
    assert rows[0].artifact_id == published.id
    assert rows[0].ref_id == "ref1"


def test_an_event_records_the_actor_who_caused_it_not_the_owner_of_the_page(db):
    """`agent` is the seat the event is FOR (the page owner); `actor` is WHO
    did it, and the two must never collapse into one another. Proven with two
    distinct values -- `agent="page-owner-seat"`, `actor="some-other-seat"`
    -- that no inference from `kind` or from `agent` could ever produce,
    matching schema.sql's own reasoning for keeping this an explicit column:
    an event means "something happened on your page that you did not do", and
    inferring `actor` is exactly how that stops being true the first time a
    future call site gets it wrong."""
    published = store.publish(db, "page-owner-seat", "Board", PAGE)
    with db.connect() as conn:
        events.append(
            conn,
            artifact_id=published.id,
            agent="page-owner-seat",
            actor="some-other-seat",
            kind="comment_added",
            summary="s",
        )
    row = events.list_for_agent(db, "page-owner-seat")[0]
    assert row.agent == "page-owner-seat"
    assert row.actor == "some-other-seat"
    assert row.agent != row.actor


def test_answering_a_decision_appends_exactly_one_event_with_the_owning_seat(db):
    """The real, live call site: decisions.answer wraps its own update and the
    event it produces in one transaction (see test below for the atomicity
    itself) and passes the owning agent, not `by="jeremy"`, as the event's
    `agent` -- an event is FOR the seat that owns the page, never for Jeremy,
    who is not a seat at all."""
    published = store.publish(db, "ops-seat", "Board", PAGE)
    row = decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=published.id,
        question="ship it?",
        options=_OPTIONS,
    )

    decisions.answer(db, row.id, letter="a", note="go ahead", by="jeremy")

    rows = events.list_for_agent(db, "ops-seat")
    assert len(rows) == 1
    assert rows[0].agent == "ops-seat"
    assert rows[0].actor == "jeremy"
    assert rows[0].kind == "decision_answered"
    assert rows[0].ref_id == row.id
    assert rows[0].summary == "a: yes"


def test_reanswering_appends_a_second_event_not_a_replacement(db):
    """Re-answering REPLACES the decision's own answer (decisions.answer's own
    docstring), but the journal is a log, not a mirror of current state -- a
    second answer is a second thing that happened, so it gets a second row."""
    published = store.publish(db, "ops-seat", "Board", PAGE)
    row = decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=published.id,
        question="ship it?",
        options=_OPTIONS,
    )
    decisions.answer(db, row.id, letter="a", by="jeremy")
    decisions.answer(db, row.id, letter="b", by="jeremy")

    rows = events.list_for_agent(db, "ops-seat")
    assert len(rows) == 2
    assert [r.summary for r in rows] == ["a: yes", "b: no"]


# ---------------------------------------------------------------------------
# Atomicity: the row and its cause commit or roll back together.
# ---------------------------------------------------------------------------


def test_a_failure_after_the_cause_rolls_back_both_the_answer_and_its_event(
    db, monkeypatch
):
    """Forces a failure INSIDE decisions.answer's transaction, after the
    decision update has already run but before the transaction commits --
    mirroring test_publish.py's own NUL-byte trick for store.publish/patch.
    `events.append` really executes its insert first (so a bug that made the
    two writes independent, rather than genuinely one transaction, would
    still show a leftover event row here); the point is that raising from
    inside the `with conn.transaction():` block must undo it again."""
    published = store.publish(db, "ops-seat", "Board", PAGE)
    row = decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=published.id,
        question="ship it?",
        options=_OPTIONS,
    )

    real_append = events.append

    def _append_then_fail(conn, **kwargs):
        real_append(conn, **kwargs)
        raise RuntimeError("simulated failure after the cause")

    monkeypatch.setattr(events, "append", _append_then_fail)

    with pytest.raises(RuntimeError):
        decisions.answer(db, row.id, letter="a", by="jeremy")

    reloaded = decisions.list_for_artifact(db, published.id, include_answered=True)[0]
    assert reloaded.answer is None
    assert events.list_for_agent(db, "ops-seat") == []


# ---------------------------------------------------------------------------
# ?since= over the agent API: only rows above the cursor, in seq order, only
# for the calling agent, honouring limit.
# ---------------------------------------------------------------------------


def test_since_returns_only_rows_above_the_cursor_in_seq_order(db):
    published = store.publish(db, "ops-seat", "Board", PAGE)
    with db.connect() as conn:
        seqs = [
            events.append(
                conn,
                artifact_id=published.id,
                agent="ops-seat",
                actor="jeremy",
                kind="comment_added",
                summary=f"e{i}",
            )
            for i in range(5)
        ]
    cursor = seqs[1]
    page = events.list_for_agent(db, "ops-seat", since=cursor)
    assert [row.seq for row in page] == seqs[2:]


def test_since_honours_limit(db):
    published = store.publish(db, "ops-seat", "Board", PAGE)
    with db.connect() as conn:
        seqs = [
            events.append(
                conn,
                artifact_id=published.id,
                agent="ops-seat",
                actor="jeremy",
                kind="comment_added",
                summary=f"e{i}",
            )
            for i in range(5)
        ]
    page = events.list_for_agent(db, "ops-seat", since=0, limit=2)
    assert [row.seq for row in page] == seqs[:2]


def test_another_agent_sees_none_of_the_first_agents_events(db):
    published = store.publish(db, "seat-a", "Board", PAGE)
    with db.connect() as conn:
        events.append(
            conn,
            artifact_id=published.id,
            agent="seat-a",
            actor="jeremy",
            kind="decision_answered",
            summary="s",
        )
    assert events.list_for_agent(db, "seat-b") == []
    assert len(events.list_for_agent(db, "seat-a")) == 1


# ---------------------------------------------------------------------------
# The same, over the actual `/api/v1/events` route a real agent polls.
# ---------------------------------------------------------------------------


def test_the_events_route_is_scoped_to_the_calling_key_and_honours_since_and_limit(db):
    token_a, _ = keys.mint(db, agent="seat-a")
    token_b, _ = keys.mint(db, agent="seat-b")
    client = TestClient(create_gallery_app(db))

    published_a = client.post(
        "/api/v1/artifacts", json={"title": "A", "body": PAGE}, headers=auth(token_a)
    ).json()
    row_a = decisions.ask(
        db,
        agent="seat-a",
        artifact_id=published_a["id"],
        question="ship it?",
        options=_OPTIONS,
    )
    decisions.answer(db, row_a.id, letter="a", by="jeremy")
    decisions.answer(db, row_a.id, letter="b", by="jeremy")

    published_b = client.post(
        "/api/v1/artifacts", json={"title": "B", "body": PAGE}, headers=auth(token_b)
    ).json()
    row_b = decisions.ask(
        db,
        agent="seat-b",
        artifact_id=published_b["id"],
        question="ship it?",
        options=_OPTIONS,
    )
    decisions.answer(db, row_b.id, letter="a", by="jeremy")

    seat_a_events = client.get("/api/v1/events", headers=auth(token_a)).json()
    assert [row["summary"] for row in seat_a_events] == ["a: yes", "b: no"]

    cursor = seat_a_events[0]["seq"]
    since_page = client.get(
        f"/api/v1/events?since={cursor}", headers=auth(token_a)
    ).json()
    assert [row["seq"] for row in since_page] == [seat_a_events[1]["seq"]]

    limited = client.get("/api/v1/events?limit=1", headers=auth(token_a)).json()
    assert len(limited) == 1
    assert limited[0]["seq"] == seat_a_events[0]["seq"]

    seat_b_events = client.get("/api/v1/events", headers=auth(token_b)).json()
    assert [row["summary"] for row in seat_b_events] == ["a: yes"]
    assert {row["seq"] for row in seat_b_events}.isdisjoint(
        {row["seq"] for row in seat_a_events}
    )


def test_the_events_page_size_is_bounded_at_the_route(db):
    """`limit` is the one page-size parameter this app exposes over HTTP, so it is
    the one that can be abused. An unbounded value reached Postgres's LIMIT
    unmodified, and a negative one surfaced as a raw 500 rather than a refusal
    naming the field."""
    token, _ = keys.mint(db, agent="seat-a")
    client = TestClient(create_gallery_app(db))
    for bad in (0, -1, 1001, 10**9):
        response = client.get(f"/api/v1/events?limit={bad}", headers=auth(token))
        assert response.status_code == 422, bad
    for good in (1, 100, 1000):
        response = client.get(f"/api/v1/events?limit={good}", headers=auth(token))
        assert response.status_code == 200, good
    assert client.get("/api/v1/events?since=-1", headers=auth(token)).status_code == 422


def test_a_waiting_agent_is_answered_the_moment_an_event_lands(client, db, first_look):
    """The mechanism by which an agent anywhere hears about an answer: it holds
    the request open, and Jeremy pressing a button is what ends the wait. No
    address for the agent is needed, which is the whole point -- a push needs
    one and an agent on a laptop has none."""
    token, _ = keys.mint(db, agent="waiter-seat")
    artifact_id = _an_artifact(client, token)

    started = threading.Event()
    answer = {}

    def hold():
        started.set()
        began = time.monotonic()
        r = client.get("/api/v1/events?since=0&wait=20", headers=auth(token))
        answer["rows"] = r.json()
        answer["waited"] = time.monotonic() - began

    thread = threading.Thread(target=hold)
    thread.start()
    started.wait(5)
    # Let the holder find nothing and settle into waiting, so this cannot
    # pass by the row simply being there before the request began.
    assert first_look.wait(10), "the held request never looked"
    _append_event(db, agent="waiter-seat", artifact_id=artifact_id)
    thread.join(20)

    assert not thread.is_alive(), "the waiting request never returned"
    assert len(answer["rows"]) == 1
    # It returned because a row landed, not because the wait ran out.
    assert answer["waited"] < 15


def test_a_wait_that_finds_nothing_returns_empty_rather_than_failing(client, db):
    """An empty answer means 'nothing yet, ask again'. A caller loops on this
    forever, so a timeout must never look like an error."""
    token, _ = keys.mint(db, agent="quiet-seat")
    began = time.monotonic()
    r = client.get("/api/v1/events?since=0&wait=0.3", headers=auth(token))
    waited = time.monotonic() - began
    assert r.status_code == 200
    assert r.json() == []
    # It really waited, and it did not overshoot what was asked for.
    assert 0.25 < waited < 5


def test_an_event_already_there_is_returned_without_waiting(client, db):
    """A cursor that has fallen behind must not be made to wait for the NEXT
    event before it is told about the ones it already missed."""
    token, _ = keys.mint(db, agent="behind-seat")
    artifact_id = _an_artifact(client, token)
    _append_event(db, agent="behind-seat", artifact_id=artifact_id)

    began = time.monotonic()
    r = client.get("/api/v1/events?since=0&wait=30", headers=auth(token))
    assert len(r.json()) == 1
    assert time.monotonic() - began < 5


def test_one_agents_wait_is_never_ended_by_another_agents_event(client, db, first_look):
    """The scoping that makes this safe to expose: a held request is a standing
    read of one agent's own events, so an unrelated agent's activity must not
    end it -- that would both wake the wrong agent and leak that something
    happened elsewhere."""
    mine, _ = keys.mint(db, agent="mine-seat")
    theirs, _ = keys.mint(db, agent="theirs-seat")
    their_artifact = _an_artifact(client, theirs)

    answer = {}

    def hold():
        r = client.get("/api/v1/events?since=0&wait=0.5", headers=auth(mine))
        answer["rows"] = r.json()

    thread = threading.Thread(target=hold)
    thread.start()
    assert first_look.wait(10), "the held request never looked"
    _append_event(db, agent="theirs-seat", artifact_id=their_artifact)
    thread.join(20)

    assert not thread.is_alive()
    assert answer["rows"] == []


def test_a_wait_longer_than_the_ceiling_is_refused_by_the_route(client, db):
    """The ceiling is enforced where a bad value becomes a 422 naming the field,
    not a thread parked for a day."""
    token, _ = keys.mint(db, agent="greedy-seat")
    assert (
        client.get("/api/v1/events?wait=100000", headers=auth(token)).status_code == 422
    )
    assert client.get("/api/v1/events?wait=-1", headers=auth(token)).status_code == 422


def test_past_the_waiter_ceiling_a_caller_is_answered_instead_of_queued(db):
    """Waiting costs a worker thread. Past the ceiling the app must degrade to
    ordinary polling rather than starve every other request -- including the
    gallery Jeremy is reading."""
    from artifacts import events as events_module

    held = threading.Semaphore(0)
    for _ in range(events_module.MAX_WAITERS):
        assert events_module._waiters.acquire(blocking=False)
    try:
        began = time.monotonic()
        rows = events_module.wait_for_agent(db, "nobody-seat", since=0, seconds=30)
        assert rows == []
        # Answered immediately, not after 30 seconds.
        assert time.monotonic() - began < 2
    finally:
        for _ in range(events_module.MAX_WAITERS):
            events_module._waiters.release()
        del held


# --- his words travel with the event ----------------------------------------


def test_an_answer_event_carries_his_note_verbatim(db):
    """THE DEFECT THIS EXISTS TO PREVENT, and it cost a real afternoon.

    Before this the event's `summary` was "letter: option label" and nothing
    else, so every consumer of the feed — the waiter, the operator's mailbox,
    any future client — learned that Jeremy had answered while being told
    nothing about what he wrote. On 20 Sep 2026 the operator worked an afternoon
    from a letter whose note said "check all folders between the two backups,
    then remove anything that is already elsewhere", and never saw it. Another
    seat was handed "(a)" on a question whose note read "explain this to me
    first, there is not enough context for me to decide on this!".

    This service's own skill says in bold that the note IS the decision. The
    notification path was stripping precisely that.
    """
    keys.mint(db, agent="seat")
    art = store.publish(db, "seat", "Board", PAGE)
    asked = decisions.ask(
        db,
        agent="seat",
        artifact_id=art.id,
        question="Which?",
        options=[{"letter": "a", "label": "Yes"}, {"letter": "b", "label": "No"}],
        recommended="a",
    )
    his_words = "start with (b) but then (a) if we can"
    decisions.answer(db, asked.id, letter="a", note=his_words, by="jeremy")

    rows = events.list_for_agent(db, agent="seat", since=0)
    assert rows, "no event at all for an answered decision"
    assert rows[-1].note == his_words, "the note did not reach the feed"


def test_a_relayed_answer_also_carries_the_note(db):
    keys.mint(db, agent="seat")
    art = store.publish(db, "seat", "Board", PAGE)
    asked = decisions.ask(
        db,
        agent="seat",
        artifact_id=art.id,
        question="Which?",
        options=[{"letter": "a", "label": "Yes"}, {"letter": "b", "label": "No"}],
        recommended="a",
    )
    decisions.record_elsewhere(
        db,
        asked.id,
        letter="b",
        note="Let it go",
        source="telegram",
        recorded_by="homelab-ops",
    )
    assert events.list_for_agent(db, agent="seat", since=0)[-1].note == "Let it go"


def test_the_note_is_its_own_field_not_folded_into_the_summary(db):
    """A consumer rendering the summary on one line has to be able to render the
    note differently — longer, quoted, hard to skim past. Concatenating them
    would take that choice away from every consumer at once."""
    keys.mint(db, agent="seat")
    art = store.publish(db, "seat", "Board", PAGE)
    asked = decisions.ask(
        db,
        agent="seat",
        artifact_id=art.id,
        question="Which?",
        options=[{"letter": "a", "label": "Yes"}, {"letter": "b", "label": "No"}],
        recommended="a",
    )
    decisions.answer(db, asked.id, letter="a", note="a long qualification", by="jeremy")
    row = events.list_for_agent(db, agent="seat", since=0)[-1]
    assert "a long qualification" not in row.summary
    assert row.note == "a long qualification"


def test_no_note_is_an_empty_string_not_a_missing_field(db):
    """A consumer must not have to distinguish absent from empty."""
    keys.mint(db, agent="seat")
    art = store.publish(db, "seat", "Board", PAGE)
    asked = decisions.ask(
        db,
        agent="seat",
        artifact_id=art.id,
        question="Which?",
        options=[{"letter": "a", "label": "Yes"}, {"letter": "b", "label": "No"}],
        recommended="a",
    )
    decisions.answer(db, asked.id, letter="a", note="", by="jeremy")
    assert events.list_for_agent(db, agent="seat", since=0)[-1].note == ""


# --- a held wait and a database that blinks -----------------------------------
#
# Measured 21 Sep 2026 from the ingress log: the only two 500s this route
# served all day were one look failing to resolve the database's service name,
# each after 90-120s of holding, with requests either side succeeding. A hold
# looks every half second, so it is the request most exposed to a blip, and
# one failed look threw the whole hold away.


def _looks(*results):
    """A stand-in for `list_for_agent` that plays the given script, one per
    look: an exception instance is raised, a list is returned, and the last
    entry repeats for ever."""
    calls = []

    def look(_db, _agent, **_kw):
        calls.append(1)
        result = results[min(len(calls), len(results)) - 1]
        if isinstance(result, Exception):
            raise result
        return result

    look.calls = calls
    return look


def test_a_blink_during_a_hold_is_ridden_out(monkeypatch):
    """The hold survives it and still answers with the event, rather than
    turning a resolver hiccup into a 500 on a request that was 100s in."""
    blink = psycopg.OperationalError("failed to resolve host")
    look = _looks([], blink, [], [{"n": 7}])
    monkeypatch.setattr(events, "list_for_agent", look)
    monkeypatch.setattr(events, "_LOOK_EVERY", 0.01)
    assert events.wait_for_agent(object(), "seat", seconds=5) == [{"n": 7}]
    assert len(look.calls) >= 4


def test_a_database_that_is_down_when_the_request_arrives_is_an_error(monkeypatch):
    """The FIRST look is never excused. A hold that swallowed it would answer
    "nothing yet" for its whole duration, which reads as Jeremy being silent
    while the service is in fact unreachable."""
    look = _looks(psycopg.OperationalError("down"))
    monkeypatch.setattr(events, "list_for_agent", look)
    monkeypatch.setattr(events, "_LOOK_EVERY", 0.01)
    with pytest.raises(psycopg.OperationalError):
        events.wait_for_agent(object(), "seat", seconds=5)
    assert len(look.calls) == 1, "and it fails at once, not after the hold"


def test_a_blink_that_does_not_stop_is_reported(monkeypatch):
    """Tolerating it for ever would disguise an outage as silence."""
    look = _looks([], psycopg.OperationalError("still down"))
    monkeypatch.setattr(events, "list_for_agent", look)
    monkeypatch.setattr(events, "_LOOK_EVERY", 0.01)
    monkeypatch.setattr(events, "_TOLERATE_FAILED_LOOKS_FOR", 0.1)
    began = time.monotonic()
    with pytest.raises(psycopg.OperationalError):
        events.wait_for_agent(object(), "seat", seconds=30)
    waited = time.monotonic() - began
    assert 0.1 <= waited < 5, "it gives up on the blink, not on the whole wait"


def test_a_programming_error_is_never_ridden_out(monkeypatch):
    """Only a connection failure is a blink. A bug in a query is a bug, and
    retrying it for ten seconds would hide it."""
    look = _looks([], psycopg.ProgrammingError("bad sql"))
    monkeypatch.setattr(events, "list_for_agent", look)
    monkeypatch.setattr(events, "_LOOK_EVERY", 0.01)
    with pytest.raises(psycopg.ProgrammingError):
        events.wait_for_agent(object(), "seat", seconds=5)
