"""An agent can take the same actions on its own pages that Jeremy can on his wall.

His rule, 20 Sep 2026, in one line: "ideally agents can take all the same
actions a human can." Archive, rename and pin had arrived as gallery forms --
things he does in a browser, behind his login -- and an agent that wanted any of
them had no way to ask. Renaming meant sending the whole body again with
--title; archiving and pinning were simply unavailable, so a seat could publish
a page and then never tidy up after itself.

The half of the rule that is NOT symmetry is in here too, and it is the half
worth testing: his controls are his because the wall is his, and these are
scoped to the calling key's own agent. One seat must never reach another's page
-- so the interesting tests below are the refusals, not the successes.
"""

import pytest
from fastapi.testclient import TestClient

from ream import keys, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


@pytest.fixture()
def mine(db):
    """A page owned by `seat`, and that seat's key."""
    token, _ = keys.mint(db, agent="seat")
    return token, store.publish(db, "seat", "Original name", PAGE)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- the three actions, on a page the caller owns ----------------------------


def test_an_agent_archives_its_own_page_and_puts_it_back(client, db, mine):
    token, page = mine
    on = client.post(
        f"/api/v1/artifacts/{page.id}/archive",
        json={"archived": True},
        headers=auth(token),
    )
    assert on.status_code == 200
    assert on.json()["archived_at"] is not None, "the answer says what it now is"
    wall = client.get("/").text
    assert 'href="/?filter=archived">Archived<span class="tn">1</span>' in wall, (
        "counted on its own tab, so it is not lost"
    )
    assert page.id in client.get("/?filter=archived").text

    off = client.post(
        f"/api/v1/artifacts/{page.id}/archive",
        json={"archived": False},
        headers=auth(token),
    )
    assert off.status_code == 200
    assert off.json()["archived_at"] is None
    assert page.id in client.get("/").text, "and it is back on the wall"


def test_an_agent_renames_its_own_page_without_sending_the_body(client, db, mine):
    token, page = mine
    out = client.post(
        f"/api/v1/artifacts/{page.id}/rename",
        json={"title": "A better name"},
        headers=auth(token),
    )
    assert out.status_code == 200
    assert out.json()["title"] == "A better name"
    assert store.get_body(db, "seat", page.id).body == PAGE, (
        "the page itself is untouched"
    )


def test_renaming_makes_no_new_version(client, db, mine):
    """Shelf state, not a revision. A rename that minted a version would put an
    entry in the history that says nothing about what the page said, and would
    move `revision` under any agent holding one for a PATCH."""
    token, page = mine
    client.post(
        f"/api/v1/artifacts/{page.id}/rename",
        json={"title": "Renamed"},
        headers=auth(token),
    )
    row = store.get_owned(db, "seat", page.id)
    assert row.current_version == 1
    assert row.revision == page.revision


def test_an_agent_pins_its_own_page(client, db, mine):
    token, page = mine
    out = client.post(
        f"/api/v1/artifacts/{page.id}/pin", json={"pinned": True}, headers=auth(token)
    )
    assert out.status_code == 200
    assert out.json()["pinned_at"] is not None
    off = client.post(
        f"/api/v1/artifacts/{page.id}/pin", json={"pinned": False}, headers=auth(token)
    )
    assert off.json()["pinned_at"] is None


# --- the scope: another seat's page is not reachable --------------------------


@pytest.fixture()
def someone_elses(db):
    """A page owned by `other`, and a DIFFERENT seat's key to poke it with."""
    keys.mint(db, agent="other")
    intruder, _ = keys.mint(db, agent="intruder")
    return intruder, store.publish(db, "other", "Not yours", PAGE)


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("archive", {"archived": True}),
        ("rename", {"title": "hijacked"}),
        ("pin", {"pinned": True}),
    ],
)
def test_a_seat_cannot_touch_another_seats_page(
    client, db, someone_elses, action, payload
):
    intruder, page = someone_elses
    out = client.post(
        f"/api/v1/artifacts/{page.id}/{action}", json=payload, headers=auth(intruder)
    )
    assert out.status_code == 404
    row = store.get_owned(db, "other", page.id)
    assert row.title == "Not yours"
    assert row.archived_at is None
    assert row.pinned_at is None


def test_a_page_that_is_not_yours_looks_exactly_like_one_that_is_not_there(
    client, db, someone_elses
):
    """Deliberately indistinguishable. A different answer for "exists but is not
    yours" would turn this route into an oracle: a key holder could walk ids and
    learn which ones are real without ever being allowed to read one."""
    intruder, page = someone_elses
    theirs = client.post(
        f"/api/v1/artifacts/{page.id}/pin",
        json={"pinned": True},
        headers=auth(intruder),
    )
    nothing = client.post(
        "/api/v1/artifacts/doesnotexist/pin",
        json={"pinned": True},
        headers=auth(intruder),
    )
    assert theirs.status_code == nothing.status_code == 404
    assert theirs.json() == nothing.json()


def test_no_key_is_no_action(client, mine):
    _, page = mine
    out = client.post(f"/api/v1/artifacts/{page.id}/archive", json={"archived": True})
    assert out.status_code == 401


# --- a title is still guarded text -------------------------------------------


def test_a_renamed_title_is_still_scanned_for_secrets(client, mine):
    """ "It is only a title" is how a field skips its checks. The same refusal
    the publish path gives, on the same input."""
    token, page = mine
    out = client.post(
        f"/api/v1/artifacts/{page.id}/rename",
        json={"title": "key AKIAIOSFODNN7EXAMPLE here"},
        headers=auth(token),
    )
    assert out.status_code == 422
    assert "AKIAIOSFODNN7EXAMPLE" not in out.text, "the refusal must not repeat it"


def test_an_oversized_title_is_refused_not_truncated(client, mine):
    token, page = mine
    out = client.post(
        f"/api/v1/artifacts/{page.id}/rename",
        json={"title": "x" * (store.MAX_TITLE_BYTES + 1)},
        headers=auth(token),
    )
    assert out.status_code == 413


def test_the_body_is_required_so_nothing_defaults_to_archived(client, mine):
    """An empty POST must not mean "archive it". A destructive-looking default
    nobody typed is how a page leaves the wall by accident."""
    token, page = mine
    assert (
        client.post(
            f"/api/v1/artifacts/{page.id}/archive", json={}, headers=auth(token)
        ).status_code
        == 422
    )


# --- and the state is visible, not just settable ------------------------------


def test_the_listing_tells_an_agent_which_of_its_pages_are_archived(client, db, mine):
    """Settable but invisible would make "put it back" a guess: the only way to
    find an archived page again would be to have remembered its id."""
    token, page = mine
    client.post(
        f"/api/v1/artifacts/{page.id}/archive",
        json={"archived": True},
        headers=auth(token),
    )
    listed = client.get("/api/v1/artifacts", headers=auth(token)).json()
    assert [r["id"] for r in listed] == [page.id], "still listed to its owner"
    assert listed[0]["archived_at"] is not None
