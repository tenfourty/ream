"""Thumbnails on the wall: never stale, never in the way of a publish.

homelab-ij8.22. Two properties carry this feature, and both are the kind that
look obviously true and quietly stop being so.
"""

import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from artifacts import keys, store, thumbs
from artifacts.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>B</title></head><body><p>one</p></body></html>"
)
PNG = b"\x89PNG\r\n\x1a\n" + b"fake png bytes for a test" * 4


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- a thumbnail can never outlive the version it depicts --------------------


def test_republishing_drops_the_old_picture(db):
    """THE ONE THAT MATTERS. A stale thumbnail is worse than none: Jeremy reads
    the wall as current, so a picture of yesterday's board under today's title
    is the exact drift this whole service exists to prevent. Storing against the
    VERSION rather than the page is what makes that impossible rather than
    merely unlikely."""
    keys.mint(db, agent="seat")
    art = store.publish(db, "seat", "Board", PAGE)
    assert store.save_thumbnail(db, art.id, art.version, PNG)
    assert store.live_thumbnail(db, art.id) == PNG

    store.patch(db, "seat", art.id, PAGE.replace("one", "two"), art.revision)
    assert store.live_thumbnail(db, art.id) is None, (
        "the new version must not inherit the old version's picture"
    )


def test_the_old_versions_picture_is_still_its_own(db):
    """Not deleted, just not live. It was never wrong about the version it
    depicts, and the history is the point of keeping versions."""
    keys.mint(db, agent="seat")
    art = store.publish(db, "seat", "Board", PAGE)
    store.save_thumbnail(db, art.id, art.version, PNG)
    store.patch(db, "seat", art.id, PAGE.replace("one", "two"), art.revision)
    with db.connect() as conn:
        kept = conn.execute(
            "select thumbnail from version where artifact_id = %s and n = 1",
            (art.id,),
        ).fetchone()
    assert bytes(kept[0]) == PNG


def test_a_picture_for_a_version_that_has_gone_is_dropped_not_raised(db):
    """A render finishes after the publish that triggered it has returned, so
    the page may have moved on or been deleted meanwhile. An ordinary race with
    an obvious answer."""
    keys.mint(db, agent="seat")
    art = store.publish(db, "seat", "Board", PAGE)
    assert store.save_thumbnail(db, art.id, 99, PNG) is False
    assert store.save_thumbnail(db, "no-such-page", 1, PNG) is False


# --- a publish never waits on a browser --------------------------------------


def test_publishing_does_not_wait_for_the_render(client, db, monkeypatch):
    """A render takes the better part of a second on a good day and twenty on a
    bad one. An agent publishing is usually mid-task with a waiter armed; making
    it pay for a decoration would be a poor trade even if browsers were
    reliable, which is the one thing they are not."""
    started = threading.Event()
    release = threading.Event()

    def slow_render(html):
        started.set()
        release.wait(timeout=10)
        return PNG, []

    monkeypatch.setattr(thumbs, "available", lambda: True)
    # render_report: the background job's entry point since it also collects
    # the page's script errors (homelab-ij8.72).
    monkeypatch.setattr(thumbs, "render_report", slow_render)

    token, _ = keys.mint(db, agent="seat")
    began = time.monotonic()
    got = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    )
    elapsed = time.monotonic() - began

    assert got.status_code == 201
    assert started.wait(timeout=5), "the render never started"
    assert elapsed < 2.0, f"publish waited {elapsed:.1f}s on the browser"
    release.set()


def test_a_render_that_explodes_never_reaches_the_publisher(client, db, monkeypatch):
    """A decoration may not take down a publish."""
    monkeypatch.setattr(thumbs, "available", lambda: True)
    monkeypatch.setattr(
        thumbs,
        "render_report",
        lambda html: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    token, _ = keys.mint(db, agent="seat")
    got = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    )
    assert got.status_code == 201


def test_no_browser_means_no_thread_at_all(db, monkeypatch):
    monkeypatch.setattr(thumbs, "available", lambda: False)
    before = threading.active_count()
    thumbs.render_in_background(db, "some-page", 1, PAGE)
    assert threading.active_count() == before


# --- serving and the wall ----------------------------------------------------


def test_the_picture_is_served_from_the_gallery(client, db):
    keys.mint(db, agent="seat")
    art = store.publish(db, "seat", "Board", PAGE)
    store.save_thumbnail(db, art.id, art.version, PNG)
    got = client.get(f"/thumb/{art.id}.png")
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/png"
    assert got.content == PNG


def test_a_page_with_no_picture_is_a_404_not_a_placeholder(client, db):
    """A broken-image icon reads as the PAGE being broken. The text tile is a
    complete tile on its own."""
    keys.mint(db, agent="seat")
    art = store.publish(db, "seat", "Board", PAGE)
    assert client.get(f"/thumb/{art.id}.png").status_code == 404
    assert client.get("/thumb/nonexistent.png").status_code == 404


def test_the_thumbnail_is_not_cached_past_a_republish(client, db):
    """The URL names the PAGE, and the page's live version moves. A long cache
    would show last week's picture of a board republished this morning."""
    keys.mint(db, agent="seat")
    art = store.publish(db, "seat", "Board", PAGE)
    store.save_thumbnail(db, art.id, art.version, PNG)
    cache = client.get(f"/thumb/{art.id}.png").headers["cache-control"]
    assert "private" in cache
    assert "max-age=60" in cache


def test_the_wall_shows_a_picture_only_when_there_is_one(client, db):
    keys.mint(db, agent="seat")
    plain = store.publish(db, "seat", "No picture", PAGE)
    shot = store.publish(db, "seat", "Has a picture", PAGE)
    store.save_thumbnail(db, shot.id, shot.version, PNG)

    page = client.get("/").text
    assert f"/thumb/{shot.id}.png" in page
    assert f"/thumb/{plain.id}.png" not in page
    assert "No picture" in page, "the text tile is still a complete tile"


def test_the_tile_image_keeps_height_auto(client, db):
    """A rule that reads as correct and silently is not.

    The img carries width/height ATTRIBUTES so the grid does not reflow as lazy
    images arrive. A height attribute makes the used height not-auto, which
    stops `aspect-ratio` computing anything — measured, the box came out 299x640
    against a 900x640 image, so `object-fit:cover` scaled to full height and
    cropped the sides, showing a hugely magnified slice of each page. Every
    property in that rule looked right. Only rendering it and measuring the box
    found it, so this asserts the one property that makes the rest work.
    """
    page = client.get("/").text
    start = page.index(".tiles .shot{")
    rule = page[start : page.index("}", start)]
    assert "height:auto" in rule, (
        "without height:auto the height attribute wins and aspect-ratio is inert; "
        "the thumbnail becomes a magnified crop"
    )
    assert re.search(r"aspect-ratio:\d+/\d+", rule), (
        "some fixed ratio must be set, or the box has no height for `auto` to "
        "resolve against and the grid reflows as each lazy image lands"
    )
    assert "object-fit:cover" in rule
    # The RATIO ITSELF is a design choice and is not asserted here. It used to
    # be pinned to 900/640 -- the renderer's own dimensions -- which made this a
    # change-detector: moving the tile to the reference's 1.9:1 broke a test
    # about `height:auto`, which had not changed. The tile's shape is covered by
    # rendering the wall and looking at it; this test exists for the one
    # property that is invisible when wrong.
    #
    # The two ratios differ ON PURPOSE. The renderer captures the page at
    # thumbs.WIDTH x thumbs.HEIGHT and the tile shows a wider crop of it,
    # anchored to the top -- `object-position:top center` -- because the top of
    # a page is the part a reader recognises.
