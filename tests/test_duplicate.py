"""Copying a page, and the one thing a copy must never carry.

homelab-ij8.84.6. "Duplicate" is in both of the reference's menus. The question
that decided the shape was whether a duplicate carries the original's
DECISIONS -- and it must not carry the answers, because an answered decision is
a record of something Jeremy SAID about the original. Copying one would
manufacture a record of him answering a question he has never seen, on a page he
has never read. Making that class of fabrication impossible is what this service
is for.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import comments, decisions, keys, store
from artifacts.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>B</title></head>"
    "<body><p>The original words.</p></body></html>"
)
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db), base_url="https://testserver")


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _ask(db, artifact_id, agent="seat"):
    return decisions.ask(
        db,
        agent=agent,
        artifact_id=artifact_id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )


# --- what a copy carries, and what it must not -------------------------------


def test_a_copy_has_the_body_and_its_own_address(db):
    keys.mint(db, agent="seat")
    original = store.publish(db, "seat", "Board", PAGE)
    copy, body = store.duplicate(db, original.id, agent="seat")

    assert copy.id != original.id
    assert copy.url != original.url
    assert body == PAGE
    assert store.get_body(db, "seat", copy.id).body == PAGE
    assert store.get_owned(db, "seat", copy.id).title == "Board (copy)"


def test_a_copy_never_carries_an_answer_he_gave_about_the_original(db):
    """THE ONE THAT MATTERS. An answered decision is a record of something he
    said. On a copy it would be a record of him answering a question he has
    never seen."""
    keys.mint(db, agent="seat")
    original = store.publish(db, "seat", "Board", PAGE)
    asked = _ask(db, original.id)
    decisions.answer(db, asked.id, letter="a", note="his actual words", by="jeremy")

    copy, _ = store.duplicate(db, original.id, agent="seat")

    assert decisions.list_for_artifact(db, copy.id, include_answered=True) == []
    kept = decisions.list_for_artifact(db, original.id, include_answered=True)
    assert len(kept) == 1, "and the original still has it"
    assert kept[0].answer_note == "his actual words"


def test_a_copy_does_not_ask_him_the_same_open_question_twice(db):
    """Quieter than the answered case but still wrong: two copies of one
    question, and answering either leaves the other looking unanswered for
    ever."""
    keys.mint(db, agent="seat")
    original = store.publish(db, "seat", "Board", PAGE)
    _ask(db, original.id)
    copy, _ = store.duplicate(db, original.id, agent="seat")
    assert decisions.list_for_artifact(db, copy.id) == []


def test_a_copy_starts_at_version_one_with_no_history(db):
    keys.mint(db, agent="seat")
    original = store.publish(db, "seat", "Board", PAGE)
    store.patch(
        db, "seat", original.id, PAGE.replace("original", "second"), original.revision
    )
    copy, _ = store.duplicate(db, original.id, agent="seat")
    assert copy.version == 1
    assert store.get_owned(db, "seat", copy.id).current_version == 1


def test_a_copy_takes_the_live_body_not_the_first_one(db):
    keys.mint(db, agent="seat")
    original = store.publish(db, "seat", "Board", PAGE)
    newer = PAGE.replace("The original words.", "The current words.")
    store.patch(db, "seat", original.id, newer, original.revision)
    _copy, body = store.duplicate(db, original.id, agent="seat")
    assert body == newer


def test_a_copy_carries_no_comments(db):
    keys.mint(db, agent="seat")
    original = store.publish(db, "seat", "Board", PAGE)
    comments.add(
        db,
        artifact_id=original.id,
        author_kind="human",
        author="jeremy",
        body="a note on the original",
    )
    copy, _ = store.duplicate(db, original.id, agent="seat")
    assert comments.list_for_artifact(db, copy.id) == []


def test_the_copy_belongs_to_the_pages_agent_not_to_whoever_copied_it(db):
    """Jeremy duplicating a page in his gallery does not make him its
    publisher. The seat that wrote it still maintains it, and the alternative
    is a page no agent can update."""
    keys.mint(db, agent="warpgate-arc")
    original = store.publish(db, "warpgate-arc", "Board", PAGE)
    copy, _ = store.duplicate(db, original.id, agent=None)  # his gallery: unscoped
    assert store.get_owned(db, "warpgate-arc", copy.id).agent == "warpgate-arc"


def test_a_very_long_title_still_fits_after_the_suffix(db):
    """Trimmed rather than refused: the caller never typed the suffix, so it
    must not be the thing that makes their publish fail."""
    keys.mint(db, agent="seat")
    long_title = "x" * store.MAX_TITLE_BYTES
    original = store.publish(db, "seat", long_title, PAGE)
    copy, _ = store.duplicate(db, original.id, agent="seat")
    title = store.get_owned(db, "seat", copy.id).title
    assert title.endswith(" (copy)")
    assert len(title.encode()) <= store.MAX_TITLE_BYTES


# --- who may do it -----------------------------------------------------------


def test_an_agent_duplicates_its_own_page_over_the_api(client, db):
    token, _ = keys.mint(db, agent="seat")
    original = store.publish(db, "seat", "Board", PAGE)
    out = client.post(f"/api/v1/artifacts/{original.id}/duplicate", headers=auth(token))
    assert out.status_code == 201
    assert out.json()["id"] != original.id
    assert store.get_owned(db, "seat", out.json()["id"]).title == "Board (copy)"


def test_a_seat_cannot_duplicate_another_seats_page(client, db):
    keys.mint(db, agent="other")
    intruder, _ = keys.mint(db, agent="intruder")
    theirs = store.publish(db, "other", "Not yours", PAGE)
    out = client.post(
        f"/api/v1/artifacts/{theirs.id}/duplicate", headers=auth(intruder)
    )
    assert out.status_code == 404
    assert len(store.list_for_agent(db, "other")) == 1, "and nothing was made"
    assert store.list_for_agent(db, "intruder") == []


def test_duplicating_from_the_gallery_goes_straight_to_the_copy(client, db):
    """The only reason to duplicate a page is to change the copy, so the copy
    is what he wants in front of him."""
    keys.mint(db, agent="seat")
    original = store.publish(db, "seat", "Board", PAGE)
    out = client.post(
        f"/a/{original.id}/duplicate", headers=SAME_ORIGIN, follow_redirects=False
    )
    assert out.status_code == 303
    assert out.headers["location"] != f"/a/{original.id}"
    assert out.headers["location"].startswith("/a/")


def test_a_cross_site_form_cannot_duplicate(client, db):
    keys.mint(db, agent="seat")
    original = store.publish(db, "seat", "Board", PAGE)
    out = client.post(
        f"/a/{original.id}/duplicate",
        headers={"sec-fetch-site": "cross-site"},
        follow_redirects=False,
    )
    assert out.status_code == 403
    assert len(store.list_for_agent(db, "seat")) == 1


def test_duplicating_something_that_is_not_there_is_a_404(client, db):
    keys.mint(db, agent="seat")
    out = client.post(
        "/a/nosuchpage/duplicate", headers=SAME_ORIGIN, follow_redirects=False
    )
    assert out.status_code == 404
