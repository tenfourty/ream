"""Step 9e-3, core's half: the outbound partner bridge stands down while a plug-in sink
named `quarterdeck` is registered, and core hands its live outbox rows over.

Core keeps owning the per-question version counter (`decision.partner_version`) until 9e-4:
the plug-in's stage asks `partner.next_version` in the answering transaction, so the hub
sees one unbroken version run across the switch. Rows core wrote before the plug-in loaded
(and the foreign answers core's inbound half still writes until 9e-4) move with
`partner.hand_over`, which deletes what it returns in the caller's transaction, so a row
is in exactly one outbox: never lost, never sent twice. `partner.take_back` is the undo.
"""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ream import decisions, listening, partner, plugin_api, readside, sinks, store
from ream.events import BRIDGE_KINDS
from ream.sinks import Sink

PAGE = (
    "<!doctype html><html><head><title>t</title></head><body><p>plan</p></body></html>"
)
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


class FakePartner:
    """Records every attempt; answers from a script of statuses (then 200)."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[tuple[str, bytes, dict]] = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, dict(headers)))
        return self.script.pop(0) if self.script else 200


@pytest.fixture()
def bridge(monkeypatch, tmp_path):
    # A throwaway key, written to a pytest temp file only so core reads it as it does
    # in production; no key is kept anywhere else.
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
    return partner.config_from_env()


@pytest.fixture()
def page(db):
    return store.publish(db, "owner", "Plan", PAGE)


@pytest.fixture(autouse=True)
def _fresh_log_flags(monkeypatch):
    monkeypatch.setattr(sinks, "_partner_stood_down_logged", False)
    monkeypatch.setattr(partner, "_sender_stood_down_logged", False)


def _ask(db, page):
    return decisions.ask(
        db,
        agent="owner",
        artifact_id=page.id,
        question="Which?",
        options=OPTIONS,
        recommended="a",
    )


def _outbox(db):
    with db.connect() as conn:
        return conn.execute(
            "select decision_id, version, kind, delivered_at from partner_outbox"
            " order by decision_id, version"
        ).fetchall()


def _counter(db, decision_id):
    with db.connect() as conn:
        return conn.execute(
            "select partner_version from decision where id = %s", (decision_id,)
        ).fetchone()[0]


class FakeQuarterdeck:
    """Stands in for the house plug-in's stage sink: takes its version from core's
    counter, the way the plug-in does until 9e-4."""

    def __init__(self):
        self.staged: list[tuple[str, int | None, str]] = []

    def stage(self, conn, event):
        version = partner.next_version(conn, event.ref_id or "", event.kind)
        self.staged.append((event.ref_id, version, event.kind))

    def sink(self):
        return Sink("quarterdeck", BRIDGE_KINDS, stage=self.stage)


# --- the stage sink -------------------------------------------------------------------


def test_core_stages_as_before_with_no_quarterdeck_sink(db, page, bridge):
    with sinks.isolated(*sinks._builtin_sinks()):
        d = _ask(db, page)
    assert [(r[1], r[2]) for r in _outbox(db)] == [(1, "asked")]
    assert _counter(db, d.id) == 1


def test_core_stage_stands_down_and_logs_once(db, page, bridge, caplog):
    qd = FakeQuarterdeck()
    caplog.set_level(logging.INFO, logger="ream.sinks")
    with sinks.isolated(*sinks._builtin_sinks(), qd.sink()):
        d = _ask(db, page)
        decisions.answer(db, d.id, letter="a", by="jeremy")
    assert _outbox(db) == []
    assert qd.staged == [(d.id, 1, "asked"), (d.id, 2, "answered")]
    assert _counter(db, d.id) == 2
    said = [r for r in caplog.records if "stands down" in r.getMessage()]
    assert len(said) == 1
    assert said[0].levelno == logging.INFO
    assert "quarterdeck" in said[0].getMessage()


def test_versions_run_on_across_the_switch(db, page, bridge):
    with sinks.isolated(*sinks._builtin_sinks()):
        d = _ask(db, page)  # core writes v1 before the plug-in loads
    qd = FakeQuarterdeck()
    with sinks.isolated(*sinks._builtin_sinks(), qd.sink()):
        decisions.answer(db, d.id, letter="b", by="jeremy")
    assert [(r[1], r[2]) for r in _outbox(db)] == [(1, "asked")]
    assert qd.staged == [(d.id, 2, "answered")]


# --- next_version ---------------------------------------------------------------------


def test_next_version_never_announces_a_question_late(db, page):
    with sinks.isolated():  # nothing staged: the counter stays 0
        d = _ask(db, page)
    with db.connect() as conn, conn.transaction():
        assert partner.next_version(conn, d.id, "withdrawn") is None
        assert partner.next_version(conn, "no-such-id", "asked") is None
        assert partner.next_version(conn, d.id, "asked") == 1
        assert partner.next_version(conn, d.id, "withdrawn") == 2
    with (
        pytest.raises(ValueError, match="unknown partner event kind"),
        db.connect() as conn,
        conn.transaction(),
    ):
        partner.next_version(conn, d.id, "opened")


def test_next_version_does_not_depend_on_cores_own_config(db, page, monkeypatch):
    # The plug-in reads its own settings; core's partner group may be unset.
    for name in partner._ENV_ALL:
        monkeypatch.delenv(name, raising=False)
    with sinks.isolated():
        d = _ask(db, page)
    with db.connect() as conn, conn.transaction():
        assert partner.next_version(conn, d.id, "asked") == 1


# --- the sender -----------------------------------------------------------------------


def test_core_sender_delivers_with_no_quarterdeck_sink(db, page, bridge):
    with sinks.isolated(*sinks._builtin_sinks()):
        _ask(db, page)
        fake = FakePartner()
        assert partner.sender_pass(db, bridge, post=fake) == (1, 0)
    assert len(fake.calls) == 1


def test_core_sender_stands_down_and_logs_once(db, page, bridge, caplog):
    with sinks.isolated(*sinks._builtin_sinks()):
        _ask(db, page)  # a row core staged before the plug-in loaded
    caplog.set_level(logging.INFO, logger="ream.partner")
    fake = FakePartner()
    with sinks.isolated(*sinks._builtin_sinks(), FakeQuarterdeck().sink()):
        assert partner.sender_pass(db, bridge, post=fake) is None
        assert partner.sender_pass(db, bridge, post=fake) is None
    assert fake.calls == []
    assert len(_outbox(db)) == 1  # left for the plug-in's hand-over, not dropped
    said = [r for r in caplog.records if "stands down" in r.getMessage()]
    assert len(said) == 1
    assert said[0].levelno == logging.INFO


def test_a_standing_down_sender_still_prunes(db, page, bridge, monkeypatch):
    pruned = []
    monkeypatch.setattr(partner, "prune", lambda db, now=None: pruned.append(1) or 0)
    with sinks.isolated(*sinks._builtin_sinks(), FakeQuarterdeck().sink()):
        partner.sender_pass(db, bridge, post=FakePartner(), prune_due=True)
    assert pruned == [1]


# --- hand-over ------------------------------------------------------------------------


def test_hand_over_moves_undelivered_rows_once(db, page, bridge):
    with sinks.isolated(*sinks._builtin_sinks()):
        d = _ask(db, page)
        decisions.answer(db, d.id, letter="a", by="jeremy")
        other = _ask(db, page)
    now = datetime.now(UTC)
    with db.connect() as conn:  # d's v1 went out already
        conn.execute(
            "update partner_outbox set delivered_at = %s"
            " where decision_id = %s and version = 1",
            (now, d.id),
        )
    with db.connect() as conn, conn.transaction():
        got = partner.hand_over(conn, now)
    assert got.held == set()
    moved = sorted((r["decision_id"], r["version"], r["kind"]) for r in got.rows)
    assert moved == sorted([(d.id, 2, "answered"), (other.id, 1, "asked")])
    for row in got.rows:
        assert set(row) >= {
            "event_id",
            "body",
            "created_at",
            "attempts",
            "next_attempt_at",
            "parked_at",
            "last_error",
            "last_error_at",
        }
        assert isinstance(row["body"], dict)
    # Only the delivered row stays; a second hand-over finds nothing; core's sender
    # has nothing left to claim.
    assert [(r[0], r[1]) for r in _outbox(db)] == [(d.id, 1)]
    with db.connect() as conn, conn.transaction():
        assert partner.hand_over(conn, now).rows == []
    assert partner._claim(db, now + timedelta(days=1)) == []


def test_hand_over_rolled_back_keeps_the_rows(db, page, bridge):
    with sinks.isolated(*sinks._builtin_sinks()):
        _ask(db, page)

    class Boom(Exception):
        pass

    with pytest.raises(Boom), db.connect() as conn, conn.transaction():
        assert len(partner.hand_over(conn, datetime.now(UTC)).rows) == 1
        raise Boom  # the plug-in's insert failed: nothing is lost
    assert len(_outbox(db)) == 1


def test_hand_over_holds_a_question_core_is_sending_now(db, page, bridge):
    with sinks.isolated(*sinks._builtin_sinks()):
        d = _ask(db, page)
        decisions.answer(db, d.id, letter="a", by="jeremy")
        other = _ask(db, page)
    now = datetime.now(UTC)
    claimed = partner._claim(db, now, 1)  # an old process is mid-attempt on d's v1
    assert claimed[0]["decision_id"] == d.id
    with db.connect() as conn, conn.transaction():
        got = partner.hand_over(conn, now)
    assert got.held == {d.id}
    assert [(r["decision_id"], r["version"]) for r in got.rows] == [(other.id, 1)]
    # Once the lease is gone, the whole question moves, in order.
    later = now + timedelta(seconds=partner.LEASE_SECONDS + 1)
    with db.connect() as conn, conn.transaction():
        got = partner.hand_over(conn, later)
    assert sorted(r["version"] for r in got.rows) == [1, 2]


def test_hand_over_carries_parked_rows_as_parked(db, page, bridge):
    with sinks.isolated(*sinks._builtin_sinks()):
        _ask(db, page)
        partner.deliver_due(db, bridge, post=FakePartner(422))
    with db.connect() as conn, conn.transaction():
        (row,) = partner.hand_over(conn, datetime.now(UTC)).rows
    assert row["parked_at"] is not None
    assert row["last_error"] == "HTTP 422"
    assert row["attempts"] == 1


def test_hand_over_takes_the_foreign_answers_cores_inbound_still_writes(db):
    now = datetime.now(UTC)
    body = partner.answered_body(
        letter="a", note="", answered_by="jeremy", answered_at=now, answer_source="page"
    )
    with db.connect() as conn, conn.transaction():
        partner.enqueue_foreign(conn, "qd:Q1", 1, "answered", body, at=now)
    with db.connect() as conn, conn.transaction():
        (row,) = partner.hand_over(conn, now).rows
    assert (row["decision_id"], row["version"], row["kind"]) == ("qd:Q1", 1, "answered")


def test_hand_over_is_bounded(db, page, bridge):
    with sinks.isolated(*sinks._builtin_sinks()):
        for _ in range(3):
            _ask(db, page)
    with db.connect() as conn, conn.transaction():
        assert len(partner.hand_over(conn, datetime.now(UTC), limit=2).rows) == 2
    assert len(_outbox(db)) == 1


def test_take_back_restores_rows_and_is_idempotent(db, page, bridge):
    with sinks.isolated(*sinks._builtin_sinks()):
        _ask(db, page)
    now = datetime.now(UTC)
    with db.connect() as conn, conn.transaction():
        rows = partner.hand_over(conn, now).rows
    assert _outbox(db) == []
    with db.connect() as conn, conn.transaction():
        assert partner.take_back(conn, rows) == 1
    with db.connect() as conn, conn.transaction():
        assert partner.take_back(conn, rows) == 0
    assert len(_outbox(db)) == 1
    fake = FakePartner()
    assert partner.deliver_due(db, bridge, post=fake) == (1, 0)


# --- the listening state's "delivered" (read side, API 1.4) ---------------------------


def test_listening_reads_a_plugins_delivered(db, page, bridge, monkeypatch):
    qd = FakeQuarterdeck()
    with sinks.isolated(*sinks._builtin_sinks(), qd.sink()):
        d = _ask(db, page)
    asked = {}
    monkeypatch.setattr(
        readside,
        "_CONTRIBUTORS",
        [
            readside.Contributor(
                "qd", enabled=lambda: True, delivered=lambda db, ids: asked
            )
        ],
    )
    route = listening.for_decisions(db, "owner", [d.id])[d.id]
    assert route.state == listening.PARTNER_DELAYED
    asked[d.id] = True
    route = listening.for_decisions(db, "owner", [d.id])[d.id]
    assert route.state == listening.SENT_TO_PARTNER


def test_a_failing_delivered_contributor_is_ignored(db, page, bridge, monkeypatch):
    with sinks.isolated(*sinks._builtin_sinks()):
        d = _ask(db, page)
    partner.deliver_due(db, bridge, post=FakePartner(200))

    def boom(db, ids):
        raise RuntimeError("down")

    monkeypatch.setattr(
        readside, "_CONTRIBUTORS", [readside.Contributor("qd", delivered=boom)]
    )
    route = listening.for_decisions(db, "owner", [d.id])[d.id]
    assert route.state == listening.SENT_TO_PARTNER  # core's own answer stands


def test_api_1_4_accepts_delivered(monkeypatch):
    monkeypatch.setattr(readside, "_CONTRIBUTORS", [])
    assert plugin_api.API_VERSION == (1, 4)
    api = plugin_api.PluginAPI("qd")
    api.read_side(delivered=lambda db, ids: {})
    assert readside.contributors()[0].delivered is not None
    with pytest.raises(plugin_api.PluginError, match="not callable"):
        plugin_api.PluginAPI("qd2").read_side(delivered=1)
