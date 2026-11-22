"""Rendering a page's picture, with the browser treated as untrusted.

homelab-ij8.22. Jeremy chose (b) — the renderer inside the service itself —
over a separate renderer, with the argument against in front of him. So the
execution of agent-written HTML happens in the service's own pod, and what
makes that acceptable is not care in the calling code but the constraints the
child runs under. Those constraints are what this file tests; everything else
about thumbnails is decoration by comparison.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# The fake browser on its DevTools pipe (tests/test_shots.py), for the render's envelope.
from test_shots import fake  # noqa: F401

from ream import rendercore, thumbs

# The real background render: tests elsewhere have it switched off.
pytestmark = pytest.mark.background_thumbnails

PAGE = (
    "<!doctype html><html><head><title>T</title>"
    "<style>body{background:#fff;font:16px sans-serif}</style></head>"
    "<body><h1>Hello</h1><p>A real page.</p></body></html>"
)

needs_browser = pytest.mark.skipif(
    not thumbs.available(),
    reason="no headless browser in this environment; the unit tests below still run",
)


# --- the constraints, which do not need a browser to verify ------------------


def test_the_child_gets_no_credentials(monkeypatch):
    """THE ONE THAT MATTERS. An inherited environment is the likeliest way a
    render becomes an exfiltration: DATABASE_URL carries the database password,
    and the pod's environment is right there for the taking. The child's
    environment is built from nothing, and this asserts it stays that way."""
    seen = {}

    def capture(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(argv, 1)  # stop before doing any work

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:hunter2@db/artifacts")
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    monkeypatch.setattr(rendercore, "_render_run", capture)

    assert thumbs.render(PAGE) is None
    env = seen["env"]
    assert "DATABASE_URL" not in env
    assert not any("hunter2" in str(v) for v in env.values())
    assert set(env) <= {"HOME", "TMPDIR", "PATH"}, f"unexpected inherited vars: {env}"


def test_the_child_is_its_own_process_group(monkeypatch):
    """The timeout is handed to the group-killing runner (the real kill is
    tested below with a real process tree)."""
    seen = {}
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    monkeypatch.setattr(
        rendercore,
        "_render_run",
        lambda argv, **kw: seen.update(kw) or subprocess.CompletedProcess(argv, 0),
    )
    thumbs.render(PAGE)
    assert seen["timeout"] == thumbs.TIMEOUT_SECONDS


def test_no_browser_means_no_thumbnail_not_an_error(monkeypatch):
    """A deployment without a browser has a wall with gaps, not a broken
    service. Publishing must never depend on this."""
    monkeypatch.setenv("REAM_BROWSER", "/nonexistent/browser")
    assert thumbs.available() is False
    assert thumbs.render(PAGE) is None


def test_a_timeout_produces_nothing_rather_than_raising(monkeypatch):
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    monkeypatch.setattr(
        rendercore,
        "_render_run",
        lambda argv, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 1)),
    )
    assert thumbs.render(PAGE) is None


def test_output_that_is_not_a_png_is_refused(monkeypatch, tmp_path):
    """A truncated or empty file would be served as a broken image, which looks
    to a reader like the PAGE is broken rather than the thumbnail."""
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")

    def write_rubbish(argv, **kw):
        shot = kw["shot"]
        Path(shot).write_bytes(b"not a png at all")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(rendercore, "_render_run", write_rubbish)
    assert thumbs.render(PAGE) is None


def test_the_flags_disable_the_network(monkeypatch):
    """The page's own CSP forbids external fetches, but a CSP is enforced by the
    component being treated as untrusted, so it is not the control relied on."""
    seen = {}
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    monkeypatch.setattr(
        rendercore,
        "_render_run",
        lambda argv, **kw: (
            seen.setdefault("argv", argv) or subprocess.CompletedProcess(argv, 0)
        ),
    )
    thumbs.render(PAGE)
    flags = " ".join(str(a) for a in seen["argv"])
    assert "NetworkService" in flags
    assert "--disable-background-networking" in flags
    assert "--disable-remote-fonts" in flags


def test_the_browser_runs_under_a_kernel_data_limit(monkeypatch):
    """The sampler can miss a page that grows faster than it polls; a hostile
    page grew past the 1.5 GiB of a test scope with no limit (22 Oct 2026). The
    kernel's per-process data limit stops it, and the sampler stays as well."""
    seen = {}
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    monkeypatch.setattr(rendercore, "_PRLIMIT", "/usr/bin/prlimit")
    monkeypatch.setattr(
        rendercore,
        "_render_run",
        lambda argv, **kw: (
            seen.setdefault("argv", argv) or subprocess.CompletedProcess(argv, 0)
        ),
    )
    thumbs.render(PAGE)
    argv = [str(a) for a in seen["argv"]]
    assert argv[:3] == [
        "/usr/bin/prlimit",
        f"--data={thumbs.RENDER_MAX_DATA_BYTES}",
        "--",
    ]
    # The browser starts through the shell line that moves its pipe into place.
    assert argv[3:7] == [rendercore._SHELL, "-c", rendercore._PIPE_SHIM, "/bin/true"]


def test_without_prlimit_the_browser_still_runs(monkeypatch):
    seen = {}
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    monkeypatch.setattr(rendercore, "_PRLIMIT", None)
    monkeypatch.setattr(
        rendercore,
        "_render_run",
        lambda argv, **kw: (
            seen.setdefault("argv", argv) or subprocess.CompletedProcess(argv, 0)
        ),
    )
    thumbs.render(PAGE)
    assert [str(a) for a in seen["argv"][:4:3]] == [rendercore._SHELL, "/bin/true"]


@pytest.mark.skipif(thumbs._PRLIMIT is None, reason="no prlimit here")
def test_the_data_limit_is_enforced_by_the_kernel():
    grow = [sys.executable, "-c", "b = bytearray(256 << 20); b[-1] = 1"]
    first = subprocess.run(grow, capture_output=True, timeout=30, check=False)  # noqa: S603 -- fixed argv
    assert first.returncode == 0
    limited = thumbs._data_limited(grow, 64 << 20)
    done = subprocess.run(limited, capture_output=True, timeout=30, check=False)  # noqa: S603 -- fixed argv
    # The limit, and not prlimit or python failing to start, is what stopped it.
    assert done.returncode != 0
    assert b"MemoryError" in done.stderr


# --- with a real browser, when there is one ----------------------------------


@needs_browser
def test_a_real_page_renders_to_a_real_png(render_mode):
    png = thumbs.render(PAGE)
    assert png is not None
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 1000, "a PNG this small rendered nothing"


@needs_browser
def test_a_page_that_never_settles_still_returns(render_mode):
    """A page can loop. It must not hold a worker: the virtual time budget ends
    the render whatever the page is doing."""
    spinning = (
        "<!doctype html><html><body><script>"
        "while(false){} setInterval(function(){document.title=Date.now()},1);"
        "</script><p>busy</p></body></html>"
    )
    png = thumbs.render(spinning)
    assert png is None or png.startswith(b"\x89PNG\r\n\x1a\n")


def test_the_browser_sees_the_page_stamped_as_a_reader_would(monkeypatch):
    """The first live preview (24 Sep 2026) read "DECISION · · BABA": the date
    marker is filled in when a page is served, and the renderer was handed the
    stored body. Every wall tile had the same gap."""
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    seen = {}

    def read_page(argv, **kw):
        seen["html"] = Path(kw["page_url"].removeprefix("file://")).read_text()
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(rendercore, "_render_run", read_page)
    thumbs.render_report("<p>DECISION · <!--artifacts:updated--> · BABA</p>")
    assert "<!--artifacts:updated-->" not in seen["html"]
    assert "· ·" not in seen["html"]


# --- zombies: the whole group dies, and a failing page stops being retried ----
# homelab-o88p: 3,900 zombie chromium processes in two days, 232 temp dirs.


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie answers kill(0); read its state instead
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _render_with(binary, tmp_path, **kw):
    """One render's run (`rendercore._render_run`, the path `thumbs.render` takes)
    of a fake browser, through the shell line that moves its pipe into place."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    argv = [rendercore._SHELL, "-c", rendercore._PIPE_SHIM, binary]
    run = {
        "page_url": (tmp_path / "page.html").as_uri(),
        "shot": work / "shot.png",
        "width": 8,
        "height": 8,
        "timeout": 30,
        "max_rss": 1 << 40,
        "env": {"HOME": str(work), "TMPDIR": str(work), "PATH": "/usr/bin:/bin"},
        "cwd": str(work),
    }
    return rendercore._render_run(argv, **{**run, **kw})


def _child_of(read):
    return next(line["child"] for line in read() if "child" in line)


def _ended(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _alive(pid)


def test_a_timeout_kills_the_whole_process_group(fake, tmp_path):
    """The browser forks a helper that would outlive it. After the timeout
    neither may be left running (subprocess.run killed only the direct child)."""
    binary, read = fake("hang")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _render_with(binary, tmp_path, timeout=1.5)
    assert time.monotonic() - started < 10
    assert _ended(_child_of(read)), "the helper survived the timeout"


def test_stat_and_statm_are_read_whatever_the_command_is_called():
    stat = "42 (we ird) name) S 1 777 777 0 -1 4194560 100"
    assert thumbs._stat_pgrp_and_pages(stat, "5000 1234 10 1 0 900 0") == (777, 1234)


def test_pss_is_read_for_a_live_process():
    # smaps_rollup is readable for our own process, and PSS never exceeds RSS
    pss = thumbs._pss_bytes(str(os.getpid()))
    assert pss is not None and pss > 0
    assert thumbs._pss_bytes("999999999") is None


def test_group_rss_counts_only_the_group():
    """The decision input, with no browser: a live process group reports some
    memory, a group that does not exist reports none."""
    proc = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        assert thumbs._group_rss(proc.pid) > 0
        assert thumbs._group_rss(2**22 + 12345) == 0
    finally:
        proc.kill()
        proc.wait()


def test_a_render_over_the_memory_ceiling_is_ended_with_its_group(
    fake, tmp_path, monkeypatch
):
    """The render's own watch (`_on_pipe`'s `_Watch`) ends a browser past the
    ceiling, helpers and all, as memory and not as a timeout."""
    binary, read = fake("hang")
    # The ceiling is read only once the browser has noted its helper: on a loaded runner
    # the first 0.1 s poll could end the group before that, leaving no pid to check
    # (CI run on e991163, 3 Nov 2026). Before that the group reads as using nothing.
    real = rendercore._group_rss
    monkeypatch.setattr(
        rendercore,
        "_group_rss",
        lambda pid: real(pid) if any("child" in line for line in read()) else 0,
    )
    started = time.monotonic()
    with pytest.raises(thumbs.RenderTooBig):
        _render_with(binary, tmp_path, max_rss=1)
    assert time.monotonic() - started < 10
    assert _ended(_child_of(read)), "the group survived the memory ceiling"


def test_a_render_under_the_ceiling_is_left_alone(fake, tmp_path):
    binary, _ = fake()
    done = _render_with(binary, tmp_path)
    assert done.returncode == 0
    assert (tmp_path / "work" / "shot.png").read_bytes().startswith(b"\x89PNG")


@pytest.mark.parametrize("mode", ["frames_stall", "frames_refused"])
def test_a_frame_wait_that_fails_still_leaves_the_picture(
    fake, tmp_path, monkeypatch, mode
):
    """A render never loses a picture to the frame wait (`rendercore._await_frames`):
    unanswered within its bound, or refused, the picture is taken as before."""
    monkeypatch.setattr(rendercore, "FRAMES_REAL_SECONDS", 0.3)
    binary, read = fake(mode)
    started = time.monotonic()
    done = _render_with(binary, tmp_path)
    assert done.returncode == 0
    assert time.monotonic() - started < 10
    assert (tmp_path / "work" / "shot.png").read_bytes().startswith(b"\x89PNG")
    assert any(line.get("method") == "Page.captureScreenshot" for line in read())


def test_an_over_long_event_during_the_capture_leaves_no_picture(
    fake, tmp_path, monkeypatch
):
    # Not the picture's own answer: no picture, nothing raised, nothing "too big".
    monkeypatch.setattr(rendercore, "RENDER_MESSAGE_MAX_BYTES", 4096)
    binary, _ = fake("huge_event", pad=8192)
    done = _render_with(binary, tmp_path)
    assert done.returncode == 0
    assert not (tmp_path / "work" / "shot.png").exists()


def test_an_unexpected_error_in_a_render_leaves_no_picture(fake, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise OverflowError("cannot convert float infinity to integer")

    monkeypatch.setattr(rendercore, "_render_converse", broken)
    binary, _ = fake()
    done = _render_with(binary, tmp_path)
    assert done.returncode == 0
    assert not (tmp_path / "work" / "shot.png").exists()


def test_a_render_that_blows_the_memory_ceiling_produces_nothing(monkeypatch):
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")

    def blow(argv, **kw):
        assert kw["max_rss"] == thumbs.RENDER_MAX_RSS_BYTES
        raise thumbs.RenderTooBig("9 bytes resident")

    monkeypatch.setattr(rendercore, "_render_run", blow)
    assert thumbs.render_report(PAGE) == (None, None)


def test_a_normal_render_sweeps_stragglers(fake, tmp_path):
    """crashpad can outlive a browser that finished cleanly."""
    binary, read = fake("child")
    assert _render_with(binary, tmp_path).returncode == 0
    assert (tmp_path / "work" / "shot.png").exists()
    assert _ended(_child_of(read))


def test_the_temp_dir_is_gone_after_a_timeout(monkeypatch):
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    seen = {}

    def hang(argv, **kw):
        seen["dir"] = Path(kw["cwd"])
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(rendercore, "_render_run", hang)
    assert thumbs.render(PAGE) is None
    assert not seen["dir"].exists()


@pytest.fixture
def clean_failures():
    thumbs._failures.clear()
    yield
    thumbs._failures.clear()


def _count_renders(monkeypatch, png):
    calls = []

    def fake(html, *a, **kw):
        calls.append(1)
        return png, None

    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    monkeypatch.setattr(thumbs, "render_report", fake)
    monkeypatch.setattr(thumbs.store, "save_thumbnail", lambda *a: True)
    monkeypatch.setattr(thumbs.store, "save_render_errors", lambda *a: True)
    monkeypatch.setattr(thumbs.threading, "Thread", _InlineThread)
    return calls


class _InlineThread:
    def __init__(self, target, **kw):
        self.target = target

    def start(self):
        self.target()


def test_a_failing_version_is_retried_with_backoff_then_given_up(
    monkeypatch, clean_failures, caplog
):
    calls = _count_renders(monkeypatch, None)
    clock = [1000.0]
    monkeypatch.setattr(thumbs.time, "monotonic", lambda: clock[0])

    thumbs.render_in_background(None, "a1", 1, PAGE)
    assert len(calls) == 1
    thumbs.render_in_background(None, "a1", 1, PAGE)  # inside the backoff
    assert len(calls) == 1
    for expected in (2, 3):
        clock[0] += thumbs.BACKOFF_SECONDS * 4
        with caplog.at_level("WARNING", logger="ream.thumbs"):
            thumbs.render_in_background(None, "a1", 1, PAGE)
        assert len(calls) == expected
    clock[0] += 10**6  # however long we wait, it is over
    for _ in range(3):
        thumbs.render_in_background(None, "a1", 1, PAGE)
    assert len(calls) == thumbs.MAX_FAILURES
    assert sum("giving up" in r.message for r in caplog.records) == 1


def test_a_new_version_gets_its_own_attempts(monkeypatch, clean_failures):
    calls = _count_renders(monkeypatch, None)
    thumbs._failures[("a1", 1)] = (thumbs.MAX_FAILURES, 0.0)
    thumbs.render_in_background(None, "a1", 1, PAGE)
    assert calls == []
    thumbs.render_in_background(None, "a1", 2, PAGE)
    assert len(calls) == 1


def test_a_success_clears_the_failure_count(monkeypatch, clean_failures):
    _count_renders(monkeypatch, b"\x89PNG\r\n\x1a\n" + b"x" * 10)
    thumbs._failures[("a1", 1)] = (2, 0.0)
    thumbs.render_in_background(None, "a1", 1, PAGE)
    assert ("a1", 1) not in thumbs._failures


# --- the browser's memory is the pod's (runcove-5t3oj.23) --------------------


def _no_browser_may_start(argv, **kw):
    raise AssertionError("a browser was started for a page over the render cap")


def test_a_page_over_the_render_cap_starts_no_browser(monkeypatch):
    """15 Oct 2026: one 8 MiB render took the pod past 1 GiB and the kernel
    killed it. A page over the cap gets the text tile, never a browser."""
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    monkeypatch.setattr(rendercore, "_render_run", _no_browser_may_start)
    big = "<p>" + "x" * thumbs.RENDER_MAX_BYTES + "</p>"
    assert thumbs.render_report(big) == (None, None)


def test_the_cap_counts_bytes_not_characters(monkeypatch):
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    monkeypatch.setattr(rendercore, "_render_run", _no_browser_may_start)
    # Under the cap in characters, over it in UTF-8 bytes.
    big = "é" * (thumbs.RENDER_MAX_BYTES // 2 + 1)
    assert len(big) < thumbs.RENDER_MAX_BYTES < len(big.encode())
    assert thumbs.render_report(big) == (None, None)


def test_a_page_over_the_cap_queues_no_background_render(monkeypatch):
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")

    def no_thread(*a, **kw):
        raise AssertionError("a render thread was started for a page over the cap")

    monkeypatch.setattr(thumbs.threading, "Thread", no_thread)
    big = "<p>" + "x" * thumbs.RENDER_MAX_BYTES + "</p>"
    thumbs.render_in_background(None, "id", 1, big)


def test_renders_take_turns(monkeypatch):
    """Two browsers at once would stack in one memory limit: the browser runs
    only while holding the one render slot."""
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    held = []

    def check_slot(argv, **kw):
        free = thumbs._RENDER_SLOT.acquire(blocking=False)
        if free:
            thumbs._RENDER_SLOT.release()
        held.append(not free)
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(rendercore, "_render_run", check_slot)
    assert thumbs.render(PAGE) is None
    assert held == [True]
    # and released afterwards, even on a timeout
    assert thumbs._RENDER_SLOT.acquire(blocking=False)
    thumbs._RENDER_SLOT.release()


def test_a_burst_of_publishes_parks_a_bounded_number_of_renders(monkeypatch):
    """One render at a time must not become a queue without end: past the
    background slots, a page gets no picture this time rather than a thread."""
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    taken = 0
    while thumbs._BACKGROUND_SLOTS.acquire(blocking=False):
        taken += 1
    try:
        assert taken == 4

        def no_thread(*a, **kw):
            raise AssertionError("a render thread was started past the bound")

        monkeypatch.setattr(thumbs.threading, "Thread", no_thread)
        thumbs.render_in_background(None, "burst", 1, PAGE)
    finally:
        for _ in range(taken):
            thumbs._BACKGROUND_SLOTS.release()


def test_a_render_that_gets_no_turn_produces_nothing(monkeypatch):
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    monkeypatch.setattr(rendercore, "SLOT_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(rendercore, "_render_run", _no_browser_may_start)
    assert thumbs._RENDER_SLOT.acquire(blocking=False)
    try:
        assert thumbs.render_report(PAGE) == (None, None)
    finally:
        thumbs._RENDER_SLOT.release()
