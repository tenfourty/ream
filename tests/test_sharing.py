"""Sharing a page with people outside his space (homelab-ij8.101, Jeremy's
rulings: revocable at once; 1a latest version; 2a just the page; 3a only him)."""

import re

import pytest
from fastapi.testclient import TestClient

from ream import decisions, keys, store
from ream.app import create_app, create_gallery_app

SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
PAGE = "<!doctype html><html><head><title>t</title></head><body><p>{}</p></body></html>"


@pytest.fixture()
def page(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return store.publish(db, "seat", "Board", PAGE.format("first words"))


@pytest.fixture()
def gallery(db):
    return TestClient(create_gallery_app(db))


def _share(gallery, page):
    done = gallery.post(f"/a/{page.id}/share", headers=SAME_ORIGIN)
    url = re.search(r'<pre class="prompt">([^<]+)</pre>', done.text).group(1)
    return url


@pytest.fixture()
def outside(db):
    """The whole service as the outside world reaches it, built once per test
    rather than once per request: building it is ~0.1 s of route set-up, and
    these tests open three or four links."""
    return TestClient(create_app(db))


def _open(outside, url):
    host, path = re.match(r"https://([^/]+)(/.*)", url).groups()
    return outside.get(path, headers={"host": host})


def test_he_shares_and_anyone_with_the_link_reads_the_page(outside, db, gallery, page):
    url = _share(gallery, page)
    assert url.startswith(f"https://{page.id}.tenfourty.com/s/")
    opened = _open(outside, url)  # no login, no frame
    assert opened.status_code == 200
    assert "first words" in opened.text
    assert opened.headers["x-robots-tag"].startswith("noindex")


def test_the_link_always_shows_the_latest_version(outside, db, gallery, page):
    url = _share(gallery, page)
    store.patch(db, "seat", page.id, PAGE.format("newer words"), page.revision)
    text = _open(outside, url).text
    assert "newer words" in text and "first words" not in text


def test_just_the_page_no_questions_no_answers_no_runtime(outside, db, gallery, page):
    asked = decisions.ask(
        db,
        agent="seat",
        artifact_id=page.id,
        question="secret question?",
        options=[{"letter": "a", "label": "x"}, {"letter": "b", "label": "y"}],
    )
    decisions.answer(db, asked.id, letter="a", note="private note", by="jeremy")
    text = _open(outside, _share(gallery, page)).text
    assert "secret question?" not in text and "private note" not in text
    assert "artifact-state" not in text, "no viewer channel to the gallery"


def test_stopping_kills_the_link_at_once_and_sharing_again_makes_a_new_one(
    outside, db, gallery, page
):
    old = _share(gallery, page)
    stopped = gallery.post(f"/a/{page.id}/share/stop", headers=SAME_ORIGIN)
    assert "Stopped. The link no longer works." in stopped.text
    dead = _open(outside, old)
    assert dead.status_code == 404 and "withdrawn" in dead.text
    new = _share(gallery, page)
    assert new != old
    assert _open(outside, old).status_code == 404
    assert _open(outside, new).status_code == 200


def test_every_failure_looks_the_same(outside, db, gallery, page):
    url = _share(gallery, page)
    wrong = _open(outside, url.rsplit("/s/", 1)[0] + "/s/not-the-token/")
    other_host = _open(outside, url.replace(page.id, "b" * 26))
    store.set_deleted(db, page.id, True, agent="seat")
    deleted = _open(outside, url)
    bodies = {(r.status_code, r.text) for r in (wrong, other_host, deleted)}
    assert len(bodies) == 1 and next(iter(bodies))[0] == 404


def test_only_he_can_share_there_is_no_agent_route(db, gallery, page):
    token, _ = keys.mint(db, agent="seat")
    auth = {"Authorization": f"Bearer {token}"}
    assert gallery.post(
        f"/api/v1/artifacts/{page.id}/share", headers=auth
    ).status_code in (404, 405)
    cross = gallery.post(
        f"/a/{page.id}/share", headers={"sec-fetch-site": "cross-site"}
    )
    assert cross.status_code == 403
    assert store.share_token(db, page.id) is None


def test_the_menu_says_when_a_page_is_shared(db, gallery, page):
    assert "Share a link" in gallery.get(f"/a/{page.id}").text
    _share(gallery, page)
    assert "Shared by link" in gallery.get(f"/a/{page.id}").text
    share_page = gallery.get(f"/a/{page.id}/share").text
    assert '<script src="/connect.js" defer></script>' in share_page, "the Copy button"


def test_visibility_follows_the_switch(db, gallery, page):
    _share(gallery, page)
    assert store.get_owned(db, "seat", page.id).visibility == "link"
    gallery.post(f"/a/{page.id}/share/stop", headers=SAME_ORIGIN)
    assert store.get_owned(db, "seat", page.id).visibility == "private"
