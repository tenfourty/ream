"""His inbox (runcove-5t3oj.39.3): every open question, open review and unseen
reply in one list in one order, answerable in place, with an API twin."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from artifacts import comments, decisions, grants, inbox, keys, onboarding, store
from artifacts.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>t</title></head><body><p>plan</p></body></html>"
)
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
HIM = {"Remote-User": "jeremy"}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db), base_url="https://testserver")


@pytest.fixture()
def page(db):
    return store.publish(db, "ops", "Board", PAGE)


def iso(**delta):
    return (datetime.now(UTC) + timedelta(**delta)).isoformat()


def ask(db, page, question="ship?", agent="ops", **fields):
    return decisions.ask(
        db,
        agent=agent,
        artifact_id=page.id,
        question=question,
        options=OPTIONS,
        **fields,
    )


def his(db, page, body="what about x?"):
    return comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body=body
    )


def reply(db, page, root, body="an answer", session="cove/ab12", author="ops"):
    return comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author=author,
        agent=author,
        body=body,
        thread_root=root.id,
        session=session,
    )


def inbox_html(client):
    response = client.get("/inbox", headers=HIM)
    assert response.status_code == 200
    return response.text


def ids_in(html):
    return re.findall(r'<li class="ask" id="i-([^"]+)"', html)


def test_it_holds_questions_reviews_and_unseen_replies(db, client, page):
    q = ask(db, page, "ship it?")
    root = his(db, page)
    client.get(f"/a/{page.id}", headers=HIM)  # he has opened it: nothing unseen
    r = reply(db, page, root, body="done, see v2")
    review = decisions.ask_review(db, agent="ops", artifact_id=page.id)
    html = inbox_html(client)
    assert set(ids_in(html)) == {q.id, review.id, r.id}
    assert "ship it?" in html and "done, see v2" in html
    assert "Reply from cove" in html and "Open the review" in html


def test_answered_closed_held_and_seen_are_left_out(db, client, page):
    answered = ask(db, page, "answered?")
    decisions.answer(db, answered.id, letter="a", note="", by="jeremy")
    closed = ask(db, page, "closed?")
    decisions.close(db, "ops", closed.id)
    first = ask(db, page, "first?")
    held = ask(db, page, "held?", depends_on=[first.id])
    root = his(db, page)
    old = reply(db, page, root, body="seen already")
    client.get(f"/a/{page.id}", headers=HIM)  # opening the page sees the reply
    assert old.id not in ids_in(inbox_html(client))
    found = ids_in(inbox_html(client))
    assert found == [first.id]
    assert answered.id not in found and closed.id not in found and held.id not in found


def test_a_reply_is_gone_once_he_opens_the_page(db, client, page):
    root = his(db, page)
    client.get(f"/a/{page.id}", headers=HIM)
    r = reply(db, page, root)
    assert ids_in(inbox_html(client)) == [r.id]
    client.get(f"/a/{page.id}", headers=HIM)
    assert ids_in(inbox_html(client)) == []
    assert "Nothing is waiting on you." in inbox_html(client)


def test_the_owner_talking_to_itself_is_not_a_reply_to_him(db, client, page):
    root = comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="ops",
        agent="ops",
        body="a note",
    )
    reply(db, page, root)
    assert ids_in(inbox_html(client)) == []


def test_order_overdue_then_urgency_then_needed_by_then_topic_then_age(
    db, client, page
):
    plain = ask(db, page, "plain")
    topic_b = ask(db, page, "b", topic={"id": "tb", "title": "Beta"})
    topic_a = ask(db, page, "a", topic={"id": "ta", "title": "alpha"})
    far = ask(db, page, "far", urgency="by", needed_by=iso(days=5))
    near = ask(db, page, "near", urgency="by", needed_by=iso(days=1))
    blocking = ask(db, page, "blocking", urgency="blocking", blocked_on="deploy")
    overdue = ask(db, page, "overdue", needed_by=iso(hours=-3))
    dated = ask(db, page, "dated", needed_by=iso(days=3))
    assert ids_in(inbox_html(client)) == [
        overdue.id,
        blocking.id,
        near.id,
        far.id,
        dated.id,
        topic_a.id,
        topic_b.id,
        plain.id,
    ]


def test_a_review_and_a_reply_sit_behind_the_questions_of_their_band(db, client, page):
    root = his(db, page)
    client.get(f"/a/{page.id}", headers=HIM)
    rep = reply(db, page, root)
    review = decisions.ask_review(db, agent="ops", artifact_id=page.id)
    calm = ask(db, page, "calm")
    blocking = ask(db, page, "blocking", urgency="blocking", blocked_on="x")
    overdue = ask(db, page, "overdue", urgency="by", needed_by=iso(hours=-1))
    # Overdue and blocking questions outrank both; a plain question of the
    # when-you-can band comes before the review and the reply, which are
    # then oldest first.
    assert ids_in(inbox_html(client)) == [
        overdue.id,
        blocking.id,
        calm.id,
        rep.id,
        review.id,
    ]


def test_the_overdue_mark_and_the_context_show(db, client, page):
    ask(
        db,
        page,
        "late?",
        context="because the cutover needs it",
        topic={"id": "t", "title": "Cutover"},
        needed_by=iso(hours=-2),
    )
    html = inbox_html(client)
    assert '<span class="over">overdue</span>' in html
    assert "because the cutover needs it" in html
    assert "Cutover" in html and "from ops" in html
    assert f'href="/a/{page.id}"' in html and f'href="/a/{page.id}#q-' in html


def test_answering_from_the_inbox_records_it_and_returns_to_the_inbox(db, client, page):
    q = ask(db, page, "ship?")
    other = ask(db, page, "second?")
    assert f'action="/d/{q.id}/answer"' in inbox_html(client)
    response = client.post(
        f"/d/{q.id}/answer",
        data={"letter": "b", "note": "later", "next": "inbox"},
        headers={**HIM, **SAME_ORIGIN},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/inbox"
    row = decisions.get_owned(db, "ops", q.id)
    assert row.answer == "b" and row.answer_note == "later"
    assert ids_in(inbox_html(client)) == [other.id]


def test_the_answer_form_is_still_guarded_and_next_is_never_an_open_redirect(
    db, client, page
):
    q = ask(db, page, "ship?")
    refused = client.post(
        f"/d/{q.id}/answer",
        data={"letter": "a", "next": "inbox"},
        headers={**HIM, "sec-fetch-site": "cross-site"},
        follow_redirects=False,
    )
    assert refused.status_code == 403
    assert decisions.get_owned(db, "ops", q.id).answer is None
    ok = client.post(
        f"/d/{q.id}/answer",
        data={"letter": "a", "next": "https://evil.example/"},
        headers={**HIM, **SAME_ORIGIN},
        follow_redirects=False,
    )
    assert ok.headers["location"] == f"/a/{page.id}"


def test_the_wall_links_to_the_inbox_with_the_count(db, client, page):
    assert 'href="/inbox">Inbox (0)</a>' in client.get("/", headers=HIM).text
    ask(db, page, "one")
    ask(db, page, "two")
    decisions.ask_review(db, agent="ops", artifact_id=page.id)
    assert 'href="/inbox">Inbox (3)</a>' in client.get("/", headers=HIM).text


def test_every_agent_string_is_escaped(db, client):
    evil = "<script>alert(1)</script>"
    bad = store.publish(db, "ops", f"T {evil}", PAGE)
    ask(
        db,
        bad,
        f"Q {evil}",
        context=f"C {evil}",
        topic={"id": "t", "title": f"Top {evil}"},
        blocked_on=evil,
        urgency="blocking",
    )
    root = his(db, bad)
    client.get(f"/a/{bad.id}", headers=HIM)
    reply(db, bad, root, body=f"R {evil}", session=f"{evil}/1")
    html = inbox_html(client)
    assert "<script>alert" not in html
    assert html.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 4


# --- the API twin -------------------------------------------------------------


@pytest.fixture()
def seats(db):
    ops, _ = keys.mint(db, agent="ops")
    other, _ = keys.mint(db, agent="other")
    return {
        "ops": {"Authorization": f"Bearer {ops}"},
        "other": {"Authorization": f"Bearer {other}"},
    }


def test_the_api_lists_own_open_questions_in_inbox_order_held_last(
    db, client, seats, page
):
    plain = ask(db, page, "plain")
    first = ask(db, page, "first?", urgency="blocking", blocked_on="x")
    held = ask(
        db, page, "held?", urgency="blocking", blocked_on="x", depends_on=[first.id]
    )
    overdue = ask(db, page, "overdue", needed_by=iso(hours=-3))
    answered = ask(db, page, "answered")
    decisions.answer(db, answered.id, letter="a", note="", by="jeremy")
    decisions.ask_review(db, agent="ops", artifact_id=page.id)
    theirs = store.publish(db, "other", "Theirs", PAGE)
    ask(db, theirs, "not mine", agent="other")
    response = client.get("/api/v1/questions?open=1", headers=seats["ops"])
    assert response.status_code == 200
    rows = response.json()
    assert [r["id"] for r in rows] == [overdue.id, first.id, plain.id, held.id]
    by_id = {r["id"]: r for r in rows}
    assert by_id[held.id]["held_by"] == [first.id]
    assert by_id[first.id]["held_by"] == []
    assert by_id[overdue.id]["overdue"] is True and by_id[plain.id]["overdue"] is False
    assert {"context", "topic", "urgency", "needed_by", "for", "agent"} <= set(rows[0])
    assert rows[0]["artifact_title"] == "Board"


def test_scope_all_needs_the_grant_and_is_refused_like_decisions(
    db, client, seats, page
):
    ask(db, page, "mine")
    theirs = store.publish(db, "other", "Theirs", PAGE)
    ask(db, theirs, "theirs", agent="other")
    refused = client.get("/api/v1/questions?scope=all", headers=seats["ops"])
    existing = client.get("/api/v1/decisions?scope=all", headers=seats["ops"])
    assert refused.status_code == existing.status_code == 403
    assert refused.json() == existing.json()
    grants.grant(db, "ops", grants.READ_ALL_DECISIONS)
    rows = client.get("/api/v1/questions?scope=all", headers=seats["ops"]).json()
    assert {r["question"] for r in rows} == {"mine", "theirs"}
    assert {r["agent"] for r in rows} == {"ops", "other"}
    unkeyed = client.get("/api/v1/questions")
    assert unkeyed.status_code in (401, 403)


def test_the_api_and_the_inbox_agree_on_order_and_held_is_only_in_the_api(
    db, client, seats, page
):
    a = ask(db, page, "a", urgency="by", needed_by=iso(days=2))
    b = ask(db, page, "b", urgency="blocking", blocked_on="x")
    held = ask(db, page, "held", depends_on=[a.id])
    api = [
        r["id"] for r in client.get("/api/v1/questions", headers=seats["ops"]).json()
    ]
    assert api == [b.id, a.id, held.id]
    assert ids_in(inbox_html(client)) == [b.id, a.id]


def test_onboarding_tells_agents_about_the_inbox():
    text = onboarding.agents_txt("http://x")
    assert "/api/v1/questions?open=1" in text and "INBOX" in text
    assert "set urgency and needed_by" in text


def test_the_item_list_takes_a_second_source_by_value():
    now = datetime.now(UTC)
    extra = inbox.Item(
        kind="reply",
        id="x",
        artifact_id="p",
        page_title="t",
        office="o",
        created_at=now - timedelta(days=1),
    )
    later = inbox.Item(
        kind="reply",
        id="y",
        artifact_id="p",
        page_title="t",
        office="o",
        created_at=now,
    )
    assert [i.id for i in inbox.order([later, extra], now)] == ["x", "y"]


# --- review round: outcomes, the count, the cap, escaping, negative arms -------


def test_an_outcome_shows_above_the_buttons_on_the_inbox_card(db, client, page):
    q = ask(db, page, "ship?")
    decisions.record_outcome(db, q.id, agent="ops", body="the premise moved <b>x</b>")
    html = inbox_html(client)
    assert "Since you were asked" in html
    assert "the premise moved &lt;b&gt;x&lt;/b&gt;" in html
    assert html.index("the premise moved") < html.index(f'action="/d/{q.id}/answer"')


def test_the_count_equals_the_list_across_a_mixed_fixture(db, client, page):
    ask(db, page, "ready")
    first = ask(db, page, "first")
    ask(db, page, "held", depends_on=[first.id])
    done = ask(db, page, "done")
    decisions.answer(db, done.id, letter="a", note="", by="jeremy")
    root = his(db, page)
    client.get(f"/a/{page.id}", headers=HIM)
    reply(db, page, root)
    decisions.ask_review(db, agent="ops", artifact_id=page.id)
    total, capped = inbox.count(db)
    assert (total, capped) == (len(inbox.items(db)), False) == (4, False)
    assert 'href="/inbox">Inbox (4)</a>' in client.get("/", headers=HIM).text


def test_a_source_that_hits_its_cap_is_said_on_the_link_and_the_page(
    db, client, page, monkeypatch
):
    monkeypatch.setattr(inbox, "_LIMIT", 2)
    for n in range(3):
        ask(db, page, f"q{n}")
    items, capped = inbox.gather(db)
    assert capped and len(items) == 2
    assert inbox.count(db) == (3, True)
    assert 'href="/inbox">Inbox (2+)</a>' in client.get("/", headers=HIM).text
    assert "2+ items waiting on you" in inbox_html(client)


def test_each_agent_string_is_escaped_on_its_own(db, client):
    def evil(name):
        return f"<img src=x onerror={name}>"

    def gone(name):
        return evil(name).replace("<", "&lt;").replace(">", "&gt;")

    names = ["ptitle", "office", "topic", "blocked", "context", "question", "label"]
    bad = store.publish(db, "ops", f'{evil("ptitle")} a"b', PAGE)
    q = decisions.ask(
        db,
        agent="ops",
        artifact_id=bad.id,
        question=evil("question"),
        options=[
            {"letter": "a", "label": evil("label")},
            {"letter": "b", "label": "n"},
        ],
        context=evil("context"),
        topic={"id": "t", "title": evil("topic")},
        urgency="blocking",
        blocked_on=evil("blocked"),
    )
    root = his(db, bad)
    client.get(f"/a/{bad.id}", headers=HIM)
    reply(db, bad, root, body=evil("rbody"), session=f"{evil('office')}/1")
    html = inbox_html(client)
    for name in [*names, "rbody"]:
        assert evil(name) not in html, name
        assert gone(name) in html, name
    assert 'a"b' not in html and "a&#34;b" in html
    assert f'id="i-{q.id}"' in html


def test_a_reply_on_an_archived_or_deleted_page_is_not_waiting(db, client):
    pages = []
    for how in ("archived", "deleted"):
        p = store.publish(db, "ops", how, PAGE)
        root = his(db, p)
        client.get(f"/a/{p.id}", headers=HIM)
        reply(db, p, root)
        pages.append(p)
    assert len(ids_in(inbox_html(client))) == 2
    store.set_archived(db, pages[0].id, True, agent=None)
    store.set_deleted(db, pages[1].id, True, agent=None)
    assert ids_in(inbox_html(client)) == []
    assert inbox.count(db) == (0, False)


def test_a_withdrawn_or_held_reply_is_not_waiting(db, client, page):
    root = his(db, page)
    client.get(f"/a/{page.id}", headers=HIM)
    withdrawn = reply(db, page, root, body="withdrawn")
    held = reply(db, page, root, body="held")
    kept = reply(db, page, root, body="kept")
    comments.withdraw(db, withdrawn.id, kind="test")
    with db.connect() as conn:
        conn.execute("update comment set held = true where id = %s", (held.id,))
    assert ids_in(inbox_html(client)) == [kept.id]
    assert inbox.count(db)[0] == 1


def test_a_reply_under_his_answer_note_is_waiting(db, client, seats, page):
    q = ask(db, page, "ship?")
    decisions.answer(db, q.id, letter="a", note="why though?", by="jeremy")
    response = client.post(
        f"/api/v1/decisions/{q.id}/replies",
        json={"body": "because of the deadline"},
        headers=seats["ops"],
    )
    assert response.status_code == 201, response.text
    html = inbox_html(client)
    assert "because of the deadline" in html and "#comments" in html
    client.get(f"/a/{page.id}", headers=HIM)
    assert "because of the deadline" not in inbox_html(client)


def test_the_wall_unread_mark_follows_an_unseen_reply(db, client, page):
    root = his(db, page)
    client.get(f"/a/{page.id}", headers=HIM)
    assert 'data-unread="1"' not in client.get("/", headers=HIM).text
    reply(db, page, root)
    wall = client.get("/", headers=HIM).text
    assert 'data-unread="1"' in wall and "Reply from cove" in wall
    client.get(f"/a/{page.id}", headers=HIM)
    assert 'data-unread="1"' not in client.get("/", headers=HIM).text


def test_open_zero_and_a_bad_scope_are_refused(client, seats):
    assert (
        client.get("/api/v1/questions?open=0", headers=seats["ops"]).status_code == 422
    )
    bad = client.get("/api/v1/questions?scope=everyone", headers=seats["ops"])
    assert bad.status_code == 422


@pytest.mark.parametrize("value", ["Inbox", "/inbox", "https://evil.example/"])
def test_only_the_exact_word_returns_to_the_inbox(db, client, page, value):
    q = ask(db, page, "ship?")
    response = client.post(
        f"/d/{q.id}/answer",
        data={"letter": "a", "next": value},
        headers={**HIM, **SAME_ORIGIN},
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/a/{page.id}"


def test_own_scope_limit_counts_questions_not_reviews(db, page):
    ask(db, page, "only question")
    for _ in range(3):
        decisions.ask_review(db, agent="ops", artifact_id=page.id)
    rows = decisions.list_for_agent(db, "ops", limit=1, kind="question")
    assert [r.question for r in rows] == ["only question"]
