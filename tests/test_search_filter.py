"""Finding a page: the search box, and the tabs beside it.

homelab-ij8.84.5, plus the filter tabs that came with the header being split
into two rows. The reference's tabs are All / Yours / Shared with you, which do
not map here -- every page is his. These are the questions worth asking of this
wall, and the middle one is the reason the service exists.

Both are GET: a search and a filter are each a URL he can keep, and both work
with scripts off.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"


def page_about(text: str) -> str:
    return (
        "<!doctype html><html><head><title>B</title></head><body>"
        f'<p class="standfirst">{text}</p></body></html>'
    )


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db=db), base_url="https://testserver")


def _ask(db, artifact_id):
    return decisions.ask(
        db,
        agent="seat",
        artifact_id=artifact_id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )


# --- search ------------------------------------------------------------------


def test_a_search_matches_the_title(client, db):
    store.publish(db, "seat", "Photo library backup", PAGE)
    store.publish(db, "seat", "Warpgate certificates", PAGE)
    found = client.get("/?q=photo").text
    assert "Photo library backup" in found
    assert "Warpgate certificates" not in found


def test_a_search_matches_the_words_on_the_page(client, db):
    """The wall already extracts a description from each body, so the body is
    there to search -- and a page is often remembered by what it said rather
    than by what it was called."""
    store.publish(db, "seat", "Board", page_about("The upload finishes on Tuesday."))
    store.publish(db, "seat", "Other", page_about("Nothing to do with it."))
    found = client.get("/?q=tuesday").text
    assert "Board" in found
    assert "Other" not in found


def test_a_search_matches_the_owning_agent(client, db):
    """On a wall with a dozen seats, "what has warpgate-arc published" is worth
    being able to ask, and it is the same keystrokes as searching for a title."""
    store.publish(db, "warpgate-arc", "One", PAGE)
    store.publish(db, "backup-arc", "Two", PAGE)
    found = client.get("/?q=warpgate").text
    assert "One" in found
    assert "Two" not in found


def test_a_search_never_matches_markup(client, db):
    """`describe` strips tags before matching, so a query cannot hit a class
    name or an SVG path -- a hit a reader cannot see anywhere on the page is
    the worst kind, because it reads as the search being broken."""
    store.publish(
        db,
        "seat",
        "Board",
        "<!doctype html><html><head><title>B</title>"
        "<style>.standfirst{color:rebeccapurple}</style></head>"
        "<body><p>Plain words.</p></body></html>",
    )
    assert "Board" not in client.get("/?q=rebeccapurple").text
    assert "Board" in client.get("/?q=plain").text


def test_the_search_is_case_insensitive(client, db):
    store.publish(db, "seat", "Photo Library", PAGE)
    assert "Photo Library" in client.get("/?q=PHOTO").text


def test_an_empty_result_says_so_and_offers_the_way_back(client, db):
    """Never a bare empty wall: with a query in the box that looks exactly like
    everything having been deleted."""
    store.publish(db, "seat", "Board", PAGE)
    out = client.get("/?q=nothingmatchesthis").text
    assert "Nothing here matches" in out
    assert "nothingmatchesthis" in out, "it says what was searched for"
    assert 'href="/?view=grid"' in out, "and offers the way back"


def test_a_search_keeps_the_view_it_was_made_in(client, db):
    """Without this, searching silently throws him back to the grid, which reads
    as the search having done something odd."""
    store.publish(db, "seat", "Board", PAGE)
    out = client.get("/?q=board&view=list")
    assert '<ul class="rows">' in out.text
    assert 'name="view" value="list"' in out.text, "and the box carries it onward"


def test_a_search_filters_the_archived_fold_too(client, db):
    """Searching only the live wall would hide the archived hit and still count
    it in the fold's total, which reads as the search having missed something."""
    gone = store.publish(db, "seat", "Archived thing", PAGE)
    other = store.publish(db, "seat", "Unrelated", PAGE)
    store.set_archived(db, gone.id, True, agent=None)
    store.set_archived(db, other.id, True, agent=None)
    out = client.get("/?q=archived").text
    assert '<span class="n">1 page</span>' in out, "the fold counts only the match"


# --- the tabs ----------------------------------------------------------------


def test_a_tab_appears_only_once_it_has_something_in_it(client, db):
    """A tab that is always empty teaches him to ignore the row it sits in, and
    that row is also where search lives."""
    published = store.publish(db, "seat", "Board", PAGE)
    plain = client.get("/").text
    assert "Waiting on you" not in plain
    assert ">Pinned<" not in plain

    _ask(db, published.id)
    store.set_pinned(db, published.id, True, agent=None)
    both = client.get("/").text
    assert "Waiting on you" in both
    assert "Pinned" in both


def test_the_waiting_tab_shows_only_pages_with_a_question(client, db):
    asking = store.publish(db, "seat", "Has a question", PAGE)
    store.publish(db, "seat", "Quiet", PAGE)
    _ask(db, asking.id)
    out = client.get("/?filter=waiting").text
    assert "Has a question" in out
    assert "Quiet" not in out


def test_the_pinned_tab_shows_only_pinned_pages(client, db):
    pinned = store.publish(db, "seat", "Kept", PAGE)
    store.publish(db, "seat", "Ordinary", PAGE)
    store.set_pinned(db, pinned.id, True, agent=None)
    out = client.get("/?filter=pinned").text
    assert "Kept" in out
    assert "Ordinary" not in out


def test_the_tab_counts_do_not_change_when_you_press_them(client, db):
    """Counted before filtering. Otherwise "Waiting on you 3" shows 3 of 3 and
    the number stops meaning anything the moment it is used."""
    asking = store.publish(db, "seat", "Has a question", PAGE)
    store.publish(db, "seat", "Quiet", PAGE)
    _ask(db, asking.id)
    assert "2 questions" not in client.get("/").text
    on_tab = client.get("/?filter=waiting").text
    assert "1 question waiting on you" in on_tab, "the alert still counts everything"


def test_a_nonsense_filter_shows_everything_rather_than_failing(client, db):
    store.publish(db, "seat", "Board", PAGE)
    out = client.get("/?filter=../../etc/passwd")
    assert out.status_code == 200
    assert "Board" in out.text
