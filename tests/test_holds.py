"""Hold or live (page review step 3, homelab-ij8.108): a mark Jeremy makes on a
page under a held review is invisible to the agent."""

import pytest
from fastapi.testclient import TestClient

from artifacts import comments, decisions, holds, keys, nudge, store
from artifacts.app import create_gallery_app
from artifacts.db import Database

PAGE = (
    "<!doctype html><title>Plan</title>"
    "<p>Rotate the keys every 90 days.</p><p>Email the team when done.</p>"
)
RANGE = {"kind": "range", "path": "/p[1]", "start": 0, "end": 6}


def _page(db, agent="seat"):
    return store.publish(db, agent, "Plan", PAGE)


def _human(db, page, **kw):
    return comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", **kw
    )


def _mark(db, page, **kw):
    return _human(db, page, kind="remove", anchor=RANGE, quote="Rotate", body="", **kw)


def _events_for(db, comment_id):
    with db.connect() as conn:
        return conn.execute(
            "select seq from event where ref_id = %s and kind = 'comment_added'",
            (comment_id,),
        ).fetchall()


def _mode_of(db, decision_id):
    with db.connect() as conn:
        return conn.execute(
            "select review_mode from decision where id = %s", (decision_id,)
        ).fetchone()[0]


@pytest.fixture()
def nudges(monkeypatch):
    sent = []
    monkeypatch.setattr(nudge, "emit", lambda **kw: sent.append(kw))
    return sent


def test_the_first_ever_mode_is_held(db: Database):
    with db.connect() as conn:
        assert holds.get_mode(conn) == "held"
        holds.set_mode(conn, "live")
        assert holds.get_mode(conn) == "live"
        with pytest.raises(ValueError):
            holds.set_mode(conn, "loud")
        assert holds.get_mode(conn) == "live"


def test_a_review_takes_his_remembered_mode_unless_the_agent_chose(db):
    page = _page(db)
    first = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    assert _mode_of(db, first.id) == "held"
    with db.connect() as conn:
        holds.set_mode(conn, "live")
    second = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    assert _mode_of(db, second.id) == "live"
    assert second.review_mode == "live"
    third = decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    assert _mode_of(db, third.id) == "held"


def test_his_mark_on_a_held_review_is_held_and_silent(db, nudges):
    page = _page(db)
    decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    row = _mark(db, page)
    assert row.held is True
    assert _events_for(db, row.id) == []
    assert nudges == []


def test_his_mark_without_a_review_or_on_a_live_review_is_not_held(db, nudges):
    page = _page(db)
    bare = _mark(db, page)
    assert bare.held is False
    assert len(_events_for(db, bare.id)) == 1
    decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="live")
    live = _mark(db, page)
    assert live.held is False
    assert len(_events_for(db, live.id)) == 1
    assert len(nudges) == 2


def test_an_agent_write_is_never_held(db):
    page = _page(db)
    root = _human(db, page, body="before the review")
    decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    reply = comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        body="noted",
        thread_root=root.id,
        agent="seat",
    )
    assert reply.held is False
    fresh = comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        body="a fresh remark",
        agent="seat",
    )
    assert fresh.held is False


def test_a_held_mark_is_in_no_agent_read(db):
    """Every agent-facing read of a comment: the per-page list, the all-pages
    list, and the events feed. `decisions.uncollected` and `store.py` were
    grepped for a comment count or list and hold none, so there is nothing
    further to assert there."""
    client = TestClient(create_gallery_app(db))
    token, _ = keys.mint(db, agent="seat")
    auth = {"Authorization": f"Bearer {token}"}
    page = _page(db)
    decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    held = _mark(db, page)
    assert held.held

    per_page = client.get(
        f"/api/v1/artifacts/{page.id}/comments?include_resolved=true", headers=auth
    ).json()
    assert held.id not in [c["id"] for c in per_page]
    every = client.get("/api/v1/comments?include_resolved=true", headers=auth).json()
    assert held.id not in [c["id"] for c in every]
    feed = client.get("/api/v1/events?since=0", headers=auth).json()
    assert held.id not in [e.get("ref_id") for e in feed]

    seen = comments.list_for_artifact(db, page.id, include_held=True)
    assert held.id in [c.id for c in seen]
    assert held.id not in [c.id for c in comments.list_for_artifact(db, page.id)]
    grouped = comments.across_pages(db)
    assert held.id in [c.id for g in grouped for c in g.rows]
    marks = comments.marks_for_page(db, page.id)
    [shown] = [m for m in marks if m["id"] == held.id]
    assert shown["held"] is True
    assert shown["kind"] == "", "a held mark's kind is not sent to the page"
    assert shown["quote"] == "Rotate", "the dotted underline still needs the quote"


def test_the_agent_cannot_reach_a_held_thread_by_its_id(db):
    client = TestClient(create_gallery_app(db))
    token, _ = keys.mint(db, agent="seat")
    auth = {"Authorization": f"Bearer {token}"}
    page = _page(db)
    decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    held = _mark(db, page)
    missing = "0" * 26

    def reply(cid):
        return client.post(
            f"/api/v1/artifacts/{page.id}/comments",
            json={"body": "hi", "thread_root": cid},
            headers=auth,
        )

    def resolve(cid):
        return client.post(f"/api/v1/comments/{cid}/resolve", headers=auth)

    for probe in (reply, resolve):
        a, b = probe(held.id), probe(missing)
        assert (a.status_code, a.json()) == (b.status_code, b.json())
        assert a.status_code in (404, 422)
    with db.connect() as conn:
        still = conn.execute(
            "select resolved_at from comment where id = %s", (held.id,)
        ).fetchone()[0]
    assert still is None


def test_resolving_a_held_mark_tells_the_agent_nothing(db, nudges):
    page = _page(db)
    decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    held = _mark(db, page)
    comments.resolve(db, held.id, by="jeremy")
    with db.connect() as conn:
        seen = conn.execute(
            "select count(*) from event where ref_id = %s", (held.id,)
        ).fetchone()[0]
        stamped = conn.execute(
            "select resolved_at from comment where id = %s", (held.id,)
        ).fetchone()[0]
    assert seen == 0
    assert stamped is not None
    assert nudges == []


def _gate_after(monkeypatch, module, name):
    """Wrap module.name so the caller pauses AFTER it returns, until released."""
    import threading

    reached, go = threading.Event(), threading.Event()
    real = getattr(module, name)

    def wrapped(*args, **kwargs):
        out = real(*args, **kwargs)
        reached.set()
        assert go.wait(10)
        return out

    monkeypatch.setattr(module, name, wrapped)
    return reached, go


def test_a_verdict_that_lands_before_the_mark_leaves_it_unheld(db, monkeypatch):
    import threading

    from artifacts import review, store

    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    reached, go = _gate_after(monkeypatch, store, "_check_secrets")
    out = {}

    def add():
        out["row"] = _mark(db, page)

    t = threading.Thread(target=add)
    t.start()
    assert reached.wait(10)
    monkeypatch.undo()
    decisions.answer(db, row.id, letter="c", by="jeremy")
    go.set()
    t.join(20)
    assert out["row"].held is False
    message, _ = review.render(db, row.id, agent="seat")
    assert "Rotate" in message


def test_a_verdict_that_waits_for_the_mark_releases_it(db, monkeypatch):
    import threading
    import time

    from artifacts import review

    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    reached, go = _gate_after(monkeypatch, holds, "page_mode")
    out = {}

    t = threading.Thread(target=lambda: out.update(row=_mark(db, page)))
    t.start()
    assert reached.wait(10)
    verdict = threading.Thread(
        target=lambda: decisions.answer(db, row.id, letter="c", by="jeremy")
    )
    verdict.start()
    time.sleep(1)
    assert verdict.is_alive(), "the verdict waits for the insert's lock"
    go.set()
    t.join(20)
    verdict.join(20)
    assert out["row"].held is True
    [stored] = comments.list_for_artifact(db, page.id)
    assert stored.id == out["row"].id and stored.held is False
    message, _ = review.render(db, row.id, agent="seat")
    assert "Rotate" in message
