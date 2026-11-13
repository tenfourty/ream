"""Renders sent to a renderer running as its own process (`REAM_RENDERER_URL`).

A stub renderer (a tiny local HTTP server, `stub_renderer` in tests/conftest.py)
answers each way a renderer can fail, and once as it should. What the app made of
each is read back through the store, so this file needs no browser and runs on
both databases and with no plug-ins. The real renderer is exercised by the browser
files, parametrized over `render_mode`.
"""

import binascii
import json
import socket
import struct
import time
import types
import zlib
from urllib.parse import parse_qs, urlsplit

import pytest

from ream import keys, rendercore, renderfind, renderwire, settings, store, thumbs

# The real background render: tests elsewhere have it switched off.
pytestmark = pytest.mark.background_thumbnails

PAGE = (
    "<!doctype html><html><head><title>R</title></head>"
    "<body><p>DECISION · <!--artifacts:updated--> · R</p></body></html>"
)
UNAVAILABLE = {
    "rendered": True,
    "errors": [],
    "verdict": "unchecked",
    "findings": [{"kind": "unchecked", "detail": renderfind.UNAVAILABLE_NOTE}],
}
CHECK = {"text": 12, "media": 0, "height": 300, "scroll": 900, "view": 900}


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data))
    )


def png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00" * ((width + 7) // 8) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(rows))
        + _chunk(b"IEND", b"")
    )


def _size(handler) -> tuple[int, int]:
    query = parse_qs(urlsplit(handler.path).query)
    return int(query["w"][0]), int(query["h"][0])


def _send(handler, status: int, body: bytes, **headers) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for name, value in headers.items():
        handler.send_header(name.replace("_", "-"), value)
    handler.end_headers()
    handler.wfile.write(body)


def answer_json(picture: bytes | None, errors=(), check=CHECK) -> bytes:
    return json.dumps(
        {
            "v": 1,
            "outcome": "rendered" if picture else "no_picture",
            "png_base64": binascii.b2a_base64(picture, newline=False).decode()
            if picture
            else None,
            "errors": list(errors),
            "check": check,
        }
    ).encode()


def valid(handler) -> None:
    _send(handler, 200, answer_json(png(*_size(handler))))


@pytest.fixture(autouse=True)
def no_local_browser(monkeypatch):
    """A browser this process would start, were it to start one: none may start."""
    started = []
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    monkeypatch.setattr(rendercore, "_run", lambda argv, **kw: started.append(argv))
    thumbs._failures.clear()
    yield started
    thumbs._failures.clear()
    assert started == [], "a browser started in this process"


class _InlineThread:
    def __init__(self, target, **kw):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture()
def page(db, monkeypatch):
    # Only thumbs' own thread runs inline: the stub renderer needs real ones.
    monkeypatch.setattr(
        thumbs, "threading", types.SimpleNamespace(Thread=_InlineThread)
    )
    keys.mint(db, agent="seat")
    return store.publish(db, "seat", "Board", PAGE)


def render(db, page, url: str) -> dict:
    with settings.override(renderer_url=url):
        assert thumbs.available() is True
        thumbs.render_in_background(db, page.id, page.version, PAGE)
    return store.render_report(db, page.id)


def test_a_valid_answer_is_stored_as_any_render_is(db, page, stub_renderer):
    stub_renderer.answer = valid
    assert render(db, page, stub_renderer.url) == {
        "rendered": True,
        "errors": [],
        "verdict": "ok",
        "findings": [],
    }
    assert store.live_thumbnail(db, page.id) == png(thumbs.WIDTH, thumbs.HEIGHT)
    [(path, headers, body)] = stub_renderer.requests
    assert path == f"/v1/render?w={thumbs.WIDTH}&h={thumbs.HEIGHT}&check=1"
    assert headers["X-Ream-Render"] == "1"
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert int(headers["Content-Length"]) == len(body)
    # Sent stamped, as a reader would see it.
    assert b"<!--artifacts:updated-->" not in body and b"DECISION" in body


def test_script_errors_and_the_check_come_back_through_the_findings(
    db, page, stub_renderer
):
    def broken(handler):
        _send(
            handler,
            200,
            answer_json(png(*_size(handler)), ["line 3: Uncaught Error: boom"]),
        )

    stub_renderer.answer = broken
    got = render(db, page, stub_renderer.url)
    assert got["verdict"] == "broken"
    assert got["errors"] == ["line 3: Uncaught Error: boom"]
    assert [f["kind"] for f in got["findings"]] == ["script_error"]


def test_a_closed_port_is_unchecked_with_its_finding(db, page):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert render(db, page, f"http://127.0.0.1:{port}") == UNAVAILABLE
    assert store.live_thumbnail(db, page.id) is None


def test_a_renderer_slower_than_the_deadline_is_unchecked(
    db, page, stub_renderer, monkeypatch
):
    monkeypatch.setattr(renderwire, "CALL_DEADLINE_SECONDS", 0.5)

    def slow(handler):
        time.sleep(3)
        valid(handler)

    stub_renderer.answer = slow
    started = time.monotonic()
    assert render(db, page, stub_renderer.url) == UNAVAILABLE
    assert time.monotonic() - started < 2.5


def test_an_answer_dripped_past_the_deadline_is_unchecked(
    db, page, stub_renderer, monkeypatch
):
    # A per-read timeout alone would reset on every byte: the deadline is the call's.
    monkeypatch.setattr(renderwire, "CALL_DEADLINE_SECONDS", 0.5)

    def drip(handler):
        body = answer_json(png(*_size(handler)))
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        # One byte at a time, for longer than the deadline (until the app goes).
        for i in range(min(len(body), 50)):
            handler.wfile.write(body[i : i + 1])
            handler.wfile.flush()
            time.sleep(0.1)

    stub_renderer.answer = drip
    started = time.monotonic()
    assert render(db, page, stub_renderer.url) == UNAVAILABLE
    assert time.monotonic() - started < 2.5


def test_an_answer_declared_over_the_limit_is_unchecked(db, page, stub_renderer):
    def declared(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", str(renderwire.RESPONSE_MAX_BYTES + 1))
        handler.end_headers()
        handler.wfile.write(b"{}")

    stub_renderer.answer = declared
    assert render(db, page, stub_renderer.url) == UNAVAILABLE


def test_an_answer_over_the_limit_without_a_length_is_unchecked(
    db, page, stub_renderer
):
    def oversize(handler):
        handler.send_response(200)  # HTTP/1.0: the body ends when the line closes
        handler.end_headers()
        handler.wfile.write(b" " * (renderwire.RESPONSE_MAX_BYTES + 1))

    stub_renderer.answer = oversize
    assert render(db, page, stub_renderer.url) == UNAVAILABLE


def test_a_picture_of_the_wrong_size_is_unchecked(db, page, stub_renderer):
    stub_renderer.answer = lambda h: _send(h, 200, answer_json(png(100, 100)))
    assert render(db, page, stub_renderer.url) == UNAVAILABLE
    assert store.live_thumbnail(db, page.id) is None


def test_an_answer_that_is_not_json_is_unchecked(db, page, stub_renderer):
    stub_renderer.answer = lambda h: _send(h, 200, b"{not json")
    assert render(db, page, stub_renderer.url) == UNAVAILABLE


def test_a_redirect_is_not_followed_and_is_unchecked(db, page, stub_renderer):
    def redirect(handler):
        if handler.path.startswith("/elsewhere"):
            valid(handler)
            return
        _send(handler, 302, b"", Location=f"{stub_renderer.url}/elsewhere")

    stub_renderer.answer = redirect
    assert render(db, page, stub_renderer.url) == UNAVAILABLE
    assert len(stub_renderer.requests) == 1


@pytest.fixture()
def slept(monkeypatch):
    waits = []
    monkeypatch.setattr(renderwire, "_sleep", waits.append)
    return waits


def test_a_busy_renderer_is_asked_once_more_after_its_retry_after(
    db, page, stub_renderer, slept
):
    answers = iter([lambda h: _send(h, 503, b"busy\n", Retry_After="5"), valid, valid])
    stub_renderer.answer = lambda h: next(answers)(h)
    assert render(db, page, stub_renderer.url) == {
        "rendered": True,
        "errors": [],
        "verdict": "ok",
        "findings": [],
    }
    assert store.live_thumbnail(db, page.id) == png(900, 640)
    assert slept == [5]
    assert len(stub_renderer.requests) == 2


def test_a_renderer_busy_twice_is_unchecked(db, page, stub_renderer, slept):
    stub_renderer.answer = lambda h: _send(h, 503, b"busy\n", Retry_After="1")
    assert render(db, page, stub_renderer.url) == UNAVAILABLE
    assert slept == [1]
    assert len(stub_renderer.requests) == 2


@pytest.mark.parametrize("after", [None, "soon", "-1", "1.5", "11", "9999"])
def test_a_busy_renderer_without_a_usable_retry_after_is_not_asked_again(
    db, page, stub_renderer, slept, after
):
    headers = {} if after is None else {"Retry_After": after}
    stub_renderer.answer = lambda h: _send(h, 503, b"busy\n", **headers)
    assert render(db, page, stub_renderer.url) == UNAVAILABLE
    assert slept == []
    assert len(stub_renderer.requests) == 1


def test_a_retry_that_would_pass_the_deadline_is_not_made(
    db, page, stub_renderer, slept, monkeypatch
):
    monkeypatch.setattr(renderwire, "CALL_DEADLINE_SECONDS", 4.0)
    stub_renderer.answer = lambda h: _send(h, 503, b"busy\n", Retry_After="5")
    assert render(db, page, stub_renderer.url) == UNAVAILABLE
    assert slept == []
    assert len(stub_renderer.requests) == 1


def test_the_retry_really_waits_within_the_deadline(stub_renderer):
    answers = iter([lambda h: _send(h, 503, b"busy\n", Retry_After="1"), valid])
    stub_renderer.answer = lambda h: next(answers)(h)
    started = time.monotonic()
    report = renderwire.call(stub_renderer.url, PAGE, 900, 640, check=True)
    assert report[0] == png(900, 640)
    assert 1.0 <= time.monotonic() - started < renderwire.CALL_DEADLINE_SECONDS


def test_a_renderer_that_did_not_render_is_unchecked_and_says_so(
    db, page, stub_renderer
):
    not_run = json.dumps(
        {"v": 1, "outcome": "not_run", "png_base64": None, "errors": [], "check": None}
    ).encode()
    stub_renderer.answer = lambda h: _send(h, 200, not_run)
    assert render(db, page, stub_renderer.url) == {
        **UNAVAILABLE,
        "findings": [{"kind": "unchecked", "detail": renderfind.NOT_RUN_NOTE}],
    }
    assert store.live_thumbnail(db, page.id) is None


def test_results_that_cannot_be_stored_are_stored_as_unchecked(
    db, page, stub_renderer, monkeypatch
):
    stub_renderer.answer = valid
    real = store.save_render_errors
    calls = []

    def first_fails(db, *args, **kw):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("the write failed")
        return real(db, *args, **kw)

    monkeypatch.setattr(store, "save_render_errors", first_fails)
    got = render(db, page, stub_renderer.url)
    assert got["verdict"] == "unchecked"
    assert got["findings"] == [
        {"kind": "unchecked", "detail": renderfind.NOT_STORED_NOTE}
    ]
    assert store.live_thumbnail(db, page.id) == png(900, 640)


def test_no_picture_is_kept_when_no_results_can_be_stored(
    db, page, stub_renderer, monkeypatch
):
    stub_renderer.answer = valid

    def fails(*args, **kw):
        raise RuntimeError("the write failed")

    monkeypatch.setattr(store, "save_render_errors", fails)
    got = render(db, page, stub_renderer.url)
    assert got["rendered"] is False
    assert store.live_thumbnail(db, page.id) is None


def test_an_unusable_answer_counts_as_a_failed_render_for_the_retries(
    db, page, stub_renderer
):
    stub_renderer.answer = lambda h: _send(h, 503, b"busy\n")
    render(db, page, stub_renderer.url)
    assert thumbs._failures[(page.id, page.version)][0] == 1


def test_no_renderer_and_no_browser_stores_nothing(db, page, monkeypatch):
    # Kept apart from an unavailable renderer: here nothing rendered at all.
    monkeypatch.setenv("REAM_BROWSER", "/nonexistent/browser")
    assert thumbs.available() is False
    thumbs.render_in_background(db, page.id, page.version, PAGE)
    assert store.render_report(db, page.id)["rendered"] is False


def test_the_error_is_one_fixed_phrase_and_never_the_answer(stub_renderer):
    stub_renderer.answer = lambda h: _send(h, 500, b"zebra-marker in the answer")
    with pytest.raises(renderwire.RenderUnavailable) as caught:
        renderwire.call(stub_renderer.url, PAGE, 900, 640, check=True)
    assert str(caught.value) == renderwire.UNAVAILABLE
    assert caught.value.__cause__ is None
    assert "zebra" not in repr(caught.value)
    stub_renderer.answer = lambda h: _send(h, 200, b'{"zebra-marker": 1}')
    with pytest.raises(renderwire.RenderUnavailable) as caught:
        renderwire.call(stub_renderer.url, PAGE, 900, 640, check=True)
    assert "zebra" not in str(caught.value)


def test_the_check_flag_is_sent_as_asked(stub_renderer):
    stub_renderer.answer = valid
    report = renderwire.call(stub_renderer.url, PAGE, 400, 800, check=False)
    assert report[0] == png(400, 800) and report.check == CHECK
    assert stub_renderer.requests[0][0] == "/v1/render?w=400&h=800&check=0"
