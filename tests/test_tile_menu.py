"""A dots menu on every tile, and pinned pages leading the wall.

homelab-ij8.84.2 and .3. Jeremy sent fifteen screenshots of claude.ai's
artifacts UI and said "lets try to replicate all of this". Two things off those
screenshots are here: a vertical-dots control sitting on each tile, carrying a
SHORTER list than the page's own title menu (Pin, Rename, Copy link), and a pin
that makes a page lead the gallery regardless of when it last changed.

The tests that matter in this file are structural, not cosmetic: the control
must be a sibling of the tile's link rather than inside it, it must be visible
without a pointer, and the pinned ordering must put NULLs last. Each of those
three is invisible when wrong -- the page still renders, and only the behaviour
is broken.
"""

import re

import pytest
from fastapi.testclient import TestClient

from artifacts import store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db=db))


def _tile(page: str, artifact_id: str) -> str:
    """The one <li> for this page, from the wall."""
    tiles = re.findall(r"<li class=\"tile-wrap[^\"]*\">.*?</li>", page, re.DOTALL)
    matching = [t for t in tiles if artifact_id in t]
    assert len(matching) == 1, f"expected exactly one tile for {artifact_id}"
    return matching[0]


# --- the shape --------------------------------------------------------------


def test_the_dots_are_a_sibling_of_the_link_never_inside_it(client, db):
    """An interactive control nested in an <a> is invalid HTML and behaves
    unpredictably -- on some engines the link swallows the click, which on a
    phone is a menu that simply never opens. It renders identically either way,
    so nothing but this test would notice."""
    published = store.publish(db, "seat", "Board", PAGE)
    tile = _tile(client.get("/").text, published.id)

    link = re.search(r"<a href=[^>]*>.*?</a>", tile, re.DOTALL)
    assert link, "the tile has a link"
    assert "<details" not in link.group(0), "the menu is not inside the link"
    assert '<details class="menu tmenu"' in tile, "but it is inside the tile"


def test_the_dots_are_visible_without_a_pointer(client, db):
    """He reads this wall on a phone. A control revealed on :hover does not
    exist there, and an earlier version of this CSS faded it in from opacity 0
    with a (hover:none) escape hatch -- two rules disagreeing about whether
    something exists."""
    store.publish(db, "seat", "Board", PAGE)
    page = client.get("/").text
    dots_rule = re.search(r"\.tile-wrap \.menu > summary\{[^}]*\}", page)
    assert dots_rule, "the dots control is styled"
    assert "opacity:0" not in dots_rule.group(0)


def test_the_tile_menu_is_shorter_than_the_pages_own_menu(client, db):
    """Per the reference: three things on a tile, everything else once you are
    on the page. A menu you glance at on a wall of twenty is not the place for
    the full list."""
    published = store.publish(db, "seat", "Board", PAGE)
    tile = _tile(client.get("/").text, published.id)
    assert f'action="/a/{published.id}/pin"' in tile
    assert f'action="/a/{published.id}/rename"' in tile
    assert "/a/" in tile and "artifacts.tenfourty.site" in tile, "and the link"
    assert f'action="/a/{published.id}/archive"' not in tile, (
        "archive stays on the page's own menu"
    )


def test_copy_link_hands_over_a_real_address_with_no_script(client, db):
    """There is no clipboard without script, so the scriptless answer is the
    address itself, in a field you can select. A menu item that silently does
    nothing would be worse than not having one."""
    published = store.publish(db, "seat", "Board", PAGE)
    tile = _tile(client.get("/").text, published.id)
    assert f'value="https://artifacts.tenfourty.site/a/{published.id}"' in tile


def test_no_inline_event_handler_anywhere_on_the_wall(client, db):
    """This origin's CSP is script-src 'self' with no 'unsafe-inline', so an
    inline handler is not a shortcut -- it is a handler that never runs. One
    was written here and caught before it shipped."""
    store.publish(db, "seat", "Board", PAGE)
    page = client.get("/").text
    assert not re.search(r"\son(click|focus|change|submit)=", page)


# --- pinning ----------------------------------------------------------------


def test_a_pinned_page_leads_the_wall_however_old_it_is(client, db):
    old = store.publish(db, "seat", "The old one", PAGE)
    store.publish(db, "seat", "The new one", PAGE)
    assert _order(client) == ["The new one", "The old one"], "newest first, normally"

    store.set_pinned(db, old.id, True, agent=None)
    assert _order(client) == ["The old one", "The new one"]


def _order(client) -> list[str]:
    return re.findall(r'<span class="t">([^<]*)</span>', client.get("/").text)


def test_unpinned_pages_sort_below_pinned_ones_not_above(client, db):
    """Postgres sorts NULLs FIRST on a DESC order, so `pinned_at desc` without
    `nulls last` puts every unpinned page above every pinned one -- precisely
    backwards, and it reads as pinning having done nothing rather than as a
    broken sort."""
    store.publish(db, "seat", "Unpinned A", PAGE)
    pinned = store.publish(db, "seat", "Pinned", PAGE)
    store.publish(db, "seat", "Unpinned B", PAGE)
    store.set_pinned(db, pinned.id, True, agent=None)
    assert _order(client)[0] == "Pinned"


def test_the_tile_of_a_pinned_page_says_so(client, db):
    published = store.publish(db, "seat", "Board", PAGE)
    assert "pinmark" not in _tile(client.get("/").text, published.id)
    store.set_pinned(db, published.id, True, agent=None)
    tile = _tile(client.get("/").text, published.id)
    assert "pinmark" in tile
    assert "Unpin" in tile, "and its menu offers the way back"


# --- the route --------------------------------------------------------------


def test_pinning_from_the_gallery_returns_to_the_wall(client, db):
    """Unlike archive and rename, which are done while looking at the page.
    Pinning is a statement about the wall's order, so the answer to "did that
    work?" is the wall, reordered."""
    published = store.publish(db, "seat", "Board", PAGE)
    out = client.post(
        f"/a/{published.id}/pin",
        data={"pinned": "1"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert out.status_code == 303
    assert out.headers["location"] == "/"


def test_a_cross_site_form_cannot_pin(client, db):
    """The same guard as answering, for the same reason: a state change made
    with his session."""
    published = store.publish(db, "seat", "Board", PAGE)
    out = client.post(
        f"/a/{published.id}/pin",
        data={"pinned": "1"},
        headers={"sec-fetch-site": "cross-site"},
        follow_redirects=False,
    )
    assert out.status_code == 403
    assert store.get_owned(db, "seat", published.id).pinned_at is None


def test_pinning_a_page_that_is_not_there_is_a_404(client):
    out = client.post(
        "/a/nosuchpage/pin",
        data={"pinned": "1"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert out.status_code == 404
