"""Watching a page (agents design, slice 3, docs/agent/specs/2026-11-26-agents-and-watching-design.md).

An agent follows a page it can read (`PUT /api/v1/pages/{id}/watch`, both words), and is told of
a new version (`watched_page_changed`, coalesced) and of other people's comments
(`watched_comment_added`) in its own feed. The rules held here: an unknown page and one it may not
read give byte-identical 404s; only readers receive the fan-out, and a page closed later goes
silent; the watcher's own act is not echoed; a comment it already gets as `comment_added` is not
sent twice; the window holds; the cap; a rename merges the rows; destroying a page cascades them.
Both databases (the `db` fixture)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from ream import (
    comments,
    decisions,
    events,
    keys,
    mcp,
    mcp_apps,
    people,
    rename,
    settings,
    store,
    watches,
)
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>t</title></head><body>{}</body></html>"
MISSING = "abcdefghijklmnopqrstuvwxyz"
CLAUDE_AI = "https://claude.ai/oauth/claude-ai/client.json"
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db), base_url="https://testserver")


@pytest.fixture()
def heads(db):
    out = {}
    for name in ("owner", "reader", "other"):
        token, _ = keys.mint(db, agent=name)
        out[name] = {"Authorization": f"Bearer {token}"}
    return out


@pytest.fixture()
def page(db):
    return store.publish(db, "owner", "Board", PAGE.format("one")).id


def _put(api, page_id, who, word="pages"):
    return api.put(f"/api/v1/{word}/{page_id}/watch", headers=who)


def _delete(api, page_id, who, word="pages"):
    return api.delete(f"/api/v1/{word}/{page_id}/watch", headers=who)


def _edit(db, agent, page_id, body):
    row = store.get_owned(db, agent, page_id)
    return store.patch(db, agent, page_id, PAGE.format(body), row.revision)


def _feed(db, agent, kinds=events.WATCHED_KINDS):
    return [
        e
        for e in events.list_for_agent(db, agent, include_watched=True)
        if e.kind in kinds
    ]


def _human_comment(db, page_id, body="a thought"):
    return comments.add(
        db, artifact_id=page_id, author_kind="human", author="jeremy", body=body
    )


def _agent_comment(db, page_id, who, body="a remark"):
    return comments.add(
        db,
        artifact_id=page_id,
        author_kind="agent",
        author=who,
        agent=who,
        body=body,
    )


# --- the routes ------------------------------------------------------------------------


@pytest.mark.parametrize("word", ["pages", "artifacts"])
def test_watch_list_and_unwatch_in_both_words(api, heads, page, word):
    first = _put(api, page, heads["reader"], word)
    assert first.status_code == 200
    assert first.json() == {"id": page, "watching": True, "created": True}
    again = _put(api, page, heads["reader"], word)
    assert again.status_code == 200 and again.json()["created"] is False
    listed = api.get("/api/v1/watches", headers=heads["reader"]).json()["watches"]
    assert [w["id"] for w in listed] == [page]
    assert listed[0]["title"] == "Board" and listed[0]["agent"] == "owner"
    assert listed[0]["version"] == 1
    gone = _delete(api, page, heads["reader"], word)
    assert gone.status_code == 200
    assert gone.json() == {"id": page, "watching": False, "removed": True}
    assert api.get("/api/v1/watches", headers=heads["reader"]).json() == {"watches": []}
    nothing = _delete(api, page, heads["reader"], word)
    assert nothing.status_code == 200 and nothing.json()["removed"] is False


def test_the_list_is_the_callers_own(api, heads, page):
    _put(api, page, heads["reader"])
    assert api.get("/api/v1/watches", headers=heads["other"]).json()["watches"] == []


def test_an_unknown_page_and_a_closed_page_answer_byte_identically(
    api, db, heads, page
):
    assert store.set_agents_can_read(db, page, False, agent="owner")
    closed = _put(api, page, heads["reader"])
    unknown = _put(api, MISSING, heads["reader"])
    assert closed.status_code == unknown.status_code == 404
    assert closed.content == unknown.content
    assert (
        _delete(api, page, heads["reader"]).content
        == _delete(api, MISSING, heads["reader"]).content
    )
    assert _delete(api, page, heads["reader"]).status_code == 404
    # A trashed page is as missing as an unknown one.
    other = store.publish(db, "owner", "Gone", PAGE.format("x")).id
    store.set_deleted(db, other, True, agent="owner")
    assert _put(api, other, heads["reader"]).content == unknown.content


def test_the_owner_may_watch_its_own_page_and_it_adds_nothing(api, db, heads, page):
    assert _put(api, page, heads["owner"]).status_code == 200
    store.set_agents_can_read(db, page, True, agent="owner")
    _agent_comment(db, page, "reader")
    assert _feed(db, "owner") == []
    assert [e.kind for e in events.list_for_agent(db, "owner")] == ["comment_added"]


def test_letting_go_of_a_watch_on_a_page_that_closed_still_works(api, db, heads, page):
    _put(api, page, heads["reader"])
    store.set_agents_can_read(db, page, False, agent="owner")
    assert api.get("/api/v1/watches", headers=heads["reader"]).json()["watches"] == []
    assert _delete(api, page, heads["reader"]).json()["removed"] is True


def test_the_routes_need_a_key(api, page):
    assert api.put(f"/api/v1/pages/{page}/watch").status_code == 401
    assert api.get("/api/v1/watches").status_code == 401


# --- the cap ---------------------------------------------------------------------------


def test_the_cap_is_two_hundred():
    assert watches.CAP == 200


def test_a_watch_past_the_cap_is_a_409_and_a_repeat_is_not(
    api, db, heads, page, monkeypatch
):
    monkeypatch.setattr(watches, "CAP", 2)
    second = store.publish(db, "owner", "Two", PAGE.format("2")).id
    third = store.publish(db, "owner", "Three", PAGE.format("3")).id
    assert _put(api, page, heads["reader"]).status_code == 200
    assert _put(api, second, heads["reader"]).status_code == 200
    over = _put(api, third, heads["reader"])
    assert over.status_code == 409
    assert "unwatch" in over.json()["detail"]
    assert _put(api, page, heads["reader"]).status_code == 200  # a repeat
    assert _delete(api, second, heads["reader"]).status_code == 200
    assert _put(api, third, heads["reader"]).status_code == 200


def test_a_watch_on_a_page_that_closed_does_not_count_against_the_cap(
    api, db, heads, page, monkeypatch
):
    monkeypatch.setattr(watches, "CAP", 1)
    second = store.publish(db, "owner", "Two", PAGE.format("2")).id
    _put(api, page, heads["reader"])
    store.set_agents_can_read(db, page, False, agent="owner")
    assert _put(api, second, heads["reader"]).status_code == 200


# --- a version was minted --------------------------------------------------------------


def test_a_watcher_hears_of_a_new_version_and_the_page_owner_does_not_get_the_kind(
    api, db, heads, page
):
    _put(api, page, heads["reader"])
    _edit(db, "owner", page, "two")
    got = _feed(db, "reader")
    assert [e.kind for e in got] == ["watched_page_changed"]
    event = got[0]
    assert event.artifact_id == page and event.actor == "owner"
    assert event.ref_id == "2"
    assert "Board" in event.summary and "version 2" in event.summary
    assert "folded in" in event.summary
    assert _feed(db, "owner") == []
    assert _feed(db, "other") == []


def test_the_summary_carries_the_versions_note(api, db, heads, page):
    _put(api, page, heads["reader"])
    row = store.get_owned(db, "owner", page)
    store.patch(
        db,
        "owner",
        page,
        PAGE.format("two"),
        row.revision,
        note="fixed the totals",
        section="totals",
    )
    assert "fixed the totals" in _feed(db, "reader")[0].summary


def test_the_watcher_writing_to_the_page_is_not_told_of_its_own_change(
    api, db, heads, page
):
    # The page is the watcher's own: the actor is skipped, whoever else watches.
    token, _ = keys.mint(db, agent="writer")
    mine = store.publish(db, "writer", "Mine", PAGE.format("a")).id
    store.set_agents_can_read(db, mine, True, agent="writer")
    _put(api, mine, heads["reader"])
    _put(api, mine, {"Authorization": f"Bearer {token}"})
    _edit(db, "writer", mine, "b")
    assert [e.agent for e in _feed(db, "reader")] == ["reader"]
    assert _feed(db, "writer") == []


def test_an_identical_republish_mints_no_version_and_sends_nothing(
    api, db, heads, page
):
    _put(api, page, heads["reader"])
    _edit(db, "owner", page, "one")
    assert _feed(db, "reader") == []


def test_only_readers_receive_the_fan_out(api, db, heads, page):
    _put(api, page, heads["reader"])
    # A watch row of an agent that cannot read the page (written as an older image or a
    # page closed later would leave it): silent, never an event.
    store.set_agents_can_read(db, page, False, agent="owner")
    with db.connect() as conn:
        conn.execute(
            "insert into page_watch (agent, artifact_id) values ('other', %s)", (page,)
        )
    _edit(db, "owner", page, "two")
    assert _feed(db, "reader") == [] and _feed(db, "other") == []


def test_a_page_closed_later_goes_silent_and_comes_back_when_reopened(
    api, db, heads, page
):
    _put(api, page, heads["reader"])
    store.set_agents_can_read(db, page, False, agent="owner")
    _edit(db, "owner", page, "two")
    _human_comment(db, page)
    assert _feed(db, "reader") == []
    assert api.get("/api/v1/watches", headers=heads["reader"]).json()["watches"] == []
    store.set_agents_can_read(db, page, True, agent="owner")
    assert (
        api.get("/api/v1/watches", headers=heads["reader"]).json()["watches"][0]["id"]
        == page
    )
    _human_comment(db, page, "again")
    assert [e.kind for e in _feed(db, "reader")] == ["watched_comment_added"]


def test_a_page_in_the_bin_goes_silent(api, db, heads, page):
    _put(api, page, heads["reader"])
    store.set_deleted(db, page, True, agent="owner")
    with db.connect() as conn:
        assert (
            watches.fan_out(
                conn,
                (page, "Board", "owner"),
                "watched_page_changed",
                actor="x",
                summary="s",
            )
            == []
        )


# --- the window ------------------------------------------------------------------------


def test_one_change_event_per_ten_minutes_and_the_window_reopens(api, db, heads, page):
    _put(api, page, heads["reader"])
    _edit(db, "owner", page, "two")
    _edit(db, "owner", page, "three")
    _edit(db, "owner", page, "four")
    first = _feed(db, "reader")
    assert [e.ref_id for e in first] == ["2"]
    with db.connect() as conn:
        stamp = conn.execute(
            "select changed_notified_at from page_watch where agent = 'reader'"
        ).fetchone()[0]
        assert stamp is not None
        conn.execute(
            "update page_watch set changed_notified_at = %s",
            (datetime.now(UTC) - watches.WINDOW - timedelta(seconds=5),),
        )
    _edit(db, "owner", page, "five")
    assert [e.ref_id for e in _feed(db, "reader")] == ["2", "5"]


def test_the_window_is_per_watcher_and_per_page(api, db, heads, page):
    second = store.publish(db, "owner", "Two", PAGE.format("2")).id
    _put(api, page, heads["reader"])
    _put(api, second, heads["reader"])
    _put(api, page, heads["other"])
    _edit(db, "owner", page, "x")
    _edit(db, "owner", second, "y")
    assert sorted((e.agent, e.artifact_id) for e in _feed(db, "reader")) == sorted(
        [("reader", page), ("reader", second)]
    )
    assert [e.artifact_id for e in _feed(db, "other")] == [page]


def test_comments_are_not_coalesced(api, db, heads, page):
    _put(api, page, heads["reader"])
    _human_comment(db, page, "one")
    _human_comment(db, page, "two")
    assert [e.kind for e in _feed(db, "reader")] == ["watched_comment_added"] * 2


# --- a comment -------------------------------------------------------------------------


def test_a_watcher_hears_of_a_human_comment(api, db, heads, page):
    _put(api, page, heads["reader"])
    mine = _human_comment(db, page, "the totals look off")
    got = _feed(db, "reader")
    assert [e.kind for e in got] == ["watched_comment_added"]
    assert got[0].ref_id == mine.id and got[0].actor == "jeremy"
    assert "the totals look off" in got[0].summary
    # The page's own agent hears it as it always did, and not as a watched one.
    assert [e.kind for e in events.list_for_agent(db, "owner")] == ["comment_added"]


def test_a_watcher_hears_of_another_agents_comment(api, db, heads, page):
    _put(api, page, heads["reader"])
    _agent_comment(db, page, "other")
    got = _feed(db, "reader")
    assert [(e.kind, e.actor) for e in got] == [("watched_comment_added", "other")]


def test_the_watchers_own_comment_is_not_echoed(api, db, heads, page):
    _put(api, page, heads["reader"])
    _put(api, page, heads["other"])
    _agent_comment(db, page, "reader")
    assert _feed(db, "reader") == []
    assert [e.actor for e in _feed(db, "other")] == ["reader"]


def test_no_double_for_a_comment_the_watcher_already_gets_as_comment_added(
    api, db, heads, page
):
    # reader replied under his answer's note, so his next note reaches it as comment_added.
    ask = decisions.ask(
        db, agent="owner", artifact_id=page, question="ship?", options=OPTIONS
    )
    decisions.answer(db, ask.id, letter="a", note="go", by="jeremy")
    root = api.post(
        f"/api/v1/decisions/{ask.id}/replies",
        json={"body": "what cost?"},
        headers=heads["reader"],
    ).json()
    assert _put(api, page, heads["reader"]).status_code == 200
    _put(api, page, heads["other"])
    comments.add(
        db,
        artifact_id=page,
        author_kind="human",
        author="jeremy",
        body="the hosting bill",
        thread_root=root["id"],
    )
    reader = events.list_for_agent(db, "reader", include_watched=True)
    assert [e.kind for e in reader] == ["comment_added"]
    assert [e.kind for e in _feed(db, "other")] == ["watched_comment_added"]


def test_a_held_comment_tells_no_watcher(api, db, heads, page, monkeypatch):
    from ream import holds

    _put(api, page, heads["reader"])
    monkeypatch.setattr(holds, "page_mode", lambda *a, **k: "held")
    _human_comment(db, page)
    assert _feed(db, "reader") == []


# --- apps ------------------------------------------------------------------------------


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


def _call(db, person):
    agent = mcp_apps.agent_for(db, person, CLAUDE_AI, "claude.ai")
    app = mcp_apps.create_app(
        db,
        person=person,
        client_id=CLAUDE_AI,
        agent=agent.agent,
        permissions=["read", "write", "relay"],
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
    )
    return mcp.app_call(db, _request(), app, "test/app", mcp.LATEST_LEGACY), agent.agent


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


def test_an_owners_app_watches_a_closed_page_and_a_second_persons_app_cannot(
    db, monkeypatch
):
    monkeypatch.setattr(people, "OPEN_SIGN_IN", True)
    closed = store.publish(db, "owner", "Closed", PAGE.format("hidden")).id
    store.set_agents_can_read(db, closed, False, agent="owner")
    mine, mine_agent = _call(db, settings.owner_id())
    ann, ann_agent = _call(db, "ann")
    ok = _tool(mine, "watch_page", page_id=closed)
    assert not ok.get("isError") and ok["structuredContent"]["created"] is True
    refused = _tool(ann, "watch_page", page_id=closed)
    missing = _tool(ann, "watch_page", page_id=MISSING)
    assert refused["isError"] and refused == missing
    _edit(db, "owner", closed, "two")
    assert [e.kind for e in _feed(db, mine_agent)] == ["watched_page_changed"]
    assert _feed(db, ann_agent) == []
    listed = _tool(mine, "watches")["structuredContent"]["watches"]
    assert [w["id"] for w in listed] == [closed]


def test_a_second_persons_app_follows_an_open_page_and_goes_silent_when_it_closes(
    db, monkeypatch
):
    monkeypatch.setattr(people, "OPEN_SIGN_IN", True)
    open_page = store.publish(db, "owner", "Open", PAGE.format("a")).id
    store.set_agents_can_read(db, open_page, True, agent="owner")
    ann, ann_agent = _call(db, "ann")
    assert _tool(ann, "watch_page", page_id=open_page)["structuredContent"]["created"]
    _edit(db, "owner", open_page, "b")
    assert len(_feed(db, ann_agent)) == 1
    store.set_agents_can_read(db, open_page, False, agent="owner")
    with db.connect() as conn:
        conn.execute("update page_watch set changed_notified_at = null")
    _edit(db, "owner", open_page, "c")
    assert len(_feed(db, ann_agent)) == 1
    assert _tool(ann, "watches")["structuredContent"]["watches"] == []
    gone = _tool(ann, "unwatch_page", page_id=open_page)["structuredContent"]
    assert gone["removed"] is True


# --- rename and destroy ------------------------------------------------------------------


def test_a_rename_moves_the_watches_and_a_merge_keeps_one_row_per_page(
    api, db, heads, page
):
    second = store.publish(db, "owner", "Two", PAGE.format("2")).id
    _put(api, page, heads["reader"])
    _put(api, second, heads["reader"])
    _put(api, page, heads["other"])
    _edit(db, "owner", page, "x")
    counts = rename.rename_agent(db, "reader", "other", dry_run=False, force=True)
    assert counts["page_watch.agent"] == 2
    with db.connect() as conn:
        rows = conn.execute(
            "select agent, artifact_id, changed_notified_at from page_watch"
            " order by artifact_id"
        ).fetchall()
    assert sorted((r[0], r[1]) for r in rows) == sorted(
        [("other", page), ("other", second)]
    )
    stamped = {r[1]: r[2] for r in rows}
    assert stamped[page] is not None  # the later stamp survives the merge
    # Events already written follow through `event.agent`.
    assert [e.kind for e in _feed(db, "other")] == ["watched_page_changed"] * 2


def test_a_rename_into_an_unused_name_moves_every_watch(api, db, heads, page):
    _put(api, page, heads["reader"])
    rename.rename_agent(db, "reader", "reader-new", dry_run=False, force=True)
    with db.connect() as conn:
        assert conn.execute("select agent from page_watch").fetchall() == [
            ("reader-new",)
        ]


def test_destroying_a_page_cascades_its_watches_away(api, db, heads, page):
    _put(api, page, heads["reader"])
    assert store.destroy(db, page, agent="owner")
    with db.connect() as conn:
        assert conn.execute("select count(*) from page_watch").fetchone()[0] == 0


# --- fan_out itself ----------------------------------------------------------------------


def test_fan_out_refuses_a_kind_that_is_not_a_watching_kind(db, page):
    with db.connect() as conn, pytest.raises(ValueError):
        watches.fan_out(
            conn, (page, "t", "owner"), "comment_added", actor="x", summary="s"
        )


def test_the_new_kinds_are_registered_by_their_stored_names():
    assert events.WATCHED_KINDS == ("watched_page_changed", "watched_comment_added")
    assert set(events.WATCHED_KINDS) <= set(events.KINDS)
