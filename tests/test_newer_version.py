"""A page republished while he is reading it tells him, and waits.

homelab-ij8.74. Jeremy's own ruling, 19 Sep 2026, answering on the status page:
(b) "it tells you a newer version exists and waits for you to say when", over
(a) refreshing under him. Before this, an agent republishing underneath him was
invisible until he reloaded by hand -- so he could be weighing a question
against text that had already been replaced.

The server's half is here: a read that HOLDS until the version differs, which
is the same machinery the agent-facing waiter uses and the reason gallery.js
needs no timer. The offering and the waiting-for-his-word are driven in
tests/js/newer.test.mjs, against the real script.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from artifacts import events, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


@pytest.fixture()
def page(db):
    return store.publish(db, "seat", "Board", PAGE)


def test_it_says_which_version_is_live(client, page):
    got = client.get(f"/a/{page.id}/current")
    assert got.status_code == 200
    assert got.json() == {"n": 1}


def test_an_unknown_page_is_a_404_not_a_zero(client):
    assert client.get("/a/nosuchpageatallxxxxxxxxxx/current").status_code == 404


def test_a_deleted_page_stops_answering(client, db, page):
    """It is in the bin; there is no live version to watch for."""
    store.set_deleted(db, page.id, True, agent=None)
    assert client.get(f"/a/{page.id}/current").status_code == 404


def test_asking_about_the_version_you_have_holds(client, page):
    """The whole point: the browser does not wake up and ask, the server waits."""
    began = time.monotonic()
    got = client.get(f"/a/{page.id}/current?since=1&wait=2")
    waited = time.monotonic() - began
    assert got.json() == {"n": 1}, "unchanged, so ask again"
    assert waited >= 1.5, f"it held rather than answering at once ({waited:.2f}s)"


def test_a_republish_ends_the_hold_within_a_second(client, db, page):
    """The acceptance criteria: a notice appears in the open view within
    seconds of an agent republishing."""
    answer = {}

    def hold():
        began = time.monotonic()
        answer["json"] = client.get(f"/a/{page.id}/current?since=1&wait=20").json()
        answer["waited"] = time.monotonic() - began

    thread = threading.Thread(target=hold)
    thread.start()
    time.sleep(0.8)  # long enough that it is genuinely holding
    store.patch(db, "seat", page.id, PAGE.replace("hi", "hi again"), page.revision)
    thread.join(20)

    assert answer["json"] == {"n": 2}
    assert answer["waited"] < 5, f"answered by the republish ({answer['waited']:.2f}s)"


def test_a_stale_since_is_answered_at_once(client, db, page):
    """A tab that has been asleep asks about a version that is already old. It
    must not be made to wait for the NEXT change to hear about the last one."""
    store.patch(db, "seat", page.id, PAGE.replace("hi", "hi again"), page.revision)
    began = time.monotonic()
    got = client.get(f"/a/{page.id}/current?since=1&wait=20")
    assert got.json() == {"n": 2}
    assert time.monotonic() - began < 2


def test_a_shutdown_ends_the_hold_rather_than_outliving_it(client, page):
    """A rollout must never wait out somebody's browser. The same rule the
    agent-facing waiter follows, through the same event."""
    answer = {}

    def hold():
        began = time.monotonic()
        answer["json"] = client.get(f"/a/{page.id}/current?since=1&wait=20").json()
        answer["waited"] = time.monotonic() - began

    thread = threading.Thread(target=hold)
    thread.start()
    time.sleep(0.5)
    events.begin_shutdown()
    try:
        thread.join(20)
        assert answer["json"] == {"n": 1}
        assert answer["waited"] < 5, "it let go when the pod was asked to stop"
    finally:
        events._shutdown.clear()


def test_too_many_watchers_are_answered_rather_than_queued(client, page, monkeypatch):
    """Past the bound it answers at once, which is ordinary polling and
    correct -- an answer now beats a thread later."""
    from artifacts import gallery

    monkeypatch.setattr(gallery, "_watchers", threading.BoundedSemaphore(1))
    held = threading.Event()

    def hold():
        held.set()
        client.get(f"/a/{page.id}/current?since=1&wait=3")

    thread = threading.Thread(target=hold)
    thread.start()
    held.wait(5)
    time.sleep(0.3)
    began = time.monotonic()
    got = client.get(f"/a/{page.id}/current?since=1&wait=3")
    assert got.json() == {"n": 1}
    assert time.monotonic() - began < 1.5, "answered immediately rather than queued"
    thread.join(10)


def test_the_wait_is_capped(client, page):
    """Above the cap it is a 422, not a request that outlives every proxy in
    front of it."""
    assert client.get(f"/a/{page.id}/current?since=1&wait=600").status_code == 422
