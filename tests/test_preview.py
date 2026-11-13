"""Look at a page before publishing it (the seats, 24 Sep 2026, homelab-ij8.107)."""

import base64

import pytest
from fastapi.testclient import TestClient

from ream import keys, store, thumbs
from ream.app import create_gallery_app

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


# --- with a renderer as its own process (REAM_RENDERER_URL) ------------------------


def _remote_png(width: int, height: int) -> bytes:
    import struct
    import zlib

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00" * ((width + 7) // 8) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _reply(handler, status, body):
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


@pytest.fixture()
def remote(stub_renderer, monkeypatch):
    """A renderer set, and a browser here that must never start."""
    import json
    from urllib.parse import parse_qs, urlsplit

    from ream import rendercore, settings

    started = []
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    monkeypatch.setattr(rendercore, "_run", lambda argv, **kw: started.append(argv))

    def valid(handler):
        q = parse_qs(urlsplit(handler.path).query)
        picture = _remote_png(int(q["w"][0]), int(q["h"][0]))
        answer = {
            "v": 1,
            "outcome": "rendered",
            "png_base64": base64.b64encode(picture).decode(),
            "errors": ["line 3: Uncaught ReferenceError: chart is not defined"],
            "check": {"text": 5, "media": 0, "height": 300, "scroll": 390, "view": 390},
        }
        _reply(handler, 200, json.dumps(answer).encode())

    stub_renderer.answer = valid
    stub_renderer.valid = valid
    with settings.override(renderer_url=stub_renderer.url):
        yield stub_renderer
    assert started == [], "a browser started in this process"


@pytest.mark.background_thumbnails  # the real thumbs.available, not the suite's "no"
def test_a_remote_preview_is_the_renderers_answer(api, auth, remote):
    got = api.post("/api/v1/preview", json={"body": PAGE}, headers=auth)
    assert got.status_code == 200
    got = got.json()
    assert got["rendered"] is True and got["renderer"] is True
    assert base64.b64decode(got["png_base64"]) == _remote_png(390, 1600)
    assert got["errors"] == ["line 3: Uncaught ReferenceError: chart is not defined"]
    assert got["verdict"] == "broken"
    assert "reason" not in got
    assert remote.requests[0][0] == "/v1/render?w=390&h=1600&check=1"


@pytest.mark.background_thumbnails
@pytest.mark.parametrize(
    "answer",
    [
        lambda h: _reply(h, 503, b"busy\n"),
        lambda h: _reply(h, 200, b"{not json"),
        lambda h: _reply(h, 302, b""),
    ],
    ids=["busy", "not-json", "redirect"],
)
def test_a_renderer_with_no_usable_answer_still_answers_200_with_a_reason(
    api, auth, remote, answer
):
    from ream import renderfind, renderwire

    remote.answer = answer
    got = api.post("/api/v1/preview", json={"body": PAGE}, headers=auth)
    assert got.status_code == 200
    assert got.json() == {
        "rendered": False,
        "renderer": True,
        "width": 390,
        "height": 1600,
        "errors": None,
        "verdict": "unchecked",
        "findings": [{"kind": "unchecked", "detail": renderfind.UNAVAILABLE_NOTE}],
        "png_base64": None,
        "reason": renderwire.UNAVAILABLE,
    }


@pytest.mark.background_thumbnails
def test_a_renderer_that_did_not_render_answers_200_with_its_own_reason(
    api, auth, remote
):
    import json

    from ream import renderfind, renderwire

    not_run = {
        "v": 1,
        "outcome": "not_run",
        "png_base64": None,
        "errors": [],
        "check": None,
    }
    remote.answer = lambda h: _reply(h, 200, json.dumps(not_run).encode())
    got = api.post("/api/v1/preview", json={"body": PAGE}, headers=auth).json()
    assert got["rendered"] is False and got["renderer"] is True
    assert got["verdict"] == "unchecked"
    assert got["findings"] == [{"kind": "unchecked", "detail": renderfind.NOT_RUN_NOTE}]
    assert got["reason"] == renderwire.NOT_RUN


@pytest.mark.background_thumbnails
def test_a_renderer_that_is_not_there_still_answers_200_with_a_reason(
    api, auth, monkeypatch
):
    import socket

    from ream import settings

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("REAM_BROWSER", "/nonexistent/browser")
    with settings.override(renderer_url=f"http://127.0.0.1:{port}"):
        got = api.post("/api/v1/preview", json={"body": PAGE}, headers=auth).json()
    assert got["renderer"] is True and got["rendered"] is False
    assert got["reason"] == "renderer_unavailable"
