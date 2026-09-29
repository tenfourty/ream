"""Comments are saved at once; GitGuardian checks agents' just after
(homelab-ij8.112). Jeremy's never wait on it."""

import logging

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
    assert gg.calls == ["hello there"]


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
