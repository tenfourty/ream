"""A signed-in app organises pages and reads questions with the same widened reach it has
for reading and changing pages (MCP sign-in design 11.2, 11.3): archive, pin, rename, file,
delete and restore the pages of any of the person's app agents, and see every question on a
page it can read. A key's reach widens nothing."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from ream import decisions, keys, mcp, mcp_apps, settings, store

CLAUDE_AI = "https://claude.ai/oauth/claude-ai/client.json"
CLAUDE_CODE = "https://claude.ai/oauth/claude-code-client-metadata"
PAGE = "<!doctype html><html><head><title>t</title></head><body>{}</body></html>"
NO_PAGE = "nopage000000"


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
    return mcp.app_call(db, _request(), app, "test/organise", mcp.LATEST_LEGACY)


def _as_key(db, agent: str) -> mcp.Call:
    return mcp.Call(
        db=db,
        request=_request(),
        key=None,
        agent=agent,
        session="test/organise",
        version=mcp.LATEST_LEGACY,
        permissions=frozenset(mcp.PERMISSIONS),
        reach=mcp.reach.of_key(agent),
    )


def _tool(call: mcp.Call, name: str, **arguments) -> dict:
    return _send(call, "tools/call", {"name": name, "arguments": arguments})


def _organise(call, page_id, action, **more):
    return _tool(call, "organise_page", page_id=page_id, action=action, **more)


def _row(db, page_id) -> dict:
    with db.connect() as conn:
        row = conn.execute(
            "select archived_at, pinned_at, deleted_at, folder_id, title"
            " from artifact where id = %s",
            (page_id,),
        ).fetchone()
    return dict(
        zip(("archived", "pinned", "deleted", "folder", "title"), row, strict=True)
    )


def _ok(out: dict) -> None:
    assert out["isError"] is False, out


@pytest.mark.parametrize("whose", ["own", "other"])
def test_an_app_organises_its_own_and_its_persons_other_apps_pages(db, whose):
    app = _app(db)
    other = _app(db, CLAUDE_CODE)
    agent = app.agent if whose == "own" else other.agent
    page = store.publish(db, agent, "Tidy me", PAGE.format("x"))
    call = _as(db, app)

    _ok(_organise(call, page.id, "rename", title="Tidied"))
    assert _row(db, page.id)["title"] == "Tidied"
    _ok(_organise(call, page.id, "pin"))
    assert _row(db, page.id)["pinned"] is not None
    _ok(_organise(call, page.id, "unpin"))
    assert _row(db, page.id)["pinned"] is None
    _ok(_organise(call, page.id, "archive"))
    assert _row(db, page.id)["archived"] is not None
    _ok(_organise(call, page.id, "unarchive"))
    assert _row(db, page.id)["archived"] is None
    _ok(_organise(call, page.id, "file", folder="work"))
    assert _row(db, page.id)["folder"] is not None
    _ok(_organise(call, page.id, "unfile"))
    assert _row(db, page.id)["folder"] is None
    _ok(_tool(call, "delete_page", page_id=page.id))
    assert _row(db, page.id)["deleted"] is not None
    _ok(_organise(call, page.id, "restore"))
    assert _row(db, page.id)["deleted"] is None
    # The page is still its own agent's.
    assert store.get_owned(db, agent, page.id).agent == agent


def test_an_app_cannot_organise_an_offices_page_and_it_reads_as_a_missing_one(db):
    app = _app(db)
    page = store.publish(db, "office", "Office", PAGE.format("x"))
    gone = store.publish(db, "office", "Binned", PAGE.format("x"))
    assert store.set_deleted(db, gone.id, True, agent="office")
    before = _row(db, page.id)
    call = _as(db, app)
    for action, more in (
        ("rename", {"title": "Mine"}),
        ("pin", {}),
        ("archive", {}),
        ("file", {"folder": "work"}),
        ("restore", {}),
    ):
        refused = _organise(call, page.id, action, **more)
        missing = _organise(call, NO_PAGE, action, **more)
        if action == "file":
            # The move route answers 200 with `missing`, for both alike.
            assert refused["isError"] is False and missing["isError"] is False
            got = refused["structuredContent"]
            assert got["moved"] == [] and got["missing"] == [page.id]
            assert missing["structuredContent"]["moved"] == []
            continue
        assert refused["isError"] is True and missing["isError"] is True, action
        assert refused["content"][0]["text"].replace(page.id, "ID") == (
            missing["content"][0]["text"].replace(NO_PAGE, "ID")
        ), action
    refused = _tool(call, "delete_page", page_id=page.id)
    missing = _tool(call, "delete_page", page_id=NO_PAGE)
    assert refused["isError"] is True and missing["isError"] is True
    assert refused["content"][0]["text"].replace(page.id, "ID") == (
        missing["content"][0]["text"].replace(NO_PAGE, "ID")
    )
    # An office's binned page is not the app's to restore either.
    assert _organise(call, gone.id, "restore")["isError"] is True
    assert _row(db, page.id) == before
    assert _row(db, gone.id)["deleted"] is not None


def test_a_keys_organising_is_unchanged(db):
    app = _app(db)
    mine = store.publish(db, "office", "Mine", PAGE.format("x"))
    theirs = store.publish(db, app.agent, "App's", PAGE.format("x"))
    call = _as_key(db, "office")
    _ok(_organise(call, mine.id, "pin"))
    assert _row(db, mine.id)["pinned"] is not None
    _ok(_organise(call, mine.id, "file", folder="work"))
    _ok(_tool(call, "delete_page", page_id=mine.id))
    _ok(_organise(call, mine.id, "restore"))
    # A key does not organise an app agent's page.
    for action in ("pin", "archive", "restore"):
        assert _organise(call, theirs.id, action)["isError"] is True
    assert _tool(call, "delete_page", page_id=theirs.id)["isError"] is True
    assert _row(db, theirs.id)["pinned"] is None
    assert store.move_many(db, "office", [theirs.id], "work")[0] == []


def test_an_apps_reach_leaves_giving_away_and_closing_a_page_alone(db):
    app = _app(db)
    other = _app(db, CLAUDE_CODE)
    page = store.publish(db, other.agent, "Theirs", PAGE.format("x"))
    keys.mint(db, agent="office")
    with mcp.reach.acting(_as(db, app).reach):
        assert store.transfer(db, page.id, "office", agent=app.agent) is None
        assert store.set_agents_can_read(db, page.id, False, agent=app.agent) is False
    assert store.get_owned(db, other.agent, page.id).agent == other.agent


def _ask(db, agent, page_id, question):
    return decisions.ask(
        db,
        agent=agent,
        artifact_id=page_id,
        question=question,
        options=[{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}],
    )


def test_an_apps_questions_list_what_waits_on_the_person_and_what_it_asked(db):
    app = _app(db)
    office = store.publish(db, "office", "Office", PAGE.format("x"))
    closed = store.publish(db, "office", "Closed", PAGE.format("x"))
    store.set_agents_can_read(db, closed.id, False, agent="office")
    binned = store.publish(db, "office", "Binned", PAGE.format("x"))
    own = store.publish(db, app.agent, "Own", PAGE.format("x"))
    on_office = _ask(db, "office", office.id, "office question")
    on_closed = _ask(db, "office", closed.id, "closed question")
    on_binned = _ask(db, "office", binned.id, "binned question")
    asked = _ask(db, app.agent, own.id, "app question")
    assert store.set_deleted(db, binned.id, True, agent="office")

    out = _tool(_as(db, app), "questions")
    _ok(out)
    ids = {q["id"] for q in out["structuredContent"]["questions"]}
    # A page the app cannot read (in the trash) shows no question.
    assert ids == {on_office.id, on_closed.id, asked.id}
    assert on_binned.id not in ids


def test_a_keys_questions_are_still_its_own(db):
    app = _app(db)
    office = store.publish(db, "office", "Office", PAGE.format("x"))
    own = store.publish(db, app.agent, "Own", PAGE.format("x"))
    mine = _ask(db, "office", office.id, "office question")
    _ask(db, app.agent, own.id, "app question")
    out = _tool(_as_key(db, "office"), "questions")
    ids = {q["id"] for q in out["structuredContent"]["questions"]}
    assert ids == {mine.id}


def test_an_apps_questions_never_list_the_trash_of_another_agent(db):
    app = _app(db)
    binned = store.publish(db, "office", "Binned", PAGE.format("x"))
    own = store.publish(db, app.agent, "Own", PAGE.format("x"))
    on_binned = _ask(db, "office", binned.id, "binned question")
    on_own = _ask(db, app.agent, own.id, "own question")
    assert store.set_deleted(db, binned.id, True, agent="office")
    assert store.set_deleted(db, own.id, True, agent=app.agent)
    out = _tool(_as(db, app), "questions", include_set_aside=True)
    _ok(out)
    ids = {q["id"] for q in out["structuredContent"]["questions"]}
    # Its own binned page's open question is set aside as ever; the office's is not its to see.
    assert ids == {on_own.id}
    # A key sees what it saw before: its own, binned page included.
    key = _tool(_as_key(db, "office"), "questions", include_set_aside=True)
    assert {q["id"] for q in key["structuredContent"]["questions"]} == {on_binned.id}


def test_a_read_only_app_has_no_organise_tool(db):
    app = _app(db, permissions=("read",))
    listed = {t["name"] for t in _send(_as(db, app), "tools/list", {})["tools"]}
    assert "questions" in listed
    assert "organise_page" not in listed and "delete_page" not in listed
    page = store.publish(db, app.agent, "P", PAGE.format("x"))
    out = _organise(_as(db, app), page.id, "pin")
    assert out["isError"] is True and "ream:write" in out["content"][0]["text"]
    assert _row(db, page.id)["pinned"] is None
