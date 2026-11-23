"""`GET /api/v1/pages/{id}/screenshot`: a picture of a page the caller can read, on demand
(runcove-uap8t, slice 3; widened from own pages on 23 Nov 2026). Runs on both databases (the `db` fixture).

The browser and the renderer are fakes here: `rendercore.shoot` is replaced, or a stub
renderer answers `/v1/shot`. What the real browser makes of a page is held by
`tests/test_shots.py` and `tests/test_shots_offline.py`.
"""

import json
import struct
import zlib
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from ream import image_store, keys, rendercore, renderwire, settings, shots, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body>one</body></html>"


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


def a_shot(width=1280, height=900, **kw) -> renderwire.Shot:
    return renderwire.Shot(
        "shot",
        png(width, height),
        width=width,
        height=height,
        page_height=kw.pop("page_height", height),
        truncated=kw.pop("truncated", False),
    )


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def seat(db):
    token, _ = keys.mint(db, agent="seat")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other(db):
    token, _ = keys.mint(db, agent="other")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def page(db, seat):
    first = store.publish(db, "seat", "Board", PAGE)
    store.patch(db, "seat", first.id, PAGE.replace("one", "two"), first.revision)
    return first.id


@pytest.fixture()
def browser(monkeypatch, tmp_path):
    """A browser here (a file at REAM_BROWSER), and `rendercore.shoot` faked: it
    records what it was asked and answers `browser.answer`."""
    binary = tmp_path / "chromium"
    binary.write_text("")

    class Fake:
        answer = a_shot()
        calls: ClassVar[list] = []

    def fake(path, html, request, **kw):
        Fake.calls.append((path, html, request))
        return Fake.answer

    monkeypatch.setattr(rendercore, "shoot", fake)
    with settings.override(browser=str(binary), renderer_url=""):
        yield Fake


def get(api, page_id, headers, word="pages", **params):
    return api.get(
        f"/api/v1/{word}/{page_id}/screenshot", params=params, headers=headers
    )


# --- the picture -----------------------------------------------------------------


@pytest.mark.parametrize("word", ["pages", "artifacts"])
def test_a_shot_of_your_own_page_is_a_png_with_its_facts(
    api, seat, page, browser, word
):
    browser.answer = a_shot(1280, 900, page_height=12000, truncated=True)
    got = get(api, page, seat, word=word)
    assert got.status_code == 200
    assert got.content == png(1280, 900)
    assert got.headers["content-type"] == "image/png"
    for name, value in image_store.SERVE_HEADERS.items():
        assert got.headers[name] == value
    assert got.headers["content-disposition"] == "attachment"
    assert got.headers["cache-control"] == "private, no-store"
    for prefix in ("x-ream-shot-", "x-artifacts-shot-"):
        assert got.headers[prefix + "version"] == "2"
        assert got.headers[prefix + "width"] == "1280"
        assert got.headers[prefix + "height"] == "900"
        assert got.headers[prefix + "truncated"] == "true"
        assert got.headers[prefix + "page-height"] == "12000"
    _, html, request = browser.calls[-1]
    assert "two" in html
    assert request == renderwire.shot_request(1280, "light")


def test_the_parameters_reach_the_browser(api, seat, page, browser):
    browser.answer = a_shot(390, 300)
    got = get(
        api, page, seat, version="1", width="390", theme="dark", section=" Intro  Part "
    )
    assert got.status_code == 200
    assert got.headers["x-ream-shot-version"] == "1"
    _, html, request = browser.calls[-1]
    assert "one" in html and "two" not in html
    assert request == renderwire.shot_request(390, "dark", section="intro part")
    get(api, page, seat, element="results")
    assert browser.calls[-1][2] == renderwire.shot_request(
        1280, "light", element="results"
    )


def test_the_page_is_stamped_as_a_reader_sees_it(api, seat, db, browser):
    from ream import stamp

    made = store.publish(db, "seat", "Stamped", PAGE.replace("one", stamp.MARKER))
    assert get(api, made.id, seat).status_code == 200
    assert stamp.MARKER not in browser.calls[-1][1]


def test_a_key_is_needed(api, page, browser):
    assert get(api, page, {}).status_code == 401
    assert browser.calls == []


# --- scope: any page the caller can read -------------------------------------------


def test_another_agents_open_page_is_shot_as_the_reader_it_is(
    api, other, page, browser
):
    """The page is open to agents (other may read its body), so other may shoot it."""
    assert api.get(f"/api/v1/pages/{page}/body", headers=other).status_code == 200
    got = get(api, page, other)
    assert got.status_code == 200
    assert got.content == png(1280, 900)
    assert got.headers["cache-control"] == "private, no-store"
    assert got.headers["x-ream-shot-version"] == "2"
    assert "two" in browser.calls[-1][1]


def test_another_agents_older_version_is_shot(api, other, page, browser):
    got = get(api, page, other, version="1")
    assert got.status_code == 200
    assert got.headers["x-ream-shot-version"] == "1"
    html = browser.calls[-1][1]
    assert "one" in html and "two" not in html


def test_a_closed_page_answers_exactly_as_an_unknown_id(
    api, seat, other, db, page, browser
):
    store.set_agents_can_read(db, page, False, agent="seat")
    assert api.get(f"/api/v1/pages/{page}/body", headers=other).status_code == 404
    theirs = get(api, page, other)
    missing = get(api, "abcdefghijklmnopqrstuvwxyz", other)
    assert theirs.status_code == missing.status_code == 404
    assert theirs.content == missing.content == b'{"detail":"no such page"}'
    # Its own agent still shoots it: closed means closed to OTHER agents.
    assert get(api, page, seat).status_code == 200
    assert len(browser.calls) == 1


def test_a_version_of_a_closed_page_answers_as_a_version_not_there(
    api, seat, other, db, page, browser
):
    store.set_agents_can_read(db, page, False, agent="seat")
    closed = get(api, page, other, version="1")
    beyond = get(api, page, seat, version="9")
    missing = get(api, "abcdefghijklmnopqrstuvwxyz", seat, version="1")
    for got in (closed, beyond, missing):
        assert got.status_code == 404
        assert got.content == b'{"detail":"no such page or version"}'
    assert browser.calls == []


def test_a_deleted_page_is_no_such_page(api, seat, db, page, browser):
    store.set_deleted(db, page, True, agent="seat")
    got = get(api, page, seat)
    assert got.status_code == 404 and got.json() == {"detail": "no such page"}
    assert browser.calls == []


# --- refused parameters, before anything is read ----------------------------------


@pytest.mark.parametrize(
    "params, field",
    [
        ({"width": "800"}, "width"),
        ({"width": "wide"}, "width"),
        ({"width": ""}, "width"),
        ({"theme": "sepia"}, "theme"),
        ({"element": "a", "section": "b"}, "element"),
        ({"element": "no spaces"}, "element"),
        ({"element": ""}, "element"),
        ({"section": "   "}, "section"),
        ({"section": "x" * 201}, "section"),
        ({"version": "0"}, "version"),
        ({"version": "-1"}, "version"),
        ({"version": "two"}, "version"),
    ],
)
def test_a_bad_parameter_is_a_field_error_for_any_page(
    api, seat, other, page, browser, params, field
):
    mine = get(api, page, seat, **params)
    theirs = get(api, page, other, **params)
    assert mine.status_code == theirs.status_code == 422
    assert mine.json() == theirs.json()
    assert mine.json()["detail"]["field"] == field
    assert set(mine.json()["detail"]) == {"field", "detail"}
    assert browser.calls == []


def test_an_id_no_page_can_have_is_a_field_error(api, seat, browser):
    got = get(api, "not.an.id", seat)
    assert got.status_code == 422
    assert got.json() == {"detail": {"field": "id", "detail": "id is not a page id"}}


# --- what the browser made of it ----------------------------------------------------


@pytest.mark.parametrize(
    "shot, params, status, detail",
    [
        (
            renderwire.Shot("no_match", matches=0),
            {"element": "results"},
            422,
            {
                "field": "element",
                "detail": "no part of the page is that element",
                "matches": 0,
            },
        ),
        (
            renderwire.Shot("ambiguous", matches=3),
            {"section": "Results"},
            422,
            {
                "field": "section",
                "detail": "more than one part of the page is that section",
                "matches": 3,
            },
        ),
        (
            renderwire.Shot("empty"),
            {"element": "results"},
            422,
            {"field": "element", "detail": "the element is drawn with no size"},
        ),
        (
            renderwire.Shot("empty"),
            {},
            422,
            {"field": "page", "detail": "the page is drawn with no size"},
        ),
        (
            renderwire.Shot("timeout"),
            {},
            422,
            {
                "field": "page",
                "reason": "timeout",
                "detail": "the page took too long to render",
            },
        ),
        (
            renderwire.Shot("memory"),
            {},
            422,
            {
                "field": "page",
                "reason": "memory",
                "detail": "the page used more memory than a render may",
            },
        ),
        (
            renderwire.Shot("picture_too_big"),
            {},
            413,
            {
                "field": "picture",
                "limit": renderwire.SHOT_PNG_MAX_BYTES,
                "detail": "the picture is too large: name an element or a section",
            },
        ),
        (
            renderwire.Shot("no_browser"),
            {},
            501,
            {"renderer": False, "detail": shots.NO_BROWSER},
        ),
        (renderwire.Shot("failed"), {}, 500, shots.FAILED),
    ],
    ids=[
        "no-match",
        "ambiguous",
        "empty-element",
        "empty-page",
        "timeout",
        "memory",
        "picture-too-big",
        "no-browser",
        "failed",
    ],
)
def test_every_outcome_has_its_answer(
    api, seat, page, browser, shot, params, status, detail
):
    browser.answer = shot
    got = get(api, page, seat, **params)
    assert got.status_code == status
    assert got.json() == {"detail": detail}


def test_a_busy_browser_is_503_with_retry_after(api, seat, page, browser):
    browser.answer = renderwire.Shot("busy")
    got = get(api, page, seat)
    assert got.status_code == 503
    assert got.headers["retry-after"] == str(rendercore.SHOT_SLOT_WAIT_SECONDS)
    assert got.json() == {"detail": shots.BUSY}


def test_a_page_too_big_to_render_is_413_and_no_browser_starts(
    api, seat, page, browser, monkeypatch
):
    monkeypatch.setattr(rendercore, "RENDER_MAX_BYTES", 10)
    got = get(api, page, seat)
    assert got.status_code == 413
    assert got.json() == {
        "detail": {
            "field": "page",
            "size": len(PAGE.replace("one", "two").encode()),
            "limit": 10,
            "detail": "the page is too large to render",
        }
    }
    assert browser.calls == []


def test_one_shot_in_flight_per_agent(api, seat, other, page, browser, db):
    theirs = store.publish(db, "other", "Theirs", PAGE)
    with shots.one_per_agent("seat"):
        # The turn is the caller's, not the page's: seat's turn does not stop another
        # agent shooting seat's page, and seat shooting theirs is held like any shot.
        assert get(api, page, other).status_code == 200
        assert get(api, theirs.id, seat).status_code == 429
        got = get(api, page, seat)
        assert got.status_code == 429
        assert got.headers["retry-after"] == str(shots.IN_FLIGHT_RETRY_AFTER_SECONDS)
        assert got.json() == {"detail": shots.IN_FLIGHT}
        assert get(api, theirs.id, other).status_code == 200, (
            "another agent is not held"
        )
    assert get(api, page, seat).status_code == 200, "the turn is given back"


def test_the_turn_is_given_back_when_the_shot_fails(api, seat, page, browser):
    browser.answer = renderwire.Shot("timeout")
    assert get(api, page, seat).status_code == 422
    browser.answer = a_shot()
    assert get(api, page, seat).status_code == 200


# --- no browser and no renderer: 501, never a 500 -----------------------------------


def test_no_browser_and_no_renderer_is_a_clean_501(api, seat, page, monkeypatch):
    """How production runs today: REAM_BROWSER names a browser that is not there."""
    monkeypatch.setenv("REAM_BROWSER", "/nonexistent/browser")
    monkeypatch.delenv("REAM_RENDERER_URL", raising=False)
    monkeypatch.delenv("ARTIFACTS_RENDERER_URL", raising=False)
    monkeypatch.delenv("ARTIFACTS_BROWSER", raising=False)
    got = get(api, page, seat)
    assert got.status_code == 501
    assert got.json() == {"detail": {"renderer": False, "detail": shots.NO_BROWSER}}


def test_no_browser_still_says_no_such_page_first_for_a_closed_page(
    api, other, db, page, monkeypatch
):
    store.set_agents_can_read(db, page, False, agent="seat")
    monkeypatch.setenv("REAM_BROWSER", "/nonexistent/browser")
    monkeypatch.delenv("REAM_RENDERER_URL", raising=False)
    monkeypatch.delenv("ARTIFACTS_RENDERER_URL", raising=False)
    monkeypatch.delenv("ARTIFACTS_BROWSER", raising=False)
    assert get(api, page, other).status_code == 404


# --- with a renderer as its own process (REAM_RENDERER_URL) -------------------------


def _reply(handler, status, body, **headers):
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(body)))
    for name, value in headers.items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


@pytest.fixture()
def remote(stub_renderer, monkeypatch):
    """A renderer set, and a browser here (REAM_BROWSER) that must never start."""
    started = []
    monkeypatch.setattr(rendercore, "shoot", lambda *a, **kw: started.append(a))
    stub_renderer.answer = lambda h: _reply(
        h, 200, renderwire.encode_shot_answer(a_shot(390, 200, page_height=700))
    )
    with settings.override(renderer_url=stub_renderer.url, browser="/bin/true"):
        yield stub_renderer
    assert started == [], "a browser started in this process"


def test_a_remote_shot_is_the_renderers_picture(api, seat, page, remote):
    got = get(api, page, seat, width="390", theme="dark", element="results")
    assert got.status_code == 200
    assert got.content == png(390, 200)
    assert got.headers["x-ream-shot-page-height"] == "700"
    path, _headers, body = remote.requests[0]
    assert path == "/v1/shot?w=390&theme=dark&element=results"
    assert body == PAGE.replace("one", "two").encode()


def test_a_remote_outcome_has_the_same_answer(api, seat, page, remote):
    remote.answer = lambda h: _reply(
        h, 200, renderwire.encode_shot_answer(renderwire.Shot("ambiguous", matches=2))
    )
    got = get(api, page, seat, element="results")
    assert got.status_code == 422
    assert got.json()["detail"]["matches"] == 2


def test_a_busy_renderer_is_503_with_retry_after(api, seat, page, remote):
    remote.answer = lambda h: _reply(h, 503, b"busy\n", **{"Retry-After": "1"})
    got = get(api, page, seat)
    assert got.status_code == 503
    assert got.headers["retry-after"] == str(rendercore.SHOT_SLOT_WAIT_SECONDS)


@pytest.mark.parametrize(
    "answer",
    [
        lambda h: _reply(h, 200, b"{not json"),
        lambda h: _reply(h, 302, b""),
        lambda h: _reply(h, 500, b"oops"),
        lambda h: _reply(
            h,
            200,
            json.dumps(
                {
                    "v": 1,
                    "outcome": "shot",
                    "png_base64": None,
                    "width": 1,
                    "height": 1,
                    "page_height": 1,
                    "truncated": False,
                    "matches": None,
                }
            ).encode(),
        ),
    ],
    ids=["not-json", "redirect", "error", "shot-without-picture"],
)
def test_a_renderer_with_no_usable_answer_is_503_never_a_local_shot(
    api, seat, page, remote, answer
):
    remote.answer = answer
    got = get(api, page, seat)
    assert got.status_code == 503
    assert got.json() == {
        "detail": {
            "renderer": True,
            "reason": renderwire.UNAVAILABLE,
            "detail": shots.UNAVAILABLE,
        }
    }


def test_a_renderer_with_no_browser_here_still_takes_the_shot(
    api, seat, page, stub_renderer, monkeypatch
):
    """REAM_RENDERER_URL set: REAM_BROWSER is not read, so a missing one is no 501."""
    stub_renderer.answer = lambda h: _reply(
        h, 200, renderwire.encode_shot_answer(a_shot())
    )
    with settings.override(
        renderer_url=stub_renderer.url, browser="/nonexistent/browser"
    ):
        assert get(api, page, seat).status_code == 200


def test_the_route_is_in_the_served_schema(api, seat):
    paths = api.get("/api/v1/openapi.json", headers=seat).json()["paths"]
    assert "get" in paths["/api/v1/pages/{artifact_id}/screenshot"]
    assert "get" in paths["/api/v1/artifacts/{artifact_id}/screenshot"]


def test_a_renderer_with_no_browser_is_unavailable_not_this_server(
    api, seat, page, remote
):
    """The renderer's own `no_browser` is the renderer being unusable: never the 501
    that says this server has no browser."""
    remote.answer = lambda h: _reply(
        h, 200, renderwire.encode_shot_answer(renderwire.Shot("no_browser"))
    )
    got = get(api, page, seat)
    assert got.status_code == 503
    assert "retry-after" not in got.headers
    assert got.json() == {
        "detail": {
            "renderer": True,
            "reason": renderwire.UNAVAILABLE,
            "detail": shots.UNAVAILABLE,
        }
    }


# --- an unexpected error gives the agent's turn back ---------------------------------


@pytest.fixture()
def lenient(db):
    """The app answering a 500 for an error it did not expect, as it does when served."""
    return TestClient(create_gallery_app(db), raise_server_exceptions=False)


def test_an_error_in_the_browser_gives_the_turn_back(
    lenient, seat, page, browser, monkeypatch
):
    failures = [RuntimeError("the browser broke")]

    def fails_once(path, html, request, **kw):
        if failures:
            raise failures.pop()
        return a_shot()

    monkeypatch.setattr(rendercore, "shoot", fails_once)
    assert get(lenient, page, seat).status_code == 500
    assert get(lenient, page, seat).status_code == 200, "not 429: the turn came back"


def test_an_error_in_the_renderer_call_gives_the_turn_back(
    lenient, seat, page, monkeypatch
):
    failures = [RuntimeError("the call broke")]

    def fails_once(url, html, request):
        if failures:
            raise failures.pop()
        return a_shot()

    started = []
    monkeypatch.setattr(renderwire, "call_shot", fails_once)
    monkeypatch.setattr(rendercore, "shoot", lambda *a, **kw: started.append(a))
    with settings.override(renderer_url="http://127.0.0.1:9", browser="/bin/true"):
        assert get(lenient, page, seat).status_code == 500
        assert get(lenient, page, seat).status_code == 200, (
            "not 429: the turn came back"
        )
    assert started == []
