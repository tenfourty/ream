"""The outbound partner bridge (runcove-5t3oj.39.4): events written with the
change that causes them, signed per attempt, delivered in order per question.

The key pair is generated in memory for each test; the private half is written
to a pytest temp file only so the service can read it the way it reads it in
production (a path in an environment variable). No key is kept anywhere else.
The partner is a fake `post` function: no network.
"""

import base64
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from artifacts import admin, decisions, partner, serve, store
from artifacts.db import Database

PAGE = (
    "<!doctype html><html><head><title>t</title></head><body><p>plan</p></body></html>"
)
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]

# Hardcoded, not read from the sample files: the contract's field names.
ENVELOPE_KEYS = {
    "event_id",
    "kind",
    "source",
    "question",
    "version",
    "key_id",
    "at",
    "sent_at",
    "body",
}
ASKED_KEYS = {
    "asking_office",
    "title",
    "options",
    "recommended",
    "context",
    "link",
    "topic",
    "depends_on",
    "replaces",
    "urgency",
    "blocked_on",
    "needed_by",
    "for",
}
ANSWERED_KEYS = {"answer", "note", "answered_by", "answered_at", "answer_source"}


class FakePartner:
    """Records every attempt; answers from a script (a status, or an exception)."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[tuple[str, bytes, dict]] = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, dict(headers)))
        outcome = self.script.pop(0) if self.script else 200
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def envelopes(self):
        return [json.loads(body) for _, body, _ in self.calls]


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture()
def keypair():
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def bridge(monkeypatch, tmp_path, keypair):
    pem = keypair.private_bytes(
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
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://gallery.invalid")
    return partner.config_from_env()


@pytest.fixture()
def page(db):
    return store.publish(db, "owner", "Plan", PAGE)


def _ask(db, page, **kwargs):
    return decisions.ask(
        db,
        agent="owner",
        artifact_id=page.id,
        question="Which?",
        options=OPTIONS,
        recommended="a",
        **kwargs,
    )


def _rows(db, decision_id=None):
    with db.connect() as conn:
        found = conn.execute(
            "select decision_id, version, kind, body, delivered_at, attempts,"  # noqa: S608 -- fixed text
            " next_attempt_at, last_error from partner_outbox"
            + (" where decision_id = %s" if decision_id else "")
            + " order by decision_id, version",
            (decision_id,) if decision_id else None,
        ).fetchall()
    return found


def _kinds(db, decision_id):
    return [(r[1], r[2]) for r in _rows(db, decision_id)]


# --- off, and half on ----------------------------------------------------------


def test_off_writes_nothing_and_starts_no_sender(db, page, monkeypatch):
    for name in partner._ENV_ALL:
        monkeypatch.delenv(name, raising=False)
    assert partner.config_from_env() is None
    assert partner.enabled() is False
    row = _ask(db, page)
    decisions.answer(db, row.id, letter="a", by="jeremy")
    decisions.close(db, "owner", row.id)
    assert _rows(db) == []
    assert partner.start(db, None) is None
    assert partner.status(db)["depth"] == 0


@pytest.mark.parametrize("drop", partner._ENV_ALL)
def test_half_configured_is_refused_naming_variables_only(bridge, monkeypatch, drop):
    monkeypatch.delenv(drop)
    with pytest.raises(partner.PartnerConfigError) as caught:
        partner.config_from_env()
    assert drop in str(caught.value)


def test_half_configured_service_refuses_to_start(bridge, monkeypatch, tmp_path):
    monkeypatch.delenv(partner.ENV_URL)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'never.db'}")
    with pytest.raises(SystemExit) as caught:
        serve.app_from_env()
    assert partner.ENV_URL in str(caught.value)
    # It refused before opening the database at all.
    assert not (tmp_path / "never.db").exists()


def test_a_bad_key_file_is_refused_without_quoting_it(bridge, monkeypatch, tmp_path):
    bad = tmp_path / "bad.pem"
    bad.write_text("-----BEGIN PRIVATE KEY-----\nTOP-SECRET-LOOKING-TEXT\n")
    monkeypatch.setenv(partner.ENV_KEY_FILE, str(bad))
    with pytest.raises(partner.PartnerConfigError) as caught:
        partner.config_from_env()
    assert "TOP-SECRET" not in str(caught.value)
    monkeypatch.setenv(partner.ENV_KEY_FILE, str(tmp_path / "missing.pem"))
    with pytest.raises(partner.PartnerConfigError):
        partner.config_from_env()


def test_a_key_that_is_not_ed25519_is_refused(bridge, monkeypatch, tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ec

    other = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "ec.pem"
    path.write_bytes(other)
    monkeypatch.setenv(partner.ENV_KEY_FILE, str(path))
    with pytest.raises(partner.PartnerConfigError, match="Ed25519"):
        partner.config_from_env()


def test_the_config_never_shows_its_key(bridge):
    assert "Ed25519" not in repr(bridge)
    assert "test-key-1" in repr(bridge)


# --- events written with their cause --------------------------------------------


def test_asked_is_enqueued_with_the_contract_fields(db, page, bridge):
    row = _ask(
        db,
        page,
        context="why",
        topic={"id": "t1", "title": "Topic one"},
        urgency="by",
        needed_by="2026-10-23T10:00:00+00:00",
        for_offices=["quarterdeck"],
    )
    (found,) = _rows(db, row.id)
    assert found[1:3] == (1, "asked")
    assert set(found[3]) == ASKED_KEYS
    assert found[3]["asking_office"] == "owner"
    assert found[3]["options"] == [
        {"letter": "a", "text": "yes"},
        {"letter": "b", "text": "no"},
    ]
    assert found[3]["link"] == f"https://gallery.invalid/a/{page.id}#q-{row.id}"
    assert found[3]["topic"] == {"id": "t1", "title": "Topic one"}
    assert found[3]["for"] == ["quarterdeck"]
    assert found[3]["needed_by"] == "2026-10-23T10:00:00+00:00"
    assert found[3]["replaces"] is None


def test_the_envelope_has_exactly_the_contract_fields(db, page, bridge):
    row = _ask(db, page)
    fake = FakePartner()
    partner.deliver_due(db, bridge, fake)
    (envelope,) = fake.envelopes()
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["kind"] == "asked"
    assert envelope["source"] == f"artifacts:{row.id}"
    assert envelope["question"] == row.id
    assert envelope["version"] == 1
    assert envelope["key_id"] == "test-key-1"
    assert set(envelope["body"]) == ASKED_KEYS
    assert fake.calls[0][0] == "http://partner.invalid/events"


def test_a_rolled_back_ask_enqueues_nothing(db, page, bridge, monkeypatch):
    first = _ask(db, page)
    before = _rows(db)

    def boom(*args, **kwargs):
        raise RuntimeError("the replace failed after the insert")

    monkeypatch.setattr(decisions, "_close_row", boom)
    with pytest.raises(RuntimeError):
        _ask(db, page, replaces=first.id)
    assert _rows(db) == before
    with db.connect() as conn:
        assert conn.execute("select count(*) from decision").fetchone()[0] == 1
        assert conn.execute(
            "select partner_version from decision where id = %s", (first.id,)
        ).fetchone() == (1,)


def test_a_rolled_back_transaction_takes_its_version_back(db, page, bridge):
    from artifacts.db import Rollback

    row = _ask(db, page)
    with db.connect() as conn, conn.transaction():
        partner.enqueue(conn, row.id, "withdrawn", {"reason": "x"})
        raise Rollback
    assert _kinds(db, row.id) == [(1, "asked")]
    with db.connect() as conn, conn.transaction():
        assert partner.enqueue(conn, row.id, "withdrawn", {"reason": "x"}) == 2


def test_answered_on_the_page_then_changed_is_a_new_version(db, page, bridge):
    row = _ask(db, page)
    decisions.answer(db, row.id, letter="a", note="go", by="jeremy")
    decisions.answer(db, row.id, letter="b", note="no, b", by="jeremy")
    assert _kinds(db, row.id) == [(1, "asked"), (2, "answered"), (3, "answered")]
    first, second = _rows(db, row.id)[1:]
    assert set(first[3]) == ANSWERED_KEYS
    assert first[3]["answer"] == "a"
    assert first[3]["note"] == "go"
    assert first[3]["answer_source"] == "page"
    assert first[3]["answered_by"] == {"name": "Jeremy", "verified_by": "artifacts"}
    assert second[3]["answer"] == "b"


def test_an_answer_relayed_from_elsewhere_names_its_source_and_seat(db, page, bridge):
    row = _ask(db, page)
    decisions.record_elsewhere(
        db,
        row.id,
        letter="b",
        note="his words",
        source="terminal",
        recorded_by="owner",
        said_at=datetime(2026, 10, 21, 9, 0, tzinfo=UTC),
    )
    body = _rows(db, row.id)[1][3]
    assert body["answer"] == "b"
    assert body["answer_source"] == "terminal"
    assert body["recorded_by"] == "owner"
    assert body["said_at"] == "2026-10-21T09:00:00+00:00"
    assert body["answered_by"] == {"name": "Jeremy", "verified_by": "agent"}
    assert ANSWERED_KEYS <= set(body)


def test_closing_without_an_answer_is_withdrawn(db, page, bridge):
    row = _ask(db, page)
    decisions.close(db, "owner", row.id)
    decisions.close(db, "owner", row.id)  # idempotent: no second event
    assert _kinds(db, row.id) == [(1, "asked"), (2, "withdrawn")]
    assert set(_rows(db, row.id)[1][3]) == {"reason"}


def test_closing_after_an_answer_tells_the_partner_nothing_more(db, page, bridge):
    row = _ask(db, page)
    decisions.answer(db, row.id, letter="a", by="jeremy")
    decisions.close(db, "owner", row.id)
    assert _kinds(db, row.id) == [(1, "asked"), (2, "answered")]


def test_replacing_a_question_says_replaced_on_the_old_one(db, page, bridge):
    old = _ask(db, page)
    new = _ask(db, page, replaces=old.id)
    assert _kinds(db, old.id) == [(1, "asked"), (2, "replaced")]
    assert _rows(db, old.id)[1][3] == {
        "replaced_by": new.id,
        "replaced_by_source": f"artifacts:{new.id}",
    }
    assert _rows(db, new.id)[0][3]["replaces"] == old.id


@pytest.mark.parametrize("how", ["archived", "deleted"])
def test_shelving_the_page_withdraws_its_open_questions(db, page, bridge, how):
    open_one = _ask(db, page)
    answered = _ask(db, page)
    decisions.answer(db, answered.id, letter="a", by="jeremy")
    review = decisions.ask_review(db, agent="owner", artifact_id=page.id)
    if how == "archived":
        assert store.set_archived(db, page.id, True, agent="owner")
    else:
        assert store.set_deleted(db, page.id, True, agent="owner")
    assert _kinds(db, open_one.id) == [(1, "asked"), (2, "withdrawn")]
    assert f"was {how}" in _rows(db, open_one.id)[1][3]["reason"]
    assert _kinds(db, answered.id) == [(1, "asked"), (2, "answered")]
    assert _rows(db, review.id) == []


def test_reviews_are_never_shared(db, page, bridge):
    review = decisions.ask_review(db, agent="owner", artifact_id=page.id)
    decisions.answer(db, review.id, letter="a", by="jeremy")
    assert _rows(db) == []


def test_a_question_asked_before_the_bridge_is_never_announced_late(
    db, page, monkeypatch, bridge
):
    for name in partner._ENV_ALL:
        monkeypatch.delenv(name)
    row = _ask(db, page)
    monkeypatch.setenv(partner.ENV_NAME, "partner-x")
    monkeypatch.setenv(partner.ENV_URL, "http://partner.invalid/events")
    monkeypatch.setenv(partner.ENV_KEY_FILE, "unused")
    monkeypatch.setenv(partner.ENV_KEY_ID, "test-key-1")
    decisions.close(db, "owner", row.id)
    assert _rows(db) == []


def test_versions_are_strictly_increasing_under_concurrency(db, page, bridge):
    row = _ask(db, page)
    errors = []

    def answer(i):
        try:
            decisions.answer(db, row.id, letter="ab"[i % 2], note=str(i), by="jeremy")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=answer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert [r[1] for r in _rows(db, row.id)] == list(range(1, 8))


# --- signing ----------------------------------------------------------------------


def test_the_signature_verifies_over_the_exact_bytes_and_not_a_changed_one(
    db, page, bridge, keypair
):
    _ask(db, page)
    fake = FakePartner()
    partner.deliver_due(db, bridge, fake)
    _, body, headers = fake.calls[0]
    signature = base64.b64decode(headers[partner.SIGNATURE_HEADER])
    public = keypair.public_key()
    public.verify(signature, body)
    changed = bytearray(body)
    changed[10] ^= 1
    with pytest.raises(InvalidSignature):
        public.verify(signature, bytes(changed))
    assert partner.SIGNATURE_HEADER == "X-Partner-Signature"


def test_the_key_id_header_names_the_same_key_as_the_signed_body(db, page, bridge):
    _ask(db, page)
    fake = FakePartner()
    partner.deliver_due(db, bridge, fake)
    _, body, headers = fake.calls[0]
    assert headers[partner.KEY_ID_HEADER] == bridge.key_id
    assert headers[partner.KEY_ID_HEADER] == json.loads(body)["key_id"]
    assert partner.KEY_ID_HEADER == "X-Partner-Key-Id"


# --- delivery ---------------------------------------------------------------------


def test_a_retry_is_resigned_with_a_fresh_sent_at_and_the_same_identity(
    db, page, bridge, keypair
):
    _ask(db, page)
    clock = Clock()
    fake = FakePartner(500, 200)
    assert partner.deliver_due(db, bridge, fake, clock) == (0, 1)
    # Not due again until its backoff has passed.
    assert partner.deliver_due(db, bridge, fake, clock) == (0, 0)
    clock.advance(seconds=partner.BACKOFF_BASE_SECONDS + 1)
    assert partner.deliver_due(db, bridge, fake, clock) == (1, 0)
    first, second = fake.envelopes()
    assert first["event_id"] == second["event_id"]
    assert first["version"] == second["version"] == 1
    assert first["sent_at"] != second["sent_at"]
    assert datetime.fromisoformat(second["sent_at"]) > datetime.fromisoformat(
        first["sent_at"]
    )
    for _, body, headers in fake.calls:
        keypair.public_key().verify(
            base64.b64decode(headers[partner.SIGNATURE_HEADER]), body
        )
    assert fake.calls[0][1] != fake.calls[1][1]
    (row,) = _rows(db)
    assert row[4] is not None and row[5] == 2


def test_backoff_grows_and_is_capped(db, page, bridge):
    assert partner._backoff(1) == timedelta(seconds=5)
    assert partner._backoff(2) == timedelta(seconds=10)
    assert partner._backoff(40) == timedelta(seconds=partner.BACKOFF_CAP_SECONDS)


def test_a_later_version_waits_for_an_earlier_one(db, page, bridge):
    row = _ask(db, page)
    decisions.answer(db, row.id, letter="a", by="jeremy")
    decisions.answer(db, row.id, letter="b", by="jeremy")  # a changed answer
    clock = Clock()
    fake = FakePartner(503)  # version 1 fails, then everything succeeds
    partner.deliver_due(db, bridge, fake, clock)
    assert [e["version"] for e in fake.envelopes()] == [1]
    # Version 2 is due, but 1 is not delivered: it must not go.
    clock.advance(seconds=1)
    partner.deliver_due(db, bridge, fake, clock)
    assert [e["version"] for e in fake.envelopes()] == [1]
    clock.advance(seconds=partner.BACKOFF_BASE_SECONDS)
    for _ in range(3):  # 1 goes, then 2, then 3: one per pass per question
        partner.deliver_due(db, bridge, fake, clock)
    assert [e["version"] for e in fake.envelopes()] == [1, 1, 2, 3]
    assert all(r[4] is not None for r in _rows(db))


def test_one_question_failing_does_not_hold_up_another(db, page, bridge):
    a = _ask(db, page)
    b = _ask(db, page)
    fake = FakePartner(500)  # the first row claimed fails
    partner.deliver_due(db, bridge, fake, Clock())
    delivered = {r[0] for r in _rows(db) if r[4] is not None}
    assert len(delivered) == 1 and delivered < {a.id, b.id}


def test_a_claimed_row_is_not_claimed_twice(db, page, bridge):
    _ask(db, page)
    now = datetime.now(UTC)
    assert len(partner._claim(db, now)) == 1
    assert partner._claim(db, now) == []
    # A crashed claimer's lease runs out, and the row is theirs to take again.
    assert (
        len(partner._claim(db, now + timedelta(seconds=partner.LEASE_SECONDS + 1))) == 1
    )


def test_the_sender_thread_delivers_and_stops(db, page, bridge, monkeypatch):
    _ask(db, page)
    fake = FakePartner()
    monkeypatch.setattr(partner, "http_post", fake)
    monkeypatch.setattr(partner, "POLL_SECONDS", 0.05)
    # deliver_due's default `post` is bound at definition; pass it through.
    original = partner.deliver_due
    monkeypatch.setattr(
        partner,
        "deliver_due",
        lambda database, config: original(database, config, fake),
    )
    thread = partner.start(db, bridge)
    try:
        for _ in range(100):
            if fake.calls:
                break
            threading.Event().wait(0.05)
    finally:
        partner.stop()
        thread.join(5)
    assert len(fake.calls) == 1
    assert not thread.is_alive()


# --- nothing secret in what is stored, logged or shown -------------------------------


def test_no_key_material_in_last_error_logs_or_status(
    db, page, bridge, keypair, caplog, capsys, monkeypatch
):
    _ask(db, page)
    raw = keypair.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    secrets = [base64.b64encode(raw).decode(), raw.hex(), "BEGIN"]
    clock = Clock()
    fake = FakePartner(
        500, partner.DeliveryFailed("could not connect"), RuntimeError("boom")
    )
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            partner.deliver_due(db, bridge, fake, clock)
            clock.advance(hours=1)
    report = partner.status(db, clock())
    monkeypatch.setattr(admin, "_open_db", lambda: db)
    admin.main(["partner-outbox"])
    shown = capsys.readouterr().out
    stored = " ".join(str(r[7]) for r in _rows(db))
    everything = caplog.text + json.dumps(report) + shown + stored
    for secret in secrets:
        assert secret not in everything
    assert report["last_error"] in {
        "HTTP 500",
        "could not connect",
        "unexpected RuntimeError",
    }
    # A response body is never echoed: only a status code or a class of failure.
    assert "HTTP 500" in caplog.text


def test_the_transport_reports_a_status_and_never_a_body(bridge):
    class Handler(BaseHTTPRequestHandler):
        seen: ClassVar[list] = []

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            Handler.seen.append((self.rfile.read(length), self.headers))
            self.send_response(500 if len(Handler.seen) == 1 else 204)
            self.end_headers()
            self.wfile.write(b"SECRET-RESPONSE-BODY")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/events"
        headers = {partner.SIGNATURE_HEADER: "abc", "Content-Type": "application/json"}
        assert partner.http_post(url, b'{"a":1}', headers, 5) == 500
        assert partner.http_post(url, b'{"a":2}', headers, 5) == 204
    finally:
        server.shutdown()
        server.server_close()
    assert Handler.seen[0][0] == b'{"a":1}'
    assert Handler.seen[1][1][partner.SIGNATURE_HEADER] == "abc"
    closed = HTTPServer(("127.0.0.1", 0), Handler)
    dead = f"http://127.0.0.1:{closed.server_port}/events"
    closed.server_close()
    with pytest.raises(partner.DeliveryFailed, match="connect"):
        partner.http_post(dead, b"{}", {}, 2)


# --- what an operator sees ------------------------------------------------------------


def test_status_reports_depth_age_and_the_last_error(db, page, bridge):
    assert partner.status(db)["depth"] == 0
    assert partner.status(db)["oldest_undelivered_age_seconds"] is None
    _ask(db, page)
    clock = Clock()
    partner.deliver_due(db, bridge, FakePartner(502), clock)
    report = partner.status(db, clock() + timedelta(seconds=120))
    assert report["depth"] == 1
    assert 110 <= report["oldest_undelivered_age_seconds"] <= 130
    assert report["last_error"] == "HTTP 502"
    assert partner.deliver_due(db, bridge, FakePartner(), clock) == (0, 0)  # not due
    clock.advance(minutes=5)
    partner.deliver_due(db, bridge, FakePartner(), clock)
    after = partner.status(db, clock())
    assert after["depth"] == 0
    # Only a row still waiting shows an error: a delivered one is history.
    assert after["last_error"] is None


def test_delivered_rows_are_pruned_after_thirty_days(db, page, bridge):
    _ask(db, page)
    clock = Clock()
    partner.deliver_due(db, bridge, FakePartner(), clock)
    assert partner.prune(db, clock()) == 0
    assert partner.prune(db, clock() + timedelta(days=31)) == 1


def test_a_database_file_from_before_the_outbox_is_upgraded(tmp_path, monkeypatch):
    import sqlite3

    from artifacts import db as dbmod

    path = tmp_path / "old.db"
    steps = dict(dbmod._SQLITE_STEPS)
    monkeypatch.setattr(
        dbmod, "_SQLITE_STEPS", {n: s for n, s in steps.items() if n < 4}
    )
    monkeypatch.setattr(dbmod, "SQLITE_SCHEMA_VERSION", 3)
    dbmod.Database(f"sqlite:///{path}").migrate()
    tables = lambda: {
        r[0]
        for r in sqlite3.connect(path).execute(
            "select name from sqlite_master where type = 'table'"
        )
    }
    assert "partner_outbox" not in tables()
    monkeypatch.setattr(dbmod, "_SQLITE_STEPS", steps)
    monkeypatch.setattr(dbmod, "SQLITE_SCHEMA_VERSION", max(steps))
    upgraded = Database(f"sqlite:///{path}")
    upgraded.migrate()
    upgraded.migrate()
    assert "partner_outbox" in tables()
    upgraded.close()


# --- close() against an answer, and against itself -----------------------------------


def _stale_read(db, decision_id):
    with db.connect() as conn:
        return conn.execute(
            f"select {decisions._COLUMNS} from decision where decision.id = %s",  # noqa: S608 -- fixed constant
            (decision_id,),
        ).fetchone()


def test_a_close_that_lost_to_an_answer_sends_no_withdrawn(db, page, bridge):
    row = _ask(db, page)
    stale = _stale_read(db, row.id)
    assert stale[6] is None  # read while still unanswered
    decisions.answer(db, row.id, letter="a", by="jeremy")
    with db.connect() as conn, conn.transaction():
        decisions._close_row(conn, "owner", stale)  # acts on the stale read
    assert _kinds(db, row.id) == [(1, "asked"), (2, "answered")]


def test_two_closes_send_one_withdrawn(db, page, bridge):
    row = _ask(db, page)
    stale = _stale_read(db, row.id)
    for _ in range(2):  # both hold the same unclosed read
        with db.connect() as conn, conn.transaction():
            decisions._close_row(conn, "owner", stale)
    assert _kinds(db, row.id) == [(1, "asked"), (2, "withdrawn")]


def test_close_still_returns_the_row_whether_open_answered_or_closed(db, page, bridge):
    open_q = _ask(db, page)
    closed = decisions.close(db, "owner", open_q.id)
    assert closed.closed_at is not None
    again = decisions.close(db, "owner", open_q.id)
    assert again.closed_at == closed.closed_at
    answered = _ask(db, page)
    decisions.answer(db, answered.id, letter="b", by="jeremy")
    done = decisions.close(db, "owner", answered.id)
    assert done.answer == "b" and done.closed_at is not None


# --- parking ----------------------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 404, 409, 410, 413, 415, 422])
def test_a_permanent_refusal_parks_the_row_and_holds_later_versions(
    db, page, bridge, code
):
    row = _ask(db, page)
    decisions.answer(db, row.id, letter="a", by="jeremy")  # version 2
    other = _ask(db, page)  # another question keeps flowing
    clock = Clock()
    fake = FakePartner(code)
    partner.deliver_due(db, bridge, fake, clock)
    sent = fake.envelopes()
    assert sent[0]["version"] == 1 and sent[0]["question"] == row.id
    # Version 2 of the same question never goes; the other question does.
    assert {e["question"] for e in sent[1:]} == {other.id}
    clock.advance(days=2)
    partner.deliver_due(db, bridge, fake, clock)
    assert [e["version"] for e in fake.envelopes() if e["question"] == row.id] == [1]
    parked = next(r for r in _rows(db, row.id) if r[1] == 1)
    assert parked[7] == f"HTTP {code}"
    report = partner.status(db, clock())
    assert report["parked"] == 1
    assert report["parked_codes"] == [f"HTTP {code}"]
    assert report["oldest_parked_age_seconds"] >= 2 * 86400 - 5


@pytest.mark.parametrize(
    "outcome", [401, 403, 405, 408, 418, 425, 429, 500, 502, 503, 302]
)
def test_other_failures_are_retried_with_backoff_not_parked(db, page, bridge, outcome):
    _ask(db, page)
    clock = Clock()
    fake = FakePartner(outcome, 200)
    partner.deliver_due(db, bridge, fake, clock)
    assert partner.status(db, clock())["parked"] == 0
    clock.advance(seconds=partner.BACKOFF_BASE_SECONDS + 1)
    assert partner.deliver_due(db, bridge, fake, clock) == (1, 0)


def test_a_network_failure_is_retried(db, page, bridge):
    _ask(db, page)
    clock = Clock()
    fake = FakePartner(partner.DeliveryFailed("timed out"), 200)
    partner.deliver_due(db, bridge, fake, clock)
    assert partner.status(db, clock())["parked"] == 0
    clock.advance(seconds=partner.BACKOFF_BASE_SECONDS + 1)
    assert partner.deliver_due(db, bridge, fake, clock) == (1, 0)


def test_retry_releases_one_or_all_parked_events(db, page, bridge, monkeypatch, capsys):
    first = _ask(db, page)
    second = _ask(db, page)
    clock = Clock()
    partner.deliver_due(db, bridge, FakePartner(404, 404), clock)
    assert partner.status(db, clock())["parked"] == 2
    monkeypatch.setattr(admin, "_open_db", lambda: db)
    with db.connect() as conn:
        (event_id,) = conn.execute(
            "select event_id from partner_outbox where decision_id = %s", (first.id,)
        ).fetchone()
    admin.main(["partner-outbox", "--retry", event_id])
    assert "released 1" in capsys.readouterr().out
    assert partner.status(db, clock())["parked"] == 1
    with pytest.raises(SystemExit):
        admin.main(["partner-outbox", "--retry", event_id])  # no longer parked
    capsys.readouterr()
    admin.main(["partner-outbox", "--retry-parked"])
    assert "released 1" in capsys.readouterr().out
    fake = FakePartner()
    clock.advance(minutes=1)  # released at the real "now"; the test clock is older
    partner.deliver_due(db, bridge, fake, clock)
    assert {e["question"] for e in fake.envelopes()} == {first.id, second.id}
    assert partner.status(db, clock())["parked"] == 0


def test_the_admin_report_shows_parked_rows(db, page, bridge, monkeypatch, capsys):
    _ask(db, page)
    partner.deliver_due(db, bridge, FakePartner(422), Clock())
    monkeypatch.setattr(admin, "_open_db", lambda: db)
    admin.main(["partner-outbox"])
    shown = capsys.readouterr().out
    assert "parked: 1" in shown and "HTTP 422" in shown


# --- the lease and idle polling ------------------------------------------------------------


def test_a_row_is_not_claimable_by_a_second_sender_during_its_attempt(db, page, bridge):
    _ask(db, page)
    seen = []

    def slow_partner(url, body, headers, timeout):
        # A second sender looks while the first is mid-request.
        seen.append(partner._claim(db, datetime.now(UTC) + timedelta(seconds=30)))
        return 200

    assert partner.deliver_due(db, bridge, slow_partner) == (1, 0)
    assert seen == [[]]


def test_an_empty_queue_takes_no_write_transaction(db, page, bridge, monkeypatch):
    from artifacts import db as dbmod

    begun = []
    for cls in (dbmod.Connection, dbmod.SqliteConnection):
        original = cls.transaction

        def counted(self, _original=original):
            begun.append(1)
            return _original(self)

        monkeypatch.setattr(cls, "transaction", counted)
    assert partner.deliver_due(db, bridge, FakePartner()) == (0, 0)
    assert begun == []
    _ask(db, page)
    begun.clear()
    partner.deliver_due(db, bridge, FakePartner())
    assert begun  # work does open one


# --- the transport -----------------------------------------------------------------------------


def test_http_needs_the_explicit_opt_in_and_https_does_not(bridge, monkeypatch):
    assert partner.config_from_env().url.startswith("http://")
    monkeypatch.delenv(partner.ENV_ALLOW_HTTP)
    with pytest.raises(partner.PartnerConfigError, match="https"):
        partner.config_from_env()
    monkeypatch.setenv(partner.ENV_ALLOW_HTTP, "yes")  # only "1" counts
    with pytest.raises(partner.PartnerConfigError):
        partner.config_from_env()
    monkeypatch.setenv(partner.ENV_URL, "https://partner.invalid/events")
    assert partner.config_from_env().url.startswith("https://")
    monkeypatch.setenv(partner.ENV_URL, "ftp://partner.invalid/events")
    with pytest.raises(partner.PartnerConfigError):
        partner.config_from_env()


def test_plain_http_is_logged_once_at_start(db, bridge, caplog, monkeypatch):
    monkeypatch.setattr(partner, "POLL_SECONDS", 0.05)
    with caplog.at_level(logging.WARNING, logger="artifacts.partner"):
        thread = partner.start(db, bridge)
        partner.stop()
        thread.join(5)
    assert caplog.text.count("plain HTTP is in use") == 1


def test_a_proxy_in_the_environment_is_ignored_and_a_redirect_is_not_followed(
    bridge, monkeypatch
):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            if self.path == "/events":
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # A proxy that does not exist: were it used, the post would fail to connect.
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    try:
        url = f"http://127.0.0.1:{server.server_port}/events"
        assert partner.http_post(url, b"{}", {}, 5) == 302
    finally:
        server.shutdown()
        server.server_close()
    assert hits == ["/events"]  # the redirect target was never requested
    assert partner._permanent("HTTP 302") is False


def test_a_run_of_401_and_403_is_counted_and_drains_once_fixed(
    db, page, bridge, monkeypatch, capsys
):
    first = _ask(db, page)
    _ask(db, page)
    clock = Clock()
    partner.deliver_due(db, bridge, FakePartner(401, 403), clock)
    report = partner.status(db, clock())
    assert report["parked"] == 0 and report["auth_errors"] == 2
    monkeypatch.setattr(admin, "_open_db", lambda: db)
    admin.main(["partner-outbox"])
    assert "401/403" in capsys.readouterr().out
    # The key is rotated: nothing to release by hand, the queue drains itself.
    clock.advance(minutes=20)
    fake = FakePartner()
    assert partner.deliver_due(db, bridge, fake, clock) == (2, 0)
    assert first.id in {e["question"] for e in fake.envelopes()}
    assert partner.status(db, clock())["auth_errors"] == 0
