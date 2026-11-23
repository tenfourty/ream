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


def test_each_bar_names_its_date_and_count_for_hover_and_screen_readers(
    db, gallery, page
):
    now = datetime.now(UTC)
    pageviews.record(db, page.id, "owner", "", now=now - timedelta(days=1))
    pageviews.record(db, page.id, "agent", "other", now=now)
    text = gallery.get(f"/a/{page.id}/stats").text
    day = now - timedelta(days=1)
    words = f"{day.day} {day:%b}: 1 view"
    assert f'title="{words}" aria-label="{words}"' in text
    # a quiet day is a plural, not "1 views"
    assert re.search(
        r'title="\d+ \w{3}: 0 views" aria-label="\d+ \w{3}: 0 views"', text
    )
    assert len(re.findall(r'<i class="bar[ "][^>]*role="img"', text)) == 3 * 30


def test_each_chart_gives_its_peak_and_first_and_last_day(db, gallery, page):
    now = datetime.now(UTC)
    for who in ("a", "b", "c"):
        pageviews.record(db, page.id, "agent", who, now=now - timedelta(days=2))
    pageviews.record(db, page.id, "agent", "a", now=now)
    text = gallery.get(f"/a/{page.id}/stats").text
    first, last = now - timedelta(days=29), now
    scales = re.findall(r'<div class="scale">(.*?)</div>', text)
    assert len(scales) == 3
    assert scales[2] == (
        f"<span>{first.day} {first:%b}</span><span>Peak 3 a day</span>"
        f"<span>{last.day} {last:%b}</span>"
    )
    assert "No views" in scales[0] and "No views" in scales[1]


def test_the_totals_pairs_share_one_spacing_and_the_table_aligns_left(gallery, page):
    text = gallery.get(f"/a/{page.id}/stats").text
    # one gap for every label/number pair, not a fixed-width label for the first
    assert ".cx .tot>span{display:flex;align-items:baseline;gap:var(--space-5)}" in text
    assert "min-width:7rem" not in text
    # the table's outer cells carry no side padding, so it starts where the headings do
    assert ".cx th:first-child,.cx td:first-child{padding-left:0}" in text
    # ...and so does the Back button's form, which the wall's `.row` rule would indent
    assert ".cx .row{padding-left:0}" in text


def test_the_readers_row_has_its_own_icon_not_the_all_pages_grid(gallery, page):
    text = gallery.get(f"/a/{page.id}").text
    row = re.search(
        r'<a class="mrow" href="/a/[^"]+/stats">(<svg.*?</svg>)Readers</a>', text
    )
    all_pages = re.search(
        r'<a class="mrow" href="/">(<svg.*?</svg>)All pages</a>', text
    )
    assert row and all_pages
    assert row.group(1) != all_pages.group(1)
    assert 'fill-rule="evenodd"' in row.group(1)


def test_the_menu_header_separates_its_parts(gallery, page):
    text = gallery.get(f"/a/{page.id}").text
    head = re.search(r'<div class="mhead">(.*?)</div>', text).group(1)
    assert "Updated" in head and "All your agents can read it" in head
    sep = '</span><span aria-hidden="true"> &middot; </span><span>'
    assert head.count(sep) == 1
    assert "</span><span>" not in head.replace(sep, "")
