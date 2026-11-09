"""Explain, shorten and fix (9 Nov 2026): three kinds of comment he sends from the
comment pill to the page's agent. Kinds of message, not a convention in the body."""

import re
from importlib import resources

import pytest
from fastapi.testclient import TestClient

from ream import comments, decide, events, keys, onboarding, store
from ream import db as dbmod
from ream.app import create_gallery_app
from ream.db import Database

PAGE = "<!doctype html><title>t</title><p>Rotate the keys every 90 days.</p>"
QUOTE = "Rotate the keys every 90 days."
RANGE = {"kind": "range", "path": "body>p:nth-of-type(1)", "start": 0, "end": 29}
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
ASK = ("explain", "shorten", "fix")


def _page(db):
    return store.publish(db, "seat", "Board", PAGE)


def _ask(db, page, kind, body=""):
    return comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body=body,
        kind=kind,
        quote=QUOTE,
        anchor=RANGE,
    )


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


@pytest.mark.parametrize("kind", ASK)
def test_the_kinds_are_stored_as_sent(db, kind):
    assert kind in comments.KINDS
    row = _ask(db, _page(db), kind, "the note")
    assert row.kind == kind and row.body == "the note"


@pytest.mark.parametrize("kind", ASK)
def test_a_bare_tap_gets_a_default_body_in_words(db, kind):
    row = _ask(db, _page(db), kind)
    assert row.body.strip() and row.body == comments.DEFAULT_BODY[kind]
    stored = comments.list_for_artifact(db, row.artifact_id)
    assert [c.body for c in stored] == [comments.DEFAULT_BODY[kind]]


@pytest.mark.parametrize("kind", ASK)
def test_the_event_summary_is_never_empty_and_is_labelled(db, kind):
    page = _page(db)
    _ask(db, page, kind)  # bare
    _ask(db, page, kind, "the 90 looks wrong")
    bare, noted = (e.summary for e in events.list_for_agent(db, "seat"))
    label = kind.capitalize()
    assert bare.strip() and noted.strip()
    assert bare == f'{label}: "{QUOTE}"'
    assert noted == f'{label}: "{QUOTE}" — the 90 looks wrong'


@pytest.mark.parametrize("kind", ASK)
def test_a_request_needs_a_quoted_range_like_a_mark(db, kind):
    page = _page(db)
    with pytest.raises(comments.InvalidKind):
        comments.add(
            db,
            artifact_id=page.id,
            author_kind="human",
            author="jeremy",
            body="",
            kind=kind,
        )


@pytest.mark.parametrize("kind", ASK)
def test_the_gallery_form_takes_a_request_with_no_note(client, db, kind):
    page = _page(db)
    r = client.post(
        f"/c/{page.id}",
        data={
            "kind": kind,
            "body": "",
            "quote": QUOTE,
            "anchor": '{"kind":"range","path":"body>p:nth-of-type(1)","start":0,"end":29}',
        },
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 303
    token, _ = keys.mint(db, agent="seat")
    rows = client.get(
        f"/api/v1/pages/{page.id}/comments",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert [(row["kind"], row["body"]) for row in rows] == [
        (kind, comments.DEFAULT_BODY[kind])
    ]


@pytest.mark.parametrize("kind", ASK)
def test_another_agent_may_not_send_one_on_a_page_it_does_not_own(db, kind):
    page = _page(db)
    with pytest.raises(comments.VerdictRefused):
        comments.add(
            db,
            artifact_id=page.id,
            author_kind="agent",
            author="other",
            body="x",
            kind=kind,
            quote=QUOTE,
            anchor=RANGE,
            agent="other",
        )


def _row(kind, **over):
    row = {
        "id": "c1",
        "artifact_id": "p1",
        "quote": "Rotate the keys",
        "kind": kind,
        "author_kind": "human",
        "author": "jeremy",
        "body": comments.DEFAULT_BODY[kind],
        "created_at": "2026-11-09T10:00:00Z",
        "thread_root": None,
    }
    row.update(over)
    return row


@pytest.mark.parametrize("kind", ASK)
def test_the_agent_sees_the_kind_and_owes_a_reply(kind):
    thread = decide._threads([_row(kind)])[0]
    text = decide._fmt_thread(thread)
    assert f'  {kind.upper()}: "Rotate the keys"' in text
    assert thread["needs_reply"] is True and "NEEDS YOUR REPLY" in text


# ---- the pill and the words agents read ------------------------------------------------


def test_the_pill_offers_the_three_kinds(client):
    html = client.get("/comments.js").text
    assert (
        'var PILL=[["comment","Comment"],["explain","Explain"],'
        '["shorten","Shorten"],["fix","Fix"]]'
    ) in html


def test_onboarding_teaches_the_kinds_and_the_body_route():
    text = onboarding.agents_txt("https://x.example")
    text = " ".join(text.split())  # the text is wrapped; a phrase may span a line
    for word in (
        "EXPLAIN, SHORTEN, FIX",
        "Act on the kind, never by parsing the body",
        "stays open",
        "decide the actual change",
        "page version",
        "GET /api/v1/pages/<id>/body",
    ):
        assert word in text, word
    skill = onboarding.skill_md("https://x.example")
    assert "`explain`, `shorten` or `fix`" in skill


def test_the_body_404_hint_no_longer_calls_it_publisher_only():
    import inspect

    from ream import api

    src = inspect.getsource(api)
    assert 'to the agent that published it"' not in src
    assert "agent that published it, and to any agent while the page is " in src


# ---- SQLite: a file at version 27 (comment.kind still has its CHECK) -------------------


def _build_v27(path) -> Database:
    database = Database(f"sqlite:///{path}")
    script = resources.files("ream").joinpath("schema_sqlite.sql").read_text()
    with database.connect() as conn, conn.transaction():
        for statement in dbmod._statements(script):
            conn.execute(statement)
        for number in range(2, 28):
            for statement in dbmod._SQLITE_STEPS[number]:
                if callable(statement):
                    statement(conn)
                else:
                    conn.execute(statement)
        conn.execute("pragma user_version = 27")
    return database


def test_the_sqlite_step_drops_comment_kinds_check_and_keeps_old_rows(tmp_path):
    database = _build_v27(tmp_path / "v27.db")

    def sql():
        with database.connect() as conn:
            return conn.execute(
                "select sql from sqlite_master where name = 'comment'"
            ).fetchone()[0]

    assert re.search(r"\bkind\s+in", sql())
    with database.connect() as conn, conn.transaction():
        conn.execute(
            "insert into artifact (id, title, agent, read_token, created_at,"
            " updated_at) values ('a', 't', 'x', 'r', datetime('now'), datetime('now'))"
        )
        conn.execute(
            "insert into comment (id, artifact_id, version_n, author_kind, author, body, kind)"
            " values ('c-old', 'a', 1, 'human', 'j', 'b', 'remove')"
        )
    database.migrate()
    assert not re.search(r"\bkind\s+in", sql())
    with database.connect() as conn:
        assert conn.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "select kind from comment where id = 'c-old'"
        ).fetchone() == ("remove",)
    with database.connect() as conn, conn.transaction():
        conn.execute(
            "insert into comment (id, artifact_id, version_n, author_kind, author, body, kind)"
            " values ('c-new', 'a', 1, 'human', 'j', 'b', 'fix')"
        )
        assert conn.execute(
            "select kind from comment where id = 'c-new'"
        ).fetchone() == ("fix",)
