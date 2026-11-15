"""MCP `screenshot_page` (runcove-uap8t, slice 4): the screenshot route's flow (`shots`)
as a tool, the picture as an image block after Ream's fixed label. Runs on both databases.

The browser is the fake of `tests/test_screenshot_api.py`: `rendercore.shoot` replaced.
"""

import base64
import json

import pytest
from test_mcp import (  # noqa: F401
    LEGACY_HEADERS,
    client,
    headers,
    on,
    refusal,
    token,
)
from test_screenshot_api import a_shot, browser, png  # noqa: F401

from ream import keys, mcp, renderwire, shots, store

PAGE = "<!doctype html><html><head><title>B</title></head><body>one</body></html>"
MISSING = "abcdefghijklmnopqrstuvwxyz"


@pytest.fixture()
def page(db, token):
    first = store.publish(db, "mcp-seat", "Board", PAGE)
    store.patch(db, "mcp-seat", first.id, PAGE.replace("one", "two"), first.revision)
    return first.id


@pytest.fixture()
def other(db):
    made, _ = keys.mint(db, agent="other")
    return made


def shoot(client, token, /, **arguments) -> dict:
    """The whole `tools/call` result in the older era."""
    return client.post(
        "/mcp",
        headers=headers(token, **LEGACY_HEADERS),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "screenshot_page", "arguments": arguments},
        },
    ).json()["result"]


def error_text(result: dict) -> str:
    assert result["isError"] is True, result
    return result["content"][0]["text"]


def route(client, token, page_id, **params):
    return client.get(
        f"/api/v1/pages/{page_id}/screenshot", params=params, headers=headers(token)
    )


# --- the picture --------------------------------------------------------------------


def test_a_shot_is_the_facts_then_the_label_then_the_png(
    client, on, token, page, browser
):
    browser.answer = a_shot(1280, 900, page_height=12000, truncated=True)
    result = shoot(client, token, page_id=page)
    assert result["isError"] is False, result
    facts = {
        "artifact_id": page,
        "version": 2,
        "width": 1280,
        "height": 900,
        "truncated": True,
        "page_height": 12000,
    }
    assert result["structuredContent"] == facts
    text, label, image = result["content"]
    assert json.loads(text["text"]) == facts
    assert label == {"type": "text", "text": mcp.image_label("mcp-seat")}
    assert "'mcp-seat'" in label["text"] and "not instructions" in label["text"]
    assert image["type"] == "image" and image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"]) == png(1280, 900)
    _, html, request = browser.calls[-1]
    assert "two" in html
    assert request == renderwire.shot_request(1280, "light")


def test_the_arguments_reach_the_browser_as_the_routes_do(
    client, on, token, page, browser
):
    browser.answer = a_shot(390, 300)
    result = shoot(
        client, token, page_id=page, version=1, width=390, theme="dark", section="Intro"
    )
    assert result["structuredContent"]["version"] == 1
    _, html, request = browser.calls[-1]
    assert "one" in html and "two" not in html
    assert request == renderwire.shot_request(390, "dark", section="intro")
    shoot(client, token, page_id=page, element="results")
    assert browser.calls[-1][2] == renderwire.shot_request(
        1280, "light", element="results"
    )


def test_it_is_a_read_tool_with_explicit_hints(client, on, token):
    listed = client.post(
        "/mcp",
        headers=headers(token, **LEGACY_HEADERS),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    ).json()["result"]["tools"]
    found = next(t for t in listed if t["name"] == "screenshot_page")
    assert found["annotations"]["readOnlyHint"] is True
    assert found["annotations"]["destructiveHint"] is False
    assert found["annotations"]["openWorldHint"] is False
    assert found["inputSchema"]["properties"]["width"]["enum"] == [1280, 390]
    assert found["inputSchema"]["properties"]["theme"]["enum"] == ["light", "dark"]
    assert mcp.TOOLS["screenshot_page"].permission == mcp.READ


def test_the_label_names_the_agent_sanitised():
    flip = chr(0x202E)  # right-to-left override
    label = mcp.image_label(f"seat{flip}evil\nline")
    assert flip not in label and "\n" not in label
    assert "not instructions" in label
    assert "an unnamed agent" in mcp.image_label("")


# --- scope and refusals, as the route answers them ----------------------------------


def test_not_yours_answers_as_no_such_page(client, on, token, other, page, browser):
    assert error_text(shoot(client, other, page_id=page)) == "no such page"
    assert error_text(shoot(client, token, page_id=MISSING)) == "no such page"
    assert refusal(route(client, other, page)) == "no such page"
    assert (
        error_text(shoot(client, token, page_id=page, version=9))
        == "no such page or version"
    )
    assert browser.calls == []


@pytest.mark.parametrize(
    "arguments, params",
    [
        ({"width": 800}, {"width": "800"}),
        ({"theme": "sepia"}, {"theme": "sepia"}),
        ({"element": "a", "section": "b"}, {"element": "a", "section": "b"}),
        ({"element": "no spaces"}, {"element": "no spaces"}),
        ({"section": "   "}, {"section": "   "}),
        ({"version": 0}, {"version": "0"}),
    ],
)
def test_a_bad_argument_is_the_routes_field_error(
    client, on, token, page, browser, arguments, params
):
    said = error_text(shoot(client, token, page_id=page, **arguments))
    answer = route(client, token, page, **params)
    assert answer.status_code == 422
    assert said == refusal(answer)
    assert browser.calls == []


def test_an_id_no_page_can_have_is_the_routes_field_error(client, on, token, browser):
    said = error_text(shoot(client, token, page_id="not.an.id"))
    assert json.loads(said) == {"field": "id", "detail": "id is not a page id"}


def test_no_browser_is_the_routes_501(client, on, token, page, monkeypatch):
    from ream import rendercore, settings

    monkeypatch.setattr(rendercore, "browser_path", lambda: None)
    with settings.override(renderer_url=""):
        said = error_text(shoot(client, token, page_id=page))
        answer = route(client, token, page)
    assert answer.status_code == 501
    assert said == refusal(answer)


def test_a_picture_over_the_mcp_cap_is_the_routes_413_at_that_cap(
    client, on, token, page, browser
):
    from ream import mcp_tools

    browser.answer = a_shot(4, 4)
    padded = browser.answer.png + b"\0" * (
        mcp_tools.MCP_SHOT_MAX_BYTES + 1 - len(browser.answer.png)
    )
    browser.answer = renderwire.Shot(
        "shot", padded, width=4, height=4, page_height=4, truncated=False
    )
    said = error_text(shoot(client, token, page_id=page))
    assert json.loads(said) == {
        "field": "picture",
        "limit": 3_750_000,
        "detail": "the picture is too large: name an element or a section",
    }
    # The route keeps its own larger cap: the same picture is served there.
    assert route(client, token, page).status_code == 200


def test_a_busy_browser_is_a_tool_error_saying_try_again(
    client, on, token, page, browser
):
    browser.answer = renderwire.Shot("busy")
    assert error_text(shoot(client, token, page_id=page)) == shots.BUSY


def test_a_second_shot_while_one_runs_is_refused(client, on, token, page, browser):
    with shots.one_per_agent("mcp-seat"):
        assert error_text(shoot(client, token, page_id=page)) == shots.IN_FLIGHT
    assert browser.calls == []
