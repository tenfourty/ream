"""Filter the wall by who published, and say which session did it.

Jeremy, 21 Sep 2026. He answered the question of where a session should be
visible with (b) -- "on the wall too, on every tile, so you can see it without
opening anything", against my recommendation of the page only -- and then added
"ideally we can filter on the gallery by agent as well as search" and "make it
subtle in the design and look good".

Subtle is the part with a test. It means: no control at all when there is
nothing to choose, nothing new on a tile that has nothing to say, and hierarchy
from size and face rather than from dropping contrast.
"""

import re

import pytest
from fastapi.testclient import TestClient

from artifacts import store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db=db), base_url="https://testserver")


def _publish(db, agent, title, session=None):
    return store.publish(db, agent, title, PAGE, session=session)


# --- the filter ----------------------------------------------------------------


def test_the_wall_can_be_narrowed_to_one_agent(client, db):
    _publish(db, "seat-a", "A's page")
    _publish(db, "seat-b", "B's page")

    both = client.get("/").text
    assert "A&#39;s page" in both and "B&#39;s page" in both

    only_a = client.get("/?agent=seat-a").text
    assert "A&#39;s page" in only_a
    assert "B&#39;s page" not in only_a


def test_no_control_at_all_when_there_is_nothing_to_choose(client, db):
    """ "Subtle" starts with not being there. A dropdown whose only entry is the
    answer you already have is furniture, and this wall is read every day by one
    person."""
    _publish(db, "seat-a", "Only page")
    assert '<details class="menu pick' not in client.get("/").text

    _publish(db, "seat-b", "Second seat")
    assert '<details class="menu pick' in client.get("/").text


def test_choosing_a_seat_never_empties_the_menu_you_chose_it_from(client, db):
    """The choices come from the unfiltered wall. If they came from the
    filtered one, picking a seat would remove every other option and leave no
    way back except editing the URL."""
    _publish(db, "seat-a", "A's page")
    _publish(db, "seat-b", "B's page")

    page = client.get("/?agent=seat-a").text
    panel = page.split('<details class="menu pick', 1)[1].split("</details>", 1)[0]
    assert "seat-b" in panel, "the other seat vanished from its own menu"
    assert "Everyone" in panel, "and there was no way back to the whole wall"


def test_the_filter_keeps_the_search_and_the_view_you_had(client, db):
    """Three things a reader can set, and setting one must not silently drop
    the other two."""
    _publish(db, "seat-a", "Findable")
    _publish(db, "seat-b", "Other")

    page = client.get("/?q=Find&view=list").text
    panel = page.split('<details class="menu pick', 1)[1].split("</details>", 1)[0]
    hrefs = re.findall(r'href="([^"]+)"', panel)
    narrowing = [h for h in hrefs if "agent=" in h]
    assert narrowing, "no filter links at all"
    for href in narrowing:
        assert "q=Find" in href
        assert "view=list" in href


def test_search_and_the_filter_compose(client, db):
    _publish(db, "seat-a", "Shared word")
    _publish(db, "seat-b", "Shared word too")

    got = client.get("/?agent=seat-a&q=Shared").text
    assert "Shared word</span>" in got or "Shared word" in got
    assert "Shared word too" not in got


def test_a_nonsense_agent_is_an_empty_wall_not_an_error(client, db):
    _publish(db, "seat-a", "A's page")
    _publish(db, "seat-b", "B's page")
    got = client.get("/?agent=../../etc/passwd")
    assert got.status_code == 200
    assert "A&#39;s page" not in got.text


def test_the_active_filter_is_visible_without_opening_the_menu(client, db):
    """Something is being hidden from the wall, and that has to be obvious at a
    glance -- but as a state, not an alarm."""
    _publish(db, "seat-a", "A's page")
    _publish(db, "seat-b", "B's page")
    page = client.get("/?agent=seat-a").text
    summary = page.split('<details class="menu pick', 1)[1].split("</summary>", 1)[0]
    assert "seat-a" in summary
    assert 'class="menu pick on"' in page


# --- which session --------------------------------------------------------------


def test_a_tile_says_which_session_last_changed_it(client, db):
    """The SHORT half and the whole label on hover. Six characters is not a
    magic number -- it is the width at which the meta row still fits one line
    in a 304px card. At eight it wrapped, which made those tiles taller than
    the rest and left the grid ragged, and no assertion in this file noticed:
    it took rendering the wall and looking at it.
    """
    _publish(db, "seat-a", "Board", session="artifacts-seat/5ed2e1c5")
    page = client.get("/").text
    assert 'class="sess"' in page
    assert ">5ed2e1<" in page, "the distinguishing part, bounded"
    assert ">5ed2e1c5<" not in page, "longer than this and the meta row wraps"
    assert "artifacts-seat/5ed2e1c5" in page, "and the whole label on hover"


def test_a_session_label_cannot_stretch_the_meta_row(client, db):
    """A caller declares this string and may send anything within the service's
    120-character bound. The wall must not be pushed about by it."""
    _publish(db, "seat-a", "Board", session="seat/" + "z" * 100)
    page = client.get("/").text
    shown = page.split('class="sess"', 1)[1].split(">", 1)[1].split("<", 1)[0]
    assert len(shown) <= 6


def test_it_is_the_session_that_last_changed_it_not_the_one_that_made_it(client, db):
    """The tile says "Edited 5h ago" in the next slot along, so the question a
    reader is asking at that moment is who did THAT."""
    made = _publish(db, "seat-a", "Board", session="first/aaaaaa")
    store.patch(
        db,
        "seat-a",
        made.id,
        PAGE + "<!--2-->",
        made.revision,
        session="second/bbbbbb",
    )
    page = client.get("/").text
    assert ">bbbbbb<" in page
    assert ">aaaaaa<" not in page


def test_a_page_with_no_session_says_nothing_rather_than_unknown(client, db):
    """Every page published before 21 Sep has none. A column of "unknown" would
    be a column of noise on a wall that is mostly history."""
    _publish(db, "seat-a", "Older page")
    page = client.get("/").text
    assert 'class="sess"' not in page


def test_the_list_view_shows_it_too(client, db):
    _publish(db, "seat-a", "Board", session="artifacts-seat/5ed2e1c5")
    assert 'class="sess"' in client.get("/?view=list").text


def test_it_does_not_drop_contrast_to_be_quiet(client, db):
    """Hierarchy from size and face, never from fading text out. The pin on
    this same wall was made invisible by taking --muted to .5 opacity, which
    measures 1.76:1, and calling it subtle."""
    _publish(db, "seat-a", "Board", session="artifacts-seat/5ed2e1c5")
    page = client.get("/").text
    rule = page[page.index(".sess{") : page.index("}", page.index(".sess{"))]
    assert "var(--muted)" in rule, "it must use the token the meta row uses"
    assert "opacity" not in rule, "a fade is how the pin became invisible"
