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
    client.post(
        f"/a/{page.id}/archive",
        data={"archived": "1"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    wall = client.get("/").text
    assert page.id not in wall, "off the live wall"
    assert 'href="/?filter=archived">Archived<span class="tn">1</span>' in wall, (
        "and on its own tab, counted -- out of the way never becomes lost. "
        "It was a fold at the foot of the wall until Jeremy asked for it up "
        "in the tab row on 21 Sep."
    )
    assert page.id in client.get("/?filter=archived").text, "and reachable there"


def test_archiving_deletes_nothing(client, db, page):
    """The whole reason this is archiving and not deletion. Pages here carry the
    record of what he was asked and what he decided; tidying the gallery must
    not be the way that record is destroyed."""
    from artifacts import decisions

    asked = decisions.ask(
        db,
        agent="seat",
        artifact_id=page.id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    decisions.answer(db, asked.id, letter="a", note="his words", by="jeremy")
    client.post(
        f"/a/{page.id}/archive",
        data={"archived": "1"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )

    assert client.get(f"/a/{page.id}").status_code == 200, "the address still works"
    assert store.get_body(db, "seat", page.id).body == PAGE
    kept = decisions.list_for_artifact(db, page.id, include_answered=True)
    assert kept[0].answer == "a"
    assert kept[0].answer_note == "his words"


def test_archiving_is_reversible(client, page):
    client.post(
        f"/a/{page.id}/archive",
        data={"archived": "1"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    client.post(
        f"/a/{page.id}/archive",
        data={"archived": "0"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    wall = client.get("/").text
    assert "archived page" not in wall
    assert page.id in wall


def test_the_page_offers_the_opposite_action_once_archived(client, page):
    assert "Archive</button>" in client.get(f"/a/{page.id}").text
    client.post(
        f"/a/{page.id}/archive",
        data={"archived": "1"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert "Put back on the wall" in client.get(f"/a/{page.id}").text


# --- renaming ----------------------------------------------------------------


def test_renaming_changes_the_name_and_nothing_else(client, db, page):
    client.post(
        f"/a/{page.id}/rename",
        data={"title": "Its real name"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert "Its real name" in client.get("/").text
    assert store.get_body(db, "seat", page.id).body == PAGE, "the body is untouched"


def test_renaming_does_not_move_the_address(client, page):
    """A name that turned out wrong must never be a reason to abandon an address
    people already have."""
    before = page.id
    client.post(
        f"/a/{page.id}/rename",
        data={"title": "Something else"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert client.get(f"/a/{before}").status_code == 200


def test_an_empty_name_is_refused(client, page):
    got = client.post(
        f"/a/{page.id}/rename",
        data={"title": "   "},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert got.status_code == 422


def test_a_name_that_looks_like_a_secret_is_refused(client, page):
    """ "It is only a title" is how a field skips its checks."""
    got = client.post(
        f"/a/{page.id}/rename",
        data={"title": "key AKIAIOSFODNN7EXAMPLE"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
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
        got = client.post(
            f"/a/nope/{action}", data=data, headers=SAME_ORIGIN, follow_redirects=False
        )
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
    client.post(
        f"/a/{page.id}/archive",
        data={"archived": "1"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert "Put back on the wall" in client.get(f"/a/{page.id}").text


def test_delete_is_offered_and_destroys_nothing(client, db, page):
    """This test used to assert the OPPOSITE -- that no delete row existed --
    because he had an open question about what deleting should do and a menu
    item that destroys his decisions must not be built on a guess.

    He answered on 20 Sep 2026: (a) recoverable for thirty days, plus a
    separate destroy-it-now. So the guard has done its job and becomes a test
    of what he actually chose. The claim it defends is the same one: the row
    exists, and pressing it destroys nothing."""
    from artifacts import decisions

    asked = decisions.ask(
        db,
        agent="seat",
        artifact_id=page.id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    decisions.answer(db, asked.id, letter="a", note="his words", by="jeremy")

    html = client.get(f"/a/{page.id}").text
    assert f'action="/a/{page.id}/delete"' in html
    assert ">Delete<" in html

    client.post(f"/a/{page.id}/delete", headers=SAME_ORIGIN, follow_redirects=False)

    kept = decisions.list_for_artifact(db, page.id, include_answered=True)
    assert len(kept) == 1 and kept[0].answer_note == "his words"
    with db.connect() as conn:
        versions = conn.execute(
            "select count(*) from version where artifact_id = %s", (page.id,)
        ).fetchone()[0]
    assert versions == 1, "the body is still stored"
    assert [row.title for row in store.list_deleted(db)] == ["Original name"]


# --- the rename dialog (homelab-ij8.84.7) ------------------------------------


def test_rename_opens_a_dialog_that_needs_no_script(client, page):
    """The reference renames in a modal. A real <dialog> needs showModal(), and
    this gallery works with scripts off -- a rename that exists only when
    JavaScript ran is a rename that is missing on the day something else on the
    page threw. So it is a :target, and the whole thing is in the served HTML.
    """
    html = client.get(f"/a/{page.id}").text
    assert '<div class="dlg" id="rename">' in html, "the dialog is in the markup"
    assert 'href="#rename"' in html, "and the menu opens it with a plain link"
    assert f'action="/a/{page.id}/rename"' in html, "with the real form inside"
    assert "Original name" in html, "the current name, ready to edit"


def test_the_dialog_can_be_dismissed_without_script(client, page):
    """The backdrop and Cancel are both links back to the page. Escape is the
    only part that needs script, and it is an upgrade rather than the way out.
    """
    html = client.get(f"/a/{page.id}").text
    dialog = html.split('<div class="dlg" id="rename">', 1)[1].split("</div>", 1)[0]
    assert html.count(f'href="/a/{page.id}"') >= 2, "backdrop and Cancel"
    assert 'class="dlg-back"' in dialog


def test_the_dialog_does_not_collide_with_the_iframe_wrapper(client, page):
    """`.sheet` is this page's iframe wrapper. The dialog used that class for
    an afternoon; a fixed-position overlay on the artifact frame is not a
    subtle bug, but it is one nothing would have failed on."""
    html = client.get(f"/a/{page.id}").text
    assert '<div class="sheet" id="artifact-sheet"' in html
    assert '<div class="dlg" id="rename">' in html
    assert 'class="sheet" id="rename"' not in html


def test_renaming_still_works_from_the_dialogs_form(client, db, page):
    out = client.post(
        f"/a/{page.id}/rename",
        data={"title": "A better name"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert out.status_code == 303
    assert store.get_owned(db, "seat", page.id).title == "A better name"


def test_archiving_goes_back_to_the_wall_and_unarchiving_opens_the_page(client, page):
    """Jeremy, 28 Sep 2026: archiving from a page left him on the page it had
    just put away."""
    put_away = client.post(
        f"/a/{page.id}/archive",
        data={"archived": "1"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert put_away.status_code == 303
    assert put_away.headers["location"] == "/"
    back = client.post(
        f"/a/{page.id}/archive",
        data={"archived": "0"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert back.headers["location"] == f"/a/{page.id}"
