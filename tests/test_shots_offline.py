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

from ream import devtools, rendercore, renderwire, thumbs


class _Listener:
    """TCP and UDP on 127.0.0.1 at one port, and TCP on ::1 at the same port where
    it can be, so a `localhost` that resolves to either is caught; counts every
    connection and datagram."""

    def __init__(self):
        self.tcp = socket.create_server(("127.0.0.1", 0))
        self.port = self.tcp.getsockname()[1]
        self.tcp6 = []
        try:
            self.tcp6.append(
                socket.create_server(("::1", self.port), family=socket.AF_INET6)
            )
        except OSError:
            pass  # no IPv6 loopback here; 127.0.0.1 is still caught
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", self.port))
        self.seen: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            ready, _, _ = select.select([self.tcp, *self.tcp6, self.udp], [], [], 0.05)
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
        for sock in [self.tcp, *self.tcp6, self.udp]:
            sock.close()


def _worker(code: str) -> str:
    return f'new Worker(URL.createObjectURL(new Blob(["{code}"])));'


# Each way out, as markup and script, with {b} the listener's http base, {h} its
# host (by address and by name) and {p} its port.
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
    "websocket": "<script>new WebSocket('ws://{h}:{p}/ws')</script>",
    "meta_refresh": '<meta http-equiv=refresh content="0;url={b}/refresh">',
    # Not here: <link rel=preconnect>, which opened no connection even with the
    # block off (headless shell 1217), so no control could show it fire.
    "prefetch": '<link rel=prefetch href="{b}/prefetch">',
    "worker_fetch": "<script>"
    + _worker("fetch('{b}/wfetch').catch(()=>0)")
    + "</script>",
    "worker_websocket": "<script>"
    + _worker("new WebSocket('ws://{h}:{p}/wws')")
    + "</script>",
    "webtransport": "<script>try{{new WebTransport('https://{h}:{p}/wt')"
    ".ready.catch(()=>0)}}catch(e){{}}</script>",
    "webrtc": "<script>try{{var pc=new RTCPeerConnection({{iceServers:[{{urls:"
    "'stun:{h}:{p}'}}]}});pc.createDataChannel('x');pc.createOffer()"
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
    # Through thumbs.render_report, the entry the wall's tiles and preview use.
    return thumbs.render_report(page, RENDER_W, RENDER_H)[0]


def _try(path: str, way: str, tries: int) -> list[str]:
    listener = _Listener()
    try:
        # Each way twice: to the listener by address, and by the name `localhost`.
        page = "<!doctype html><p>out</p>" + "".join(
            WAYS[way].format(b=f"http://{h}:{listener.port}", h=h, p=listener.port)
            for h in ("127.0.0.1", "localhost")
        )
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
    # WebRTC's STUN goes out in most runs without the flags, not all, and on some
    # browser builds less often: its control passes if any of 8 tries fires. The
    # as-shipped side below is not loosened by this: every try there must reach
    # nothing.
    tries = 8 if way == "webrtc" else 3
    assert _try(path, way, tries), f"{path} {way}: the probe never reached the listener"


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


GREEN = (0, 255, 0)
_BOX = "border:0;width:200px;height:100px;display:block"


def _local_files(tmp_path) -> dict[str, str]:
    """Other local files a page might draw from, each green where it is drawn."""
    files = {
        "html": ("other.html", "<body style='margin:0;background:#0f0'>other</body>"),
        "svg": (
            "other.svg",
            (
                "<svg xmlns='http://www.w3.org/2000/svg' width='200' height='100'>"
                "<rect width='200' height='100' fill='#0f0'/></svg>"
            ),
        ),
        "css": ("other.css", "#frame{background:#0f0 !important}"),
        "js": ("other.js", "document.getElementById('frame').style.background='#0f0'"),
        "blank": ("blank.html", "<body>"),
    }
    uris = {}
    for key, (name, text) in files.items():
        (tmp_path / name).write_text(text)
        uris[key] = (tmp_path / name).as_uri()
    return uris


def _local_ways(tmp_path) -> dict[str, str]:
    """Each way a page might draw another local file into its picture, as the
    body of a page whose top left 200 by 100 the drawing would fill."""
    u = _local_files(tmp_path)
    h, s = u["html"], u["svg"]
    div = f"<div id=frame style='{_BOX}'></div>"
    late = (
        "border:0;width:200px;height:100px;display:block;position:absolute;top:0;left:0"
    )
    return {
        "object": f"<object id=frame data='{h}' style='{_BOX}'></object>",
        "embed": f"<embed id=frame src='{h}' style='{_BOX}'>",
        "img_svg": f"<img id=frame src='{s}' style='{_BOX}'>",
        "css_bg": f"<div id=frame style=\"{_BOX};background:url('{s}')\"></div>",
        "stylesheet": f"<link rel=stylesheet href='{u['css']}'>{div}",
        "script": f"{div}<script src='{u['js']}'></script>",
        "location": f"{div}<script>location.href='{h}'</script>",
        # Held the same way: with the load done first, the refresh moved the page
        # after the picture in most runs even with the block off.
        "meta_refresh": f"<meta http-equiv=refresh content='0;url={h}'>{div}"
        f"<iframe src='{u['blank']}' style='border:0;width:0;height:0'></iframe>",
        "iframe_srcdoc_nested": f"<iframe id=frame style='{_BOX}' srcdoc=\"<body "
        f"style='margin:0'><iframe src='{h}' style='border:0;width:200px;"
        "height:100px'></iframe>\"></iframe>",
        # The timer fires while the hidden frame still holds the page's load, so
        # the frame it adds is part of the load the picture waits for. One added
        # after the load (a 50 ms timer) was not drawn even with the block off,
        # so no control could show it fire.
        "js_iframe_late": f"{div}<script>setTimeout(function(){{var f=document."
        f"createElement('iframe');f.src='{h}';f.style.cssText='{late}';"
        "document.body.appendChild(f)},0)</script>"
        f"<iframe src='{u['blank']}' style='border:0;width:0;height:0'></iframe>",
        "query_variant": f"<iframe id=frame src='{h}?x=1' style='{_BOX}'></iframe>",
    }


# Not here: a frame of the page's own directory, whose listing headless shell 1217
# did not draw even with the block off, so no control could show it fire.
LOCAL_WAYS = [
    "css_bg",
    "embed",
    "iframe_srcdoc_nested",
    "img_svg",
    "js_iframe_late",
    "location",
    "meta_refresh",
    "object",
    "query_variant",
    "script",
    "stylesheet",
]
# Ways that navigate the page itself away: the picture may then be none at all.
NAVIGATING = {"location", "meta_refresh"}
# The shot reads the page's parts after its load; a meta refresh moves the page
# mid-read and the shot made a picture in 1 of 3 runs with the block off, so on
# that path no control can show it fire reliably. On the render path the refresh
# drew in 8 of 9 runs with the block off: there each side gets TRIES runs, the
# control passing if any draws and the as-shipped side only if none does.
TRIES = {"meta_refresh": 3}
LOCAL_CASES = [
    (path, way)
    for path in PATHS
    for way in LOCAL_WAYS
    if (path, way) != ("shot", "meta_refresh")
]


def test_every_local_way_is_tried(tmp_path):
    assert sorted(_local_ways(tmp_path)) == LOCAL_WAYS


def _local_centre(path: str, way: str, tmp_path):
    """The centre of the top left 200 by 100 in the whole picture `path` makes of
    the page drawing `way`, None if it made none."""
    page = (
        "<!doctype html><body style='margin:0;background:#fff'>"
        + _local_ways(tmp_path)[way]
        + "</body>"
    )
    png = _picture(path, page)
    if png is None:
        assert way in NAVIGATING, f"{path} {way}: no picture"
        return None
    return pixels(png)[2][50][100]


@needs_browser
@pytest.mark.parametrize(("path", "way"), LOCAL_CASES)
def test_another_local_file_shows_with_the_block_off(path, way, tmp_path, block_off):
    got = [_local_centre(path, way, tmp_path) for _ in range(TRIES.get(way, 1))]
    assert GREEN in got, got


@needs_browser
@pytest.mark.parametrize(("path", "way"), LOCAL_CASES)
def test_another_local_file_is_not_drawn(path, way, tmp_path):
    got = [_local_centre(path, way, tmp_path) for _ in range(TRIES.get(way, 1))]
    assert GREEN not in got, got


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
