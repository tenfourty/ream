"""Hold or live (page review step 3, homelab-ij8.108): a mark Jeremy makes on a
page under a held review is invisible to the agent."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from ream import comments, decisions, holds, keys, nudge, store
from ream.app import create_gallery_app
from ream.db import Database, in_list

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


def _until_waiting_on_a_lock(db, timeout=10.0):
    """Return once some session in this test's database is blocked on a lock:
    the positive evidence a racing statement is really waiting, instead of a
    sleep that hopes it got there. Fails loudly if nothing ever waits."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db.connect() as conn:
            (waiting,) = conn.execute(
                "select count(*) from pg_stat_activity"
                " where datname = current_database() and wait_event_type = 'Lock'"
            ).fetchone()
        if waiting:
            return
        time.sleep(0.005)
    raise AssertionError("no statement ever waited on a lock")


def _gate_after(monkeypatch, module, name, seen=None):
    """Wrap module.name so the caller pauses AFTER it returns, until released.
    A list passed as `seen` gets what the real function returned, appended
    before the pause: what the caller was told, read in its own thread, not
    through a row another session can change once the caller commits."""
    import threading

    reached, go = threading.Event(), threading.Event()
    real = getattr(module, name)

    def wrapped(*args, **kwargs):
        out = real(*args, **kwargs)
        if seen is not None:
            seen.append(out)
        reached.set()
        assert go.wait(10)
        return out

    monkeypatch.setattr(module, name, wrapped)
    return reached, go


def test_a_verdict_that_lands_before_the_mark_leaves_it_unheld(db, monkeypatch):
    import threading

    from ream import review, store

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


@pytest.mark.postgres_only  # waits for a lock through pg_stat_activity
def test_a_verdict_that_waits_for_the_mark_releases_it(db, monkeypatch):
    import threading

    from ream import review

    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    seen = []
    reached, go = _gate_after(monkeypatch, holds, "page_mode", seen=seen)
    out = {}

    t = threading.Thread(target=lambda: out.update(row=_mark(db, page)))
    t.start()
    assert reached.wait(10)
    verdict = threading.Thread(
        target=lambda: decisions.answer(db, row.id, letter="c", by="jeremy")
    )
    verdict.start()
    _until_waiting_on_a_lock(db)
    assert verdict.is_alive(), "the verdict waits for the insert's lock"
    go.set()
    t.join(20)
    verdict.join(20)
    # The mark went in held. Not `out["row"].held`: `comments.add` reads the row
    # back after it commits, and the verdict that was waiting may release it first.
    assert seen == ["held"]
    [stored] = comments.list_for_artifact(db, page.id)
    assert stored.id == out["row"].id and stored.held is False
    message, _ = review.render(db, row.id, agent="seat")
    assert "Rotate" in message


# ---- release: verdict, flip, close or shelve (task 2) ----


def _held_review(db, marks=2, agent="seat"):
    page = _page(db, agent)
    row = decisions.ask_review(db, agent=agent, artifact_id=page.id, mode="held")
    made = [_mark(db, page) for _ in range(marks)]
    assert all(m.held for m in made)
    return page, row, made


def _comment_events(db, ids):
    with db.connect() as conn:
        return conn.execute(
            "select ref_id from event where kind = 'comment_added'"  # noqa: S608 -- placeholders only
            f" and ref_id in {in_list(list(ids))}",
            tuple(ids),
        ).fetchall()


def _held_flags(db, page):
    return {
        c.id: c.held for c in comments.list_for_artifact(db, page.id, include_held=True)
    }


def test_a_verdict_releases_his_held_marks_without_extra_events(db, nudges):
    from ream import review

    page, row, made = _held_review(db)
    decisions.answer(db, row.id, letter="c", by="jeremy")
    assert not any(_held_flags(db, page).values())
    assert _comment_events(db, [m.id for m in made]) == []
    message, _ = review.render(db, row.id, agent="seat")
    assert message.count("Rotate") >= 2


def test_approved_with_held_marks_counts_as_approved_with_notes(db, nudges):
    page, row, _ = _held_review(db, marks=1)
    assert decisions.answer(db, row.id, letter="a", by="jeremy").answer == "b"

    page, row, _ = _held_review(db, marks=1)
    out = decisions.record_elsewhere(
        db, row.id, letter="a", note="ok", source="telegram", recorded_by="seat"
    )
    assert out.answer == "b"

    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    assert decisions.answer(db, row.id, letter="a", by="jeremy").answer == "a"

    page, row, [mark] = _held_review(db, marks=1)
    with db.connect() as conn:
        conn.execute(
            "update comment set resolved_at = %s where id = %s",
            (datetime.now(UTC), mark.id),
        )
    assert decisions.answer(db, row.id, letter="a", by="jeremy").answer == "a"


def test_dismissed_releases_them_as_context(db, nudges):
    from ream import review

    page, row, _ = _held_review(db)
    decisions.answer(db, row.id, letter="d", by="jeremy")
    assert not any(_held_flags(db, page).values())
    message, _ = review.render(db, row.id, agent="seat")
    assert "Rotate" in message


def test_flipping_to_live_releases_with_events_and_remembers(db, nudges):
    page, row, made = _held_review(db, marks=1)
    reply = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        kind="comment",
        body="and more",
        thread_root=made[0].id,
    )
    assert reply.held
    extra = _mark(db, page)
    assert holds.set_review_mode(db, row.id, "live") == (page.id, 3)
    assert len(_comment_events(db, [made[0].id, extra.id])) == 2
    assert _comment_events(db, [reply.id]) == []
    with db.connect() as conn:
        assert holds.get_mode(conn) == "live"
    assert nudges == []
    assert _mark(db, page).held is False


def test_flipping_back_to_held_holds_new_marks_only(db, nudges):
    page, row, _ = _held_review(db, marks=0)
    holds.set_review_mode(db, row.id, "live")
    first = _mark(db, page)
    assert first.held is False
    holds.set_review_mode(db, row.id, "held")
    assert _mark(db, page).held is True
    assert _held_flags(db, page)[first.id] is False


def test_a_settled_or_non_review_decision_cannot_be_flipped(db):
    page, answered, _ = _held_review(db, marks=0)
    decisions.answer(db, answered.id, letter="c", by="jeremy")
    closed = decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    decisions.close(db, "seat", closed.id)
    question = decisions.ask(
        db,
        agent="seat",
        artifact_id=page.id,
        question="Q?",
        options=[{"letter": "a", "label": "x"}, {"letter": "b", "label": "y"}],
    )
    with db.connect() as conn:
        holds.set_mode(conn, "held")
    for did, exc in (
        (answered.id, decisions.AlreadyDecided),
        (closed.id, decisions.AlreadyDecided),
        (question.id, store.NotFound),
    ):
        with pytest.raises(exc):
            holds.set_review_mode(db, did, "live")
        with db.connect() as conn:
            assert holds.get_mode(conn) == "held"
    with pytest.raises(ValueError):
        holds.set_review_mode(db, closed.id, "loud")


def test_closing_or_archiving_releases_held_marks(db, nudges):
    page, row, made = _held_review(db, marks=2)
    decisions.close(db, "seat", row.id)
    assert not any(_held_flags(db, page).values())
    assert len(_comment_events(db, [m.id for m in made])) == 2

    page, row, made = _held_review(db, marks=2)
    assert store.set_archived(db, page.id, True, agent="seat")
    assert not any(_held_flags(db, page).values())
    assert len(_comment_events(db, [m.id for m in made])) == 2
    assert nudges == []


@pytest.mark.postgres_only  # waits for a lock through pg_stat_activity
def test_a_flip_racing_approved_never_drops_a_mark(db, monkeypatch, nudges):
    import threading

    from ream import review

    page, row, _made = _held_review(db, marks=2)
    reached, go = _gate_after(monkeypatch, holds, "approved_letter")
    out = {}

    def approve():
        out["verdict"] = decisions.answer(db, row.id, letter="a", by="jeremy")

    def flip():
        try:
            out["flip"] = holds.set_review_mode(db, row.id, "live")
        except decisions.AlreadyDecided as err:
            out["flip"] = err

    t = threading.Thread(target=approve)
    t.start()
    assert reached.wait(10)
    f = threading.Thread(target=flip)
    f.start()
    _until_waiting_on_a_lock(db)
    assert f.is_alive(), "the flip waits for the verdict's row lock"
    go.set()
    t.join(20)
    f.join(20)
    assert out["verdict"].answer == "b"
    assert not any(_held_flags(db, page).values())
    message, _ = review.render(db, row.id, agent="seat")
    assert message.count("Rotate") >= 2


def test_a_flip_that_lands_first_makes_approved_plain(db, nudges):
    _page, row, made = _held_review(db, marks=1)
    holds.set_review_mode(db, row.id, "live")
    assert decisions.answer(db, row.id, letter="a", by="jeremy").answer == "a"
    assert len(_comment_events(db, [made[0].id])) == 1


def test_acknowledging_an_open_held_review_releases_nothing(db, nudges):
    client = TestClient(create_gallery_app(db))
    token, _ = keys.mint(db, agent="seat")
    auth = {"Authorization": f"Bearer {token}"}
    page, row, made = _held_review(db, marks=2)
    r = client.post(f"/api/v1/decisions/{row.id}/acknowledge", headers=auth)
    assert r.status_code == 200
    assert all(_held_flags(db, page).values())
    assert _comment_events(db, [m.id for m in made]) == []


def test_flipping_one_review_to_live_leaves_another_reviews_marks_held(db, nudges):
    page, first, made = _held_review(db, marks=2)
    second = decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    assert holds.set_review_mode(db, first.id, "live") == (page.id, 0)
    assert all(_held_flags(db, page).values())
    assert _comment_events(db, [m.id for m in made]) == []
    assert holds.set_review_mode(db, second.id, "live") == (page.id, 2)
    assert not any(_held_flags(db, page).values())


@pytest.mark.postgres_only  # waits for a lock through pg_stat_activity
def test_an_archive_racing_a_mark_leaves_no_mark_held_on_a_closed_review(
    db, monkeypatch, nudges
):
    import threading

    page, _row, _ = _held_review(db, marks=1)
    seen = []
    reached, go = _gate_after(monkeypatch, holds, "page_mode", seen=seen)
    out = {}
    t = threading.Thread(target=lambda: out.update(row=_mark(db, page)))
    t.start()
    assert reached.wait(10)
    a = threading.Thread(
        target=lambda: store.set_archived(db, page.id, True, agent="seat")
    )
    a.start()
    _until_waiting_on_a_lock(db)
    assert a.is_alive(), "the archive waits for the insert's share lock"
    go.set()
    t.join(20)
    a.join(20)
    # The mark went in held. Not `out["row"].held`: `comments.add` reads the row
    # back after it commits, and the archive that was waiting may release it first
    # (CI run 45332 saw exactly that: the row came back held=False).
    assert seen == ["held"]
    assert not any(_held_flags(db, page).values())
    assert len(_comment_events(db, [out["row"].id])) == 1


# ---- the switch and the held marks drawn for him (task 3) ----

SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


@pytest.fixture()
def gclient(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def _mode_post(client, rid, mode, headers=SAME_ORIGIN):
    return client.post(
        f"/d/{rid}/mode",
        data={"mode": mode},
        headers=headers,
        follow_redirects=False,
    )


def test_the_held_card_offers_the_switch_and_counts_held_marks(db, gclient, nudges):
    page, row, _ = _held_review(db, marks=2)
    html = gclient.get(f"/a/{page.id}").text
    assert "Holding your marks until your verdict" in html
    assert f'action="/d/{row.id}/mode"' in html
    assert 'name="mode" value="live"' in html and "Send them as I go" in html
    assert "2 marks held" in html
    assert "Approved with held marks counts as Approved with notes." in html


def test_one_held_mark_is_singular_and_none_shows_no_count(db, gclient, nudges):
    page, _row, _ = _held_review(db, marks=1)
    assert "1 mark held" in gclient.get(f"/a/{page.id}").text
    empty, _row2, _ = _held_review(db, marks=0)
    html = gclient.get(f"/a/{empty.id}").text
    assert "Holding your marks until your verdict" in html
    assert "marks held" not in html and "mark held" not in html
    assert "counts as Approved with notes" not in html


def test_flipping_to_live_releases_and_shows_the_live_card(db, gclient, nudges):
    page, row, _made = _held_review(db, marks=2)
    resp = _mode_post(gclient, row.id, "live")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/a/{page.id}"
    html = gclient.get(f"/a/{page.id}").text
    assert "Sending your marks as you make them" in html
    assert "Hold them until my verdict" in html
    assert 'name="mode" value="held"' in html
    assert "Holding your marks until your verdict" not in html
    assert not any(_held_flags(db, page).values())
    back = _mode_post(gclient, row.id, "held")
    assert back.status_code == 303
    assert "Holding your marks until your verdict" in gclient.get(f"/a/{page.id}").text


def test_the_switch_refuses_bad_modes_questions_settled_and_cross_origin(
    db, gclient, nudges
):
    page, row, _ = _held_review(db, marks=1)
    assert _mode_post(gclient, row.id, "loud").status_code == 422
    assert _mode_of(db, row.id) == "held"
    q = decisions.ask(
        db,
        agent="seat",
        artifact_id=page.id,
        question="q?",
        options=[{"letter": "a", "label": "x"}, {"letter": "b", "label": "y"}],
    )
    assert _mode_post(gclient, q.id, "live").status_code == 404
    cross = gclient.post(
        f"/d/{row.id}/mode",
        data={"mode": "live"},
        headers={"sec-fetch-site": "cross-site"},
        follow_redirects=False,
    )
    assert cross.status_code == 403
    assert _mode_of(db, row.id) == "held"
    decisions.answer(db, row.id, letter="c", by="jeremy")
    settled = _mode_post(gclient, row.id, "live")
    assert settled.status_code == 409
    assert "Changes requested" in settled.text


def test_the_panel_tags_a_held_row_and_the_page_gets_the_flag(db, gclient, nudges):
    page, row, _made = _held_review(db, marks=1)
    html = gclient.get(f"/a/{page.id}").text
    assert '<span class="htag">held</span>' in html
    assert '"held": true' in html or '"held":true' in html
    _mode_post(gclient, row.id, "live")
    html = gclient.get(f"/a/{page.id}").text
    assert '<span class="htag">held</span>' not in html
    assert '"held": false' in html or '"held":false' in html


def test_the_shim_names_the_held_highlight():
    from ream import content

    source = content._viewer_report()
    assert "ar-held" in source and "underline dotted" in source


def test_a_flip_on_a_review_the_agent_closed_says_so(db, gclient, nudges):
    page, row, _ = _held_review(db, marks=1)
    decisions.close(db, "seat", row.id)
    resp = _mode_post(gclient, row.id, "live")
    assert resp.status_code == 409
    assert "None" not in resp.text
    assert "The agent closed this review, so there is nothing left to switch." in (
        resp.text
    )
    assert f'href="/a/{page.id}"' in resp.text
    assert _mode_of(db, row.id) == "held"


def test_the_card_hint_follows_the_mode(db, gclient, nudges):
    page, row, _ = _held_review(db, marks=1)
    held_hint = "Your marks on this page go with your verdict."
    live_hint = (
        "Each mark reaches the agent as you make it, and all of them go with "
        "your verdict. Your first verdict is final."
    )
    html = gclient.get(f"/a/{page.id}").text
    assert held_hint in html and live_hint not in html
    _mode_post(gclient, row.id, "live")
    html = gclient.get(f"/a/{page.id}").text
    assert live_hint in html and held_hint not in html


def test_a_mixed_page_shows_the_count_and_hint_on_every_open_review(
    db, gclient, nudges
):
    page, held_row, _ = _held_review(db, marks=1)
    live_row = decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="live")
    html = gclient.get(f"/a/{page.id}").text
    assert html.count("1 mark held") == 2
    assert html.count("Approved with held marks counts as Approved with notes.") == 2
    assert live_row.id in html and held_row.id in html


def test_a_released_reply_in_a_visible_thread_gets_its_own_event(db, nudges):
    page, row, _ = _held_review(db, marks=0)
    root = comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        body="a remark",
        agent="seat",
    )
    reply = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        kind="comment",
        body="my answer",
        thread_root=root.id,
    )
    assert reply.held
    assert holds.set_review_mode(db, row.id, "live") == (page.id, 1)
    assert len(_comment_events(db, [reply.id])) == 1


def test_a_resolved_root_is_released_without_an_event(db, nudges):
    page, row, [mark, other] = _held_review(db, marks=2)
    with db.connect() as conn:
        conn.execute(
            "update comment set resolved_at = %s where id = %s",
            (datetime.now(UTC), mark.id),
        )
    assert holds.set_review_mode(db, row.id, "live") == (page.id, 2)
    assert _comment_events(db, [mark.id]) == []
    assert len(_comment_events(db, [other.id])) == 1
    assert not any(_held_flags(db, page).values())


def test_a_review_is_released_when_its_page_is_set_aside(db, nudges):
    """Archiving sets a review's question aside (it stays open): the marks he
    held go to the agent now, and the question and review both count as set aside."""
    page = _page(db)
    decisions.ask(
        db,
        agent="seat",
        artifact_id=page.id,
        question="Q?",
        options=[{"letter": "a", "label": "x"}, {"letter": "b", "label": "y"}],
    )
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    mark = _mark(db, page)
    assert mark.held is True
    assert store.set_archived(db, page.id, True, agent="seat")
    assert not any(_held_flags(db, page).values())
    assert len(_comment_events(db, [mark.id])) == 1
    with db.connect() as conn:
        aside = conn.execute(
            "select count(*) from event where kind = 'decision_set_aside'"
            " and artifact_id = %s",
            (page.id,),
        ).fetchone()[0]
        closed = conn.execute(
            "select count(*) from decision where artifact_id = %s"
            " and closed_at is not null",
            (page.id,),
        ).fetchone()[0]
    assert aside == 2
    assert closed == 0
    assert row.id
