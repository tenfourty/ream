"""MCP `organise_page` action `duplicate`: the duplicate route as a tool, for the calling
key's own pages only. Runs on both databases."""

import pytest
from test_mcp import (  # noqa: F401
    call_tool,
    client,
    headers,
    on,
    token,
    unfence_all,
)
from test_mcp_organise import _app, _as, _tool

from ream import comments, decisions, mcp, partchanges, store, thumbs

PAGE = (
    "<!doctype html><html><head><title>B</title></head>"
    "<body><p>The original words.</p></body></html>"
)
NO_PAGE = "abcdefghijklmnopqrstuvwxyz"
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]
GIVE = "Give the person page_url."


@pytest.fixture()
def original(db, token):
    return store.publish(db, "mcp-seat", "Board", PAGE)


def duplicate(client, token, page_id, **more):
    return call_tool(
        client, token, "organise_page", page_id=page_id, action="duplicate", **more
    )


def test_the_answer_has_the_routes_keys_and_the_copy_is_a_new_page(
    db, client, on, token, original
):
    error, got = duplicate(client, token, original.id)
    assert not error, got
    route = client.post(
        f"/api/v1/pages/{original.id}/duplicate", headers=headers(token)
    )
    assert route.status_code == 201
    assert set(got) - {"next"} == set(route.json())
    got = unfence_all(got)
    assert got["id"] != original.id and got["id"] != route.json()["id"]
    assert got["version"] == 1
    assert got["title"] == "Board (copy)"
    assert got["agents_can_read"] is True and got["agent_read"]
    copy = store.get_owned(db, "mcp-seat", got["id"])
    assert copy.agent == "mcp-seat" and copy.title == "Board (copy)"
    assert store.get_body(db, "mcp-seat", got["id"]).body == PAGE
    assert (
        store.get_body(db, "mcp-seat", got["id"]).body
        == store.get_body(db, "mcp-seat", original.id).body
    )


def test_ream_own_values_come_back_bare_and_the_title_fenced(
    client, on, token, original
):
    _, got = duplicate(client, token, original.id)
    for key in ("id", "url", "page_url", "revision", "agent_read", "next"):
        assert not got[key].startswith("<<<"), key
    assert got["title"].startswith("<<<")
    assert got["next"].startswith(GIVE)


def test_the_hint_is_only_for_duplicate(client, on, token, original):
    for action, more in (("pin", {}), ("rename", {"title": "New"})):
        error, got = call_tool(
            client,
            token,
            "organise_page",
            page_id=original.id,
            action=action,
            **more,
        )
        assert not error and "next" not in got, action


def test_no_decision_comment_or_answer_is_carried(db, client, on, token, original):
    asked = decisions.ask(
        db,
        agent="mcp-seat",
        artifact_id=original.id,
        question="Which?",
        options=OPTIONS,
        recommended="a",
    )
    decisions.answer(db, asked.id, letter="a", note="his words", by="jeremy")
    decisions.ask(
        db,
        agent="mcp-seat",
        artifact_id=original.id,
        question="Open?",
        options=OPTIONS,
        recommended="a",
    )
    comments.add(
        db,
        artifact_id=original.id,
        author_kind="human",
        author="jeremy",
        body="a note",
    )
    error, got = duplicate(client, token, original.id)
    assert not error, got
    copy = unfence_all(got)["id"]
    assert decisions.list_for_artifact(db, copy, include_answered=True) == []
    assert comments.list_for_artifact(db, copy) == []
    kept = decisions.list_for_artifact(db, original.id, include_answered=True)
    assert len(kept) == 2


def test_a_page_closed_to_agents_copies_closed(db, client, on, token):
    closed = store.publish(db, "mcp-seat", "Quiet", PAGE, agents_can_read=False)
    error, got = duplicate(client, token, closed.id)
    assert not error, got
    got = unfence_all(got)
    assert got["agents_can_read"] is False
    route = client.post(
        f"/api/v1/pages/{closed.id}/duplicate", headers=headers(token)
    ).json()
    assert got["agent_read"] == route["agent_read"]
    assert store.get_owned(db, "mcp-seat", got["id"]).agents_can_read is False


def test_not_yours_answers_exactly_as_no_such_page(db, client, on, token):
    theirs = store.publish(db, "other", "Theirs", PAGE)
    binned = store.publish(db, "mcp-seat", "Binned", PAGE)
    assert store.set_deleted(db, binned.id, True, agent="mcp-seat")
    missing = duplicate(client, token, NO_PAGE)
    assert missing == (True, "no such page")
    # Another agent's page, though open to agents, is as missing as one that never was.
    assert duplicate(client, token, theirs.id) == missing
    assert duplicate(client, token, binned.id) == missing
    assert len(store.list_for_agent(db, "other")) == 1
    assert len(store.list_for_agent(db, "mcp-seat")) == 0


def test_an_app_duplicates_its_own_agents_page_only(db):
    app = _app(db)
    other = _app(db, "https://claude.ai/oauth/claude-code-client-metadata")
    office = store.publish(db, "office", "Office", PAGE)
    siblings = store.publish(db, other.agent, "Sibling", PAGE)
    own = store.publish(db, app.agent, "Mine", PAGE)
    call = _as(db, app)
    out = _tool(call, "organise_page", page_id=own.id, action="duplicate")
    assert out["isError"] is False, out
    made = unfence_all(out["structuredContent"])["id"]
    assert store.get_owned(db, app.agent, made).title == "Mine (copy)"
    missing = _tool(call, "organise_page", page_id=NO_PAGE, action="duplicate")
    assert missing["isError"] is True
    for page in (office, siblings):
        refused = _tool(call, "organise_page", page_id=page.id, action="duplicate")
        assert refused == missing, page.id
    assert len(store.list_for_agent(db, "office")) == 1
    assert len(store.list_for_agent(db, other.agent)) == 1


def test_the_picture_and_the_parts_are_started_once(
    client, on, token, original, monkeypatch
):
    seen = {"thumb": [], "parts": []}
    monkeypatch.setattr(
        thumbs, "render_in_background", lambda *a, **k: seen["thumb"].append(a[1:])
    )
    monkeypatch.setattr(
        partchanges, "in_background", lambda *a, **k: seen["parts"].append(a[1:])
    )
    error, got = duplicate(client, token, original.id)
    assert not error, got
    copy = unfence_all(got)["id"]
    assert seen["thumb"] == [(copy, 1, PAGE)]
    assert seen["parts"] == [(copy, 1)]


@pytest.mark.parametrize("more", [{"title": "X"}, {"session": "s/1"}, {"folder": "w"}])
def test_duplicate_takes_no_other_argument(db, client, on, token, original, more):
    error, text = duplicate(client, token, original.id, **more)
    assert error and "unknown argument" in text, text
    assert len(store.list_for_agent(db, "mcp-seat")) == 1


def test_no_new_tool_and_the_hints_stay(client, on, token):
    registered = mcp.TOOLS["organise_page"]
    assert registered.annotations["destructiveHint"] is False
    assert registered.annotations["idempotentHint"] is False
    assert registered.annotations["readOnlyHint"] is False
    assert "duplicate" in registered.input_schema["properties"]["action"]["enum"]
    assert "duplicate_page" not in mcp.TOOLS
