"""Breaker state and plug-in failure counts shared across processes (core-and-plugins
design 4.5; slice 7b part 5). Two `Database` objects on one dsn stand for two worker
processes: neither shares memory with the other, only the tables."""

from datetime import UTC, datetime, timedelta

import pytest

from ream import plugin_api, sinks
from ream.db import Database

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture()
def two(db, dsn, monkeypatch):
    other = Database(dsn)
    monkeypatch.setattr(plugin_api, "_failures", {})
    with sinks.isolated():
        yield db, other
    sinks.bind(None)
    other.close()


def _fail(name, n, at=T0):
    for i in range(n):
        sinks.breaker_record(name, False, at, key=f"row{i}")


def test_failures_in_two_processes_open_one_breaker(two):
    a, b = two
    sinks.bind(a)
    _fail("s", sinks.BREAKER_THRESHOLD - 2)
    sinks.bind(b)  # the other worker process
    sinks.breaker_record("s", False, T0, key="rowx")
    assert sinks.breaker_peek("s", T0)
    sinks.breaker_record("s", False, T0, key="rowy")
    sinks.bind(a)
    soon = T0 + timedelta(seconds=1)
    assert not sinks.breaker_peek("s", soon)
    assert "s" in sinks.open_breakers(soon)
    sinks.bind(None)
    assert sinks.open_breakers(soon) == {}  # nothing leaked into this process's memory


def test_only_one_process_gets_the_trial(two):
    a, b = two
    sinks.bind(a)
    _fail("s", sinks.BREAKER_THRESHOLD)
    later = T0 + timedelta(seconds=sinks.BREAKER_COOLDOWN_SECONDS + 1)
    assert sinks.breaker_reserve("s", later)
    sinks.bind(b)
    assert not sinks.breaker_reserve("s", later)
    sinks.breaker_record("s", True, later, started=later)  # the trial worked
    sinks.bind(a)
    assert sinks.breaker_reserve("s", later)
    assert sinks.open_breakers(later) == {}


def test_one_poison_row_across_processes_does_not_open_it(two):
    a, b = two
    for n in range(sinks.BREAKER_THRESHOLD + 2):
        sinks.bind(a if n % 2 else b)
        sinks.breaker_record("s", False, T0, key="same-row")
    assert sinks.breaker_peek("s", T0)


def test_an_old_success_is_ignored_across_processes(two):
    a, b = two
    sinks.bind(a)
    _fail("s", sinks.BREAKER_THRESHOLD, at=T0)
    sinks.bind(b)
    sinks.breaker_record(
        "s", True, T0 + timedelta(seconds=2), started=T0 - timedelta(1)
    )
    assert not sinks.breaker_peek("s", T0 + timedelta(seconds=1))


def test_plugin_failure_counts_add_up_across_processes(two):
    a, b = two
    sinks.bind(a)
    plugin_api.count_failure("toy", "sink")
    plugin_api.count_failure("toy", "sink")
    plugin_api.failure_counts()  # waits for the stores
    sinks.bind(b)
    plugin_api.count_failure("toy", "sink")
    plugin_api.count_failure("toy", "scanner")
    assert plugin_api.failure_counts() == {"toy:scanner": 1, "toy:sink": 3}
    plugin_api._failures.clear()  # a fresh process: memory empty, the total remains
    assert plugin_api.failure_counts()["toy:sink"] == 3
    assert sinks.status(b)["plugin_failures"]["toy:sink"] == 3


def test_an_unreachable_database_falls_back_to_memory(two):
    class Broken:
        def connect(self):
            raise OSError("down")

    sinks.bind(Broken())
    _fail("s", sinks.BREAKER_THRESHOLD)
    assert not sinks.breaker_peek("s", T0 + timedelta(seconds=1))
    plugin_api.count_failure("toy", "sink")
    assert plugin_api.failure_counts() == {"toy:sink": 1}


def test_start_binds_and_stop_unbinds(db):
    try:
        sinks.start(db)
        assert sinks.shared() is db
    finally:
        sinks.stop()
    assert sinks.shared() is None
