"""Viewer-typed text is flattened before it leaves the service, not after.

An event summary carries what a human typed. Its consumers print it into a
terminal, a log line or an agent's context — places with no renderer, where an
escape sequence moves the cursor and a newline fakes a second line. Sanitising
at each consumer means every future client re-implements it or quietly does
not; sanitising at the boundary means there is no list to keep in step.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, events, keys, text
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"

# What a hostile note can actually be. Postgres refuses a NUL byte in a `text`
# column outright (psycopg raises DataError before the statement is sent), so a
# NUL can never reach the event table by any route -- it is exercised against
# the pure function only, and everything that goes through the database uses
# the escape sequences and newlines that CAN get there.
HOSTILE = "yes\x1b[2J\x07 do this\nsecond line"
HOSTILE_WITH_NUL = HOSTILE + "\x00"


@pytest.fixture()
def client(db):
    return TestClient(create_gallery_app(db=db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ the rule


def test_control_characters_are_removed_not_escaped():
    cleaned = text.sanitize(HOSTILE_WITH_NUL)
    for bad in ("\x1b", "\x07", "\x00", "\\x1b"):
        assert bad not in cleaned
    assert "do this" in cleaned


def test_a_newline_cannot_fake_a_second_line():
    assert "\n" not in text.sanitize("first\nsecond")
    assert text.sanitize("first\nsecond") == "first second"


def test_runs_of_whitespace_collapse():
    assert text.sanitize("a   \t  b\r\n c") == "a b c"


def test_it_is_capped_with_an_ellipsis():
    capped = text.sanitize("x" * 500)
    assert len(capped) == text.SUMMARY_LIMIT
    assert capped.endswith("…")


def test_it_is_idempotent():
    """Applied on write and again on read, so it has to be."""
    once = text.sanitize(HOSTILE_WITH_NUL)
    assert text.sanitize(once) == once


def test_ordinary_text_is_untouched():
    plain = "start with (b) but then (a) if we can"
    assert text.sanitize(plain) == plain


def test_the_nudge_path_uses_the_same_function():
    """It used to own the only copy, which is how the feed went uncleaned."""
    from artifacts import nudge

    assert nudge._sanitize is text.sanitize


# ------------------------------------------------- through the event boundary


def _publish(client, token) -> str:
    return client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()["id"]


def test_an_event_is_stored_flat(db, client):
    token, _ = keys.mint(db, agent="ops-seat")
    artifact_id = _publish(client, token)
    with db.connect() as conn:
        events.append(
            conn,
            artifact_id=artifact_id,
            agent="ops-seat",
            actor="jeremy",
            kind="decision_answered",
            summary=HOSTILE,
        )
        stored = conn.execute(
            "select summary from event where artifact_id = %s", (artifact_id,)
        ).fetchone()[0]

    assert "\x1b" not in stored and "\n" not in stored and "\x07" not in stored


def test_a_row_written_before_this_existed_is_cleaned_on_the_way_out(db, client):
    """The durable fix is on write; rows already in the table predate it, and a
    consumer must not be the first thing to meet one."""
    token, _ = keys.mint(db, agent="ops-seat")
    artifact_id = _publish(client, token)
    with db.connect() as conn:
        conn.execute(
            "insert into event (artifact_id, agent, actor, kind, summary)"
            " values (%s, %s, %s, %s, %s)",
            (artifact_id, "ops-seat", "jeremy", "decision_answered", HOSTILE),
        )

    rows = events.list_for_agent(db, "ops-seat")

    assert rows, "the fixture must produce a row"
    for row in rows:
        assert "\x1b" not in row.summary
        assert "\n" not in row.summary
        assert "\x07" not in row.summary


def test_the_feed_over_http_never_carries_a_control_character(db, client):
    token, _ = keys.mint(db, agent="ops-seat")
    artifact_id = _publish(client, token)
    with db.connect() as conn:
        conn.execute(
            "insert into event (artifact_id, agent, actor, kind, summary)"
            " values (%s, %s, %s, %s, %s)",
            (artifact_id, "ops-seat", "jeremy", "decision_answered", HOSTILE),
        )

    response = client.get("/api/v1/events?since=0&wait=0", headers=auth(token))

    assert response.status_code == 200
    for row in response.json():
        assert "\x1b" not in row["summary"]
        assert "\x07" not in row["summary"]


def test_an_answer_note_reaches_the_feed_flattened(db, client):
    """The real producer: Jeremy's own free text on a decision."""
    token, _ = keys.mint(db, agent="ops-seat")
    artifact_id = _publish(client, token)
    created = client.post(
        f"/api/v1/artifacts/{artifact_id}/decisions",
        json={
            "question": "How should sharing work?",
            "options": [
                {"letter": "a", "label": "Secret link"},
                {"letter": "b", "label": "Named people only"},
            ],
            "recommended": "a",
        },
        headers=auth(token),
    ).json()
    decisions.answer(db, created["id"], letter="a", note=HOSTILE, by="jeremy")

    rows = events.list_for_agent(db, "ops-seat")

    assert rows, "answering must produce an event"
    for row in rows:
        assert "\x1b" not in row.summary and "\n" not in row.summary
