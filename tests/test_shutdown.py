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

import asyncio
import signal
import threading
import time

import pytest
from fastapi.testclient import TestClient

from artifacts import events, serve
from artifacts.app import create_app


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


def _start_lifespan(app):
    """Drive the ASGI lifespan startup the way uvicorn does."""

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(_message):
        return None

    asyncio.run(app({"type": "lifespan"}, receive, send))


def test_sigterm_is_wired_to_end_held_requests(monkeypatch):
    """The wiring, without sending a real signal: arm a stand-in app, start its
    lifespan, then call the SIGTERM handler the way the kernel would."""
    seen = []

    async def inner(scope, _receive, _send):
        seen.append(scope["type"])

    previous_called = []

    def previous(signum, frame):
        previous_called.append(signum)

    monkeypatch.setattr(signal, "getsignal", lambda _sig: previous)
    captured = {}
    monkeypatch.setattr(signal, "signal", lambda sig, fn: captured.__setitem__(sig, fn))

    armed = serve._arm_shutdown_signals(inner)
    _start_lifespan(armed)

    assert seen == ["lifespan"], "the wrapper must still pass the scope through"
    assert signal.SIGTERM in captured and signal.SIGINT in captured

    events.clear_shutdown()
    captured[signal.SIGTERM](signal.SIGTERM, None)

    assert events._shutdown.is_set(), "SIGTERM must end held requests"
    assert previous_called == [signal.SIGTERM], (
        "uvicorn's own handler must still run, or graceful shutdown never happens"
    )
    events.clear_shutdown()


def test_nothing_is_installed_until_the_lifespan_starts(monkeypatch):
    """uvicorn installs its own signal handlers before the lifespan runs, so
    installing ours any earlier would simply be overwritten."""

    async def inner(_scope, _receive, _send):
        return None

    calls = []
    monkeypatch.setattr(signal, "signal", lambda sig, fn: calls.append(sig))
    monkeypatch.setattr(signal, "getsignal", lambda _sig: None)

    armed = serve._arm_shutdown_signals(inner)

    assert calls == [], "nothing may be installed before the lifespan starts"
    _start_lifespan(armed)
    assert calls, "the lifespan startup must install them"


def test_arming_works_on_what_create_app_actually_returns(db, monkeypatch):
    """The control that was missing, and the reason this file has a bug to fix.

    The first version armed the app with `add_event_handler`, which exists on a
    FastAPI object and not on the bare host-routing function `create_app`
    returns. The test passed anyway, because the stand-in it armed HAD that
    method: a fake that is kinder than the real thing proves nothing. So this
    one arms the real object and starts its real lifespan.
    """
    monkeypatch.setattr(signal, "signal", lambda _sig, _fn: None)
    monkeypatch.setattr(signal, "getsignal", lambda _sig: None)

    armed = serve._arm_shutdown_signals(create_app(db))
    _start_lifespan(armed)  # would raise AttributeError against the first version

    response = TestClient(armed, base_url="https://artifacts.tenfourty.site").get("/healthz")
    assert response.status_code == 200, "arming must not change how the app routes"


# ----------------------------------------------------- the look is bounded too


def test_a_dsn_gets_a_connect_timeout_and_keeps_one_it_already_has():
    """A connect that can block forever turns a bounded wait into an unbounded
    one: `wait_for_agent` opens one connection per look, twice a second, and a
    Postgres failover mid-hold would otherwise park the loop past a deadline it
    has already promised the caller."""
    from artifacts.db import Database

    assert "connect_timeout=5" in Database("postgresql:///artifacts").dsn
    assert "?connect_timeout=5" in Database("postgresql:///artifacts").dsn
    assert "&connect_timeout=5" in Database("postgresql:///a?sslmode=require").dsn

    already = Database("postgresql:///a?connect_timeout=30").dsn
    assert already.count("connect_timeout") == 1, "a stated timeout must win"
    assert "connect_timeout=30" in already


def test_no_look_starts_after_the_deadline_has_passed(db, monkeypatch):
    """A look is a round trip, not a free operation. One started after the
    deadline spends time the caller never agreed to."""
    looks = []

    def counting_look(*_args, **_kwargs):
        looks.append(time.monotonic())
        return []

    monkeypatch.setattr(events, "list_for_agent", counting_look)

    began = time.monotonic()
    rows = events.wait_for_agent(db, "ops-seat", since=0, seconds=0.0)
    elapsed = time.monotonic() - began

    assert rows == []
    assert elapsed < 0.5
    assert len(looks) <= 1, f"{len(looks)} looks for a hold that had already expired"


def test_the_shutdown_answer_is_the_feed_s_normal_empty_shape(db, monkeypatch):
    """What a client actually receives matters as much as when it receives it.

    Measured 19 Sep 2026: the server half worked -- a held request ended at the
    roll in 110.8 s instead of hanging to the client's 300 s ceiling -- and a
    waiter still died, because the body it got back was not JSON and it treated
    that as fatal. Whatever this service says on its way out has to be an
    ORDINARY quiet round in the feed's own shape: same content type, an empty
    list, no rows. Anything else asks every client, including ones nobody here
    wrote, to special-case a deploy.
    """
    from fastapi.testclient import TestClient

    from artifacts import keys
    from artifacts.app import create_gallery_app

    token, _ = keys.mint(db, agent="leaving-seat")
    client = TestClient(create_gallery_app(db))

    events.begin_shutdown()
    response = client.get(
        "/api/v1/events?since=0&wait=30", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == []
