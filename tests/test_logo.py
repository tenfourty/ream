"""The Ream mark in the gallery: inline in the bar, and the browser-tab icon at /favicon.svg.
It is the gallery's own chrome, so it is served on the gallery host alone and never reaches
a page an agent published."""

import re
from pathlib import Path
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from ream import frame_pass, keys, logo, store
from ream.app import create_app, create_gallery_app

GALLERY_HOST = "artifacts.tenfourty.site"
FORMER = "old.tenfourty.site"
API_HOST = "api.tenfourty.site"
PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"
ICON_LINK = '<link rel="icon" type="image/svg+xml" sizes="any" href="/favicon.svg">'
BRAND = Path(logo.__file__).resolve().parent / "brand"
LIGHT = ("#191a17", "#0f6f63")
DARK = ("#e9eae4", "#5ec9b8")


@pytest.fixture()
def client(db) -> TestClient:
    return TestClient(create_app(db), follow_redirects=False)


@pytest.fixture()
def gallery(db) -> TestClient:
    return TestClient(create_gallery_app(db))


def _parsed(svg: str) -> ElementTree.Element:
    """Well-formed or it raises: a broken SVG icon just shows nothing. Only ever this
    repo's own drawings, so the XML-attack lint does not apply."""
    return ElementTree.fromstring(svg)  # noqa: S314


def _geometry(svg: str) -> list[str]:
    drawing = svg.split("<g", 1)[1]
    return re.findall(r'\s((?:d|x|y|width|height)="[^"]*")', drawing)


def _bar(html: str) -> str:
    after = re.split(r'<header class="bar[^"]*">', html, maxsplit=1)[1]
    return after.split("</header>")[0]


@pytest.mark.parametrize("size", ["lg", "sm"])
@pytest.mark.parametrize("theme", ["lt", "dk", "mono"])
def test_the_chosen_mark_is_in_the_repo(size, theme):
    svg = (BRAND / f"rooted-{size}-{theme}.svg").read_text(encoding="utf-8")
    assert _parsed(svg).get("viewBox") == "0 0 512 512"
    assert "Ream mark: rooted" in svg


def test_the_icon_is_served_on_the_gallery_host(client):
    response = client.get("/favicon.svg", headers={"host": GALLERY_HOST})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"].startswith("private")
    _parsed(response.text)


def test_the_icon_is_the_small_drawing_and_follows_the_theme(client):
    svg = client.get("/favicon.svg", headers={"host": GALLERY_HOST}).text
    small = (BRAND / "rooted-sm-lt.svg").read_text(encoding="utf-8")
    assert _geometry(svg) == _geometry(small)
    style = svg.split("<style>", 1)[1].split("</style>", 1)[0]
    light, dark = style.split("@media (prefers-color-scheme:dark)", 1)
    assert all(colour in light for colour in LIGHT)
    assert all(colour in dark for colour in DARK)


def test_the_icon_fetches_nothing(client):
    svg = client.get("/favicon.svg", headers={"host": GALLERY_HOST}).text
    without_namespace = svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "http" not in without_namespace
    assert "url(" not in without_namespace
    assert "<script" not in without_namespace


def test_a_page_of_the_gallery_names_the_icon_in_its_head(db, gallery):
    published = store.publish(db, "ops-seat", "Board", PAGE)
    for path in ("/", f"/a/{published.id}", "/connect"):
        head = gallery.get(path).text.split("</head>", 1)[0]
        assert ICON_LINK in head, path


def test_the_wall_shows_the_large_drawing_beside_its_title(gallery):
    bar = _bar(gallery.get("/").text)
    assert bar.index('class="mark mark-lg"') < bar.index("<h1>")
    assert 'class="home"' not in bar
    large = (BRAND / "rooted-lg-lt.svg").read_text(encoding="utf-8")
    assert _geometry(bar.split("</svg>")[0]) == _geometry(large)


def test_on_a_page_the_mark_is_the_way_back_and_is_the_small_drawing(db, gallery):
    published = store.publish(db, "ops-seat", "Board", PAGE)
    bar = _bar(gallery.get(f"/a/{published.id}").text)
    link = re.search(r'<a class="home" href="/" aria-label="All pages">(.*?)</a>', bar)
    assert link is not None
    assert 'class="mark mark-sm"' in link[1]
    small = (BRAND / "rooted-sm-lt.svg").read_text(encoding="utf-8")
    assert _geometry(link[1]) == _geometry(small)


def test_the_mark_in_the_bar_is_inline_markup_coloured_by_the_stylesheet(gallery):
    html = gallery.get("/").text
    mark = _bar(html).split("</svg>")[0].split("<svg", 1)[1]
    assert "#" not in mark, "colours belong to the stylesheet, which follows the theme"
    assert "href" not in mark and "http" not in mark
    style = html.split("<style>", 1)[1].split("</style>", 1)[0]
    assert all(colour in style for colour in LIGHT + DARK)


def test_the_mark_carries_no_id_a_page_could_collide_with(gallery):
    assert 'id="mark"' not in gallery.get("/").text


def test_the_former_host_forwards_the_icon_and_never_serves_it(client, monkeypatch):
    monkeypatch.setenv("REAM_FORMER_HOST", FORMER)
    response = client.get("/favicon.svg", headers={"host": FORMER})
    assert response.status_code == 308
    assert response.headers["location"] == f"https://{GALLERY_HOST}/favicon.svg"


def test_the_api_host_has_no_icon(client, monkeypatch):
    monkeypatch.setenv("REAM_API_HOST", API_HOST)
    response = client.get("/favicon.svg", headers={"host": API_HOST})
    assert response.status_code == 404


def test_a_published_page_never_gets_the_mark(db, client):
    keys.mint(db, agent="ops-seat")
    artifact = store.publish(db, "ops-seat", "Board", PAGE)
    host = {"host": f"{artifact.id}.tenfourty.com"}
    framed = {**host, "sec-fetch-dest": "iframe"}
    page = client.get(f"/p/{frame_pass.mint(db, artifact.id)}/", headers=framed)
    assert page.status_code == 200
    assert "favicon.svg" not in page.text
    assert "mark-i" not in page.text
    assert 'class="mark' not in page.text
    assert client.get("/favicon.svg", headers=host).status_code == 404
