"""A page that embeds fonts as data: addresses publishes with a warning naming them."""

from __future__ import annotations

import pytest
import scalecheck
from fastapi.testclient import TestClient

from ream import embeddedfonts, keys
from ream.app import create_gallery_app
from ream.renderfind import MAX_SCAN_CHARS, Budget

FONT = "data:font/woff2;base64," + "A" * 100


@pytest.fixture
def client(db, monkeypatch):
    return TestClient(create_gallery_app(db=db))


def shapes(body: str) -> list[dict]:
    return embeddedfonts.check(body)


def test_a_data_font_in_a_style_block_is_named_with_count_size_and_line():
    body = (
        "<!doctype html>\n<style>\n"
        f"@font-face{{font-family:A;src:url({FONT}) format('woff2')}}\n"
        f"@font-face{{font-family:B;src:url('{FONT}')}}\n"
        "</style>\n<p>x</p>"
    )
    (found,) = shapes(body)
    assert found["shape"] == "embedded_fonts" and found["line"] == 3
    assert "2 fonts" in found["hint"] and f"{2 * len(FONT)} bytes" in found["hint"]
    assert "text a person sees differ" in found["hint"]


def test_a_data_url_without_a_font_type_inside_font_face_counts():
    body = "<style>@font-face { src: url(data:application/octet-stream;base64,AAAA) }</style>"
    assert "1 font " in shapes(body)[0]["hint"]


def test_other_pages_get_no_finding():
    assert shapes("<p>hi</p>") == []
    assert shapes("<style>@font-face{src:url(/f.woff2)}</style>") == []
    # a data: address outside @font-face is the image's business, not a font's
    assert shapes(f"<style>a{{background:url({FONT})}}</style>") == []
    assert shapes(f"<style>@font-face{{/* url({FONT}) */ src:local(x)}}</style>") == []


def test_an_unclosed_font_face_runs_to_the_end_of_the_text():
    assert shapes("@font-face {" * 50_000) == []
    # the face is open, and its data: address is still a font
    (found,) = shapes(f"<style>@font-face{{src:url({FONT})")
    assert "1 font " in found["hint"]


def test_offsets_survive_a_character_that_grows_when_lower_cased():
    body = (
        "<title>"
        + "İ" * 200
        + f"</title>\n<style>@font-face{{src:url({FONT})}}</style>"
    )
    (found,) = shapes(body)
    assert "1 font " in found["hint"] and f"{len(FONT)} bytes" in found["hint"]
    assert found["line"] == 2


def test_a_comment_hides_the_closing_brace_and_the_font():
    body = f"<style>@font-face{{/* }} */ src:url({FONT})}}</style>"
    assert "1 font " in shapes(body)[0]["hint"]
    # a comment that never closes runs to the end, so the address in it is not read
    assert shapes(f"<style>@font-face{{/* src:url({FONT})}}</style>") == []


def test_a_brace_inside_a_string_does_not_end_the_block():
    body = f"<style>@font-face{{font-family:'}}';src:url({FONT})}}</style>"
    assert "1 font " in shapes(body)[0]["hint"]


def test_whitespace_inside_the_quotes_before_data_is_read():
    body = f"<style>@font-face{{src:url(' {FONT}')}}</style>"
    (found,) = shapes(body)
    assert f"{len(FONT)} bytes" in found["hint"]


def test_a_face_with_fallbacks_is_one_font_and_a_paren_in_quotes_does_not_cut():
    plain = "data:font/woff;base64,AAAA"
    body = (
        f"<style>@font-face{{src:url({FONT}) format('woff2'),url({plain}) format('woff')}}"
        "@font-face{src:url('data:font/x;base64,A)B')}</style>"
    )
    (found,) = shapes(body)
    assert "2 fonts" in found["hint"]
    assert (
        f"{len(FONT) + len(plain) + len('data:font/x;base64,A)B')} bytes"
        in found["hint"]
    )


def test_the_block_cap_says_at_least():
    body = (
        "<style>"
        + f"@font-face{{src:url({FONT})}}" * (embeddedfonts.MAX_BLOCKS + 5)
        + "</style>"
    )
    (found,) = shapes(body)
    assert f"at least {embeddedfonts.MAX_BLOCKS} fonts" in found["hint"]


def test_a_page_too_large_or_a_spent_budget_says_the_check_did_not_run():
    (found,) = shapes("x" * (MAX_SCAN_CHARS + 1))
    assert found["shape"] == "embedded_fonts_not_run"
    spent = Budget(0)
    (found,) = embeddedfonts.check(
        f"<style>@font-face{{src:url({FONT})}}</style>", spent
    )
    assert found["shape"] == "embedded_fonts_not_run"


@pytest.mark.scaling
@pytest.mark.parametrize(
    ("first", "unit", "last"),
    [
        ("<style>@font-face{", "/* ", "}</style>"),
        ("<style>", "@font-face{/**/", "}</style>"),
        ("<style>@font-face{", "'}", "</style>"),
    ],
    ids=["unclosed comments", "closed comments", "quotes"],
)
def test_eight_times_the_page_costs_about_eight_times_the_time(first, unit, last):
    scalecheck.assert_linear(
        lambda text: embeddedfonts.check(text, Budget(600)), first, unit, last
    )


def test_publish_answers_the_warning(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    body = f"<!doctype html><style>@font-face{{src:url({FONT})}}</style><p>x</p>"
    response = client.post(
        "/api/v1/artifacts",
        json={"title": "Fonts", "body": body},
        headers={"authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201
    found = [w for w in response.json()["warnings"] if w["shape"] == "embedded_fonts"]
    assert len(found) == 1
