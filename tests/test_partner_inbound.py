"""The inbound partner bridge (runcove-5t3oj.39.4, second half): a signed event
from the partner, each refusal with its agreed status, and what each event does.

Every key pair is generated in memory for the test. The one file a test writes
is the partner's PUBLIC key bundle, to a pytest temp path, so the service can
read it the way it does in production. No private key is written anywhere
except the outbound signing key the outbound half already needs (a temp file).
"""

import base64
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from artifacts import (
    decisions,
    grants,
    hosts,
    keys,
    partner,
    partner_in,
    store,
)
from artifacts.app import create_app

PAGE = (
    "<!doctype html><html><head><title>t</title></head><body><p>plan</p></body></html>"
)
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]
HOST = "artifacts-svc.internal"
KEY_ID = "qd-test-1"
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
HIM = {"Remote-User": "jeremy"}
SLACK_ID = "U0TESTSLACK"


def _public_pem(private) -> str:
    return (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


@pytest.fixture()
def bridge(monkeypatch, tmp_path):
    """The outbound half configured (the inbound needs it), with a throwaway key."""
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
    monkeypatch.setenv(partner.ENV_KEY_ID, "artifacts-test-1")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://gallery.invalid")
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "content.invalid")
    monkeypatch.setenv("ARTIFACTS_API_HOST", "api.invalid")
    return partner.config_from_env()


@pytest.fixture()
def partner_key():
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def inbound(bridge, partner_key):
    return partner_in.Inbound(host=HOST, keys={KEY_ID: partner_key.public_key()})


@pytest.fixture()
def app(db, inbound):
    return create_app(db, inbound)


@pytest.fixture()
def client(app):
    return TestClient(app, base_url=f"http://{HOST}")


@pytest.fixture()
def gallery(app):
    return TestClient(app, base_url="https://gallery.invalid")


@pytest.fixture()
def page(db):
    return store.publish(db, "owner", "Plan", PAGE)


def ask(db, page, question="Which?", **fields):
    return decisions.ask(
        db,
        agent="owner",
        artifact_id=page.id,
        question=question,
        options=OPTIONS,
        recommended="a",
        **fields,
    )


# --- building and sending events ---------------------------------------------------


def stamp(**delta) -> str:
    return (datetime.now(UTC) + timedelta(**delta)).isoformat()


def make(kind, source, version, body, /, **over) -> dict:
    event = {
        "event_id": f"ev-{uuid.uuid4().hex[:12]}",
        "kind": kind,
        "source": source,
        "question": source.split(":", 1)[1],
        "version": version,
        "key_id": KEY_ID,
        "at": stamp(),
        "sent_at": stamp(),
        "body": body,
    }
    event.update(over)
    return event


def raw_of(event: dict) -> bytes:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()


def sign(key, raw: bytes) -> str:
    return base64.b64encode(key.sign(raw)).decode()


def post(client, key, event, *, raw=None, signature=None, headers=None):
    raw = raw if raw is not None else raw_of(event)
    sent = {partner.SIGNATURE_HEADER: signature or sign(key, raw)}
    sent.update(headers or {})
    return client.post(partner_in.PATH, content=raw, headers=sent)


def pressed(answer="b", note="", **who):
    return {
        "answer": answer,
        "note": note,
        "answered_by": {
            "name": "Jeremy",
            "slack_id": SLACK_ID,
            "verified_by": "quarterdeck",
            **who,
        },
        "answered_at": stamp(),
        "answer_source": "slack",
    }


def asked_body(**over):
    body = {
        "asking_office": "warpgate",
        "title": "Open the port?",
        "options": [
            {"letter": "a", "text": "Yes"},
            {"letter": "b", "text": "No"},
        ],
        "recommended": "a",
        "context": "It is about the firewall.",
        "link": "https://slack.invalid/archives/C1/p1",
        "topic": {"id": "t1", "title": "Network"},
        "depends_on": [],
        "replaces": None,
        "urgency": "when_you_can",
        "blocked_on": None,
        "needed_by": None,
        "for": [],
    }
    body.update(over)
    return body


def _outbox(db):
    with db.connect() as conn:
        return conn.execute(
            "select decision_id, version, kind, body, event_id from partner_outbox"
            " order by created_at, version"
        ).fetchall()


def _seen(db, event_id):
    with db.connect() as conn:
        return conn.execute(
            "select outcome, detail from partner_event_seen where event_id = %s",
            (event_id,),
        ).fetchone()


def _row(db, decision_id):
    return decisions.get_owned(db, "owner", decision_id)


# --- configuration -----------------------------------------------------------------


def test_off_when_neither_variable_is_set(bridge):
    assert partner_in.config_from_env(bridge) is None


def test_half_configured_is_refused_without_naming_a_value(
    bridge, monkeypatch, tmp_path
):
    monkeypatch.setenv(partner_in.ENV_HOST, HOST)
    with pytest.raises(partner_in.PartnerInboundConfigError) as caught:
        partner_in.config_from_env(bridge)
    assert partner_in.ENV_KEYS_FILE in str(caught.value) and HOST not in str(
        caught.value
    )


def test_it_needs_the_outbound_bridge_too(monkeypatch, tmp_path, partner_key):
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps({KEY_ID: _public_pem(partner_key)}))
    monkeypatch.setenv(partner_in.ENV_HOST, HOST)
    monkeypatch.setenv(partner_in.ENV_KEYS_FILE, str(keys_file))
    with pytest.raises(partner_in.PartnerInboundConfigError, match="outbound"):
        partner_in.config_from_env(None)


def test_several_key_ids_are_accepted_for_a_rotation(
    bridge, monkeypatch, tmp_path, partner_key
):
    other = Ed25519PrivateKey.generate()
    raw_b64 = base64.b64encode(
        other.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps({"old": _public_pem(partner_key), "new": raw_b64}))
    monkeypatch.setenv(partner_in.ENV_HOST, HOST)
    monkeypatch.setenv(partner_in.ENV_KEYS_FILE, str(keys_file))
    config = partner_in.config_from_env(bridge)
    assert set(config.keys) == {"old", "new"} and config.host == HOST
    assert "BEGIN" not in repr(config)


def test_a_bad_key_file_names_the_variable_never_the_content(
    bridge, monkeypatch, tmp_path
):
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps({"k": "SECRET-LOOKING-TEXT"}))
    monkeypatch.setenv(partner_in.ENV_HOST, HOST)
    monkeypatch.setenv(partner_in.ENV_KEYS_FILE, str(keys_file))
    with pytest.raises(partner_in.PartnerInboundConfigError) as caught:
        partner_in.config_from_env(bridge)
    assert "SECRET-LOOKING-TEXT" not in str(caught.value)


@pytest.mark.parametrize(
    "name", ["gallery.invalid", "api.invalid", "x.content.invalid"]
)
def test_the_host_cannot_be_a_public_face(
    bridge, monkeypatch, tmp_path, partner_key, name
):
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps({KEY_ID: _public_pem(partner_key)}))
    monkeypatch.setenv(partner_in.ENV_HOST, name)
    monkeypatch.setenv(partner_in.ENV_KEYS_FILE, str(keys_file))
    with pytest.raises(partner_in.PartnerInboundConfigError, match="its own"):
        partner_in.config_from_env(bridge)


# --- the in-cluster route ----------------------------------------------------------


def test_the_endpoint_answers_on_the_cluster_host(client, partner_key):
    event = make("asked", "qd:q1", 1, asked_body())
    assert post(client, partner_key, event).status_code == 200


def test_the_endpoint_is_not_on_the_public_hosts(
    db, app, gallery, partner_key, monkeypatch
):
    event = make("asked", "qd:q1", 1, asked_body())
    on_gallery = post(gallery, partner_key, event)
    assert on_gallery.status_code in (404, 405)
    api = post(TestClient(app, base_url="https://api.invalid"), partner_key, event)
    assert api.status_code in (404, 405)
    content_host = f"{'a' * 26}.content.invalid"
    content = post(
        TestClient(app, base_url=f"https://{content_host}"), partner_key, event
    )
    assert content.status_code in (404, 405)
    unknown = post(
        TestClient(app, base_url="https://elsewhere.invalid"), partner_key, event
    )
    assert unknown.status_code == 404
    with db.connect() as conn:
        assert conn.execute("select count(*) from partner_question").fetchone()[0] == 0


def test_the_endpoint_does_not_exist_when_the_bridge_is_off(db, bridge, partner_key):
    off = TestClient(create_app(db), base_url=f"http://{HOST}")
    event = make("asked", "qd:q1", 1, asked_body())
    assert post(off, partner_key, event).status_code == 404


def test_a_request_through_a_proxy_is_refused(client, partner_key):
    event = make("asked", "qd:q1", 1, asked_body())
    for header in ("X-Forwarded-For", "CF-Connecting-IP", "X-Real-IP"):
        r = post(client, partner_key, event, headers={header: "203.0.113.9"})
        assert r.status_code == 404


def test_the_gallery_routes_are_not_on_the_cluster_host(client):
    assert client.get("/inbox", headers=HIM).status_code == 404
    assert client.get("/api/v1/questions").status_code in (404, 405)


def test_it_is_not_behind_the_agent_key(client, partner_key):
    r = post(client, partner_key, make("asked", "qd:q1", 1, asked_body()))
    assert r.status_code == 200  # no Authorization header was sent


# --- every status arm, in the agreed order -----------------------------------------


def test_oversize_is_400(client, partner_key):
    raw = b'{"x":"' + b"a" * partner_in.MAX_BODY + b'"}'
    assert post(client, partner_key, None, raw=raw).status_code == 400


def test_oversize_by_content_length_is_400_before_reading(client, partner_key):
    r = client.post(
        partner_in.PATH,
        content=b"{}",
        headers={"content-length": str(partner_in.MAX_BODY + 1)},
    )
    assert r.status_code == 400


@pytest.mark.parametrize("raw", [b"not json", b"", b"[1,2]", b'"str"', b"\xff\xfe"])
def test_not_json_or_not_an_object_is_400(client, partner_key, raw):
    assert post(client, partner_key, None, raw=raw).status_code == 400


@pytest.mark.parametrize(
    "field",
    [
        "event_id",
        "kind",
        "source",
        "question",
        "version",
        "key_id",
        "at",
        "sent_at",
        "body",
    ],
)
def test_a_missing_field_is_422(client, partner_key, field):
    event = make("asked", "qd:q1", 1, asked_body())
    del event[field]
    assert post(client, partner_key, event).status_code == 422


@pytest.mark.parametrize(
    "over",
    [
        {"version": "1"},
        {"version": 0},
        {"version": True},
        {"kind": "exploded"},
        {"source": "elsewhere:q1"},
        {"source": "qd:"},
        {"question": "other"},
        {"body": "text"},
        {"sent_at": "yesterday"},
        {"sent_at": "2026-10-21T10:00:00"},  # no zone
        {"event_id": ""},
        {"key_id": 5},
    ],
)
def test_a_mistyped_field_is_422(client, partner_key, over):
    event = make("asked", "qd:q1", 1, asked_body(), **over)
    assert post(client, partner_key, event).status_code == 422


def test_malformed_beats_a_bad_signature(client):
    # Cheapest first: a body that cannot be parsed is refused before any key is tried.
    event = make("asked", "qd:q1", 1, asked_body())
    del event["event_id"]
    other = Ed25519PrivateKey.generate()
    assert post(client, other, event).status_code == 422


def test_an_unknown_key_id_is_401(client, partner_key):
    event = make("asked", "qd:q1", 1, asked_body(), key_id="not-listed")
    assert post(client, partner_key, event).status_code == 401


def test_a_signature_by_another_key_is_401(client):
    event = make("asked", "qd:q1", 1, asked_body())
    assert post(client, Ed25519PrivateKey.generate(), event).status_code == 401


def test_a_missing_or_garbled_signature_is_401(client, partner_key):
    raw = raw_of(make("asked", "qd:q1", 1, asked_body()))
    assert client.post(partner_in.PATH, content=raw).status_code == 401
    garbled = client.post(
        partner_in.PATH, content=raw, headers={partner.SIGNATURE_HEADER: "!!not b64!!"}
    )
    assert garbled.status_code == 401


def test_one_byte_changed_after_signing_is_401(client, db, partner_key):
    raw = raw_of(make("asked", "qd:q1", 1, asked_body()))
    signature = sign(partner_key, raw)
    tampered = raw.replace(b"Open the port?", b"Open the pory?")
    assert len(tampered) == len(raw) and tampered != raw
    r = client.post(
        partner_in.PATH, content=tampered, headers={partner.SIGNATURE_HEADER: signature}
    )
    assert r.status_code == 401
    with db.connect() as conn:
        assert conn.execute("select count(*) from partner_question").fetchone()[0] == 0


def test_the_signature_is_over_the_raw_bytes_not_the_parsed_json(client, partner_key):
    event = make("asked", "qd:q1", 1, asked_body())
    spaced = json.dumps(event, indent=2).encode()  # not the compact form
    assert post(client, partner_key, event, raw=spaced).status_code == 200
    # ...and a signature over the compact form does not cover the spaced bytes.
    wrong = post(
        client,
        partner_key,
        event,
        raw=spaced,
        signature=sign(partner_key, raw_of(event)),
    )
    assert wrong.status_code == 401


@pytest.mark.parametrize("delta", [{"minutes": -6}, {"minutes": -5, "seconds": -5}])
def test_a_stale_sent_at_is_401(client, partner_key, delta):
    event = make("asked", "qd:q1", 1, asked_body(), sent_at=stamp(**delta))
    assert post(client, partner_key, event).status_code == 401


def test_a_sent_at_too_far_in_the_future_is_401(client, partner_key):
    event = make("asked", "qd:q1", 1, asked_body(), sent_at=stamp(seconds=90))
    assert post(client, partner_key, event).status_code == 401


def test_a_little_clock_skew_either_way_is_accepted(client, partner_key):
    ahead = make("asked", "qd:q1", 1, asked_body(), sent_at=stamp(seconds=30))
    behind = make("asked", "qd:q2", 1, asked_body(), sent_at=stamp(minutes=-4))
    assert post(client, partner_key, ahead).status_code == 200
    assert post(client, partner_key, behind).status_code == 200


def test_a_duplicate_event_id_is_2xx_and_changes_nothing(client, db, partner_key):
    event = make("asked", "qd:q1", 1, asked_body())
    assert post(client, partner_key, event).json() == {"result": "applied"}
    again = post(client, partner_key, event)
    assert again.status_code == 200 and again.json() == {"result": "duplicate"}
    # A retry re-signed with a fresh sent_at is the same event id.
    event["sent_at"] = stamp(seconds=5)
    assert post(client, partner_key, event).json() == {"result": "duplicate"}


def test_an_older_version_is_2xx_and_changes_nothing(client, db, partner_key):
    post(client, partner_key, make("asked", "qd:q1", 5, asked_body(title="newer")))
    old = make("asked", "qd:q1", 3, asked_body(title="older"))
    r = post(client, partner_key, old)
    assert r.status_code == 200 and r.json() == {"result": "stale"}
    same = post(
        client, partner_key, make("asked", "qd:q1", 5, asked_body(title="same"))
    )
    assert same.status_code == 200
    with db.connect() as conn:
        assert conn.execute(
            "select title, version from partner_question"
        ).fetchone() == ("newer", 5)
    assert _seen(db, old["event_id"])[0] == "stale"


def test_a_refusal_never_echoes_the_body_or_a_key(client, partner_key, caplog):
    caplog.set_level(logging.DEBUG)
    event = make("asked", "qd:q1", 1, asked_body(context="PRIVATE-CONTEXT-TEXT"))
    raw = raw_of(event)
    signature = sign(partner_key, raw)
    seen = []
    for response in (
        client.post(partner_in.PATH, content=raw),  # no signature
        client.post(
            partner_in.PATH,
            content=raw.replace(b"PRIVATE", b"PRIVATF"),
            headers={partner.SIGNATURE_HEADER: signature},
        ),
        client.post(partner_in.PATH, content=b"PRIVATE-CONTEXT-TEXT{"),
    ):
        seen.append(response.text)
        assert response.json() in ({"error": "unauthorized"}, {"error": "malformed"})
    ok = post(client, partner_key, event)
    seen.append(ok.text)
    everything = "\n".join(seen) + "\n" + caplog.text
    assert "PRIVATE" not in everything
    assert signature not in everything
    assert "BEGIN" not in everything and KEY_ID not in "\n".join(seen)


# --- a question whose home is artifacts --------------------------------------------


def test_a_slack_answer_settles_the_page_question(
    db, bridge, client, partner_key, page
):
    q = ask(db, page)
    event = make("answered", f"artifacts:{q.id}", 1, pressed("b", note="go slow"))
    r = post(client, partner_key, event)
    assert r.status_code == 200 and r.json() == {"result": "applied"}
    row = _row(db, q.id)
    assert (row.answer, row.answer_note) == ("b", "go slow")
    assert row.answer_source == "slack"
    assert row.recorded_by == "quarterdeck" and row.answered_by == "jeremy"
    outcome, detail = _seen(db, event["event_id"])
    assert outcome == "applied" and detail["slack_id"] == SLACK_ID
    assert detail["verified_by"] == "quarterdeck"


def test_it_emits_the_normal_answered_event_and_the_outbound_answered(
    db, bridge, client, partner_key, page
):
    q = ask(db, page)
    post(client, partner_key, make("answered", f"artifacts:{q.id}", 1, pressed("a")))
    with db.connect() as conn:
        journal = conn.execute(
            "select kind, agent, ref_id, summary from event where ref_id = %s",
            (q.id,),
        ).fetchall()
    assert [(k, a, ref) for k, a, ref, _ in journal] == [
        ("decision_answered", "owner", q.id)
    ]
    assert "Slack" in journal[0][3]
    sent = [r for r in _outbox(db) if r[0] == q.id]
    assert [(r[1], r[2]) for r in sent] == [(1, "asked"), (2, "answered")]
    body = sent[1][3]
    assert body["answer"] == "a" and body["answer_source"] == "slack"
    assert body["answered_by"] == {"name": "Jeremy", "verified_by": "quarterdeck"}
    assert "recorded_by" not in body


def test_a_changed_slack_answer_is_allowed_and_the_same_one_is_not_repeated(
    db, bridge, client, partner_key, page
):
    q = ask(db, page)
    src = f"artifacts:{q.id}"
    post(client, partner_key, make("answered", src, 1, pressed("a")))
    same = post(client, partner_key, make("answered", src, 2, pressed("a")))
    assert same.status_code == 200
    assert len([r for r in _outbox(db) if r[2] == "answered"]) == 1
    changed = post(client, partner_key, make("answered", src, 3, pressed("b")))
    assert changed.json() == {"result": "applied"}
    assert _row(db, q.id).answer == "b"
    assert len([r for r in _outbox(db) if r[2] == "answered"]) == 2


def test_a_page_answer_after_a_slack_one_replaces_it_as_a_page_answer(
    db, bridge, client, partner_key, page
):
    q = ask(db, page)
    post(client, partner_key, make("answered", f"artifacts:{q.id}", 1, pressed("a")))
    decisions.answer(db, q.id, letter="b", by="jeremy")
    row = _row(db, q.id)
    assert (row.answer, row.answer_source, row.recorded_by) == ("b", "page", None)


def test_an_unknown_decision_is_2xx_and_changes_nothing(
    db, bridge, client, partner_key, page
):
    q = ask(db, page)
    before = len(_outbox(db))
    event = make("answered", "artifacts:" + "z" * 26, 1, pressed("a"))
    r = post(client, partner_key, event)
    assert r.status_code == 200
    assert _row(db, q.id).answer is None and len(_outbox(db)) == before
    # Indistinguishable from a real one that is not applicable: same status, same body.
    real_but_wrong_kind = post(
        client, partner_key, make("asked", f"artifacts:{q.id}", 1, asked_body())
    )
    assert real_but_wrong_kind.status_code == r.status_code


def test_a_review_or_closed_or_unannounced_decision_is_not_settled(
    db, bridge, client, partner_key, page, monkeypatch
):
    review = decisions.ask_review(db, agent="owner", artifact_id=page.id)
    closed = ask(db, page, "closed?")
    decisions.close(db, "owner", closed.id)
    for target in (review.id, closed.id):
        r = post(
            client,
            partner_key,
            make("answered", f"artifacts:{target}", 1, pressed("a")),
        )
        assert r.status_code == 200
    assert decisions.get_owned(db, "owner", review.id).answer is None
    assert decisions.get_owned(db, "owner", closed.id).answer is None
    # A question asked before the bridge was on was never announced: not settled.
    monkeypatch.delenv(partner.ENV_NAME)
    old = ask(db, page, "before the bridge")
    monkeypatch.setenv(partner.ENV_NAME, "partner-x")
    r = post(
        client, partner_key, make("answered", f"artifacts:{old.id}", 1, pressed("a"))
    )
    assert r.status_code == 200 and _row(db, old.id).answer is None


def test_a_held_question_is_not_settled_and_the_refusal_is_recorded(
    db, bridge, client, partner_key, page
):
    first = ask(db, page, "first?")
    second = ask(db, page, "second?", depends_on=[first.id])
    event = make("answered", f"artifacts:{second.id}", 1, pressed("a"))
    r = post(client, partner_key, event)
    assert r.status_code == 200
    assert _row(db, second.id).answer is None
    assert _seen(db, event["event_id"])[0] == "held"
    # Once the prerequisite is answered the next press (a new version) settles it.
    decisions.answer(db, first.id, letter="a", by="jeremy")
    again = post(
        client, partner_key, make("answered", f"artifacts:{second.id}", 2, pressed("b"))
    )
    assert again.json() == {"result": "applied"}
    assert _row(db, second.id).answer == "b"


def test_only_a_quarterdeck_verified_press_counts(
    db, bridge, client, partner_key, page
):
    q = ask(db, page)
    unverified = pressed("a", verified_by="agent")
    r = post(client, partner_key, make("answered", f"artifacts:{q.id}", 1, unverified))
    assert r.status_code == 422 and _row(db, q.id).answer is None
    no_who = pressed("a")
    del no_who["answered_by"]
    r = post(client, partner_key, make("answered", f"artifacts:{q.id}", 1, no_who))
    assert r.status_code == 422


def test_an_option_that_does_not_exist_is_ignored_with_2xx(
    db, bridge, client, partner_key, page
):
    q = ask(db, page)
    event = make("answered", f"artifacts:{q.id}", 1, pressed("z"))
    assert post(client, partner_key, event).status_code == 200
    assert (
        _row(db, q.id).answer is None and _seen(db, event["event_id"])[0] == "refused"
    )


def test_other_events_about_an_artifacts_question_change_nothing(
    db, bridge, client, partner_key, page
):
    q = ask(db, page)
    for version, kind in enumerate(("asked", "withdrawn", "replaced", "edited"), 1):
        r = post(client, partner_key, make(kind, f"artifacts:{q.id}", version, {}))
        assert r.status_code == 200
    assert _row(db, q.id).closed_at is None and _row(db, q.id).answer is None
    with db.connect() as conn:
        assert conn.execute("select count(*) from partner_question").fetchone()[0] == 0


def test_the_answer_and_the_dedupe_record_commit_together(
    db, bridge, client, partner_key, page, monkeypatch
):
    q = ask(db, page)
    event = make("answered", f"artifacts:{q.id}", 1, pressed("a"))

    def boom(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(partner, "enqueue", boom)
    r = post(client, partner_key, event)
    assert r.status_code == 500 and r.json() == {"error": "failed"}
    assert _row(db, q.id).answer is None
    assert _seen(db, event["event_id"]) is None  # retried: nothing was recorded
    monkeypatch.undo()


# --- a question whose home is the partner -----------------------------------------


def _inbox_ids(client):
    html = client.get("/inbox", headers=HIM).text
    return html, re.findall(r'<li class="ask" id="i-([^"]+)"', html)


def test_an_asked_copy_appears_in_his_inbox_with_the_slack_label(
    db, bridge, app, client, gallery, partner_key, page
):
    post(client, partner_key, make("asked", "qd:q1", 1, asked_body()))
    html, ids = _inbox_ids(gallery)
    assert ids == ["qd:q1"]
    assert "from Slack" in html and "Open the port?" in html
    assert "It is about the firewall." in html
    assert 'href="https://slack.invalid/archives/C1/p1"' in html
    assert "(a) Yes" in html and "(b) No" in html and "recommended" in html
    assert "Network" in html and "from warpgate" in html


def test_a_copy_takes_its_place_in_the_one_order(
    db, bridge, app, client, gallery, partner_key, page
):
    later = ask(db, page, "page question")
    post(
        client,
        partner_key,
        make(
            "asked",
            "qd:blocking",
            1,
            asked_body(urgency="blocking", blocked_on="the build"),
        ),
    )
    post(
        client,
        partner_key,
        make("asked", "qd:easy", 1, asked_body(title="Easy one", topic=None)),
    )
    post(
        client,
        partner_key,
        make(
            "asked",
            "qd:late",
            1,
            asked_body(title="Overdue", urgency="by", needed_by=stamp(hours=-2)),
        ),
    )
    _, ids = _inbox_ids(gallery)
    assert ids == ["qd:late", "qd:blocking", later.id, "qd:easy"]


def test_a_stored_link_that_is_not_http_is_dropped(
    db, bridge, app, client, gallery, partner_key
):
    post(
        client,
        partner_key,
        make("asked", "qd:q1", 1, asked_body(link="javascript:alert(1)")),
    )
    html, _ = _inbox_ids(gallery)
    assert "javascript:" not in html and "Asked in Slack" in html


def test_hostile_text_is_escaped(db, bridge, app, client, gallery, partner_key):
    body = asked_body(
        title="<script>alert(1)</script>", context="<img src=x onerror=y>"
    )
    post(client, partner_key, make("asked", "qd:q1", 1, body))
    html, _ = _inbox_ids(gallery)
    assert "<script>alert(1)</script>" not in html and "<img src=x" not in html


def test_the_inbox_count_includes_a_copy(db, bridge, client, partner_key):
    from artifacts import inbox

    assert inbox.count(db) == (0, False)
    post(client, partner_key, make("asked", "qd:q1", 1, asked_body()))
    assert inbox.count(db) == (1, False)


def test_answering_a_copy_here_is_pending_and_sends_an_answered_event(
    db, bridge, app, client, gallery, partner_key
):
    post(client, partner_key, make("asked", "qd:q1", 1, asked_body()))
    r = gallery.post(
        "/pq/q1/answer",
        data={"letter": "b", "note": "later", "next": "inbox"},
        headers={**HIM, **SAME_ORIGIN},
        follow_redirects=False,
    )
    assert r.status_code == 303
    out = [row for row in _outbox(db) if row[0] == "qd:q1"]
    assert [(row[1], row[2]) for row in out] == [(1, "answered")]
    assert out[0][3]["answer"] == "b" and out[0][3]["note"] == "later"
    # The envelope that goes out names the partner's own source.
    event = json.loads(
        partner.envelope(
            {
                "event_id": out[0][4],
                "decision_id": out[0][0],
                "version": out[0][1],
                "kind": out[0][2],
                "body": out[0][3],
                "created_at": datetime.now(UTC),
            },
            "artifacts-test-1",
            datetime.now(UTC),
        )
    )
    assert event["source"] == "qd:q1" and event["question"] == "q1"
    html, _ids = _inbox_ids(gallery)
    # No longer asked of him, and shown as pending until the home answers.
    assert "waiting for quarterdeck" in html
    assert 'name="letter"' not in html


def test_the_homes_answered_finalises_it(db, bridge, app, client, gallery, partner_key):
    post(client, partner_key, make("asked", "qd:q1", 1, asked_body()))
    gallery.post(
        "/pq/q1/answer",
        data={"letter": "b"},
        headers={**HIM, **SAME_ORIGIN},
        follow_redirects=False,
    )
    home = pressed("a", note="decided in Slack")
    home["answered_by"] = {"name": "Jeremy", "verified_by": "quarterdeck"}
    post(client, partner_key, make("answered", "qd:q1", 2, home))
    with db.connect() as conn:
        row = conn.execute(
            "select state, answer, answer_note, pending_answer from partner_question"
        ).fetchone()
    assert tuple(row) == ("answered", "a", "decided in Slack", None)
    html, _ids = _inbox_ids(gallery)
    assert "waiting for quarterdeck" not in html
    assert "Settled in Slack" in html and "(a) Yes" in html


def test_our_versions_for_a_copy_are_our_own_counter(
    db, bridge, app, client, gallery, partner_key
):
    # The home is already at version 9; our events count 1, 2, ... separately.
    post(client, partner_key, make("asked", "qd:q1", 9, asked_body()))
    for letter in ("a", "b"):
        gallery.post(
            "/pq/q1/answer",
            data={"letter": letter},
            headers={**HIM, **SAME_ORIGIN},
            follow_redirects=False,
        )
    assert [row[1] for row in _outbox(db) if row[0] == "qd:q1"] == [1, 2]


def test_the_answer_form_is_guarded_and_checked(
    db, bridge, app, client, gallery, partner_key
):
    post(client, partner_key, make("asked", "qd:q1", 1, asked_body()))
    cross = gallery.post(
        "/pq/q1/answer",
        data={"letter": "a"},
        headers={**HIM, "sec-fetch-site": "cross-site"},
    )
    assert cross.status_code == 403
    bad = gallery.post(
        "/pq/q1/answer", data={"letter": "z"}, headers={**HIM, **SAME_ORIGIN}
    )
    assert bad.status_code == 422
    gone = gallery.post(
        "/pq/nope/answer", data={"letter": "a"}, headers={**HIM, **SAME_ORIGIN}
    )
    assert gone.status_code == 404
    assert _outbox(db) == []


def test_withdrawn_and_replaced_remove_it_from_the_inbox(
    db, bridge, app, client, gallery, partner_key
):
    for qid in ("w", "r", "kept"):
        post(client, partner_key, make("asked", f"qd:{qid}", 1, asked_body(title=qid)))
    post(
        client,
        partner_key,
        make("withdrawn", "qd:w", 2, {"reason": "no longer needed"}),
    )
    post(
        client,
        partner_key,
        make(
            "replaced", "qd:r", 2, {"replaced_by": "r2", "replaced_by_source": "qd:r2"}
        ),
    )
    _, ids = _inbox_ids(gallery)
    assert ids == ["qd:kept"]
    with db.connect() as conn:
        states = dict(
            conn.execute("select source, state from partner_question").fetchall()
        )
    assert states == {"qd:w": "withdrawn", "qd:r": "replaced", "qd:kept": "open"}
    # A press on a withdrawn copy is refused.
    r = gallery.post(
        "/pq/w/answer", data={"letter": "a"}, headers={**HIM, **SAME_ORIGIN}
    )
    assert r.status_code == 404


def test_edited_changes_the_context_and_message_added_is_ignored(
    db, bridge, app, client, gallery, partner_key
):
    post(client, partner_key, make("asked", "qd:q1", 1, asked_body()))
    post(client, partner_key, make("edited", "qd:q1", 2, {"context": "new context"}))
    reply = post(
        client, partner_key, make("message_added", "qd:q1", 3, {"text": "hello"})
    )
    assert reply.status_code == 200
    html, _ = _inbox_ids(gallery)
    assert "new context" in html and "hello" not in html


def test_an_event_before_its_asked_is_503_retryable_and_stores_nothing(
    db, bridge, client, partner_key
):
    early = make("answered", "qd:q1", 2, pressed("a"))
    first = post(client, partner_key, early)
    assert first.status_code == 503 and first.headers["retry-after"] == "30"
    for kind, body in (
        ("edited", {"context": "c"}),
        ("withdrawn", {}),
        ("replaced", {}),
    ):
        r = post(client, partner_key, make(kind, "qd:q1", 2, body))
        assert r.status_code == 503
    with db.connect() as conn:
        for table in ("partner_event_seen", "partner_version_seen", "partner_question"):
            assert conn.execute(f"select count(*) from {table}").fetchone()[0] == 0  # noqa: S608
    assert post(
        client, partner_key, make("asked", "qd:q1", 1, asked_body())
    ).json() == {"result": "applied"}
    # The sender's retry of the very same event now applies.
    retried = post(client, partner_key, {**early, "sent_at": stamp()})
    assert retried.json() == {"result": "applied"}
    with db.connect() as conn:
        assert (
            conn.execute("select state from partner_question").fetchone()[0]
            == "answered"
        )


def test_message_added_before_its_asked_is_still_ignored(
    db, bridge, client, partner_key
):
    r = post(client, partner_key, make("message_added", "qd:q1", 2, {"text": "hi"}))
    assert r.status_code == 200 and r.json() == {"result": "ignored"}


def test_deeply_nested_json_is_400(client, partner_key):
    raw = b"[" * 30000 + b"]" * 30000
    assert post(client, partner_key, None, raw=raw).status_code == 400
    raw = b'{"a":' * 12000 + b"1" + b"}" * 12000
    assert post(client, partner_key, None, raw=raw).status_code == 400


def test_a_bad_asked_body_is_422(client, partner_key):
    for bad in (
        asked_body(options=[]),
        asked_body(
            options=[{"letter": "a", "text": "x"}, {"letter": "a", "text": "y"}]
        ),
        asked_body(title=""),
        asked_body(urgency="whenever"),
        asked_body(needed_by="soon"),
        asked_body(depends_on="q1"),
    ):
        assert (
            post(client, partner_key, make("asked", "qd:q1", 1, bad)).status_code == 422
        )


def test_old_events_and_settled_copies_are_pruned_after_30_days(
    db, bridge, client, partner_key
):
    post(client, partner_key, make("asked", "qd:q1", 1, asked_body()))
    post(client, partner_key, make("withdrawn", "qd:q1", 2, {}))
    now = datetime.now(UTC)
    partner.prune(db, now)
    with db.connect() as conn:
        assert (
            conn.execute("select count(*) from partner_event_seen").fetchone()[0] == 2
        )
    partner.prune(db, now + timedelta(days=31))
    with db.connect() as conn:
        assert (
            conn.execute("select count(*) from partner_event_seen").fetchone()[0] == 0
        )
        assert conn.execute("select count(*) from partner_question").fetchone()[0] == 0
        # The last version per question is kept, so a replay still reads as old.
        assert (
            conn.execute("select count(*) from partner_version_seen").fetchone()[0] == 1
        )


# --- the API twin ------------------------------------------------------------------


def test_the_api_lists_copies_only_for_read_all_with_scope_all(
    db, bridge, app, client, gallery, partner_key, page
):
    post(client, partner_key, make("asked", "qd:q1", 1, asked_body()))
    ask(db, page, "page question")
    reader, _ = keys.mint(db, agent="reader")
    plain, _ = keys.mint(db, agent="plain")
    grants.grant(db, "reader", grants.READ_ALL_DECISIONS)
    h_reader = {"Authorization": f"Bearer {reader}"}
    h_plain = {"Authorization": f"Bearer {plain}"}

    everything = gallery.get("/api/v1/questions?scope=all", headers=h_reader).json()
    mine = [r for r in everything if r.get("source") == "qd:q1"]
    assert len(mine) == 1
    view = mine[0]
    assert view["home"] == "partner" and view["read_only"] is True
    assert view["question"] == "Open the port?" and view["agent"] == "warpgate"
    assert view["options"][0] == {"letter": "a", "label": "Yes"}
    assert any(r["question"] == "page question" for r in everything)

    # Not for a key without the grant, and not in the default scope.
    assert (
        gallery.get("/api/v1/questions?scope=all", headers=h_plain).status_code == 403
    )
    own = gallery.get("/api/v1/questions", headers=h_reader).json()
    assert all(r.get("source") is None for r in own)
    # Read-only: there is no route that answers it.
    r = gallery.post(
        "/api/v1/decisions/q1/record",
        headers=h_reader,
        json={"letter": "a", "note": "n", "source": "slack"},
    )
    assert r.status_code in (404, 422)


def test_agents_cannot_record_a_slack_source(db, bridge, gallery, page):
    q = ask(db, page)
    owner, _ = keys.mint(db, agent="owner")
    r = gallery.post(
        f"/api/v1/decisions/{q.id}/record",
        headers={"Authorization": f"Bearer {owner}"},
        json={"letter": "a", "note": "n", "source": "slack"},
    )
    assert r.status_code == 422 and _row(db, q.id).answer is None


# --- what the page shows -----------------------------------------------------------


def test_the_page_says_answered_in_slack_verified_by_quarterdeck(
    db, bridge, client, gallery, partner_key, page
):
    q = ask(db, page)
    post(client, partner_key, make("answered", f"artifacts:{q.id}", 1, pressed("a")))
    html = gallery.get(f"/a/{page.id}", headers=HIM).text
    assert "Slack" in html and "quarterdeck" in html
    assert hosts.normalise(HOST) == HOST
