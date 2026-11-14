"""A page reaches nothing but itself, in a screenshot (`rendercore.shoot`) and in a
render (`rendercore.screenshot`: thumbnails, preview, renderd's /v1/render): no
host, no other local file.

Each way out is tried twice on each path against a listener on this machine's
loopback (TCP and UDP on one port): once with the block off (the flags
`_NO_REACH_FLAGS` and the request filter `_Offline` disabled), which must reach
it, so the probe is known to fire; and once as shipped, which must reach it zero
times.
"""

import select
import socket
import threading
import time

import pytest
from test_shots import needs_browser, pixels

from ream import devtools, rendercore, renderwire


class _Listener:
    """TCP and UDP on 127.0.0.1 at one port; counts every connection and datagram."""

    def __init__(self):
        self.tcp = socket.create_server(("127.0.0.1", 0))
        self.port = self.tcp.getsockname()[1]
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", self.port))
        self.seen: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            ready, _, _ = select.select([self.tcp, self.udp], [], [], 0.05)
            for sock in ready:
                if sock is self.udp:
                    self.udp.recvfrom(4096)
                    self.seen.append("<udp>")
                    continue
                conn, _ = sock.accept()
                conn.settimeout(0.5)
                try:
                    first = conn.recv(512).split(b"\r\n", 1)[0]
                except OSError:
                    first = b""
                self.seen.append(first.decode("latin-1") or "<no bytes>")
                conn.close()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.tcp.close()
        self.udp.close()


def _worker(code: str) -> str:
    return f'new Worker(URL.createObjectURL(new Blob(["{code}"])));'


# Each way out, as markup and script, with {b} the listener's http base and {p} its port.
WAYS = {
    "fetch": "<script>fetch('{b}/fetch').catch(()=>0)</script>",
    "xhr": "<script>var x=new XMLHttpRequest();x.open('GET','{b}/xhr');x.send()</script>",
    "img": '<img src="{b}/img">',
    "script": '<script src="{b}/script"></script>',
    "stylesheet": '<link rel=stylesheet href="{b}/css">',
    "css_url": "<div style=\"background:url('{b}/bg')\">x</div>",
    "iframe": '<iframe src="{b}/iframe"></iframe>',
    "popup": "<script>window.open('{b}/popup')</script>",
    "beacon": "<script>navigator.sendBeacon('{b}/beacon','x')</script>",
    "websocket": "<script>new WebSocket('ws://127.0.0.1:{p}/ws')</script>",
    "meta_refresh": '<meta http-equiv=refresh content="0;url={b}/refresh">',
    # Not here: <link rel=preconnect>, which opened no connection even with the
    # block off (headless shell 1217), so no control could show it fire.
    "prefetch": '<link rel=prefetch href="{b}/prefetch">',
    "worker_fetch": "<script>"
    + _worker("fetch('{b}/wfetch').catch(()=>0)")
    + "</script>",
    "worker_websocket": "<script>"
    + _worker("new WebSocket('ws://127.0.0.1:{p}/wws')")
    + "</script>",
    "webtransport": "<script>try{{new WebTransport('https://127.0.0.1:{p}/wt')"
    ".ready.catch(()=>0)}}catch(e){{}}</script>",
    "webrtc": "<script>try{{var pc=new RTCPeerConnection({{iceServers:[{{urls:"
    "'stun:127.0.0.1:{p}'}}]}});pc.createDataChannel('x');pc.createOffer()"
    ".then(o=>pc.setLocalDescription(o))}}catch(e){{}}</script>",
}


class _Open(rendercore._Offline):
    """The request filter turned off: every request goes on, nothing is set
    offline, every target runs. For the controls only."""

    def _decide(self, params, session):
        self.pipe.send(
            "Fetch.continueRequest", {"requestId": params["requestId"]}, session
        )

    def _hold(self, params):
        child = params["sessionId"]
        self.attached[child] = str(params["targetInfo"].get("targetId"))
        self.pipe.send("Target.setAutoAttach", rendercore._AUTO_ATTACH, child)
        self.pipe.send("Runtime.runIfWaitingForDebugger", session=child)


@pytest.fixture()
def block_off(monkeypatch):
    monkeypatch.setattr(rendercore, "_NO_REACH_FLAGS", [])
    monkeypatch.setattr(rendercore, "_Offline", _Open)


PATHS = ("shot", "render")
RENDER_W, RENDER_H = 400, 300


def _picture(path: str, page: str, element: str | None = None) -> bytes | None:
    """The PNG one run of `path` makes of `page`, None if it made none."""
    if path == "shot":
        request = renderwire.shot_request(element=element)
        shot = rendercore.shoot(rendercore.browser_path(), page, request)
        return shot.png if shot.outcome == "shot" else None
    report = rendercore.screenshot(rendercore.browser_path(), page, RENDER_W, RENDER_H)
    return report[0]


def _try(path: str, way: str, tries: int) -> list[str]:
    listener = _Listener()
    try:
        base = f"http://127.0.0.1:{listener.port}"
        page = "<!doctype html><p>out</p>" + WAYS[way].format(b=base, p=listener.port)
        for _ in range(tries):
            png = _picture(path, page)
            # A page that navigates itself away may leave nothing to shoot.
            if way != "meta_refresh":
                assert png is not None, f"{path}: no picture"
            time.sleep(0.5)  # a connection still in flight as the browser exits
            if listener.seen:
                break
    finally:
        listener.close()
    return listener.seen


@needs_browser
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("way", sorted(WAYS))
def test_the_probe_fires_with_the_block_off(path, way, block_off):
    # WebRTC's STUN went out in 13 of 16 renders without the flags: up to 3 tries.
    assert _try(path, way, 3), f"{path} {way}: the probe never reached the listener"


@needs_browser
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("way", sorted(WAYS))
def test_a_page_reaches_nothing(path, way):
    assert _try(path, way, 3) == [], f"{path} {way} reached the listener"


def _local_file_page(tmp_path) -> str:
    other = tmp_path / "other.html"
    other.write_text("<body style='margin:0;background:#0f0'>other</body>")
    return (
        "<!doctype html><body style='margin:0;background:#fff'>"
        f"<iframe id=frame src='{other.as_uri()}' "
        "style='border:0;width:200px;height:100px;display:block'></iframe></body>"
    )


def _frame_centre(path: str, page: str):
    # The shot is of the frame alone; the render is of the window, whose top left
    # the frame fills (200 by 100), so the same pixel is its centre in both.
    png = _picture(path, page, element="frame")
    assert png is not None, f"{path}: no picture"
    return pixels(png)[2][50][100]


@needs_browser
@pytest.mark.parametrize("path", PATHS)
def test_a_framed_local_file_shows_with_the_block_off(path, tmp_path, block_off):
    assert _frame_centre(path, _local_file_page(tmp_path)) == (0, 255, 0)


@needs_browser
@pytest.mark.parametrize("path", PATHS)
def test_a_framed_local_file_is_not_drawn(path, tmp_path):
    assert _frame_centre(path, _local_file_page(tmp_path)) != (0, 255, 0)


def test_the_flags_are_in_every_run():
    for flag in rendercore._NO_REACH_FLAGS:
        assert flag in rendercore._isolation_flags()
    assert devtools.Pipe  # the shot's pipe is the one the controls drive


def test_a_render_runs_on_the_pipe_and_never_by_its_own_screenshot(monkeypatch):
    """The render is driven over DevTools (`_render_run`), never by the browser's
    own --screenshot, which no request filter could hold."""
    seen = {}

    def capture(argv, **kw):
        seen["argv"] = [str(a) for a in argv]
        seen.update(kw)
        raise rendercore.subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(rendercore, "_render_run", capture)
    rendercore.screenshot("/bin/true", "<p>x</p>", RENDER_W, RENDER_H)
    argv = seen["argv"]
    assert "--remote-debugging-pipe" in argv
    assert not any(
        a.startswith(("--screenshot", "--virtual-time-budget")) for a in argv
    )
    assert not any(a.startswith("file:") for a in argv)
    assert seen["page_url"].startswith("file://")
