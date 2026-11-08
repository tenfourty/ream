"""Comments that reach the agent (homelab-ij8.18, Jeremy's order of 24 Sep
2026): his comment wakes the agent's waiter, the agent lists, replies and
settles threads from its own tool."""

import time

import pytest
from test_client import key_file, live  # noqa: F401

from ream import comments, decide, store

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>the plan</p></body></html>"


@pytest.fixture()
def page(db, key_file):
    # Published by the same office the waiter below declares ("t"): a waiter
    # hears only comments on its own office's pages (feedback, 6 Oct 2026).
    return store.publish(db, "ops-seat", "Board", PAGE, session="t/0")


def _run(args, key_path) -> int:
    return decide.main(["--key-file", str(key_path), "--session", "t/1", *args])


def _he_comments(db, page, body, quote="", thread_root=None):
    return comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body=body,
        quote=quote,
        thread_root=thread_root,
    )


def test_his_comment_wakes_the_waiter(
    db, live, key_file, page, capsys, on_first_listen
):
    path, _ = key_file
    # He comments only once the waiter is listening, so exit 0 means the
    # comment woke it -- it cannot have been found by the first look.
    listener = on_first_listen(
        lambda: _he_comments(
            db, page, "can you make the second chart bigger?", quote="the plan"
        )
    )
    started = time.monotonic()
    assert _run(["wait", "--page", page.id, "--timeout", "40s"], path) == 0
    assert time.monotonic() - started < 15
    listener.woke_the_first_hold()
    out = capsys.readouterr().out
    assert "NEEDS YOUR REPLY" in out
    assert "HE SAID: can you make the second chart bigger?" in out
    assert 'on: "the plan"' in out
    assert "artifacts-decide reply" in out


def test_answers_only_ignores_comments(db, live, key_file, page, capsys, brief_holds):
    path, _ = key_file
    _he_comments(db, page, "a comment")
    assert (
        _run(["wait", "--page", page.id, "--timeout", "0.1s", "--answers-only"], path)
        == 3
    )


def test_list_reply_and_settle_a_thread(db, live, key_file, page, capsys):
    path, _ = key_file
    root = _he_comments(db, page, "why this order?")
    assert _run(["comments"], path) == 0
    assert "NEEDS YOUR REPLY" in capsys.readouterr().out

    assert _run(["reply", root.id, "because the forge moves first"], path) == 0
    capsys.readouterr()
    thread = comments.list_for_artifact(db, page.id, include_resolved=False)
    assert [c.body for c in thread] == [
        "why this order?",
        "because the forge moves first",
    ]
    assert _run(["comments"], path) == 0
    assert "[replied]" in capsys.readouterr().out

    # his reply puts the ball back in the agent's court
    _he_comments(db, page, "ok, and then?", thread_root=root.id)
    assert _run(["comments"], path) == 0
    assert "NEEDS YOUR REPLY" in capsys.readouterr().out

    assert _run(["settle", root.id], path) == 0
    capsys.readouterr()
    assert _run(["comments"], path) == 0
    assert "no open comment threads" in capsys.readouterr().out


def test_an_agent_can_reopen_a_thread_it_settled(live, key_file, db, page, capsys):
    path, _ = key_file
    root = _he_comments(db, page, "why this order?")
    assert _run(["settle", root.id], path) == 0
    capsys.readouterr()
    assert _run(["reopen", root.id], path) == 0
    assert f"reopened {root.id}" in capsys.readouterr().out
    assert _run(["comments"], path) == 0
    assert "NEEDS YOUR REPLY" in capsys.readouterr().out


def test_replying_to_a_thread_that_is_not_yours_is_refused(live, key_file, capsys):
    path, _ = key_file
    assert _run(["reply", "nope", "hello"], path) == 2
    assert "no open comment thread" in capsys.readouterr().err


# --- his side: select text, comment on it -------------------------------------


@pytest.fixture()
def gallery(db):
    from fastapi.testclient import TestClient

    from ream.app import create_gallery_app

    return TestClient(create_gallery_app(db))


def test_he_comments_on_selected_text_and_the_anchor_is_kept(db, gallery):
    page = store.publish(db, "seat", "Board", PAGE)
    import json

    anchor = {"kind": "range", "path": "body>p:nth-of-type(1)", "start": 4, "end": 8}
    response = gallery.post(
        f"/c/{page.id}",
        data={"body": "tighten this", "quote": "plan", "anchor": json.dumps(anchor)},
        headers={"sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    (row,) = comments.list_for_artifact(db, page.id, include_resolved=False)
    assert row.quote == "plan" and row.anchor == anchor
    bad = gallery.post(
        f"/c/{page.id}",
        data={"body": "x", "anchor": '{"kind": "range", "path": 1}'},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert bad.status_code == 422


def test_the_page_view_loads_the_selection_script(db, gallery):
    page = store.publish(db, "seat", "Board", PAGE)
    assert (
        '<script src="/comments.js" defer></script>'
        in gallery.get(f"/a/{page.id}").text
    )
    served = gallery.get("/comments.js")
    assert served.headers["cache-control"] == "no-cache"


def test_the_page_reports_selections_only_to_the_gallery(monkeypatch):
    from ream import content

    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://g.example")
    shim = content._viewer_report()
    assert "selectionchange" in shim and "artifact-selection" in shim
    assert '"*"' not in shim


# --- an agent comments on another office's page (21 Oct 2026) -----------------


def test_comment_on_another_offices_page_names_the_owner(db, live, key_file, capsys):
    path, _ = key_file
    theirs = store.publish(db, "cove", "Boot", PAGE, session="cove/ab12")
    assert (
        _run(["comment", theirs.id, "cap it lower", "--quote", "the plan"], path) == 0
    )
    out = capsys.readouterr().out
    assert f"{theirs.page_url}#comments" in out
    assert "page owner: cove (last session cove/ab12)" in out
    [row] = comments.list_for_artifact(db, theirs.id)
    assert (row.author, row.quote, row.session) == ("ops-seat", "the plan", "t/1")


def test_comment_refuses_a_quote_that_is_not_there(db, live, key_file, capsys):
    path, _ = key_file
    theirs = store.publish(db, "cove", "Boot", PAGE)
    assert _run(["comment", theirs.id, "x", "--quote", "absent words"], path) == 1
    assert "not found" in capsys.readouterr().err
    assert _run(["comment", theirs.id, "x", "--section", "Intro"], path) == 2


def test_another_offices_comment_on_my_page_needs_my_reply(
    db, live, key_file, page, capsys
):
    path, _ = key_file
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="cove",
        agent="cove",
        body="have you considered X?",
        session="cove/ab12",
    )
    assert _run(["comments"], path) == 0
    out = capsys.readouterr().out
    assert "NEEDS YOUR REPLY" in out
    assert "cove (agent, via session cove/ab12) said: have you considered X?" in out


def test_reply_note_round_trip_and_the_hint(db, live, key_file, page, capsys):
    from ream import decisions

    path, _ = key_file
    ask = decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=page.id,
        question="ship?",
        options=[{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}],
    )
    # Not answered yet: nothing to reply to.
    assert _run(["reply-note", ask.id, "hello"], path) == 1
    capsys.readouterr()
    decisions.answer(db, ask.id, letter="a", note="go, mind the cost", by="jeremy")
    assert _run(["reply-note", ask.id, "which cost?"], path) == 0
    out = capsys.readouterr().out
    assert f"#note-{ask.id}" in out and "page owner: ops-seat" in out
    thread = comments.note_threads(db, page.id)[ask.id]
    assert [m.body for m in thread] == ["which cost?"]
    # He answers back; the thread now needs the agent, and says where.
    _he_comments(db, page, "the hosting bill", thread_root=thread[0].id)
    assert _run(["comments"], path) == 0
    out = capsys.readouterr().out
    assert f"under his answer on decision {ask.id}" in out
    assert f'artifacts-decide reply-note {ask.id} "..."' in out
