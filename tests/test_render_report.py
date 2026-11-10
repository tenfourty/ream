"""A page's script errors, from the service's own render, reported to its
author (homelab-ij8.72)."""

import pytest
from fastapi.testclient import TestClient

from ream import api as api_module
from ream import client, keys, store, thumbs
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>t</title></head><body><p>x</p></body></html>"

LOG = (
    '[9:9:0924/120000.1:INFO:CONSOLE(12)] "Uncaught ReferenceError: chart is not defined", '
    "source: file:///t/page.html (12)\n"
    '[9:9:0924/120000.2:INFO:CONSOLE(3)] "hello from the page", source: file:///t/page.html (3)\n'
    '[9:9:0924/120000.3:INFO:CONSOLE(40)] "TypeError: x.map is not a function", '
    "source: file:///t/page.html (40)\n"
)


# Captured from the real browser in the pod (chromium 153, 24 Sep 2026), not
# tidied: the first pattern was proved only against the older shape below and
# found nothing in real output.
REAL = (
    '[382:382:0924/111411.633292:INFO:CONSOLE:1] "E-TEST", source: file:///tmp/p.html (1)\n'
    '[382:382:0924/111411.634413:INFO:CONSOLE:1] "Uncaught ReferenceError: chart is not '
    'defined", source: file:///tmp/p.html (1)\n'
)


def test_the_real_browsers_log_shape_is_read():
    assert thumbs.console_errors(REAL) == [
        "line 1: Uncaught ReferenceError: chart is not defined"
    ]


def test_only_errors_are_taken_from_the_browser_log():
    assert thumbs.console_errors(LOG) == [
        "line 12: Uncaught ReferenceError: chart is not defined",
        "line 40: TypeError: x.map is not a function",
    ]
    assert thumbs.console_errors("") == []


def test_the_list_is_capped():
    many = "".join(
        f'[1:1:0/0:INFO:CONSOLE({n})] "Uncaught Error: {n}", source: file:///p (1)\n'
        for n in range(50)
    )
    assert len(thumbs.console_errors(many)) == 10


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db))


def test_the_route_says_rendered_and_what_threw(db, api, monkeypatch):
    token, _ = keys.mint(db, agent="seat")
    auth = {"Authorization": f"Bearer {token}"}
    page = store.publish(db, "seat", "Board", PAGE)
    monkeypatch.setattr(api_module.thumbs, "available", lambda: True)
    before = api.get(f"/api/v1/artifacts/{page.id}/render", headers=auth).json()
    assert before == {
        "rendered": False,
        "errors": [],
        "renderer": True,
        "verdict": "unchecked",
        "findings": [],
    }
    store.save_render_errors(
        db, page.id, 1, ["line 12: Uncaught ReferenceError: chart"]
    )
    after = api.get(f"/api/v1/artifacts/{page.id}/render", headers=auth).json()
    assert after["rendered"] is True and after["errors"] == [
        "line 12: Uncaught ReferenceError: chart"
    ]


def test_the_tool_warns_loudly_and_claims_nothing_when_unrendered(monkeypatch, capsys):
    answers = iter(
        [
            {"rendered": False, "errors": [], "renderer": True},
            {
                "rendered": True,
                "errors": ["line 12: Uncaught ReferenceError: chart"],
                "renderer": True,
            },
        ]
    )
    monkeypatch.setattr(client, "_request", lambda *a, **k: next(answers))
    monkeypatch.setattr(client.time, "sleep", lambda _s: None)
    client._report_render("abc", "k")
    err = capsys.readouterr().err
    assert "WARNING" in err and "Uncaught ReferenceError" in err

    monkeypatch.setattr(client, "_request", lambda *a, **k: {"renderer": False})
    client._report_render("abc", "k")
    assert capsys.readouterr().err == "", "no renderer here: nothing claimed, no wait"
