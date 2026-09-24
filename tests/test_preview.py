"""Look at a page before publishing it (the seats, 24 Sep 2026, homelab-ij8.107)."""

import base64

import pytest
from fastapi.testclient import TestClient

from artifacts import keys, store, thumbs
from artifacts.app import create_gallery_app

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20
PAGE = "<!doctype html><title>t</title><p>x</p>"


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def auth(db):
    token, _ = keys.mint(db, agent="seat")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def browser(monkeypatch):
    seen = {}

    def fake(html, width, height):
        seen.update(html=html, width=width, height=height)
        return PNG, ["line 3: Uncaught ReferenceError: chart is not defined"]

    monkeypatch.setattr(thumbs, "available", lambda: True)
    monkeypatch.setattr(thumbs, "render_report", fake)
    return seen


def test_a_preview_is_a_phone_shot_and_the_errors_and_nothing_is_stored(
    db, api, auth, browser
):
    got = api.post("/api/v1/preview", json={"body": PAGE}, headers=auth).json()
    assert got["rendered"] is True
    assert base64.b64decode(got["png_base64"]) == PNG
    assert got["errors"] == ["line 3: Uncaught ReferenceError: chart is not defined"]
    assert (browser["width"], browser["height"]) == (390, 1600)
    assert store.list_for_agent(db, "seat") == [], "a preview publishes nothing"


def test_the_viewport_is_clamped(api, auth, browser):
    api.post(
        "/api/v1/preview",
        json={"body": PAGE, "width": 5, "height": 99999},
        headers=auth,
    )
    assert (browser["width"], browser["height"]) == (320, 4000)


def test_no_browser_says_so_and_never_reads_as_clean(api, auth, monkeypatch):
    monkeypatch.setattr(thumbs, "available", lambda: False)
    got = api.post("/api/v1/preview", json={"body": PAGE}, headers=auth).json()
    assert got == {"rendered": False, "renderer": False, "errors": None}


def test_a_preview_needs_a_key_and_the_publish_size_limit(api, auth, browser):
    assert api.post("/api/v1/preview", json={"body": PAGE}).status_code == 401
    big = "x" * (store.MAX_BODY_BYTES + 1)
    assert (
        api.post("/api/v1/preview", json={"body": big}, headers=auth).status_code == 413
    )
