"""Where Jeremy's answer goes, and whether a session is waiting for it
(docs/agent/specs/2026-10-29-where-an-answer-goes.md).

The honesty rules of the spec, section 2, are the tests that matter here: the
line says "sent", never "received"; a plain poll is never a wait; another
agent's sessions and decisions are never visible, and a decision that is not
yours answers exactly as one that does not exist.
"""

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from test_client import key_file, live  # noqa: F401

from ream import decide, decisions, events, keys, listening, nudge, partner, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(nudge, "emit", lambda **kw: None)


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def bridge(monkeypatch, tmp_path):
    pem = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "signing.pem"
    path.write_bytes(pem)
    monkeypatch.setenv(partner.ENV_NAME, "partner-x")
    monkeypatch.setenv(partner.ENV_URL, "http://partner.invalid/events")
    monkeypatch.setenv(partner.ENV_ALLOW_HTTP, "1")
    monkeypatch.setenv(partner.ENV_KEY_FILE, str(path))
    monkeypatch.setenv(partner.ENV_KEY_ID, "test-key-1")
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://gallery.invalid")


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _ask(db, agent="cove", session="cove/1", question="ship?"):
    page = store.publish(db, agent, "Board", PAGE)
    asked = decisions.ask(
        db,
        agent=agent,
        artifact_id=page.id,
        question=question,
        options=OPTIONS,
        session=session,
    )
    return page, asked


def _deliver(db, decision_id):
    with db.connect() as conn:
        conn.execute(
            "update partner_outbox set delivered_at = %s where decision_id = %s",
            (datetime.now(UTC), decision_id),
        )


def _hold(db, agent, session, seconds=50):
    return events.note_read(db, agent, session, hold_seconds=seconds)


def _state(db, agent, decision_id):
    route = listening.for_decision(db, agent, decision_id)
    return None if route is None else route.state


def _held_until(db, agent, session):
    with db.connect() as conn:
        row = conn.execute(
            "select held_until from seat_read where agent = %s and session = %s",
            (agent, session),
        ).fetchone()
    return None if row is None else listening._moment(row[0])


# --- the heartbeat: events.note_read / note_hold_end --------------------------


def test_a_hold_sets_held_until_to_its_end_plus_the_grace(db):
    before = datetime.now(UTC)
    until = _hold(db, "cove", "cove/1", 50)
    expected = before + timedelta(seconds=50 + events.HOLD_GRACE_SECONDS)
    assert abs((until - expected).total_seconds()) < 5
    assert abs((_held_until(db, "cove", "cove/1") - until).total_seconds()) < 1


def test_a_plain_poll_never_marks_a_session_as_holding(db):
    events.note_read(db, "cove", "cove/1")
    assert _held_until(db, "cove", "cove/1") is None
    assert events.last_read(db, "cove", "cove/1") is not None, "it is still a read"


def test_a_poll_leaves_an_earlier_hold_alone(db):
    until = _hold(db, "cove", "cove/1", 50)
    assert events.note_read(db, "cove", "cove/1") is None
    assert abs((_held_until(db, "cove", "cove/1") - until).total_seconds()) < 1


def test_a_shorter_hold_never_replaces_a_longer_one(db):
    long = _hold(db, "cove", "cove/1", 200)
    _hold(db, "cove", "cove/1", 5)
    assert abs((_held_until(db, "cove", "cove/1") - long).total_seconds()) < 1


def test_the_end_of_a_hold_stops_it_reading_as_waiting_at_once(db):
    until = _hold(db, "cove", "cove/1", 50)
    events.note_hold_end(db, "cove", "cove/1", until)
    assert _held_until(db, "cove", "cove/1") <= datetime.now(UTC)


def test_the_end_of_an_older_hold_leaves_a_newer_one_standing(db):
    old = _hold(db, "cove", "cove/1", 5)
    newer = _hold(db, "cove", "cove/1", 100)
    events.note_hold_end(db, "cove", "cove/1", old)
    assert abs((_held_until(db, "cove", "cove/1") - newer).total_seconds()) < 1


def test_hold_bookkeeping_that_fails_never_raises(db, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("seat_read is having a bad day")

    monkeypatch.setattr(db, "connect", boom)
    assert events.note_read(db, "cove", "cove/1", hold_seconds=50) is None
    events.note_hold_end(db, "cove", "cove/1", datetime.now(UTC))


def test_a_held_events_request_shows_as_waiting_and_stops_when_it_returns(db, client):
    token, _ = keys.mint(db, agent="cove")
    headers = {**auth(token), "x-artifacts-session": "cove/ab12"}
    holder = threading.Thread(
        target=lambda: client.get("/api/v1/events?since=999999&wait=2", headers=headers)
    )
    holder.start()
    try:
        seen = False
        for _ in range(40):
            body = client.get("/api/v1/listening", headers=headers).json()
            if [w["session"] for w in body["waiting"]] == ["cove/ab12"]:
                seen = True
                break
            time.sleep(0.1)
        assert seen, "a held request did not show as waiting"
    finally:
        holder.join(10)
    body = client.get("/api/v1/listening", headers=headers).json()
    assert body["waiting"] == []
    assert [r["session"] for r in body["recent"]] == ["cove/ab12"]


def test_a_wait_of_zero_over_http_is_not_a_wait(db, client):
    token, _ = keys.mint(db, agent="cove")
    headers = {**auth(token), "x-artifacts-session": "cove/ab12"}
    client.get("/api/v1/events?since=0", headers=headers)
    body = client.get("/api/v1/listening", headers=headers).json()
    assert body["waiting"] == []
    assert [r["session"] for r in body["recent"]] == ["cove/ab12"]


def test_the_cloudflare_cap_is_what_is_stamped(db, client, monkeypatch):
    stamped = []
    real = events.note_read

    def spy(db_, agent, session=None, hold_seconds=0):
        stamped.append(hold_seconds)
        return real(db_, agent, session, hold_seconds=0)

    monkeypatch.setattr(events, "note_read", spy)
    monkeypatch.setattr(events, "wait_for_agent", lambda *a, **k: [])
    token, _ = keys.mint(db, agent="cove")
    client.get(
        "/api/v1/events?since=999999&wait=300",
        headers={**auth(token), "cf-ray": "abc"},
    )
    client.get("/api/v1/events?since=999999&wait=300", headers=auth(token))
    assert stamped == [50, 300], "what is stamped is what is really held"


# --- the four states -----------------------------------------------------------


def test_waiting_now_when_a_session_of_the_asking_office_holds(db):
    _, asked = _ask(db, session="cove/1")
    _hold(db, "cove", "cove/2")  # another session of the same office hears it
    route = listening.for_decision(db, "cove", asked.id)
    assert route.state == listening.WAITING_NOW
    assert route.waiting_session == "cove/2"
    assert "A session is waiting for your answer now (cove/2)" in listening.line(route)


def test_a_session_of_another_office_on_the_same_key_does_not_count(db):
    _, asked = _ask(db, agent="shared", session="cove/1")
    _hold(db, "shared", "homelab/9")
    assert _state(db, "shared", asked.id) == listening.NOT_SENT


def test_an_unlabelled_waiter_hears_every_office_so_it_counts(db):
    _, asked = _ask(db, agent="shared", session="cove/1")
    _hold(db, "shared", None)
    assert _state(db, "shared", asked.id) == listening.WAITING_NOW


def test_an_unlabelled_question_goes_to_the_office_named_like_the_key(db):
    _, asked = _ask(db, agent="cove", session=None)
    _hold(db, "cove", "homelab/1")
    assert _state(db, "cove", asked.id) == listening.NOT_SENT
    _hold(db, "cove", "cove/7")
    assert _state(db, "cove", asked.id) == listening.WAITING_NOW


def test_a_lapsed_hold_is_not_waiting(db):
    _, asked = _ask(db)
    _hold(db, "cove", "cove/1")
    with db.connect() as conn:
        conn.execute(
            "update seat_read set held_until = %s",
            (datetime.now(UTC) - timedelta(seconds=1),),
        )
    assert _state(db, "cove", asked.id) == listening.NOT_SENT


def test_a_recent_poll_alone_is_never_waiting(db):
    _, asked = _ask(db)
    events.note_read(db, "cove", "cove/1")
    assert _state(db, "cove", asked.id) == listening.NOT_SENT


def test_sent_to_slack_when_the_bridge_delivered_the_question(db, bridge):
    _, asked = _ask(db)
    _deliver(db, asked.id)
    route = listening.for_decision(db, "cove", asked.id)
    assert route.state == listening.SENT_TO_PARTNER
    assert route.announced and route.delivered
    text = listening.line(route)
    assert "delivered to cove in Slack" in text
    assert "acted on at its next check" in text


def test_slack_delayed_while_the_copy_is_undelivered(db, bridge):
    _, asked = _ask(db)
    route = listening.for_decision(db, "cove", asked.id)
    assert route.state == listening.PARTNER_DELAYED
    assert route.announced and not route.delivered
    assert "has not been delivered yet" in listening.line(route)


def test_a_parked_copy_reads_as_delayed_not_delivered(db, bridge):
    _, asked = _ask(db)
    with db.connect() as conn:
        conn.execute(
            "update partner_outbox set parked_at = %s where decision_id = %s",
            (datetime.now(UTC), asked.id),
        )
    assert _state(db, "cove", asked.id) == listening.PARTNER_DELAYED


def test_not_sent_when_the_bridge_is_off(db):
    assert not partner.enabled()
    _, asked = _ask(db)
    route = listening.for_decision(db, "cove", asked.id)
    assert route.state == listening.NOT_SENT
    assert "not in Slack" in listening.line(route)


def test_not_sent_for_a_question_that_predates_the_bridge(db, bridge):
    _, asked = _ask(db)
    with db.connect() as conn:
        conn.execute("delete from partner_outbox")
        conn.execute("update decision set partner_version = 0")
    assert _state(db, "cove", asked.id) == listening.NOT_SENT


def test_a_live_wait_wins_over_the_slack_copy(db, bridge):
    _, asked = _ask(db)
    _deliver(db, asked.id)
    _hold(db, "cove", "cove/1")
    assert _state(db, "cove", asked.id) == listening.WAITING_NOW


# --- honesty of the words ------------------------------------------------------


def test_the_line_says_sent_and_never_claims_the_office_received_or_when():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    for state in listening.STATES:
        route = listening.Route(
            id="d", agent="cove", state=state, waiting_session=None,
            last_seen=None, announced=True, delivered=True,
        )  # fmt: skip
        said = listening.line(route, now).lower()
        assert "received" not in said and "will be acted" not in said
        assert " at 1" not in said and "within" not in said, "no promised time"


def test_last_seen_is_stated_as_an_age_or_as_never_seen():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

    def said(last):
        return listening.line(
            listening.Route(
                id="d", agent="cove", state=listening.NOT_SENT,
                waiting_session=None, last_seen=last,
                announced=False, delivered=False,
            ),
            now,
        )  # fmt: skip

    assert "Last seen 12 min ago." in said((now - timedelta(minutes=12)).isoformat())
    assert "Last seen 3 h ago." in said((now - timedelta(hours=3)).isoformat())
    assert "Last seen just now." in said((now - timedelta(seconds=5)).isoformat())
    assert "has not been seen asking for events" in said(None)


def test_every_state_has_wording_in_the_one_place():
    extra = {listening.SENT_TO_PARTNER_PICKUP, listening.NOT_SENT_PICKUP}
    assert set(listening.WORDING) == set(listening.STATES) | extra


# --- the API twin and its scoping ----------------------------------------------


def test_the_api_reports_the_decision_with_the_same_line_the_page_uses(
    db, client, bridge
):
    page, asked = _ask(db)
    _deliver(db, asked.id)
    token, _ = keys.mint(db, agent="cove")
    r = client.get(f"/api/v1/listening?decision={asked.id}", headers=auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["agent"] == "cove" and body["bridge"] == {"enabled": True}
    assert body["ttl_grace_seconds"] == events.HOLD_GRACE_SECONDS
    decision = body["decision"]
    assert decision["state"] == "sent_to_partner"
    assert decision["route"] == {
        "kind": "partner",
        "agent": "cove",
        "announced": True,
        "delivered": True,
    }
    assert decision["line"].startswith("No session is waiting. Your answer is deli")
    wall = TestClient(create_gallery_app(db))
    on_page = wall.get(f"/a/{page.id}").text
    assert decision["line"].split(" It has not been seen")[0] in on_page


def test_another_agents_decision_answers_exactly_like_a_missing_one(db, client):
    _, theirs = _ask(db, agent="other")
    token, _ = keys.mint(db, agent="cove")
    foreign = client.get(f"/api/v1/listening?decision={theirs.id}", headers=auth(token))
    missing = client.get("/api/v1/listening?decision=d-nope", headers=auth(token))
    assert foreign.status_code == missing.status_code == 404
    assert foreign.content == missing.content


def test_a_deleted_pages_decision_is_not_found(db, client):
    page, asked = _ask(db)
    token, _ = keys.mint(db, agent="cove")
    with db.connect() as conn:
        conn.execute(
            "update artifact set deleted_at = %s where id = %s",
            (datetime.now(UTC), page.id),
        )
    r = client.get(f"/api/v1/listening?decision={asked.id}", headers=auth(token))
    assert r.status_code == 404


def test_a_page_readable_by_all_agents_is_still_not_yours_to_ask_about(db, client):
    page, theirs = _ask(db, agent="other")
    with db.connect() as conn:
        conn.execute(
            "update artifact set agents_can_read = true where id = %s", (page.id,)
        )
    token, _ = keys.mint(db, agent="cove")
    r = client.get(f"/api/v1/listening?decision={theirs.id}", headers=auth(token))
    assert r.status_code == 404


def test_the_listing_never_shows_another_agents_sessions(db, client):
    _hold(db, "other", "other/secret")
    events.note_read(db, "other", "other/older")
    _hold(db, "cove", "cove/1")
    token, _ = keys.mint(db, agent="cove")
    body = client.get("/api/v1/listening", headers=auth(token)).json()
    assert [w["session"] for w in body["waiting"]] == ["cove/1"]
    assert body["recent"] == []
    assert "secret" not in json.dumps(body) and "older" not in json.dumps(body)


def test_recent_is_the_last_day_newest_first_capped_at_twenty(db, client):
    for n in range(25):
        events.note_read(db, "cove", f"cove/s{n:02d}")
    with db.connect() as conn:
        conn.execute(
            "update seat_read set last_read_at = %s where session = 'cove/s00'",
            (datetime.now(UTC) - timedelta(hours=25),),
        )
        conn.execute(
            "update seat_read set last_read_at = %s where session = 'cove/s01'",
            (datetime.now(UTC) + timedelta(seconds=30),),
        )
    token, _ = keys.mint(db, agent="cove")
    recent = client.get("/api/v1/listening", headers=auth(token)).json()["recent"]
    assert len(recent) == 20
    assert recent[0]["session"] == "cove/s01", "newest first"
    assert "cove/s00" not in [r["session"] for r in recent], "older than a day"


def test_listening_does_not_record_the_caller_as_a_listener(db, client):
    token, _ = keys.mint(db, agent="cove")
    client.get(
        "/api/v1/listening",
        headers={**auth(token), "x-artifacts-session": "cove/ab12"},
    )
    with db.connect() as conn:
        assert conn.execute("select count(*) from seat_read").fetchone()[0] == 0


def test_listening_needs_a_key(client):
    assert client.get("/api/v1/listening").status_code in (401, 403)


# --- the line on the page ------------------------------------------------------


def test_an_open_question_card_carries_the_line(db, client):
    page, _asked = _ask(db)
    _hold(db, "cove", "cove/1")
    html = client.get(f"/a/{page.id}").text
    assert 'class="qroute waiting_now"' in html
    assert "A session is waiting for your answer now (cove/1)" in html


def test_the_page_says_where_the_answer_goes_when_nobody_waits(db, client, bridge):
    page, asked = _ask(db)
    _deliver(db, asked.id)
    html = client.get(f"/a/{page.id}").text
    assert "No session is waiting. Your answer is delivered to cove in Slack" in html
    assert "acted on at its next check" in html


def test_an_answered_question_card_has_no_line(db, client):
    page, asked = _ask(db)
    decisions.answer(db, asked.id, letter="a", note="", by="jeremy")
    assert 'class="qroute' not in client.get(f"/a/{page.id}").text


def test_a_review_card_has_no_line(db, client):
    page = store.publish(db, "cove", "Plan", PAGE)
    decisions.ask_review(db, agent="cove", artifact_id=page.id)
    assert 'class="qroute' not in client.get(f"/a/{page.id}").text


def test_a_hostile_session_label_is_escaped_on_the_page(db, client):
    page, _asked = _ask(db, session="cove/1")
    _hold(db, "cove", "cove/<script>alert(1)</script>")
    html = client.get(f"/a/{page.id}").text
    assert "<script>alert(1)</script>" not in html
    assert "cove/&lt;script&gt;" in html


# --- the CLI -------------------------------------------------------------------


def _run(args, key_path, monkeypatch, session="cove/1") -> int:
    if session is None:
        monkeypatch.delenv("REAM_SESSION", raising=False)
    else:
        monkeypatch.setenv("REAM_SESSION", session)
    return decide.main(["--key-file", str(key_path), *args])


def test_cli_exit_3_when_no_session_of_my_office_waits(
    db, live, key_file, capsys, monkeypatch
):
    path, _ = key_file
    _hold(db, "ops-seat", "homelab/2")
    assert _run(["listening"], path, monkeypatch) == 3
    assert "no session of cove is waiting" in capsys.readouterr().out


def test_cli_exit_0_when_a_session_of_my_office_waits(
    db, live, key_file, capsys, monkeypatch
):
    path, _ = key_file
    _hold(db, "ops-seat", "cove/9")
    assert _run(["listening"], path, monkeypatch) == 0
    assert "waiting now: cove/9" in capsys.readouterr().out


def test_cli_office_flag_overrides_the_session_label(
    db, live, key_file, capsys, monkeypatch
):
    path, _ = key_file
    _hold(db, "ops-seat", "homelab/2")
    assert _run(["listening", "--office", "homelab"], path, monkeypatch) == 0


def test_cli_json_carries_the_service_answer(db, live, key_file, capsys, monkeypatch):
    path, _ = key_file
    _hold(db, "ops-seat", "cove/9")
    assert _run(["listening", "--json"], path, monkeypatch) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["office"] == "cove" and body["office_waiting"] is True
    assert body["waiting"][0]["session"] == "cove/9"


def test_cli_decision_prints_the_page_line_and_follows_its_state(
    db, live, key_file, capsys, monkeypatch
):
    path, _ = key_file
    page = store.publish(db, "ops-seat", "Board", PAGE)
    asked = decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=page.id,
        question="ship?",
        options=OPTIONS,
        session="cove/1",
    )
    assert _run(["listening", "--decision", asked.id], path, monkeypatch) == 3
    assert "this question is not in Slack" in capsys.readouterr().out
    _hold(db, "ops-seat", "cove/9")
    assert _run(["listening", "--decision", asked.id], path, monkeypatch) == 0
    assert "A session is waiting for your answer now" in capsys.readouterr().out


def test_cli_refuses_a_decision_that_is_not_yours_like_a_missing_one(
    db, live, key_file, capsys, monkeypatch
):
    path, _ = key_file
    _, theirs = _ask(db, agent="other")
    assert _run(["listening", "--decision", theirs.id], path, monkeypatch) == 1
    first = capsys.readouterr().err
    assert _run(["listening", "--decision", "d-nope"], path, monkeypatch) == 1
    assert capsys.readouterr().err.replace("d-nope", "X") == first.replace(
        theirs.id, "X"
    )


def test_wait_notes_a_session_of_its_office_already_waiting_and_still_waits(
    db, live, key_file, capsys, monkeypatch, brief_holds
):
    path, _ = key_file
    _hold(db, "ops-seat", "cove/2", 600)
    rc = _run(["wait", "--timeout", "0.1s"], path, monkeypatch)
    assert rc == 3, "it waited as well, and ended on its own timeout"
    err = capsys.readouterr().err
    assert "cove/2 already waiting for cove's answers" in err


def test_wait_says_nothing_when_nobody_of_its_office_waits(
    db, live, key_file, capsys, monkeypatch, brief_holds
):
    path, _ = key_file
    _hold(db, "ops-seat", "homelab/2", 600)
    _run(["wait", "--timeout", "0.1s"], path, monkeypatch)
    assert "already waiting" not in capsys.readouterr().err


def test_help_text_states_exit_codes_and_what_it_cannot_see(capsys):
    with pytest.raises(SystemExit):
        decide.main(["listening", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "Exit 0" in out and "Exit 3" in out
    assert "polls `list` leaves no trace" in out


# --- onboarding ------------------------------------------------------------------


def test_onboarding_names_the_check_before_arming_a_wait():
    from ream import onboarding

    text = onboarding.agents_txt("https://artifacts.invalid")
    assert "artifacts-decide listening" in text
    assert "/api/v1/listening" in text
    assert "leaves no trace" in text


# --- rename keeps the later hold ---------------------------------------------------


def test_a_rename_merge_keeps_the_later_hold_and_never_shortens_one(db):
    from ream import rename

    now = datetime.now(UTC)
    with db.connect() as conn:
        for agent, session, until in (
            ("old-name", "cove/1", now + timedelta(seconds=90)),
            ("new-name", "cove/1", now + timedelta(seconds=10)),
            ("old-name", "cove/2", None),
            ("new-name", "cove/2", now + timedelta(seconds=40)),
            ("old-name", "cove/3", now + timedelta(seconds=60)),
        ):
            conn.execute(
                "insert into seat_read (agent, session, last_read_at, held_until)"
                " values (%s, %s, %s, %s)",
                (agent, session, now, until),
            )
    rename.rename_agent(db, "old-name", "new-name", dry_run=False)
    held = {s: _held_until(db, "new-name", s) for s in ("cove/1", "cove/2", "cove/3")}
    assert held["cove/1"] > now + timedelta(seconds=80), "the later hold won"
    assert now + timedelta(seconds=30) < held["cove/2"] < now + timedelta(seconds=50)
    assert held["cove/3"] is not None, "a row that only the old name had moved over"
