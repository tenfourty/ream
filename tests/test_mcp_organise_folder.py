"""MCP `organise_folder`: rename, archive and unarchive a folder, the two folder routes as one
tool. A folder is its agent's alone, so another agent's folder and an unknown slug answer
alike (`no such folder`); a signed-in app acts on its own agent's folders only. Runs on both
databases."""

import json

import pytest
from test_mcp import (  # noqa: F401
    FENCED,
    SECRET,
    call_tool,
    client,
    headers,
    on,
    token,
    unfence,
)
from test_mcp_organise import _app, _as
from test_mcp_organise import _tool as app_tool

from ream import decisions, mcp, store

PAGE = "<!doctype html><html><head><title>B</title></head><body>one</body></html>"
NOT_YOURS = "no such folder"


def _seed(db, agent="mcp-seat", slug="plan", pages=2) -> list[str]:
    return [
        store.publish(db, agent, f"P{n}", PAGE, folder=slug).id for n in range(pages)
    ]


def _ask(db, agent, page_id, question="Which?"):
    return decisions.ask(
        db,
        agent=agent,
        artifact_id=page_id,
        question=question,
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
    )


def _archived(db, page_id):
    with db.connect() as conn:
        return conn.execute(
            "select archived_at from artifact where id = %s", (page_id,)
        ).fetchone()[0]


def _route(client, token, folder, verb, **body):
    return client.post(
        f"/api/v1/folders/{folder}/{verb}", json=body, headers=headers(token)
    )


def _route_error(got) -> str:
    detail = got.json()["detail"]
    return detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)


def _slugs(client, token) -> list[str]:
    return [
        f["slug"] for f in client.get("/api/v1/folders", headers=headers(token)).json()
    ]


def test_a_slug_rename_answers_the_route_and_the_old_slug_still_lists(
    db, client, on, token
):
    ids = _seed(db)
    error, got = call_tool(
        client,
        token,
        "organise_folder",
        folder="plan",
        action="rename",
        new_slug="roadmap",
        title="The roadmap",
    )
    assert not error
    # Ream's own values come back bare; the title, an agent's text, fenced.
    assert got["slug"] == "roadmap" and got["previous_slug"] == "plan"
    assert FENCED.fullmatch(got["title"]) and unfence(got["title"]) == "The roadmap"
    for slug in ("roadmap", "plan"):
        error, listed = call_tool(client, token, "list_pages", folder=slug)
        assert not error and {p["id"] for p in listed["pages"]} == set(ids)
    again = _route(client, token, "roadmap", "rename", title="The roadmap")
    assert again.json()["previous_slug"] is None


def test_a_title_only_rename_keeps_the_slug(db, client, on, token):
    _seed(db)
    error, got = call_tool(
        client, token, "organise_folder", folder="plan", action="rename", title="Plan"
    )
    assert not error
    assert got["slug"] == "plan" and got["previous_slug"] is None
    assert unfence(got["title"]) == "Plan"


def test_a_rename_that_says_nothing_is_the_routes_422(db, client, on, token):
    _seed(db)
    error, text = call_tool(
        client, token, "organise_folder", folder="plan", action="rename"
    )
    assert error
    assert text == _route_error(_route(client, token, "plan", "rename"))
    assert "send a new slug, a new title, or both" in text


def test_a_taken_slug_and_a_bad_slug_are_the_routes_refusals(db, client, on, token):
    _seed(db, slug="plan")
    _seed(db, slug="other", pages=1)
    for new in ("other", "Bad Slug"):
        error, text = call_tool(
            client,
            token,
            "organise_folder",
            folder="plan",
            action="rename",
            new_slug=new,
        )
        assert error
        assert text == _route_error(_route(client, token, "plan", "rename", slug=new))
    assert _slugs(client, token) == ["other", "plan"]


def test_a_secret_in_the_title_is_refused_and_never_echoed(db, client, on, token):
    _seed(db)
    error, text = call_tool(
        client,
        token,
        "organise_folder",
        folder="plan",
        action="rename",
        title=f"key {SECRET}",
    )
    assert error and SECRET not in text
    assert text == _route_error(
        _route(client, token, "plan", "rename", title=f"key {SECRET}")
    )
    assert _slugs(client, token) == ["plan"]


@pytest.mark.parametrize(
    ("action", "more"),
    [
        ("rename", {"title": "Mine"}),
        ("archive", {"confirm": True}),
        ("archive", {}),
        ("unarchive", {}),
    ],
)
def test_another_agents_folder_reads_as_a_missing_one(
    db, client, on, token, action, more
):
    _seed(db, agent="other", slug="theirs", pages=1)
    before = _slugs(client, token)
    theirs = call_tool(
        client, token, "organise_folder", folder="theirs", action=action, **more
    )
    ghost = call_tool(
        client, token, "organise_folder", folder="ghost", action=action, **more
    )
    assert theirs == ghost == (True, NOT_YOURS)
    assert _slugs(client, token) == before == []
    (page,) = store.list_in_folder(db, "other", "theirs")
    assert page.archived_at is None


def test_archive_without_confirm_changes_nothing_and_equals_the_routes_preview(
    db, client, on, token
):
    one, two = _seed(db)
    _ask(db, "mcp-seat", one)
    _ask(db, "mcp-seat", one, "Also?")
    _ask(db, "mcp-seat", two)
    error, got = call_tool(
        client, token, "organise_folder", folder="plan", action="archive"
    )
    assert not error
    assert got.pop("next") == (
        "Nothing changed yet: call again with confirm true to archive the folder."
    )
    assert got == _route(client, token, "plan", "archive", archived=True).json()
    assert got["would_archive"] == {"pages": 2, "questions_set_aside": 3}
    assert _archived(db, one) is None and _archived(db, two) is None
    error, got = call_tool(
        client,
        token,
        "organise_folder",
        folder="plan",
        action="archive",
        confirm=False,
    )
    assert not error and "would_archive" in got and "next" in got


def test_archive_with_confirm_archives_the_pages_and_sets_the_questions_aside(
    db, client, on, token
):
    one, two = _seed(db)
    loose = store.publish(db, "mcp-seat", "Loose", PAGE).id
    _ask(db, "mcp-seat", one)
    _ask(db, "mcp-seat", loose)
    error, got = call_tool(
        client,
        token,
        "organise_folder",
        folder="plan",
        action="archive",
        confirm=True,
    )
    assert not error
    assert got == {
        "slug": "plan",
        "archived": True,
        "pages": 2,
        "questions_set_aside": 1,
    }
    assert _archived(db, one) is not None and _archived(db, two) is not None
    assert _archived(db, loose) is None


def test_unarchive_restores_only_the_pages_the_folder_archived(db, client, on, token):
    one, two = _seed(db)
    assert store.set_archived(db, two, True, agent="mcp-seat")  # before the folder
    call_tool(
        client,
        token,
        "organise_folder",
        folder="plan",
        action="archive",
        confirm=True,
    )
    error, got = call_tool(
        client, token, "organise_folder", folder="plan", action="unarchive"
    )
    assert not error
    assert got == {
        "slug": "plan",
        "archived": False,
        "pages_restored": 1,
        "questions_reopened": 0,
    }
    assert _archived(db, one) is None and _archived(db, two) is not None


@pytest.mark.parametrize(
    ("action", "more", "unknown"),
    [
        ("rename", {"title": "T", "confirm": True}, "confirm"),
        ("archive", {"new_slug": "x"}, "new_slug"),
        ("archive", {"title": "x"}, "title"),
        ("unarchive", {"confirm": True}, "confirm"),
        ("unarchive", {"new_slug": "x"}, "new_slug"),
    ],
)
def test_an_argument_the_action_does_not_take_is_refused(
    db, client, on, token, action, more, unknown
):
    _seed(db)
    error, text = call_tool(
        client, token, "organise_folder", folder="plan", action=action, **more
    )
    assert error and text == f"unknown argument: {unknown}"
    assert _slugs(client, token) == ["plan"]


def test_the_arguments_are_checked(db, client, on, token):
    _seed(db)
    for arguments in (
        {"action": "rename", "title": "T"},  # no folder
        {"folder": "plan"},  # no action
        {"folder": "plan", "action": "delete"},
        {"folder": "plan", "action": "archive", "confirm": "yes"},
    ):
        error, _ = call_tool(client, token, "organise_folder", **arguments)
        assert error, arguments
    assert _archived(db, store.list_in_folder(db, "mcp-seat", "plan")[0].id) is None


def test_it_is_a_write_tool_with_explicit_hints():
    registered = mcp.TOOLS["organise_folder"]
    assert registered.permission == mcp.WRITE
    assert registered.annotations["readOnlyHint"] is False
    assert registered.annotations["destructiveHint"] is False
    assert registered.annotations["idempotentHint"] is False


def test_an_app_organises_its_own_agents_folders_and_no_others(db):
    app = _app(db)
    ids = _seed(db, agent=app.agent, slug="mine")
    _seed(db, agent="office", slug="theirs")
    call = _as(db, app)

    got = app_tool(
        call, "organise_folder", folder="mine", action="rename", new_slug="kept"
    )
    assert got["isError"] is False
    assert got["structuredContent"]["slug"] == "kept"
    preview = app_tool(call, "organise_folder", folder="kept", action="archive")
    assert preview["structuredContent"]["would_archive"]["pages"] == 2
    assert _archived(db, ids[0]) is None
    done = app_tool(
        call, "organise_folder", folder="kept", action="archive", confirm=True
    )
    assert done["structuredContent"]["pages"] == 2 and _archived(db, ids[0])

    theirs = app_tool(
        call, "organise_folder", folder="theirs", action="rename", title="x"
    )
    ghost = app_tool(
        call, "organise_folder", folder="ghost", action="rename", title="x"
    )
    assert theirs == ghost
    assert theirs["isError"] is True and theirs["content"][0]["text"] == NOT_YOURS
    assert store.list_in_folder(db, "office", "theirs") is not None
