"""The index is a wall of tiles that say something, not a list of links.

homelab-ij8.22. Jeremy, 19 Sep 2026: the index is bare; he wants tiles with the
title and some detail. He chose a real rendered thumbnail and qualified it --
"start with (b) but then (a) if we can" -- so this is (b): everything a tile can
carry without a browser.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import store, text
from artifacts.app import create_gallery_app

STANDFIRST = (
    "<!doctype html><html><head><title>Board</title></head><body>"
    '<p class="standfirst">Where the backup arc stands, updated as it moves.</p>'
    "<p>Something else entirely.</p></body></html>"
)
BARE = "<!doctype html><html><head><title>Bare</title></head><body><h1>Hi</h1></body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_DISPLAY_TZ", "Europe/Paris")
    return TestClient(create_gallery_app(db=db))


def test_a_tile_says_what_the_page_is_about(client, db):
    store.publish(db, "seat-a", "The backups", STANDFIRST)
    page = client.get("/").text
    assert "Where the backup arc stands, updated as it moves." in page


def test_a_page_that_says_nothing_gets_no_invented_description(client, db):
    """An empty description is honest; a guessed one puts words in an agent's
    mouth on a page Jeremy reads as that agent's own."""
    store.publish(db, "seat-a", "Bare", BARE)
    page = client.get("/").text
    assert "Bare" in page
    assert 'class="d"' not in page


def test_the_owning_agent_is_on_the_tile(client, db):
    store.publish(db, "warpgate-arc", "Theirs", STANDFIRST)
    assert "warpgate-arc" in client.get("/").text


def test_the_time_is_in_the_readers_zone_not_the_databases(client, db):
    """The index printed a raw database timestamp sliced to 16 characters -- UTC,
    looking exactly like a local time. That is the failure Jeremy named on a page
    ("your status time is back to UTC"), and it was still live on the index after
    the pages were fixed. CEST or CET, never a bare ISO string."""
    store.publish(db, "seat-a", "Timed", STANDFIRST)
    page = client.get("/").text
    assert "CEST" in page or "CET" in page
    assert "T00:00" not in page
    # A raw psycopg timestamp renders as "2026-09-20 12:34:56+00:00".
    assert "+00:00" not in page


def test_the_counts_are_not_multiplied_by_each_other(client, db):
    """Two joins onto one row multiply it, and the numbers merely look large.

    A page with several versions and several open questions is the case that
    exposes it: a join to `version` fans the artifact row out, a join to
    `decision` fans it again, and every count comes back as the product. This
    asserts the real numbers rather than the absence of an error, because the
    wrong answer is a plausible-looking number and not a crash.
    """
    published = store.publish(db, "seat-a", "Busy", STANDFIRST)
    revision = published.revision
    for n in range(3):
        updated = store.patch(
            db, "seat-a", published.id, STANDFIRST + f"<!--{n}-->", revision
        )
        revision = updated.revision
    from artifacts import decisions

    for n in range(2):
        decisions.ask(
            db,
            agent="seat-a",
            artifact_id=published.id,
            question=f"Question {n}?",
            options=[{"letter": "a", "label": "Yes"}, {"letter": "b", "label": "No"}],
            recommended="a",
        )
    page = client.get("/").text
    assert "v4" in page, "4 versions: one publish plus three updates"
    assert '<span class="wait">2 questions for you</span>' in page
    assert "v8" not in page and "8 questions" not in page


def test_one_open_question_is_singular(client, db):
    from artifacts import decisions

    published = store.publish(db, "seat-a", "Asking", STANDFIRST)
    decisions.ask(
        db,
        agent="seat-a",
        artifact_id=published.id,
        question="Just the one?",
        options=[{"letter": "a", "label": "Yes"}, {"letter": "b", "label": "No"}],
        recommended="a",
    )
    assert '<span class="wait">1 question for you</span>' in client.get("/").text


def test_an_answered_question_stops_shouting(client, db):
    from artifacts import decisions

    published = store.publish(db, "seat-a", "Answered", STANDFIRST)
    asked = decisions.ask(
        db,
        agent="seat-a",
        artifact_id=published.id,
        question="Settled?",
        options=[{"letter": "a", "label": "Yes"}, {"letter": "b", "label": "No"}],
        recommended="a",
    )
    decisions.answer(db, asked.id, letter="a", note="", by="jeremy")
    page = client.get("/").text
    # The BADGE, not the phrase: the gallery's own stylesheet carries a comment
    # explaining the badge, so a substring search for "for you" matches the CSS
    # and passes or fails for reasons that have nothing to do with the tile.
    # Caught by this test failing in the full suite while passing alone.
    assert 'class="wait"' not in page


def test_a_page_cannot_put_markup_on_the_gallery_origin(client, db):
    """The description is extracted from agent HTML and lands on the gallery's
    own origin, which holds Jeremy's session. It is plain text and it is escaped
    here; nothing an agent writes may become an element."""
    nasty = (
        "<!doctype html><html><body>"
        '<p class="standfirst">hello <img src=x onerror=alert(1)> and '
        "&lt;script&gt;bad&lt;/script&gt; done</p></body></html>"
    )
    store.publish(db, "seat-a", "Nasty <b>title</b>", nasty)
    page = client.get("/").text
    assert "onerror" not in page
    assert "<img" not in page
    assert "<b>title</b>" not in page
    assert "&lt;b&gt;title&lt;/b&gt;" in page


def test_describe_prefers_the_pages_own_statement_of_intent():
    meta = (
        '<html><head><meta name="description" content="The explicit one">'
        '</head><body><p class="standfirst">The standfirst one</p></body></html>'
    )
    assert text.describe(meta) == "The explicit one"


def test_describe_ignores_prose_inside_comments():
    """Every template here carries REPLACE guidance in HTML comments, and the
    first of those would otherwise become the page's description on the wall."""
    commented = (
        "<html><body><!-- REPLACE: one sentence saying what this page is and "
        "who it is for, in plain words --><p>The real first paragraph here.</p>"
        "</body></html>"
    )
    assert text.describe(commented) == "The real first paragraph here."


def test_describe_returns_nothing_rather_than_a_fragment():
    assert text.describe("<html><body><p>Hi</p></body></html>") == ""
    assert text.describe("<html><body></body></html>") == ""
