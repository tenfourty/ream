"""What a decision caused, and what changed under it.

homelab-ij8.44. The model was question -> options -> answer -> answer_note and
stopped there, so the durable record of what a decision PRODUCED -- a PR, a
closed bead, a correction to the agent's own framing -- lived only in prose
somebody happened to rewrite. An agent hand-edited its page's HTML to say
"Done - PR #14, and the job was smaller than I claimed"; the structured object
still read as merely answered.

The case that shaped the design came from the operator on 21 Sep: they corrected
two promises on a page WHILE ITS QUESTION WAS STILL OPEN. So an outcome is not
only "what the answer produced", and it is not gated on there being an answer.
"""

import pytest
from fastapi.testclient import TestClient

from ream import decisions, keys, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


@pytest.fixture()
def asked(db):
    keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Board", PAGE)
    return page, decisions.ask(
        db,
        agent="seat",
        artifact_id=page.id,
        question="Which way?",
        options=[{"letter": "a", "label": "One"}, {"letter": "b", "label": "Two"}],
        recommended="a",
    )


# --- the record ---------------------------------------------------------------


def test_an_outcome_can_be_recorded_on_a_question_he_has_not_answered(db, asked):
    """THE CASE WITH TEETH. A question whose premise moved after Jeremy read it
    and before he answered is one he can answer wrongly through no fault of his
    own, and until this there was nowhere to say so."""
    _, decision = asked
    written = decisions.record_outcome(
        db,
        decision.id,
        agent="seat",
        body="Framing corrected: the second promise was wrong.",
    )
    assert written.decision_id == decision.id
    assert decisions.outcomes_for(db, decision.id)[0].body == written.body


def test_outcomes_accumulate_and_none_replaces_another(db, asked):
    """Append only. The failure this fixes IS a summary that somebody keeps
    rewriting until what actually happened is gone, so there is no update and
    no 'current outcome' anywhere."""
    _, decision = asked
    for body in ("PR #14 opened.", "Bead closed.", "The job was smaller than I said."):
        decisions.record_outcome(db, decision.id, agent="seat", body=body)
    got = [o.body for o in decisions.outcomes_for(db, decision.id)]
    assert got == ["PR #14 opened.", "Bead closed.", "The job was smaller than I said."]


def test_it_records_which_session_wrote_it(db, asked):
    """Three seats share one key, so 'who recorded this' is otherwise only
    answerable as 'that machine'."""
    _, decision = asked
    written = decisions.record_outcome(
        db, decision.id, agent="seat", body="Done.", session="ops/abc123"
    )
    assert written.agent == "seat"
    assert written.session == "ops/abc123"


def test_another_agents_decision_is_not_writable(db, asked):
    """Scoped like every other write: 404 covers both 'no such decision' and
    'not yours', deliberately indistinguishable."""
    _, decision = asked
    keys.mint(db, agent="other")
    with pytest.raises(store.NotFound):
        decisions.record_outcome(db, decision.id, agent="other", body="mine now")
    with pytest.raises(store.NotFound):
        decisions.record_outcome(db, "nosuchdecision", agent="seat", body="x")


def test_an_outcome_is_scanned_like_everything_else_that_is_stored(db, asked):
    """ "It is only a note" is how a field skips its checks."""
    _, decision = asked
    with pytest.raises(store.SecretFound):
        decisions.record_outcome(
            db,
            decision.id,
            agent="seat",
            body="deployed with AKIA" + "IOSFODNN7EXAMPLE",
        )
    assert decisions.outcomes_for(db, decision.id) == []


def test_it_is_bounded(db, asked):
    _, decision = asked
    with pytest.raises(store.BodyTooLarge):
        decisions.record_outcome(
            db, decision.id, agent="seat", body="x" * (decisions.MAX_OUTCOME_BYTES + 1)
        )


def test_destroying_the_page_takes_its_outcomes_with_it(db, asked):
    page, decision = asked
    decisions.record_outcome(db, decision.id, agent="seat", body="Done.")
    store.destroy(db, page.id, agent=None)
    with db.connect() as conn:
        assert conn.execute("select count(*) from decision_outcome").fetchone()[0] == 0


# --- what Jeremy sees ----------------------------------------------------------


def test_an_open_question_shows_what_moved_under_it_BEFORE_the_buttons(
    db, asked, client
):
    """He must read it before he chooses, not after. Asserted as an ORDER,
    because a warning below the thing it warns about is not a warning."""
    page, decision = asked
    decisions.record_outcome(
        db,
        decision.id,
        agent="seat",
        body="Framing corrected: the second promise was wrong.",
    )
    body = client.get(f"/a/{page.id}").text
    assert "Since you were asked" in body
    assert "the second promise was wrong" in body
    assert body.index("the second promise was wrong") < body.index(
        f'action="/d/{decision.id}/answer"'
    ), "the warning rendered after the buttons it is warning about"


def test_a_settled_question_calls_it_what_it_led_to(db, asked, client):
    page, decision = asked
    decisions.answer(db, decision.id, letter="a", note="", by="jeremy")
    decisions.record_outcome(db, decision.id, agent="seat", body="PR #14 opened.")
    body = client.get(f"/a/{page.id}").text
    assert "What it led to" in body
    assert "PR #14 opened." in body


def test_a_decision_with_nothing_to_report_renders_no_block_at_all(db, asked, client):
    page, _ = asked
    body = client.get(f"/a/{page.id}").text
    assert '<div class="since">' not in body


def test_an_outcome_is_escaped_on_the_page(db, asked, client):
    """It is agent-written text landing in Jeremy's own signed-in origin."""
    page, decision = asked
    decisions.record_outcome(
        db, decision.id, agent="seat", body="<script>alert(1)</script> done"
    )
    body = client.get(f"/a/{page.id}").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


# --- over HTTP, which is how an agent anywhere reaches it ----------------------


def test_an_agent_records_and_reads_an_outcome_over_the_api(db, asked, client):
    _page, decision = asked
    token, _ = keys.mint(db, agent="seat")
    h = {"Authorization": f"Bearer {token}", "X-Artifacts-Session": "seat/xyz"}

    made = client.post(
        f"/api/v1/decisions/{decision.id}/outcome",
        json={"body": "PR #14 opened; smaller than I claimed."},
        headers=h,
    )
    assert made.status_code == 201
    assert made.json()["session"] == "seat/xyz"

    listed = client.get(f"/api/v1/decisions/{decision.id}/outcomes", headers=h).json()
    assert [o["body"] for o in listed] == ["PR #14 opened; smaller than I claimed."]


def test_outcomes_come_back_with_the_decisions_rather_than_needing_a_second_ask(
    db, asked, client
):
    """A caller reading "what happened here" should not have to know to ask
    twice, and the one thing an outcome is for is being read beside the
    decision it is about."""
    page, decision = asked
    token, _ = keys.mint(db, agent="seat")
    h = {"Authorization": f"Bearer {token}"}
    decisions.record_outcome(db, decision.id, agent="seat", body="Bead closed.")

    rows = client.get(f"/api/v1/artifacts/{page.id}/decisions", headers=h).json()
    assert [o["body"] for o in rows[0]["outcomes"]] == ["Bead closed."]


def test_another_seat_gets_the_same_404_for_not_yours_and_no_such_decision(
    db, asked, client
):
    _, decision = asked
    other, _ = keys.mint(db, agent="other-seat")
    h = {"Authorization": f"Bearer {other}"}

    theirs = client.post(
        f"/api/v1/decisions/{decision.id}/outcome", json={"body": "x"}, headers=h
    )
    missing = client.post(
        "/api/v1/decisions/nosuchdecision/outcome", json={"body": "x"}, headers=h
    )
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json() == missing.json()


def test_there_is_no_way_to_edit_or_delete_one(db, asked, client):
    """Append only is the point, not an omission. If a PUT or DELETE ever
    appears here, the record stops being a record."""
    _, decision = asked
    token, _ = keys.mint(db, agent="seat")
    h = {"Authorization": f"Bearer {token}"}
    made = client.post(
        f"/api/v1/decisions/{decision.id}/outcome", json={"body": "Done."}, headers=h
    ).json()

    for method in ("put", "patch", "delete"):
        got = getattr(client, method)(
            f"/api/v1/decisions/{decision.id}/outcomes/{made['id']}", headers=h
        )
        assert got.status_code in (404, 405), f"{method} reached something"


# --- the 404 that says which of two things went wrong -------------------------


def test_an_unknown_decision_route_names_the_real_ones(db, asked, client):
    """The operator's first `artifacts-decide outcome` answered a bare 404 and
    could not distinguish "the route is not deployed yet" (it was not; the push
    was four minutes old) from "this key does not own that decision". Those need
    opposite responses and looked identical.
    """
    _page, decision = asked
    token, _ = keys.mint(db, agent="seat")
    got = client.post(
        f"/api/v1/decisions/{decision.id}/result",
        json={"body": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert got.status_code == 404
    detail = got.json()["detail"]
    assert "outcome" in detail["routes"]
    assert "outcome" in (detail["did_you_mean"] or "")
    assert "older than the client" in detail["note"], (
        "the one thing the reader needs to know when a route is simply absent"
    )


def test_the_catch_all_has_not_swallowed_the_real_routes(db, asked, client):
    """It is declared last on purpose -- FastAPI matches in declaration order,
    so a path route above it wins. Asserted because getting this wrong breaks
    every decision verb at once and the tests for each would all fail with the
    same confusing 404."""
    _page, decision = asked
    token, _ = keys.mint(db, agent="seat")
    h = {"Authorization": f"Bearer {token}"}

    assert (
        client.post(
            f"/api/v1/decisions/{decision.id}/outcome", json={"body": "real"}, headers=h
        ).status_code
        == 201
    )
    assert (
        client.get(f"/api/v1/decisions/{decision.id}/outcomes", headers=h).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/decisions/{decision.id}/acknowledge", headers=h
        ).status_code
        == 200
    )


def test_not_yours_still_reads_differently_from_no_such_route(db, asked, client):
    """The two 404s a caller has to tell apart. Same status, different bodies,
    and now the absent-route one says so in words."""
    _page, decision = asked
    other, _ = keys.mint(db, agent="other-seat")
    h = {"Authorization": f"Bearer {other}"}

    not_yours = client.post(
        f"/api/v1/decisions/{decision.id}/outcome", json={"body": "x"}, headers=h
    )
    no_route = client.post(
        f"/api/v1/decisions/{decision.id}/nonsense", json={"body": "x"}, headers=h
    )
    assert not_yours.status_code == no_route.status_code == 404
    assert not_yours.json()["detail"] == "no such decision"
    assert isinstance(no_route.json()["detail"], dict)
