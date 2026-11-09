"""Delete your own comment, with a short undo (9 Nov 2026).

The owner deletes his own comments from the gallery (the card and the panel); an agent deletes
its own over the API. Soft: nothing is removed, `restore` takes it back within
`comments.RESTORE_WINDOW`. A deleted first comment with replies stays as a placeholder; a thread
with nothing live left is gone, mark and all.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from ream import comments, events, keys, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
RANGE = {"kind": "range", "path": "p", "start": 0, "end": 1}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db=db), base_url="https://testserver")


@pytest.fixture()
def page(db):
    return store.publish(db, "seat", "Board", PAGE)


def _auth(agent: str, db) -> dict[str, str]:
    token, _ = keys.mint(db, agent=agent)
    return {"Authorization": f"Bearer {token}"}


def _same_origin(extra=None):
    return {"Sec-Fetch-Site": "same-origin", **(extra or {})}


def _his(db, page, body="HIS WORDS", **more):
    return comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body=body,
        **more,
    )


def _agents(db, page, body, author="seat", **more):
    return comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author=author,
        body=body,
        agent=author,
        **more,
    )


def _delete(db, comment_id, kind="human", author="jeremy"):
    return comments.delete(db, comment_id, author_kind=kind, author=author)


def _restore(db, comment_id, kind="human", author="jeremy"):
    return comments.restore(db, comment_id, author_kind=kind, author=author)


def _bodies(db, page):
    return [
        r.body for r in comments.list_for_artifact(db, page.id, include_resolved=True)
    ]


# --- the agent's side, over the API --------------------------------------------------


def test_an_agent_deletes_its_own_comment_and_it_is_gone(client, db, page):
    mine = _agents(db, page, "my note", author="ops")
    headers = _auth("ops", db)
    got = client.delete(f"/api/v1/comments/{mine.id}", headers=headers)
    assert got.status_code == 200, got.text
    assert got.json()["deleted_at"] and got.json()["restorable_until"]
    assert got.json()["body"] == ""
    assert comments.list_for_artifact(db, page.id, include_resolved=True) == []


def test_another_agents_comment_answers_exactly_like_a_missing_one(client, db, page):
    theirs = _agents(db, page, "not yours", author="other")
    headers = _auth("ops", db)
    not_mine = client.delete(f"/api/v1/comments/{theirs.id}", headers=headers)
    missing = client.delete("/api/v1/comments/nope", headers=headers)
    assert not_mine.status_code == missing.status_code == 404
    assert not_mine.json() == missing.json()
    restore_not_mine = client.post(
        f"/api/v1/comments/{theirs.id}/restore", headers=headers
    )
    restore_missing = client.post("/api/v1/comments/nope/restore", headers=headers)
    assert restore_not_mine.status_code == restore_missing.status_code == 404
    assert restore_not_mine.json() == restore_missing.json()
    assert _bodies(db, page) == ["not yours"]


def test_an_agent_cannot_delete_his_comment(client, db, page):
    his = _his(db, page)
    got = client.delete(f"/api/v1/comments/{his.id}", headers=_auth("seat", db))
    assert got.status_code == 404
    assert _bodies(db, page) == ["HIS WORDS"]


def test_undo_brings_it_back(client, db, page):
    mine = _agents(db, page, "my note", author="ops")
    headers = _auth("ops", db)
    client.delete(f"/api/v1/comments/{mine.id}", headers=headers)
    got = client.post(f"/api/v1/comments/{mine.id}/restore", headers=headers)
    assert got.status_code == 200, got.text
    assert got.json()["deleted_at"] is None and got.json()["body"] == "my note"
    assert _bodies(db, page) == ["my note"]


def test_an_expired_undo_is_a_409(client, db, page, monkeypatch):
    mine = _agents(db, page, "my note", author="ops")
    headers = _auth("ops", db)
    client.delete(f"/api/v1/comments/{mine.id}", headers=headers)
    monkeypatch.setattr(comments, "RESTORE_WINDOW", timedelta(seconds=-1))
    got = client.post(f"/api/v1/comments/{mine.id}/restore", headers=headers)
    assert got.status_code == 409
    assert _bodies(db, page) == []


def test_deleting_twice_and_restoring_a_live_one_change_nothing(db, page):
    root = _his(db, page)
    assert _restore(db, root.id).deleted_at is None
    first = _delete(db, root.id)
    again = _delete(db, root.id)
    assert again.deleted_at == first.deleted_at


# --- what readers see -----------------------------------------------------------------


def test_the_signature_changes_on_delete_and_on_restore(db, page):
    root = _his(db, page)
    before = comments.signature(db, page.id)
    _delete(db, root.id)
    deleted = comments.signature(db, page.id)
    assert deleted != before
    _restore(db, root.id)
    restored = comments.signature(db, page.id)
    assert restored != deleted


def test_a_deleted_first_comment_with_replies_stays_as_a_placeholder(db, page):
    root = _his(db, page, "SECRET FIRST", anchor=RANGE, quote="x")
    _agents(db, page, "a live reply", thread_root=root.id)
    _delete(db, root.id)
    rows = comments.list_for_artifact(db, page.id)
    first = next(r for r in rows if r.id == root.id)
    assert first.deleted_at and first.body == ""
    assert [r.body for r in rows if r.id != root.id] == ["a live reply"]
    assert [m["id"] for m in comments.marks_for_page(db, page.id)] == [root.id]


def test_a_thread_vanishes_with_its_mark_when_everything_is_deleted(db, page):
    root = _his(db, page, "first", anchor=RANGE, quote="x")
    reply = _agents(db, page, "reply", thread_root=root.id)
    assert comments.marks_for_page(db, page.id)
    _delete(db, root.id)
    _delete(db, reply.id, kind="agent", author="seat")
    assert comments.list_for_artifact(db, page.id, include_resolved=True) == []
    assert comments.marks_for_page(db, page.id) == []
    _restore(db, root.id)
    assert [m["id"] for m in comments.marks_for_page(db, page.id)] == [root.id]


def test_a_deleted_reply_is_gone_while_the_thread_stays(db, page):
    root = _his(db, page, "first")
    reply = _agents(db, page, "reply", thread_root=root.id)
    _delete(db, reply.id, kind="agent", author="seat")
    assert _bodies(db, page) == ["first"]


# --- events -----------------------------------------------------------------------------


def test_the_owning_agent_hears_of_a_delete_and_a_restore(db, page):
    root = _his(db, page, "DELETED TEXT")
    _delete(db, root.id)
    _restore(db, root.id)
    rows = events.list_for_agent(db, "seat")
    kinds = [r.kind for r in rows if r.kind in ("comment_deleted", "comment_restored")]
    assert kinds == ["comment_deleted", "comment_restored"]
    deleted = next(r for r in rows if r.kind == "comment_deleted")
    assert deleted.actor == "jeremy" and deleted.ref_id == root.id


def test_a_settled_thread_with_a_deleted_root_does_not_leak_its_words(db, page):
    root = _his(db, page, "LEAKY ROOT TEXT")
    _agents(db, page, "live reply", thread_root=root.id)
    _delete(db, root.id)
    comments.resolve(db, root.id, by="jeremy", agent=None)
    comments.reopen(db, root.id, agent=None, by="jeremy")
    rows = events.list_for_agent(db, "seat")
    said = [r.summary for r in rows if r.kind in ("thread_resolved", "thread_reopened")]
    assert len(said) == 2
    assert not any("LEAKY" in s for s in said)


# --- the gallery's routes, for him ------------------------------------------------------


def test_he_deletes_and_undoes_his_own_comment_in_the_gallery(client, db, page):
    root = _his(db, page)
    got = client.post(
        f"/c/{root.id}/delete", headers=_same_origin(), follow_redirects=False
    )
    assert got.status_code == 303 and got.headers["location"].endswith("#comments")
    assert _bodies(db, page) == []
    got = client.post(
        f"/c/{root.id}/restore",
        headers=_same_origin({"Accept": "application/json"}),
    )
    assert got.status_code == 200 and got.json() == {"id": root.id, "deleted": False}
    assert _bodies(db, page) == ["HIS WORDS"]
    got = client.post(
        f"/c/{root.id}/delete", headers=_same_origin({"Accept": "application/json"})
    )
    assert got.json() == {"id": root.id, "deleted": True}


def test_in_the_gallery_an_agents_comment_is_a_404_like_a_missing_one(client, db, page):
    theirs = _agents(db, page, "agent words")
    not_his = client.post(f"/c/{theirs.id}/delete", headers=_same_origin())
    missing = client.post("/c/nope/delete", headers=_same_origin())
    assert not_his.status_code == missing.status_code == 404
    assert not_his.json() == missing.json()
    assert _bodies(db, page) == ["agent words"]


def test_the_gallery_refuses_an_expired_undo_and_a_cross_site_post(
    client, db, page, monkeypatch
):
    root = _his(db, page)
    cross = client.post(
        f"/c/{root.id}/delete", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert cross.status_code == 403 and _bodies(db, page) == ["HIS WORDS"]
    client.post(f"/c/{root.id}/delete", headers=_same_origin())
    monkeypatch.setattr(comments, "RESTORE_WINDOW", timedelta(seconds=-1))
    late = client.post(f"/c/{root.id}/restore", headers=_same_origin())
    assert late.status_code == 409


def test_the_panel_offers_delete_only_on_his_own_and_a_placeholder_when_gone(
    client, db, page
):
    root = _his(db, page, "HIS TEXT", anchor=RANGE, quote="x")
    reply = _agents(db, page, "AGENT TEXT", thread_root=root.id)
    html = client.get(f"/a/{page.id}").text
    assert f'action="/c/{root.id}/delete"' in html
    assert f'action="/c/{reply.id}/delete"' not in html
    _delete(db, root.id)
    html = client.get(f"/a/{page.id}").text
    assert "Comment deleted" in html and "HIS TEXT" not in html
    assert f'action="/c/{root.id}/delete"' not in html
