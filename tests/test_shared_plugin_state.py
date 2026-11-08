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


def test_stop_unbinds(db):
    sinks.bind(db)
    assert sinks.shared() is db
    sinks.stop()
    assert sinks.shared() is None


def _writer_threads():
    import threading

    return [t for t in threading.enumerate() if t.name == "plugin-failure-writer"]


class Counting:
    """A database that counts the connections opened and delegates to a real one."""

    def __init__(self, real):
        self.real = real
        self.opened = 0

    def connect(self):
        self.opened += 1
        return self.real.connect()


def test_a_burst_of_failures_uses_one_writer_and_few_connections(two):
    a, _ = two
    counting = Counting(a)
    sinks.bind(counting)
    before = len(_writer_threads())
    for _ in range(500):
        plugin_api.count_failure("toy", "route")
    assert len(_writer_threads()) <= before + 1
    assert plugin_api.failure_counts()["toy:route"] == 500
    # 500 failures, a handful of flushes (plus the one connection the read took).
    assert counting.opened < 50
    sinks.stop()
    assert _writer_threads() == []


def test_a_down_database_keeps_counts_in_memory_and_retries(two, monkeypatch):
    a, _ = two
    monkeypatch.setattr(plugin_api, "_RETRY_SECONDS", 0.05)

    class Flaky:
        down = True

        def connect(self):
            if self.down:
                raise OSError("down")
            return a.connect()

    flaky = Flaky()
    sinks.bind(flaky)
    for _ in range(3):
        plugin_api.count_failure("toy", "sink")  # never raises
    assert plugin_api.failure_counts() == {"toy:sink": 3}  # memory
    flaky.down = False
    import time

    deadline = time.monotonic() + 5
    while (plugin_api._pending or plugin_api._writing) and time.monotonic() < deadline:
        time.sleep(0.02)
    sinks.bind(a)
    assert plugin_api.failure_counts() == {"toy:sink": 3}  # now stored, not doubled
    with a.connect() as conn:
        assert conn.execute("select failures from plugin_failure").fetchone()[0] == 3


def test_pending_adds_to_the_stored_total_of_other_processes(two):
    a, b = two
    sinks.bind(a)
    plugin_api.count_failure("toy", "sink")
    plugin_api.failure_counts()
    sinks.bind(b)
    plugin_api.count_failure("toy", "sink")
    assert plugin_api.failure_counts() == {"toy:sink": 2}


def test_peek_and_open_breakers_take_no_write_transaction(two):
    a, _ = two

    class NoTx:
        def __init__(self, conn):
            self._conn = conn

        def transaction(self):
            raise AssertionError("a read opened a transaction")

        def __getattr__(self, name):
            return getattr(self._conn, name)

    class Spy:
        def connect(self):
            from contextlib import contextmanager

            @contextmanager
            def cm():
                with a.connect() as conn:
                    yield NoTx(conn)

            return cm()

    sinks.bind(a)
    _fail("s", sinks.BREAKER_THRESHOLD)
    sinks.bind(Spy())
    assert not sinks.breaker_peek("s", T0 + timedelta(seconds=1))
    assert "s" in sinks.open_breakers(T0 + timedelta(seconds=1))
