"""A page that embeds fonts as data: addresses publishes with a warning naming them."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ream import embeddedfonts, keys
from ream.app import create_gallery_app

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


def test_an_unclosed_font_face_ends_the_reading_without_a_finding():
    assert shapes("@font-face {" * 50_000) == []


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
