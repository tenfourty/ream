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

from ream import decisions, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"


def page_about(text: str) -> str:
    return (
        "<!doctype html><html><head><title>B</title></head><body>"
        f'<p class="standfirst">{text}</p></body></html>'
    )


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
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
    """ "9 pages" next to an Archived tab showing two is the count contradicting
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


def test_a_search_finds_a_match_older_than_the_newest_wall_page(
    client, db, monkeypatch
):
    """The search ran over the newest `_WALL_PAGE` rows only, so an older page that matched
    was never found (and every wall load pulled whole bodies). The match is in the query."""
    from ream import gallery

    monkeypatch.setattr(gallery, "_WALL_PAGE", 3)
    store.publish(db, "seat", "Old avatar notes", PAGE)
    for i in range(6):
        store.publish(db, "seat", f"Filler {i}", PAGE)
    found = client.get("/?q=avatar").text
    assert "Old avatar notes" in found
    assert "Filler 5" not in found


def test_a_search_term_with_like_wildcards_is_literal(client, db):
    store.publish(db, "seat", "100% done", PAGE)
    store.publish(db, "seat", "Other page", PAGE)
    found = client.get("/?q=100%25").text
    assert "100% done" in found
    assert "Other page" not in found


def test_a_search_matches_words_not_the_middle_of_words(client, db):
    store.publish(db, "seat", "Chips", page_about("Silicon and the favicon."))
    store.publish(db, "seat", "Pictures", page_about("The icons are redrawn."))
    found = client.get("/?q=icon").text
    assert "Pictures" in found
    assert "Chips" not in found


def test_every_word_of_a_search_must_match_in_any_order(client, db):
    store.publish(db, "seat", "Office avatars", page_about("Round, with a border."))
    store.publish(db, "seat", "Office chairs", page_about("Ergonomic."))
    found = client.get("/?q=border+avatars").text
    assert "Office avatars" in found
    assert "Office chairs" not in found


def test_a_title_match_comes_before_a_body_match(client, db):
    store.publish(db, "seat", "Title hit avatar", PAGE)
    store.publish(db, "seat", "Body hit", page_about("About the avatar."))
    store.publish(db, "seat", "Newest filler", page_about("avatar"))
    out = client.get("/?q=avatar").text
    assert out.index("Title hit avatar") < out.index("Body hit")
    assert out.index("Title hit avatar") < out.index("Newest filler")


def _body(text: str) -> str:
    return f"<!doctype html><html><head><title>B</title></head><body><p>{text}</p></body></html>"


def test_a_search_finds_words_a_body_stores_as_entities(client, db):
    """A body holds `R&amp;D`, `it&#39;s`, `&mdash;`; the words on the page are decoded."""
    store.publish(
        db, "seat", "Plans", _body("R&amp;D budget and &quot;quoted&quot; text")
    )
    store.publish(db, "seat", "Other", _body("Nothing"))
    assert "Plans" in client.get("/?q=R%26D").text
    assert "Plans" in client.get("/?q=%22quoted%22").text
    assert "Other" not in client.get("/?q=R%26D").text


def test_a_search_is_not_case_blind_only_for_ascii(client, db):
    store.publish(db, "seat", "Grüße", _body("Über alles in café"))
    store.publish(db, "seat", "Other", _body("nothing"))
    for q in ("%C3%BCber", "%C3%9CBER", "caf%C3%A9", "gr%C3%BC"):
        found = client.get(f"/?q={q}").text
        assert "Grüße" in found, q
        assert "Other" not in found, q


def test_false_positives_of_the_prefilter_do_not_push_a_match_past_the_limit(
    client, db, monkeypatch
):
    """`icon` is in `silicon`: the SQL keeps those rows, the word check drops them, and the
    true match older than all of them must still be listed."""
    from ream import gallery

    monkeypatch.setattr(gallery, "_WALL_PAGE", 3)
    store.publish(db, "seat", "True icons", PAGE)
    for i in range(7):
        store.publish(db, "seat", f"Silicon {i}", PAGE)
    found = client.get("/?q=icon").text
    assert "True icons" in found
    assert "Silicon 3" not in found


def test_a_search_pages_on_with_older_pages_of_matches(client, db, monkeypatch):
    from ream import gallery

    monkeypatch.setattr(gallery, "_WALL_PAGE", 2)
    for i in range(5):
        store.publish(db, "seat", f"Match {i}", PAGE)
        store.publish(db, "seat", f"Filler {i}", PAGE)
    first = client.get("/?q=match").text
    assert "Match 4" in first and "Match 3" in first and "Match 2" not in first
    older = re.search(r'href="([^"]*older=[^"]*)"', first)
    assert older, "an Older pages link"
    second = client.get(older.group(1).replace("&amp;", "&")).text
    assert "Match 2" in second and "Match 1" in second and "Match 3" not in second


def test_the_agent_filter_is_applied_before_the_limit(client, db, monkeypatch):
    from ream import gallery

    monkeypatch.setattr(gallery, "_WALL_PAGE", 2)
    store.publish(db, "quiet-seat", "Mine avatar", PAGE)
    for i in range(5):
        store.publish(db, "busy-seat", f"Theirs avatar {i}", PAGE)
    found = client.get("/?q=avatar&agent=quiet-seat").text
    assert "Mine avatar" in found
    assert "Theirs avatar" not in found


def test_the_agent_picker_and_counts_ignore_the_search(client, db):
    store.publish(db, "seat-one", "Photo", PAGE)
    store.publish(db, "seat-two", "Zebrafish ledger", PAGE)
    found = client.get("/?q=photo").text
    assert "Zebrafish ledger" not in found
    assert "seat-two" in found, "the picker still lists every agent on the wall"


def test_a_word_with_a_quote_pages_across_batches_and_older_pages(
    client, db, monkeypatch
):
    """`it's` is prefiltered on `it`, so `Item` rows pass the SQL and fail the word check.
    With two rows to a page and two batches to a search, the cursor must hand over cleanly:
    every match once, in order, whichever batch or "Older pages" click it falls in."""
    from ream import gallery

    monkeypatch.setattr(gallery, "_WALL_PAGE", 2)
    monkeypatch.setattr(gallery, "_SEARCH_BATCHES", 2)
    for i in range(6):
        store.publish(db, "seat", f"It's match {i}", PAGE)
        for j in range(2):
            store.publish(db, "seat", f"Item filler {i}{j}", PAGE)
    seen: list[str] = []
    url = "/?q=it%27s"
    for _ in range(20):
        out = client.get(url).text
        assert "Item filler" not in out
        seen += dict.fromkeys(re.findall(r"It(?:'|&#x27;|&#39;)s match (\d)", out))
        # "Older pages" is the link carrying `older` that is not the Select toggle.
        links = [
            h
            for h in re.findall(r'href="([^"]*older=[^"]*)"', out)
            if "select=1" not in h
        ]
        if not links:
            break
        url = links[-1].replace("&amp;", "&")
    assert seen == ["5", "4", "3", "2", "1", "0"]


def test_a_word_with_no_ascii_letters_is_still_found(client, db):
    store.publish(db, "seat", "Dash", _body("one &mdash; two"))
    store.publish(db, "seat", "Plain", _body("one two"))
    found = client.get("/?q=%E2%80%94").text
    assert "Dash" in found
    assert "Plain" not in found


# --- the entity-aware prefilter (28 Nov 2026) --------------------------------


def test_raw_spellings_list_every_stored_form_of_a_word():
    from ream.text import raw_spellings

    assert raw_spellings("it's") == ("it's", "it&#39;s", "it&amp;#39;s")
    assert raw_spellings("r&d") == ("r&d", "r&amp;d")
    assert raw_spellings("plain") == ("plain",)
    # Cased non-ASCII cannot be listed (SQLite folds only ASCII): the caller falls back.
    assert raw_spellings("café") is None
    assert raw_spellings("it’s") == ("it’s",)
    # Too many apostrophes: past the limit, fall back rather than build a huge clause.
    assert raw_spellings("'" * 8) is None


@pytest.mark.parametrize(
    ("stored", "query"),
    [
        ("it&#39;s fine", "it's"),
        ("It&#39;s fine", "IT'S"),
        ("it's fine", "it's"),
        ("it&amp;#39;s fine", "it's"),
        ("R&amp;D budget", "R&D"),
        ("r&d budget", "r&d"),
        ("R&amp;amp;D budget", "r&amp;d"),
        ("a &lt;b&gt; c", "<b>"),
        ("say &quot;hi&quot;", '"hi"'),
        ("it’s fine", "it’s"),
        ("fine &mdash; done", "fine"),
    ],
)
def test_the_prefilter_keeps_every_spelling_the_matcher_accepts(
    client, db, stored, query
):
    """The SQL may only narrow: a page `_matches` accepts must reach it, in whatever
    spelling its body stores the word."""
    from urllib.parse import quote

    from ream import gallery

    store.publish(db, "seat", "Target", _body(stored))
    store.publish(db, "seat", "Decoy", _body("nothing relevant here"))
    row = (
        "id",
        "Target",
        None,
        "seat",
        1,
        0,
        _body(stored),
    )
    accepted = gallery._matches(row, query.lower())
    found = client.get(f"/?q={quote(query)}").text
    assert ("Target" in found) == accepted
    assert "Decoy" not in found


def test_entity_spelled_words_find_their_pages_and_nothing_else(client, db):
    store.publish(db, "seat", "Apos", _body("it&#39;s done"))
    store.publish(db, "seat", "Plain", _body("it's done"))
    store.publish(db, "seat", "Fragment", _body("item sits in the r&amp;e column"))
    store.publish(db, "seat", "Amp", _body("R&amp;D"))
    store.publish(db, "seat", "Other", _body("nothing"))
    found = client.get("/?q=it%27s").text
    assert "Apos" in found and "Plain" in found
    assert "Fragment" not in found and "Other" not in found
    found = client.get("/?q=r%26d").text
    assert "Amp" in found
    assert "Fragment" not in found


def test_the_prefilter_narrows_a_word_with_a_quote_to_pages_that_can_hold_it(
    client, db, monkeypatch
):
    """`it's` used to be asked of the SQL as `it`, which every `Item` page passed. Count the
    rows the SQL hands the word check: only the pages that can hold the word now."""
    from ream import gallery

    store.publish(db, "seat", "Item list", _body("item sits"))
    store.publish(db, "seat", "Hit", _body("it&#39;s here"))
    seen: list[str] = []
    real = gallery._matches

    def spy(row, needle):
        seen.append(row[1])
        return real(row, needle)

    monkeypatch.setattr(gallery, "_matches", spy)
    assert "Hit" in client.get("/?q=it%27s").text
    assert "Item list" not in seen
    assert "Hit" in seen


def test_search_results_are_the_same_whatever_the_batch_size(client, db, monkeypatch):
    """Wider later batches change how many passes a search makes, never what it finds."""
    from ream import gallery

    for i in range(7):
        store.publish(db, "seat", f"Item {i}", _body(f"item {i}"))
        store.publish(db, "seat", f"Quote {i}", _body(f"it&#39;s {i}"))
    monkeypatch.setattr(gallery, "_WALL_PAGE", 3)

    def titles(query):
        seen = []
        url = f"/?q={query}"
        for _ in range(20):
            out = client.get(url).text
            seen += re.findall(r"<span class=\"t\">(Quote \d)</span>", out)
            links = [
                h
                for h in re.findall(r'href="([^"]*older=[^"]*)"', out)
                if "select=1" not in h
            ]
            if not links:
                return seen
            url = links[-1].replace("&amp;", "&")
        return seen

    wide = titles("it%27s")
    monkeypatch.setattr(gallery, "_SEARCH_GROWTH", 1)
    assert titles("it%27s") == wide
    assert sorted(wide) == sorted(f"Quote {i}" for i in range(7))


def test_a_percent_or_underscore_in_a_word_is_literal(client, db):
    store.publish(db, "seat", "Pct", _body("grew 50% last year"))
    store.publish(db, "seat", "Und", _body("a_b is a name"))
    store.publish(db, "seat", "Other", _body("grew 500 last year; axb"))
    assert "Pct" in client.get("/?q=50%25").text
    assert "Other" not in client.get("/?q=50%25").text
    out = client.get("/?q=a_b").text
    assert "Und" in out and "Other" not in out
