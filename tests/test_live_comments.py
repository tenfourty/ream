"""Comments an agent adds, answers or settles reach a page Jeremy has open.

9 Nov 2026, Jeremy: new threads, replies and settles from the page's agent did not
appear while he had the page open; he sometimes reloaded to see them all.
"Sometimes" was the republish watch: `/a/{id}/current` is a held read on the
page's VERSION, and a comment is not a new version, so it never answered for a
comment. Only an agent that happened to publish a new version at the same time
made the "newer version" bar appear, and Show it (a reload) brought the
comments along. Nothing watched the comments themselves.

The server's half is here: `/a/{id}/comments-live`, a held read on the page's
comment signature that answers with the panel, the count and the marks. The
page's half is driven in a real browser in tests/test_panel_browser.py.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from ream import comments, events, gallery, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    real_wait = events.wait_for_shutdown
    monkeypatch.setattr(
        events, "wait_for_shutdown", lambda timeout: real_wait(min(timeout, 0.02))
    )
    return TestClient(create_gallery_app(db=db), base_url="https://testserver")


@pytest.fixture()
def page(db):
    return store.publish(db, "seat", "Board", PAGE)


def _agent(db, page, body, **kw):
    return comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        body=body,
        agent="seat",
        **kw,
    )


def test_a_comment_is_not_a_new_version_which_is_why_current_never_saw_it(
    client, db, page
):
    """The root cause, in one assertion: the version watch cannot carry comments."""
    before = client.get(f"/a/{page.id}/current").json()
    _agent(db, page, "A new thread from the agent")
    after = client.get(f"/a/{page.id}/current").json()
    assert before == after == {"n": 1}


def test_the_signature_moves_for_every_kind_of_change(db, page):
    seen = [comments.signature(db, page.id)]

    def moved(what):
        now = comments.signature(db, page.id)
        assert now != seen[-1], what
        seen.append(now)

    root = _agent(db, page, "first")
    moved("a new thread")
    time.sleep(0.01)
    reply = _agent(db, page, "a reply", thread_root=root.id)
    moved("a reply")
    comments.resolve(db, root.id, by="seat", agent="seat")
    moved("a settle")
    comments.reopen(db, root.id, agent="seat")
    moved("a reopen")
    assert reply.id


def test_the_signature_is_stable_when_nothing_changes(db, page):
    _agent(db, page, "first")
    assert comments.signature(db, page.id) == comments.signature(db, page.id)


def test_the_page_carries_the_signature_it_was_drawn_from(client, db, page):
    html = client.get(f"/a/{page.id}").text
    assert f'data-csig="{comments.signature(db, page.id)}"' in html
    assert '<script src="/live.js" defer></script>' in html


def test_the_live_script_is_served(client):
    got = client.get("/live.js")
    assert got.status_code == 200
    assert "comments-live" in got.text


def test_asking_with_a_stale_signature_answers_at_once_with_the_panel(client, db, page):
    old = comments.signature(db, page.id)
    root = _agent(db, page, "AGENTNOTE look at this")
    got = client.get(f"/a/{page.id}/comments-live", params={"since": old, "wait": 25})
    body = got.json()
    assert got.status_code == 200
    assert body["sig"] == comments.signature(db, page.id) != old
    assert "AGENTNOTE look at this" in body["panel"]
    assert f'id="thread-{root.id}"' in body["panel"]
    assert body["open"] == 1
    assert body["marks"] == []


def test_a_reply_a_settle_and_a_reopen_each_come_through(client, db, page):
    root = _agent(db, page, "thread")
    sig = comments.signature(db, page.id)

    _agent(db, page, "REPLYTEXT", thread_root=root.id)
    got = client.get(f"/a/{page.id}/comments-live", params={"since": sig}).json()
    assert "REPLYTEXT" in got["panel"]
    sig = got["sig"]

    comments.resolve(db, root.id, by="seat", agent="seat")
    got = client.get(f"/a/{page.id}/comments-live", params={"since": sig}).json()
    assert got["open"] == 0
    sig = got["sig"]

    comments.reopen(db, root.id, agent="seat")
    got = client.get(f"/a/{page.id}/comments-live", params={"since": sig}).json()
    assert got["open"] == 1


def test_marks_follow_a_settle(client, db, page):
    quote = "x"
    root = _agent(
        db,
        page,
        "mark",
        quote=quote,
        anchor={"kind": "range", "path": "body>p:nth-of-type(1)", "start": 0, "end": 1},
    )
    sig = comments.signature(db, page.id)
    comments.resolve(db, root.id, by="seat", agent="seat")
    got = client.get(f"/a/{page.id}/comments-live", params={"since": sig}).json()
    assert got["marks"] == []
    assert comments.marks_for_page(db, page.id) == []


def test_nothing_new_after_the_hold_is_said_plainly(client, page, db):
    sig = comments.signature(db, page.id)
    got = client.get(
        f"/a/{page.id}/comments-live", params={"since": sig, "wait": 0.1}
    ).json()
    assert got == {"same": True, "sig": sig}


def test_a_held_read_ends_when_the_agent_comments(client, db, page, monkeypatch):
    fresh = threading.BoundedSemaphore(gallery._MAX_WATCHERS)
    monkeypatch.setattr(gallery, "_watchers", fresh)
    sig = comments.signature(db, page.id)
    out = {}

    def ask():
        out["r"] = client.get(
            f"/a/{page.id}/comments-live", params={"since": sig, "wait": 20}
        ).json()

    t = threading.Thread(target=ask)
    t.start()
    deadline = time.monotonic() + 10
    while fresh._value >= gallery._MAX_WATCHERS and time.monotonic() < deadline:
        time.sleep(0.002)
    assert fresh._value < gallery._MAX_WATCHERS, "the read never began holding"
    started = time.monotonic()
    _agent(db, page, "LIVENOTE")
    t.join(10)
    assert not t.is_alive()
    assert time.monotonic() - started < 5
    assert "LIVENOTE" in out["r"]["panel"]


def test_an_unknown_page_is_a_404(client):
    got = client.get("/a/nosuchpageatallxxxxxxxxxx/comments-live")
    assert got.status_code == 404
