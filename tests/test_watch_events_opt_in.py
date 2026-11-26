"""The watching kinds appear on no feed that did not ask, and wake no waiter that did not ask
(agents design, slice 3, "Guards"; decision A). `GET /events`, `events.list_for_agent`,
`events.wait_for_agent`, MCP `wait_for_answer` and every caller without the flag answer exactly
as they did before the kinds existed; `include=watched` (alone, or in a comma list with
`receipts`) and MCP `whats_new` add them. Both databases."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from test_mcp import PAGE as MCP_PAGE
from test_mcp import call_tool, client, on, token  # noqa: F401

from ream import comments, decisions, events, keys, store, watches
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>t</title></head><body>{}</body></html>"
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db), base_url="https://testserver")


@pytest.fixture()
def reader(db):
    token, _ = keys.mint(db, agent="reader")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def world(db, api, reader):
    """`reader` owns a page with an answered question (an ordinary event) and follows another
    agent's open page, which then changes and gets a comment (two watched events)."""
    mine = store.publish(db, "reader", "Mine", PAGE.format("m")).id
    ask = decisions.ask(
        db, agent="reader", artifact_id=mine, question="ship?", options=OPTIONS
    )
    decisions.answer(db, ask.id, letter="a", note="", by="jeremy")
    theirs = store.publish(db, "owner", "Theirs", PAGE.format("t")).id
    assert api.put(f"/api/v1/pages/{theirs}/watch", headers=reader).status_code == 200
    return {"mine": mine, "theirs": theirs}


def _change(db, world):
    row = store.get_owned(db, "owner", world["theirs"])
    store.patch(db, "owner", world["theirs"], PAGE.format("t2"), row.revision)
    comments.add(
        db,
        artifact_id=world["theirs"],
        author_kind="human",
        author="jeremy",
        body="noted",
    )


def _kinds(response):
    return [e["kind"] for e in response.json()]


def test_a_feed_without_the_flag_is_byte_identical_to_one_with_no_watching(
    db, api, reader, world
):
    before = api.get("/api/v1/events", headers=reader)
    _change(db, world)
    after = api.get("/api/v1/events", headers=reader)
    assert _kinds(before) == ["decision_answered"]
    assert after.content == before.content
    assert after.status_code == 200


def test_the_flag_adds_the_watching_kinds_in_seq_order(db, api, reader, world):
    _change(db, world)
    got = api.get("/api/v1/events?include=watched", headers=reader)
    assert _kinds(got) == [
        "decision_answered",
        "watched_page_changed",
        "watched_comment_added",
    ]
    seqs = [e["seq"] for e in got.json()]
    assert seqs == sorted(seqs)


def test_receipts_and_watched_are_independent_and_a_comma_list_asks_for_both(
    db, api, reader, world
):
    ask = decisions.list_for_artifact(db, world["mine"], include_answered=True)[0]
    api.post(
        f"/api/v1/decisions/{ask.id}/receipt",
        json={"state": "seen"},
        headers=reader,
    )
    _change(db, world)
    only_receipts = _kinds(api.get("/api/v1/events?include=receipts", headers=reader))
    assert (
        "receipt_set" in only_receipts and "watched_page_changed" not in only_receipts
    )
    only_watched = _kinds(api.get("/api/v1/events?include=watched", headers=reader))
    assert "watched_page_changed" in only_watched and "receipt_set" not in only_watched
    both = _kinds(api.get("/api/v1/events?include=receipts,watched", headers=reader))
    assert {"receipt_set", "watched_page_changed", "watched_comment_added"} <= set(both)
    swapped = _kinds(api.get("/api/v1/events?include=watched,receipts", headers=reader))
    assert swapped == both


@pytest.mark.parametrize(
    "bad", ["everything", "watched,", ",watched", "watch", "receipt"]
)
def test_any_other_include_is_still_a_422(api, reader, bad):
    assert api.get(f"/api/v1/events?include={bad}", headers=reader).status_code == 422


def test_the_cursor_header_skips_watched_rows_for_a_caller_that_did_not_ask(
    db, api, reader, world
):
    _change(db, world)
    plain = api.get("/api/v1/events", headers=reader)
    asked = api.get("/api/v1/events?include=watched", headers=reader)
    assert int(plain.headers["x-ream-seen"]) == int(asked.headers["x-ream-seen"])
    assert int(plain.headers["x-ream-seen"]) == asked.json()[-1]["seq"]


def test_list_for_agent_leaves_them_out_by_default(db, world):
    _change(db, world)
    assert [e.kind for e in events.list_for_agent(db, "reader")] == [
        "decision_answered"
    ]
    assert len(events.list_for_agent(db, "reader", include_watched=True)) == 3
    # `since` and `limit` still behave as a cursor over what was asked for.
    top = events.list_for_agent(db, "reader", include_watched=True)[0].seq
    assert [
        e.kind
        for e in events.list_for_agent(db, "reader", since=top, include_watched=True)
    ] == ["watched_page_changed", "watched_comment_added"]


def test_a_waiter_without_the_flag_does_not_wake_and_one_with_it_does(db, world):
    since = events.latest_seq(db, "reader")
    _change(db, world)
    started = time.monotonic()
    assert events.wait_for_agent(db, "reader", since=since, seconds=0.4) == []
    assert time.monotonic() - started >= 0.35  # it held, it did not return early
    woke = events.wait_for_agent(
        db, "reader", since=since, seconds=5, include_watched=True
    )
    assert [e.kind for e in woke] == ["watched_page_changed", "watched_comment_added"]


def test_a_held_waiter_is_not_woken_by_a_watched_event_that_lands_mid_hold(db, world):
    since = events.latest_seq(db, "reader")
    thread = threading.Timer(0.2, lambda: _change(db, world))
    thread.start()
    try:
        assert events.wait_for_agent(db, "reader", since=since, seconds=0.8) == []
    finally:
        thread.join()
    assert events.latest_seq(db, "reader") > since  # it did land


def test_a_held_waiter_that_asked_is_woken_by_it(db, world):
    since = events.latest_seq(db, "reader")
    thread = threading.Timer(0.2, lambda: _change(db, world))
    thread.start()
    try:
        woke = events.wait_for_agent(
            db, "reader", since=since, seconds=10, include_watched=True
        )
    finally:
        thread.join()
    assert woke and woke[0].kind == "watched_page_changed"


def test_get_events_wait_holds_without_the_flag_and_wakes_with_it(
    db, api, reader, world
):
    since = events.latest_seq(db, "reader")
    _change(db, world)
    quiet = api.get(f"/api/v1/events?since={since}&wait=0.4", headers=reader)
    assert quiet.json() == []
    woke = api.get(
        f"/api/v1/events?since={since}&wait=5&include=watched", headers=reader
    )
    assert _kinds(woke) == ["watched_page_changed", "watched_comment_added"]


def test_a_decision_answer_still_wakes_a_waiter_without_the_flag(db, world):
    since = events.latest_seq(db, "reader")
    _change(db, world)
    ask = decisions.ask(
        db,
        agent="reader",
        artifact_id=world["mine"],
        question="again?",
        options=OPTIONS,
    )
    decisions.answer(db, ask.id, letter="b", note="", by="jeremy")
    woke = events.wait_for_agent(db, "reader", since=since, seconds=5)
    assert [e.kind for e in woke] == ["decision_answered"]


# --- MCP -------------------------------------------------------------------------------


@pytest.fixture()
def mcp_world(db, client, on, token):
    """`mcp-seat` follows another agent's open page that then changes and is commented on."""
    theirs = store.publish(db, "other", "Theirs", MCP_PAGE.format("their body")).id
    store.set_agents_can_read(db, theirs, True, agent="other")
    error, out = call_tool(client, token, "watch_page", page_id=theirs)
    assert not error and out["created"] is True
    row = store.get_owned(db, "other", theirs)
    store.patch(db, "other", theirs, MCP_PAGE.format("changed"), row.revision)
    comments.add(
        db, artifact_id=theirs, author_kind="human", author="jeremy", body="a thought"
    )
    return theirs


def test_whats_new_includes_the_watching_kinds_labelled_by_kind(
    client, token, mcp_world
):
    error, out = call_tool(client, token, "whats_new", since=0)
    assert not error
    assert [e["kind"] for e in out["events"]] == [
        "watched_page_changed",
        "watched_comment_added",
    ]
    assert all(e["artifact_id"] == mcp_world for e in out["events"])


def test_wait_for_answer_does_not_include_or_wake_on_them(client, token, mcp_world):
    error, out = call_tool(client, token, "wait_for_answer", seconds=1)
    assert not error
    assert out["events"] == [] and out["timed_out"] is True


def test_watches_is_empty_for_a_key_that_follows_nothing(client, on, token):
    error, out = call_tool(client, token, "watches")
    assert not error and out["watches"] == []


def test_a_watch_without_a_reader_cannot_be_made_by_the_tool(client, token, db, on):
    error, out = call_tool(client, token, "watch_page", page_id="nope" * 6 + "ab")
    assert error and "no such page" in out
    hidden = store.publish(db, "other", "Hidden", MCP_PAGE.format("x")).id
    store.set_agents_can_read(db, hidden, False, agent="other")
    again = call_tool(client, token, "watch_page", page_id=hidden)
    assert again == (True, out)
    assert watches.listing(db, "mcp-seat") == []
