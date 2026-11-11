"""Plug-in API 1.2 (core-and-plugins design, step 9e prerequisite): an in-transaction
`stage` sink, start / stop hooks and plug-in-owned tables. A throwaway plug-in uses all
three; plug-ins load through a faked `_entry_points`, as in test_plugin_api.py."""

import threading

import pytest

from ream import db as dbmod
from ream import events, plugin_api, sinks, store
from ream.plugin_api import PluginError

PAGE = "<!doctype html><html><head><title>t</title></head><body>hi</body></html>"

pytestmark = pytest.mark.plugin_loader


class EP:
    def __init__(self, name, register):
        self.name = name
        self._register = register

    def load(self):
        return self._register


@pytest.fixture()
def world(monkeypatch):
    monkeypatch.setattr(plugin_api, "_failures", {})
    monkeypatch.setattr(plugin_api, "_loaded", [])
    monkeypatch.setattr(plugin_api, "_SCHEMA", [])
    monkeypatch.setattr(plugin_api, "_STARTS", [])
    monkeypatch.setattr(plugin_api, "_STOPS", [])

    def install(*eps):
        monkeypatch.setattr(plugin_api, "_entry_points", lambda: list(eps))

    with sinks.isolated():
        yield install


@pytest.fixture()
def page(db):
    return store.publish(db, "seat-a", "Plan", PAGE)


def _event(page, kind="asked"):
    return events.Event(
        kind=kind, artifact_id=page.id, agent="seat-a", actor="owner", summary="s"
    )


def _emit(db, event):
    with db.connect() as conn, conn.transaction():
        return events.emit(conn, event)


def _journal(db):
    with db.connect() as conn:
        return conn.execute("select count(*) from event").fetchone()[0]


def _rows(db):
    with db.connect() as conn:
        return conn.execute("select id, n from toy_queue order by id").fetchall()


J = "decision_answered"  # a journal kind: the action's own row is the journal's
TABLE = (
    "create table if not exists toy_queue ("
    "id text primary key, n integer not null default 0, at timestamptz, body jsonb)"
)


def test_a_plugin_stages_in_the_transaction_starts_stops_and_owns_a_table(
    world, db, page, fresh_schema
):
    log = []

    def stage(conn, event):
        conn.execute(
            "insert into toy_queue (id, n) values (%s, %s)", (event.id, len(log))
        )
        log.append("stage")

    def register(api):
        assert api.version == (1, 2)
        api.schema(
            [TABLE, "create index if not exists toy_queue_n_idx on toy_queue (n)"]
        )
        api.sink("toy", ("asked",), stage=stage, label="Toy")
        api.on_start(lambda database: log.append(("start", database is db)))
        api.on_stop(lambda: log.append("stop"))

    world(EP("toy", register))
    plugin_api.load()
    plugin_api.apply_schema(db)
    plugin_api.apply_schema(db)  # idempotent
    plugin_api.start_all(db)
    assert ("start", True) in log

    out = _emit(db, _event(page))
    assert [r[0] for r in _rows(db)] == [out.event.id]
    # a stage sink gets no core outbox row
    with db.connect() as conn:
        assert conn.execute("select count(*) from outbox").fetchone()[0] == 0
    assert sinks.label_of("toy") == "Toy"

    plugin_api.stop_all()
    assert log[-1] == "stop"


def test_a_staging_failure_keeps_the_action_and_is_counted(world, db, page):
    def stage(conn, event):
        conn.execute("insert into no_such_table values (1)")  # a database error

    def register(api):
        api.sink("toy", ("asked",), stage=stage)

    world(EP("toy", register))
    plugin_api.load()
    # Another sink's writes in the same transaction survive the failed stage.
    seen = []
    sinks.register(
        sinks.Sink("other", ("asked",), stage=lambda c, e: seen.append(e.id))
    )
    out = _emit(db, _event(page))  # does not raise
    assert seen == [out.event.id]
    assert plugin_api.failure_counts()["toy:stage"] == 1


def test_start_and_stop_failures_are_isolated(world, db):
    order = []

    def bad_start(_db):
        raise RuntimeError("secret detail")

    def make(tag):
        def register(api):
            api.on_start(bad_start if tag == "a" else lambda _db: order.append("b"))

            def stop():
                order.append(tag)
                if tag == "b":
                    raise RuntimeError("x")

            api.on_stop(stop)

        return register

    world(EP("a", make("a")), EP("b", make("b")))
    plugin_api.load()
    plugin_api.start_all(db)
    plugin_api.stop_all()
    assert order == ["b", "b", "a"]  # b started; stops in reverse, b's raise isolated
    counts = plugin_api.failure_counts()
    assert counts["a:start"] == 1 and counts["b:stop"] == 1


@pytest.mark.parametrize(
    "statement",
    [
        "create table if not exists other_t (id text)",  # wrong prefix
        "create table toy_t (id text)",  # no if not exists
        "drop table toy_t",
        "create table if not exists toy_t (id text); drop table decision",
        "create table if not exists toy_t as select * from decision",
        "create table if not exists toy_t (d text references decision (id))",
        "create table if not exists toy_t (id text) -- hi",
        "create index if not exists toy_i on decision (id)",  # not its own table
        "create table if not exists toy_t (id text default '%s')",
        # nothing may follow the column list
        "create table if not exists toy_t (id text) as table decision",
        "create table if not exists toy_t (id) as (select id from decision)",
        "create table if not exists toy_t (id) as values ('decision')",
        "create table if not exists toy_t (id) as with d as (select 1) select 1",
        "create table if not exists toy_t (id text) inherits (decision)",
        "create table if not exists toy_t (id text",  # never closes
        # SQLite's conflict clause that ends the whole transaction
        "create table if not exists toy_t (id text unique on conflict rollback)",
        # quoting the parenthesis scanner cannot follow
        "create table if not exists toy_t (id text default $$)$$, x text)",
        "create table if not exists toy_t (id text default E'\\'')",
        "create table if not exists toy_t (`id` text)",
        "create table if not exists toy_t ([id] text)",
    ],
)
def test_schema_statements_outside_the_rules_stop_registration(world, statement):
    def register(api):
        api.schema([statement])

    world(EP("toy", register))
    with pytest.raises(PluginError) as exc:
        plugin_api.load()
    assert "toy" in str(exc.value)
    assert "decision" not in str(exc.value)  # the text is never echoed


def test_sink_needs_exactly_one_of_deliver_and_stage(world):
    def neither(api):
        api.sink("toy", ("asked",))

    def both(api):
        api.sink("toy", ("asked",), lambda e: None, stage=lambda c, e: None)

    def after_commit(api):
        api.sink("toy", ("asked",), stage=lambda c, e: None, delivery="after_commit")

    for register in (neither, both, after_commit):
        world(EP("toy", register))
        with pytest.raises(PluginError):
            plugin_api.load()


def test_a_stage_that_swallows_its_own_database_error_keeps_the_action(
    world, db, page, fresh_schema
):
    """On Postgres a caught error still aborts the transaction: the savepoint must be
    rolled back to (and the failure counted), or RELEASE fails and the action is lost.
    SQLite aborts only the statement, so there the stage simply succeeded."""

    def stage(conn, event):
        conn.execute("insert into toy_queue (id) values (%s)", (event.id,))
        try:
            conn.execute("insert into toy_queue (id) values (%s)", (event.id,))
        except dbmod.DUPLICATE_KEY:
            pass  # swallowed: the plug-in "handled" it

    def register(api):
        api.schema([TABLE])
        api.sink("toy", (J,), stage=stage)

    world(EP("toy", register))
    plugin_api.load()
    plugin_api.apply_schema(db)
    seen = []
    sinks.register(sinks.Sink("other", (J,), stage=lambda c, e: seen.append(e.id)))
    out = _emit(db, _event(page, J))  # does not raise
    assert _journal(db) == 1  # the action committed
    assert seen == [out.event.id]  # and the sink after it ran
    counts = plugin_api.failure_counts()
    if db.sqlite:
        assert [r[0] for r in _rows(db)] == [out.event.id]
        assert "toy:stage" not in counts
    else:
        assert _rows(db) == []  # only the stage's own writes were undone
        assert counts["toy:stage"] == 1


def test_a_stage_that_ends_the_transaction_fails_the_whole_action(world, db, page):
    """A stage cannot be saved from ending the transaction itself: the action fails
    whole (nothing half-written, nothing written in autocommit after it)."""

    def stage(conn, event):
        conn.execute("rollback")

    def register(api):
        api.sink("toy", (J,), stage=stage)

    world(EP("toy", register))
    plugin_api.load()
    after = []
    sinks.register(sinks.Sink("later", (J,), stage=lambda c, e: after.append(1)))
    with pytest.raises(dbmod.TransactionLost):
        _emit(db, _event(page, J))
    assert _journal(db) == 0
    assert after == []
    assert plugin_api.failure_counts()["toy:stage"] == 1


def test_an_or_rollback_conflict_in_a_stage_fails_the_whole_action_on_sqlite(
    world, db, page, fresh_schema
):
    if not db.sqlite:
        pytest.skip("SQLite's own conflict clause")

    def stage(conn, event):
        conn.execute("insert into toy_queue (id) values (%s)", (event.id,))
        try:
            conn.execute(
                "insert or rollback into toy_queue (id) values (%s)", (event.id,)
            )
        except dbmod.INTEGRITY:
            pass

    def register(api):
        api.schema([TABLE])
        api.sink("toy", (J,), stage=stage)

    world(EP("toy", register))
    plugin_api.load()
    plugin_api.apply_schema(db)
    with pytest.raises(dbmod.TransactionLost):
        _emit(db, _event(page, J))
    assert _journal(db) == 0
    assert _rows(db) == []


@pytest.mark.parametrize(
    ("plugin", "statements"),
    [
        # a prefix that covers a core table: the table, an index on it, a reference
        ("agent", ["create table if not exists agent_key (id text)"]),
        ("agent", ["create index if not exists agent_x_idx on agent_key (id)"]),
        (
            "agent",
            [
                "create table if not exists agent_mine (k text references agent_key (id))"
            ],
        ),
        ("service", ["create table if not exists service_meta (k text)"]),
        ("partner", ["create table if not exists partner_outbox (k text)"]),
        # its own prefix, but a table its own statements never create
        ("toy", ["create table if not exists toy_t (k text references toy_other)"]),
        ("toy", ["create index if not exists toy_i on toy_other (k)"]),
    ],
)
def test_a_schema_that_reaches_a_table_not_its_own_is_refused(
    world, db, plugin, statements
):
    def register(api):
        api.schema(statements)

    world(EP(plugin, register))
    plugin_api.load()
    with pytest.raises(PluginError) as exc:
        plugin_api.apply_schema(db)
    assert repr(plugin) in str(exc.value)
    assert "agent_key" not in str(exc.value) and "toy_other" not in str(exc.value)
    with db.connect() as conn:
        tables, _ = conn.catalog()
    assert not {"agent_mine", "toy_t"} & tables  # nothing ran


def test_one_plugins_prefix_may_not_cover_anothers(world):
    world(
        EP(
            "acme",
            lambda api: api.schema(["create table if not exists acme_t (k text)"]),
        ),
        EP(
            "acme-chat",
            lambda api: api.schema(["create table if not exists acme_chat_t (k text)"]),
        ),
    )
    with pytest.raises(PluginError) as exc:
        plugin_api.load()
    assert "'acme'" in str(exc.value) and "'acme-chat'" in str(exc.value)


def test_overlapping_plugins_that_declare_no_schema_still_load(world):
    world(EP("acme", lambda api: None), EP("acme-chat", lambda api: None))
    assert plugin_api.load() == ["acme", "acme-chat"]


def test_a_plugin_named_event_may_not_take_the_journal_sequence(world, db):
    if db.sqlite:
        pytest.skip("sequences are a Postgres relation")

    def register(api):
        api.schema(["create table if not exists event_seq_seq (k text)"])

    world(EP("event", register))
    plugin_api.load()
    with pytest.raises(PluginError):
        plugin_api.apply_schema(db)


def test_a_statement_the_database_refuses_names_the_plugin(world, db, fresh_schema):
    def register(api):
        api.schema(
            [
                "create table if not exists toy_t (id text)",
                "create index if not exists toy_i on toy_t (secretcol)",
            ]
        )

    world(EP("toy", register))
    plugin_api.load()
    with pytest.raises(PluginError) as exc:
        plugin_api.apply_schema(db)
    assert "'toy'" in str(exc.value)
    assert "secretcol" not in str(exc.value)
    with db.connect() as conn:
        assert "toy_t" not in conn.catalog()[0]  # the whole schema rolled back


def test_a_stuck_stop_hook_is_left_behind_and_counted(world):
    release = threading.Event()
    order = []

    def make(tag):
        def register(api):
            if tag == "a":
                api.on_stop(lambda: order.append("a"))
            else:
                api.on_stop(lambda: release.wait(5))

        return register

    world(EP("a", make("a")), EP("b", make("b")))
    plugin_api.load()
    try:
        plugin_api.stop_all(timeout=0.2)
    finally:
        release.set()
    assert order == ["a"]  # the stuck one did not keep the next from running
    assert plugin_api.failure_counts()["b:stop"] == 1
