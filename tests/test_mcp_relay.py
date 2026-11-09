"""A signed-in app passes on his answers (MCP sign-in design 11.5, slice 7): stored as a
relay with the core source `mcp`, never as his tap, on any open question put to him."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_mcp_reach import PAGE, _app, _as, _send, _tool

from ream import decisions, events, gallery, keys, mcp, settings, store

OPTIONS = [{"letter": "a", "label": "Alpha"}, {"letter": "b", "label": "Beta"}]


@pytest.fixture()
def asked(db):
    keys.mint(db, agent="office")
    page = store.publish(db, "office", "Board", PAGE.format("x"))
    # Closed to agents: the app reads it as the owner, a key of another office would not.
    store.set_agents_can_read(db, page.id, False, agent="office")
    return decisions.ask(
        db, agent="office", artifact_id=page.id, question="Which?", options=OPTIONS
    )


def _text(out: dict) -> str:
    return out["content"][0]["text"]


def test_an_app_passes_on_an_answer_to_an_offices_question(db, asked):
    app = _app(db)
    out = _tool(
        _as(db, app), "record_answer", decision_id=asked.id, letter="b", words="go (b)"
    )
    assert out["isError"] is False, out
    row = decisions.get_owned(db, "office", asked.id)
    assert (row.answer, row.answer_note) == ("b", "go (b)")
    assert row.answer_source == "mcp" == decisions.MCP_SOURCE
    # The same semantics as a key's relay: the person is the answerer, the app's agent
    # is the one who wrote it down.
    assert row.answered_by == settings.owner_id()
    assert row.recorded_by == app.agent


def test_the_asking_offices_wait_wakes_with_the_relay(db, asked):
    _tool(
        _as(db, _app(db)), "record_answer", decision_id=asked.id, letter="a", words="ok"
    )
    rows = events.list_for_agent(db, agent="office", since=0)
    answered = [r for r in rows if r.kind in ("answered", "decision_answered")]
    assert answered
    assert any("relayed from mcp" in (r.summary or "") for r in answered)
    assert any(r.note == "ok" for r in answered if r.kind == "decision_answered")


def test_a_key_cannot_claim_source_mcp(db, asked):
    keys.mint(db, agent="office")
    with pytest.raises(decisions.InvalidSource):
        decisions.record_elsewhere(
            db, asked.id, letter="a", note="x", source="mcp", recorded_by="office"
        )
    assert "mcp" not in decisions.relay_sources()
    with pytest.raises(ValueError):
        decisions.register_source("mcp", relay=True)
    assert decisions.get_owned(db, "office", asked.id).answer is None


def test_an_app_cannot_claim_another_source(db, asked):
    call = _as(db, _app(db))
    out = _tool(
        call,
        "record_answer",
        decision_id=asked.id,
        letter="a",
        words="x",
        source="page",
    )
    assert out["isError"] is True
    with pytest.raises(decisions.InvalidSource):
        decisions.record_elsewhere(
            db,
            asked.id,
            letter="a",
            note="x",
            source="terminal",
            recorded_by="a",
            by_app=True,
        )
    assert decisions.get_owned(db, "office", asked.id).answer is None


@pytest.mark.parametrize("words", ["", "   "])
def test_empty_words_are_refused(db, asked, words):
    out = _tool(
        _as(db, _app(db)),
        "record_answer",
        decision_id=asked.id,
        letter="a",
        words=words,
    )
    assert out["isError"] is True
    assert "words is required" in _text(out)
    assert decisions.get_owned(db, "office", asked.id).answer is None


@pytest.mark.parametrize("permissions", [("read",), ("read", "write")])
def test_without_relay_there_is_no_tool(db, asked, permissions):
    call = _as(db, _app(db, permissions=permissions))
    listed = {t["name"] for t in _send(call, "tools/list", {})["tools"]}
    assert "record_answer" not in listed
    out = _tool(call, "record_answer", decision_id=asked.id, letter="a", words="x")
    assert out["isError"] is True
    assert decisions.get_owned(db, "office", asked.id).answer is None


def test_the_tool_is_offered_with_relay(db):
    listed = {t["name"] for t in _send(_as(db, _app(db)), "tools/list", {})["tools"]}
    assert "record_answer" in listed


def test_a_reviews_first_verdict_still_counts(db):
    keys.mint(db, agent="office")
    page = store.publish(db, "office", "Draft", PAGE.format("x"))
    review = decisions.ask_review(db, agent="office", artifact_id=page.id)
    letters = [o["letter"] for o in review.options]
    call = _as(db, _app(db))
    first = _tool(
        call, "record_answer", decision_id=review.id, letter=letters[0], words="fine"
    )
    assert first["isError"] is False, first
    second = _tool(
        call, "record_answer", decision_id=review.id, letter=letters[1], words="no"
    )
    assert second["isError"] is True
    assert decisions.get_owned(db, "office", review.id).answer == letters[0]


def test_the_gallery_says_he_said_it_in_the_apps_host(db, asked):
    app = _app(db)
    _tool(_as(db, app), "record_answer", decision_id=asked.id, letter="b", words="yes")
    row = decisions.get_owned(db, "office", asked.id)
    html = gallery._provenance(row, {app.agent: "claude.ai"})
    assert f"{settings.owner_name()} said (b) in claude.ai" in html
    assert f"{settings.owner_name()}, via claude.ai" in html
    assert "not tapped on this page" in html
    # A tap carries no such note.
    assert gallery._provenance(SimpleNamespace(answer_source="page")) == ""


def test_the_tool_needs_the_relay_permission():
    assert mcp.TOOLS["record_answer"].permission == mcp.RELAY
