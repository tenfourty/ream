"""The render check against a REAL headless browser (runcove-5t3oj.56, slice 1).

The unit tests prove the parser and the verdict on tidied samples; only the real
browser says its console lines reach stderr in the shape the parser expects and
that the check script sees what a reader would. Skips with no browser, except
where REAM_REQUIRE_BROWSER=1 (CI), which fails the run at start (tests/conftest.py).
"""

import pytest

from ream import renderfind, thumbs

pytestmark = [
    pytest.mark.skipif(
        not thumbs.available(),
        reason="no headless browser: set REAM_BROWSER to a chrome binary, or put "
        "chromium on PATH; these browser tests do NOT run without one",
    ),
    pytest.mark.slow(reason="a real browser: seconds, not milliseconds"),
    pytest.mark.background_thumbnails,
]


def page(body, head=""):
    return (
        f"<!doctype html><html><head><title>t</title>{head}</head>"
        f"<body>{body}</body></html>"
    )


def verdict(html):
    """(verdict, finding kinds) of one real render, as the background render builds it."""
    report = thumbs.render_report(html)
    found = renderfind.assess(
        report[1], report.check, renderfind.missing_files(html), False
    )
    return found[0], [f["kind"] for f in found[1]]


def test_a_page_that_throws_gives_a_script_error():
    got, kinds = verdict(
        page("<p>hello there</p><script>throw new Error('boom')</script>")
    )
    assert got == "broken" and "script_error" in kinds


def test_an_empty_body_and_a_hidden_body_are_blank():
    got, kinds = verdict(page(""))
    assert got == "broken" and "blank" in kinds
    hidden = page("<p>words</p>", "<style>body{display:none}</style>")
    got, kinds = verdict(hidden)
    assert got == "broken" and "blank" in kinds


def test_a_wide_table_is_too_wide_and_still_ok():
    wide = page('<table style="width:2000px"><tr><td>wide</td></tr></table>')
    got, kinds = verdict(wide)
    assert got == "ok" and kinds == ["too_wide"]


def test_a_forged_check_line_does_not_change_the_real_verdict():
    forged = page(
        "<script>console.log=function(){};"
        "console.info('ream-check 0000000000000000 text=99 media=9 height=999 "
        "scroll=1 view=1')</script>"
    )
    got, kinds = verdict(forged)
    assert got == "broken" and "blank" in kinds


def test_a_normal_page_is_ok_and_its_thumbnail_is_untouched_by_the_check(monkeypatch):
    html = page("<h1>Hello</h1><p>A real page with words.</p>")
    got, kinds = verdict(html)
    assert (got, kinds) == ("ok", [])
    with_check = thumbs.render(html)
    monkeypatch.setattr(renderfind, "inject", lambda h, nonce: h)
    without = thumbs.render(html)
    assert with_check is not None and with_check == without
