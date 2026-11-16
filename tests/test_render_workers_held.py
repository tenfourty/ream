"""Every worker a page starts is held at its start (`rendercore._Offline`), in a
screenshot (`rendercore.shoot`) and in a render (`rendercore.screenshot`), so it
never runs a line and never makes a request: a shared worker, a classic dedicated
worker, a module worker and a service worker, each started for real.

Why a served page. As shipped, the page is a local file, and from a file none of
these four starts at all (a file's origin is opaque: a shared or module worker
needs a script of its own origin, and a service worker a secure one), so a test
that loads one as a file reaches the listener zero times whether the hold works
or not. Here the page and the workers' scripts are served by a listener on
127.0.0.1, so each worker really starts (runcove-5t3oj.142).

What is turned off, in both arms, so that the hold is the one thing tested:
  * the browser's own network block (`_NO_REACH_FLAGS`, emptied): its proxy would
    stop the served page itself, and the workers with it;
  * the page's offline emulation (`Network.emulateNetworkConditions`, not sent):
    it too would stop the served page; it never reached a worker anyway;
  * the request filter (`_Offline._decide`, which lets every request go on): as
    shipped it refuses the workers' scripts (every URL but the page's), and a
    worker whose script is refused never starts. It also sees a dedicated
    worker's own requests: with the hold off and the filter on, the module
    worker reached nothing (headless shell 1217), so with the filter on the
    module worker's case would not test the hold. It does not see a shared or a
    service worker's requests: those reached the listener with the filter on.

Then each kind is run twice on each path: once with the hold undone (`_Open`:
the shipped hold, then the held target let go, so the two arms differ by that
alone), where the worker's request must reach the listener, so the probe is known
to fire; and once with the hold as shipped (`_Held`), where a target of that kind
must have been attached and held, and its request must reach nothing.

The page's settle. A render lets the page settle in its own (virtual) time
(`rendercore._load`). Under it, full Chromium (Chrome for Testing 147, 16 Nov
2026) started no dedicated worker at all, classic or module, even with the hold
off: no target, no request. So both arms settle in real time instead (`_settle`:
the same navigation and load, then a real wait), where every kind fired with the
hold off on Chromium 147 and the headless shell 1217. The held arm also runs with
the shipped settle, where its request must reach nothing as well; that arm does
NOT prove the hold (the worker may never have started), only real_time does.

The shipped settle's control (`test_a_classic_worker_under_the_shipped_settle`):
a classic `new Worker(url)` with nothing blocking it, under the shipped settle. If
it starts and reaches the listener, the shipped "held" case for it means
something. If it never starts, the control SKIPS saying so and naming the browser
version it saw, so that the shipped held case is known to prove nothing there. If
it starts but its request does not land, it SKIPS too, naming the browser: the
shipped settle runs the page's own time to a budget and then ends the run, and
nothing in it waits for a worker's request, so a started worker's request may or
may not land first (Chromium 156, CI run 61854). It never fails: started and
reached nothing is a race of the settle, not a broken probe.

Untested on purpose, preconnect and dns-prefetch, because no control can show
them fire (a test that cannot fail proves nothing): <link rel=preconnect> opened
no connection to the listener even with every block off and the page served
(headless shell 1217 and Chromium 147), and <link rel=dns-prefetch> is a name
lookup this machine's resolver answers, which no listener on a port can see.
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_shots import needs_browser

from ream import rendercore, renderwire

JS = "text/javascript"

# Each kind: the page's script starting it, the worker's script (served at
# `script`), the target type the browser attaches it as, and the request it makes
# when it runs.
KINDS = {
    "shared_worker": {
        "page": "new SharedWorker('/shared.js')",
        "script": "/shared.js",
        "body": "fetch('/ping/shared').catch(()=>0)",
        "type": "shared_worker",
        "ping": "/ping/shared",
    },
    "classic_worker": {
        "page": "new Worker('/classic.js')",
        "script": "/classic.js",
        "body": "fetch('/ping/classic').catch(()=>0)",
        "type": "worker",
        "ping": "/ping/classic",
    },
    "module_worker": {
        "page": "new Worker('/module.js',{type:'module'})",
        "script": "/module.js",
        # A static import is a request too, made before the module's first line.
        "body": "import '/ping/module-import.js'; fetch('/ping/module').catch(()=>0)",
        "type": "worker",
        "ping": "/ping/module",
        "also": "/ping/module-import.js",
    },
    "service_worker": {
        "page": "navigator.serviceWorker.register('/sw.js').catch(()=>0)",
        "script": "/sw.js",
        "body": "fetch('/ping/service').catch(()=>0)",
        "type": "service_worker",
        "ping": "/ping/service",
    },
}
PATHS = ("shot", "render")
RENDER_W, RENDER_H = 400, 300
# How long the page is let run, in real time, after its load.
REAL_SETTLE_SECONDS = 2.0


class _Listener:
    """An http server on 127.0.0.1 serving the page and the worker scripts, and
    noting the path of every request it is sent."""

    def __init__(self, kind: str):
        spec = KINDS[kind]
        files = {
            "/page.html": (
                "text/html",
                f"<!doctype html><p>workers</p><script>{spec['page']}</script>",
            ),
            spec["script"]: (JS, spec["body"]),
            "/ping/module-import.js": (JS, ""),
        }
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                found = files.get(self.path)
                if found is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                ctype, body = found
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body.encode())

            do_POST = do_GET

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.page_url = self.base + "/page.html"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=2)


class _NoOfflineEmulation:
    """The pipe, but `Network.emulateNetworkConditions` is not sent (see the top)."""

    def __init__(self, pipe):
        self._pipe = pipe

    def send(self, method, params=None, session=None, **kw):
        if method == "Network.emulateNetworkConditions":
            return None
        return self._pipe.send(method, params, session, **kw)


# Every target `_hold` was handed in one run, as (type, url), and those it held.
ATTACHED: list[tuple[str, str]] = []
HELD: list[tuple[str, str]] = []


def _target(params) -> tuple[str, str]:
    info = params.get("targetInfo") or {}
    return str(info.get("type")), str(info.get("url"))


class _Held(rendercore._Offline):
    """The hold as shipped (`_Offline._hold`, unchanged), with the page's offline
    emulation not sent and every request let go on by the filter (checked: an
    error answer fails the run; stricter than the shipped filter, which sends
    its answers unchecked)."""

    def _decide(self, params, session):
        self.pipe.send(
            "Fetch.continueRequest",
            {"requestId": params["requestId"]},
            session,
            checked=True,
        )

    def _hold(self, params):
        ATTACHED.append(_target(params))
        before = self.held
        real = self.pipe
        self.pipe = _NoOfflineEmulation(real)
        try:
            super()._hold(params)
        finally:
            self.pipe = real
        if self.held > before:
            HELD.append(_target(params))


# Every target the control let go after the shipped hold, as (type, url).
RELEASED: list[tuple[str, str]] = []


class _Open(_Held):
    """`_Held` with the hold undone: each target the shipped `_hold` held is let go
    at once. Everything else is `_Held`'s (the page's interception, the filter that
    lets every request on, no offline emulation), so the two arms differ by the
    release alone, and a worker's request takes the same way in both. For the
    controls only."""

    def _hold(self, params):
        before = self.held
        super()._hold(params)
        if self.held > before:
            HELD.pop()
            RELEASED.append(_target(params))
            self.pipe.send(
                "Runtime.runIfWaitingForDebugger",
                session=params["sessionId"],
                checked=True,
            )


def _settle(pipe, session, page_url):
    """`rendercore._load` in real time: the same navigation and load, then the page
    is let run REAL_SETTLE_SECONDS while every event is read (and so every target
    it starts handed to `_hold`). No virtual time is set, so the event waited for
    never comes and the wait is the whole of it."""
    navigated = pipe.call("Page.navigate", {"url": page_url}, session)
    assert not navigated.get("errorText"), navigated
    pipe.wait_for(
        "Page.loadEventFired",
        session,
        until=min(
            pipe.deadline, time.monotonic() + rendercore.SHOT_SETTLE_REAL_SECONDS
        ),
    )
    pipe.wait_for(
        "Emulation.virtualTimeBudgetExpired",
        session,
        until=min(pipe.deadline, time.monotonic() + REAL_SETTLE_SECONDS),
    )


def _run(path: str, listener: _Listener, monkeypatch) -> bool:
    """One run of `path` (the real `shoot` or `screenshot`) with the page at the
    listener's address in place of the local file: True if it made a picture."""
    url = listener.page_url
    if path == "shot":
        shoot_in = rendercore._shoot_in
        monkeypatch.setattr(
            rendercore,
            "_shoot_in",
            lambda argv, workdir, _page, request: shoot_in(argv, workdir, url, request),
        )
        shot = rendercore.shoot(
            rendercore.browser_path(), "<p>unused</p>", renderwire.shot_request()
        )
        return shot.outcome == "shot"
    render_run = rendercore._render_run

    def served(argv, *, page_url, **kw):
        return render_run(argv, page_url=url, **kw)

    monkeypatch.setattr(rendercore, "_render_run", served)
    png, _errors = rendercore.screenshot(
        rendercore.browser_path(), "<p>unused</p>", RENDER_W, RENDER_H
    )[:2]
    return png is not None


@pytest.fixture()
def no_network_block(monkeypatch):
    monkeypatch.setattr(rendercore, "_NO_REACH_FLAGS", [])
    ATTACHED.clear()
    HELD.clear()
    RELEASED.clear()


def _try(path, kind, monkeypatch, tries):
    """The listener's requests over up to `tries` runs (stopping at the first that
    reaches the worker's request), each run's picture asserted made."""
    spec = KINDS[kind]
    listener = _Listener(kind)
    try:
        for _ in range(tries):
            assert _run(path, listener, monkeypatch), f"{path} {kind}: no picture"
            time.sleep(0.5)  # a request still in flight as the browser exits
            if spec["ping"] in listener.seen:
                break
    finally:
        listener.close()
    # The page itself was served: the run talked to the listener at all.
    assert "/page.html" in listener.seen, listener.seen
    return listener.seen


@needs_browser
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_the_worker_runs_with_the_hold_off(path, kind, no_network_block, monkeypatch):
    monkeypatch.setattr(rendercore, "_Offline", _Open)
    monkeypatch.setattr(rendercore, "_load", _settle)
    seen = _try(path, kind, monkeypatch, tries=3)
    spec = KINDS[kind]
    assert spec["ping"] in seen, f"{path} {kind}: the worker never ran ({seen})"
    if "also" in spec:
        assert spec["also"] in seen, seen
    # The worker that ran is the one the shipped hold held and the control let go.
    mine = [(t, u) for t, u in RELEASED if u.endswith(spec["script"])]
    assert mine and all(t == spec["type"] for t, _ in mine), (ATTACHED, RELEASED)


@needs_browser
@pytest.mark.parametrize("settle", ["real_time", "shipped"])
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_the_worker_is_held_and_reaches_nothing(
    path, kind, settle, no_network_block, monkeypatch
):
    monkeypatch.setattr(rendercore, "_Offline", _Held)
    if settle == "real_time":
        monkeypatch.setattr(rendercore, "_load", _settle)
    seen = _try(path, kind, monkeypatch, tries=3)
    spec = KINDS[kind]
    reached = [p for p in seen if p.startswith("/ping/")]
    assert reached == [], f"{path} {kind} ({settle}): the held worker reached {reached}"
    # Only the real_time arm proves the hold. In the shipped arm the worker may
    # never have started (full Chromium starts no dedicated worker under the
    # render's virtual time), so reaching nothing there proves nothing of the
    # hold; it is kept only to show the shipped settle lets nothing out either.
    if settle == "real_time":
        # The worker did start: a target of its kind, at its script, was held.
        mine = [(t, u) for t, u in HELD if u.endswith(spec["script"])]
        assert mine and all(t == spec["type"] for t, _ in mine), (ATTACHED, HELD)
    # Nothing but the page and its frames was ever let go.
    let_go = [t for t in ATTACHED if t not in HELD]
    assert {t for t, _ in let_go} <= {"page", "iframe"}, let_go


def _browser_version() -> str:
    import subprocess

    try:
        out = subprocess.run(  # noqa: S603 - the browser the tests already run
            [rendercore.browser_path(), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return (out.stdout or out.stderr).strip() or "unknown"
    except Exception as exc:  # noqa: BLE001 - only to name the browser in a skip
        return f"unknown ({type(exc).__name__})"


@needs_browser
@pytest.mark.parametrize("path", PATHS)
def test_a_classic_worker_under_the_shipped_settle(path, no_network_block, monkeypatch):
    """Control for the shipped arm of the held cases: with the hold undone and the
    shipped (virtual-time) settle, does a classic dedicated worker start and
    reach the listener? Passes if so. Skips, naming the browser, if it never
    started, or if it started but its request did not land (the shipped settle
    does not wait for a worker's request); the held case's shipped arm proves
    nothing then."""
    monkeypatch.setattr(rendercore, "_Offline", _Open)
    seen = _try(path, "classic_worker", monkeypatch, tries=3)
    spec = KINDS["classic_worker"]
    started = [(t, u) for t, u in RELEASED if u.endswith(spec["script"])]
    if spec["ping"] in seen:
        assert started and all(t == spec["type"] for t, _ in started), (
            ATTACHED,
            RELEASED,
        )
        return
    if started:
        pytest.skip(
            f"{path}: under the shipped settle a classic worker started ({started}) "
            f"but its request did not land before the run ended on "
            f"{_browser_version()} ({seen}): the shipped settle does not wait for a "
            "worker's request, so the shipped held arm for classic_worker proves "
            "nothing; real_time proves it"
        )
    pytest.skip(
        f"{path}: under the shipped settle a classic dedicated worker never starts "
        f"on {_browser_version()} (no target at {spec['script']}: {ATTACHED}), so the "
        "shipped held arm for classic_worker proves nothing; real_time proves it"
    )
