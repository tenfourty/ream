"""The nudge journal: `append` (always inside a caller's own transaction) and
`list_for_agent` (the `?since=` cursor an agent anywhere -- not just one a
nudge can reach -- polls with). `decisions.answer` is the one live caller of
`append` this slice wires up (see `test_decisions.py` for its own behaviour);
the tests here exercise `events.py` itself, directly, across every kind the
schema names, so the journal's own contract is proven independently of which
store happens to call it today.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, events, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"
_OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
