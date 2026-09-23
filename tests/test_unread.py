"""What Jeremy has not read rises to the top, and its agent can see when he
has (homelab-ij8.103, his confirmed ruling of 23 Sep 2026)."""

import re

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>t</title></head><body>{}</body></html>"


@pytest.fixture()
def gallery(db):
    return TestClient(create_gallery_app(db))


def _order(gallery):
    wall = gallery.get("/").text
    return re.findall(r'<a href="/a/([a-z0-9]+)">', wall)


def _publish(db, title):
    return store.publish(db, "seat", title, PAGE.format(title))


def test_a_new_page_is_unread_and_opening_it_makes_it_read(db, gallery):
    page = _publish(db, "One")
    assert 'data-unread="1"' in gallery.get("/").text
    gallery.get(f"/a/{page.id}")
    assert 'data-unread="1"' not in gallery.get("/").text


def test_a_republished_page_is_unread_again(db, gallery):
    page = _publish(db, "One")
    gallery.get(f"/a/{page.id}")
    store.patch(db, "seat", page.id, PAGE.format("changed"), page.revision)
    assert 'data-unread="1"' in gallery.get("/").text


def test_the_wall_order_is_pinned_then_questions_then_unread_then_the_rest(db, gallery):
    read_old = _publish(db, "read-old")
    asking = _publish(db, "asking")
    pinned = _publish(db, "pinned")
    unread = _publish(db, "unread")
    read_new = _publish(db, "read-new")
    for page in (read_old, asking, pinned, read_new):
        gallery.get(f"/a/{page.id}")
    decisions.ask(
        db, agent="seat", artifact_id=asking.id, question="ship?",
        options=[{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}],
    )
    store.set_pinned(db, pinned.id, True, agent=None)
    order = _order(gallery)
    assert order[:5] == [pinned.id, asking.id, unread.id, read_new.id, read_old.id]


def test_the_agent_sees_whether_he_has_opened_its_latest_version(db, gallery):
    token, _ = keys.mint(db, agent="seat")
    auth = {"Authorization": f"Bearer {token}"}
    page = _publish(db, "One")
    before = gallery.get(f"/api/v1/artifacts/{page.id}", headers=auth).json()
    assert before["seen_version"] is None and before["seen_at"] is None
    gallery.get(f"/a/{page.id}")
    after = gallery.get(f"/api/v1/artifacts/{page.id}", headers=auth).json()
    assert after["seen_version"] == after["current_version"] == 1
    listed = gallery.get("/api/v1/artifacts", headers=auth).json()
    assert listed[0]["seen_version"] == 1
    store.patch(db, "seat", page.id, PAGE.format("changed"), page.revision)
    stale = gallery.get(f"/api/v1/artifacts/{page.id}", headers=auth).json()
    assert stale["seen_version"] == 1 and stale["current_version"] == 2


def test_only_the_owner_sees_it(db, gallery):
    other, _ = keys.mint(db, agent="other")
    page = _publish(db, "One")
    gallery.get(f"/a/{page.id}")
    response = gallery.get(
        f"/api/v1/artifacts/{page.id}", headers={"Authorization": f"Bearer {other}"}
    )
    assert response.status_code == 404
