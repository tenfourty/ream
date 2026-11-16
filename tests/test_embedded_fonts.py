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


def test_count_bytes_and_line_of_the_first():
    body = (
        "<!doctype html>\n<style>\n"
        f"@font-face{{font-family:A;src:url({FONT}) format('woff2')}}\n"
        f"@font-face{{font-family:B;src:url('{FONT}')}}\n"
        "</style>\n<p>x</p>"
    )
    (found,) = shapes(body)
    assert found["shape"] == "embedded_fonts" and found["line"] == 3
    assert "2 font files" in found["hint"] and f"{2 * len(FONT)} bytes" in found["hint"]
    assert "text a person sees differ" in found["hint"] and "advisory" in found["hint"]


def test_one_file_reads_singular():
    assert (
        "1 font file "
        in shapes(f"<style>@font-face{{src:url({FONT})}}</style>")[0]["hint"]
    )


@pytest.mark.parametrize(
    "address",
    [
        "DATA:FONT/WOFF2;base64,AAAA",
        "data:  font/woff;base64,AAAA",
        "data:font/ttf,AAAA",
        "data:application/font-woff;base64,AAAA",
        "data:application/x-font-ttf;base64,AAAA",
        "data:application/vnd.ms-fontobject;base64,AAAA",
        "data:application/font-sfnt;base64,AAAA",
    ],
)
def test_the_font_types_and_the_case_and_space_around_them(address):
    body = f"<style>@font-face{{src:url(' {address}')}}</style>"
    (found,) = shapes(body)
    assert found["shape"] == "embedded_fonts"
    assert f"{len(address)} bytes" in found["hint"]


def test_a_quote_paren_space_or_angle_bracket_ends_the_address():
    for end in ["'", '"', ")", " ", "<"]:
        assert f"{len(FONT)} bytes" in shapes(f"x url({FONT}{end}y")[0]["hint"]


def test_a_font_anywhere_in_the_text_counts_even_in_a_comment_or_template():
    assert shapes(f"<style>/* url({FONT}) */</style>")
    assert shapes(f"<template><style>@font-face{{src:url({FONT})}}</style></template>")


def test_other_pages_get_no_finding():
    assert shapes("<p>hi</p>") == []
    assert shapes("<style>@font-face{src:url(/f.woff2)}</style>") == []
    assert (
        shapes(f"<style>a{{background:url(data:image/png;base64,{'A' * 50})}}</style>")
        == []
    )
    assert shapes("data:font/woff2 no separator") == []
    assert shapes("data:application/octet-stream;base64,AAAA") == []


def test_a_page_too_large_or_a_spent_budget_says_the_check_did_not_run():
    (found,) = shapes("x" * (MAX_SCAN_CHARS + 1))
    assert found["shape"] == "embedded_fonts_not_run"
    (found,) = embeddedfonts.check(f"url({FONT})", Budget(0))
    assert found["shape"] == "embedded_fonts_not_run"


@pytest.mark.scaling
@pytest.mark.parametrize(
    ("first", "unit", "last"),
    [
        ("<style>", "url(data:font/woff2;base64,", "</style>"),
        ("<style>", "data:", "</style>"),
        ("<style>", "data:font/", "</style>"),
        ("<style>", "data:     ", "</style>"),
        ("<style>", "data:font/woff2;base64,AAAA ", "</style>"),
    ],
    ids=["fonts", "no type", "type cut short", "spaces", "spaced fonts"],
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
