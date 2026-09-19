"""A held request must end when the process is asked to stop.

Measured on 19 Sep 2026, and the reason this file exists: every deploy that
evening silently broke every waiting agent. The pod rolls, the old process is
asked to stop, a request sleeping out a 280-second hold does not finish, and
the platform kills it 30 seconds later. The client never sees a clean end — it
sees a request that never answers, and the client of the day called that
silence. Two seats logged the identical signature within a minute of a roll
while the same service answered wait=5, 30, 70, 110 and 180 exactly on time.

The failure is therefore not "long holds do not work". It is "a hold does not
survive a deploy, and does not fail honestly either". A test that only proved
long holds work would have passed throughout.
"""

import signal
import threading
import time

import pytest

from artifacts import events, serve


@pytest.fixture(autouse=True)
def _no_shutdown_leaks():
    """A set flag leaking out of one test would make every later wait return
    instantly and prove nothing."""
    events.clear_shutdown()
    yield
    events.clear_shutdown()


def test_a_hold_ends_the_moment_shutdown_begins(db):
    """Not "ends eventually": the answer has to go out while the process can
    still send it."""
    started = threading.Event()
    result = {}

    def hold():
        started.set()
        began = time.monotonic()
        result["rows"] = events.wait_for_agent(db, "ops-seat", since=0, seconds=30)
        result["elapsed"] = time.monotonic() - began

    waiter = threading.Thread(target=hold)
    waiter.start()
    assert started.wait(timeout=5)
    time.sleep(0.2)

    events.begin_shutdown()
    waiter.join(timeout=5)

    assert not waiter.is_alive(), "the hold outlived the shutdown"
    assert result["elapsed"] < 2, (
        f"took {result['elapsed']:.2f}s to end; a deploy would have killed it first"
    )
    assert result["rows"] == []


def test_the_answer_is_an_ordinary_empty_200_not_an_error(db):
    """An empty answer already means "nothing yet, ask again" — which is
    exactly what a client should do when the server is going away. Anything
    that looked like an error would train clients to treat a routine deploy as
    a failure."""
    events.begin_shutdown()

    rows = events.wait_for_agent(db, "ops-seat", since=0, seconds=30)

    assert rows == []


def test_a_hold_does_not_even_start_once_shutdown_has_begun(db):
    events.begin_shutdown()
    began = time.monotonic()

    events.wait_for_agent(db, "ops-seat", since=0, seconds=30)

    assert time.monotonic() - began < 2


def test_rows_that_landed_are_still_returned_at_shutdown(db, monkeypatch):
    """One last look before giving up: an event that arrives in the same moment
    as the shutdown must not be thrown away, or a deploy could lose exactly the
    answer an agent was waiting for."""
    row = "a row that landed at the last moment"

    def looks_empty_until_shutdown(*_args, **_kwargs):
        return [row] if events._shutdown.is_set() else []

    monkeypatch.setattr(events, "list_for_agent", looks_empty_until_shutdown)
    started = threading.Event()
    result = {}

    def hold():
        started.set()
        result["rows"] = events.wait_for_agent(db, "ops-seat", since=0, seconds=30)

    waiter = threading.Thread(target=hold)
    waiter.start()
    assert started.wait(timeout=5)
    time.sleep(0.2)

    events.begin_shutdown()
    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert result["rows"] == [row]


def test_normal_waiting_is_unchanged_when_nothing_is_shutting_down(db):
    began = time.monotonic()

    rows = events.wait_for_agent(db, "ops-seat", since=0, seconds=1)

    elapsed = time.monotonic() - began
    assert rows == []
    assert 0.9 < elapsed < 3, f"a 1s hold took {elapsed:.2f}s"


# ------------------------------------------------------------ the signal wiring


def test_sigterm_is_wired_to_end_held_requests(monkeypatch):
    """The wiring, without sending a real signal: install the handlers onto a
    stand-in app, then call the SIGTERM handler the way the kernel would."""
    installed = {}

    class FakeApp:
        def add_event_handler(self, name, fn):
            installed[name] = fn

    previous_called = []

    def previous(signum, frame):
        previous_called.append(signum)

    monkeypatch.setattr(signal, "getsignal", lambda _sig: previous)
    captured = {}
    monkeypatch.setattr(signal, "signal", lambda sig, fn: captured.__setitem__(sig, fn))

    serve._arm_shutdown_signals(FakeApp())
    assert "startup" in installed, "the handler must be installed at startup"
    installed["startup"]()

    assert signal.SIGTERM in captured and signal.SIGINT in captured

    events.clear_shutdown()
    captured[signal.SIGTERM](signal.SIGTERM, None)

    assert events._shutdown.is_set(), "SIGTERM must end held requests"
    assert previous_called == [signal.SIGTERM], (
        "uvicorn's own handler must still run, or graceful shutdown never happens"
    )
    events.clear_shutdown()


def test_the_handler_is_installed_at_startup_not_at_import(monkeypatch):
    """uvicorn installs its own signal handlers before lifespan startup, so
    installing ours any earlier would simply be overwritten."""
    installed = {}

    class FakeApp:
        def add_event_handler(self, name, fn):
            installed[name] = fn

    calls = []
    monkeypatch.setattr(signal, "signal", lambda sig, fn: calls.append(sig))
    monkeypatch.setattr(signal, "getsignal", lambda _sig: None)

    serve._arm_shutdown_signals(FakeApp())

    assert calls == [], "nothing may be installed before startup runs"
    installed["startup"]()
    assert calls, "startup must install them"
