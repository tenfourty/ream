"""The renderer as its own process (`ream.renderd`).

Most of these call the ASGI app directly, with a fake browser run
(`rendercore._run`) and a fake process list; the last ones start the real
command (`python -m ream.renderd`) as a process of its own, on loopback, with a
real browser.
"""

import asyncio
import io
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
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
    monkeypatch.setattr(renderd, "STRAY_CONFIRM_SECONDS", 0)
    me, uid = os.getpid(), os.getuid()
    monkeypatch.setattr(
        renderd, "_read_procs", lambda: [(1, 0, "S", 0), (me, 1, "S", uid)]
    )
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


def test_leftovers_are_live_descendants_of_this_process_only():
    me, uid = 50, 1000
    procs = [
        (1, 0, "S", 0),  # its supervisor
        (40, 1, "S", uid),  # its parent
        (50, 40, "S", uid),  # itself
        (60, 50, "S", uid),  # a child left running
        (61, 60, "S", uid),  # ...and that child's child
        (62, 50, "S", uid),  # an orphaned grandchild, reparented to this subreaper
        (63, 50, "Z", uid),  # a zombie: runs nothing
        (70, 1, "S", uid),  # same uid, not started by it: an exec probe, a shell
        (71, 70, "S", uid),  # ...and what that started
        (80, 40, "S", uid),  # a sibling under the same parent
    ]
    assert renderd.leftovers(procs, me, uid) == [60, 61, 62]
    assert renderd.leftovers([(1, 0, "S", 0), (50, 1, "S", uid)], me, uid) == []


def test_a_process_of_its_uid_whose_parent_went_mid_read_is_a_leftover():
    # The list is read entry by entry: a browser helper read just before its
    # parent exited still names that parent, which was listed but gone by the
    # time its own entry was read.
    me, uid = 50, 1000
    gone = renderd.GONE
    procs = [
        (1, 0, "S", 0),
        (50, 1, "S", uid),
        (77, -1, gone, -1),  # listed, gone before it was read
        (90, 77, "S", uid),  # its child, of this uid: a leftover
        (91, 77, "S", 0),  # another uid's: not this process's concern
        (92, 77, "Z", uid),  # a zombie: runs nothing
        (93, 0, "S", uid),  # parent 0: a namespace's first process
        (94, 1490, "S", uid),  # parent never listed (hidepid hides it): not ours
    ]
    assert renderd.leftovers(procs, me, uid) == [90]


def test_an_ancestor_whose_parent_went_mid_read_is_not_a_leftover():
    me, uid = 50, 1000
    procs = [(30, -1, renderd.GONE, -1), (40, 30, "S", uid), (50, 40, "S", uid)]
    assert renderd.leftovers(procs, me, uid) == []


def _fake_proc(monkeypatch, failures):
    """`_read_procs` over a /proc listing this process and the pids of `failures`,
    whose status reads raise the exception given (or answer the text given)."""
    real_open = open

    def fake_listdir(path):
        assert path == "/proc"
        return ["self", *map(str, failures), str(os.getpid())]

    def fake_open(path, *a, **kw):
        for pid, outcome in failures.items():
            if path == f"/proc/{pid}/status":
                if isinstance(outcome, BaseException):
                    raise outcome
                return io.StringIO(outcome)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(renderd.os, "listdir", fake_listdir)
    monkeypatch.setattr("builtins.open", fake_open)
    return renderd._read_procs()


def test_the_process_reader_lists_an_entry_gone_mid_read_as_gone(monkeypatch):
    procs = _fake_proc(
        monkeypatch,
        {
            424246: FileNotFoundError("/proc/424246/status"),
            424247: ProcessLookupError(),
        },
    )
    assert (424246, -1, renderd.GONE, -1) in procs
    assert (424247, -1, renderd.GONE, -1) in procs
    assert [p[0] for p in procs] == [424246, 424247, os.getpid()]


def test_the_process_reader_lists_an_entry_it_may_not_read_as_unreadable(
    monkeypatch,
):
    procs = _fake_proc(
        monkeypatch,
        {424248: PermissionError("/proc/424248/status"), 424249: "Name:\tx\n"},
    )
    assert (424248, -1, renderd.UNREADABLE, -1) in procs
    assert (424249, -1, renderd.UNREADABLE, -1) in procs


def test_a_child_of_an_unreadable_parent_is_not_a_leftover():
    # hidepid, or another uid's entry: the parent is alive, only not readable.
    me, uid = 50, 1000
    procs = [
        (1, 0, "S", 0),
        (50, 1, "S", uid),
        (77, -1, renderd.UNREADABLE, -1),
        (90, 77, "S", uid),
    ]
    assert renderd.leftovers(procs, me, uid) == []


def test_one_empty_list_is_not_enough_to_call_a_render_clean(monkeypatch):
    # The first list misses the leftover (its parent went mid-read and it was
    # read as nobody's); the second, read a moment later, shows it.
    me, uid = os.getpid(), os.getuid()
    base = [(1, 0, "S", 0), (me, 1, "S", uid)]
    lists = iter([base, base + [(424245, me, "S", uid)]])
    slept = []
    monkeypatch.setattr(renderd, "STRAY_GRACE_SECONDS", 0)
    monkeypatch.setattr(renderd, "_read_procs", lambda: next(lists))
    monkeypatch.setattr(renderd.time, "sleep", slept.append)
    assert renderd._strays_after_render() == [424245]
    assert slept == [renderd.STRAY_CONFIRM_SECONDS]


def test_two_empty_lists_apart_call_a_render_clean(monkeypatch):
    me, uid = os.getpid(), os.getuid()
    base = [(1, 0, "S", 0), (me, 1, "S", uid)]
    reads = []

    def read():
        reads.append(1)
        return base

    slept = []
    monkeypatch.setattr(renderd, "STRAY_GRACE_SECONDS", 0)
    monkeypatch.setattr(renderd, "_read_procs", read)
    monkeypatch.setattr(renderd.time, "sleep", slept.append)
    assert renderd._strays_after_render() == []
    assert len(reads) == 2
    assert slept == [renderd.STRAY_CONFIRM_SECONDS]
    assert renderd.STRAY_CONFIRM_SECONDS == pytest.approx(0.1)


def _after_a_render_with(client, monkeypatch, procs):
    me, uid = os.getpid(), os.getuid()
    listed = [
        (1, 0, "S", 0),
        (me, 1, "S", uid),
        *((pid, me if ppid == "me" else ppid, st, uid) for pid, ppid, st in procs),
    ]
    monkeypatch.setattr(renderd, "_read_procs", lambda: listed)
    return client.post(f"/v1/render?w={W}&h={H}", content=PAGE, headers=HEADERS)


def test_a_process_left_after_a_render_ends_the_renderer(
    client, monkeypatch, exits, browser_runs, caplog
):
    r = _after_a_render_with(client, monkeypatch, [(424242, "me", "S")])
    assert exits == [renderd.EXIT_STRAY]
    assert r.status_code == 500 and r.content == b""
    assert "424242" in caplog.text
    # The slot is free again for whatever comes next.
    assert rendercore._RENDER_SLOT.acquire(blocking=False)
    rendercore._RENDER_SLOT.release()


def test_an_orphaned_grandchild_ends_the_renderer(
    client, monkeypatch, exits, browser_runs, caplog
):
    # The browser (424240) is gone; its helper, orphaned, was reparented to this
    # process because it is a subreaper.
    r = _after_a_render_with(client, monkeypatch, [(424241, "me", "S")])
    assert exits == [renderd.EXIT_STRAY]
    assert r.status_code == 500
    assert "424241" in caplog.text


def test_a_process_of_the_same_uid_it_did_not_start_is_not_a_leftover(
    client, monkeypatch, exits, browser_runs
):
    # An exec probe in the pod, or the user's other processes on a shared
    # machine: same uid, not descended from the renderer.
    r = _after_a_render_with(
        client, monkeypatch, [(424243, 1, "S"), (424244, 424243, "S")]
    )
    assert r.status_code == 200
    assert exits == []


def test_nothing_left_means_no_exit(client, exits, browser_runs):
    r = client.post(f"/v1/render?w={W}&h={H}", content=PAGE, headers=HEADERS)
    assert r.status_code == 200
    assert exits == []


def test_the_process_reader_sees_this_process():
    mine = [p for p in renderd._read_procs() if p[0] == os.getpid()]
    assert mine and mine[0][1] == os.getppid() and mine[0][3] == os.getuid()


SUBREAPER_CHECK = """
import os, subprocess, sys
from ream import renderd
renderd.set_subreaper()
assert renderd.is_subreaper()
# The shell starts a sleep and exits at once: the sleep, orphaned, is
# reparented to this process, so it is a leftover of this process's.
pid = int(subprocess.run(["sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"], capture_output=True,
                         text=True, check=True).stdout)
try:
    procs = renderd._read_procs()
    assert dict((p[0], p[1]) for p in procs)[pid] == os.getpid()
    assert pid in renderd.leftovers(procs, os.getpid(), os.getuid())
finally:
    os.kill(pid, 9)
    os.waitpid(pid, 0)
print("ok")
"""


def test_it_can_be_a_subreaper_here():
    """The real prctl call, in a process of its own (this one is left as it is)."""
    done = subprocess.run(  # noqa: S603 -- argv list, no shell
        [sys.executable, "-c", SUBREAPER_CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


def test_it_refuses_to_start_when_it_cannot_be_a_subreaper(
    monkeypatch, capsys, tmp_path
):
    for name in list(os.environ):
        if name.startswith(("REAM_", "ARTIFACTS_")) or name == "DATABASE_URL":
            monkeypatch.delenv(name)

    def refused(on=True):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(renderd, "set_subreaper", refused)
    started = []
    monkeypatch.setattr(renderd, "prepare_work_root", started.append)
    short = tempfile.mkdtemp(prefix="rd", dir="/tmp")
    try:
        code = renderd.main(["--port", "1", "--work-dir", short + "/w"])
    finally:
        shutil.rmtree(short, ignore_errors=True)
    assert code == renderd.EXIT_REFUSED
    assert started == []


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


_LISTENING = re.compile(r"Uvicorn running on http://127\.0\.0\.1:(\d+) ")


@pytest.fixture()
def served():
    """`python -m ream.renderd` as a process of its own on loopback, on a port it
    picks itself (`--port 0`): the fixture reads the port from its start-up line,
    so there is no window in which another process can take it. Yields `(port,
    process, lines)`; `lines` holds all of its log once the test is done with it."""
    # A short work directory, as in the image (`/tmp/ream-renderd`): see
    # renderd.WORK_ROOT_MAX_CHARS. pytest's own tmp_path is longer than that.
    short = Path(tempfile.mkdtemp(prefix="rd", dir="/tmp"))
    # Only what it takes: it refuses to start with anything else of the app's.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
        "REAM_BROWSER": rendercore.browser_path() or "",
    }
    proc = subprocess.Popen(  # noqa: S603 -- argv list, no shell
        [
            sys.executable,
            "-m",
            "ream.renderd",
            "--port",
            "0",
            "--work-dir",
            str(short / "w"),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: list[str] = []
    seen: queue.Queue = queue.Queue()

    def read():
        for line in proc.stderr:
            lines.append(line)
            seen.put(line)
        seen.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    port = None
    try:
        while port is None:
            line = seen.get(timeout=30)
            assert line is not None, f"renderd ended before listening: {lines}"
            if m := _LISTENING.search(line):
                port = int(m.group(1))
        yield port, proc, lines
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        # Its stderr is at its end once it has exited: every line is in.
        reader.join(15)
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
def test_a_rendered_page_cannot_drive_the_renderer(served):
    port, proc, lines = served
    body = PROBE.replace("PORT", str(port)).encode()
    answer = httpx.post(
        f"http://127.0.0.1:{port}/v1/render?w={W}&h={H}&check=1",
        content=body,
        headers=HEADERS,
        timeout=60,
    )
    assert answer.status_code == 200, lines
    report = renderwire.parse_answer(answer.content, W, H)
    assert report[0] is not None, "the probe page itself rendered"

    outcomes = [e for e in report[1] if "probe " in e]
    assert outcomes, f"the page reported what its requests did: {report[1]}"
    for line in outcomes:
        assert "status 200" not in line, line

    # Still up: the browser left nothing running. Asked over HTTP, so this is
    # after the render's check has finished, not a race with it.
    assert httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=10).status_code == 200
    assert proc.poll() is None, lines

    # Stop it, so that all of its log is in, then read what it answered.
    proc.terminate()
    proc.wait(15)
    served_lines = [l for l in lines if " ream.renderd " in l]
    assert not [l for l in served_lines if "left after a render" in l], served_lines
    answered = [l for l in served_lines if l.rstrip().endswith("POST /v1/render 200")]
    assert len(answered) == 1, served_lines
