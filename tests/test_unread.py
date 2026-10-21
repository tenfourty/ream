"""What Jeremy has not read rises to the top, and its agent can see when he
has (homelab-ij8.103, his confirmed ruling of 23 Sep 2026)."""

import re

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>t</title></head><body>{}</body></html>"


@pytest.fixture()
def gallery(db):
    return TestClient(create_gallery_app(db))


def _order(gallery):
    wall = gallery.get("/").text
    return re.findall(r'<a href="/a/([a-z0-9]+)">', wall)


def _publish(db, title):
    return store.publish(db, "seat", title, PAGE.format(title))


def test_a_new_page_is_unread_and_opening_it_makes_it_read(db, gallery):
    page = _publish(db, "One")
    assert 'data-unread="1"' in gallery.get("/").text
    gallery.get(f"/a/{page.id}")
    assert 'data-unread="1"' not in gallery.get("/").text


def test_a_republished_page_is_unread_again(db, gallery):
    page = _publish(db, "One")
    gallery.get(f"/a/{page.id}")
    store.patch(db, "seat", page.id, PAGE.format("changed"), page.revision)
    assert 'data-unread="1"' in gallery.get("/").text


def test_the_wall_order_is_pinned_then_questions_then_unread_then_the_rest(db, gallery):
    read_old = _publish(db, "read-old")
    asking = _publish(db, "asking")
    pinned = _publish(db, "pinned")
    unread = _publish(db, "unread")
    read_new = _publish(db, "read-new")
    for page in (read_old, asking, pinned, read_new):
        gallery.get(f"/a/{page.id}")
    decisions.ask(
        db,
        agent="seat",
        artifact_id=asking.id,
        question="ship?",
        options=[{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}],
    )
    store.set_pinned(db, pinned.id, True, agent=None)
    order = _order(gallery)
    assert order[:5] == [pinned.id, asking.id, unread.id, read_new.id, read_old.id]


def test_the_agent_sees_whether_he_has_opened_its_latest_version(db, gallery):
    token, _ = keys.mint(db, agent="seat")
    auth = {"Authorization": f"Bearer {token}"}
    page = _publish(db, "One")
    before = gallery.get(f"/api/v1/artifacts/{page.id}", headers=auth).json()
    assert before["seen_version"] is None and before["seen_at"] is None
    gallery.get(f"/a/{page.id}")
    after = gallery.get(f"/api/v1/artifacts/{page.id}", headers=auth).json()
    assert after["seen_version"] == after["current_version"] == 1
    listed = gallery.get("/api/v1/artifacts", headers=auth).json()
    assert listed[0]["seen_version"] == 1
    store.patch(db, "seat", page.id, PAGE.format("changed"), page.revision)
    stale = gallery.get(f"/api/v1/artifacts/{page.id}", headers=auth).json()
    assert stale["seen_version"] == 1 and stale["current_version"] == 2


def test_only_the_owner_sees_it(db, gallery):
    other, _ = keys.mint(db, agent="other")
    page = _publish(db, "One")
    gallery.get(f"/a/{page.id}")
    # Other agents may READ an open page (homelab-ij8.105), but whether he has
    # read it is the publisher's alone to know.
    response = gallery.get(
        f"/api/v1/artifacts/{page.id}", headers={"Authorization": f"Bearer {other}"}
    )
    assert response.status_code == 200
    assert (
        response.json()["seen_version"] is None and response.json()["seen_at"] is None
    )


def test_an_unread_tile_has_no_tooltip(db, gallery):
    """No `title` on the unread tile or row (Jeremy, 14 Oct): it covered the whole tile,
    and after a click the tooltip stayed over the opened page's frame, which takes the
    mouse events that would have dismissed it. The dot and the screen-reader word remain."""
    _publish(db, "One")
    for view in ("/", "/?view=list"):
        wall = gallery.get(view).text
        assert 'data-unread="1"' in wall
        assert not re.search(r'<li [^>]*data-unread="1"[^>]*title=', wall), view
        assert "Not opened since it changed" not in wall, view


def _his(db, page, body="his question"):
    from artifacts import comments

    return comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body=body
    )


def _reply(db, page, root, session="cove/ab12", author="seat"):
    from artifacts import comments

    return comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author=author,
        agent=author,
        body="an answer",
        thread_root=root.id,
        session=session,
    )


def test_a_reply_under_his_comment_makes_a_read_page_unread_and_names_the_office(
    db, gallery
):
    page = _publish(db, "One")
    his = _his(db, page)
    gallery.get(f"/a/{page.id}")
    assert 'data-unread="1"' not in gallery.get("/").text
    _reply(db, page, his)
    wall = gallery.get("/").text
    assert 'data-unread="1"' in wall
    assert "Reply from cove" in wall
    gallery.get(f"/a/{page.id}")
    wall = gallery.get("/").text
    assert 'data-unread="1"' not in wall and "Reply from" not in wall


def test_two_replies_show_the_newest_office(db, gallery):
    page = _publish(db, "One")
    his = _his(db, page)
    gallery.get(f"/a/{page.id}")
    _reply(db, page, his, session="cove/1")
    _reply(db, page, his, session="runcove/2")
    wall = gallery.get("/").text
    assert "Reply from runcove" in wall and "Reply from cove<" not in wall


def test_the_owner_in_its_own_thread_he_never_spoke_in_is_not_a_reply(db, gallery):
    from artifacts import comments

    page = _publish(db, "One")
    gallery.get(f"/a/{page.id}")
    root = comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        agent="seat",
        body="a note",
    )
    _reply(db, page, root)
    wall = gallery.get("/").text
    assert 'data-unread="1"' not in wall and "Reply from" not in wall


def test_a_withdrawn_reply_does_not_mark_the_page_unread(db, gallery):
    from artifacts import comments

    page = _publish(db, "One")
    his = _his(db, page)
    gallery.get(f"/a/{page.id}")
    reply = _reply(db, page, his)
    comments.withdraw(db, reply.id, kind="test")
    wall = gallery.get("/").text
    assert 'data-unread="1"' not in wall and "Reply from" not in wall


def test_a_reply_lifts_the_page_in_the_wall_order(db, gallery):
    first = _publish(db, "first")
    his = _his(db, first)
    second = _publish(db, "second")
    for page in (first, second):
        gallery.get(f"/a/{page.id}")
    assert _order(gallery)[:2] == [second.id, first.id]
    _reply(db, first, his)
    assert _order(gallery)[:2] == [first.id, second.id]


def test_a_reply_under_his_answer_note_makes_the_page_unread(db, gallery):
    from artifacts import comments

    page = _publish(db, "One")
    ask = decisions.ask(
        db,
        agent="seat",
        artifact_id=page.id,
        question="ship?",
        options=[{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}],
    )
    decisions.answer(db, ask.id, letter="a", note="go", by="jeremy")
    gallery.get(f"/a/{page.id}")
    assert 'data-unread="1"' not in gallery.get("/").text
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        agent="seat",
        body="a question about your note",
        anchor={"kind": "decision", "decision_id": ask.id},
        session="cove/ab12",
    )
    wall = gallery.get("/").text
    assert 'data-unread="1"' in wall and "Reply from cove" in wall
    gallery.get(f"/a/{page.id}")
    assert 'data-unread="1"' not in gallery.get("/").text
