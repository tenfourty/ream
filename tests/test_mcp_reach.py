"""A signed-in app's reach and permissions (MCP sign-in design 11.2, 11.3), with apps made
directly in the database: no sign-in exists yet, so each call is `mcp.app_call`'s."""

from __future__ import annotations

import json

import pytest
from starlette.requests import Request

from ream import comments, keys, mcp, mcp_apps, rename, settings, store

CLAUDE_AI = "https://claude.ai/oauth/claude-ai/client.json"
CLAUDE_CODE = "https://claude.ai/oauth/claude-code-client-metadata"
PAGE = "<!doctype html><html><head><title>t</title></head><body>{}</body></html>"


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "query_string": b"",
            "headers": [(b"host", b"artifacts.tenfourty.site")],
        }
    )


def _app(db, client_id=CLAUDE_AI, permissions=("read", "write", "relay")):
    agent = mcp_apps.agent_for(db, settings.owner_id(), client_id, "claude.ai")
    return mcp_apps.create_app(
        db,
        person=settings.owner_id(),
        client_id=client_id,
        agent=agent.agent,
        permissions=list(permissions),
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
    )


def _send(call: mcp.Call, method: str, params: dict) -> dict:
    status, body = mcp._dispatch(
        call, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )
    assert status == 200, body
    return body["result"]


def _as(db, app) -> mcp.Call:
    return mcp.app_call(db, _request(), app, "test/reach", mcp.LATEST_LEGACY)


def _as_key(db, agent: str) -> mcp.Call:
    return mcp.Call(
        db=db,
        request=_request(),
        key=None,
        agent=agent,
        session="test/reach",
        version=mcp.LATEST_LEGACY,
        permissions=frozenset(mcp.PERMISSIONS),
        reach=mcp.reach.of_key(agent),
    )


def _tool(call: mcp.Call, name: str, **arguments) -> dict:
    return _send(call, "tools/call", {"name": name, "arguments": arguments})


@pytest.fixture()
def closed(db) -> str:
    page = store.publish(db, "office", "Closed", PAGE.format("hidden words"))
    store.set_agents_can_read(db, page.id, False, agent="office")
    return page.id


def test_an_app_reads_a_page_closed_to_agents_and_a_key_does_not(db, closed):
    app = _app(db)
    read = _tool(_as(db, app), "read_page", page_id=closed)
    assert read["isError"] is False, read
    assert "hidden words" in json.dumps(read)
    listed = _tool(_as(db, app), "list_pages")
    assert closed in json.dumps(listed)
    # A key's reach is today's: the closed page is "no such page".
    other = _tool(_as_key(db, "seat"), "read_page", page_id=closed)
    assert other["isError"] is True
    assert closed not in json.dumps(_tool(_as_key(db, "seat"), "list_pages"))


def test_an_app_cannot_change_an_offices_page(db, closed):
    app = _app(db)
    page = store.publish(db, "office", "Open", PAGE.format("open"))
    for page_id in (page.id, closed):
        out = _tool(
            _as(db, app), "update_page", page_id=page_id, body=PAGE.format("mine now")
        )
        assert out["isError"] is True, out
    assert "mine now" not in store.get_body(db, "office", page.id).body


def test_an_app_changes_a_page_its_persons_other_app_made(db):
    app = _app(db)
    other = _app(db, CLAUDE_CODE)
    assert other.agent != app.agent
    page = store.publish(db, other.agent, "By the other app", PAGE.format("first"))
    out = _tool(
        _as(db, app),
        "update_page",
        page_id=page.id,
        body=PAGE.format("second"),
        expected_revision=page.revision,
    )
    assert out["isError"] is False, out
    assert "second" in store.get_body(db, other.agent, page.id).body
    # Outside a call the store is a key's again: the page is still the other app's own.
    assert store.get_owned(db, other.agent, page.id).agent == other.agent
    with pytest.raises(store.NotFound):
        store.get_owned(db, app.agent, page.id)


def test_an_app_comments_on_an_offices_page_as_the_owner_via_its_host(db, closed):
    app = _app(db)
    out = _tool(_as(db, app), "comment", page_id=closed, body="a thought")
    assert out["isError"] is False, out
    assert "Jeremy, via claude.ai" in out["structuredContent"]["comment"]["author_name"]
    stored = comments.list_for_artifact(db, closed, include_held=True)
    assert [(c.author, c.body) for c in stored] == [(app.agent, "a thought")]


def test_a_read_only_app_lists_and_calls_no_write_tools(db):
    app = _app(db, permissions=("read",))
    listed = {t["name"] for t in _send(_as(db, app), "tools/list", {})["tools"]}
    assert "whoami" in listed and "read_page" in listed
    for name, found in mcp.TOOLS.items():
        if found.permission in (mcp.WRITE, mcp.RELAY):
            assert name not in listed
    out = _tool(_as(db, app), "publish_page", title="t", body=PAGE.format("x"))
    assert out["isError"] is True
    assert "ream:write" in out["content"][0]["text"]
    assert store.list_for_agent(db, app.agent) == []


def test_a_rename_never_touches_a_person_and_app_agent(db):
    app = _app(db)
    keys.mint(db, agent="seat")
    with pytest.raises(rename.Refused, match="person-and-app"):
        rename.rename_agent(db, "seat", app.agent, dry_run=False)
    with pytest.raises(rename.Refused, match="person-and-app"):
        rename.rename_agent(db, app.agent, "seat-two", dry_run=False)
