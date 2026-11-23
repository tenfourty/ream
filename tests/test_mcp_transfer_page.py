"""MCP `transfer_page`: `POST /pages/{id}/transfer` as a tool, for keys only. A page that is
not the caller's own, a binned page and an unknown id answer alike (`no such page`); a signed-in
app never has the tool. Runs on both databases."""

import pytest
from test_mcp import (  # noqa: F401
    LEGACY_HEADERS,
    call_tool,
    client,
    headers,
    on,
    token,
)
from test_mcp_organise import _app, _as, _send

from ream import keys, mcp, rename, store

PAGE = "<!doctype html><html><head><title>B</title></head><body>one</body></html>"
MISSING = "abcdefghijklmnopqrstuvwxyz"
NEXT = "The page is no longer yours: you can read it only while it is open to agents."


@pytest.fixture()
def taker(db):
    made, _ = keys.mint(db, agent="taker")
    return made


@pytest.fixture()
def page(db, token):
    return store.publish(db, "mcp-seat", "Board", PAGE).id


def _route(client, token, page_id, to):
    return client.post(
        f"/api/v1/pages/{page_id}/transfer", json={"to": to}, headers=headers(token)
    )


def test_it_answers_what_the_route_answers(db, client, on, token, taker, page):
    other = store.publish(db, "mcp-seat", "Other", PAGE).id
    error, got = call_tool(client, token, "transfer_page", page_id=page, to="taker")
    assert not error
    assert got.pop("next") == NEXT
    route = _route(client, token, other, "taker").json()
    assert got == {"id": page, "from": "mcp-seat", "to": "taker"}
    assert route == {"id": other, "from": "mcp-seat", "to": "taker"}
    assert store.get_owned(db, "taker", page).agent == "taker"


def test_the_new_owner_can_update_it_and_the_old_owner_cannot(
    db, client, on, token, taker, page
):
    call_tool(client, token, "transfer_page", page_id=page, to="taker")
    error, _ = call_tool(
        client, taker, "organise_page", page_id=page, action="rename", title="Mine now"
    )
    assert not error
    revision = store.get_owned(db, "taker", page).revision
    error, got = call_tool(
        client,
        taker,
        "update_page",
        page_id=page,
        body=PAGE,
        expected_revision=revision,
    )
    assert not error, got
    error, text = call_tool(
        client,
        token,
        "update_page",
        page_id=page,
        body=PAGE,
        expected_revision=revision,
    )
    assert error and text == "no such page"
    error, text = call_tool(client, token, "transfer_page", page_id=page, to="taker")
    assert error and text == "no such page", "a second call finds it not yours"


def test_not_yours_binned_and_unknown_are_one_answer(db, client, on, token, taker):
    theirs = store.publish(db, "taker", "Theirs", PAGE).id
    binned = store.publish(db, "mcp-seat", "Binned", PAGE).id
    assert store.set_deleted(db, binned, True, agent="mcp-seat")
    answers = {
        call_tool(client, token, "transfer_page", page_id=target, to="taker")
        for target in (theirs, binned, MISSING)
    }
    assert answers == {(True, "no such page")}
    assert store.get_owned(db, "taker", theirs).agent == "taker"
    assert store.owner(db, binned) == "mcp-seat"


def test_an_unknown_destination_is_the_routes_422_sentence(db, client, on, token, page):
    error, text = call_tool(client, token, "transfer_page", page_id=page, to="nobody")
    assert error
    route = _route(client, token, page, "nobody")
    assert route.status_code == 422 and text == route.json()["detail"]
    assert "not a connected machine with a working key" in text
    assert store.get_owned(db, "mcp-seat", page).agent == "mcp-seat"


def test_a_renamed_agents_old_name_and_the_underscore_spelling_reach_the_same_agent(
    db, client, on, token
):
    keys.mint(db, agent="old-seat")
    rename.rename_agent(db, "old-seat", "new-seat", dry_run=False)
    keys.mint(db, agent="ops-seat")
    ids = [store.publish(db, "mcp-seat", f"P{n}", PAGE).id for n in range(3)]
    for page_id, to in zip(ids, ("new-seat", "old-seat", "ops_seat"), strict=True):
        error, got = call_tool(client, token, "transfer_page", page_id=page_id, to=to)
        assert not error, got
        assert got["to"] == {"old-seat": "new-seat"}.get(to, to.replace("_", "-"))
    assert [store.owner(db, i) for i in ids] == ["new-seat", "new-seat", "ops-seat"]


def test_two_agents_one_spelling_apart_are_never_guessed_between(
    db, client, on, token, page
):
    keys.mint(db, agent="seat-one")
    keys.mint(db, agent="Seat_One")
    error, text = call_tool(client, token, "transfer_page", page_id=page, to="seat_one")
    assert error and "page list" in text
    assert store.get_owned(db, "mcp-seat", page).agent == "mcp-seat"


def test_to_yourself_is_a_no_op(db, client, on, token, page):
    error, got = call_tool(client, token, "transfer_page", page_id=page, to="mcp-seat")
    assert not error
    assert got == {"id": page, "from": "mcp-seat", "to": "mcp-seat"}
    assert store.get_owned(db, "mcp-seat", page).agent == "mcp-seat"


def test_the_arguments_are_checked(db, client, on, token, taker, page):
    for arguments in (
        {"page_id": page},
        {"to": "taker"},
        {"page_id": page, "to": "taker", "confirm": True},
        {"page_id": page, "to": 7},
    ):
        error, text = call_tool(client, token, "transfer_page", **arguments)
        assert error, arguments
    assert text.startswith("to:")
    assert (
        call_tool(client, token, "transfer_page", page_id=page, to="taker", extra=1)[1]
        == "unknown argument: extra"
    )
    assert store.get_owned(db, "mcp-seat", page).agent == "mcp-seat"


def test_it_is_a_destructive_write_tool_listed_to_a_key(client, on, token):
    listed = client.post(
        "/mcp",
        headers=headers(token, **LEGACY_HEADERS),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    ).json()["result"]["tools"]
    hints = next(t for t in listed if t["name"] == "transfer_page")["annotations"]
    assert hints["readOnlyHint"] is False
    assert hints["destructiveHint"] is True
    assert hints["idempotentHint"] is True
    assert hints["openWorldHint"] is False
    assert mcp.TOOLS["transfer_page"].permission == mcp.WRITE


def test_a_signed_in_app_does_not_have_the_tool(db):
    app = _app(db)
    keys.mint(db, agent="taker")
    mine = store.publish(db, app.agent, "Mine", PAGE).id
    call = _as(db, app)
    listed = {t["name"] for t in _send(call, "tools/list", {})["tools"]}
    assert "organise_page" in listed and "transfer_page" not in listed

    def refused(name):
        with pytest.raises(mcp._Refusal) as raised:
            mcp._dispatch(
                call,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": name,
                        "arguments": {"page_id": mine, "to": "taker"},
                    },
                },
            )
        return raised.value.status, raised.value.code, raised.value.message

    got = refused("transfer_page")
    lacking = refused("no_such_tool")
    assert got[:2] == lacking[:2]
    assert got[2] == "Unknown tool: transfer_page"
    assert lacking[2] == "Unknown tool: no_such_tool"
    assert store.get_owned(db, app.agent, mine).agent == app.agent
