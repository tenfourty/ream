"""A second person's signed-in app reads as its person (agents design, slice 2,
docs/agent/specs/2026-11-26-agents-and-watching-design.md): the pages of that person's personal
agents and pages open to agents, never the owner's closed page, and "not yours" answers as "no
such page". The owner's app and every key are unchanged. `people.OPEN_SIGN_IN` is flipped as
`test_agent_belongs.py` does, and the app is built directly, as `test_mcp_reach.py` does."""

from __future__ import annotations

import json

import pytest
from starlette.requests import Request

from ream import access, mcp, mcp_apps, people, reach, settings, store

CLAUDE_AI = "https://claude.ai/oauth/claude-ai/client.json"
CLAUDE_CODE = "https://claude.ai/oauth/claude-code-client-metadata"
PAGE = "<!doctype html><html><head><title>t</title></head><body>{}</body></html>"


@pytest.fixture(autouse=True)
def _sign_in_open(monkeypatch):
    monkeypatch.setattr(people, "OPEN_SIGN_IN", True)


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


def _app(db, person, client_id=CLAUDE_AI):
    agent = mcp_apps.agent_for(db, person, client_id, "claude.ai")
    return mcp_apps.create_app(
        db,
        person=person,
        client_id=client_id,
        agent=agent.agent,
        permissions=["read", "write", "relay"],
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
    )


def _as(db, app) -> mcp.Call:
    return mcp.app_call(db, _request(), app, "test/person", mcp.LATEST_LEGACY)


def _tool(call, name, **arguments):
    status, body = mcp._dispatch(
        call,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert status == 200, body
    return body["result"]


def _closed(db, agent, title="Closed"):
    page = store.publish(db, agent, title, PAGE.format("hidden words"))
    store.set_agents_can_read(db, page.id, False, agent=agent)
    return page.id


@pytest.fixture()
def world(db):
    ann = _app(db, "ann")
    ann2 = _app(db, "ann", CLAUDE_CODE)
    owner = _app(db, settings.owner_id())
    return {
        "ann": ann,
        "ann2": ann2,
        "owner_app": owner,
        "ann_closed": _closed(db, ann2.agent, "Ann closed"),
        "ann_open": store.publish(db, ann2.agent, "Ann open", PAGE.format("a")).id,
        "owner_closed": _closed(db, owner.agent, "Owner app closed"),
        "office_closed": _closed(db, "office", "Office closed"),
        "office_open": store.publish(db, "office", "Open", PAGE.format("o")).id,
        "trash": store.publish(db, ann2.agent, "Trashed", PAGE.format("t")).id,
    }


def test_reach_of_carries_the_person(db, world):
    wide = mcp_apps.reach_of(db, world["ann"])
    assert wide.person == "ann" and not wide.reads_all() and wide.reads_person()
    assert set(wide.writes()) == {world["ann"].agent, world["ann2"].agent}
    owner = mcp_apps.reach_of(db, world["owner_app"])
    assert owner.reads_all() and owner.person == settings.owner_id()
    assert reach.of_key("office").person is None


def test_a_second_persons_app_reads_its_persons_pages_and_open_pages(db, world):
    ann = world["ann"]
    with reach.acting(mcp_apps.reach_of(db, ann)):
        for page in ("ann_closed", "ann_open", "office_open"):
            assert (
                store.get_owned(db, ann.agent, world[page], as_reader=True).id
                == world[page]
            )
        listed = {r.id for r in store.list_readable(db, ann.agent)}
    assert listed == {
        world["ann_closed"],
        world["ann_open"],
        world["office_open"],
        world["trash"],
    }


def test_it_never_reads_the_owners_closed_page_and_the_404_is_the_same(db, world):
    ann = world["ann"]
    errors = []
    with reach.acting(mcp_apps.reach_of(db, ann)):
        for page in (world["owner_closed"], world["office_closed"], "no-such-page"):
            with pytest.raises(store.NotFound) as caught:
                store.get_owned(db, ann.agent, page, as_reader=True)
            errors.append((type(caught.value), str(caught.value)))
        for page in ("owner_closed", "office_closed"):
            assert world[page] not in {r.id for r in store.list_readable(db, ann.agent)}
    assert len(set(errors)) == 1


def test_over_mcp_not_yours_is_byte_identical_to_no_such_page(db, world):
    call = _as(db, world["ann"])
    mine = _tool(call, "read_page", page_id=world["ann_closed"])
    assert mine["isError"] is False and "hidden words" in json.dumps(mine)
    outcomes = [
        json.dumps(_tool(call, "read_page", page_id=page), sort_keys=True)
        for page in (world["owner_closed"], world["office_closed"])
    ]
    missing = json.dumps(_tool(call, "read_page", page_id="nopagehere"), sort_keys=True)
    # The id the caller passed is the only thing that may differ.
    for page, out in zip(("owner_closed", "office_closed"), outcomes, strict=True):
        assert json.loads(out)["isError"] is True
        assert out.replace(world[page], "X") == missing.replace("nopagehere", "X")
    listed = json.dumps(_tool(call, "list_pages", scope="all"))
    assert world["owner_closed"] not in listed and world["office_closed"] not in listed


def test_it_never_reads_the_trash(db, world):
    ann = world["ann"]
    assert store.set_deleted(db, world["trash"], True, agent=world["ann2"].agent)
    with reach.acting(mcp_apps.reach_of(db, ann)):
        with pytest.raises(store.NotFound):
            store.get_owned(db, ann.agent, world["trash"], as_reader=True)
        assert world["trash"] not in {r.id for r in store.list_readable(db, ann.agent)}


def test_it_changes_its_persons_agents_pages_and_nobody_elses(db, world):
    ann = world["ann"]
    with reach.acting(mcp_apps.reach_of(db, ann)):
        assert store.rename(db, world["ann_open"], "Renamed", agent=ann.agent)
        assert not store.rename(db, world["office_open"], "Mine", agent=ann.agent)
        assert not store.rename(db, world["owner_closed"], "Mine", agent=ann.agent)
    assert store.get_owned(db, "office", world["office_open"]).title == "Open"


def test_the_owners_app_is_unchanged(db, world):
    owner = world["owner_app"]
    with reach.acting(mcp_apps.reach_of(db, owner)):
        for page in ("office_closed", "ann_closed", "owner_closed"):
            assert (
                store.get_owned(db, owner.agent, world[page], as_reader=True).id
                == world[page]
            )
        assert {world["ann_closed"], world["office_closed"]} <= {
            r.id for r in store.list_readable(db, owner.agent)
        }


def test_a_key_is_unchanged(db, world):
    assert {r.id for r in store.list_readable(db, "office")} == {
        world["office_closed"],
        world["office_open"],
        world["ann_open"],
        world["trash"],
    }
    with pytest.raises(store.NotFound):
        store.get_owned(db, "office", world["ann_closed"], as_reader=True)


def test_a_nudge_for_another_agent_inside_the_apps_request_sees_key_reach(db, world):
    ann = world["ann"]
    with reach.acting(mcp_apps.reach_of(db, ann)):
        assert reach.widened("office") is None
        with pytest.raises(store.NotFound):
            store.get_owned(db, "office", world["ann_closed"], as_reader=True)
        assert {r.id for r in store.list_readable(db, "office")} == {
            world["office_closed"],
            world["office_open"],
            world["ann_open"],
            world["trash"],
        }
        assert store.rename(db, world["ann_open"], "x", agent="office") is False


def test_person_reads_is_the_one_predicate(db, world):
    with db.connect() as conn:
        for page, expect in (
            ("ann_closed", True),
            ("ann_open", True),
            ("office_open", True),
            ("office_closed", False),
            ("owner_closed", False),
        ):
            assert access.person_reads(conn, "ann", world[page]) is expect, page
        assert access.person_reads(conn, "ann", "no-such-page") is False
        # The owner reads every live page.
        owner = settings.owner_id()
        assert access.person_reads(conn, owner, world["office_closed"]) is True
        # A person with no app agent reads only what is open to agents.
        assert access.person_reads(conn, "bob", world["office_open"]) is True
        assert access.person_reads(conn, "bob", world["ann_closed"]) is False
