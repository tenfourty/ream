"""The gallery's "Readers" panel, `GET /a/{id}/stats` (runcove-5t3oj.158.1, slice 4): the
owner's twin of the agent's stats route. Server-rendered, no script, everything escaped."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ream import email_share, pageviews, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body>one</body></html>"
EVIL = "<script>alert(1)</script>@example.com"


@pytest.fixture()
def gallery(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def page(db):
    return store.publish(db, "seat", "Board", PAGE)


def test_a_page_nobody_read_says_so_and_has_no_since(gallery, page):
    got = gallery.get(f"/a/{page.id}/stats")
    assert got.status_code == 200
    assert "Nobody has read this page yet" in got.text
    assert "First view counted on" not in got.text
    assert "<script" not in got.text


def test_counting_since_is_worded_as_the_first_view_counted(db, gallery, page):
    pageviews.record(db, page.id, "owner", "")
    day = datetime.now(UTC).date().isoformat()
    text = gallery.get(f"/a/{page.id}/stats").text
    assert f"First view counted on {day}" in text
    assert "Nobody has read this page yet" not in text


def test_totals_and_the_thirty_day_bars_per_kind(db, gallery, page):
    now = datetime.now(UTC)
    pageviews.record(db, page.id, "owner", "", now=now - timedelta(days=1))
    pageviews.record(db, page.id, "agent", "other", now=now)
    pageviews.record(db, page.id, "external", "", now=now)
    text = gallery.get(f"/a/{page.id}/stats").text
    for kind in ("owner", "external", "agent"):
        assert f'data-kind="{kind}"' in text
    assert len(re.findall(r'<i class="bar[ \"]', text)) == 3 * 30
    assert "You</span><b>1</b>" in text


def test_a_named_reader_is_listed_with_opens_and_last_open_escaped(db, gallery, page):
    share, _ = email_share.create(db, page.id, "ann@example.com")
    pageviews.record(db, page.id, "external", share.id)
    evil, _ = email_share.create(db, page.id, EVIL)
    pageviews.record(db, page.id, "external", evil.id)
    text = gallery.get(f"/a/{page.id}/stats").text
    assert "ann@example.com" in text
    assert "<script>alert(1)" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;@example.com" in text


def test_a_withdrawn_recipient_is_marked(db, gallery, page):
    share, _ = email_share.create(db, page.id, "ann@example.com")
    email_share.withdraw(db, page.id, share.id)
    assert "withdrawn" in gallery.get(f"/a/{page.id}/stats").text


def test_an_unknown_page_is_a_404(gallery):
    assert gallery.get("/a/abcdefghijklmnopqrstuvwxyz/stats").status_code == 404


def test_the_page_menu_links_to_readers(gallery, page):
    assert f'href="/a/{page.id}/stats"' in gallery.get(f"/a/{page.id}").text


def test_the_share_panel_shows_opens(db, gallery, page):
    share, _ = email_share.create(db, page.id, "ann@example.com")
    pageviews.record(db, page.id, "external", share.id)
    assert "opened 1 time" in gallery.get(f"/a/{page.id}/share").text
