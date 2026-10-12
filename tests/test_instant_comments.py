"""Comments are saved at once; GitGuardian checks agents' just after
(homelab-ij8.112). Jeremy's never wait on it."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from artifacts import comments, decisions, ggscan, keys, review, store
from artifacts.app import create_gallery_app
from artifacts.scan import Finding

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
AWS = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db=db), base_url="https://testserver")


@pytest.fixture()
def auth(db):
    token, _ = keys.mint(db, agent="seat")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def page(db):
    return store.publish(db, "seat", "Board", PAGE)


@pytest.fixture()
def gg(monkeypatch):
    """Counts relay calls; `gg.result` is what it answers."""

    class Relay:
        def __init__(self):
            self.calls: list[str] = []
            self.result: list | None = []

    relay = Relay()

    def scan(body):
        relay.calls.append(body)
        return relay.result

    monkeypatch.setattr(ggscan, "scan", scan)
    monkeypatch.setattr(ggscan, "configured", lambda: True)
    return relay


def _post(client, auth, page, body, **extra):
    return client.post(
        f"/api/v1/artifacts/{page.id}/comments",
        json={"body": body, **extra},
        headers=auth,
    )


def _events(db, kind):
    with db.connect() as conn:
        return conn.execute(
            "select agent, summary, ref_id, note from event where kind = %s", (kind,)
        ).fetchall()


def test_his_comment_never_waits_on_ggshield(client, db, page, gg):
    got = client.post(
        f"/c/{page.id}",
        data={"body": "fine"},
        headers={"Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert got.status_code == 303
    assert gg.calls == []
    refused = client.post(
        f"/c/{page.id}",
        data={"body": f"key {AWS}"},
        headers={"Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert refused.status_code != 303
    assert [r.body for r in comments.list_for_artifact(db, page.id)] == ["fine"]
    assert gg.calls == []


def test_an_agent_comment_is_saved_then_scanned(client, auth, page, gg):
    got = _post(client, auth, page, "hello there")
    assert got.status_code == 201
    assert gg.calls == ["hello there\n"]


def test_a_finding_withdraws_it_and_tells_the_agent(client, db, auth, page, gg, caplog):
    gg.result = [Finding(shape="ggshield_slack_bot_token", line=1)]
    caplog.set_level(logging.WARNING)
    got = _post(
        client,
        auth,
        page,
        "the token is hunter2",
        anchor={"kind": "range", "path": "p", "start": 0, "end": 1},
        quote="x",
    )
    assert got.status_code == 201
    cid = got.json()["id"]
    assert comments.list_for_artifact(db, page.id) == []
    assert comments.list_for_agent(db, "seat") == []
    assert comments.across_pages(db) == []
    assert comments.marks_for_page(db, page.id) == []
    assert (
        client.get(f"/api/v1/artifacts/{page.id}/comments", headers=auth).json() == []
    )
    rows = _events(db, "comment_withdrawn")
    assert rows == [
        (
            "seat",
            (
                "Your comment was withdrawn: it looked like a "
                "ggshield_slack_bot_token. Nothing of it was kept visible."
            ),
            cid,
            "",
        )
    ]
    with db.connect() as conn:
        stored = conn.execute(
            "select body, quote, withdrawn_kind from comment where id = %s", (cid,)
        ).fetchone()
    assert stored == ("", "", "ggshield_slack_bot_token")
    assert f"comment withdrawn: agent=seat artifact={page.id} comment={cid}" in (
        caplog.text
    )
    assert "hunter2" not in caplog.text


def test_an_unreachable_relay_keeps_it_and_logs(client, db, auth, page, gg, caplog):
    gg.result = None
    caplog.set_level(logging.WARNING)
    assert _post(client, auth, page, "still here").status_code == 201
    assert [r.body for r in comments.list_for_artifact(db, page.id)] == ["still here"]
    assert "comment kept on the built-in scan only" in caplog.text
    assert "reason=ggshield_unavailable" in caplog.text
    assert _events(db, "comment_withdrawn") == []


def test_a_withdrawn_root_hides_its_replies_without_breaking(
    client, db, auth, page, gg
):
    root = comments.add(
        db, artifact_id=page.id, author_kind="agent", author="seat", body="root"
    )
    reply = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="reply",
        thread_root=root.id,
    )
    assert comments.withdraw(db, root.id, kind="ggshield_x") is True
    assert comments.withdraw(db, root.id, kind="ggshield_x") is False
    assert comments.withdraw(db, "nope", kind="ggshield_x") is False
    assert comments.list_for_artifact(db, page.id) == []
    assert comments.list_for_agent(db, "seat") == []
    assert comments.across_pages(db) == []
    for r in (root, reply):
        with pytest.raises(store.NotFound):
            comments.resolve(db, r.id, by="jeremy", agent=None)
    with pytest.raises(comments.NoSuchThread):
        comments.add(
            db,
            artifact_id=page.id,
            author_kind="human",
            author="jeremy",
            body="late",
            thread_root=root.id,
        )
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    decisions.answer(db, row.id, letter="c", by="jeremy")
    message, view = review.render(db, row.id, agent="seat")
    assert "reply" not in message + str(view)


def test_publish_still_waits_for_ggshield(db, gg):
    store.publish(db, "seat", "Board", PAGE)
    assert len(gg.calls) == 1


# --- durable scanning ---------------------------------------------------------


def _age(db, cid, seconds=120):
    with db.connect() as conn:
        conn.execute(
            "update comment set created_at = %s where id = %s",
            (datetime.now(UTC) - timedelta(seconds=seconds), cid),
        )


def _stamped(db, cid):
    with db.connect() as conn:
        return conn.execute(
            "select gg_scanned_at is not null from comment where id = %s", (cid,)
        ).fetchone()[0]


def _agent_comment(db, page, body="hello", **kw):
    return comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        body=body,
        **kw,
    )


def test_the_sweep_scans_an_old_unscanned_agent_comment_and_stamps_it(db, page, gg):
    row = _agent_comment(db, page)
    _age(db, row.id)
    assert comments.sweep_unscanned(db) == 1
    assert gg.calls == ["hello\n"]
    assert _stamped(db, row.id)
    assert comments.sweep_unscanned(db) == 0


def test_the_sweep_skips_what_it_should(db, page, gg):
    human = comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="h"
    )
    gone = _agent_comment(db, page, "gone")
    done = _agent_comment(db, page, "done")
    new = _agent_comment(db, page, "new")
    comments.withdraw(db, gone.id, kind="ggshield_x")
    with db.connect() as conn:
        conn.execute(
            "update comment set gg_scanned_at = %s where id = %s",
            (datetime.now(UTC), done.id),
        )
    for r in (human, gone, done):
        _age(db, r.id)
    assert comments.sweep_unscanned(db) == 0
    assert gg.calls == []
    assert not _stamped(db, new.id)


def test_an_unavailable_relay_leaves_it_unstamped_for_the_next_pass(db, page, gg):
    row = _agent_comment(db, page)
    _age(db, row.id)
    gg.result = None
    comments.sweep_unscanned(db)
    assert not _stamped(db, row.id)
    gg.result = []
    assert comments.sweep_unscanned(db) == 1
    assert _stamped(db, row.id)


def test_a_finding_in_the_quote_withdraws_it(db, page, gg):
    row = _agent_comment(
        db,
        page,
        "fine",
        anchor={"kind": "range", "path": "p", "start": 0, "end": 1},
        quote="secretish",
    )
    gg.result = [Finding(shape="ggshield_x", line=2)]
    comments.second_scan(db, row.id)
    assert gg.calls == ["fine\nsecretish"]
    assert comments.list_for_artifact(db, page.id) == []


def test_withdrawing_a_root_blanks_its_text_in_thread_resolved_events(db, page, gg):
    root = _agent_comment(db, page, "the secret body")
    comments.resolve(db, root.id, by="jeremy", agent=None)
    comments.withdraw(db, root.id, kind="ggshield_x")
    rows = _events(db, "thread_resolved")
    assert len(rows) == 1
    assert "secret body" not in rows[0][1] + (rows[0][3] or "")


def test_release_says_nothing_of_replies_under_a_withdrawn_root(db, page):
    from artifacts import holds

    root = _agent_comment(db, page, "root")
    decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="held reply",
        thread_root=root.id,
    )
    comments.withdraw(db, root.id, kind="ggshield_x")
    with db.connect() as conn, conn.transaction():
        holds.release(conn, page.id, emit=True)
    assert _events(db, "comment_added") == []


@pytest.mark.postgres_only  # reads information_schema
def test_a_fresh_agent_comment_is_unscanned_and_migrating_again_keeps_it_so(db, page):
    row = _agent_comment(db, page)
    assert not _stamped(db, row.id)
    db.migrate()
    assert not _stamped(db, row.id)
    with db.connect() as conn:
        default = conn.execute(
            "select column_default from information_schema.columns"
            " where table_name = 'comment' and column_name = 'gg_scanned_at'"
        ).fetchone()[0]
    assert default is None


def test_a_pass_stops_after_two_unavailable_answers_in_a_row(db, page, gg):
    for i in range(3):
        _age(db, _agent_comment(db, page, f"c{i}").id)
    gg.result = None
    assert comments.sweep_unscanned(db) == 0
    assert len(gg.calls) == 2


def test_a_withdrawal_waits_for_an_in_flight_reply(db, page, gg, monkeypatch):
    import threading

    from artifacts import events

    root = _agent_comment(db, page, "root")
    inside, go = threading.Event(), threading.Event()
    real = events.append

    def slow(*a, **kw):
        # Inside the reply's transaction, after its insert, before its commit.
        inside.set()
        assert go.wait(10)
        return real(*a, **kw)

    monkeypatch.setattr(events, "append", slow)
    out = {}

    def reply():
        out["reply"] = comments.add(
            db,
            artifact_id=page.id,
            author_kind="human",
            author="jeremy",
            body="r",
            thread_root=root.id,
        )

    t = threading.Thread(target=reply)
    t.start()
    assert inside.wait(10)
    w = threading.Thread(
        target=lambda: out.update(w=comments.withdraw(db, root.id, kind="ggshield_x"))
    )
    w.start()
    w.join(0.7)
    assert w.is_alive(), "the withdrawal must wait for the reply's transaction"
    go.set()
    t.join(10)
    w.join(10)
    assert out["w"] is True and out["reply"].thread_root == root.id


def test_a_withdrawal_leaves_migrate_working_and_blanks_reply_quotes(db, page, gg):
    hidden_quote = "AKIA-ish-quote-hidden_quote"
    root = _agent_comment(
        db,
        page,
        "root",
        anchor={"kind": "range", "path": "p", "start": 0, "end": 1},
        quote=hidden_quote,
    )
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="reply",
        thread_root=root.id,
    )
    comments.withdraw(db, root.id, kind="ggshield_x")
    db.migrate()  # what every pod start runs, with a comment_withdrawn row present
    with db.connect() as conn:
        held = conn.execute(
            "select count(*) from comment where quote = %s", (hidden_quote,)
        ).fetchone()[0]
    assert held == 0


def test_a_comment_the_relay_always_refuses_does_not_block_a_newer_one(
    db, page, monkeypatch
):
    old = _agent_comment(db, page, "old")
    new = _agent_comment(db, page, "new")
    _age(db, old.id, 300)
    _age(db, new.id, 200)
    seen = []

    def scan(body):
        seen.append(body.split("\n")[0])
        return None if body.startswith("old") else []

    monkeypatch.setattr(ggscan, "scan", scan)
    monkeypatch.setattr(ggscan, "configured", lambda: True)
    comments.sweep_unscanned(db)
    comments.sweep_unscanned(db)
    assert _stamped(db, new.id) and not _stamped(db, old.id)
