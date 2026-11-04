"""Delivery receipts, slice 1: an agent states what it did with Jeremy's answer
(seen, working, done, blocked, won't do) and reads it back on the decision
(docs/agent/specs/2026-11-04-receipts-design.md, 4.1-4.4 and 7).

What these keep apart: a receipt is the agent's own statement, never a read and never a
closing; `done` does not close the decision; the journal's `receipt_set` never wakes a
waiter; and a new human act on a decision makes every earlier receipt history.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, events, formanswer, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"
FORM_PAGE = (
    "<!doctype html><html><head><title>F</title></head><body>"
    '<form data-artifact-form="rollout">'
    '<label>Why <input type="text" name="why"></label></form></body></html>'
)
_OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture()
def client(db):
    return TestClient(create_gallery_app(db))


def auth(token, session=None):
    headers = {"Authorization": f"Bearer {token}"}
    if session:
        headers["x-artifacts-session"] = session
    return headers


@pytest.fixture()
def token(db):
    return keys.mint(db, agent="ops-seat")[0]


def _decision(db, agent="ops-seat"):
    art = store.publish(db, agent, "Board", PAGE)
    return decisions.ask(
        db, agent=agent, artifact_id=art.id, question="ship it?", options=_OPTIONS
    )


def _declare(client, token, did, state, session=None, **body):
    return client.post(
        f"/api/v1/decisions/{did}/receipt",
        json={"state": state, **body},
        headers=auth(token, session),
    )


def _read(client, token, did):
    rows = client.get("/api/v1/decisions", headers=auth(token)).json()
    return next(r for r in rows if r["id"] == did)


# --- reads ------------------------------------------------------------------


def test_a_decision_nobody_has_touched_has_no_receipt(client, db, token):
    d = _decision(db)
    assert _read(client, token, d.id)["receipt"] is None
    assert decisions.get_owned(db, "ops-seat", d.id).receipt is None


def test_seen_is_the_acknowledgement_and_ack_still_means_the_same(client, db, token):
    d = _decision(db)
    r = _declare(client, token, d.id, "seen", session="ops-seat/s1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "seen" and body["agent"] == "ops-seat"
    assert body["session"] == "ops-seat/s1"
    assert decisions.get_owned(db, "ops-seat", d.id).acknowledged_at is not None

    other = _decision(db)
    client.post(f"/api/v1/decisions/{other.id}/acknowledge", headers=auth(token))
    assert _read(client, token, other.id)["receipt"]["state"] == "seen"


def test_seen_takes_no_note_or_link(client, db, token):
    d = _decision(db)
    assert _declare(client, token, d.id, "seen", note="x").status_code == 422


def test_working_carries_a_note_and_expires_after_four_hours_by_default(
    client, db, token
):
    d = _decision(db)
    r = _declare(client, token, d.id, "working", note="bead abc")
    assert r.status_code == 200, r.text
    got = _read(client, token, d.id)["receipt"]
    assert got["state"] == "working" and got["note"] == "bead abc"
    assert got["lapsed"] is False
    expires = datetime.fromisoformat(got["expires_at"])
    assert (
        timedelta(hours=3, minutes=55)
        < expires - datetime.now(UTC)
        < timedelta(hours=4, minutes=5)
    )


def test_working_lifetime_is_set_with_for_and_capped_at_twelve_hours(client, db, token):
    d = _decision(db)
    ok = _declare(client, token, d.id, "working", **{"for": "1h"})
    assert ok.status_code == 200
    soon = datetime.fromisoformat(ok.json()["expires_at"]) - datetime.now(UTC)
    assert timedelta(minutes=55) < soon < timedelta(minutes=65)
    assert _declare(client, token, d.id, "working", **{"for": "13h"}).status_code == 422
    assert (
        _declare(client, token, d.id, "working", **{"for": "soon"}).status_code == 422
    )


def test_working_again_renews_it_and_the_history_keeps_both(client, db, token):
    d = _decision(db)
    _declare(client, token, d.id, "working", note="first")
    _declare(client, token, d.id, "working", note="second")
    assert _read(client, token, d.id)["receipt"]["note"] == "second"
    history = client.get(
        f"/api/v1/decisions/{d.id}/receipts", headers=auth(token)
    ).json()
    assert [h["note"] for h in history] == ["first", "second"]


def test_a_lapsed_working_is_computed_on_read_never_stored(client, db, token):
    d = _decision(db)
    _declare(client, token, d.id, "working", note="x")
    with db.connect() as conn:
        conn.execute(
            "update receipt set expires_at = %s where subject_id = %s",
            (datetime.now(UTC) - timedelta(minutes=1), d.id),
        )
    got = _read(client, token, d.id)["receipt"]
    assert got["state"] == "working" and got["lapsed"] is True


@pytest.mark.parametrize("state", ["blocked", "wont-do"])
def test_blocked_and_wont_do_need_a_reason(client, db, token, state):
    d = _decision(db)
    assert _declare(client, token, d.id, state).status_code == 422
    ok = _declare(client, token, d.id, state, note="waiting on a key")
    assert ok.status_code == 200
    assert ok.json()["state"] == state.replace("-", "_")


def test_a_link_belongs_to_done_only_and_must_be_a_web_address(client, db, token):
    d = _decision(db)
    assert (
        _declare(client, token, d.id, "blocked", note="n", link="https://x.test/1")
    ).status_code == 422
    assert (
        _declare(client, token, d.id, "done", link="javascript:alert(1)")
    ).status_code == 422
    assert (
        _declare(client, token, d.id, "done", link="https://x.test/pr/1")
    ).status_code == 200


def test_done_records_an_outcome_with_the_link_and_closes_nothing(client, db, token):
    d = _decision(db)
    r = _declare(
        client, token, d.id, "done", note="shipped in PR 9", link="https://x.test/9"
    )
    assert r.status_code == 200, r.text
    row = decisions.get_owned(db, "ops-seat", d.id)
    assert row.closed_at is None, "closing stays its own act"
    assert row.receipt["state"] == "done" and row.receipt["link"] == "https://x.test/9"
    (outcome,) = decisions.outcomes_for(db, d.id)
    assert outcome.body == "shipped in PR 9" and outcome.link == "https://x.test/9"


def test_a_later_state_replaces_an_earlier_one_as_current(client, db, token):
    d = _decision(db)
    _declare(client, token, d.id, "working", note="on it")
    _declare(client, token, d.id, "blocked", note="stuck")
    assert _read(client, token, d.id)["receipt"]["state"] == "blocked"


def test_every_receipt_stores_the_agent_and_the_session(client, db, token):
    d = _decision(db)
    _declare(client, token, d.id, "working", session="ops-seat/lane-3", note="n")
    (h,) = client.get(f"/api/v1/decisions/{d.id}/receipts", headers=auth(token)).json()
    assert h["agent"] == "ops-seat" and h["session"] == "ops-seat/lane-3"


# --- scope ------------------------------------------------------------------


def test_not_yours_answers_exactly_like_no_such(client, db, token):
    mine = _decision(db)
    other = keys.mint(db, agent="another-seat")[0]
    theirs = _declare(client, other, mine.id, "seen")
    missing = _declare(client, other, "nosuchdecisionidatall2345", "seen")
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json() == missing.json()
    assert (
        client.get(
            f"/api/v1/decisions/{mine.id}/receipts", headers=auth(other)
        ).status_code
        == 404
    )
    assert decisions.get_owned(db, "ops-seat", mine.id).receipt is None


def test_the_route_needs_a_key(client, db):
    d = _decision(db)
    r = client.post(f"/api/v1/decisions/{d.id}/receipt", json={"state": "seen"})
    assert r.status_code == 401


def test_an_unknown_state_is_refused(client, db, token):
    d = _decision(db)
    assert _declare(client, token, d.id, "finished").status_code == 422


def test_an_idempotency_key_makes_a_retry_write_once(client, db, token):
    d = _decision(db)
    headers = {**auth(token), "Idempotency-Key": "k1"}
    for _ in range(2):
        r = client.post(
            f"/api/v1/decisions/{d.id}/receipt",
            json={"state": "working", "note": "n"},
            headers=headers,
        )
        assert r.status_code == 200
    history = client.get(
        f"/api/v1/decisions/{d.id}/receipts", headers=auth(token)
    ).json()
    assert len(history) == 1


# --- the journal ------------------------------------------------------------


def test_receipt_set_is_a_known_event_kind():
    assert "receipt_set" in events.KINDS


def test_a_receipt_is_on_events_only_when_asked_for(client, db, token):
    d = _decision(db)
    _declare(client, token, d.id, "working", session="ops-seat/s1", note="bead x")
    plain = client.get("/api/v1/events", headers=auth(token)).json()
    assert [e for e in plain if e["kind"] == "receipt_set"] == []
    asked = client.get("/api/v1/events?include=receipts", headers=auth(token)).json()
    (ev,) = [e for e in asked if e["kind"] == "receipt_set"]
    assert ev["ref_id"] == d.id and ev["session"] == "ops-seat/s1"
    assert "working" in ev["summary"] and ev["actor"] == "ops-seat"


def test_a_receipt_never_wakes_a_waiter(client, db, token):
    d = _decision(db)
    _declare(client, token, d.id, "working", note="n")
    assert events.wait_for_agent(db, "ops-seat", since=0, seconds=0.6) == []
    held = client.get("/api/v1/events?wait=1&since=0", headers=auth(token))
    assert held.json() == []


def test_seen_leaves_a_journal_row_once(client, db, token):
    d = _decision(db)
    _declare(client, token, d.id, "seen")
    _declare(client, token, d.id, "seen")
    asked = client.get("/api/v1/events?include=receipts", headers=auth(token)).json()
    assert len([e for e in asked if e["kind"] == "receipt_set"]) == 1, (
        "seen is first-wins: the second call changed nothing"
    )


# --- a new human act makes earlier receipts history --------------------------


def test_a_form_answer_after_a_receipt_reads_as_not_picked_up_again(client, db):
    token = keys.mint(db, agent="ops")[0]
    page_ = client.post(
        "/api/v1/artifacts",
        json={"title": "F", "body": FORM_PAGE},
        headers=auth(token),
    ).json()
    (form,) = client.get(
        f"/api/v1/artifacts/{page_['id']}/forms", headers=auth(token)
    ).json()
    did = form["decision_id"]
    formanswer.submit(db, did, version=1, values={"why": "go"})
    _declare(client, token, did, "seen")
    _declare(client, token, did, "done", note="done once")
    assert decisions.get_owned(db, "ops", did).receipt["state"] == "done"

    formanswer.submit(db, did, version=1, values={"why": "go again"})

    row = decisions.get_owned(db, "ops", did)
    assert row.receipt is None
    assert row.acknowledged_at is None
    history = client.get(f"/api/v1/decisions/{did}/receipts", headers=auth(token))
    assert [h["state"] for h in history.json()] == ["seen", "done"], "kept, as history"
    assert not any(h["current"] for h in history.json())
