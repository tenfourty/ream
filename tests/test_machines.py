"""The machines page: see every connected machine, rename it, revoke it
(homelab-ij8.103, Jeremy's confirmed ask of 23 Sep 2026)."""

import re

import pytest
from fastapi.testclient import TestClient

from artifacts import keys, store
from artifacts.app import create_gallery_app

SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
PAGE = "<!doctype html><html><head><title>t</title></head><body>x</body></html>"


@pytest.fixture()
def gallery(db):
    return TestClient(create_gallery_app(db))


def test_every_gallery_page_has_his_menu_with_machines_and_connect(gallery):
    page = gallery.get("/").text
    assert 'class="menu profile"' in page
    assert 'href="/machines"' in page and 'href="/connect"' in page


def test_the_page_lists_machines_with_what_he_needs_and_no_secret(db, gallery):
    token, _ = keys.mint(db, agent="laptop")
    store.publish(db, "laptop", "Board", PAGE, session="claude/abc")
    gallery.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    page = gallery.get("/machines").text
    assert "<h2>laptop</h2>" in page
    assert "claude/abc" in page
    assert re.search(r"<dt>Pages</dt><dd>1</dd>", page)
    assert "not since it connected" not in page, "last used is recorded"
    assert token not in page and "art_" not in page
    assert "sha256" not in page


def test_he_renames_a_machine_and_its_pages_keep_their_identity(db, gallery):
    _t, key_id = keys.mint(db, agent="laptop")
    gallery.post(
        f"/machines/{key_id}/rename",
        data={"label": "Jeremy's MacBook"},
        headers=SAME_ORIGIN,
    )
    page = gallery.get("/machines").text
    assert "<h2>Jeremy&#39;s MacBook</h2>" in page
    assert "<dt>Files pages as</dt><dd>laptop</dd>" in page


def test_revoke_asks_once_more_then_the_key_stops_working(db, gallery):
    token, key_id = keys.mint(db, agent="laptop")
    auth = {"Authorization": f"Bearer {token}"}
    confirm = gallery.get(f"/machines?revoke={key_id}").text
    assert "Its key stops working at once" in confirm
    assert gallery.get("/api/v1/me", headers=auth).status_code == 200
    done = gallery.post(f"/machines/{key_id}/revoke", headers=SAME_ORIGIN)
    assert "Revoked. That key no longer works." in done.text
    assert gallery.get("/api/v1/me", headers=auth).status_code == 401
    assert "Revoked (1)" in gallery.get("/machines").text


def test_another_site_cannot_revoke_or_rename_with_his_session(db, gallery):
    token, key_id = keys.mint(db, agent="laptop")
    cross = {"sec-fetch-site": "cross-site"}
    assert gallery.post(f"/machines/{key_id}/revoke", headers=cross).status_code == 403
    assert (
        gallery.post(
            f"/machines/{key_id}/rename", data={"label": "x"}, headers=cross
        ).status_code
        == 403
    )
    assert (
        gallery.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 200
    )


def test_last_used_is_not_written_on_every_request(db, gallery):
    token, key_id = keys.mint(db, agent="laptop")
    auth = {"Authorization": f"Bearer {token}"}
    gallery.get("/api/v1/me", headers=auth)
    with db.connect() as conn:
        first = conn.execute(
            "select last_used_at from agent_key where id = %s", (key_id,)
        ).fetchone()[0]
    gallery.get("/api/v1/me", headers=auth)
    with db.connect() as conn:
        second = conn.execute(
            "select last_used_at from agent_key where id = %s", (key_id,)
        ).fetchone()[0]
    assert first is not None and first == second
