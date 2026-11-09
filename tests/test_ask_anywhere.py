"""A signed-in app asks on any page he can read (MCP sign-in design 11.4, slice 8): the
question records who asked, the answer goes to the asker, the page's agent is told a question
was put on its page, only the asker closes it, and the card says who asked. A key still asks on
its own pages only."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_mcp_reach import PAGE, _app, _as, _as_key, _tool

from ream import decisions, events, gallery, ids, keys, settings, store
from ream.app import create_gallery_app

OPTIONS = [{"letter": "a", "label": "Alpha"}, {"letter": "b", "label": "Beta"}]
ASKING = {"question": "Which one?", "options": OPTIONS}


@pytest.fixture()
def page(db):
    keys.mint(db, agent="office")
    made = store.publish(db, "office", "Board", PAGE.format("plan"))
    # Closed to agents: the app reads it as the owner, another office's key could not.
    store.set_agents_can_read(db, made.id, False, agent="office")
    return made


def _ask_as_app(db, page_id, **extra):
    app = _app(db)
    out = _tool(_as(db, app), "ask", page_id=page_id, **ASKING, **extra)
    return app, out


def _kinds(db, agent, kind):
    return [r for r in events.list_for_agent(db, agent, since=0) if r.kind == kind]


def test_an_app_asks_on_a_page_another_agent_published(db, page):
    app, out = _ask_as_app(db, page.id)
    assert out["isError"] is False, out
    row = decisions.get_owned(db, app.agent, out["structuredContent"]["id"])
    assert row.asked_by == app.agent
    assert row.artifact_id == page.id
    assert row.answer is None


def test_an_app_asking_on_its_own_page_stores_no_asker(db):
    app = _app(db)
    own = store.publish(db, app.agent, "Mine", PAGE.format("x"))
    out = _tool(_as(db, app), "ask", page_id=own.id, **ASKING)
    assert out["isError"] is False, out
    row = decisions.get_owned(db, app.agent, out["structuredContent"]["id"])
    assert row.asked_by is None


def test_a_question_an_agent_asks_stores_no_asker(db, page):
    row = decisions.ask(
        db, agent="office", artifact_id=page.id, question="Q?", options=OPTIONS
    )
    assert row.asked_by is None
    assert decisions.get_owned(db, "office", row.id).asked_by is None


def test_a_key_still_cannot_ask_on_another_agents_page(db, page):
    keys.mint(db, agent="seat")
    store.set_agents_can_read(
        db, page.id, True, agent="office"
    )  # even one open to agents
    refused = _tool(_as_key(db, "seat"), "ask", page_id=page.id, **ASKING)
    missing = _tool(_as_key(db, "seat"), "ask", page_id=ids.new_id(), **ASKING)
    assert refused["isError"] is True
    # The same words as for a page that does not exist: a key learns nothing.
    assert refused["content"] == missing["content"]
    with pytest.raises(store.NotFound):
        decisions.ask(
            db, agent="seat", artifact_id=page.id, question="Q?", options=OPTIONS
        )
    assert decisions.list_for_agent(db, "office", include_answered=True) == []


def test_a_key_cannot_borrow_an_apps_reach_on_the_call_path(db, page):
    # The reach is set for one message and only for the agent that carries it.
    app = _app(db)
    _tool(_as(db, app), "ask", page_id=page.id, **ASKING)
    with pytest.raises(store.NotFound):
        decisions.ask(
            db, agent="seat", artifact_id=page.id, question="Q?", options=OPTIONS
        )


def test_an_app_cannot_ask_on_a_binned_page_or_a_missing_one(db, page):
    store.set_deleted(db, page.id, True, agent="office")
    _, binned = _ask_as_app(db, page.id)
    _, missing = _ask_as_app(db, ids.new_id())
    assert binned["isError"] is True
    assert binned["content"] == missing["content"]


def test_the_answer_wakes_the_apps_feed_and_not_the_page_agents(db, page):
    app, out = _ask_as_app(db, page.id)
    qid = out["structuredContent"]["id"]
    decisions.answer(db, qid, letter="b", by=settings.owner_id(), note="go")
    mine = [r for r in _kinds(db, app.agent, "decision_answered") if r.ref_id == qid]
    assert len(mine) == 1 and mine[0].note == "go"
    assert [
        r for r in _kinds(db, "office", "decision_answered") if r.ref_id == qid
    ] == []
    # And through the tool the app reads its feed with.
    feed = _tool(_as(db, app), "whats_new", since=0)
    assert any(
        e["kind"] == "decision_answered" and e["ref_id"] == qid
        for e in feed["structuredContent"]["events"]
    )


def test_an_unread_answer_is_the_apps_to_collect_not_the_page_agents(db, page):
    app, out = _ask_as_app(db, page.id)
    qid = out["structuredContent"]["id"]
    decisions.answer(db, qid, letter="a", by=settings.owner_id())
    owed = decisions.uncollected(db, stuck_after_seconds=0)
    assert [(u.decision.id, u.agent) for u in owed] == [(qid, app.agent)]
    decisions.acknowledge(db, app.agent, qid)
    assert decisions.uncollected(db, stuck_after_seconds=0) == []


def test_a_relay_by_the_app_goes_to_the_app_too(db, page):
    app, out = _ask_as_app(db, page.id)
    qid = out["structuredContent"]["id"]
    relayed = _tool(
        _as(db, app), "record_answer", decision_id=qid, letter="a", words="ok"
    )
    assert relayed["isError"] is False, relayed
    assert [
        r for r in _kinds(db, "office", "decision_answered") if r.ref_id == qid
    ] == []
    assert [r for r in _kinds(db, app.agent, "decision_answered") if r.ref_id == qid]


def test_the_page_agent_is_told_a_question_was_put_on_its_page(db, page):
    app, out = _ask_as_app(db, page.id)
    qid = out["structuredContent"]["id"]
    told = _kinds(db, "office", "question_asked")
    assert len(told) == 1
    assert (told[0].actor, told[0].ref_id, told[0].artifact_id) == (
        app.agent,
        qid,
        page.id,
    )
    assert "Which one?" in told[0].summary
    # The asker is not told about its own question; nothing else is asked of the agent.
    assert _kinds(db, app.agent, "question_asked") == []


def test_no_notice_for_a_question_on_ones_own_page(db, page):
    decisions.ask(
        db, agent="office", artifact_id=page.id, question="Q?", options=OPTIONS
    )
    assert _kinds(db, "office", "question_asked") == []


def test_only_the_asker_can_withdraw_it(db, page):
    app, out = _ask_as_app(db, page.id)
    qid = out["structuredContent"]["id"]
    # The page's agent cannot close it, by the store or by the tool, and cannot read it as
    # its own either: "not yours" is "no such decision".
    with pytest.raises(store.NotFound):
        decisions.close(db, "office", qid)
    refused = _tool(_as_key(db, "office"), "close_question", decision_id=qid)
    assert refused["isError"] is True
    assert decisions.get_owned(db, "office", qid) is None
    assert decisions.get_owned(db, app.agent, qid).closed_at is None
    closed = _tool(_as(db, app), "close_question", decision_id=qid)
    assert closed["isError"] is False, closed
    assert decisions.get_owned(db, app.agent, qid).closed_at is not None


def test_a_question_set_aside_with_an_archived_page_is_not_answerable(db, page):
    app, out = _ask_as_app(db, page.id)
    assert out["isError"] is False
    store.set_archived(db, page.id, True, agent="office")
    row = decisions.get_owned(db, app.agent, out["structuredContent"]["id"])
    assert row.set_aside is True


def test_the_question_can_replace_an_earlier_one_of_the_apps(db, page):
    app, first = _ask_as_app(db, page.id)
    old = first["structuredContent"]["id"]
    second = _tool(_as(db, app), "ask", page_id=page.id, replaces=old, **ASKING)
    assert second["isError"] is False, second
    assert decisions.get_owned(db, app.agent, old).closed_at is not None


# --- the card --------------------------------------------------------------------------


def _view(db, page):
    client = TestClient(create_gallery_app(db))
    response = client.get(f"/a/{page.id}", headers={"Remote-User": "jeremy"})
    assert response.status_code == 200
    return response.text


def test_the_card_says_his_claude_asked(db, page):
    _ask_as_app(db, page.id)
    html = _view(db, page)
    card = html[html.index('class="ask"') :]
    assert "Which one?" in card
    assert f"Asked by {settings.owner_name()}, via claude.ai" in card
    assert "not by this page" in card


def test_a_page_agents_own_card_carries_no_such_line(db, page):
    decisions.ask(
        db, agent="office", artifact_id=page.id, question="Mine?", options=OPTIONS
    )
    assert 'class="qasker"' not in _view(db, page)


def test_an_answered_card_still_says_who_asked(db, page):
    _, out = _ask_as_app(db, page.id)
    decisions.answer(db, out["structuredContent"]["id"], letter="a", by="jeremy")
    html = _view(db, page)
    assert f"Asked by {settings.owner_name()}, via claude.ai" in html


def test_the_asker_name_is_escaped_on_the_card():
    row = SimpleNamespace(asked_by="<script>alert(1)</script>")
    html = gallery._asker_line(row, None)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    hosted = gallery._asker_line(row, {row.asked_by: "<b>host</b>"})
    assert "<b>" not in hosted
    assert gallery._asker_line(SimpleNamespace(asked_by=None), None) == ""
