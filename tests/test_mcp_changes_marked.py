"""MCP `read_page` marked changes view: `changes_to` and `changes_format` beside
`changes_since`, the same view the changes route and the gallery give a person. Runs on both
databases."""

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

from ream import pagediff, store

NO_PAGE = "abcdefghijklmnopqrstuvwxyz"
PAGING = ("offset", "size", "next_offset", "partial")


def page(*sections: str) -> str:
    return (
        "<!doctype html><html><head><title>t</title></head><body>"
        + "".join(sections)
        + "</body></html>"
    )


def sec(id_: str, text: str) -> str:
    return f'<section id="{id_}"><h2>{id_.title()}</h2><p>{text}</p></section>'


V1 = page(sec("running", "a"), sec("results", "r"))
V2 = page(sec("running", "b"), sec("results", "r"))
V3 = page(sec("running", "b"), sec("results", "r"), sec("risks", "new"))


def _three_versions(db, agent: str, **more):
    made = store.publish(db, agent, "Board", V1, **more)
    rev = made.revision
    for body in (V2, V3):
        rev = store.patch(db, agent, made.id, body, rev).revision
    return made


@pytest.fixture()
def mine(db, token):
    return _three_versions(db, "mcp-seat")


def route(client, token, page_id, **params):
    got = client.get(
        f"/api/v1/pages/{page_id}/changes", headers=headers(token), params=params
    )
    assert got.status_code == 200, got.text
    return got.json()


def read(client, token, page_id, **arguments):
    return call_tool(client, token, "read_page", page_id=page_id, **arguments)


def test_marked_is_what_the_route_answers(client, on, token, mine):
    error, got = read(client, token, mine.id, changes_since=1, changes_format="marked")
    assert not error, got
    got = unfence_all(got)
    paging = {k: got.pop(k) for k in PAGING}
    assert got == route(client, token, mine.id, **{"from": 1, "format": "marked"})
    assert "ar-diff-ins" in got["body"] and got["reason"] is None
    assert paging == {
        "offset": 0,
        "size": len(got["body"]),
        "next_offset": None,
        "partial": False,
    }


def test_the_default_format_is_the_parts_view_with_no_body(client, on, token, mine):
    error, got = read(client, token, mine.id, changes_since=1)
    assert not error, got
    assert unfence_all(got) == route(client, token, mine.id, **{"from": 1})
    assert "body" not in got
    error, same = read(client, token, mine.id, changes_since=1, changes_format="parts")
    assert not error and unfence_all(same) == unfence_all(got)


def test_changes_to_is_the_routes_to(client, on, token, mine):
    error, got = read(client, token, mine.id, changes_since=1, changes_to=2)
    assert not error, got
    assert unfence_all(got) == route(client, token, mine.id, **{"from": 1, "to": 2})
    assert got["to"] == 2
    error, got = read(
        client, token, mine.id, changes_since=1, changes_to=2, changes_format="marked"
    )
    assert not error, got
    got = unfence_all(got)
    for key in PAGING:
        got.pop(key)
    assert got == route(
        client, token, mine.id, **{"from": 1, "to": 2, "format": "marked"}
    )


def test_a_backwards_span_answers_the_routes_sentence(client, on, token, mine):
    error, text = read(client, token, mine.id, changes_since=3, changes_to=1)
    refused = client.get(
        f"/api/v1/pages/{mine.id}/changes",
        headers=headers(token),
        params={"from": 3, "to": 1},
    )
    assert refused.status_code == 422
    assert error and text == refused.json()["detail"] == "from must not be after to"
    # Nothing that was already an error changed its words.
    assert read(client, token, mine.id, changes_since=9) == (
        True,
        "no such page or version",
    )
    assert read(client, token, mine.id, changes_since=1, changes_to=9) == (
        True,
        "no such page or version",
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"changes_to": 2},
        {"changes_format": "marked"},
        {"changes_format": "parts"},
        {"history": True, "changes_format": "marked"},
        {"version": 1, "changes_to": 2},
        {"section": "running", "changes_format": "marked"},
    ],
)
def test_the_two_arguments_go_with_changes_since(client, on, token, mine, arguments):
    error, text = read(client, token, mine.id, **arguments)
    assert error and "changes_to and changes_format go with changes_since" in text


@pytest.mark.parametrize(
    "arguments",
    [
        {"changes_format": "diff"},
        {"changes_format": 1},
        {"changes_to": 0},
        {"changes_to": "2"},
        {"changes_to": True},
        {"history": True},
        {"version": 1},
        {"section": "running"},
    ],
)
def test_bad_values_and_other_modes_are_refused(client, on, token, mine, arguments):
    error, text = read(client, token, mine.id, changes_since=1, **arguments)
    assert error, text


def test_the_marked_body_pages_as_a_plain_body_does(client, on, token, mine):
    full = route(client, token, mine.id, **{"from": 1, "format": "marked"})["body"]
    error, first = read(
        client,
        token,
        mine.id,
        changes_since=1,
        changes_format="marked",
        limit=100,
    )
    assert not error, first
    first = unfence_all(first)
    assert first["body"] == full[:100]
    assert first["size"] == len(full) and first["partial"] is True
    assert first["next_offset"] == 100 and first["offset"] == 0
    error, last = read(
        client,
        token,
        mine.id,
        changes_since=1,
        changes_format="marked",
        offset=100,
    )
    assert not error, last
    last = unfence_all(last)
    assert last["body"] == full[100:] and last["next_offset"] is None
    assert last["partial"] is True
    error, text = read(
        client,
        token,
        mine.id,
        changes_since=1,
        changes_format="marked",
        offset=len(full) + 1,
    )
    assert error and "past the end" in text


def test_offset_and_limit_still_need_a_body_or_the_marked_view(client, on, token, mine):
    error, text = read(client, token, mine.id, changes_since=1, limit=5)
    assert error and "live page or one version" in text
    error, text = read(
        client, token, mine.id, changes_since=1, changes_format="parts", offset=5
    )
    assert error and "live page" in text


def test_the_marked_body_is_fenced_as_page_content(client, on, token, mine):
    _, got = read(client, token, mine.id, changes_since=1, changes_format="marked")
    assert got["body"].startswith("<<<") and "page content" in got["body"]


def test_not_yours_answers_exactly_as_no_such_page(db, client, on, token, mine):
    closed = _three_versions(db, "other", agents_can_read=False)
    binned = _three_versions(db, "other")
    assert store.set_deleted(db, binned.id, True, agent="other")
    for arguments in (
        {"changes_since": 1},
        {"changes_since": 1, "changes_format": "marked"},
        {"changes_since": 1, "changes_to": 2, "changes_format": "marked"},
    ):
        missing = read(client, token, NO_PAGE, **arguments)
        assert missing == (True, "no such page"), arguments
        assert read(client, token, closed.id, **arguments) == missing, arguments
        assert read(client, token, binned.id, **arguments) == missing, arguments


def test_another_agents_open_page_is_readable(db, client, on, token):
    theirs = _three_versions(db, "other")
    error, got = read(
        client, token, theirs.id, changes_since=1, changes_format="marked"
    )
    assert not error, got
    assert "ar-diff-ins" in unfence_all(got)["body"]


def test_a_signed_in_app_reads_a_closed_pages_marked_changes(db):
    app = _app(db)
    closed = _three_versions(db, "office", agents_can_read=False)
    out = _tool(
        _as(db, app),
        "read_page",
        page_id=closed.id,
        changes_since=1,
        changes_format="marked",
    )
    assert out["isError"] is False, out
    got = unfence_all(out["structuredContent"])
    assert "ar-diff-ins" in got["body"] and got["parts"]


def test_a_failed_mark_up_says_why_and_the_body_is_the_new_page(
    client, on, token, mine, monkeypatch
):
    pagediff._CACHE.clear()
    monkeypatch.setattr(
        pagediff, "_run_in_worker", lambda *a, **k: ((V3, "boom"), True)
    )
    error, got = read(client, token, mine.id, changes_since=1, changes_format="marked")
    assert not error, got
    got = unfence_all(got)
    assert got["reason"] == "boom" and got["body"] == V3
    assert got["parts"]
