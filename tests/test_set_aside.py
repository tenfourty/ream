"""Archiving or binning a page SETS ASIDE its open questions; unarchiving or
restoring reopens them (Jeremy, 2 Nov 2026; runcove-5t3oj.64; design
docs/agent/specs/2026-11-02-archive-sets-aside-design.md).

A question is set aside when it is open and its page is archived or binned: derived,
never stored, so it comes back by itself, with the same id and the original asked-at.
"""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from artifacts import decisions, events, keys, partner, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
OPTIONS = [{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}]
HIM = {"Remote-User": "jeremy"}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db), base_url="https://testserver")


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
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://gallery.invalid")
    return partner.config_from_env()


def _ask(db, page, question="Which?", session=None):
    return decisions.ask(
        db,
        agent="seat",
        artifact_id=page.id,
        question=question,
        options=OPTIONS,
        recommended="a",
        session=session,
    )


def _row(db, decision_id):
    (row,) = [
        r
        for r in decisions.list_for_artifact(
            db,
            _artifact_of(db, decision_id),
            include_answered=True,
            include_closed=True,
        )
        if r.id == decision_id
    ]
    return row


def _artifact_of(db, decision_id):
    with db.connect() as conn:
        return conn.execute(
            "select artifact_id from decision where id = %s", (decision_id,)
        ).fetchone()[0]


def _kinds(db, agent="seat"):
    return [
        e.kind
        for e in events.list_for_agent(db, agent)
        if e.kind in ("decision_set_aside", "decision_reopened")
    ]


def _outbox(db, decision_id):
    with db.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                "select kind from partner_outbox where decision_id = %s"
                " order by version",
                (decision_id,),
            ).fetchall()
        ]


@pytest.fixture()
def page(db):
    keys.mint(db, agent="seat")
    return store.publish(db, "seat", "Plan", PAGE)


def test_archive_then_unarchive_keeps_the_same_question(db, page, bridge):
    asked = _ask(db, page, session="seat/1")
    asked_at = _row(db, asked.id).created_at

    assert store.set_archived(db, page.id, True, agent="seat")
    parked = _row(db, asked.id)
    assert parked.set_aside and parked.set_aside_why == "archived"
    assert parked.closed_at is None and parked.answer is None
    assert parked.reopened_at is None

    assert store.set_archived(db, page.id, False, agent="seat")
    back = _row(db, asked.id)
    assert not back.set_aside
    assert back.id == asked.id and back.created_at == asked_at
    assert back.closed_at is None
    assert back.reopened_at is not None

    seen = [
        e
        for e in events.list_for_agent(db, "seat")
        if e.kind in ("decision_set_aside", "decision_reopened")
    ]
    assert [e.kind for e in seen] == ["decision_set_aside", "decision_reopened"]
    assert all(e.session == "seat/1" and e.ref_id == asked.id for e in seen)
    assert all(e.actor for e in seen)
    assert _outbox(db, asked.id) == ["asked", "set_aside", "reopened"]


def test_bin_then_restore(db, page):
    asked = _ask(db, page)
    assert store.set_deleted(db, page.id, True, agent="seat")
    assert _row(db, asked.id).set_aside_why == "binned"
    assert _row(db, asked.id).closed_at is None
    assert store.set_deleted(db, page.id, False, agent="seat")
    assert not _row(db, asked.id).set_aside
    assert _row(db, asked.id).reopened_at is not None
    assert _kinds(db) == ["decision_set_aside", "decision_reopened"]


def test_archived_and_binned_reopens_only_when_both_are_lifted(db, page):
    asked = _ask(db, page)
    store.set_archived(db, page.id, True, agent="seat")
    store.set_deleted(db, page.id, True, agent="seat")
    assert _kinds(db) == ["decision_set_aside"], "already away: nothing more to say"
    store.set_deleted(db, page.id, False, agent="seat")
    assert _row(db, asked.id).set_aside, "still archived"
    assert _kinds(db) == ["decision_set_aside"]
    store.set_archived(db, page.id, False, agent="seat")
    assert not _row(db, asked.id).set_aside
    assert _kinds(db) == ["decision_set_aside", "decision_reopened"]


def test_archiving_an_archived_page_emits_nothing(db, page):
    _ask(db, page)
    store.set_archived(db, page.id, True, agent="seat")
    store.set_archived(db, page.id, True, agent="seat")
    assert _kinds(db) == ["decision_set_aside"]


def test_a_question_asked_on_an_archived_page_is_born_set_aside(
    db, client, page, bridge
):
    store.set_archived(db, page.id, True, agent="seat")
    token, _ = keys.mint(db, agent="seat")
    response = client.post(
        f"/api/v1/artifacts/{page.id}/decisions",
        json={"question": "Late?", "options": OPTIONS},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code in (200, 201), response.text
    body = response.json()
    assert "set aside" in body["warning"]
    assert _outbox(db, body["id"]) == ["asked", "set_aside"]
    store.set_archived(db, page.id, False, agent="seat")
    assert _outbox(db, body["id"]) == ["asked", "set_aside", "reopened"]


def test_a_question_asked_on_an_archived_page_announces_the_set_aside(db, page):
    """Born set aside still sends `decision_set_aside`, so the unarchive's
    `decision_reopened` is never the first the agent hears of it."""
    store.set_archived(db, page.id, True, agent="seat")
    asked = _ask(db, page, question="Late?", session="seat/7")
    (event,) = [
        e
        for e in events.list_for_agent(db, "seat")
        if e.kind == "decision_set_aside" and e.ref_id == asked.id
    ]
    assert event.summary == "archived: Late?" and event.session == "seat/7"
    store.set_archived(db, page.id, False, agent="seat")
    assert _kinds(db) == ["decision_set_aside", "decision_reopened"]


def test_a_question_asked_on_a_binned_page_says_binned(db, page):
    store.set_deleted(db, page.id, True, agent="seat")
    asked = _ask(db, page, question="Gone?")
    (event,) = [
        e
        for e in events.list_for_agent(db, "seat")
        if e.kind == "decision_set_aside" and e.ref_id == asked.id
    ]
    assert event.summary == "binned: Gone?"


def test_every_list_leaves_set_aside_out_unless_asked(db, client, page):
    live = store.publish(db, "seat", "Live", PAGE)
    waiting = _ask(db, live, question="Live?")
    parked = _ask(db, page, question="Parked?")
    store.set_archived(db, page.id, True, agent="seat")
    token, _ = keys.mint(db, agent="seat")
    head = {"Authorization": f"Bearer {token}"}

    assert decisions.open_counts(db)[2] == 1
    ids = [r.decision.id for r in decisions.across_seats(db)]
    assert ids == [waiting.id]
    ids = [r.decision.id for r in decisions.across_seats(db, include_set_aside=True)]
    assert set(ids) == {waiting.id, parked.id}
    assert [r.id for r in decisions.list_for_agent(db, "seat")] == [waiting.id]
    both = decisions.list_for_agent(db, "seat", include_set_aside=True)
    assert {r.id for r in both} == {waiting.id, parked.id}

    plain = client.get("/api/v1/decisions?scope=mine", headers=head).json()
    assert [r["id"] for r in plain] == [waiting.id]
    more = client.get(
        "/api/v1/decisions?scope=mine&include_set_aside=true", headers=head
    ).json()
    assert {r["id"] for r in more} == {waiting.id, parked.id}
    assert next(r for r in more if r["id"] == parked.id)["set_aside"] is True

    qs = client.get("/api/v1/questions", headers=head).text
    assert waiting.id in qs and parked.id not in qs
    qs = client.get("/api/v1/questions?include_set_aside=true", headers=head).text
    assert waiting.id in qs and parked.id in qs


def test_list_for_agent_shows_set_aside_on_a_binned_page_only_when_asked(db, page):
    open_q = _ask(db, page, question="Open?")
    answered = _ask(db, page, question="Done?")
    decisions.answer(db, answered.id, letter="a", note="", by="jeremy")
    store.set_deleted(db, page.id, True, agent="seat")
    assert decisions.list_for_agent(db, "seat") == []
    got = decisions.list_for_agent(
        db, "seat", include_set_aside=True, include_answered=True
    )
    assert [r.id for r in got] == [open_q.id]
    assert got[0].set_aside_why == "binned"


def test_the_wall_and_the_page_hide_a_set_aside_question(db, client, page):
    _ask(db, page, question="Zebra crossing?")
    assert "waiting on you" in client.get("/", headers=HIM).text
    store.set_archived(db, page.id, True, agent="seat")
    assert "waiting on you" not in client.get("/", headers=HIM).text
    assert "Zebra crossing?" not in client.get(f"/a/{page.id}", headers=HIM).text


def test_a_set_aside_question_cannot_be_answered(db, page):
    asked = _ask(db, page)
    store.set_archived(db, page.id, True, agent="seat")
    with pytest.raises(store.NotFound):
        decisions.answer(db, asked.id, letter="a", note="", by="jeremy")
    assert _row(db, asked.id).answer is None


def test_the_partner_may_not_send_set_aside_or_reopened(db):
    from artifacts import partner_in

    assert "set_aside" not in partner_in.KINDS
    assert "reopened" not in partner_in.KINDS
    assert "answered" in partner_in.KINDS


def test_the_purge_withdraws_a_question_still_set_aside(db, page, bridge):
    from datetime import UTC, datetime, timedelta

    asked = _ask(db, page)
    store.set_deleted(db, page.id, True, agent="seat")
    with db.connect() as conn:
        conn.execute(
            "update artifact set deleted_at = %s where id = %s",
            (datetime.now(UTC) - timedelta(days=31), page.id),
        )
    assert store.purge_expired(db) == [page.id]
    assert _outbox(db, asked.id)[-1] == "withdrawn"


def test_setting_aside_a_review_releases_its_holds(db, page):
    review = decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    store.set_archived(db, page.id, True, agent="seat")
    row = _row(db, review.id)
    assert row.set_aside and row.closed_at is None
    assert _kinds(db) == ["decision_set_aside"]


def test_an_archive_between_the_read_and_the_write_refuses_the_answer(
    db, page, monkeypatch
):
    """`answer` checks set-aside on a row read before its write transaction; the
    write itself must also require a live page, or an archive in between lets an
    answer land on a parked question."""
    asked = _ask(db, page)
    real = decisions._set_aside_fields

    def archive_after_the_read(row):
        out = real(row)
        monkeypatch.setattr(decisions, "_set_aside_fields", real)
        store.set_archived(db, page.id, True, agent="seat")
        return out

    monkeypatch.setattr(decisions, "_set_aside_fields", archive_after_the_read)
    with pytest.raises(store.NotFound):
        decisions.answer(db, asked.id, letter="a", note="", by="jeremy")
    assert _row(db, asked.id).answer is None


def test_an_archive_between_the_read_and_the_write_refuses_a_relay(
    db, page, monkeypatch
):
    """The same guard on `record_elsewhere`: refused, and nothing stored."""
    asked = _ask(db, page)
    real = decisions._set_aside_fields

    def archive_after_the_read(row):
        out = real(row)
        monkeypatch.setattr(decisions, "_set_aside_fields", real)
        store.set_archived(db, page.id, True, agent="seat")
        return out

    monkeypatch.setattr(decisions, "_set_aside_fields", archive_after_the_read)
    with pytest.raises(store.NotFound):
        decisions.record_elsewhere(
            db,
            asked.id,
            letter="a",
            note="his words",
            source="terminal",
            recorded_by="seat",
        )
    assert _row(db, asked.id).answer is None
