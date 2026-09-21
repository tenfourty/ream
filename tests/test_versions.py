"""A page's history: list it, read an old one, roll back.

homelab-ij8.30, first-user feedback from the cove seat on 19 Sep 2026: publish
returns a revision hash and the skill says a page keeps its history, but no
command listed revisions, showed an old one, or rolled back -- so the hash was
a number the agent was told and could do nothing with. And homelab-50ze, the
same gap from the other side: an agent could read back its page but not an
older version of it.

ROLLING BACK IS NOT A VERB HERE. It is a read plus an ordinary update, so the
restored bytes land as a NEW version rather than rewriting history. A service
whose whole argument is that the record is not edited cannot have a command
that edits the record.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body>one</body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db=db))


@pytest.fixture()
def three_versions(db):
    keys.mint(db, agent="seat")
    first = store.publish(db, "seat", "Board", PAGE, session="seat/aaa")
    second = store.patch(
        db, "seat", first.id, PAGE.replace("one", "two"), first.revision,
        session="seat/bbb",
    )
    store.patch(
        db, "seat", first.id, PAGE.replace("one", "three"), second.revision,
        session="seat/ccc",
    )
    return first.id


def test_the_history_lists_every_version_newest_first(db, three_versions):
    rows = store.list_versions(db, "seat", three_versions)
    assert [r.n for r in rows] == [3, 2, 1], (
        "a history is read for what happened recently, so newest first"
    )


def test_it_says_which_one_is_on_the_wall(db, three_versions):
    """Without this you can read a history and still not know which version a
    reader is actually looking at, which makes the rest of it unactionable."""
    rows = store.list_versions(db, "seat", three_versions)
    assert [r.live for r in rows] == [True, False, False]


def test_it_carries_who_wrote_each_one(db, three_versions):
    """Sessions landed this morning, so "which of us did this" is answerable
    per version rather than per page."""
    rows = store.list_versions(db, "seat", three_versions)
    assert [r.session for r in rows] == ["seat/ccc", "seat/bbb", "seat/aaa"]


def test_it_never_carries_the_bodies(db, three_versions):
    """A history is read to CHOOSE a version. Shipping every body would make
    this the largest response the service produces."""
    rows = store.list_versions(db, "seat", three_versions)
    assert not any(hasattr(r, "body") for r in rows)
    assert all(r.bytes > 0 for r in rows)


def test_the_byte_count_counts_bytes_and_not_characters(db):
    """`length()` in Postgres counts CHARACTERS. A page with any non-ASCII in
    it would then report fewer bytes than it has -- which is the one number
    this row exists to give honestly."""
    keys.mint(db, agent="seat")
    body = PAGE.replace("one", "wide — dash é")
    made = store.publish(db, "seat", "Board", body)
    row = store.list_versions(db, "seat", made.id)[0]
    assert row.bytes == len(body.encode())
    assert row.bytes > len(body), "it reported characters, not bytes"


def test_an_old_version_can_be_read_back(db, three_versions):
    assert "one" in store.get_version_body(db, "seat", three_versions, 1).body
    assert "three" in store.get_version_body(db, "seat", three_versions, 3).body


def test_rolling_back_lands_as_a_new_version(db, three_versions):
    """The restored bytes become version 4. History is appended to, never
    rewritten -- and the page keeps its address throughout."""
    old = store.get_version_body(db, "seat", three_versions, 1)
    live = store.get_body(db, "seat", three_versions)
    back = store.patch(db, "seat", three_versions, old.body, live.revision)

    assert back.version == 4
    assert store.get_body(db, "seat", three_versions).body == old.body
    assert [r.n for r in store.list_versions(db, "seat", three_versions)] == [4, 3, 2, 1]


# --- scope --------------------------------------------------------------------


def test_another_agents_history_is_a_404_not_a_403(db, three_versions):
    keys.mint(db, agent="other")
    with pytest.raises(store.NotFound):
        store.list_versions(db, "other", three_versions)
    with pytest.raises(store.NotFound):
        store.get_version_body(db, "other", three_versions, 1)


def test_a_version_that_does_not_exist_is_a_404(db, three_versions):
    with pytest.raises(store.NotFound):
        store.get_version_body(db, "seat", three_versions, 99)


# --- over HTTP ------------------------------------------------------------------


def test_both_routes_answer_for_the_owning_key(client, db, three_versions):
    token, _ = keys.mint(db, agent="seat")
    headers = {"Authorization": f"Bearer {token}"}

    listing = client.get(
        f"/api/v1/artifacts/{three_versions}/versions", headers=headers
    )
    assert listing.status_code == 200
    assert [r["n"] for r in listing.json()] == [3, 2, 1]
    assert "body" not in listing.json()[0]

    one = client.get(
        f"/api/v1/artifacts/{three_versions}/versions/1", headers=headers
    )
    assert one.status_code == 200
    assert "one" in one.json()["body"]


def test_another_key_gets_the_same_404_for_both(client, db, three_versions):
    other, _ = keys.mint(db, agent="other")
    headers = {"Authorization": f"Bearer {other}"}
    assert (
        client.get(
            f"/api/v1/artifacts/{three_versions}/versions", headers=headers
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/artifacts/{three_versions}/versions/1", headers=headers
        ).status_code
        == 404
    )
