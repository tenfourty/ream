"""Step 9e-4, core's half (C1): the inbound half of the partner bridge has its own switch,
the `service_meta` key `partner_inbound_owner` (no row means core). Every inbound writer
locks that row first in its own transaction and writes only while core holds it; a plug-in
that holds the outbound bridge claims it (`partner.claim_inbound`), moves core's rows away
(`partner.hand_over_inbound`) and gives them back with `partner.release_inbound`.

Once claimed: core's endpoint answers 503 with `Retry-After` and records nothing, his
answer on a partner-home copy is "not mine" in core, core's stage refuses to write core's
outbox, and the partner host goes to the plug-in's route only if the key names it.

Every key pair is generated in memory (the fixtures of `test_partner_inbound`).
"""

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from test_partner_inbound import (  # noqa: F401 -- fixtures and senders
    HOST,
    _outbox,
    _row,
    _seen,
    app,
    ask,
    asked_body,
    bridge,
    client,
    gallery,
    inbound,
    make,
    page,
    partner_key,
    post,
    pressed,
)

from ream import (
    admin,
    decisions,
    partner,
    partner_in,
    plugin_api,
    settings,
    sinks,
    store,
)
from ream.app import create_app
from ream.events import BRIDGE_KINDS
from ream.sinks import Sink


def _boom(conn, event):
    raise RuntimeError("down")


def _claim(db, owner="qd"):
    with db.connect() as conn, conn.transaction():
        partner.claim_bridge(conn, owner)
        partner.claim_inbound(conn, owner)


def _holder(db):
    with db.connect() as conn:
        return partner.inbound_owner(conn)


def _count(db, table):
    with db.connect() as conn:
        return conn.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608 -- fixed names


def _stale_first_read(monkeypatch):
    """The request's first, unlocked look says core although the database says a plug-in:
    what a claim committing between that look and the writer's locked read looks like."""
    real = partner.inbound_owner
    reads = []

    def stale(conn):
        reads.append(1)
        return partner.CORE if len(reads) == 1 else real(conn)

    monkeypatch.setattr(partner, "inbound_owner", stale)
    return reads


# --- the switch ---------------------------------------------------------------------------


def test_no_row_means_core_and_nothing_seeds_one(db):
    assert _holder(db) == partner.CORE
    with db.connect() as conn:
        assert (
            conn.execute(
                "select count(*) from service_meta where key = %s",
                (partner.INBOUND_OWNER_KEY,),
            ).fetchone()[0]
            == 0
        )


def test_the_first_writer_locks_the_row_as_core(db, client, partner_key):
    r = post(client, partner_key, make("asked", "qd:q1", 1, asked_body()))
    assert r.status_code == 200
    with db.connect() as conn:
        assert conn.execute(
            "select value from service_meta where key = %s",
            (partner.INBOUND_OWNER_KEY,),
        ).fetchall() == [("core",)]


def test_claim_inbound_needs_the_outbound_bridge_first(db):
    with (
        pytest.raises(partner.BridgeHeld, match="outbound"),
        db.connect() as conn,
        conn.transaction(),
    ):
        partner.claim_inbound(conn, "qd")
    assert _holder(db) == partner.CORE


def test_claim_inbound_sets_once_and_refuses_another_owner(db):
    _claim(db)
    _claim(db)  # the same owner again: nothing changes
    assert _holder(db) == "qd"
    # Another plug-in cannot hold outbound, so it cannot get as far as inbound either;
    # with the outbound key forced to it, inbound still names its holder.
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "update service_meta set value = 'other' where key = %s",
            (partner.BRIDGE_OWNER_KEY,),
        )
    with (
        pytest.raises(partner.BridgeHeld, match="'qd'"),
        db.connect() as conn,
        conn.transaction(),
    ):
        partner.claim_inbound(conn, "other")
    assert _holder(db) == "qd"


@pytest.mark.parametrize("name", ["core", ""])
def test_claim_inbound_takes_only_a_plugins_name(db, name):
    with pytest.raises(ValueError), db.connect() as conn, conn.transaction():
        partner.claim_inbound(conn, name)


def test_claim_inbound_works_over_a_core_row(db, client, partner_key):
    post(client, partner_key, make("asked", "qd:q1", 1, asked_body()))  # writes 'core'
    _claim(db)
    assert _holder(db) == "qd"


# --- core's endpoint stands down ----------------------------------------------------------


def test_a_claimed_endpoint_answers_503_and_records_nothing(
    db, client, partner_key, page
):
    q = ask(db, page)
    _claim(db)
    home = make("answered", f"artifacts:{q.id}", 1, pressed("b"))
    copy = make("asked", "qd:q1", 1, asked_body())
    for event in (home, copy):
        r = post(client, partner_key, event)
        assert r.status_code == 503
        assert r.json() == {"error": "stood_down"}
        assert r.headers["Retry-After"] == "30"
        assert _seen(db, event["event_id"]) is None
    assert _row(db, q.id).answer is None
    assert _count(db, "partner_question") == 0
    assert _count(db, "partner_version_seen") == 0


def test_a_claimed_endpoint_still_checks_the_signature_first(db, client, partner_key):
    _claim(db)
    event = make("asked", "qd:q1", 1, asked_body())
    r = post(client, partner_key, event, signature="AAAA")
    assert r.status_code == 401


def test_a_claim_during_a_page_home_answer_rolls_the_answer_back(
    db, client, partner_key, page, monkeypatch
):
    # No stage in this process, so the answer reaches the hook: the gate inside the
    # answer's own transaction refuses, and the decision row it wrote goes with it.
    with sinks.isolated():
        q = ask(db, page)
        with db.connect() as conn, conn.transaction():
            conn.execute(
                "update decision set partner_version = 1 where id = %s", (q.id,)
            )
        _claim(db)
        reads = _stale_first_read(monkeypatch)
        event = make("answered", f"artifacts:{q.id}", 2, pressed("b"))
        r = post(client, partner_key, event)
    assert reads
    assert r.status_code == 503 and r.json() == {"error": "stood_down"}
    assert r.headers["Retry-After"] == "30"
    assert _row(db, q.id).answer is None
    assert _seen(db, event["event_id"]) is None


def test_a_claim_during_a_copy_event_writes_nothing(
    db, client, partner_key, monkeypatch
):
    _claim(db)
    reads = _stale_first_read(monkeypatch)
    event = make("asked", "qd:q1", 1, asked_body())
    r = post(client, partner_key, event)
    assert reads
    assert r.status_code == 503 and r.json() == {"error": "stood_down"}
    assert _count(db, "partner_question") == 0
    assert _seen(db, event["event_id"]) is None


def test_a_claim_during_an_event_that_only_records_writes_nothing(
    db, client, partner_key, monkeypatch
):
    _claim(db)
    reads = _stale_first_read(monkeypatch)
    event = make("answered", "artifacts:nosuchdecision", 1, pressed("b"))
    r = post(client, partner_key, event)
    assert reads
    assert r.status_code == 503
    assert _seen(db, event["event_id"]) is None


def test_cores_stage_refusing_inside_an_answer_is_503_not_500(
    db, client, partner_key, page, monkeypatch
):
    # Core's own stage is the only one here. The claim lands after the request's first
    # look: the stage refuses inside `decisions.answer`, before the hook.
    q = ask(db, page)
    _claim(db)
    _stale_first_read(monkeypatch)
    event = make("answered", f"artifacts:{q.id}", 2, pressed("b"))
    r = post(client, partner_key, event)
    assert r.status_code == 503 and r.json() == {"error": "stood_down"}
    assert r.headers["Retry-After"] == "30"
    assert _row(db, q.id).answer is None
    assert [row[2] for row in _outbox(db)] == ["asked"]


def test_a_failing_answer_hook_is_503_not_500(
    db, client, partner_key, page, monkeypatch
):
    def boom(conn, decision, answer):
        raise RuntimeError("down")

    monkeypatch.setattr(
        decisions, "_ANSWER_HOOKS", [("guard", "check", boom, {"slack"})]
    )
    monkeypatch.setattr(plugin_api, "_failures", {})
    q = ask(db, page)
    event = make("answered", f"artifacts:{q.id}", 1, pressed("b"))
    r = post(client, partner_key, event)
    assert r.status_code == 503 and r.json() == {"error": "not_yet"}
    assert r.headers["Retry-After"] == "30"
    assert _row(db, q.id).answer is None
    assert _seen(db, event["event_id"]) is None


def test_the_bridge_holders_failing_stage_in_an_answer_is_503_not_500(
    db, client, partner_key, page
):
    # The question is asked while core still holds the bridge. The plug-in then claims
    # the outbound bridge only, so core's endpoint still applies the event: the holder's
    # stage fails inside the answer, which rolls back.
    q = ask(db, page)
    with db.connect() as conn, conn.transaction():
        partner.claim_bridge(conn, "qd")
    with sinks.isolated(*sinks._builtin_sinks()):
        plugin_api.PluginAPI("qd").sink("quarterdeck", BRIDGE_KINDS, stage=_boom)
        event = make("answered", f"artifacts:{q.id}", 1, pressed("b"))
        r = post(client, partner_key, event)
    assert r.status_code == 503 and r.json() == {"error": "not_yet"}
    assert r.headers["Retry-After"] == "30"
    assert _row(db, q.id).answer is None
    assert _seen(db, event["event_id"]) is None


def test_his_answer_on_a_copy_is_not_cores_once_claimed(db, client, partner_key):
    post(client, partner_key, make("asked", "qd:q1", 1, asked_body()))
    # The key alone, the copy still in core's table: the gate decides, not the row.
    with db.connect() as conn, conn.transaction():
        partner.claim_bridge(conn, "qd")
        partner.claim_inbound(conn, "qd")
    with pytest.raises(store.NotFound):
        partner_in.answer_here(db, "q1", letter="b")
    assert [row for row in _outbox(db) if row[0] == "qd:q1"] == []
    with db.connect() as conn:
        assert conn.execute(
            "select pending_answer, out_version from partner_question"
        ).fetchall() == [(None, 0)]


# --- the move and its undo ----------------------------------------------------------------


def test_hand_over_inbound_is_refused_while_core_holds_it(db):
    with (
        pytest.raises(partner.BridgeHeld, match="core"),
        db.connect() as conn,
        conn.transaction(),
    ):
        partner.hand_over_inbound(conn)


def _seed(db, client, partner_key, page):
    """One copy with a pending answer, one page question answered from Slack, one
    question announced and open."""
    post(client, partner_key, make("asked", "qd:q1", 3, asked_body()))
    partner_in.answer_here(db, "q1", letter="b", note="later")
    answered = ask(db, page)
    pressed_event = make("answered", f"artifacts:{answered.id}", 1, pressed("b"))
    assert post(client, partner_key, pressed_event).status_code == 200
    waiting = ask(db, page, question="And this?")
    return answered, waiting, pressed_event


def test_the_move_leaves_every_row_in_one_table_set(db, client, partner_key, page):
    answered, waiting, pressed_event = _seed(db, client, partner_key, page)
    with db.connect() as conn, conn.transaction():
        partner.claim_bridge(conn, "qd")
        partner.claim_inbound(conn, "qd")
        moved = partner.hand_over_inbound(conn)
    assert set(moved.rows) == set(partner.INBOUND_TABLES)
    for table, cols in partner.INBOUND_TABLES.items():
        assert _count(db, table) == 0
        assert moved.rows[table] and all(tuple(r) == cols for r in moved.rows[table])
    (copy,) = moved.rows["partner_question"]
    assert copy["source"] == "qd:q1" and copy["version"] == 3
    assert copy["pending_answer"] == "b" and copy["out_version"] == 1
    assert [o["letter"] for o in copy["options"]] == ["a", "b"]
    assert {r["source"] for r in moved.rows["partner_version_seen"]} == {
        "qd:q1",
        f"artifacts:{answered.id}",
    }
    press = next(
        r
        for r in moved.rows["partner_event_seen"]
        if r["event_id"] == pressed_event["event_id"]
    )
    assert press["outcome"] == "applied" and press["detail"]["slack_id"]
    # asked, then the outbound `answered` of the Slack press; asked alone.
    assert moved.versions == {answered.id: 2, waiting.id: 1}
    assert partner_in.open_copies(db, waiting_on_him=False) == []


def test_a_rolled_back_move_keeps_the_rows_in_core(db, client, partner_key, page):
    _seed(db, client, partner_key, page)
    before = {t: _count(db, t) for t in partner.INBOUND_TABLES}
    with pytest.raises(RuntimeError), db.connect() as conn, conn.transaction():
        partner.claim_bridge(conn, "qd")
        partner.claim_inbound(conn, "qd")
        partner.hand_over_inbound(conn)
        raise RuntimeError("the plug-in's own insert failed")
    assert {t: _count(db, t) for t in partner.INBOUND_TABLES} == before
    assert _holder(db) == partner.CORE


def test_release_puts_the_rows_and_the_counters_back(db, client, partner_key, page):
    answered, waiting, _ = _seed(db, client, partner_key, page)
    before = partner_in.open_copies(db, waiting_on_him=False)
    assert len(before) == 1
    with db.connect() as conn, conn.transaction():
        partner.claim_bridge(conn, "qd")
        partner.claim_inbound(conn, "qd")
        moved = partner.hand_over_inbound(conn)
    total = sum(len(rows) for rows in moved.rows.values())
    # The plug-in counted on: one question further, one never above core's number.
    versions = {answered.id: 5, waiting.id: 1, "gone": 9}
    with db.connect() as conn, conn.transaction():
        assert partner.release_inbound(conn, "qd", moved.rows, versions) == total
    assert _holder(db) == partner.CORE
    assert partner_in.open_copies(db, waiting_on_him=False) == before
    with db.connect() as conn:
        assert dict(
            conn.execute(
                "select id, partner_version from decision where partner_version > 0"
            ).fetchall()
        ) == {answered.id: 5, waiting.id: 1}
    # Core's endpoint is back, and still knows what it had seen.
    again = make("asked", "qd:q1", 3, asked_body())
    r = post(client, partner_key, again)
    assert r.status_code == 200 and r.json() == {"result": "stale"}
    # ... and the outbound half can now go back too.
    with db.connect() as conn, conn.transaction():
        partner.release_bridge(conn, "qd", [])
        assert partner.bridge_owner(conn) is None


def test_release_never_lowers_a_counter_or_replaces_a_row(
    db, client, partner_key, page
):
    answered, _waiting, _ = _seed(db, client, partner_key, page)
    _claim(db)  # the key only: core's rows are still here
    cols = partner.INBOUND_TABLES["partner_question"]
    with db.connect() as conn:
        held = conn.execute(
            f"select {', '.join(cols)} from partner_question"  # noqa: S608 -- fixed names
        ).fetchone()
    stale_copy = {**dict(zip(cols, held, strict=True)), "version": 1, "title": "old"}
    with db.connect() as conn, conn.transaction():
        put = partner.release_inbound(
            conn, "qd", {"partner_question": [stale_copy]}, {answered.id: 1}
        )
    assert put == 0
    with db.connect() as conn:
        assert conn.execute("select version from partner_question").fetchall() == [(3,)]
        assert conn.execute(
            "select partner_version from decision where id = %s", (answered.id,)
        ).fetchone() == (2,)


def test_release_inbound_refuses_another_owner_and_keeps_everything(db, bridge, page):
    q = ask(db, page)
    _claim(db)
    with (
        pytest.raises(partner.BridgeHeld, match="'other'"),
        db.connect() as conn,
        conn.transaction(),
    ):
        partner.release_inbound(conn, "other", {}, {q.id: 7})
    assert _holder(db) == "qd"
    with db.connect() as conn:
        assert conn.execute(
            "select partner_version from decision where id = %s", (q.id,)
        ).fetchone() == (1,)


def test_release_inbound_takes_only_the_inbound_tables(db):
    _claim(db)
    with (
        pytest.raises(ValueError, match="decision"),
        db.connect() as conn,
        conn.transaction(),
    ):
        partner.release_inbound(conn, "qd", {"decision": [{}]}, {})


def test_the_outbound_bridge_cannot_go_back_while_inbound_is_held(db):
    _claim(db)
    with (
        pytest.raises(partner.InboundHeld) as refused,
        db.connect() as conn,
        conn.transaction(),
    ):
        partner.release_bridge(conn, "qd", [])
    assert refused.value.owner == "qd"
    with db.connect() as conn:
        assert partner.bridge_owner(conn) == "qd"
    assert _holder(db) == "qd"


def test_admin_shows_the_inbound_holder_and_refuses_a_blind_release(
    db, monkeypatch, capsys
):
    from ream import plugin_cli

    monkeypatch.setenv("DATABASE_URL", db.dsn)
    monkeypatch.setattr(plugin_cli, "entry_points", list)
    admin.main(["partner-bridge"])
    assert "inbound half: held by core" in capsys.readouterr().out
    _claim(db)
    admin.main(["partner-bridge"])
    out = capsys.readouterr().out
    assert "partner bridge: held by plug-in 'qd'" in out
    assert "inbound half: held by plug-in 'qd'" in out
    with pytest.raises(SystemExit, match="inbound half") as refused:
        admin.main(["partner-bridge", "--release"])
    assert "changed hands" not in str(refused.value)
    with db.connect() as conn:
        assert partner.bridge_owner(conn) == "qd"
    assert _holder(db) == "qd"


# --- core's stage -------------------------------------------------------------------------


def test_cores_stage_refuses_to_write_cores_outbox_once_inbound_is_claimed(
    db, bridge, page
):
    _claim(db)
    with sinks.isolated(*sinks._builtin_sinks()), pytest.raises(partner.InboundHeld):
        ask(db, page)
    assert _outbox(db) == []
    assert _count(db, "decision") == 0


def test_cores_stage_still_stands_down_quietly_where_the_owner_stages(db, bridge, page):
    staged = []
    owner = Sink(
        "quarterdeck",
        BRIDGE_KINDS,
        stage=lambda conn, event: staged.append(event.kind),
        plugin="qd",
    )
    _claim(db)
    with sinks.isolated(*sinks._builtin_sinks(), owner):
        q = ask(db, page)
        decisions.answer(db, q.id, letter="a", by="jeremy")
    assert staged == ["asked", "answered"]
    assert _outbox(db) == []


def test_cores_stage_passes_what_it_would_not_have_written(db, bridge, page):
    # A question asked before the bridge was never announced: core's stage writes
    # nothing for it, so there is nothing to refuse.
    with sinks.isolated():
        q = ask(db, page)
    _claim(db)
    with sinks.isolated(*sinks._builtin_sinks()):
        decisions.answer(db, q.id, letter="a", by="jeremy")
    assert _row(db, q.id).answer == "a"
    assert _outbox(db) == []


def test_cores_stage_passes_with_no_bridge_configured(db, page):
    _claim(db)
    with sinks.isolated(*sinks._builtin_sinks()):
        q = ask(db, page)
    assert _row(db, q.id) is not None
    assert _outbox(db) == []


def test_only_the_inbound_claim_stops_cores_stage(db, bridge, page):
    # The outbound claim alone (step 9e-3) keeps core staging for an absent owner.
    with db.connect() as conn, conn.transaction():
        partner.claim_bridge(conn, "qd")
    with sinks.isolated(*sinks._builtin_sinks()):
        ask(db, page)
    assert [row[2] for row in _outbox(db)] == ["asked"]


# --- the partner host ---------------------------------------------------------------------


@pytest.fixture()
def plugin_route(monkeypatch):
    """A plug-in `qd` with a route on the partner inbound host."""
    monkeypatch.setattr(plugin_api, "_ROUTERS", {})
    monkeypatch.setattr(plugin_api, "_failures", {})
    router = APIRouter()

    @router.post(partner_in.PATH)
    def receive():
        return {"result": "the plug-in's"}

    with settings.override(partner_inbound_host=HOST):
        plugin_api.PluginAPI("qd").router(router, HOST)
        yield


def _on_host(app_):
    return TestClient(app_, base_url=f"http://{HOST}", raise_server_exceptions=False)


def test_a_plugin_may_mount_on_the_partner_host(plugin_route):
    assert plugin_api.router_for(HOST)[0] == "qd"
    assert plugin_api.router_for("other.example") is None


def test_the_partner_host_is_cores_until_the_key_names_the_plugin(
    db, inbound, partner_key, plugin_route
):
    http = _on_host(create_app(db, inbound))
    event = make("asked", "qd:q1", 1, asked_body())
    r = post(http, partner_key, event)
    assert r.status_code == 200 and r.json() == {"result": "applied"}
    _claim(db)
    r = post(http, partner_key, make("asked", "qd:q2", 1, asked_body()))
    assert r.status_code == 200 and r.json() == {"result": "the plug-in's"}
    assert r.headers["x-content-type-options"] == "nosniff"


def test_the_partner_host_stays_cores_when_the_key_names_another_plugin(
    db, inbound, partner_key, plugin_route
):
    _claim(db, "other")
    r = post(
        _on_host(create_app(db, inbound)),
        partner_key,
        make("asked", "qd:q1", 1, asked_body()),
    )
    assert r.status_code == 503 and r.json() == {"error": "stood_down"}


def test_without_cores_endpoint_an_unclaimed_plugin_route_is_not_served(
    db, partner_key, plugin_route
):
    # The inbound host is set but core's endpoint was not built: the route on that
    # host is still served only once the key names its plug-in, never by default.
    http = _on_host(create_app(db))
    event = make("asked", "qd:q1", 1, asked_body())
    assert post(http, partner_key, event).status_code == 404
    _claim(db)
    assert post(http, partner_key, event).json() == {"result": "the plug-in's"}


def test_the_holder_is_read_on_every_request(db, inbound, partner_key, plugin_route):
    http = _on_host(create_app(db, inbound))
    _claim(db)
    assert post(http, partner_key, make("asked", "qd:q1", 1, asked_body())).json() == {
        "result": "the plug-in's"
    }
    with db.connect() as conn, conn.transaction():
        partner.release_inbound(conn, "qd", {}, {})
    r = post(http, partner_key, make("asked", "qd:q1", 1, asked_body()))
    assert r.json() == {"result": "applied"}


def test_an_unreadable_holder_is_503_never_a_guess(
    db, inbound, partner_key, plugin_route, monkeypatch
):
    def down(conn):
        raise RuntimeError("database away")

    http = _on_host(create_app(db, inbound))
    monkeypatch.setattr(partner, "inbound_owner", down)
    r = post(http, partner_key, make("asked", "qd:q1", 1, asked_body()))
    assert r.status_code == 503 and r.headers["Retry-After"] == "30"
    assert "database away" not in r.text


# --- the command-line purge ---------------------------------------------------------------


def test_purge_deleted_loads_the_plugins_but_not_on_a_dry_run(db, monkeypatch, capsys):
    loads = []
    monkeypatch.setattr(admin, "_database_url", lambda: db.dsn)
    monkeypatch.setattr(plugin_api, "load", lambda: loads.append(1) or [])
    admin.main(["purge-deleted", "--dry-run"])
    assert loads == []
    admin.main(["purge-deleted"])
    assert loads == [1]
    assert "0 destroyed" in capsys.readouterr().out


def test_purge_deleted_stops_on_a_plugin_that_cannot_load(db, monkeypatch):
    def broken():
        raise plugin_api.PluginError("plug-in 'qd' failed to register: RuntimeError")

    monkeypatch.setattr(admin, "_database_url", lambda: db.dsn)
    monkeypatch.setattr(plugin_api, "load", broken)
    with pytest.raises(SystemExit, match="'qd' failed to register"):
        admin.main(["purge-deleted"])
