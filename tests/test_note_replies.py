"""A short thread under the note he leaves when answering a decision
(Jeremy, 21 Oct 2026). Any agent that can read the page replies, he answers,
the office answers back; none of it ever touches the decision row.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import comments, decisions, events, grants, keys, store
from artifacts.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>t</title></head><body><p>plan</p></body></html>"
)
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db), base_url="https://testserver")


@pytest.fixture()
def seats(db):
    owner, _ = keys.mint(db, agent="owner")
    reader, _ = keys.mint(db, agent="reader")
    return {
        "owner": {"Authorization": f"Bearer {owner}"},
        "reader": {"Authorization": f"Bearer {reader}"},
    }


@pytest.fixture()
def page(db):
    return store.publish(db, "owner", "Plan", PAGE)


def _ask(db, page, answer="a", note="go, but watch the cost"):
    row = decisions.ask(
        db, agent="owner", artifact_id=page.id, question="ship?", options=OPTIONS
    )
    if answer:
        decisions.answer(db, row.id, letter=answer, note=note, by="jeremy")
    return row


def _reply(client, seats, decision_id, who="reader", body="what cost?", **extra):
    return client.post(
        f"/api/v1/decisions/{decision_id}/replies",
        json={"body": body},
        headers={**seats[who], **extra},
    )


def _row(db, page, decision_id):
    return next(
        r
        for r in decisions.list_for_artifact(
            db, page.id, include_answered=True, include_closed=True
        )
        if r.id == decision_id
    )


def _he_posts(client, page_id, root, body="the hosting bill"):
    return client.post(
        f"/c/{page_id}",
        data={"thread_root": root, "body": body},
        headers={"origin": store.gallery_origin(), "sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )


def test_the_owner_and_a_reader_both_reply(client, db, seats, page):
    ask = _ask(db, page)
    first = _reply(client, seats, ask.id, who="reader")
    assert first.status_code == 201
    assert first.json()["anchor"] == {"kind": "decision", "decision_id": ask.id}
    assert first.json()["thread_root"] is None
    second = _reply(client, seats, ask.id, who="owner", body="the hosting bill")
    assert second.status_code == 201
    assert second.json()["thread_root"] == first.json()["id"]
    threads = comments.note_threads(db, page.id)
    assert [m.body for m in threads[ask.id]] == ["what cost?", "the hosting bill"]


def test_a_reply_never_changes_the_decision(client, db, seats, page):
    ask = _ask(db, page)
    before = _row(db, page, ask.id)
    assert _reply(client, seats, ask.id).status_code == 201
    after = _row(db, page, ask.id)
    for field in ("answer", "answer_note", "acknowledged_at", "closed_at"):
        assert getattr(after, field) == getattr(before, field)


def test_closed_deleted_unknown_and_unreadable_all_answer_the_same_404(
    client, db, seats, page
):
    hidden = store.publish(db, "owner", "Hidden", PAGE)
    hidden_ask = _ask(db, hidden)
    store.set_agents_can_read(db, hidden.id, False, agent="owner")
    gone = store.publish(db, "owner", "Gone", PAGE)
    gone_ask = _ask(db, gone)
    store.set_deleted(db, gone.id, True, agent="owner")
    answers = set()
    for decision_id in (hidden_ask.id, gone_ask.id, "no-such-decision"):
        got = _reply(client, seats, decision_id, who="reader")
        assert got.status_code == 404
        answers.add(got.text)
    assert len(answers) == 1


def test_a_closed_but_answered_decision_still_takes_a_reply(client, db, seats, page):
    ask = _ask(db, page)
    decisions.close(db, "owner", ask.id)
    assert _reply(client, seats, ask.id).status_code == 201


def test_an_unanswered_decision_is_422(client, db, seats, page):
    ask = _ask(db, page, answer=None)
    got = _reply(client, seats, ask.id)
    assert got.status_code == 422
    assert got.json()["detail"] == {
        "field": "decision",
        "detail": "he has not answered yet",
    }


def test_his_reply_raises_comment_added_for_the_owner_and_the_owners_own_does_not(
    client, db, seats, page
):
    ask = _ask(db, page)
    root = _reply(client, seats, ask.id, who="owner").json()
    own = [e for e in events.list_for_agent(db, "owner") if e.kind == "comment_added"]
    assert own == []
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="the hosting bill",
        thread_root=root["id"],
    )
    heard = [e for e in events.list_for_agent(db, "owner") if e.kind == "comment_added"]
    assert len(heard) == 1 and heard[0].actor == "jeremy"
    _reply(client, seats, ask.id, who="reader", body="noted")
    heard = [e for e in events.list_for_agent(db, "owner") if e.kind == "comment_added"]
    assert sorted(e.actor for e in heard) == ["jeremy", "reader"]


def test_the_generic_route_refuses_a_new_decision_thread_but_takes_replies(
    client, db, seats, page
):
    ask = _ask(db, page)
    refused = client.post(
        f"/api/v1/artifacts/{page.id}/comments",
        json={
            "body": "x",
            "anchor": {"kind": "decision", "decision_id": ask.id},
        },
        headers=seats["reader"],
    )
    assert refused.status_code == 422
    assert "/decisions/" in refused.json()["detail"]["detail"]
    root = _reply(client, seats, ask.id).json()
    ok = client.post(
        f"/api/v1/artifacts/{page.id}/comments",
        json={"body": "more", "thread_root": root["id"]},
        headers=seats["reader"],
    )
    assert ok.status_code == 201


def test_a_decision_on_another_page_cannot_be_anchored(db, page):
    other = store.publish(db, "owner", "Other", PAGE)
    ask = _ask(db, other)
    with pytest.raises(comments.NoSuchDecision):
        comments.add(
            db,
            artifact_id=page.id,
            author_kind="agent",
            author="owner",
            agent="owner",
            body="x",
            anchor={"kind": "decision", "decision_id": ask.id},
        )


def test_it_is_never_held_during_a_held_review(client, db, seats, page):
    ask = _ask(db, page)
    decisions.ask_review(db, agent="owner", artifact_id=page.id, mode="held")
    root = _reply(client, seats, ask.id, who="owner").json()
    mine = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="the hosting bill",
        thread_root=root["id"],
    )
    assert mine.held is False and mine.review_round is None
    seen = client.get(f"/api/v1/artifacts/{page.id}/comments", headers=seats["owner"])
    assert mine.id in [c["id"] for c in seen.json()]


def test_the_note_thread_renders_under_his_note_even_when_closed(
    client, db, seats, page
):
    ask = _ask(db, page)
    _reply(
        client, seats, ask.id, body="what cost?", **{"X-Artifacts-Session": "cove/1"}
    )
    decisions.close(db, "owner", ask.id)
    html = client.get(f"/a/{page.id}").text
    assert f'id="note-{ask.id}"' in html
    assert "what cost?" in html and "go, but watch the cost" in html
    assert 'class="nreply"' in html
    assert f'action="/c/{page.id}"' in html


def test_a_closed_decision_with_no_thread_stays_hidden(client, db, page):
    ask = _ask(db, page)
    decisions.close(db, "owner", ask.id)
    assert "ship?" not in client.get(f"/a/{page.id}").text


def test_no_form_until_a_thread_exists(client, db, page):
    _ask(db, page)
    assert 'class="nreply"' not in client.get(f"/a/{page.id}").text


def test_the_drawer_and_its_count_leave_the_thread_out(client, db, seats, page):
    ask = _ask(db, page)
    _reply(client, seats, ask.id, body="a note reply")
    html = client.get(f"/a/{page.id}").text
    # Once on the page: under his note, never again in the drawer.
    assert html.count("a note reply") == 1
    everywhere = client.get(f"/a/{page.id}?comments=all").text
    assert everywhere.count("a note reply") == 1


def test_his_form_posts_and_redirects_to_the_note(client, db, seats, page):
    ask = _ask(db, page)
    root = _reply(client, seats, ask.id).json()
    got = _he_posts(client, page.id, root["id"])
    assert got.status_code == 303
    assert got.headers["location"] == f"/a/{page.id}#note-{ask.id}"
    thread = comments.note_threads(db, page.id)[ask.id]
    assert [m.author_kind for m in thread] == ["agent", "human"]
    json_post = client.post(
        f"/c/{page.id}",
        data={"thread_root": root["id"], "body": "again"},
        headers={
            "origin": store.gallery_origin(),
            "sec-fetch-site": "same-origin",
            "accept": "application/json",
        },
    )
    assert json_post.json() == {"reload": True}


def test_the_decisions_route_carries_the_replies(client, db, seats, page):
    ask = _ask(db, page)
    _reply(client, seats, ask.id)
    rows = client.get(
        f"/api/v1/artifacts/{page.id}/decisions", headers=seats["owner"]
    ).json()
    assert [r["body"] for r in rows[0]["replies"]] == ["what cost?"]


def test_the_read_all_grant_sees_the_answer_but_never_the_thread(
    client, db, seats, page
):
    """decisions:read-all reads questions, answers and outcomes on a page it
    cannot open, never the conversation under his note: that is a comment."""
    ask = _ask(db, page)
    _reply(client, seats, ask.id, who="owner")
    assert store.set_agents_can_read(db, page.id, False, agent="owner")
    ops, _ = keys.mint(db, agent="ops")
    grants.grant(db, "ops", grants.READ_ALL_DECISIONS)
    rows = client.get(
        f"/api/v1/artifacts/{page.id}/decisions",
        headers={"Authorization": f"Bearer {ops}"},
    ).json()
    assert rows[0]["answer"] == "a"
    assert rows[0]["replies"] == []


def test_a_new_reply_opens_the_entry_and_is_tagged(client, db, seats, page):
    ask = _ask(db, page)
    client.get(f"/a/{page.id}")
    _reply(client, seats, ask.id)
    html = client.get(f"/a/{page.id}").text
    assert "new reply" in html and '<details class="settled" open>' in html
    again = client.get(f"/a/{page.id}").text
    assert "new reply" not in again


def test_a_thread_started_before_a_re_answer_says_so(client, db, seats, page):
    ask = _ask(db, page)
    _reply(client, seats, ask.id)
    decisions.answer(db, ask.id, letter="b", note="no, actually", by="jeremy")
    html = client.get(f"/a/{page.id}").text
    assert "About an earlier answer" in html


def test_a_fresh_thread_does_not_say_it_is_about_an_earlier_answer(
    client, db, seats, page
):
    ask = _ask(db, page)
    _reply(client, seats, ask.id)
    assert "About an earlier answer" not in client.get(f"/a/{page.id}").text


def test_a_retried_keyed_reply_makes_one_comment(client, db, seats, page):
    ask = _ask(db, page)
    first = _reply(client, seats, ask.id, **{"Idempotency-Key": "k1"})
    again = _reply(client, seats, ask.id, **{"Idempotency-Key": "k1"})
    assert first.json()["id"] == again.json()["id"]
    assert len(comments.note_threads(db, page.id)[ask.id]) == 1


def test_two_roots_for_one_decision_read_as_one_thread(db, page):
    ask = _ask(db, page)
    for who in ("a", "b"):
        comments.add(
            db,
            artifact_id=page.id,
            author_kind="agent",
            author="owner",
            agent="owner",
            body=f"first from {who}",
            anchor={"kind": "decision", "decision_id": ask.id},
        )
    assert len(comments.note_threads(db, page.id)[ask.id]) == 2


def _he_answers_in(db, page, root_id, body="the hosting bill"):
    return comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body=body,
        thread_root=root_id,
    )


def _heard(db, agent):
    return [
        e
        for e in events.list_for_agent(db, agent)
        if e.kind == "comment_added" and e.actor == "jeremy"
    ]


def test_a_replier_on_another_offices_page_hears_his_answer(client, db, seats, page):
    ask = _ask(db, page)
    root = _reply(client, seats, ask.id, who="reader").json()
    assert _heard(db, "reader") == []
    mine = _he_answers_in(db, page, root["id"])
    got = _heard(db, "reader")
    assert len(got) == 1 and got[0].artifact_id == page.id
    assert got[0].ref_id == mine.id


def test_a_replier_that_lost_read_access_does_not_hear_him(client, db, seats, page):
    ask = _ask(db, page)
    root = _reply(client, seats, ask.id, who="reader").json()
    assert store.set_agents_can_read(db, page.id, False, agent="owner")
    _he_answers_in(db, page, root["id"])
    assert _heard(db, "reader") == []
    assert len(_heard(db, "owner")) == 1


def test_the_owner_gets_one_event_when_it_and_a_reader_are_both_in_the_thread(
    client, db, seats, page
):
    ask = _ask(db, page)
    root = _reply(client, seats, ask.id, who="owner").json()
    _reply(client, seats, ask.id, who="reader", body="noted")
    _he_answers_in(db, page, root["id"])
    assert len(_heard(db, "owner")) == 1
    assert len(_heard(db, "reader")) == 1


def test_his_ordinary_comment_does_not_reach_a_note_replier(client, db, seats, page):
    ask = _ask(db, page)
    _reply(client, seats, ask.id, who="reader")
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="unrelated",
    )
    assert _heard(db, "reader") == []


def test_a_wrong_decision_subroute_points_at_replies(client, seats):
    got = client.post(
        "/api/v1/decisions/whatever/reply", json={}, headers=seats["reader"]
    )
    assert got.status_code == 404
    detail = got.json()["detail"]
    assert "replies" in detail["routes"]
    assert detail["did_you_mean"].startswith("replies")
