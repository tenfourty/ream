"""What the service's own render found wrong with a page: the check script's
line, the static missing-file check, the verdict, and where they are kept and
read back (runcove-5t3oj.56, slice 1)."""

import pytest
from fastapi.testclient import TestClient

from ream import api as api_module
from ream import keys, renderfind, store, thumbs
from ream.app import create_gallery_app

pytestmark = pytest.mark.background_thumbnails

NONCE = "0123456789abcdef"
GOOD = {"text": 40, "media": 1, "height": 600, "scroll": 900, "view": 900}


def line(nonce=NONCE, text=40, media=1, height=600, scroll=900, view=900):
    body = (
        f"ream-check {nonce} text={text} media={media} height={height}"
        f" scroll={scroll} view={view}"
    )
    return f'[1:1:0101/000000.1:INFO:CONSOLE:1] "{body}", source: file:///p.html (1)\n'


# --- the check line ----------------------------------------------------------


def test_the_right_nonce_is_accepted():
    assert renderfind.parse_check(line(), NONCE) == GOOD


def test_a_forged_line_with_another_nonce_is_ignored():
    assert renderfind.parse_check(line(nonce="f" * 16, text=0, media=0), NONCE) is None


def test_the_last_line_with_the_right_nonce_wins():
    both = line(text=0, media=0) + line(text=7)
    assert renderfind.parse_check(both, NONCE)["text"] == 7


def test_no_line_means_none_never_fine():
    assert renderfind.parse_check("", NONCE) is None
    assert renderfind.parse_check("noise\n", NONCE) is None


def test_a_malformed_or_oversized_line_is_dropped():
    bad = line().replace("text=40", "text=forty")
    assert renderfind.parse_check(bad, NONCE) is None
    huge = line(text=10**15)
    assert renderfind.parse_check(huge, NONCE) is None


def test_the_script_goes_first_in_the_head_and_carries_the_nonce():
    page = "<!doctype html><html><head><title>t</title></head><body>x</body></html>"
    out = renderfind.inject(page, NONCE)
    assert out.index("<head>") + len("<head>") == out.index("<script>")
    assert NONCE in out and out.endswith("</html>")
    assert out.replace(renderfind.script(NONCE), "") == page


def test_inject_without_a_head_still_places_it():
    out = renderfind.inject("<p>hi</p>", NONCE)
    assert NONCE in out and out.endswith("<p>hi</p>")
    out = renderfind.inject("<html><body>x</body></html>", NONCE)
    assert out.index("<script>") < out.index("<body>")


# --- the static missing-file check -------------------------------------------


def test_relative_and_root_relative_paths_are_flagged():
    page = (
        '<img src="logo.png"><script src="/app.js"></script>'
        '<link rel="stylesheet" href="css/site.css">'
    )
    assert renderfind.missing_files(page) == ["logo.png", "/app.js", "css/site.css"]


def test_what_is_served_or_not_a_file_is_not_flagged():
    page = (
        '<link rel="stylesheet" href="/_shared.css">'
        '<img src="data:image/png;base64,AAAA"><a href="#top">x</a>'
        '<a href="other.html">navigation is not a file</a>'
        '<img src="https://x.test/a.png"><img src="//x.test/a.png">'
        '<link rel="canonical" href="page.html"><img src="">'
    )
    assert renderfind.missing_files(page) == []


def test_comments_and_scripts_are_ignored():
    page = (
        '<!-- <img src="gone.png"> -->'
        "<script>var s = '<img src=\"inside.png\">';</script>"
    )
    assert renderfind.missing_files(page) == []


def test_live_data_is_recognised():
    assert renderfind.loads_live_data("<script>fetch('/api/v1/x')</script>")
    assert renderfind.loads_live_data("<script>await window.claude.use('db')</script>")
    assert not renderfind.loads_live_data("<p>fetch the milk</p>")


# --- the verdict -------------------------------------------------------------


def kinds(findings):
    return [f["kind"] for f in findings]


def test_a_clean_render_is_ok():
    assert renderfind.assess([], GOOD, [], False) == ("ok", [])


def test_a_script_error_is_broken():
    verdict, found = renderfind.assess(["line 3: Uncaught Error: x"], GOOD, [], False)
    assert verdict == "broken" and kinds(found) == ["script_error"]
    assert found[0]["detail"] == "line 3: Uncaught Error: x"


def test_a_blank_page_is_broken_and_so_is_one_with_no_height():
    blank = {**GOOD, "text": 0, "media": 0}
    assert renderfind.assess([], blank, [], False)[0] == "broken"
    flat = {**GOOD, "height": 0}
    verdict, found = renderfind.assess([], flat, [], False)
    assert verdict == "broken" and kinds(found) == ["blank"]


def test_a_missing_file_is_broken():
    verdict, found = renderfind.assess([], GOOD, ["logo.png"], False)
    assert verdict == "broken" and "logo.png" in found[0]["detail"]


def test_too_wide_is_for_the_agent_only_and_does_not_break():
    verdict, found = renderfind.assess([], {**GOOD, "scroll": 2000}, [], False)
    assert verdict == "ok" and kinds(found) == ["too_wide"]


def test_no_check_line_is_unchecked_never_ok():
    verdict, found = renderfind.assess([], None, [], False)
    assert verdict == "unchecked" and kinds(found) == ["unchecked"]


def test_a_missing_check_does_not_hide_a_script_error():
    verdict, found = renderfind.assess(["line 1: Uncaught x"], None, [], False)
    assert verdict == "broken" and "script_error" in kinds(found)


def test_a_live_page_blank_or_error_goes_to_the_agent_only():
    blank = {**GOOD, "text": 0, "media": 0}
    verdict, found = renderfind.assess(["line 1: Uncaught x"], blank, [], True)
    assert verdict == "unchecked"
    assert set(kinds(found)) == {"blank", "script_error"}
    assert all("live data" in f["detail"] for f in found)
    # a missing file is still certain on a live page
    assert renderfind.assess([], GOOD, ["a.png"], True)[0] == "broken"


def test_findings_are_capped_and_cleaned():
    errors = [f"line {n}: Uncaught \x1b[31m{'x' * 500}" for n in range(30)]
    _, found = renderfind.assess(errors, GOOD, [], False)
    assert len(found) == 10
    assert all(len(f["detail"]) <= 300 and "\x1b" not in f["detail"] for f in found)


# --- the render itself, with the browser faked --------------------------------


def test_render_report_still_unpacks_as_two_and_carries_the_check(monkeypatch):
    report = thumbs.Report(b"png", [], GOOD)
    png, errors = report
    assert (png, errors) == (b"png", []) and report.check == GOOD
    assert report == (b"png", [])


def test_a_render_with_no_browser_is_not_a_result(monkeypatch):
    monkeypatch.setenv("REAM_BROWSER", "/nonexistent/browser")
    assert thumbs.render_report("<p>x</p>") == (None, None)


# --- stored, read back, and on the route -------------------------------------


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db))


PAGE = "<!doctype html><html><head><title>t</title></head><body><p>x</p></body></html>"


def test_the_route_carries_verdict_and_findings_and_keeps_the_old_fields(
    db, api, monkeypatch
):
    token, _ = keys.mint(db, agent="seat")
    auth = {"Authorization": f"Bearer {token}"}
    page = store.publish(db, "seat", "Board", PAGE)
    monkeypatch.setattr(api_module.thumbs, "available", lambda: True)
    before = api.get(f"/api/v1/pages/{page.id}/render", headers=auth).json()
    assert before == {
        "rendered": False,
        "errors": [],
        "renderer": True,
        "verdict": "unchecked",
        "findings": [],
    }
    found = [{"kind": "missing_file", "detail": "logo.png"}]
    store.save_render_errors(db, page.id, 1, [], "broken", found)
    after = api.get(f"/api/v1/pages/{page.id}/render", headers=auth).json()
    assert after["rendered"] is True and after["errors"] == []
    assert after["verdict"] == "broken" and after["findings"] == found
    row = store.render_report(db, page.id)
    assert row["verdict"] == "broken"


def test_an_old_render_without_findings_reads_as_unchecked(db):
    page = store.publish(db, "seat", "Board", PAGE)
    store.save_render_errors(db, page.id, 1, [])
    row = store.render_report(db, page.id)
    assert row["rendered"] is True and row["verdict"] == "unchecked"
    assert row["findings"] == []


def test_the_background_render_stores_the_verdict(db, monkeypatch):
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    page = store.publish(
        db, "seat", "Board", PAGE.replace("<p>x</p>", '<img src="logo.png">')
    )
    html = store.get_body(db, "seat", page.id, as_reader=True).body
    monkeypatch.setattr(
        thumbs, "render_report", lambda h, *a: thumbs.Report(None, [], GOOD)
    )

    class Inline:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(thumbs.threading, "Thread", Inline)
    thumbs._failures.clear()
    thumbs.render_in_background(db, page.id, 1, html)
    row = store.render_report(db, page.id)
    assert row["verdict"] == "broken"
    assert row["findings"][0]["kind"] == "missing_file"


def test_the_tool_prints_every_finding_and_says_unchecked_not_fine(monkeypatch, capsys):
    from ream import client

    answer = {
        "rendered": True,
        "renderer": True,
        "errors": [],
        "verdict": "broken",
        "findings": [
            {"kind": "missing_file", "detail": "points to logo.png"},
            {"kind": "unchecked", "detail": "the check did not report"},
            {"kind": "script_error", "detail": "line 1: Uncaught x"},
        ],
    }
    monkeypatch.setattr(client, "_request", lambda *a, **k: answer)
    client._report_render("abc", "k")
    err = capsys.readouterr().err
    assert "missing file: points to logo.png" in err
    assert "not checked: the check did not report" in err
    assert "script_error" not in err
