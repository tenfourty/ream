"""Step 9e-3, core's half: the outbound partner bridge stands down once a plug-in holds it
(`partner.claim_bridge`, a database fact every process reads), and core hands its live
outbox rows over.

Core keeps owning the per-question version counter (`decision.partner_version`) until 9e-4:
the plug-in's stage asks `partner.next_version` in the answering transaction, so the hub
sees one unbroken version run across the switch. Rows core wrote before the plug-in loaded
(and the foreign answers core's inbound half still writes until 9e-4) move with
`partner.hand_over`, which deletes what it returns in the caller's transaction, so a row
is in exactly one outbox: never lost, never sent twice. `partner.release_bridge` is the
undo.
"""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ream import (
    admin,
    decisions,
    listening,
    partner,
    plugin_api,
    plugin_cli,
    readside,
    sinks,
    store,
)
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
    monkeypatch.setattr(partner, "_owner_absent_warned", False)


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
    """Stands in for the house plug-in's stage sink: it stages only while the database
    says it holds the bridge, and takes its version from core's counter."""

    def __init__(self, owner="qd"):
        self.owner = owner
        self.staged: list[tuple[str, int | None, str]] = []

    def stage(self, conn, event):
        if partner.bridge_owner(conn) != self.owner:
            return
        version = partner.next_version(conn, event.ref_id or "", event.kind)
        self.staged.append((event.ref_id, version, event.kind))

    def sink(self):
        return Sink("quarterdeck", BRIDGE_KINDS, stage=self.stage, plugin=self.owner)


def _claim(db, owner="qd"):
    with db.connect() as conn, conn.transaction():
        partner.claim_bridge(conn, owner)


# --- the switch is a database fact -----------------------------------------------------


def test_claim_bridge_sets_once_and_refuses_another_owner(db):
    with db.connect() as conn:
        assert partner.bridge_owner(conn) is None
    _claim(db)
    _claim(db)  # the same owner again: nothing changes
    with db.connect() as conn:
        assert partner.bridge_owner(conn) == "qd"
    with pytest.raises(partner.BridgeHeld), db.connect() as conn, conn.transaction():
        partner.claim_bridge(conn, "other")


def test_a_claim_rolled_back_leaves_core_in_charge(db):
    class Boom(Exception):
        pass

    with pytest.raises(Boom), db.connect() as conn, conn.transaction():
        partner.claim_bridge(conn, "qd")
        raise Boom
    with db.connect() as conn:
        assert partner.bridge_owner(conn) is None


# --- the stage sink -------------------------------------------------------------------


def test_core_stages_as_before_with_no_claim(db, page, bridge):
    with sinks.isolated(*sinks._builtin_sinks()):
        d = _ask(db, page)
    assert [(r[1], r[2]) for r in _outbox(db)] == [(1, "asked")]
    assert _counter(db, d.id) == 1


def test_a_sink_merely_named_quarterdeck_does_not_silence_core(db, page, bridge):
    named = Sink("quarterdeck", ("decision_answered",), deliver=lambda e: None)
    with sinks.isolated(*sinks._builtin_sinks(), named):
        _ask(db, page)
    assert [(r[1], r[2]) for r in _outbox(db)] == [(1, "asked")]


def test_core_stage_stands_down_once_claimed_and_logs_once(db, page, bridge, caplog):
    qd = FakeQuarterdeck()
    caplog.set_level(logging.INFO, logger="ream.sinks")
    _claim(db)
    with sinks.isolated(*sinks._builtin_sinks(), qd.sink()):
        d = _ask(db, page)
        decisions.answer(db, d.id, letter="a", by="jeremy")
    assert _outbox(db) == []
    assert qd.staged == [(d.id, 1, "asked"), (d.id, 2, "answered")]
    assert _counter(db, d.id) == 2
    said = [r for r in caplog.records if "stands down" in r.getMessage()]
    assert len(said) == 1
    assert said[0].levelno == logging.INFO


def test_core_stages_in_a_process_without_the_owners_stage(db, page, bridge, caplog):
    # A process where the owner's stage is not loaded (`artifacts-admin purge-deleted`,
    # another REAM_PLUGINS): nobody else would write the event, so core's outbox keeps
    # it, and the plug-in's next hand-over moves it. The owner being absent is a WARNING,
    # once per process.
    caplog.set_level(logging.WARNING, logger="ream.partner")
    _claim(db)
    with sinks.isolated(*sinks._builtin_sinks()):
        d = _ask(db, page)
        decisions.answer(db, d.id, letter="a", by="jeremy")
    assert [(r[1], r[2]) for r in _outbox(db)] == [(1, "asked"), (2, "answered")]
    said = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(said) == 1
    assert "'qd'" in said[0].getMessage()
    with db.connect() as conn, conn.transaction():
        moved = partner.hand_over(conn, datetime.now(UTC))
    assert [r["version"] for r in moved.rows] == [1, 2]
    assert _outbox(db) == []


def test_a_cli_process_still_tells_the_partner(db, page, bridge):
    # The command-line plug-in API registers no sink (`artifacts-admin purge-deleted`
    # withdraws a purged page's open question there): the event must still be staged.
    _claim(db)
    with sinks.isolated(*sinks._builtin_sinks()):
        plugin_cli.CliPluginAPI("qd").sink("quarterdeck", BRIDGE_KINDS, stage=_boom)
        d = _ask(db, page)
        decisions.answer(db, d.id, letter="a", by="jeremy")
    assert [r[2] for r in _outbox(db)] == ["asked", "answered"]


def test_another_plugins_bridge_stage_does_not_silence_core(db, page, bridge):
    # The check is by owner, never by sink name: a stage sink named quarterdeck that
    # belongs to another plug-in leaves core writing.
    other = FakeQuarterdeck(owner="other")
    _claim(db)
    with sinks.isolated(*sinks._builtin_sinks(), other.sink()):
        _ask(db, page)
    assert [(r[1], r[2]) for r in _outbox(db)] == [(1, "asked")]
    assert other.staged == []


def test_core_stages_the_kinds_the_owners_stage_does_not_take(db, page, bridge):
    qd = FakeQuarterdeck()
    only_asked = Sink("quarterdeck", ("asked",), stage=qd.stage, plugin="qd")
    _claim(db)
    with sinks.isolated(*sinks._builtin_sinks(), only_asked):
        d = _ask(db, page)
        decisions.answer(db, d.id, letter="a", by="jeremy")
    assert qd.staged == [(d.id, 1, "asked")]
    assert [(r[1], r[2]) for r in _outbox(db)] == [(2, "answered")]


def test_exactly_one_stages_before_and_after_the_claim(db, page, bridge):
    qd = FakeQuarterdeck()
    with sinks.isolated(*sinks._builtin_sinks(), qd.sink()):
        d = _ask(db, page)  # loaded, not yet claimed: core stages, the plug-in does not
        _claim(db)
        decisions.answer(db, d.id, letter="b", by="jeremy")
    assert [(r[1], r[2]) for r in _outbox(db)] == [(1, "asked")]
    assert qd.staged == [(d.id, 2, "answered")]


# --- a failing stage of the bridge's holder fails the action ---------------------------


def _plugin_stage(fn):
    api = plugin_api.PluginAPI("qd")
    api.sink("quarterdeck", BRIDGE_KINDS, stage=fn)


def _boom(conn, event):
    raise RuntimeError("down")


def test_the_holders_failing_stage_fails_the_action(db, page, bridge):
    _claim(db)
    with sinks.isolated(*sinks._builtin_sinks()):
        _plugin_stage(_boom)
        with pytest.raises(plugin_api.BridgeStageFailed):
            _ask(db, page)
    with db.connect() as conn:
        assert conn.execute("select count(*) from decision").fetchone()[0] == 0


def test_a_failing_stage_of_a_plugin_not_holding_the_bridge_is_isolated(
    db, page, bridge
):
    with sinks.isolated(*sinks._builtin_sinks()):
        _plugin_stage(_boom)
        _ask(db, page)  # core holds the bridge: core staged, the answer is kept
    assert [(r[1], r[2]) for r in _outbox(db)] == [(1, "asked")]


def test_versions_run_on_across_the_switch(db, page, bridge):
    with sinks.isolated(*sinks._builtin_sinks()):
        d = _ask(db, page)  # core writes v1 before the claim
    qd = FakeQuarterdeck()
    _claim(db)
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


def test_core_sender_delivers_with_no_claim(db, page, bridge):
    named = Sink("quarterdeck", ("decision_answered",), deliver=lambda e: None)
    with sinks.isolated(*sinks._builtin_sinks(), named):
        _ask(db, page)
        fake = FakePartner()
        assert partner.sender_pass(db, bridge, post=fake) == (1, 0)
    assert len(fake.calls) == 1


def test_core_sender_stands_down_once_claimed_and_logs_once(db, page, bridge, caplog):
    with sinks.isolated(*sinks._builtin_sinks()):
        _ask(db, page)  # a row core staged before the claim
    _claim(db)
    caplog.set_level(logging.INFO, logger="ream.partner")
    fake = FakePartner()
    with sinks.isolated(*sinks._builtin_sinks()):  # no plug-in in this process
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
    _claim(db)
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


def test_release_bridge_gives_rows_back_and_core_the_bridge_at_once(db, page, bridge):
    with sinks.isolated(*sinks._builtin_sinks()):
        _ask(db, page)
    with db.connect() as conn, conn.transaction():
        partner.claim_bridge(conn, "qd")
        rows = partner.hand_over(conn, datetime.now(UTC)).rows
    with db.connect() as conn, conn.transaction():
        assert partner.release_bridge(conn, "qd", rows) == 1
    with db.connect() as conn:
        assert partner.bridge_owner(conn) is None
    fake = FakePartner()
    with sinks.isolated(*sinks._builtin_sinks()):
        assert partner.sender_pass(db, bridge, post=fake) == (1, 0)


def test_release_bridge_refuses_another_owner_and_keeps_everything(db, page, bridge):
    _claim(db)
    with pytest.raises(partner.BridgeHeld), db.connect() as conn, conn.transaction():
        partner.release_bridge(conn, "other", [])
    with db.connect() as conn:
        assert partner.bridge_owner(conn) == "qd"


def test_a_question_past_the_limit_comes_back_held(db, page, bridge):
    with sinks.isolated(*sinks._builtin_sinks()):
        first = _ask(db, page)
        second = _ask(db, page)
    with db.connect() as conn, conn.transaction():
        got = partner.hand_over(conn, datetime.now(UTC), limit=1)
    assert [r["decision_id"] for r in got.rows] == [first.id]
    assert got.held == {second.id}


def test_a_question_not_wholly_locked_is_given_back(db, page, bridge, monkeypatch):
    # Postgres: a sender elsewhere locked or leased one row between the pick and the
    # lock. Simulated by the lock step returning only part of a question.
    with sinks.isolated(*sinks._builtin_sinks()):
        d = _ask(db, page)
        decisions.answer(db, d.id, letter="a", by="jeremy")
        other = _ask(db, page)
    real = partner._lock_rows

    def partial(conn, picked, now):
        rows = real(conn, picked, now)
        return [r for r in rows if not (r["decision_id"] == d.id and r["version"] == 2)]

    monkeypatch.setattr(partner, "_lock_rows", partial)
    with db.connect() as conn, conn.transaction():
        got = partner.hand_over(conn, datetime.now(UTC))
    assert [(r["decision_id"], r["version"]) for r in got.rows] == [(other.id, 1)]
    assert d.id in got.held
    assert sorted((r[0], r[1]) for r in _outbox(db)) == [(d.id, 1), (d.id, 2)]


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


# --- second review round ----------------------------------------------------------------


def test_two_claims_at_once_the_second_is_refused_cleanly(db, monkeypatch):
    # Two plug-in processes claim together: both read "nobody", the first inserts. The
    # second must get BridgeHeld, never the primary key's raw database error.
    _claim(db, "other")
    real = partner.bridge_owner
    reads = []

    def stale_first_read(conn):
        reads.append(1)
        return None if len(reads) == 1 else real(conn)

    monkeypatch.setattr(partner, "bridge_owner", stale_first_read)
    with pytest.raises(partner.BridgeHeld), db.connect() as conn, conn.transaction():
        partner.claim_bridge(conn, "qd")
    monkeypatch.setattr(partner, "bridge_owner", real)
    with db.connect() as conn:
        assert partner.bridge_owner(conn) == "other"


def test_a_claim_between_two_rows_of_a_pass_stops_the_pass(db, page, bridge):
    # The sender reads the owner per row, inside the claim: a plug-in that took the bridge
    # after the pass began gets the second question, core never sends it.
    with sinks.isolated(*sinks._builtin_sinks()):
        _ask(db, page)
        _ask(db, page)

    class ClaimsOnFirstCall(FakePartner):
        def __call__(self, url, body, headers, timeout):
            if not self.calls:
                _claim(db)
            return super().__call__(url, body, headers, timeout)

    fake = ClaimsOnFirstCall()
    with sinks.isolated(*sinks._builtin_sinks()):
        assert partner.sender_pass(db, bridge, post=fake) == (1, 0)
    assert len(fake.calls) == 1
    assert sorted(r[3] is None for r in _outbox(db)) == [False, True]


def test_core_outbox_clear_holds_what_core_still_has(db, page, bridge):
    # The fragment the plug-in's claim statement must carry, so a row core committed
    # after the hand-over's `held` snapshot still holds the question's later versions.
    with sinks.isolated(*sinks._builtin_sinks()):
        held = _ask(db, page)
    with sinks.isolated():
        free = _ask(db, page)
    clear = partner.core_outbox_clear("d.id")
    query = "select d.id from decision d where " + clear  # noqa: S608 -- fixed text
    with db.connect() as conn:
        got = {r[0] for r in conn.execute(query).fetchall()}
    assert free.id in got and held.id not in got
    fake = FakePartner()
    assert partner.deliver_due(db, bridge, post=fake) == (1, 0)
    with db.connect() as conn:
        got = {r[0] for r in conn.execute(query).fetchall()}
    assert {free.id, held.id} <= got  # delivered rows hold nothing


@pytest.mark.parametrize("bad", ["", "d.id; drop table x", "1x", "a.b.c", "d.id--"])
def test_core_outbox_clear_takes_only_a_column_name(bad):
    with pytest.raises(ValueError):
        partner.core_outbox_clear(bad)


def test_status_names_the_owner_and_whether_its_stage_is_here(db):
    assert partner.status(db)["bridge_owner"] is None
    _claim(db)
    with sinks.isolated(*sinks._builtin_sinks()):
        got = partner.status(db)
    assert (got["bridge_owner"], got["bridge_owner_loaded"]) == ("qd", False)
    with sinks.isolated(*sinks._builtin_sinks(), FakeQuarterdeck().sink()):
        assert partner.status(db)["bridge_owner_loaded"] is True


def test_start_up_warns_once_when_the_owner_is_not_loaded(db, caplog):
    caplog.set_level(logging.WARNING, logger="ream.partner")
    with sinks.isolated(*sinks._builtin_sinks()):
        partner.warn_if_owner_absent(db)  # nobody holds it: nothing to say
        assert caplog.records == []
        _claim(db)
        with sinks.isolated(*sinks._builtin_sinks(), FakeQuarterdeck().sink()):
            partner.warn_if_owner_absent(db)
        assert caplog.records == []
        partner.warn_if_owner_absent(db)
        partner.warn_if_owner_absent(db)
    said = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(said) == 1
    assert "partner-bridge --release" in said[0].getMessage()


def test_a_standing_down_sender_warns_when_the_owner_is_not_here(
    db, page, bridge, caplog
):
    caplog.set_level(logging.INFO, logger="ream.partner")
    _claim(db)
    with sinks.isolated(*sinks._builtin_sinks()):
        assert partner.sender_pass(db, bridge, post=FakePartner()) is None
    assert [r.levelno for r in caplog.records if "'qd'" in r.getMessage()] == [
        logging.WARNING
    ]


class _EntryPoint:
    def __init__(self, name):
        self.name = name


def test_admin_partner_bridge_shows_the_owner(db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    admin.main(["partner-bridge"])
    assert "held by core" in capsys.readouterr().out
    _claim(db)
    admin.main(["partner-bridge"])
    assert "held by plug-in 'qd'" in capsys.readouterr().out


def test_admin_release_gives_core_the_bridge_when_the_owner_is_gone(
    db, page, bridge, monkeypatch, capsys
):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    monkeypatch.setattr(plugin_cli, "entry_points", list)
    _claim(db)
    with sinks.isolated(*sinks._builtin_sinks()):
        _ask(db, page)  # queued in core's outbox while the owner is absent
    admin.main(["partner-bridge", "--release"])
    out = capsys.readouterr().out
    assert "released" in out and "1 undelivered" in out
    with db.connect() as conn:
        assert partner.bridge_owner(conn) is None
    with sinks.isolated(*sinks._builtin_sinks()):
        assert partner.sender_pass(db, bridge, post=FakePartner()) == (1, 0)


def test_admin_release_refuses_while_the_owner_is_installed_here(db, monkeypatch):
    # Installed and selected by this shell's REAM_PLUGINS: the plug-in's own release
    # command gives its rows back too; clearing the key here would strand them.
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    monkeypatch.delenv("REAM_PLUGINS", raising=False)
    monkeypatch.delenv("ARTIFACTS_PLUGINS", raising=False)
    monkeypatch.setattr(plugin_cli, "entry_points", lambda: [_EntryPoint("qd")])
    _claim(db)
    with pytest.raises(SystemExit, match="'qd' is installed"):
        admin.main(["partner-bridge", "--release"])
    with db.connect() as conn:
        assert partner.bridge_owner(conn) == "qd"
    monkeypatch.setenv("REAM_PLUGINS", "none")  # installed but not selected: allowed
    admin.main(["partner-bridge", "--release"])
    with db.connect() as conn:
        assert partner.bridge_owner(conn) is None


def test_admin_release_with_nobody_holding_it_says_so(db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    admin.main(["partner-bridge", "--release"])
    assert "held by core" in capsys.readouterr().out
