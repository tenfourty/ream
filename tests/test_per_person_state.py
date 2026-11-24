"""What a person has read, prefers and pinned is theirs (people design, slice 2;
docs/agent/specs/2026-11-24-people-design.md). Nobody but the owner can sign in yet, so a second
person here is the `people.current` dependency overridden on the gallery app, with
`people.OPEN_SIGN_IN` flipped the way step 4 will. The agent-facing reads (`seen_version`,
`pinned_at`) stay the owner's.
"""

import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from ream import comments, decisions, frame_pass, holds, inbox, keys, people, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>t</title></head><body>{}</body></html>"
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]
ANN = people.Person(id="ann", name="Ann", is_owner=False)


@pytest.fixture(autouse=True)
def _sign_in_open(monkeypatch):
    monkeypatch.setattr(people, "OPEN_SIGN_IN", True)


@pytest.fixture()
def app(db):
    return create_gallery_app(db)


@pytest.fixture()
def owner_wall(app):
    return TestClient(app)


@pytest.fixture()
def ann_wall(db):
    other = create_gallery_app(db)
    other.dependency_overrides[people.current] = lambda: ANN
    return TestClient(other)


def _publish(db, title):
    return store.publish(db, "seat", title, PAGE.format(title))


def _order(wall):
    return re.findall(r'<a href="/a/([a-z0-9]+)">', wall.get("/").text)


def _unread(wall) -> bool:
    return 'data-unread="1"' in wall.get("/").text


def _rows(db, sql, *params):
    with db.connect() as conn:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]


def test_opening_a_page_marks_it_read_for_that_person_only(db, owner_wall, ann_wall):
    page = _publish(db, "One")
    assert _unread(owner_wall) and _unread(ann_wall)
    owner_wall.get(f"/a/{page.id}")
    assert not _unread(owner_wall)
    assert _unread(ann_wall)
    assert [r[0] for r in _rows(db, "select person from page_read")] == ["jeremy"]
    ann_wall.get(f"/a/{page.id}")
    assert not _unread(ann_wall)
    assert sorted(r[0] for r in _rows(db, "select person from page_read")) == [
        "ann",
        "jeremy",
    ]


def test_a_republish_makes_it_unread_for_each_person_who_had_read_it(
    db, owner_wall, ann_wall
):
    page = _publish(db, "One")
    owner_wall.get(f"/a/{page.id}")
    ann_wall.get(f"/a/{page.id}")
    store.patch(db, "seat", page.id, PAGE.format("changed"), page.revision)
    assert _unread(owner_wall) and _unread(ann_wall)
    ann_wall.get(f"/a/{page.id}")
    assert _unread(owner_wall) and not _unread(ann_wall)


def test_pins_are_per_person_and_unpinning_takes_only_your_own(
    db, owner_wall, ann_wall
):
    first = _publish(db, "first")
    second = _publish(db, "second")
    for wall in (owner_wall, ann_wall):
        for page in (first, second):
            wall.get(f"/a/{page.id}")
    store.set_pinned(db, first.id, True, agent=None, person="jeremy")
    store.set_pinned(db, second.id, True, agent=None, person="ann")
    assert _order(owner_wall)[:2] == [first.id, second.id]
    assert _order(ann_wall)[:2] == [second.id, first.id]
    # Both pin the same page; one unpins; the other's pin stays.
    store.set_pinned(db, first.id, True, agent=None, person="ann")
    store.set_pinned(db, first.id, False, agent=None, person="jeremy")
    assert sorted(_rows(db, "select person, artifact_id from page_pin")) == sorted(
        [("ann", first.id), ("ann", second.id)]
    )
    # A page that is not there is refused either way.
    assert store.set_pinned(db, "nope", True, agent=None, person="ann") is False
    assert store.set_pinned(db, "nope", False, agent=None, person="ann") is False


def test_the_owner_pins_through_the_gallery_and_it_is_a_page_pin_row(db, owner_wall):
    page = _publish(db, "One")
    out = owner_wall.post(
        f"/a/{page.id}/pin",
        data={"pinned": "1"},
        headers={"sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert out.status_code == 303
    assert [r[:2] for r in _rows(db, "select person, artifact_id from page_pin")] == [
        ("jeremy", page.id)
    ]
    # `artifact.pinned_at` is kept only as a record (not a rollback path) and neither read nor written.
    assert _rows(db, "select pinned_at from artifact where id = %s", page.id) == [
        (None,)
    ]


def test_a_pin_leaves_with_its_page(db):
    page = _publish(db, "One")
    store.set_pinned(db, page.id, True, agent=None, person="ann")
    with db.connect() as conn:
        conn.execute("delete from artifact where id = %s", (page.id,))
    assert _rows(db, "select * from page_pin") == []


def test_the_agent_sees_the_owners_read_mark_and_pin_only(db, owner_wall, ann_wall):
    token, _ = keys.mint(db, agent="seat")
    auth = {"Authorization": f"Bearer {token}"}
    page = _publish(db, "One")
    ann_wall.get(f"/a/{page.id}")
    store.set_pinned(db, page.id, True, agent=None, person="ann")
    seen = owner_wall.get(f"/api/v1/artifacts/{page.id}", headers=auth).json()
    assert seen["seen_version"] is None and seen["pinned_at"] is None
    owner_wall.get(f"/a/{page.id}")
    listed = owner_wall.get("/api/v1/artifacts", headers=auth).json()
    assert listed[0]["seen_version"] == 1 and listed[0]["pinned_at"] is None
    # An agent's own pin is the owner's pin, as it always was.
    assert owner_wall.post(
        f"/api/v1/artifacts/{page.id}/pin", json={"pinned": True}, headers=auth
    ).json()["pinned_at"]
    assert sorted(r[0] for r in _rows(db, "select person from page_pin")) == [
        "ann",
        "jeremy",
    ]


def test_the_hold_or_live_preference_is_per_person(db):
    page = _publish(db, "Plan")
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    with db.connect() as conn:
        assert holds.get_mode(conn, "ann") == "held"
    assert holds.set_review_mode(db, row.id, "live", "ann")[0] == page.id
    with db.connect() as conn:
        assert holds.get_mode(conn, "ann") == "live"
        assert holds.get_mode(conn, "jeremy") == "held"
        assert (
            holds.get_mode(conn) == "held"
        )  # an agent opening a review asks for the owner's
        holds.set_mode(conn, "held", "ann")
        assert holds.get_mode(conn, "ann") == "held"
    assert sorted(r[0] for r in _rows(db, "select person from human_pref")) == ["ann"]


def test_an_unseen_reply_is_unseen_per_person(db):
    page = _publish(db, "Thread")
    root = comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="hm"
    )
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        body="answer",
        thread_root=root.id,
        agent="seat",
    )
    # Whose thread a reply is under (`comments._HIS_THREAD`) is still any human's, not the
    # reading person's; slice 3 settles that, so this asserts only that the READ MARK is
    # per person: one person opening the page never clears another's unseen reply.
    assert inbox.count(db, person="jeremy")[0] == 1
    store.mark_read(db, page.id, "ann")
    assert inbox.count(db, person="ann")[0] == 0
    assert inbox.count(db, person="jeremy")[0] == 1
    assert [i.kind for i in inbox.gather(db, person="jeremy")[0]] == ["reply"]
    store.mark_read(db, page.id, "jeremy")
    assert inbox.count(db, person="jeremy")[0] == 0


def test_a_frame_pass_records_who_it_was_minted_for(db, owner_wall, ann_wall):
    page = _publish(db, "One")
    owner_wall.get(f"/a/{page.id}")
    ann_wall.get(f"/a/{page.id}")
    ann_wall.get(f"/a/{page.id}/frame")
    frame_pass.mint(db, page.id)
    assert sorted(r[0] or "" for r in _rows(db, "select person from frame_pass")) == [
        "",
        "ann",
        "ann",
        "jeremy",
    ]


def test_a_pinned_page_reads_back_with_a_datetime_on_every_read_path(db):
    """`pinned_at` is a scalar subquery with no declared type: on SQLite it is the alias's
    column-type tag that turns the stored text into a datetime."""
    page = _publish(db, "Pinned")
    store.set_pinned(db, page.id, True, agent="seat")
    assert isinstance(store.get_owned(db, "seat", page.id).pinned_at, datetime)
    assert isinstance(store.list_for_agent(db, "seat")[0].pinned_at, datetime)
    assert isinstance(store.list_readable(db, "seat")[0].pinned_at, datetime)
