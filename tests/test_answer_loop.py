"""The answer loop, fixed from the first outside agent's feedback
(homelab-ij8.106, 23 Sep 2026): its answers went unseen, its listener died at
Cloudflare, and `list` hid the answers it most needed."""

import threading
import time

import pytest
from fastapi.testclient import TestClient
from test_client import key_file, live  # noqa: F401

from artifacts import api, decide, decisions, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture()
def page(db, key_file):
    return store.publish(db, "ops-seat", "Board", PAGE)


def _ask(db, page, question="ship?", session=None):
    return decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=page.id,
        question=question,
        options=OPTIONS,
        session=session,
    )


def _run(args, key_path) -> int:
    return decide.main(["--key-file", str(key_path), *args])


# --- the service side --------------------------------------------------------


@pytest.fixture()
def client(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def auth(db):
    token, _ = keys.mint(db, agent="seat")
    return {"Authorization": f"Bearer {token}"}


def test_through_cloudflare_a_hold_is_capped(client, auth, monkeypatch):
    """Measured 23 Sep: wait=60 through Cloudflare died as a 524 after 125 s."""
    monkeypatch.setattr(api, "PUBLIC_MAX_WAIT", 0.3)
    started = time.monotonic()
    response = client.get(
        "/api/v1/events?since=999999&wait=5", headers={**auth, "cf-ray": "abc-AMS"}
    )
    assert response.status_code == 200 and response.json() == []
    assert time.monotonic() - started < 3


def test_inside_the_house_the_hold_is_what_was_asked(client, auth, monkeypatch):
    monkeypatch.setattr(api, "PUBLIC_MAX_WAIT", 0.1)
    started = time.monotonic()
    client.get("/api/v1/events?since=999999&wait=1.2", headers=auth)
    assert time.monotonic() - started >= 1.0


def test_every_answer_carries_the_cursor_for_now(db, client, auth):
    first = client.get("/api/v1/events?since=999999", headers=auth)
    assert first.headers["x-artifacts-cursor"] == "0"
    page = store.publish(db, "seat", "Board", PAGE)
    decisions.ask(db, agent="seat", artifact_id=page.id, question="q?", options=OPTIONS)
    asked = decisions.list_for_artifact(
        db, page.id, include_answered=True, include_closed=False
    )[0]
    decisions.answer(db, asked.id, letter="a", by="jeremy")
    now = int(
        client.get("/api/v1/events?since=999999", headers=auth).headers[
            "x-artifacts-cursor"
        ]
    )
    assert now > 0
    assert client.get(f"/api/v1/events?since={now}", headers=auth).json() == []


def test_an_agent_can_leave_out_its_own_actions(db, client, auth):
    # Binning its own page shelves its open question: an event whose actor
    # is the seat itself, and one it did not need telling about.
    page = store.publish(db, "seat", "Board", PAGE)
    decisions.ask(db, agent="seat", artifact_id=page.id, question="q?", options=OPTIONS)
    store.set_deleted(db, page.id, True, agent="seat")
    everything = client.get("/api/v1/events?since=0", headers=auth).json()
    assert any(r["actor"] == "seat" for r in everything)
    others = client.get("/api/v1/events?since=0&exclude_self=true", headers=auth).json()
    assert all(r["actor"] != "seat" for r in others)


def test_a_relayed_answer_is_never_left_out_as_the_agents_own(db, client, auth):
    """The answer is his; the relay only carried it. A key is shared by every
    session on a machine, so a waiter in one session must hear a verdict that
    another session relayed with the same key. Found by the live proof of
    page review step 2, 29 Sep 2026: the waiter slept through a relayed
    verdict and exited 3."""
    page = store.publish(db, "seat", "Board", PAGE)
    asked = decisions.ask(
        db, agent="seat", artifact_id=page.id, question="q?", options=OPTIONS
    )
    decisions.record_elsewhere(
        db,
        asked.id,
        letter="a",
        note="said in the terminal",
        source="terminal",
        recorded_by="seat",
    )
    others = client.get("/api/v1/events?since=0&exclude_self=true", headers=auth).json()
    assert [r["kind"] for r in others if r["ref_id"] == asked.id] == [
        "decision_answered"
    ]
    held = client.get(
        "/api/v1/events?since=0&exclude_self=true&wait=1", headers=auth
    ).json()
    assert any(r["kind"] == "decision_answered" for r in held)


def test_a_relayed_answer_carries_the_session_that_asked(db):
    keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Board", PAGE)
    asked = decisions.ask(
        db,
        agent="seat",
        artifact_id=page.id,
        question="q?",
        options=OPTIONS,
        session="claude/abc",
    )
    decisions.record_elsewhere(
        db, asked.id, letter="b", note="n", source="terminal", recorded_by="seat"
    )
    with db.connect() as conn:
        sessions = [
            r[0]
            for r in conn.execute(
                "select session from event where kind = 'decision_answered'"
            ).fetchall()
        ]
    assert sessions == ["claude/abc"]


# --- the commands ------------------------------------------------------------


def test_list_puts_new_answers_first_and_hides_acknowledged_ones(
    db,
    live,
    key_file,
    page,
    capsys,
):
    path, _ = key_file
    open_one = _ask(db, page, "still open?")
    answered = _ask(db, page, "answered?")
    seen = _ask(db, page, "already seen?")
    decisions.answer(db, answered.id, letter="a", note="go on", by="jeremy")
    decisions.answer(db, seen.id, letter="b", by="jeremy")
    decisions.acknowledge(db, "ops-seat", seen.id)
    assert _run(["list"], path) == 0
    out = capsys.readouterr().out
    assert out.index("answered?") < out.index("still open?")
    assert "go on" in out
    assert "already seen?" not in out
    assert _run(["list", "--answered"], path) == 0
    assert "already seen?" in capsys.readouterr().out
    assert _run(["list", "--open-only"], path) == 0
    only = capsys.readouterr().out
    assert "still open?" in only and "answered?" not in only
    assert open_one.id


def test_wait_prints_an_answer_that_arrived_before_it_started(
    db,
    live,
    key_file,
    page,
    capsys,
):
    path, _ = key_file
    asked = _ask(db, page)
    decisions.answer(db, asked.id, letter="a", note="early", by="jeremy")
    started = time.monotonic()
    assert _run(["wait", "--page", page.id, "--timeout", "30s"], path) == 0
    assert time.monotonic() - started < 5
    assert "early" in capsys.readouterr().out


def test_wait_wakes_when_he_answers(db, live, key_file, page, capsys):
    path, _ = key_file
    asked = _ask(db, page)

    def answer_soon():
        time.sleep(1.5)
        decisions.answer(db, asked.id, letter="b", note="later", by="jeremy")

    threading.Thread(target=answer_soon, daemon=True).start()
    started = time.monotonic()
    assert _run(["wait", "--page", page.id, "--timeout", "40s"], path) == 0
    took = time.monotonic() - started
    assert 1.0 < took < 15, took
    assert "later" in capsys.readouterr().out


def test_wait_says_nothing_yet_at_the_timeout(db, live, key_file, page, capsys):
    path, _ = key_file
    _ask(db, page)
    assert _run(["wait", "--page", page.id, "--timeout", "2s"], path) == 3
    assert "nothing yet" in capsys.readouterr().err


def test_wait_refuses_loudly_on_a_bad_key(live, tmp_path, capsys):
    bad = tmp_path / "key"
    bad.write_text("art_" + "x" * 43)
    bad.chmod(0o600)
    assert _run(["wait", "--timeout", "5s"], bad) == 1
    assert "REFUSED" in capsys.readouterr().err
