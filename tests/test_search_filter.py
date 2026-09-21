"""Finding a page: the search box, and the tabs beside it.

homelab-ij8.84.5, plus the filter tabs that came with the header being split
into two rows. The reference's tabs are All / Yours / Shared with you, which do
not map here -- every page is his. These are the questions worth asking of this
wall, and the middle one is the reason the service exists.

Both are GET: a search and a filter are each a URL he can keep, and both work
with scripts off.
"""

import re

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


def _tile_titles(out: str) -> set[str]:
    """The titles actually rendered as tiles or rows.

    Asserting a bare word is absent from a 40 KB document is a test that fails
    on prose: on 21 Sep a CSS comment containing the word "Quiet" broke this,
    and the page under test was entirely correct. Match the element instead.
    """
    return set(re.findall(r'<span class="t">([^<]*)</span>', out)) | set(
        re.findall(r'<span class="rt">([^<]*)</span>', out)
    )


def test_the_waiting_tab_shows_only_pages_with_a_question(client, db):
    asking = store.publish(db, "seat", "Has a question", PAGE)
    store.publish(db, "seat", "Quiet", PAGE)
    _ask(db, asking.id)
    titles = _tile_titles(client.get("/?filter=waiting").text)
    assert "Has a question" in titles
    assert "Quiet" not in titles


def test_the_pinned_tab_shows_only_pinned_pages(client, db):
    pinned = store.publish(db, "seat", "Kept", PAGE)
    store.publish(db, "seat", "Ordinary", PAGE)
    store.set_pinned(db, pinned.id, True, agent=None)
    titles = _tile_titles(client.get("/?filter=pinned").text)
    assert "Kept" in titles
    assert "Ordinary" not in titles


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


def test_a_search_reaches_the_archived_tab_too(client, db):
    """Searching only the live wall would hide the archived hit, which reads as
    the search having missed something. The empty live wall says where the
    match is and links straight to it."""
    gone = store.publish(db, "seat", "Archived thing", PAGE)
    other = store.publish(db, "seat", "Unrelated", PAGE)
    store.set_archived(db, gone.id, True, agent=None)
    store.set_archived(db, other.id, True, agent=None)

    live = client.get("/?q=archived").text
    assert "is a match in the archived pages" in live
    assert 'href="/?filter=archived&q=archived&view=grid"' in live, "and a way to it"

    on_tab = client.get("/?filter=archived&q=archived").text
    assert _tile_titles(on_tab) == {"Archived thing"}, "the tab shows only the match"
    assert "1 page" in on_tab, "and the count follows the tab it is beside"


# --- archived is a tab, not a fold at the foot of the page --------------------
#
# Jeremy, 21 Sep 2026: "can you move the archived toggle that is at the bottom
# of the page to be one of the tabs at the top alongside All, Waiting on you
# and Deleted? same look and feel."


def test_the_archived_tab_appears_only_once_something_is_archived(client, db):
    kept = store.publish(db, "seat", "Board", PAGE)
    assert ">Archived<" not in client.get("/").text
    store.set_archived(db, kept.id, True, agent=None)
    assert 'href="/?filter=archived">Archived<span class="tn">1</span>' in (
        client.get("/").text
    )


def test_the_archived_tab_shows_only_archived_pages(client, db):
    gone = store.publish(db, "seat", "Put away", PAGE)
    store.publish(db, "seat", "On the wall", PAGE)
    store.set_archived(db, gone.id, True, agent=None)
    titles = _tile_titles(client.get("/?filter=archived").text)
    assert titles == {"Put away"}


def test_an_archived_page_still_looks_archived_on_its_tab(client, db):
    """The skin is what survived the move: a page off the wall must not look
    like one on it, whichever tab reached it."""
    gone = store.publish(db, "seat", "Put away", PAGE)
    store.set_archived(db, gone.id, True, agent=None)
    assert '<div class="archived-body">' in client.get("/?filter=archived").text


def test_the_count_beside_the_search_follows_the_tab(client, db):
    """"9 pages" next to an Archived tab showing two is the count contradicting
    what is under it."""
    for n in range(3):
        store.publish(db, "seat", f"Live {n}", PAGE)
    gone = store.publish(db, "seat", "Put away", PAGE)
    store.set_archived(db, gone.id, True, agent=None)
    assert "3 pages" in client.get("/").text
    assert "1 page<" in client.get("/?filter=archived").text


def test_an_empty_archived_tab_says_so_rather_than_showing_nothing(client, db):
    store.publish(db, "seat", "Board", PAGE)
    assert "Nothing is archived" in client.get("/?filter=archived").text


def test_the_wall_with_everything_archived_points_at_the_tab(client, db):
    """It used to say "anything archived is below", which stops being true the
    moment the fold is a tab."""
    gone = store.publish(db, "seat", "Put away", PAGE)
    store.set_archived(db, gone.id, True, agent=None)
    out = client.get("/").text
    assert 'href="/?filter=archived&view=grid">1 archived</a>' in out
    assert "below" not in out.split("<style>")[0] + out.split("</style>")[-1]


def test_the_archived_tab_keeps_the_view_it_was_asked_for(client, db):
    gone = store.publish(db, "seat", "Put away", PAGE)
    store.set_archived(db, gone.id, True, agent=None)
    assert '<ul class="rows">' in client.get("/?filter=archived&view=list").text
