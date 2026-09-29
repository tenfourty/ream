"""Claims (page review step 5): one session claims a review round with a
lease, a second session is told who has it, and a session that cannot act
hands the round back with a reason."""

import threading
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_client import live  # noqa: F401 -- fixture

from artifacts import client as cli
from artifacts import decisions, keys, nudge, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><title>Plan</title><p>Rotate the keys every 90 days.</p>"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(nudge, "emit", lambda **kw: None)


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def _auth(db, agent="seat"):
    token, _ = keys.mint(db, agent=agent)
    return {"Authorization": f"Bearer {token}"}


def _review(db, agent="seat"):
    page = store.publish(db, agent, "Plan", PAGE)
    return page, decisions.ask_review(db, agent=agent, artifact_id=page.id)


def _claim(client, h, rid, session):
    return client.post(
        f"/api/v1/reviews/{rid}/claim", headers=h, json={"session": session}
    )


def _release(client, h, rid, session, reason):
    return client.post(
        f"/api/v1/reviews/{rid}/release",
        headers=h,
        json={"session": session, "reason": reason},
    )


def _state(db, rid):
    with db.connect() as conn:
        return conn.execute(
            "select n, claimed_by, claim_expires_at, released_reason,"
            " extract(epoch from claim_expires_at - now())"
            " from review_round where review_id = %s order by n desc limit 1",
            (rid,),
        ).fetchone()


# ---- claim ----


def test_a_claim_takes_the_round_for_five_minutes(db, client):
    h = _auth(db)
    _page, review = _review(db)
    r = _claim(client, h, review.id, "seat/one")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"claimed_by", "claim_expires_at", "round"}
    assert body["claimed_by"] == "seat/one" and body["round"] == 1
    n, by, expires, reason, left = _state(db, review.id)
    assert (n, by, reason) == (1, "seat/one", None)
    assert 290 < left <= 300
    assert datetime.fromisoformat(body["claim_expires_at"]) == expires


def test_a_second_session_is_told_who_has_it(db, client):
    h = _auth(db)
    _page, review = _review(db)
    first = _claim(client, h, review.id, "seat/one").json()
    r = _claim(client, h, review.id, "seat/two")
    assert r.status_code == 409
    body = r.json()
    assert body["claimed_by"] == "seat/one"
    assert body["claim_expires_at"] == first["claim_expires_at"]
    assert body["round"] == 1
    assert _state(db, review.id)[1] == "seat/one"


def test_the_same_session_reclaims_its_own_round(db, client):
    h = _auth(db)
    _page, review = _review(db)
    first = _claim(client, h, review.id, "seat/one").json()
    time.sleep(0.01)
    again = _claim(client, h, review.id, "seat/one")
    assert again.status_code == 200
    assert again.json()["claimed_by"] == "seat/one"
    assert again.json()["claim_expires_at"] > first["claim_expires_at"]


def test_a_lease_that_has_run_out_can_be_taken_but_not_before(db, client, monkeypatch):
    monkeypatch.setattr(decisions, "CLAIM_SECONDS", 1)
    h = _auth(db)
    _page, review = _review(db)
    started = time.monotonic()
    assert _claim(client, h, review.id, "seat/dead").status_code == 200
    assert _claim(client, h, review.id, "seat/new").status_code == 409
    deadline = time.monotonic() + 20
    while True:
        r = _claim(client, h, review.id, "seat/new")
        if r.status_code == 200:
            break
        assert r.status_code == 409
        assert time.monotonic() < deadline, "the lease never ran out"
        time.sleep(0.05)
    assert time.monotonic() - started >= 0.9, "taken before the lease ran out"
    assert r.json()["claimed_by"] == "seat/new"


def test_a_backdated_lease_is_free_to_another_session(db, client):
    h = _auth(db)
    _page, review = _review(db)
    _claim(client, h, review.id, "seat/dead")
    with db.connect() as conn:
        conn.execute(
            "update review_round set claim_expires_at = now() - interval '1 second'"
            " where review_id = %s",
            (review.id,),
        )
    assert _claim(client, h, review.id, "seat/new").status_code == 200


def test_two_sessions_racing_to_claim_get_exactly_one_winner(db, live, monkeypatch):
    """The loser waits on the decision row's lock while the winner holds it,
    then sees the winner's claim: 409, never a second 200."""
    token, _ = keys.mint(db, agent="seat")
    _page, review = _review(db)
    base = f"{live}/api/v1"
    reached, go = threading.Event(), threading.Event()
    real = decisions._lock_current_round
    gated = []

    def wrapped(conn, review_id):
        out = real(conn, review_id)
        if not gated:
            gated.append(1)
            reached.set()
            assert go.wait(10)
        return out

    monkeypatch.setattr(decisions, "_lock_current_round", wrapped)
    results = {}

    def claim(session):
        try:
            results[session] = (
                200,
                cli._request(
                    "POST",
                    f"{base}/reviews/{review.id}/claim",
                    token,
                    {"session": session},
                ),
            )
        except cli.HTTPFailure as exc:
            results[session] = (exc.status, exc.body)

    a = threading.Thread(target=claim, args=("seat/a",))
    a.start()
    assert reached.wait(10)
    b = threading.Thread(target=claim, args=("seat/b",))
    b.start()
    _until_waiting_on_a_lock(db)
    assert b.is_alive(), "the second claim waits for the first one's lock"
    go.set()
    a.join(20)
    b.join(20)
    assert results["seat/a"][0] == 200
    assert results["seat/b"][0] == 409
    assert results["seat/b"][1]["claimed_by"] == "seat/a"
    assert _state(db, review.id)[1] == "seat/a"


def _until_waiting_on_a_lock(db, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with db.connect() as conn:
            waiting = conn.execute(
                "select 1 from pg_stat_activity"
                " where datname = current_database() and wait_event_type = 'Lock'"
            ).fetchone()
        if waiting:
            return
        time.sleep(0.005)
    raise AssertionError("no statement ever waited on a lock")


def test_a_review_that_is_not_yours_is_the_same_404_as_a_missing_one(db, client):
    _auth(db)
    other = _auth(db, agent="other")
    _page, review = _review(db)
    theirs = _claim(client, other, review.id, "other/one")
    missing = _claim(client, other, "nosuchreview", "other/one")
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json() == missing.json()
    released = _release(client, other, review.id, "other/one", "not mine")
    assert released.status_code == 404 and released.json() == missing.json()
    assert _state(db, review.id)[1] is None


def test_a_question_is_not_a_review(db, client):
    h = _auth(db)
    page = store.publish(db, "seat", "Plan", PAGE)
    question = decisions.ask(
        db,
        agent="seat",
        artifact_id=page.id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
    )
    assert _claim(client, h, question.id, "seat/one").status_code == 404


@pytest.mark.parametrize("body", [{}, {"session": ""}, {"session": "   "}])
def test_a_claim_needs_a_session(db, client, body):
    h = _auth(db)
    _page, review = _review(db)
    r = client.post(f"/api/v1/reviews/{review.id}/claim", headers=h, json=body)
    assert r.status_code == 422
    assert _state(db, review.id)[1] is None


def test_a_new_round_starts_unclaimed(db, client):
    h = _auth(db)
    page, review = _review(db)
    _claim(client, h, review.id, "seat/one")
    decisions.answer(db, review.id, letter="c", note="tighten it", by="jeremy")
    with db.connect() as conn:
        revision = conn.execute(
            "select v.revision from artifact a join version v"
            " on v.artifact_id = a.id and v.n = a.current_version where a.id = %s",
            (page.id,),
        ).fetchone()[0]
    store.patch(db, "seat", page.id, PAGE.replace("90", "30"), revision)
    n, by, expires, reason, _left = _state(db, review.id)
    assert (n, by, expires, reason) == (2, None, None, None)
    r = _claim(client, h, review.id, "seat/two")
    assert r.status_code == 200 and r.json()["round"] == 2


# ---- release ----


def test_releasing_hands_the_round_back_with_a_reason(db, client):
    h = _auth(db)
    _page, review = _review(db)
    _claim(client, h, review.id, "seat/one")
    r = _release(client, h, review.id, "seat/one", "cannot deploy from here")
    assert r.status_code == 200
    n, by, expires, reason, _left = _state(db, review.id)
    assert (n, by, expires, reason) == (1, None, None, "cannot deploy from here")
    # Free to anyone now, and a claim clears the reason.
    assert _claim(client, h, review.id, "seat/two").status_code == 200
    assert _state(db, review.id)[3] is None


def test_releasing_writes_no_event_to_the_agent(db, client):
    h = _auth(db)
    _page, review = _review(db)
    _claim(client, h, review.id, "seat/one")
    with db.connect() as conn:
        before = conn.execute("select count(*) from event").fetchone()[0]
    _release(client, h, review.id, "seat/one", "not mine to fix")
    with db.connect() as conn:
        after = conn.execute("select count(*) from event").fetchone()[0]
    assert after == before


def test_a_session_without_the_lease_cannot_release_it(db, client):
    h = _auth(db)
    _page, review = _review(db)
    held = _claim(client, h, review.id, "seat/one").json()
    r = _release(client, h, review.id, "seat/two", "let me have it")
    assert r.status_code == 409
    body = r.json()
    assert body["claimed_by"] == "seat/one"
    assert body["claim_expires_at"] == held["claim_expires_at"]
    assert body["round"] == 1
    assert _state(db, review.id)[1:4:2] == ("seat/one", None)


@pytest.mark.parametrize(
    "reason,status",
    [("", 422), ("   ", 422), ("x" * 501, 413), ("AKIA" + "IOSFODNN7EXAMPLE", 422)],
)
def test_a_release_reason_is_required_bounded_and_scanned(db, client, reason, status):
    h = _auth(db)
    _page, review = _review(db)
    _claim(client, h, review.id, "seat/one")
    r = _release(client, h, review.id, "seat/one", reason)
    assert r.status_code == status
    assert "AKIA" not in r.text
    assert _state(db, review.id)[1] == "seat/one"


def test_a_release_needs_a_session(db, client):
    h = _auth(db)
    _page, review = _review(db)
    r = client.post(
        f"/api/v1/reviews/{review.id}/release", headers=h, json={"reason": "x"}
    )
    assert r.status_code == 422


def test_releasing_an_unclaimed_round_just_records_the_reason(db, client):
    h = _auth(db)
    _page, review = _review(db)
    assert _release(client, h, review.id, "seat/one", "not for me").status_code == 200
    assert _state(db, review.id)[3] == "not for me"


def test_claim_expiry_is_a_real_timestamp(db, client):
    h = _auth(db)
    _page, review = _review(db)
    body = _claim(client, h, review.id, "seat/one").json()
    expires = datetime.fromisoformat(body["claim_expires_at"])
    assert expires.tzinfo is not None
    assert 200 < (expires - datetime.now(UTC)).total_seconds() <= 305


def _released_by(db, rid):
    with db.connect() as conn:
        return conn.execute(
            "select released_by, released_reason from review_round"
            " where review_id = %s order by n desc limit 1",
            (rid,),
        ).fetchone()


def test_a_release_names_the_session_and_a_later_claim_clears_it(db, client):
    h = _auth(db)
    _page, review = _review(db)
    _claim(client, h, review.id, "seat/one")
    _release(client, h, review.id, "seat/one", "cannot deploy from here")
    assert _released_by(db, review.id) == ("seat/one", "cannot deploy from here")
    assert _claim(client, h, review.id, "seat/two").status_code == 200
    assert _released_by(db, review.id) == (None, None)
