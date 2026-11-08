"""A body that never claims to be HTML is refused.

homelab-ij8.28, 19 Sep 2026: a builder script printed "wrote <path>" to
stdout, that log line was piped into a publish, and a 145-byte body with no
doctype, no html and no head was accepted, stored as a new version, and served
at an address Jeremy has. The publish path guarded size and scanned for
secrets and never asked the first question.

The guard is deliberately generous and must never become a validator. A page
that is odd, or minimal, or a fragment somebody meant is the author's
business. This refuses one shape: a body that is not HTML at all.
"""

import pytest
from fastapi.testclient import TestClient

from ream import keys, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- what must be refused -----------------------------------------------------


THE_LOG_LINE = "wrote /tmp/artifacts-status.html (37443 bytes)\n"


@pytest.mark.parametrize(
    "body",
    [
        THE_LOG_LINE,
        '{"id": "abc", "url": "https://example.com"}',
        "# A markdown document\n\nWith a paragraph.\n",
        "   \n\t\n",
        "",
    ],
    ids=["the log line", "json", "markdown", "whitespace", "empty"],
)
def test_a_body_that_is_not_a_page_is_refused(db, body):
    keys.mint(db, agent="seat")
    with pytest.raises(store.NotAPage) as caught:
        store.publish(db, "seat", "Board", body)
    assert caught.value.field == "body", "named like every other refusal"


def test_a_page_with_no_doctype_and_no_html_element_still_publishes(db):
    """THE ONE THAT NEARLY CAUSED AN OUTAGE. The bead asked for "no doctype and
    no <html>" to be refused, and the first version did exactly that -- then a
    sweep of the live wall found the operator's "Homelab decisions and status",
    83 KB, republished daily, which begins <title> and has none of those
    elements. A guard that refuses a working page on somebody else's daily
    schedule is not a guard. Any element-looking tag counts now; the shapes
    this exists to catch have none at all."""
    published = store.publish(
        db,
        "seat",
        "Decisions",
        "<title>Homelab decisions and status</title>\n<style>:root{}</style>\n"
        "<header>Today</header>\n",
    )
    assert published.id


def test_an_update_is_refused_too(db):
    """The incident was an UPDATE: a page Jeremy already had, replaced by a log
    line. Guarding only the create would have left the damage exactly where it
    happened."""
    art = store.publish(db, "seat", "Board", PAGE)
    with pytest.raises(store.NotAPage):
        store.patch(db, "seat", art.id, THE_LOG_LINE, art.revision)
    assert store.get_body(db, "seat", art.id).body == PAGE, "the page is untouched"


def test_the_refusal_is_a_named_422_over_the_api(client, db):
    token, _ = keys.mint(db, agent="seat")
    response = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": THE_LOG_LINE},
        headers=auth(token),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["field"] == "body"
    assert "doctype" in detail["detail"]
    assert "no HTML tag anywhere" in detail["detail"], (
        "the refusal describes the rule as it actually is -- it said "
        '"no <html>, <head> or <body>" for an hour after the rule widened'
    )
    assert "wrote /tmp" not in str(detail), "and it never echoes what was sent"


def test_nothing_is_stored_by_a_refused_publish(client, db):
    token, _ = keys.mint(db, agent="seat")
    client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": THE_LOG_LINE},
        headers=auth(token),
    )
    assert client.get("/api/v1/artifacts", headers=auth(token)).json() == []


# --- what must still publish --------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        PAGE,
        "<!DOCTYPE HTML>\n<p>Shouting the doctype is still a doctype.</p>",
        "﻿<!doctype html>\n<p>A byte order mark first.</p>",
        "<!-- a note first -->\n<!doctype html>\n<p>Words.</p>",
        "<!-- one --><!-- two -->\n\n  <!doctype html><p>Words.</p>",
        "<html lang='en'><body><p>No doctype, but it says html.</p></body></html>",
        "<body><p>Only a body element.</p></body>",
        "<head><title>Only a head</title></head>",
        "<!doctype html>",
        "<title>Homelab decisions and status</title>\n<style>:root{}</style>\n<p>x</p>",
        "<main><h1>Just a fragment somebody meant</h1></main>",
    ],
    ids=[
        "an ordinary page",
        "an upper-case doctype",
        "a BOM",
        "a leading comment",
        "several leading comments",
        "html with no doctype",
        "only a body",
        "only a head",
        "a doctype and nothing else",
        "a real live page that starts with <title>",
        "a deliberate fragment",
    ],
)
def test_the_shapes_that_must_still_publish_do(db, body):
    """The bead names these: a fragment somebody meant, leading comments before
    the doctype, a BOM. Each is a page an agent could have written on purpose,
    and this guard has no opinion about any of them."""
    published = store.publish(db, "seat", "Board", body)
    assert published.id


def test_every_shim_injection_input_still_publishes(db):
    """The acceptance criteria, literally: the shim injector and this guard
    read the same regex for the same reason, so a body the injector can splice
    is a body this must accept."""
    from ream import content

    for body in (
        "<!doctype html><p>x</p>",
        "﻿  <!doctype html>\n<p>x</p>",
        "<!-- <!doctype html> --><!doctype html><p>x</p>",
        "<!--><!doctype html><p>x</p>",
        "<html><p>no doctype at all</p></html>",
    ):
        published = store.publish(db, "seat", f"Board {body[:12]}", body)
        assert content.inject(body, content.SHIM).count(content.SHIM) == 1
        assert published.id
