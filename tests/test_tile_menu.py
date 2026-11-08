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

from ream import store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
STANDFIRST = (
    "<!doctype html><html><head><title>Board</title></head><body>"
    '<p class="standfirst">Where the backup arc stands, updated as it moves.</p>'
    "</body></html>"
)
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
# A 1x1 PNG: the wall only asks whether a thumbnail EXISTS.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db=db))


def _tile(page: str, artifact_id: str) -> str:
    """The one <li> for this page, from the wall."""
    tiles = re.findall(r"<li class=\"tile-wrap[^>]*>.*?</li>", page, re.DOTALL)
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


def test_every_row_in_the_tile_menu_is_the_same_shape(client, db):
    """Rename and Copy link used to be inline mini-forms -- a text field plus a
    button on its own line -- which made a four-item menu three different
    visual weights and twice the height it needed. Jeremy's word was janky.

    A menu is a list of actions; anything that needs typing is not a menu item,
    so those two open a dialog on the page instead. This asserts the shape,
    because "looks tidy" is not something a test can see but "every row is an
    .mrow" is."""
    published = store.publish(db, "seat", "Board", PAGE)
    tile = _tile(client.get("/").text, published.id)
    panel = tile.split('<div class="mpanel right">', 1)[1]

    assert 'class="mrow"' in panel
    assert "<input name=" not in panel, "no text field wedged into the menu"
    assert 'type="search"' not in panel
    # Every action is reachable, as a row.
    assert f'action="/a/{published.id}/pin"' in panel
    assert f'href="/a/{published.id}#rename"' in panel
    assert f'action="/a/{published.id}/duplicate"' in panel
    assert f'href="/a/{published.id}#link"' in panel
    assert f'action="/a/{published.id}/archive"' in panel
    assert f'action="/a/{published.id}/delete"' in panel


def test_copy_link_reaches_a_real_address_with_no_script(client, db):
    """There is no clipboard without script, so the scriptless answer is still
    the address itself in a field you can select -- it just lives in a dialog
    now rather than wedged into a 15rem menu panel, where it never had room to
    show a full URL."""
    published = store.publish(db, "seat", "Board", PAGE)
    tile = _tile(client.get("/").text, published.id)
    assert f'href="/a/{published.id}#link"' in tile

    page = client.get(f"/a/{published.id}").text
    assert '<div class="dlg" id="link">' in page
    assert f'value="https://artifacts.tenfourty.site/a/{published.id}"' in page


def test_no_inline_event_handler_anywhere_on_the_wall(client, db):
    """This origin's CSP is script-src 'self' with no 'unsafe-inline', so an
    inline handler is not a shortcut -- it is a handler that never runs. One
    was written here and caught before it shipped."""
    store.publish(db, "seat", "Board", PAGE)
    page = client.get("/").text
    assert not re.search(r"\son(click|focus|change|submit)=", page)


def test_every_tile_has_a_preview_area_even_with_no_picture(client, db):
    """A wall mixing pages that have a thumbnail with pages that do not came
    out visibly ragged: the ones without stretch to the row's height with their
    text stranded at the top. Every fixture had been uniform, so it took
    rendering the LIVE wall -- where pages published before thumbnails existed
    sit next to pages published after -- to see it."""
    plain = store.publish(db, "seat", "No picture", PAGE)
    shot = store.publish(db, "seat", "Has a picture", PAGE)
    store.save_thumbnail(db, shot.id, 1, _PNG)

    page = client.get("/").text
    assert 'class="shot none"' in _tile(page, plain.id), "a placeholder panel"
    assert f'src="/thumb/{shot.id}.png"' in _tile(page, shot.id), "and a real one"


# --- what is waiting on him --------------------------------------------------


def _ask(db, artifact_id, question="Which?"):
    from ream import decisions

    return decisions.ask(
        db,
        agent="seat",
        artifact_id=artifact_id,
        question=question,
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )


def test_the_header_leads_with_what_is_waiting_on_him(client, db):
    """ "5 pages" is a fact about the service; "2 questions waiting on you" is a
    fact about his afternoon, and it is the reason this service exists. So when
    anything is waiting, that is what the header is for."""
    published = store.publish(db, "seat", "Board", PAGE)
    page = client.get("/").text
    assert 'class="waiting"' not in page, "nothing waiting, nothing claimed"
    assert "1 page" in page

    _ask(db, published.id)
    _ask(db, published.id, "And the other?")
    page = client.get("/").text
    assert "2 questions waiting on you" in page
    assert "1 page" in page, "the page count stays, quietly, beside it"


def test_the_indicator_shows_him_everything_that_is_waiting(client, db):
    """It FILTERS rather than jumping to an anchor. With three pages asking,
    jumping to the first answers "where is it"; the useful question is "show me
    them", and the filtered wall answers both."""
    store.publish(db, "seat", "Nothing to answer", PAGE)
    asking = store.publish(db, "seat", "Has a question", PAGE)
    _ask(db, asking.id)

    assert 'href="/?filter=waiting"' in client.get("/").text
    shown = client.get("/?filter=waiting").text
    assert asking.id in shown
    assert "Nothing to answer" not in shown


def test_the_count_is_of_questions_not_of_pages(client, db):
    first = store.publish(db, "seat", "First", PAGE)
    second = store.publish(db, "seat", "Second", PAGE)
    _ask(db, first.id)
    _ask(db, second.id)
    page = client.get("/").text
    assert "3 questions" not in page, "two pages asking once each is two questions"
    assert "2 questions waiting on you" in page


def test_an_answered_question_stops_claiming_his_attention(client, db):
    """The count is of what is still open. A page that goes on saying "waiting
    on you" after he has answered is the exact failure that filled the top of
    his own status board with things he had already settled."""
    from ream import decisions

    published = store.publish(db, "seat", "Board", PAGE)
    asked = _ask(db, published.id)
    assert "1 question waiting on you" in client.get("/").text

    decisions.answer(db, asked.id, letter="a", note="", by="jeremy")
    page = client.get("/").text
    assert 'class="waiting"' not in page
    assert "asking" not in _tile(page, published.id).split(">", 1)[0]


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


# --- the tile as the reference has it ----------------------------------------


def test_a_tile_with_a_picture_carries_no_description(client, db):
    """The picture IS the description -- which is why the reference carries no
    tile description at all. Dropping it is what takes a card from ~350px to
    ~234px, so nine fit on a screen where three did."""
    published = store.publish(db, "seat", "Board", STANDFIRST)
    store.save_thumbnail(db, published.id, 1, _PNG)
    tile = _tile(client.get("/").text, published.id)
    assert "the backup arc stands" not in tile, "no standfirst under the title"
    assert f'src="/thumb/{published.id}.png"' in tile


def test_a_tile_with_no_picture_shows_what_the_page_is_about_instead(client, db):
    """The one case the reference never has, because its preview always
    renders. A placeholder that is merely blank would be a rectangle with a
    name on it."""
    published = store.publish(db, "seat", "Board", STANDFIRST)
    tile = _tile(client.get("/").text, published.id)
    assert 'class="shot none"' in tile
    assert "the backup arc stands" in tile


def test_the_time_is_relative_on_the_face_and_exact_in_the_attribute(client, db):
    """ "Edited 5h ago" is what a wall is read for, and it is zone-free -- the
    failure `stamp` exists to prevent (Jeremy read 08:53 UTC at 10:53 on his own
    clock). The precise, zone-correct moment stays one hover away."""
    published = store.publish(db, "seat", "Board", PAGE)
    tile = _tile(client.get("/").text, published.id)
    assert "Edited just now" in tile
    assert re.search(r'<time title="[^"]*20[0-9]{2}[^"]*">', tile), (
        "the exact stamp is still there, in the title attribute"
    )


def test_a_page_with_a_question_outranks_one_that_merely_changed(client, db):
    """The wall sorts itself by what needs him. This service exists so he can
    "interact on decisions and not just visualise progress", so a page holding
    a question he has not answered leads one that is simply newer."""
    from ream import decisions

    asking = store.publish(db, "seat", "Has a question", PAGE)
    store.publish(db, "seat", "Merely newer", PAGE)
    assert _order(client) == ["Merely newer", "Has a question"], "newest first"

    decisions.ask(
        db,
        agent="seat",
        artifact_id=asking.id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    assert _order(client)[0] == "Has a question"


def test_a_pin_still_outranks_a_question(client, db):
    """Two orderings that can disagree, and his explicit one wins. A pin is him
    saying "keep this in front of me"; a question is the service guessing that
    he will want to."""
    from ream import decisions

    asking = store.publish(db, "seat", "Has a question", PAGE)
    pinned = store.publish(db, "seat", "Pinned", PAGE)
    decisions.ask(
        db,
        agent="seat",
        artifact_id=asking.id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    store.set_pinned(db, pinned.id, True, agent=None)
    assert _order(client) == ["Pinned", "Has a question"]


def test_the_waiting_badge_rides_the_picture_not_the_body(client, db):
    """In the body it made that tile taller and the grid equalised the whole
    row to match, leaving every other card with a hand's width of nothing."""
    from ream import decisions

    published = store.publish(db, "seat", "Board", PAGE)
    decisions.ask(
        db,
        agent="seat",
        artifact_id=published.id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    tile = _tile(client.get("/").text, published.id)
    frame = re.search(
        r'<span class="frame">.*?</span>\s*<span class="body">', tile, re.DOTALL
    )
    assert frame, "the preview and its overlays are one block"
    assert 'class="wait"' in frame.group(0), "the badge is on the picture"
    body = tile.split('<span class="body">', 1)[1]
    assert 'class="wait"' not in body, "and not in the body below it"
    assert "asking" in tile.split(">", 1)[0], "and the card's edge says so"


def test_archiving_is_reachable_from_the_wall_not_only_from_the_page(client, db):
    """This was left off the tile menu for about an hour, with a test asserting
    its absence, on the grounds that the tile list should be the short one.
    True premise, wrong conclusion, and Jeremy said so: archiving is a WALL
    action -- its whole purpose is to take a page off the wall -- so needing to
    open the page to do it is backwards."""
    published = store.publish(db, "seat", "Board", PAGE)
    tile = _tile(client.get("/").text, published.id)
    assert f'action="/a/{published.id}/archive"' in tile
    assert 'value="1"' in tile, "and it would archive it"
    assert ">Archive<" in tile


def test_an_archived_tile_offers_the_way_back_not_the_way_out(client, db):
    """The same row, reversed, so the archived tab is not a place things only
    go into. A menu that still said "Archive" on an archived page would be a
    no-op dressed as an action."""
    published = store.publish(db, "seat", "Board", PAGE)
    store.set_archived(db, published.id, True, agent=None)

    page = client.get("/?filter=archived").text
    body = page.split('<div class="archived-body">', 1)[1]
    assert "Unarchive" in body
    assert ">Archive<" not in body
    assert 'name="archived" value="0"' in body, "and it would unarchive it"


def test_the_pin_button_is_not_faded_to_the_point_of_being_absent(client, db):
    """It shipped at opacity .5, which computes to 1.76:1 against a white card.
    An icon wants 3:1. Jeremy's report was "I still don't see the pin", and he
    was right -- it was not subtle, it was invisible.

    Asserting the absence of a fade rather than a contrast ratio, because the
    ratio depends on the token and the token is checked where it is defined.
    What this catches is somebody reaching for `opacity` again to make a
    control feel secondary: past about .6 that stops meaning secondary and
    starts meaning gone."""
    store.publish(db, "seat", "Board", PAGE)
    page = client.get("/").text
    rule = re.search(r"\.pinbtn button\{[^}]*\}", page)
    assert rule, "the pin button is styled"
    faded = re.search(r"opacity:(0?\.\d+)", rule.group(0))
    assert not faded, f"the resting pin is faded to {faded.group(1) if faded else ''}"


def test_every_tile_has_a_pin_button_that_is_also_the_indicator(client, db):
    """The reference has a 24x24 pin button on every card, in the meta row.
    This wall had pinning only in the menu, so it took two clicks and a read --
    Jeremy noticed it was missing.

    One control, not a control and a label: the button IS the state. Pinned and
    it is on and offers the way back; not pinned and it offers the way in."""
    published = store.publish(db, "seat", "Board", PAGE)

    tile = _tile(client.get("/").text, published.id)
    assert 'class="pinbtn"' in tile, "a pin button on an unpinned tile too"
    assert 'name="pinned" value="1"' in tile, "which would pin it"
    assert "Pin this page" in tile

    store.set_pinned(db, published.id, True, agent=None)
    tile = _tile(client.get("/").text, published.id)
    assert 'class="pinbtn on"' in tile, "and it shows the state"
    assert 'name="pinned" value="0"' in tile, "and now unpins"
    assert "Unpin this page" in tile


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
