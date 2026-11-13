"""The renderer as its own process (`ream.renderd`).

Most of these call the ASGI app directly, with a fake browser run
(`rendercore._run`) and a fake process list; the last one starts the real thing
on loopback with a real browser.
"""

import asyncio
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import zlib
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from ream import rendercore, renderd, renderwire, settings

W, H = 900, 640
PAGE = "<!doctype html><html><body><h1>Hello</h1></body></html>"
HEADERS = {"X-Ream-Render": "1", "Content-Type": "text/html; charset=utf-8"}
CONSOLE = (
    '[1:1:1113/101010.000001:INFO:CONSOLE:3] "Uncaught ReferenceError: x is not'
    ' defined", source: file:///tmp/page.html (3)\n'
)

_REAL_READ_PROCS = renderd._read_procs

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


def png(width=W, height=H) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00" * ((width + 7) // 8) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def no_cors(headers) -> bool:
    return not any(k.lower().startswith("access-control-") for k in headers)


@pytest.fixture()
def work(tmp_path):
    return renderd.prepare_work_root(tmp_path / "work")


@pytest.fixture()
def exits():
    return []


@pytest.fixture()
def app(work, exits, monkeypatch):
    """The app over a fresh work directory, a browser that is a plain file, and
    a process list holding only this process, so nothing is left after a render."""
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    monkeypatch.setattr(renderd, "STRAY_GRACE_SECONDS", 0)
    me = os.getpid()
    monkeypatch.setattr(renderd, "_read_procs", lambda: [(me, 1, {os.getuid()}, "S")])
    return renderd.Renderd(work, exit=exits.append)


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def browser_runs(monkeypatch):
    """A fake browser run that writes a picture of the size asked for and logs a
    console error; records each run's argv and environment."""
    runs = []

    def fake_run(argv, **kw):
        size = next(a for a in argv if a.startswith("--window-size="))
        w, h = (int(n) for n in size.split("=", 1)[1].split(","))
        shot = next(a for a in argv if a.startswith("--screenshot="))
        Path(shot.split("=", 1)[1]).write_bytes(png(w, h))
        # Something of the browser's own in its directory, as a real one leaves.
        profile = Path(kw["env"]["TMPDIR"]) / "org.chromium.Chromium.scoped_dir.x"
        (profile / "Default").mkdir(parents=True)
        runs.append({"argv": argv, "env": dict(kw["env"]), "cwd": kw["cwd"]})
        return subprocess.CompletedProcess(argv, 0, b"", CONSOLE.encode())

    monkeypatch.setattr(rendercore, "_run", fake_run)
    return runs


def call(app, method, path, *, query=b"", headers=(), body=b""):
    """One request straight into the ASGI app: `(status, headers, body, reads)`,
    where `reads` counts how often the app asked for the request body."""
    reads = []
    sent = []

    async def receive():
        reads.append(1)
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
    }
    asyncio.run(app(scope, receive, send))
    start = sent[0]
    return (
        start["status"],
        {k.decode(): v.decode() for k, v in start["headers"]},
        b"".join(m.get("body", b"") for m in sent[1:]),
        len(reads),
    )


# --- the environment ----------------------------------------------------------------


def test_the_renderer_takes_only_its_own_settings():
    count = settings.renderer_foreign_count
    assert count({}) == 0
    assert count({"REAM_BROWSER": "/b", "ARTIFACTS_BROWSER": "/b"}) == 0
    assert count({"REAM_BIND_HOST": "127.0.0.2", "ARTIFACTS_BIND_HOST": "x"}) == 0
    assert count({"PATH": "/bin", "HOME": "/h", "TMPDIR": "/t"}) == 0
    assert count({"DATABASE_URL": "postgresql://u:p@db/x"}) == 1
    # An allow-list: any other name of either prefix, set or blank, is refused.
    assert count({"REAM_GALLERY_ORIGIN": "https://x"}) == 1
    assert count({"ARTIFACTS_NUDGE_TOKEN": "t", "REAM_ANYTHING_NEW": ""}) == 2
    assert count({"REAM_RENDERER_URL": "http://r:8091", "REAM_BROWSER": "/b"}) == 1


def test_it_refuses_to_start_naming_a_count_and_no_name_or_value(
    monkeypatch, capsys, tmp_path
):
    for name in list(os.environ):
        if name.startswith(("REAM_", "ARTIFACTS_")) or name == "DATABASE_URL":
            monkeypatch.delenv(name)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:hunter2@db/artifacts")
    monkeypatch.setenv("ARTIFACTS_SECRET_THING", "swordfish")
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    started = []
    monkeypatch.setattr(renderd, "prepare_work_root", started.append)

    code = renderd.main(["--port", "1", "--work-dir", str(tmp_path / "w")])

    assert code == renderd.EXIT_REFUSED
    assert started == []
    err = capsys.readouterr().err
    assert "2 variable(s)" in err
    for leaked in ("hunter2", "swordfish", "SECRET_THING", "postgresql"):
        assert leaked not in err


# --- refusals --------------------------------------------------------------------------


def test_without_the_header_it_is_a_bare_403(client):
    plain = {"Content-Type": "text/html"}
    for headers in (plain, {**plain, "X-Ream-Render": "yes"}):
        r = client.post(f"/v1/render?w={W}&h={H}", content=PAGE, headers=headers)
        assert r.status_code == 403
        assert r.content == b""
        assert no_cors(r.headers)


def test_options_is_a_bare_403(client):
    asked = {
        **HEADERS,
        "Origin": "http://127.0.0.1:9",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-ream-render",
    }
    r = client.options(f"/v1/render?w={W}&h={H}", headers=asked)
    assert r.status_code == 403
    assert r.content == b""
    assert no_cors(r.headers)


def test_other_methods_and_paths_get_no_cors_either(client):
    assert client.get("/v1/render").status_code == 405
    assert client.put("/v1/render", content=PAGE, headers=HEADERS).status_code == 405
    for r in (
        client.post("/", content=PAGE, headers=HEADERS),
        client.get("/v1/other"),
        client.post("/healthz"),
    ):
        assert r.status_code in (404, 405)
        assert no_cors(r.headers)


def test_an_unknown_query_key_is_a_400(client, browser_runs):
    for query in (
        f"w={W}&h={H}&full=1",
        f"w={W}&h={H}&selector=main",
        f"w={W}",
        f"w=10&h={H}",
    ):
        r = client.post(f"/v1/render?{query}", content=PAGE, headers=HEADERS)
        assert r.status_code == 400, query
    assert browser_runs == []


def test_only_html_is_taken(client, browser_runs):
    for kind in ("text/plain", "application/json", "text/html; charset=latin-1"):
        r = client.post(
            f"/v1/render?w={W}&h={H}",
            content=PAGE,
            headers={**HEADERS, "Content-Type": kind},
        )
        assert r.status_code == 415, kind
    assert browser_runs == []


def test_a_missing_or_oversize_length_is_a_413_before_the_body_is_read(
    app, browser_runs
):
    query = f"w={W}&h={H}".encode()
    base = [("X-Ream-Render", "1"), ("Content-Type", "text/html")]
    too_big = str(rendercore.RENDER_MAX_BYTES + 1)
    for headers in (
        base,
        [*base, ("Content-Length", too_big)],
        [*base, ("Content-Length", "-1")],
        [*base, ("Content-Length", "12"), ("Content-Length", "12")],
        [*base, ("Transfer-Encoding", "chunked"), ("Content-Length", "12")],
    ):
        status, _, _, reads = call(
            app, "POST", "/v1/render", query=query, headers=headers, body=b"<p>x</p>"
        )
        assert status == 413, headers
        assert reads == 0, headers
    assert browser_runs == []

    # At the limit it is read, all of it.
    body = b"<p>" + b"x" * (rendercore.RENDER_MAX_BYTES - 7) + b"</p>"
    status, _, _, reads = call(
        app,
        "POST",
        "/v1/render",
        query=query,
        headers=[*base, ("Content-Length", str(len(body)))],
        body=body,
    )
    assert (status, reads) == (200, 1)


def test_a_body_that_is_not_its_length_is_a_400(app, browser_runs):
    headers = [
        ("X-Ream-Render", "1"),
        ("Content-Type", "text/html"),
        ("Content-Length", "100"),
    ]
    status, *_ = call(
        app, "POST", "/v1/render", query=b"w=900&h=640", headers=headers, body=b"<p>"
    )
    assert status == 400
    assert browser_runs == []


def test_busy_is_a_503_with_retry_after(client, browser_runs, monkeypatch):
    monkeypatch.setattr(renderd, "SLOT_WAIT_SECONDS", 0.05)
    assert rendercore._RENDER_SLOT.acquire(blocking=False)
    try:
        r = client.post(f"/v1/render?w={W}&h={H}", content=PAGE, headers=HEADERS)
    finally:
        rendercore._RENDER_SLOT.release()
    assert r.status_code == 503
    assert int(r.headers["retry-after"]) > 0
    assert browser_runs == []


# --- a render ----------------------------------------------------------------------------


def test_a_render_answers_what_the_app_accepts(client, browser_runs):
    r = client.post("/v1/render?w=1200&h=800&check=1", content=PAGE, headers=HEADERS)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    assert no_cors(r.headers)
    report = renderwire.parse_answer(r.content, 1200, 800)
    assert report[0] == png(1200, 800)
    assert report[1] == ["line 3: Uncaught ReferenceError: x is not defined"]
    # The renderer stamps nothing and makes the nonce: the page it ran is the
    # body with the check script added, nothing else.
    run = browser_runs[0]
    assert "--window-size=1200,800" in run["argv"]


def test_check_0_answers_no_check_numbers(client, monkeypatch, browser_runs):
    numbers = {"text": 5, "media": 0, "height": 640, "scroll": 900, "view": 640}
    monkeypatch.setattr(
        rendercore.renderfind, "parse_check", lambda log, nonce: dict(numbers)
    )
    with_check = client.post(
        f"/v1/render?w={W}&h={H}&check=1", content=PAGE, headers=HEADERS
    )
    without = client.post(
        f"/v1/render?w={W}&h={H}&check=0", content=PAGE, headers=HEADERS
    )
    assert renderwire.parse_answer(with_check.content, W, H).check == numbers
    assert renderwire.parse_answer(without.content, W, H).check is None


def test_no_browser_answers_not_run(client, monkeypatch, browser_runs):
    monkeypatch.setenv("REAM_BROWSER", "/nonexistent/browser")
    r = client.post(f"/v1/render?w={W}&h={H}", content=PAGE, headers=HEADERS)
    assert r.status_code == 200
    assert renderwire.parse_answer(r.content, W, H) == (None, None)
    assert browser_runs == []


# --- the work directory ----------------------------------------------------------------


def test_the_work_directory_is_wiped_at_start(tmp_path):
    root = tmp_path / "work"
    (root / "old-render" / "profile").mkdir(parents=True)
    (root / "old-render" / "page.html").write_text("<p>someone's page</p>")
    (root / "loose").write_text("x")
    assert renderd.prepare_work_root(root) == root
    assert list(root.iterdir()) == []
    assert root.stat().st_mode & 0o777 == 0o700

    # A link there is replaced, and what it pointed at is left alone.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep").write_text("k")
    root.rmdir()
    root.symlink_to(elsewhere)
    renderd.prepare_work_root(root)
    assert not root.is_symlink() and root.is_dir()
    assert (elsewhere / "keep").read_text() == "k"


def test_every_render_gets_a_new_profile_directory_removed_after(
    client, work, browser_runs
):
    for _ in range(2):
        r = client.post(f"/v1/render?w={W}&h={H}", content=PAGE, headers=HEADERS)
        assert r.status_code == 200
        # Nothing of the render is left once it has answered.
        assert list(work.iterdir()) == []
    first, second = browser_runs
    for run in browser_runs:
        # The browser's home and temporary directory, where it makes its
        # profile and cache, are the render's own directory under the work root.
        assert run["env"]["HOME"] == run["env"]["TMPDIR"] == run["cwd"]
        assert Path(run["cwd"]).parent == work
    assert first["cwd"] != second["cwd"]


def test_what_a_render_leaves_in_the_work_directory_is_removed(
    client, work, monkeypatch, browser_runs
):
    real = rendercore._run

    def leave_something(argv, **kw):
        (work / "left-by-the-browser").mkdir()
        return real(argv, **kw)

    monkeypatch.setattr(rendercore, "_run", leave_something)
    r = client.post(f"/v1/render?w={W}&h={H}", content=PAGE, headers=HEADERS)
    assert r.status_code == 200
    assert list(work.iterdir()) == []


def test_a_long_work_directory_is_refused_at_start(monkeypatch, capsys):
    for name in list(os.environ):
        if name.startswith(("REAM_", "ARTIFACTS_")) or name == "DATABASE_URL":
            monkeypatch.delenv(name)
    long = "/var/" + "x" * renderd.WORK_ROOT_MAX_CHARS
    started = []
    monkeypatch.setattr(renderd, "prepare_work_root", started.append)
    assert renderd.main(["--selfcheck", "--work-dir", long]) == renderd.EXIT_REFUSED
    assert started == []
    assert "work directory" in capsys.readouterr().err


# --- what is left after a render ------------------------------------------------------


def test_strays_are_this_uids_processes_but_itself_and_its_ancestors():
    me, uid = 50, 1000
    procs = [
        (1, 0, {uid}, "S"),  # its supervisor
        (50, 1, {uid}, "S"),  # itself
        (60, 50, {uid}, "S"),  # a child left running
        (61, 1, {uid}, "S"),  # a child that was reparented
        (62, 1, {uid}, "Z"),  # a zombie: runs nothing
        (70, 1, {0}, "S"),  # another uid
    ]
    assert renderd.strays(procs, me, uid) == [60, 61]


def test_a_process_left_after_a_render_ends_the_renderer(
    client, monkeypatch, exits, browser_runs, caplog
):
    me = os.getpid()
    monkeypatch.setattr(
        renderd,
        "_read_procs",
        lambda: [
            (me, 1, {os.getuid()}, "S"),
            (424242, 1, {os.getuid()}, "S"),
        ],
    )
    r = client.post(f"/v1/render?w={W}&h={H}", content=PAGE, headers=HEADERS)
    assert exits == [renderd.EXIT_STRAY]
    assert r.status_code == 500 and r.content == b""
    assert "424242" in caplog.text
    # The slot is free again for whatever comes next.
    assert rendercore._RENDER_SLOT.acquire(blocking=False)
    rendercore._RENDER_SLOT.release()


def test_nothing_left_means_no_exit(client, exits, browser_runs):
    r = client.post(f"/v1/render?w={W}&h={H}", content=PAGE, headers=HEADERS)
    assert r.status_code == 200
    assert exits == []


def test_the_process_reader_sees_this_process():
    mine = [p for p in renderd._read_procs() if p[0] == os.getpid()]
    assert mine and os.getuid() in mine[0][2]
    assert mine[0][1] == os.getppid()


# --- health -----------------------------------------------------------------------------


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok\n"
    assert r.headers["content-type"].startswith("text/plain")


def test_readyz(client, app, work, monkeypatch):
    monkeypatch.setattr(rendercore, "_PRLIMIT", "/usr/bin/prlimit")
    assert client.get("/readyz").status_code == 200

    monkeypatch.setenv("REAM_BROWSER", "/nonexistent/browser")
    monkeypatch.setattr(rendercore, "_PRLIMIT", None)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert "browser" in r.text and "prlimit" in r.text

    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    monkeypatch.setattr(rendercore, "_PRLIMIT", "/usr/bin/prlimit")
    work.rmdir()
    r = client.get("/readyz")
    assert r.status_code == 503 and "work directory" in r.text


def test_selfcheck_fails_without_a_picture(app, monkeypatch):
    monkeypatch.setattr(
        rendercore, "_run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0)
    )
    assert renderd.selfcheck(app) is False


# --- the real thing ---------------------------------------------------------------------


def _descendants_of_this_process():
    """The real process list, cut to this process and what it started: the
    suite shares its uid with everything else the machine runs as that user."""
    procs = _REAL_READ_PROCS()
    parent = {pid: ppid for pid, ppid, _, _ in procs}
    me = os.getpid()

    def ours(pid):
        seen = set()
        while pid and pid not in seen:
            if pid == me:
                return True
            seen.add(pid)
            pid = parent.get(pid, 0)
        return False

    return [p for p in procs if ours(p[0])]


@pytest.fixture()
def served(monkeypatch, exits):
    """renderd under uvicorn on loopback, in a thread of this process."""
    import uvicorn

    monkeypatch.setattr(renderd, "_read_procs", _descendants_of_this_process)
    # A short work directory, as in the image (`/tmp/ream-renderd`): see
    # renderd.WORK_ROOT_MAX_CHARS. pytest's own tmp_path is longer than that.
    short = Path(tempfile.mkdtemp(prefix="rd", dir="/tmp"))
    app = renderd.Renderd(renderd.prepare_work_root(short / "w"), exit=exits.append)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, lifespan="on", access_log=False, log_config=None)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        shutil.rmtree(short, ignore_errors=True)


PROBE = """<!doctype html><html><head><meta charset=utf-8><title>probe</title></head>
<body><h1>probe</h1><pre id=out></pre><script>
const target = "http://127.0.0.1:PORT/v1/render?w=900&h=640";
const page = "<!doctype html><p>asked for by a page</p>";
function note(what) {
  document.getElementById("out").textContent += what + "\\n";
  console.error("Error: probe " + what);
}
fetch(target, {method: "POST", body: page,
               headers: {"X-Ream-Render": "1", "Content-Type": "text/html"}})
  .then(r => note("fetch status " + r.status), e => note("fetch failed " + e));
fetch(target, {method: "POST", mode: "no-cors", body: page})
  .then(r => note("no-cors fetch status " + r.status + " " + r.type),
        e => note("no-cors fetch failed " + e));
const x = new XMLHttpRequest();
x.open("POST", target);
x.setRequestHeader("X-Ream-Render", "1");
x.setRequestHeader("Content-Type", "text/html");
x.onload = () => note("xhr status " + x.status);
x.onerror = () => note("xhr failed");
x.send(page);
</script></body></html>"""


@needs_browser
def test_a_rendered_page_cannot_drive_the_renderer(served, exits, caplog):
    caplog.set_level("INFO", logger="ream.renderd")
    port = served
    body = PROBE.replace("PORT", str(port)).encode()
    answer = httpx.post(
        f"http://127.0.0.1:{port}/v1/render?w={W}&h={H}&check=1",
        content=body,
        headers=HEADERS,
        timeout=60,
    )
    assert answer.status_code == 200
    report = renderwire.parse_answer(answer.content, W, H)
    assert report[0] is not None, "the probe page itself rendered"
    assert exits == [], "the browser left nothing running"

    outcomes = [e for e in report[1] if "probe " in e]
    assert outcomes, f"the page reported what its requests did: {report[1]}"
    for line in outcomes:
        assert "status 200" not in line, line

    renders = [r.getMessage() for r in caplog.records if r.name == "ream.renderd"]
    answered = [m for m in renders if m == "POST /v1/render 200"]
    assert answered == ["POST /v1/render 200"], renders
