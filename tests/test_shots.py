"""A screenshot on request (`rendercore.shoot`), and its wire (`renderwire`).

The first tests check the parameters and the answer on their own. The middle ones
run `shoot` against a fake browser: a small program that speaks the DevTools
pipe on fds 3 and 4 as chromium does, so the envelope (environment, process
group, timeout, memory ceiling, slot, limits) is tested without a browser. The
last ones (`needs_browser`) take real pictures; CI runs them in its serial
browser step.
"""

import base64
import json
import os
import struct
import subprocess
import sys
import textwrap
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from ream import anchors, rendercore, renderwire

needs_browser = pytest.mark.skipif(
    not rendercore.available(),
    reason="no headless browser in this environment; the unit tests still run",
)


def chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data))
    )


def png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00" * ((width + 7) // 8) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


# --- the parameters ------------------------------------------------------------------


def test_the_id_and_heading_rules_are_the_anchors_own():
    assert renderwire.SHOT_ID_RE.pattern == anchors.ID_RE.pattern
    assert renderwire.SHOT_SECTION_MAX_CHARS == anchors.HEADING_MAX


def test_a_request_defaults_to_a_light_desktop_page():
    assert renderwire.shot_request() == (1280, "light", None, None)
    assert renderwire.shot_request(390, "dark", element="chart-1").element == "chart-1"


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"width": 800}, "width"),
        ({"width": "1280"}, "width"),
        ({"width": True}, "width"),
        ({"theme": "sepia"}, "theme"),
        ({"element": "a", "section": "b"}, "element"),
        ({"element": "has space"}, "element"),
        ({"element": "x" * 129}, "element"),
        ({"element": ""}, "element"),
        ({"section": "   "}, "section"),
        ({"section": "x" * 201}, "section"),
        ({"section": "tab\tinside"}, "section"),
        ({"section": "\ud800"}, "section"),
    ],
)
def test_a_bad_request_names_its_field(kwargs, field):
    with pytest.raises(renderwire.ShotRequestInvalid) as caught:
        renderwire.shot_request(**kwargs)
    assert caught.value.field == field


def test_a_section_is_matched_by_its_folded_words():
    request = renderwire.shot_request(section="  Second   PART ")
    assert request.section == "second part"


def test_a_section_is_folded_by_the_resolvers_own_white_space():
    # The resolver folds a heading with JavaScript's white space: U+FEFF is space
    # there, so it is here too, and the class it uses is this one.
    request = renderwire.shot_request(section="\ufeffSecond\u3000\ufeffPart\u00a0")
    assert request.section == "second part"
    assert renderwire.fold_heading("a\u2028b\u205fc") == "a b c"
    assert "/" + renderwire.SHOT_SPACE_CLASS + "+/g" in rendercore._RESOLVE_JS


def test_the_query_goes_there_and_back():
    request = renderwire.shot_request(390, "dark", section="Café & co = 100%")
    query = renderwire.shot_query(request)
    assert query.isascii() and " " not in query
    assert renderwire.parse_shot_query(query) == request
    assert renderwire.parse_shot_query(query.encode()) == request
    element = renderwire.shot_request(element="a.b:c-d_e")
    assert renderwire.parse_shot_query(renderwire.shot_query(element)) == element


@pytest.mark.parametrize(
    "query",
    [
        "",
        "w=1280",
        "theme=light",
        "w=1280&theme=light&scale=2",
        "w=1280&w=390&theme=light",
        "w=1280&theme",
        "w=1280&theme=light&section=%FF",
        "w=1280&theme=light&element=a&section=b",
        "w=01280x&theme=light",
        "w=1280&theme=light&section=" + "a" * 5000,
    ],
)
def test_a_bad_query_is_refused(query):
    with pytest.raises(renderwire.ShotRequestInvalid):
        renderwire.parse_shot_query(query)


@pytest.mark.parametrize("key", ["theme=dark", "element=x", "section=x"])
def test_a_shot_key_sent_to_the_render_route_is_refused(key):
    """So a renderer that knows only renders fails loudly on a shot."""
    with pytest.raises(renderwire.RenderRequestInvalid):
        renderwire.parse_request_query(f"w=900&h=640&check=1&{key}")


# --- the answer -----------------------------------------------------------------------


def a_shot(**kw):
    values = {
        "width": 300,
        "height": 120,
        "page_height": 2000,
        "truncated": False,
    }
    values.update(kw)
    return renderwire.Shot("shot", png(values["width"], values["height"]), **values)


@pytest.mark.parametrize(
    "shot",
    [
        a_shot(),
        a_shot(height=renderwire.SHOT_MAX_HEIGHT, truncated=True),
        renderwire.Shot("no_match", matches=0),
        renderwire.Shot("ambiguous", matches=3),
        *(
            renderwire.Shot(o)
            for o in renderwire.SHOT_OUTCOMES
            if o not in ("shot", "no_match", "ambiguous")
        ),
    ],
)
def test_an_answer_goes_there_and_back(shot):
    back = renderwire.parse_shot_answer(renderwire.encode_shot_answer(shot))
    assert (back.outcome, back.png, back.width, back.height) == (
        shot.outcome,
        shot.png,
        shot.width,
        shot.height,
    )
    assert (back.page_height, back.truncated, back.matches) == (
        shot.page_height,
        shot.truncated,
        shot.matches,
    )


def test_a_shot_never_shows_its_bytes():
    assert "PNG" not in repr(a_shot())


def answer(**changes) -> bytes:
    body = json.loads(renderwire.encode_shot_answer(a_shot()))
    body.update(changes)
    return json.dumps(body).encode()


@pytest.mark.parametrize(
    "raw",
    [
        answer(outcome="maybe"),
        answer(outcome="failed"),  # a picture with an outcome that has none
        answer(width=301),  # not the picture's size
        answer(height=0),
        answer(truncated=1),
        answer(matches=0),
        answer(page_height=-1),
        answer(v=2),
        answer(extra=1),
        answer(png_base64="!!!"),
        json.dumps(
            {
                "v": 1,
                "outcome": "ambiguous",
                "png_base64": None,
                "width": 0,
                "height": 0,
                "page_height": 0,
                "truncated": False,
                "matches": 1,
            }
        ).encode(),
        json.dumps(
            {
                "v": 1,
                "outcome": "timeout",
                "png_base64": None,
                "width": 0,
                "height": 0,
                "page_height": 0,
                "truncated": False,
                "matches": 0,
            }
        ).encode(),
        b"[]",
        b"{",
    ],
)
def test_a_bad_answer_is_refused(raw):
    with pytest.raises(renderwire.RenderAnswerInvalid):
        renderwire.parse_shot_answer(raw)


def test_a_picture_over_the_limit_is_refused(monkeypatch):
    monkeypatch.setattr(renderwire, "SHOT_PNG_MAX_BYTES", 10)
    with pytest.raises(renderwire.RenderAnswerInvalid):
        renderwire.check_shot(a_shot())


# --- the app's call ---------------------------------------------------------------------


class _Stub(BaseHTTPRequestHandler):
    status = 200
    body = b""
    seen: ClassVar[list] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).seen.append((self.path, dict(self.headers), self.rfile.read(length)))
        self.send_response(type(self).status)
        self.send_header("Content-Length", str(len(type(self).body)))
        if type(self).status == 503:
            self.send_header("Retry-After", "1")
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def stub():
    _Stub.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_the_call_sends_the_shot_and_reads_its_answer(stub):
    _Stub.status, _Stub.body = 200, renderwire.encode_shot_answer(a_shot())
    request = renderwire.shot_request(390, "dark", section="Intro")
    shot = renderwire.call_shot(
        f"http://127.0.0.1:{stub.server_port}", "<p>é</p>", request
    )
    assert shot.outcome == "shot" and (shot.width, shot.height) == (300, 120)
    path, headers, body = _Stub.seen[0]
    assert path == "/v1/shot?w=390&theme=dark&section=intro"
    assert headers["X-Ream-Render"] == "1"
    assert body == "<p>é</p>".encode()


def test_a_busy_renderer_is_the_outcome_busy(stub):
    _Stub.status, _Stub.body = 503, b"busy\n"
    shot = renderwire.call_shot(
        f"http://127.0.0.1:{stub.server_port}", "<p>x</p>", renderwire.shot_request()
    )
    assert shot.outcome == "busy"
    assert len(_Stub.seen) == 1, "not asked again: the caller says busy instead"


@pytest.mark.parametrize(
    ("status", "body"),
    [(404, b""), (400, b"query has an unknown key\n"), (200, b"{}"), (302, b"")],
)
def test_anything_else_is_unavailable(stub, status, body):
    _Stub.status, _Stub.body = status, body
    with pytest.raises(renderwire.RenderUnavailable):
        renderwire.call_shot(
            f"http://127.0.0.1:{stub.server_port}",
            "<p>x</p>",
            renderwire.shot_request(),
        )


# --- the envelope, with a fake browser ---------------------------------------------------

# A stand-in for chromium on its DevTools pipe. Its behaviour comes from the JSON
# file named in its first line; it writes what it saw (environment, arguments,
# every command, in order) to the log named there.
FAKE = r"""
import base64, json, os, struct, subprocess, sys, time, zlib
CONFIG = __CONFIG__
cfg = json.load(open(CONFIG))
log = open(cfg["log"], "a")
def note(**kw):
    log.write(json.dumps(kw) + "\n"); log.flush()
note(env=dict(os.environ), argv=sys.argv[1:], pgid=os.getpgid(0))
mode = cfg["mode"]
if mode == "exit":
    sys.exit(0)
if mode in ("hang", "child"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    note(child=child.pid)
    if mode == "hang":
        time.sleep(60)
fin, fout = os.fdopen(3, "rb", buffering=0), os.fdopen(4, "wb", buffering=0)
def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
def png(w, h):
    rows = b"".join(b"\x00" + b"\x00" * ((w + 7) // 8) for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 1, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
def out(msg):
    fout.write(json.dumps(msg).encode() + b"\0")
buf = b""
while True:
    while b"\0" not in buf:
        part = fin.read(65536)
        if not part:
            sys.exit(0)
        buf += part
    raw, buf = buf.split(b"\0", 1)
    msg = json.loads(raw)
    method, sid, params = msg["method"], msg.get("sessionId"), msg.get("params", {})
    note(method=method, session=sid, params=params)
    result, events = {}, []
    if method in cfg.get("refuse", []):
        out({"id": msg["id"], "error": {"code": -32000, "message": "no"}, "sessionId": sid})
        continue
    if method == "Target.createTarget":
        result = {"targetId": "T1"}
        events.append({"method": "Target.attachedToTarget", "params": {
            "sessionId": "S1", "targetInfo": {"targetId": "T1", "type": "page"},
            "waitingForDebugger": True}})
    elif method == "Page.navigate":
        result = {"frameId": "F1"}
        for rid, url in (("R1", params["url"]), ("R2", "http://example.invalid/x")):
            events.append({"method": "Fetch.requestPaused", "sessionId": "S1",
                           "params": {"requestId": rid, "request": {"url": url}}})
        if cfg.get("worker"):
            events.append({"method": "Target.attachedToTarget", "sessionId": "S1", "params": {
                "sessionId": "S2", "targetInfo": {"targetId": "W1", "type": "worker"},
                "waitingForDebugger": True}})
        events.append({"method": "Page.loadEventFired", "sessionId": "S1", "params": {}})
    elif method == "Emulation.setVirtualTimePolicy":
        events.append({"method": "Emulation.virtualTimeBudgetExpired", "sessionId": "S1",
                       "params": {}})
    elif method == "Page.getFrameTree":
        result = {"frameTree": {"frame": {"id": "F1"}}}
    elif method == "Page.createIsolatedWorld":
        result = {"executionContextId": 7}
    elif method == "Runtime.evaluate":
        if mode == "stall":
            time.sleep(60)
        result = {"result": {"value": cfg["found"]}}
    elif method == "Page.captureScreenshot":
        clip = params["clip"]
        w, h = clip["width"], clip["height"]
        if mode == "wrong_size":
            w += 1
        data = png(w, h)
        if mode == "huge":
            data += b"\0" * cfg["pad"]
        if mode == "huge_event":
            out({"method": "Page.frameNavigated", "sessionId": sid,
                 "params": {"url": "u" * cfg["pad"]}})
        result = {"data": base64.b64encode(data).decode()}
    out({"id": msg["id"], "result": result, **({"sessionId": sid} if sid else {})})
    for event in events:
        out(event)
"""


@pytest.fixture()
def fake(tmp_path):
    """`make(mode, **config)`: the path of a fake browser, and its log's reader."""
    made = []

    def make(mode="ok", **config):
        n = len(made)
        cfg_path = tmp_path / f"fake{n}.json"
        log = tmp_path / f"fake{n}.log"
        found = {"n": -1, "ph": 1500, "vw": 1280}
        cfg = {"mode": mode, "log": str(log), "found": found, **config}
        cfg_path.write_text(json.dumps(cfg))
        script = tmp_path / f"fake{n}.py"
        script.write_text(FAKE.replace("__CONFIG__", repr(str(cfg_path))))
        binary = tmp_path / f"fake{n}"
        binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        binary.chmod(0o755)

        def read():
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text().splitlines()]

        made.append(binary)
        return str(binary), read

    return make


PAGE = "<!doctype html><html><body><h1>Hello</h1></body></html>"


def methods(lines, session=None):
    return [
        line["method"]
        for line in lines
        if "method" in line and (session is None or line["session"] == session)
    ]


def test_a_full_page_is_the_width_by_the_page_height(fake):
    binary, read = fake()
    shot = rendercore.shoot(binary, PAGE, renderwire.shot_request())
    assert shot.outcome == "shot", shot
    assert (shot.width, shot.height, shot.page_height) == (1280, 1500, 1500)
    assert shot.truncated is False
    capture = next(
        line for line in read() if line.get("method") == "Page.captureScreenshot"
    )
    assert capture["params"]["clip"] == {
        "x": 0,
        "y": 0,
        "width": 1280,
        "height": 1500,
        "scale": 1,
    }
    assert capture["params"]["captureBeyondViewport"] is True


def test_the_browser_gets_an_environment_built_from_nothing(fake, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://not-a-real-one")
    monkeypatch.setenv("REAM_SHOT_SENTINEL", "1")
    binary, read = fake()
    rendercore.shoot(binary, PAGE, renderwire.shot_request())
    first = read()[0]
    env = first["env"]
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == env["TMPDIR"]
    assert Path(env["HOME"]).name.startswith("artifacts-shot-")
    # A shell may add its own bookkeeping, and the fake's Python its locale
    # (LC_CTYPE); nothing of the caller's comes through.
    assert "DATABASE_URL" not in env and "REAM_SHOT_SENTINEL" not in env
    assert set(env) <= {
        "HOME",
        "TMPDIR",
        "PATH",
        "PWD",
        "OLDPWD",
        "SHLVL",
        "_",
        "LC_CTYPE",
    }
    assert first["pgid"] != os.getpgid(0), "its own process group"
    assert "--remote-debugging-pipe" in first["argv"]
    for flag in rendercore._isolation_flags():
        assert flag in first["argv"]
    assert not Path(env["HOME"]).exists(), "its directory is removed afterwards"


def test_it_runs_under_the_kernels_data_limit(fake, monkeypatch):
    seen = []
    real = rendercore._data_limited

    def spy(argv, limit):
        seen.append(limit)
        return real(argv, limit)

    monkeypatch.setattr(rendercore, "_data_limited", spy)
    binary, _ = fake()
    rendercore.shoot(binary, PAGE, renderwire.shot_request())
    assert seen == [rendercore.RENDER_MAX_DATA_BYTES]


def test_every_target_is_held_to_the_page_and_offline_before_it_runs(fake):
    binary, read = fake(worker=True)
    shot = rendercore.shoot(binary, PAGE, renderwire.shot_request())
    assert shot.outcome == "shot", shot
    lines = read()
    order = methods(lines)
    assert order[0] == "Target.setAutoAttach"
    page = methods(lines, "S1")
    run = page.index("Runtime.runIfWaitingForDebugger")
    for before in (
        "Fetch.enable",
        "Network.enable",
        "Network.emulateNetworkConditions",
    ):
        assert page.index(before) < run, page
    assert page.index("Runtime.runIfWaitingForDebugger") < page.index("Page.navigate")
    offline = next(
        line
        for line in lines
        if line.get("method") == "Network.emulateNetworkConditions"
    )
    assert offline["params"]["offline"] is True
    # The page itself goes on; anything else it asks for fails.
    decided = {
        line["params"]["requestId"]: line["method"]
        for line in lines
        if line.get("method", "").startswith("Fetch.") and "requestId" in line["params"]
    }
    assert decided == {"R1": "Fetch.continueRequest", "R2": "Fetch.failRequest"}
    # A worker is held at its start and never let go.
    assert methods(lines, "S2") == []


def test_a_target_that_cannot_be_held_fails_the_shot(fake):
    binary, _ = fake(refuse=["Network.emulateNetworkConditions"])
    assert rendercore.shoot(binary, PAGE, renderwire.shot_request()).outcome == "failed"


def test_theme_and_width_are_set_before_the_page_opens(fake):
    binary, read = fake()
    rendercore.shoot(binary, PAGE, renderwire.shot_request(390, "dark"))
    lines = read()
    page = methods(lines, "S1")
    assert page.index("Emulation.setEmulatedMedia") < page.index("Page.navigate")
    metrics = next(
        line
        for line in lines
        if line.get("method") == "Emulation.setDeviceMetricsOverride"
    )
    assert metrics["params"]["width"] == 390
    assert metrics["params"]["height"] == 844
    assert metrics["params"]["deviceScaleFactor"] == 1
    media = next(
        line for line in lines if line.get("method") == "Emulation.setEmulatedMedia"
    )
    assert media["params"]["features"] == [
        {"name": "prefers-color-scheme", "value": "dark"}
    ]


def test_the_parts_are_found_in_a_world_of_their_own(fake):
    binary, read = fake(found={"n": 1, "ph": 900, "vw": 1280, "box": {}})
    rendercore.shoot(binary, PAGE, renderwire.shot_request(element="x"))
    lines = read()
    evaluate = next(line for line in lines if line.get("method") == "Runtime.evaluate")
    assert evaluate["params"]["contextId"] == 7
    assert evaluate["params"]["expression"].endswith('("x", null)')


@pytest.mark.parametrize(
    ("box", "clip", "truncated"),
    [
        ({"x": 10.5, "y": 20.25, "w": 100, "h": 50}, (10, 20, 101, 51), False),
        ({"x": -5, "y": 0, "w": 20, "h": 20}, (0, 0, 15, 20), False),
        ({"x": 0, "y": 100, "w": 400, "h": 9000}, (0, 100, 400, 8000), True),
        ({"x": 0, "y": 0, "w": 3000, "h": 10}, (0, 0, 2560, 10), True),
    ],
)
def test_an_element_is_shot_to_its_box_in_whole_pixels(fake, box, clip, truncated):
    binary, read = fake(found={"n": 1, "ph": 20000, "vw": 1280, "box": box})
    shot = rendercore.shoot(binary, PAGE, renderwire.shot_request(element="x"))
    assert shot.outcome == "shot", shot
    assert (shot.width, shot.height, shot.truncated) == (clip[2], clip[3], truncated)
    capture = next(
        line for line in read() if line.get("method") == "Page.captureScreenshot"
    )
    got = capture["params"]["clip"]
    assert (got["x"], got["y"], got["width"], got["height"]) == clip


def test_a_tall_page_is_cut_and_says_so(fake):
    binary, _ = fake(found={"n": -1, "ph": 12345, "vw": 1280})
    shot = rendercore.shoot(binary, PAGE, renderwire.shot_request())
    assert (shot.height, shot.page_height, shot.truncated) == (8000, 12345, True)


@pytest.mark.parametrize(
    ("found", "outcome", "matches"),
    [
        ({"n": 0, "ph": 900, "vw": 1280}, "no_match", 0),
        ({"n": 3, "ph": 900, "vw": 1280}, "ambiguous", 3),
        (
            {"n": 1, "ph": 900, "vw": 1280, "box": {"x": 0, "y": 0, "w": 0, "h": 9}},
            "empty",
            None,
        ),
    ],
)
def test_a_selector_that_does_not_pick_one_part_says_why(fake, found, outcome, matches):
    binary, read = fake(found=found)
    shot = rendercore.shoot(binary, PAGE, renderwire.shot_request(section="x"))
    assert (shot.outcome, shot.matches, shot.png) == (outcome, matches, None)
    assert "Page.captureScreenshot" not in methods(read())


def test_a_picture_not_of_the_size_asked_is_no_picture(fake):
    binary, _ = fake("wrong_size")
    assert rendercore.shoot(binary, PAGE, renderwire.shot_request()).outcome == "failed"


def test_a_picture_over_the_limit_is_picture_too_big(fake, monkeypatch):
    monkeypatch.setattr(renderwire, "SHOT_PNG_MAX_BYTES", 4096)
    binary, _ = fake("huge", pad=8192)
    shot = rendercore.shoot(binary, PAGE, renderwire.shot_request())
    assert shot.outcome == "picture_too_big"


def test_a_message_over_the_pipes_limit_is_not_read_whole(fake, monkeypatch):
    monkeypatch.setattr(rendercore, "SHOT_MESSAGE_MAX_BYTES", 2048)
    binary, _ = fake("huge", pad=8192)
    shot = rendercore.shoot(binary, PAGE, renderwire.shot_request())
    assert shot.outcome == "picture_too_big"


def test_an_event_over_the_pipes_limit_is_a_failed_shot(fake, monkeypatch):
    # Over-long while the picture was asked for, but not the picture's answer.
    monkeypatch.setattr(rendercore, "SHOT_MESSAGE_MAX_BYTES", 4096)
    binary, _ = fake("huge_event", pad=8192)
    shot = rendercore.shoot(binary, PAGE, renderwire.shot_request())
    assert shot.outcome == "failed"


def test_an_unexpected_error_is_a_failed_shot(fake, monkeypatch):
    def broken(found, request):
        raise OverflowError("cannot convert float infinity to integer")

    monkeypatch.setattr(rendercore, "_shot_box", broken)
    binary, _ = fake()
    assert rendercore.shoot(binary, PAGE, renderwire.shot_request()).outcome == "failed"


def test_the_watch_ends_the_group_at_its_deadline_on_its_own():
    # Nothing reads a pipe here: only the watch's deadline arm can end it.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        started = time.monotonic()
        watch = rendercore._Watch(proc, time.monotonic() + 0.3)
        proc.wait(timeout=10)
        watch.stop()
        assert watch.reason == "timeout"
        assert time.monotonic() - started < 10
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_browser_that_exits_is_a_failed_shot(fake):
    binary, _ = fake("exit")
    assert rendercore.shoot(binary, PAGE, renderwire.shot_request()).outcome == "failed"


def _gone(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] in ("Z", "X")


def test_a_browser_that_hangs_is_ended_with_its_whole_group(fake, monkeypatch):
    monkeypatch.setattr(rendercore, "TIMEOUT_SECONDS", 1.5)
    binary, read = fake("hang")
    started = time.monotonic()
    shot = rendercore.shoot(binary, PAGE, renderwire.shot_request())
    assert shot.outcome == "timeout"
    assert time.monotonic() - started < 10
    child = next(line["child"] for line in read() if "child" in line)
    deadline = time.monotonic() + 5
    while not _gone(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(child), "the browser's helper was ended with it"


def test_a_page_that_stalls_the_browser_is_a_timeout(fake, monkeypatch):
    monkeypatch.setattr(rendercore, "TIMEOUT_SECONDS", 1.5)
    binary, _ = fake("stall")
    assert (
        rendercore.shoot(binary, PAGE, renderwire.shot_request()).outcome == "timeout"
    )


def test_a_browser_past_the_memory_ceiling_is_ended(fake, monkeypatch):
    monkeypatch.setattr(rendercore, "RENDER_MAX_RSS_BYTES", 1)
    binary, read = fake("hang")
    # The ceiling is read only once the fake has noted its helper: on a loaded runner the
    # first poll could end the group before that, leaving no helper to check. Until then
    # the group reads as using nothing.
    real = rendercore._group_rss
    monkeypatch.setattr(
        rendercore,
        "_group_rss",
        lambda pid: real(pid) if any("child" in line for line in read()) else 0,
    )
    shot = rendercore.shoot(binary, PAGE, renderwire.shot_request())
    assert shot.outcome == "memory"
    child = next(line["child"] for line in read() if "child" in line)
    deadline = time.monotonic() + 5
    while not _gone(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(child)


def test_a_normal_shot_ends_what_the_browser_left(fake):
    binary, read = fake("child")
    shot = rendercore.shoot(binary, PAGE, renderwire.shot_request())
    assert shot.outcome == "shot", shot
    child = next(line["child"] for line in read() if "child" in line)
    deadline = time.monotonic() + 5
    while not _gone(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(child)


def test_a_shot_waits_for_the_one_slot_then_is_busy(fake, monkeypatch):
    monkeypatch.setattr(rendercore, "SHOT_SLOT_WAIT_SECONDS", 0.1)
    binary, read = fake()
    assert rendercore._RENDER_SLOT.acquire(timeout=5)
    try:
        shot = rendercore.shoot(binary, PAGE, renderwire.shot_request())
    finally:
        rendercore._RENDER_SLOT.release()
    assert shot.outcome == "busy"
    assert read() == [], "no browser started"


def test_the_slot_is_released_after_a_shot_and_not_taken_when_held(fake):
    binary, _ = fake()
    assert rendercore.shoot(binary, PAGE, renderwire.shot_request()).outcome == "shot"
    assert rendercore._RENDER_SLOT.acquire(blocking=False)
    try:
        shot = rendercore.shoot(binary, PAGE, renderwire.shot_request(), slot_held=True)
    finally:
        rendercore._RENDER_SLOT.release()
    assert shot.outcome == "shot"


def test_no_browser_and_too_big_a_page_start_nothing(fake):
    assert rendercore.shoot(None, PAGE, renderwire.shot_request()).outcome == (
        "no_browser"
    )
    binary, read = fake()
    big = "<p>" + "x" * (rendercore.RENDER_MAX_BYTES + 1) + "</p>"
    assert rendercore.shoot(binary, big, renderwire.shot_request()).outcome == (
        "page_too_big"
    )
    assert read() == []


# --- real pictures ------------------------------------------------------------------------


def pixels(data: bytes):
    """`(width, height, rows)` of an 8-bit RGB or RGBA PNG, each row a list of
    (r, g, b) tuples. Enough of a decoder to read a screenshot's colours."""
    pos, idat, header = 8, b"", None
    while pos < len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    width, height, depth, colour, _, _, interlace = header
    assert depth == 8 and colour in (2, 6) and interlace == 0, header
    bpp = 3 if colour == 2 else 4
    raw = zlib.decompress(idat)
    stride = width * bpp
    rows, prior = [], bytearray(stride)
    for y in range(height):
        kind = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        for i in range(stride):
            left = line[i - bpp] if i >= bpp else 0
            up = prior[i]
            corner = prior[i - bpp] if i >= bpp else 0
            if kind == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif kind == 4:
                p = left + up - corner
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
                pred = left if pa <= pb and pa <= pc else up if pb <= pc else corner
                line[i] = (line[i] + pred) & 0xFF
        rows.append([tuple(line[x * bpp : x * bpp + 3]) for x in range(width)])
        prior = line
    return width, height, rows


REAL = textwrap.dedent(
    """\
    <!doctype html><html><head><meta charset=utf-8><style>
    body{margin:0;font:16px sans-serif;background:#fff;color:#000}
    @media (prefers-color-scheme:dark){body{background:#000;color:#fff}}
    #chart{position:absolute;left:20px;top:40px;width:300px;height:120px;background:#f00}
    #flat{height:0}
    .twin{width:10px;height:10px}
    #spacer{height:SPACERpx}
    h2{margin:0;height:100px}
    </style></head><body>
    <div id=chart></div><div id=flat></div>
    <div class=twin id=twin></div><div class=twin id=twin></div>
    <div style="height:200px"></div>
    <h2>Second part</h2><p style="margin:0;height:300px">words</p>
    <h2>Third part</h2><div id=spacer></div>
    </body></html>"""
)


def real_page(spacer: int = 100) -> str:
    return REAL.replace("SPACER", str(spacer))


@needs_browser
@pytest.mark.parametrize("width", [1280, 390])
def test_a_real_page_is_shot_whole_at_either_width(width):
    shot = rendercore.shoot(
        rendercore.browser_path(), real_page(), renderwire.shot_request(width)
    )
    assert shot.outcome == "shot", shot
    viewport = renderwire.SHOT_VIEWPORTS[width]
    assert shot.width == width
    assert shot.height == shot.page_height == max(viewport, 720 + 100)
    renderwire.validate_png(shot.png, shot.width, shot.height)


@needs_browser
def test_a_real_element_is_shot_to_its_box():
    shot = rendercore.shoot(
        rendercore.browser_path(),
        real_page(),
        renderwire.shot_request(element="chart"),
    )
    assert shot.outcome == "shot", shot
    assert (shot.width, shot.height) == (300, 120)
    _, _, rows = pixels(shot.png)
    assert rows[60][150] == (255, 0, 0), "the element itself, red"


@needs_browser
def test_a_real_section_runs_to_the_next_heading_of_its_rank():
    shot = rendercore.shoot(
        rendercore.browser_path(),
        real_page(),
        renderwire.shot_request(section="SECOND   part"),
    )
    assert shot.outcome == "shot", shot
    assert (shot.width, shot.height) == (1280, 400)


@needs_browser
def test_the_theme_is_the_pages_own_dark_or_light():
    binary = rendercore.browser_path()
    light = rendercore.shoot(binary, real_page(), renderwire.shot_request(390, "light"))
    dark = rendercore.shoot(binary, real_page(), renderwire.shot_request(390, "dark"))
    assert pixels(light.png)[2][700][380] == (255, 255, 255)
    assert pixels(dark.png)[2][700][380] == (0, 0, 0)


@needs_browser
@pytest.mark.parametrize(
    ("request_", "outcome", "matches"),
    [
        (renderwire.shot_request(element="nowhere"), "no_match", 0),
        (renderwire.shot_request(element="twin"), "ambiguous", 2),
        (renderwire.shot_request(section="no such heading"), "no_match", 0),
        (renderwire.shot_request(element="flat"), "empty", None),
    ],
)
def test_a_real_selector_that_picks_no_one_part(request_, outcome, matches):
    shot = rendercore.shoot(rendercore.browser_path(), real_page(), request_)
    assert (shot.outcome, shot.matches) == (outcome, matches)


@needs_browser
def test_a_real_page_taller_than_the_cap_is_cut():
    shot = rendercore.shoot(
        rendercore.browser_path(), real_page(12000), renderwire.shot_request()
    )
    assert shot.outcome == "shot", shot
    assert (shot.height, shot.truncated) == (renderwire.SHOT_MAX_HEIGHT, True)
    assert shot.page_height == 720 + 12000


@needs_browser
def test_a_real_page_that_never_finishes_is_ended(monkeypatch):
    monkeypatch.setattr(rendercore, "TIMEOUT_SECONDS", 5)
    page = "<!doctype html><p>loop</p><script>for(;;){}</script>"
    # This process may have children of its own (a test database): only what the
    # shot adds counts.
    before = _children()
    started = time.monotonic()
    shot = rendercore.shoot(rendercore.browser_path(), page, renderwire.shot_request())
    assert shot.outcome == "timeout"
    assert time.monotonic() - started < 15
    assert _children() - before == set(), "nothing of the browser is left"


def _children() -> set[int]:
    return {
        int(pid)
        for pid in os.listdir("/proc")
        if pid.isdigit() and not _gone(int(pid)) and _parent(int(pid)) == os.getpid()
    }


def _parent(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return int(stat.rsplit(")", 1)[1].split()[1])


def test_the_pictures_from_the_fake_decode():
    """The decoder above reads what it is given (a guard on the test's own helper):
    a 2x1 RGB picture, Sub-filtered."""
    raw = b"\x01" + bytes([10, 20, 30, 5, 5, 5])
    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    assert pixels(data) == (2, 1, [[(10, 20, 30), (15, 25, 35)]])


def test_base64_of_the_largest_picture_fits_the_answer():
    assert renderwire.SHOT_PNG_BASE64_MAX_CHARS < renderwire.RESPONSE_MAX_BYTES
    assert len(base64.b64encode(b"\0" * renderwire.SHOT_PNG_MAX_BYTES)) == (
        renderwire.SHOT_PNG_BASE64_MAX_CHARS
    )
