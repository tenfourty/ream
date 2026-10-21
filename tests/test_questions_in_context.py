"""Questions in context on the page (runcove-5t3oj.39.2): the open list at the
top, the chained hold, the context shown with each question, and `held_by` for
agents."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, onboarding, store
from artifacts.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>t</title></head><body><p>plan</p></body></html>"
)
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


@pytest.fixture()
def client(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def page(db):
    return store.publish(db, "ops-seat", "Board", PAGE)


def ask(db, page, question="ship?", **fields):
    return decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=page.id,
        question=question,
        options=OPTIONS,
        **fields,
    )


def iso(**delta):
    return (datetime.now(UTC) + timedelta(**delta)).isoformat()


def view(client, page):
    response = client.get(f"/a/{page.id}", headers={"Remote-User": "jeremy"})
    assert response.status_code == 200
    return response.text


def open_list(html):
    match = re.search(r'<nav class="openlist".*?</nav>', html, re.DOTALL)
    return match.group(0) if match else None


def listed_ids(html):
    block = open_list(html)
    return re.findall(r'href="#q-([^"]+)"', block) if block else []


def answer(client, decision_id, letter="a"):
    return client.post(
        f"/d/{decision_id}/answer", data={"letter": letter}, headers=SAME_ORIGIN
    )


# --- the open list --------------------------------------------------------


def test_nothing_open_means_no_list(db, client, page):
    assert open_list(view(client, page)) is None
    row = ask(db, page)
    assert answer(client, row.id).status_code in (200, 303)
    assert open_list(view(client, page)) is None


def test_each_entry_links_to_its_question_on_the_page(db, client, page):
    row = ask(db, page, "ship it?")
    html = view(client, page)
    assert f'href="#q-{row.id}"' in open_list(html)
    assert f'id="q-{row.id}"' in html
    assert "ship it?" in open_list(html)


def test_overdue_comes_first_then_urgency(db, client, page):
    later = ask(db, page, "later")
    by = ask(db, page, "by", urgency="by", needed_by=iso(days=2))
    blocking = ask(db, page, "blocking", urgency="blocking", blocked_on="deploy")
    overdue = ask(db, page, "overdue", needed_by=iso(hours=-3))
    assert listed_ids(view(client, page)) == [
        overdue.id,
        blocking.id,
        by.id,
        later.id,
    ]


def test_soonest_needed_by_then_topic_then_created(db, client, page):
    far = ask(db, page, "far", urgency="by", needed_by=iso(days=5))
    near = ask(db, page, "near", urgency="by", needed_by=iso(days=1))
    assert listed_ids(view(client, page)) == [near.id, far.id]

    # No needed-by after one with it, within the same urgency.
    plain = ask(db, page, "plain")
    dated = ask(db, page, "dated", needed_by=iso(days=3))
    ids = listed_ids(view(client, page))
    assert ids.index(dated.id) < ids.index(plain.id)

    # Same urgency and time: topic title (none last), then created order.
    topic_b = ask(db, page, "b", topic={"id": "tb", "title": "Beta"})
    topic_a = ask(db, page, "a", topic={"id": "ta", "title": "alpha"})
    untitled = ask(db, page, "untitled")
    first = ask(db, page, "first-asked")
    ids = listed_ids(view(client, page))
    group = [i for i in ids if i in (topic_b.id, topic_a.id, untitled.id, first.id)]
    assert group == [topic_a.id, topic_b.id, untitled.id, first.id]


def test_a_blocking_question_names_what_it_blocks_and_overdue_is_marked(
    db, client, page
):
    ask(
        db,
        page,
        "go?",
        urgency="blocking",
        blocked_on="the cutover",
        needed_by=iso(hours=-1),
        topic={"id": "t", "title": "Cutover"},
    )
    block = open_list(view(client, page))
    assert "Cutover" in block
    assert "blocking: the cutover" in block
    assert "overdue" in block


def test_a_question_that_is_not_overdue_is_not_marked(db, client, page):
    ask(db, page, "go?", urgency="by", needed_by=iso(days=1))
    block = open_list(view(client, page))
    assert "needed by" in block
    assert "overdue" not in block


# --- the hold -------------------------------------------------------------


def test_a_held_question_has_no_form_and_is_not_listed(db, client, page):
    first = ask(db, page, "first?")
    second = ask(db, page, "second?", depends_on=[first.id])
    html = view(client, page)
    assert listed_ids(html) == [first.id]
    assert f'action="/d/{first.id}/answer"' in html
    assert f'action="/d/{second.id}/answer"' not in html
    assert "second?" in html
    assert "Waiting on an earlier question" in html
    assert f'href="#q-{first.id}"' in html


def test_a_held_question_refuses_his_answer(db, client, page):
    first = ask(db, page)
    second = ask(db, page, "second?", depends_on=[first.id])
    response = answer(client, second.id)
    assert response.status_code == 409
    assert first.id in response.text
    stored = decisions.list_for_artifact(db, page.id, include_answered=True)
    assert [r.answer for r in stored if r.id == second.id] == [None]


def test_it_opens_by_itself_when_the_earlier_one_is_answered(db, client, page):
    first = ask(db, page)
    second = ask(db, page, "second?", depends_on=[first.id])
    assert answer(client, first.id).status_code in (200, 303)
    html = view(client, page)
    assert f'action="/d/{second.id}/answer"' in html
    assert listed_ids(html) == [second.id]
    assert answer(client, second.id).status_code in (200, 303)


def test_a_withdrawn_prerequisite_releases_the_question(db, client, page):
    first = ask(db, page)
    second = ask(db, page, "second?", depends_on=[first.id])
    decisions.close(db, "ops-seat", first.id)
    html = view(client, page)
    assert f'action="/d/{second.id}/answer"' in html
    assert listed_ids(html) == [second.id]
    assert answer(client, second.id).status_code in (200, 303)


def test_a_replaced_prerequisite_releases_the_question(db, page):
    first = ask(db, page)
    second = ask(db, page, "second?", depends_on=[first.id])
    ask(db, page, "better first?", replaces=first.id)
    assert _held(db, second.id) == []


def test_a_chain_holds_each_link_on_its_own_prerequisite(db, client, page):
    a = ask(db, page, "a?")
    b = ask(db, page, "b?", depends_on=[a.id])
    c = ask(db, page, "c?", depends_on=[b.id])
    assert answer(client, c.id).status_code == 409
    assert answer(client, a.id).status_code in (200, 303)
    assert answer(client, c.id).status_code == 409  # b is still unanswered
    assert answer(client, b.id).status_code in (200, 303)
    assert answer(client, c.id).status_code in (200, 303)


def test_one_unanswered_prerequisite_among_several_holds(db, client, page):
    a = ask(db, page, "a?")
    b = ask(db, page, "b?")
    c = ask(db, page, "c?", depends_on=[a.id, b.id])
    answer(client, a.id)
    assert answer(client, c.id).status_code == 409
    answer(client, b.id)
    assert answer(client, c.id).status_code in (200, 303)


def test_an_already_answered_question_can_still_be_changed(db, client, page):
    first = ask(db, page)
    second = ask(db, page, "second?", depends_on=[first.id])
    answer(client, first.id)
    assert answer(client, second.id).status_code in (200, 303)
    assert answer(client, second.id, "b").status_code in (200, 303)


# --- agents see it too ----------------------------------------------------


def _held(db, decision_id):
    row = decisions.get_owned(db, "ops-seat", decision_id)
    return decisions.public(row)["held_by"]


def test_held_by_is_in_agent_output_and_clears_on_answer(db, client, page):
    first = ask(db, page)
    second = ask(db, page, "second?", depends_on=[first.id])
    assert first.held_by == []
    assert second.held_by == [first.id]
    assert _held(db, first.id) == []
    assert _held(db, second.id) == [first.id]
    listed = {
        r.id: decisions.public(r).get("held_by")
        for r in decisions.list_for_agent(db, "ops-seat")
    }
    assert listed == {first.id: [], second.id: [first.id]}
    per_page = {r.id: r.held_by for r in decisions.list_for_artifact(db, page.id)}
    assert per_page[second.id] == [first.id]
    answer(client, first.id)
    assert _held(db, second.id) == []


def test_held_by_over_the_api(db, page):
    from artifacts import keys
    from artifacts.app import create_gallery_app as app

    token, _ = keys.mint(db, agent="ops-seat")
    api = TestClient(app(db))
    headers = {"Authorization": f"Bearer {token}"}
    first = ask(db, page)
    second = ask(db, page, "second?", depends_on=[first.id])
    rows = api.get(f"/api/v1/artifacts/{page.id}/decisions", headers=headers).json()
    assert {r["id"]: r["held_by"] for r in rows} == {
        first.id: [],
        second.id: [first.id],
    }
    everything = api.get("/api/v1/decisions", headers=headers).json()
    assert {r["id"]: r["held_by"] for r in everything}[second.id] == [first.id]


# --- context and escaping -------------------------------------------------


def test_the_question_shows_its_context_topic_urgency_and_for(db, client, page):
    ask(
        db,
        page,
        "go?",
        context="the migration is half done",
        topic={"id": "infra", "title": "Infrastructure"},
        urgency="by",
        needed_by="2026-12-24T10:00:00+00:00",
        for_offices=["beta", "gamma"],
    )
    html = view(client, page)
    card = html[html.index('class="ask"') :]
    assert "the migration is half done" in card
    assert "Infrastructure" in card
    assert "needed by 24 Dec, 10:00 UTC" in card
    assert "also needed by beta, gamma" in card


def test_a_plain_question_shows_no_extra_lines(db, client, page):
    ask(db, page)
    html = view(client, page)
    assert 'class="qctx"' not in html
    assert 'class="qmeta"' not in html


EVIL = "<script>alert(1)</script>"


def test_agent_strings_are_escaped_everywhere_they_appear(db, client, page):
    first = ask(db, page, "first?")
    ask(
        db,
        page,
        "<b>q</b>",
        context=EVIL,
        topic={"id": "t", "title": EVIL},
        urgency="blocking",
        blocked_on=EVIL,
        depends_on=[first.id],
    )
    ask(
        db,
        page,
        "<b>q2</b>",
        context=EVIL,
        topic={"id": "t", "title": EVIL},
        urgency="blocking",
        blocked_on=EVIL,
    )
    html = view(client, page)
    assert "<script>alert(1)</script>" not in html
    assert "<b>q" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in open_list(html)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# --- placement is unchanged ----------------------------------------------


def test_a_slot_still_names_its_question_and_unplaced_ones_stay_above(db, client):
    body = PAGE.replace("<p>plan</p>", '<div data-artifact-decision="SLOT"></div>')
    page = store.publish(db, "ops-seat", "Slots", body)
    row = ask(db, page)
    html = view(client, page)
    # Every open question rides in the block above the page with its id; the
    # gallery script lifts the ones a slot names, the rest stay.
    assert f'data-decision-id="{row.id}"' in html
    assert 'id="open-asks"' in html
    assert html.index('id="open-list"') < html.index('id="open-asks"')


# --- onboarding -----------------------------------------------------------


def test_onboarding_says_what_is_true_now():
    text = onboarding.agents_txt("http://x")
    assert "comes in a later release" not in text
    assert "held_by" in text
    assert "refuses" in text or "refused" in text
    assert "held back" in onboarding.skill_md("http://x")
