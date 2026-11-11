"""Plug-in API 1.2 (core-and-plugins design, step 9e prerequisite): an in-transaction
`stage` sink, start / stop hooks and plug-in-owned tables. A throwaway plug-in uses all
three; plug-ins load through a faked `_entry_points`, as in test_plugin_api.py."""

import pytest

from ream import events, plugin_api, sinks
from ream.plugin_api import PluginError

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


def _event(kind="asked"):
    return events.Event(
        kind=kind, artifact_id="art1", agent="seat-a", actor="owner", summary="s"
    )


def _emit(db, event):
    with db.connect() as conn, conn.transaction():
        return events.emit(conn, event)


def _rows(db):
    with db.connect() as conn:
        return conn.execute("select id, n from toy_queue order by id").fetchall()


TABLE = (
    "create table if not exists toy_queue ("
    "id text primary key, n integer not null default 0, at timestamptz, body jsonb)"
)


def test_a_plugin_stages_in_the_transaction_starts_stops_and_owns_a_table(
    world, db, fresh_schema
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

    out = _emit(db, _event())
    assert [r[0] for r in _rows(db)] == [out.event.id]
    # a stage sink gets no core outbox row
    with db.connect() as conn:
        assert conn.execute("select count(*) from outbox").fetchone()[0] == 0
    assert sinks.label_of("toy") == "Toy"

    plugin_api.stop_all()
    assert log[-1] == "stop"


def test_a_staging_failure_keeps_the_action_and_is_counted(world, db):
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
    out = _emit(db, _event())  # does not raise
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
