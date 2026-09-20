"""Taking a page off the wall, and giving it a different name.

Jeremy, 20 Sep 2026: "I would also like the ability to archive so that they
don't appear in my gallery", and asking whether rename and delete existed.
Rename existed only by republishing the whole body with --title, which is fine
for an agent holding the file and impossible for him in a browser. Archive did
not exist. Delete still does not, deliberately — it is a separate question
because it would take his answers with it.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


@pytest.fixture()
def page(db):
    keys.mint(db, agent="seat")
    return store.publish(db, "seat", "Original name", PAGE)


# --- archiving ---------------------------------------------------------------


def test_an_archived_page_leaves_the_wall(client, page):
    assert page.id in client.get("/").text
    client.post(f"/a/{page.id}/archive", data={"archived": "1"},
                headers=SAME_ORIGIN, follow_redirects=False)
    wall = client.get("/").text
    assert "1 archived page" in wall, "counted, so out of the way never becomes lost"


def test_archiving_deletes_nothing(client, db, page):
    """The whole reason this is archiving and not deletion. Pages here carry the
    record of what he was asked and what he decided; tidying the gallery must
    not be the way that record is destroyed."""
    from artifacts import decisions

    asked = decisions.ask(
        db, agent="seat", artifact_id=page.id, question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    decisions.answer(db, asked.id, letter="a", note="his words", by="jeremy")
    client.post(f"/a/{page.id}/archive", data={"archived": "1"},
                headers=SAME_ORIGIN, follow_redirects=False)

    assert client.get(f"/a/{page.id}").status_code == 200, "the address still works"
    assert store.get_body(db, "seat", page.id).body == PAGE
    kept = decisions.list_for_artifact(db, page.id, include_answered=True)
    assert kept[0].answer == "a"
    assert kept[0].answer_note == "his words"


def test_archiving_is_reversible(client, page):
    client.post(f"/a/{page.id}/archive", data={"archived": "1"},
                headers=SAME_ORIGIN, follow_redirects=False)
    client.post(f"/a/{page.id}/archive", data={"archived": "0"},
                headers=SAME_ORIGIN, follow_redirects=False)
    wall = client.get("/").text
    assert "archived page" not in wall
    assert page.id in wall


def test_the_page_offers_the_opposite_action_once_archived(client, page):
    assert "Archive</button>" in client.get(f"/a/{page.id}").text
    client.post(f"/a/{page.id}/archive", data={"archived": "1"},
                headers=SAME_ORIGIN, follow_redirects=False)
    assert "Put back on the wall" in client.get(f"/a/{page.id}").text


# --- renaming ----------------------------------------------------------------


def test_renaming_changes_the_name_and_nothing_else(client, db, page):
    client.post(f"/a/{page.id}/rename", data={"title": "Its real name"},
                headers=SAME_ORIGIN, follow_redirects=False)
    assert "Its real name" in client.get("/").text
    assert store.get_body(db, "seat", page.id).body == PAGE, "the body is untouched"


def test_renaming_does_not_move_the_address(client, page):
    """A name that turned out wrong must never be a reason to abandon an address
    people already have."""
    before = page.id
    client.post(f"/a/{page.id}/rename", data={"title": "Something else"},
                headers=SAME_ORIGIN, follow_redirects=False)
    assert client.get(f"/a/{before}").status_code == 200


def test_an_empty_name_is_refused(client, page):
    got = client.post(f"/a/{page.id}/rename", data={"title": "   "},
                      headers=SAME_ORIGIN, follow_redirects=False)
    assert got.status_code == 422


def test_a_name_that_looks_like_a_secret_is_refused(client, page):
    """"It is only a title" is how a field skips its checks."""
    got = client.post(f"/a/{page.id}/rename",
                      data={"title": "key AKIAIOSFODNN7EXAMPLE"},
                      headers=SAME_ORIGIN, follow_redirects=False)
    assert got.status_code == 422


# --- neither is reachable from somewhere else --------------------------------


@pytest.mark.parametrize("action", ["archive", "rename"])
def test_a_cross_site_form_cannot_do_either(client, page, action):
    """Both change state using Jeremy's session, so a form on someone else's
    page must not be able to make the change."""
    got = client.post(
        f"/a/{page.id}/{action}",
        data={"archived": "1", "title": "hijacked"},
        headers={"sec-fetch-site": "cross-site"},
        follow_redirects=False,
    )
    assert got.status_code == 403


def test_an_unknown_page_is_a_404(client):
    for action, data in (("archive", {"archived": "1"}), ("rename", {"title": "x"})):
        got = client.post(f"/a/nope/{action}", data=data,
                          headers=SAME_ORIGIN, follow_redirects=False)
        assert got.status_code == 404


# --- the page bar (homelab-ij8.84.1) -----------------------------------------


def test_the_title_carries_the_actions(client, page):
    """The reference puts the actions on the title, not at the bottom of the
    page. The previous version was a fold of unstyled form controls below
    everything, which is what Jeremy meant by "wow that ux is ugly"."""
    html = client.get(f"/a/{page.id}").text
    assert 'class="menu"' in html
    assert "Page by seat" in html, "who published it"
    assert "/rename" in html and "/archive" in html
    assert "All pages" in html
    assert "housekeeping" not in html, "the old bottom fold is gone"


def test_the_menu_needs_no_script(client, page):
    """A menu that opens only with JavaScript is a menu that is missing on the
    day something else on the page has thrown. <details> opens either way."""
    html = client.get(f"/a/{page.id}").text
    start = html.index('<details class="menu"')
    assert "<summary" in html[start : start + 200]


def test_an_archived_page_offers_the_opposite_action_in_the_menu(client, page):
    assert "Archive</button>" in client.get(f"/a/{page.id}").text
    client.post(f"/a/{page.id}/archive", data={"archived": "1"},
                headers=SAME_ORIGIN, follow_redirects=False)
    assert "Put back on the wall" in client.get(f"/a/{page.id}").text


def test_no_delete_row_until_he_has_decided(client, page):
    """He has an open question about what deleting should do. A menu item that
    destroys his decisions must not be built on a guess, and a test is the way
    that stays true when someone adds the other rows later."""
    html = client.get(f"/a/{page.id}").text
    assert "/delete" not in html
    assert ">Delete" not in html
