"""Which SESSION did this, when three seats on one machine share one key.

homelab-ij8.32. Jeremy ruled on 20 Sep 2026, verbatim: "(a) one key per agent
session -- maybe something like a key per machine/host plus some sort of uuid
for the agent sessions? any agent with a key could theoretically edit another
agents artifact or read it but that is okay if they are authenticated."

So three things come apart, and the tests below are organised by them:
  AUTHENTICATION stays per key, per machine. Untouched.
  ATTRIBUTION is the new column -- a label the caller declares.
  AUTHORISATION stays OPEN across seats sharing a key. He said so explicitly.

The most important tests here are the ones in the last section, which assert
that the label is NOT a permission. It is caller-declared, so any key holder can
send any value; a scope built on it would be defeated by editing an environment
variable, and a check that weak is worse than none because it reads like one.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, events, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
SEAT_A = "artifacts-seat/aaa111"
SEAT_B = "homelab-31/bbb222"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


@pytest.fixture()
def key(db):
    token, _ = keys.mint(db, agent="baba-operator")
    return token


def _h(token, session=None):
    headers = {"Authorization": f"Bearer {token}"}
    if session:
        headers["X-Artifacts-Session"] = session
    return headers


# --- attribution -------------------------------------------------------------


def test_a_publish_records_which_session_made_it(client, key, db):
    got = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE},
        headers=_h(key, SEAT_A),
    )
    assert got.status_code == 201
    with db.connect() as conn:
        row = conn.execute(
            "select session from artifact where id = %s", (got.json()["id"],)
        ).fetchone()
    assert row[0] == SEAT_A


def test_each_version_records_the_session_that_wrote_THAT_one(client, key, db):
    """A page three sessions have edited has three answers to "who did this",
    and only the per-version one is ever right."""
    made = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE},
        headers=_h(key, SEAT_A),
    ).json()
    client.patch(
        f"/api/v1/artifacts/{made['id']}",
        json={"body": PAGE + "<!--2-->", "expected_revision": made["revision"]},
        headers=_h(key, SEAT_B),
    )
    with db.connect() as conn:
        versions = conn.execute(
            "select n, session from version where artifact_id = %s order by n",
            (made["id"],),
        ).fetchall()
        artifact = conn.execute(
            "select session from artifact where id = %s", (made["id"],)
        ).fetchone()
    assert versions == [(1, SEAT_A), (2, SEAT_B)]
    assert artifact[0] == SEAT_A, (
        "the artifact keeps who FIRST published it; overwriting it on every "
        "edit would turn 'who made this' into 'who touched it last', which the "
        "version rows already say and say better"
    )


def test_a_caller_that_declares_nothing_stores_null_not_empty_string(client, key, db):
    """ "did not say" needs exactly one representation, or every reader has to
    decide whether "" means unknown or means a session that named itself
    nothing."""
    for header in (None, "   "):
        made = client.post(
            "/api/v1/artifacts",
            json={"title": "Board", "body": PAGE},
            headers=_h(key, header),
        ).json()
        with db.connect() as conn:
            row = conn.execute(
                "select session from artifact where id = %s", (made["id"],)
            ).fetchone()
        assert row[0] is None


def test_me_echoes_back_what_you_declared(client, key):
    """So a seat can check its own plumbing in one call. null means "your
    writes are indistinguishable from the other seats sharing this key", which
    is exactly the question /me exists to answer."""
    assert client.get("/api/v1/me", headers=_h(key, SEAT_A)).json()["session"] == SEAT_A
    assert client.get("/api/v1/me", headers=_h(key)).json()["session"] is None


def test_an_absurdly_long_label_is_refused_by_the_field_that_named_it(client, key):
    got = client.get("/api/v1/me", headers=_h(key, "x" * 200))
    assert got.status_code == 422
    assert "x-artifacts-session" in got.json()["detail"]


# --- the feed, which is what this was asked for ------------------------------


def test_an_answer_wakes_the_session_that_asked_not_the_machine(client, key, db):
    """homelab-31, 21 Sep, after two false wakes in ten minutes on another
    seat's decisions: "a watcher which cries wolf trains its reader to stop
    looking, and the one wake that matters is indistinguishable from these."
    """
    mine = client.post(
        "/api/v1/artifacts",
        json={"title": "Mine", "body": PAGE},
        headers=_h(key, SEAT_A),
    ).json()
    theirs = client.post(
        "/api/v1/artifacts",
        json={"title": "Theirs", "body": PAGE},
        headers=_h(key, SEAT_B),
    ).json()
    asked_a = decisions.ask(
        db,
        agent="baba-operator",
        artifact_id=mine["id"],
        question="Mine?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
        session=SEAT_A,
    )
    asked_b = decisions.ask(
        db,
        agent="baba-operator",
        artifact_id=theirs["id"],
        question="Theirs?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
        session=SEAT_B,
    )
    decisions.answer(db, asked_a.id, letter="a", note="", by="jeremy")
    decisions.answer(db, asked_b.id, letter="b", note="", by="jeremy")

    unfiltered = events.list_for_agent(db, "baba-operator")
    assert len(unfiltered) == 2, "both are still visible to the key, as he ruled"

    mine_only = events.list_for_agent(db, "baba-operator", session=SEAT_A)
    assert [e.ref_id for e in mine_only] == [asked_a.id]
    assert mine_only[0].session == SEAT_A


def test_a_withdrawal_reaches_the_session_that_asked(client, key, db):
    """Archiving closes open questions (Jeremy, 21 Sep). The event saying so has
    to be filterable too, or the one wake that means "stop waiting" is the one
    lost in another seat's noise."""
    page = client.post(
        "/api/v1/artifacts",
        json={"title": "Mine", "body": PAGE},
        headers=_h(key, SEAT_A),
    ).json()
    decisions.ask(
        db,
        agent="baba-operator",
        artifact_id=page["id"],
        question="Mine?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
        session=SEAT_A,
    )
    store.set_archived(db, page["id"], True, agent=None)

    shelved = events.list_for_agent(db, "baba-operator", session=SEAT_A)
    assert [e.kind for e in shelved] == ["decision_shelved"]


def test_the_filter_is_reachable_over_http(client, key, db):
    page = client.post(
        "/api/v1/artifacts",
        json={"title": "Mine", "body": PAGE},
        headers=_h(key, SEAT_A),
    ).json()
    asked = decisions.ask(
        db,
        agent="baba-operator",
        artifact_id=page["id"],
        question="Mine?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
        session=SEAT_A,
    )
    decisions.answer(db, asked.id, letter="a", note="", by="jeremy")

    assert len(client.get("/api/v1/events", headers=_h(key)).json()) == 1
    hit = client.get(f"/api/v1/events?session={SEAT_A}", headers=_h(key)).json()
    assert len(hit) == 1
    miss = client.get(f"/api/v1/events?session={SEAT_B}", headers=_h(key)).json()
    assert miss == []


def test_rows_with_no_session_are_left_out_of_a_filtered_feed(client, key, db):
    """The cautious direction, on purpose: everything written before sessions
    existed has NULL here, and folding those into every filtered feed would
    reproduce exactly the noise the filter is for."""
    page = client.post(
        "/api/v1/artifacts", json={"title": "Old", "body": PAGE}, headers=_h(key)
    ).json()
    asked = decisions.ask(
        db,
        agent="baba-operator",
        artifact_id=page["id"],
        question="Old?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    decisions.answer(db, asked.id, letter="a", note="", by="jeremy")

    assert len(events.list_for_agent(db, "baba-operator")) == 1
    assert events.list_for_agent(db, "baba-operator", session=SEAT_A) == []


# --- IT IS NOT A PERMISSION --------------------------------------------------


def test_a_different_session_can_still_read_and_edit_the_same_pages(client, key, db):
    """HE RULED THIS, and it is not an oversight to be tidied up later: "any
    agent with a key could theoretically edit another agents artifact or read it
    but that is okay if they are authenticated."

    So this test exists to STOP someone hardening it. If it ever fails because
    a scope was added on `session`, the scope is the bug.
    """
    made = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE},
        headers=_h(key, SEAT_A),
    ).json()

    seen = client.get(f"/api/v1/artifacts/{made['id']}", headers=_h(key, SEAT_B))
    assert seen.status_code == 200, "another session, same key, must still read it"

    edited = client.patch(
        f"/api/v1/artifacts/{made['id']}",
        json={"body": PAGE + "<!--b-->", "expected_revision": made["revision"]},
        headers=_h(key, SEAT_B),
    )
    assert edited.status_code == 200, "and must still edit it"


def test_the_label_is_unverified_and_the_test_says_so_out_loud(client, key, db):
    """Any key holder can claim to be any session. Asserted deliberately: this
    is the property that makes it unusable as a boundary, and writing it down is
    what stops the next reader assuming otherwise."""
    made = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE},
        headers=_h(key, "some-seat-that-has-never-existed/ffffff"),
    )
    assert made.status_code == 201
    with db.connect() as conn:
        row = conn.execute(
            "select session from artifact where id = %s", (made.json()["id"],)
        ).fetchone()
    assert row[0] == "some-seat-that-has-never-existed/ffffff"


def test_scoping_still_uses_the_agent_from_the_key(client, key, db):
    """The real boundary, unchanged. Another AGENT's page is 404 whatever
    session header is sent -- and 404 rather than 403, so a key cannot be walked
    to learn which ids are real."""
    keys.mint(db, agent="another-seat")
    theirs = store.publish(db, "another-seat", "Not yours", PAGE)
    store.set_agents_can_read(
        db, theirs.id, False, agent=None
    )  # closed to other agents: homelab-ij8.105 made pages readable by default
    got = client.get(f"/api/v1/artifacts/{theirs.id}", headers=_h(key, SEAT_A))
    assert got.status_code == 404


# --- a withdrawn question stays readable on the polling route ----------------


def test_the_polling_route_can_see_a_withdrawn_question(client, key, db):
    """homelab-ij8.44's second report: decisions.list_for_agent excluded closed
    rows with NO escape hatch, so the cross-artifact poll -- the surface an
    agent actually uses -- could never show one.

    The reason written in its docstring was "the agent withdrew it, so there is
    nothing left to learn". That stopped being true on 21 Sep, when the SERVICE
    started withdrawing questions whose page was archived or deleted.
    """
    page = client.post(
        "/api/v1/artifacts",
        json={"title": "Mine", "body": PAGE},
        headers=_h(key, SEAT_A),
    ).json()
    asked = decisions.ask(
        db,
        agent="baba-operator",
        artifact_id=page["id"],
        question="Mine?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
        session=SEAT_A,
    )
    store.set_archived(db, page["id"], True, agent=None)

    plain = client.get("/api/v1/decisions", headers=_h(key)).json()
    assert asked.id not in [d["id"] for d in plain], (
        "a withdrawn question is not waiting on anybody and must not be in the "
        "default poll"
    )

    withdrawn = client.get(
        "/api/v1/decisions?include_closed=true&include_answered=true",
        headers=_h(key),
    ).json()
    assert asked.id in [d["id"] for d in withdrawn]
    row = next(d for d in withdrawn if d["id"] == asked.id)
    assert row["closed_at"] is not None
    assert row["answer"] is None, "withdrawn, not answered -- different events"


# --- the listing says WHICH SESSION, because a shared key cannot -------------
#
# 22 Sep 2026, brought by the operator seat and ruled on by Jeremy the same
# morning. Everything above records the session; none of it was ever SURFACED,
# and what a listing cannot say, nobody checks. The cost was concrete: the cove
# seat's listing grew a page published by a different session under the same
# key, Jeremy attributed it to the wrong seat and nearly had it rewritten, and
# four of his answers sat uncollected for days because a departed session and a
# live one looked identical through a seat name.
#
# The rule these tests hold to, in the operator's words: a 404 under a key is a
# strong negative, a successful listing under a shared key is NOT a strong
# positive.


def test_a_listing_says_which_session_published_the_live_version(client, key, db):
    made = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE},
        headers=_h(key, SEAT_A),
    ).json()
    rows = client.get("/api/v1/artifacts", headers=_h(key, SEAT_A)).json()
    mine = next(r for r in rows if r["id"] == made["id"])
    assert mine["last_session"] == SEAT_A


def test_two_sessions_behind_one_key_are_told_apart_in_the_listing(client, key, db):
    """The failure itself: one key, two sessions, and a listing that used to
    show ten indistinguishable pages."""
    first = client.post(
        "/api/v1/artifacts",
        json={"title": "Mine", "body": PAGE},
        headers=_h(key, SEAT_A),
    ).json()
    second = client.post(
        "/api/v1/artifacts",
        json={"title": "Theirs", "body": PAGE},
        headers=_h(key, SEAT_B),
    ).json()
    rows = {
        r["id"]: r
        for r in client.get("/api/v1/artifacts", headers=_h(key, SEAT_A)).json()
    }
    assert rows[first["id"]]["last_session"] == SEAT_A
    assert rows[second["id"]]["last_session"] == SEAT_B, (
        "both pages are listed to either session -- that is deliberate and "
        "Jeremy ruled on it -- so the listing has to say which is whose"
    )


def test_the_listing_follows_the_LIVE_version_not_the_first_one(client, key, db):
    """'Who owes Jeremy an answer about this page' is about whoever published
    what he is reading now, not whoever started it a week ago."""
    made = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE},
        headers=_h(key, SEAT_A),
    ).json()
    client.patch(
        f"/api/v1/artifacts/{made['id']}",
        json={"body": PAGE + "<!--2-->", "expected_revision": made["revision"]},
        headers=_h(key, SEAT_B),
    )
    rows = client.get("/api/v1/artifacts", headers=_h(key, SEAT_A)).json()
    assert next(r for r in rows if r["id"] == made["id"])["last_session"] == SEAT_B


def test_a_page_published_before_sessions_existed_says_nothing_not_nobody(
    client, key, db
):
    """Unknown and unattributed are different facts, and printing one as the
    other is how a stale attribution gets believed."""
    made = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE},
        headers=_h(key, None),
    ).json()
    rows = client.get("/api/v1/artifacts", headers=_h(key, SEAT_A)).json()
    assert next(r for r in rows if r["id"] == made["id"])["last_session"] is None


def test_the_deleted_listing_says_it_too(client, key, db):
    """The bin is where 'whose was this?' is hardest and matters most: nobody
    remembers, and restoring someone else's page is the mistake available."""
    made = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE},
        headers=_h(key, SEAT_B),
    ).json()
    assert (
        client.delete(
            f"/api/v1/artifacts/{made['id']}", headers=_h(key, SEAT_B)
        ).status_code
        == 200
    )
    binned = client.get("/api/v1/artifacts/deleted", headers=_h(key, SEAT_A)).json()
    assert next(r for r in binned if r["id"] == made["id"])["last_session"] == SEAT_B
