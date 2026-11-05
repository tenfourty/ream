"""Reopening a settled comment thread tells the page's agent (5 Nov 2026,
runcove-5t3oj.79): a `thread_reopened` event in the same transaction, a nudge after
it, and `artifacts-decide wait` waking on it. An owner's own reopen stays silent,
as its own resolve does."""

import json
from importlib import resources

import pytest
from fastapi.testclient import TestClient
from test_client import key_file, live  # noqa: F401

from artifacts import comments, decide, events, keys, nudge, store
from artifacts import db as dbmod
from artifacts.app import create_gallery_app
from artifacts.db import Database

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"


@pytest.fixture()
def page(db, key_file):
    return store.publish(db, "ops-seat", "Board", PAGE, session="cove/1")


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db), base_url="https://testserver")


def _kinds(db, agent="ops-seat"):
    return [e.kind for e in events.list_for_agent(db, agent)]


def _settled(db, page, body="Can the chart go first?", quote="the velocity chart"):
    root = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body=body,
        anchor={"kind": "range", "path": "/p[1]", "start": 0, "end": 5},
        quote=quote,
    )
    comments.resolve(db, root.id, by="jeremy")
    return root


def test_his_reopen_writes_an_event_for_the_owner(db, page):
    root = _settled(db, page)
    comments.reopen(db, root.id, agent=None, by="jeremy")
    event = events.list_for_agent(db, "ops-seat")[-1]
    assert event.kind == "thread_reopened"
    assert event.actor == "jeremy"
    assert event.ref_id == root.id
    assert event.agent == "ops-seat"
    assert event.session == "cove/1"
    assert "the velocity chart" in event.summary


def test_a_reply_id_names_the_root_in_the_event(db, page):
    root = _settled(db, page)
    reply = comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="ops-seat",
        body="done",
        thread_root=root.id,
        agent="ops-seat",
    )
    comments.resolve(db, root.id, by="jeremy")
    comments.reopen(db, reply.id, agent=None, by="jeremy")
    assert events.list_for_agent(db, "ops-seat")[-1].ref_id == root.id


def test_the_event_is_pushed_after_the_row_is_durable(db, page, monkeypatch):
    root = _settled(db, page)
    seen = []
    monkeypatch.setattr(nudge, "emit", lambda **kw: seen.append(kw))
    comments.reopen(db, root.id, agent=None, by="jeremy")
    (sent,) = seen
    assert sent["kind"] == "thread_reopened" and sent["agent"] == "ops-seat"
    assert sent["event_id"] == str(events.list_for_agent(db, "ops-seat")[-1].seq)


def test_the_nudge_has_its_own_line():
    assert nudge._ALERTNAMES["thread_reopened"] == "Jeremy reopened a thread"


def test_reopening_an_open_thread_says_nothing(db, page):
    root = _settled(db, page)
    comments.reopen(db, root.id, agent=None, by="jeremy")
    before = _kinds(db)
    comments.reopen(db, root.id, agent=None, by="jeremy")
    assert _kinds(db) == before


def test_the_owner_reopening_its_own_thread_is_silent(db, page):
    root = comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="ops-seat",
        body="mine",
        agent="ops-seat",
    )
    comments.resolve(db, root.id, by="ops-seat", agent="ops-seat")
    comments.reopen(db, root.id, agent="ops-seat", by="ops-seat")
    assert "thread_reopened" not in _kinds(db)


def test_a_journal_failure_takes_the_reopen_with_it(db, page, monkeypatch):
    root = _settled(db, page)

    def boom(*_a, **_kw):
        raise RuntimeError("journal down")

    monkeypatch.setattr(events, "append", boom)
    with pytest.raises(RuntimeError):
        comments.reopen(db, root.id, agent=None, by="jeremy")
    rows = comments.list_for_artifact(db, page.id, include_resolved=True)
    assert all(r.resolved_at is not None for r in rows)


def test_the_gallery_button_writes_the_event(db, page, client):
    root = _settled(db, page)
    got = client.post(
        f"/c/{root.id}/reopen",
        headers={"Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert got.status_code == 303
    event = events.list_for_agent(db, "ops-seat")[-1]
    assert (event.kind, event.actor, event.ref_id) == (
        "thread_reopened",
        "jeremy",
        root.id,
    )


def test_the_api_reopen_by_the_owner_works_and_is_silent(db, page, client):
    token, _ = keys.mint(db, agent="ops-seat")
    root = _settled(db, page)
    got = client.post(
        f"/api/v1/comments/{root.id}/reopen",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert got.status_code == 200 and got.json()["resolved_at"] is None
    assert "thread_reopened" not in _kinds(db)


def test_the_waiter_wakes_on_his_reopen_and_prints_the_thread(
    db,
    live,
    key_file,
    page,
    capsys,
    monkeypatch,
    on_first_listen,
):
    path, _ = key_file
    root = _settled(db, page)
    comments.add(  # the last word is the agent's, so nothing is "owed" a reply
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="ops-seat",
        body="moved it",
        thread_root=root.id,
        agent="ops-seat",
        session="cove/1",
    )
    comments.resolve(db, root.id, by="jeremy")
    listener = on_first_listen(
        lambda: comments.reopen(db, root.id, agent=None, by="jeremy")
    )
    monkeypatch.setenv("ARTIFACTS_SESSION", "cove/1")
    code = decide.main(
        ["--key-file", str(path), "wait", "--page", page.id, "--timeout", "40s"]
    )
    listener.woke_the_first_hold()
    out = capsys.readouterr().out
    assert code == 0
    assert "REOPENED by jeremy" in out and root.id in out
    assert "the velocity chart" in out


def test_the_waiter_says_so_in_json_too(
    db,
    live,
    key_file,
    page,
    capsys,
    monkeypatch,
    on_first_listen,
):
    path, _ = key_file
    root = _settled(db, page)
    listener = on_first_listen(
        lambda: comments.reopen(db, root.id, agent=None, by="jeremy")
    )
    monkeypatch.setenv("ARTIFACTS_SESSION", "cove/1")
    code = decide.main(
        [
            "--key-file",
            str(path),
            "wait",
            "--page",
            page.id,
            "--json",
            "--timeout",
            "40s",
        ]
    )
    listener.woke_the_first_hold()
    assert code == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("{"))
    (thread,) = json.loads(line)["threads"]
    assert thread["id"] == root.id and thread["reopened_by"] == "jeremy"


def test_answers_only_does_not_wake_on_a_reopen(
    db,
    live,
    key_file,
    page,
    capsys,
    monkeypatch,
    on_first_listen,
):
    path, _ = key_file
    root = _settled(db, page)
    on_first_listen(lambda: comments.reopen(db, root.id, agent=None, by="jeremy"))
    monkeypatch.setenv("ARTIFACTS_SESSION", "cove/1")
    code = decide.main(
        [
            "--key-file",
            str(path),
            "wait",
            "--page",
            page.id,
            "--answers-only",
            "--timeout",
            "3s",
        ]
    )
    assert code == 3


def test_fmt_thread_names_who_reopened_it():
    thread = {
        "id": "t1",
        "artifact_id": "a1",
        "needs_reply": False,
        "quote": "",
        "messages": [{"author_kind": "human", "body": "hm"}],
        "reopened_by": "jeremy",
    }
    assert "REOPENED by jeremy" in decide._fmt_thread(thread)


def test_the_kind_is_known_to_the_journal():
    assert "thread_reopened" in events.KINDS


def test_sqlite_step_18_widens_the_event_check(tmp_path):
    """A file left by step 17 refuses the kind; migrating accepts it, twice."""
    database = Database(f"sqlite:///{tmp_path / 'v17.db'}")
    script = resources.files("artifacts").joinpath("schema_sqlite.sql").read_text()
    with database.connect() as conn, conn.transaction():
        for statement in dbmod._statements(script):
            conn.execute(statement)
        for number in range(2, 18):
            for statement in dbmod._SQLITE_STEPS[number]:
                if callable(statement):
                    statement(conn)
                else:
                    conn.execute(statement)
        conn.execute("pragma user_version = 17")
    insert = (
        "insert into event (artifact_id, agent, actor, kind)"
        " values ('a', 'x', 'jeremy', 'thread_reopened')"
    )

    def put() -> None:
        with database.connect() as conn:
            conn.execute("pragma foreign_keys = off")
            conn.execute(insert)

    with pytest.raises(Exception, match="(?i)constraint"):
        put()
    database.migrate()
    database.migrate()
    put()
    with database.connect() as conn:
        assert conn.execute("pragma user_version").fetchone()[0] == (
            dbmod.SQLITE_SCHEMA_VERSION
        )
    assert dbmod.SQLITE_SCHEMA_VERSION >= 18
