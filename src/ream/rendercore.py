"""The browser runner: one headless screenshot of one page, and what its log says.

Moved out of `thumbs.py` so that running the browser imports nothing of the
database layer (no `ream.store`, no `ream.db`; `tests/test_rendercore.py` holds
that). `thumbs.py` keeps the store-facing parts, the background render and the
saving, and calls `screenshot` here.

What the runner guarantees, from the note that has always stood over it:
  * NO CREDENTIALS IN THE CHILD. The subprocess environment is built from
    nothing, not inherited: DATABASE_URL, the agent keys and everything else
    simply are not there to read. An inherited environment is the single most
    likely way a render becomes an exfiltration.
  * NO NETWORK. The page's own CSP already forbids external fetches, but a CSP
    is enforced by the thing we are asking to be untrusted. The flags below
    disable the network stack itself.
  * A HARD TIMEOUT AND A KILLED PROCESS GROUP. A page can loop; it must not hold
    a worker. On timeout the whole group dies, not just the parent.
  * NOTHING ON DISK IT DID NOT BRING. The render happens in a temp directory
    that is removed afterwards, whatever happened.
  * A MEMORY CEILING. A small page can build a huge DOM; the browser's memory is
    watched and its group killed past RENDER_MAX_RSS_BYTES, so it cannot take
    the pod's container down with it.
  * A FAILED RENDER PRODUCES NO THUMBNAIL. Never a stale one, never a partial
    one. The wall is allowed to have gaps; it is not allowed to show Jeremy a
    picture of a page that is not what the page says now.

`screenshot` returns no PNG for every failure, because every failure has the
same correct response and a caller that has to distinguish them is a caller
that will get one of them wrong.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from ream import renderfind, settings

# The logger keeps the name it had in thumbs.py, so the log reads as before.
log = logging.getLogger("ream.thumbs")

# Overridable so the container can say where it put the browser, and so a
# developer machine can point at Playwright's copy. Absent means no thumbnails,
# which is a degraded wall and not a broken service.
BROWSER_ENV = "REAM_BROWSER"

TIMEOUT_SECONDS = 20

# THE BROWSER'S MEMORY IS THE POD'S MEMORY. Chromium runs inside the pod's own
# memory limit, and the kernel kills the whole container when that is crossed,
# so one big render takes the gallery down for every reader. That happened on 15
# Oct 2026: a single 8 MiB publish was OOM-killed at the 1 GiB limit. Measured on
# the management machine with chrome-headless-shell, peak memory of one render of a plain table page:
# 1 MiB 442 Mi, 2 MiB 458 Mi, 3 MiB 612 Mi, 8 MiB 1511 Mi. So:
#   * no picture of a page over RENDER_MAX_BYTES: its tile is the text tile, as
#     for any failed render (runcove-5t3oj.23);
#   * one render at a time, previews included, so two big publishes in the same
#     moment cannot stack two browsers in one limit.
# The image's Debian chromium needs more: on 22 Oct 2026 in production a 1 MiB
# table rendered, and 1.5 MiB and 2 MiB tables passed the sampler's ceiling
# below (756 and 734 MB) and got no picture (runcove-5t3oj.42). So the cap is
# the largest size proven there, not one that only fit the headless shell.
RENDER_MAX_BYTES = 1 * 1024 * 1024
# The input cap bounds the HTML, not what a page builds from it: a few hundred
# bytes of script can grow a DOM far past the limit. So the browser's own memory
# is watched while it runs (PSS summed over its process group, polled every
# RSS_POLL_SECONDS) and the group is SIGKILLed past this ceiling; the render then
# fails like any other and the tile is the text tile. 700 MiB sits above a 1 MiB
# page in production and well under the 1 GiB pod limit, which also holds
# the service. The sum is PSS, which shares each page among the processes that map
# it, so it is the memory the group really adds to the pod (plain resident size
# counted chromium's shared pages once per process and read 870 MiB for a 16 KiB
# page, which killed every render). A JS heap cap was not chosen: it does not
# bound the native memory of the DOM, layout and the compositor. Sampling can miss
# a spike that grows faster than the poll, so the kernel holds a second line below.
RENDER_MAX_RSS_BYTES = 700 * 1024 * 1024
# THE KERNEL'S LINE: every browser process runs under RLIMIT_DATA (prlimit --data),
# which since Linux 4.7 counts private writable mappings, so it bounds the native
# DOM and layout memory a JS heap cap does not. Per process, not per group: the
# renderer is the one a page grows. A cgroup of the browser's own would bound the
# group, but the pod's /sys/fs/cgroup is read-only and owned by root, so it would
# need a privileged container (runcove-5t3oj.31, 22 Oct 2026). Measured on the management machine
# that day with chrome-headless-shell, one render at a time:
#   a 2 MiB table page (then the largest given a picture) fails at 640 MiB, renders
#   at 768 MiB and above, peak PSS 631-657 MiB;
#   a page whose script grows its DOM without end ran past a 1.5 GiB scope with
#   no limit, and stopped at 745 MiB PSS under 768 MiB.
# The service itself uses about 115 MiB, so one renderer at its limit plus the
# service stays under the 1 GiB pod limit; the group's sum is still the sampler's
# to bound. Measured with Playwright's headless shell, not the image's Debian
# chromium; in production a 1 MiB page renders under it (runcove-5t3oj.42). A render
# that hits the limit fails like a timeout: no picture.
RENDER_MAX_DATA_BYTES = 768 * 1024 * 1024
_PRLIMIT = shutil.which("prlimit", path="/usr/bin:/bin")
_WARNED_NO_PRLIMIT = False
RSS_POLL_SECONDS = 0.1
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
_RENDER_SLOT = threading.BoundedSemaphore(1)
# A render waits at most this long for its turn.
SLOT_WAIT_SECONDS = TIMEOUT_SECONDS + 10


def too_big_to_render(html: str) -> bool:
    return len(html) > RENDER_MAX_BYTES or len(html.encode()) > RENDER_MAX_BYTES


# The tile is rendered at this width and cropped to this height: a thumbnail is
# the TOP of a page, the part a reader would recognise, not a squashed whole
# page. A full-page shot of a long status board is an unreadable grey smear.
WIDTH = 900
HEIGHT = 640


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group. The child was started with
    start_new_session, so its pgid is its pid and every chromium helper it
    spawned (zygote, renderer, crashpad) is in it. Safe to call on a group that
    is already gone."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class RenderTooBig(subprocess.SubprocessError):
    """The browser's memory crossed RENDER_MAX_RSS_BYTES and its group was killed."""


def _stat_pgrp_and_pages(stat: str, statm: str) -> tuple[int, int]:
    """(process group, resident pages) from the text of /proc/<pid>/stat and
    /proc/<pid>/statm. The command name in stat is in parentheses and may hold
    spaces, so the fields are counted from the LAST closing parenthesis."""
    fields = stat.rsplit(")", 1)[1].split()  # fields[0] is the state, field 3
    return int(fields[2]), int(statm.split()[1])


def _pss_bytes(entry: str) -> int | None:
    """Proportional set size of /proc/<entry>, in bytes, or None if unreadable.
    PSS divides each shared page among the processes sharing it, so the sum over
    a process group is the memory the group really adds to the pod; the plain
    resident size counts chromium's shared pages once per process and read 870
    MiB for a 16 KiB page (live, 16 Oct 2026), which killed every render."""
    try:
        with open(f"/proc/{entry}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _group_rss(pgid: int) -> int:
    """Memory in bytes of every process in group `pgid` (summed PSS, falling
    back to resident pages where PSS cannot be read), 0 if there is none. A
    process that exits while being read is skipped."""
    total = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                stat = f.read()
            with open(f"/proc/{entry}/statm") as f:
                statm = f.read()
            group, pages = _stat_pgrp_and_pages(stat, statm)
        except (OSError, ValueError, IndexError):
            continue
        if group == pgid:
            pss = _pss_bytes(entry)
            total += pss if pss is not None else pages * _PAGE_SIZE
    return total


def _run(
    argv: list[str],
    *,
    timeout: float,
    max_rss: int | None = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """subprocess.run, except that the whole process GROUP dies on a timeout,
    and, given `max_rss`, when the group's memory passes it (RenderTooBig).

    subprocess.run kills only the direct child on a timeout, so chromium's
    helpers were orphaned to PID 1, which (uvicorn) never reaped them: 3,900
    zombies in two days (homelab-o88p). Here the group is SIGKILLed and the
    child is waited on, and the group is swept after a normal exit too, because
    crashpad can outlive a browser that exited cleanly."""
    proc = subprocess.Popen(  # noqa: S603 -- argv list, no shell, binary from config
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,  # its own process group, so a timeout kills all of it
        **kwargs,
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                out, err = proc.communicate(
                    timeout=min(left, RSS_POLL_SECONDS) if max_rss else left
                )
                break
            except subprocess.TimeoutExpired:
                if max_rss and (rss := _group_rss(proc.pid)) > max_rss:
                    raise RenderTooBig(f"{rss} bytes resident") from None
    except BaseException:
        _kill_group(proc)
        try:
            proc.communicate(timeout=5)  # reap the child; drain the pipes
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    _kill_group(proc)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _data_limited(argv: list[str], limit: int) -> list[str]:
    """`argv` run under the kernel's per-process data limit, when prlimit is here.
    Without it the browser still runs, guarded by the sampler alone."""
    if _PRLIMIT is None:
        global _WARNED_NO_PRLIMIT
        if not _WARNED_NO_PRLIMIT:
            _WARNED_NO_PRLIMIT = True
            log.warning("no prlimit: the browser runs with the memory sampler only")
        return argv
    return [_PRLIMIT, f"--data={limit}", "--", *argv]


def browser_path() -> str | None:
    """Where the headless browser is, or None if this deployment has none."""
    explicit = settings.current().browser
    if explicit:
        return explicit if Path(explicit).is_file() else None
    return shutil.which("chrome-headless-shell") or shutil.which("chromium")


def available() -> bool:
    return browser_path() is not None


# A console line the browser prints with --enable-logging=stderr. Debian's
# chromium 153 (the one in the image) writes, measured in the pod 24 Sep 2026:
#   [382:382:0924/111411.634413:INFO:CONSOLE:1] "Uncaught ReferenceError: chart is not defined", source: file:///tmp/p.html (1)
# Older builds wrote CONSOLE(1)]. Both are accepted. The first version of this
# pattern knew only the older shape and was proved against a tidied sample --
# it returned nothing from the real browser.
_CONSOLE = re.compile(r':CONSOLE(?:\((\d+)\)|:(\d+))\] "(.*)", source: ')
_MAX_ERRORS = 10


def console_errors(stderr: str) -> list[str]:
    """The page's uncaught script errors, from the browser's own log, as
    "line N: message". Only errors: an ordinary console.log is the author's
    business. Capped, and each line bounded, because this goes back to the
    author in an API response (homelab-ij8.72)."""
    found = []
    for match in _CONSOLE.finditer(stderr or ""):
        line, message = match.group(1) or match.group(2), match.group(3)
        if "Uncaught" in message or "Error" in message.split(":", 1)[0]:
            found.append(f"line {line}: {message[:300]}")
        if len(found) >= _MAX_ERRORS:
            break
    return found


class Report(tuple):
    """`(png or None, script errors)`, which is all most callers unpack, with
    the check script's numbers (or None: it did not report) kept alongside as
    `.check`. A tuple, so a caller and a test double that know only two things
    keep working."""

    check: dict | None

    def __new__(cls, png, errors, check=None):
        self = super().__new__(cls, (png, errors))
        self.check = check
        return self


def screenshot(
    binary: str, html: str, width: int = WIDTH, height: int = HEIGHT
) -> tuple[bytes | None, list[str] | None]:
    """Run `binary` on `html` (already stamped as a reader would see it) and
    return `(PNG or None, script errors or None)`, as a `Report` with the check
    script's numbers once the browser has run.
    `width`/`height` are the viewport, and so the shot. Never raises."""
    # The check script goes into the RENDER copy only, never the stored body.
    nonce = renderfind.new_nonce()
    html = renderfind.inject(html, nonce)

    # ignore_cleanup_errors: a straggler writing into the directory while it is
    # removed raised "Directory not empty" (homelab-o88p); the group is killed
    # first, and a leftover is then no reason to fail the render.
    with tempfile.TemporaryDirectory(
        prefix="artifacts-thumb-", ignore_cleanup_errors=True
    ) as workdir:
        page = Path(workdir) / "page.html"
        shot = Path(workdir) / "shot.png"
        try:
            page.write_text(html, encoding="utf-8")
        except (OSError, UnicodeEncodeError):
            return None, None

        argv = [
            binary,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",  # the container is the sandbox; see the note below
            "--hide-scrollbars",
            "--disable-dev-shm-usage",
            # The page must not be able to reach anything. Its own CSP says so
            # too, but a CSP is enforced by the component we are treating as
            # untrusted, so it is not the control we rely on.
            "--disable-remote-fonts",
            "--disable-background-networking",
            "--disable-sync",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-features=NetworkService,NetworkServiceInProcess",
            f"--window-size={int(width)},{int(height)}",
            # The page's console to stderr, so its script errors can be reported
            # to its author (homelab-ij8.72).
            "--enable-logging=stderr",
            "--v=0",
            # Late layout settles before the shot; without it a page with web
            # fonts or a chart renders half-built and the tile is a lie.
            "--virtual-time-budget=3000",
            f"--screenshot={shot}",
            page.as_uri(),
        ]
        if not _RENDER_SLOT.acquire(timeout=SLOT_WAIT_SECONDS):
            log.warning(
                "thumbnail render skipped: no turn within %ss", SLOT_WAIT_SECONDS
            )
            return None, None
        try:
            try:
                done = _run(
                    _data_limited(argv, RENDER_MAX_DATA_BYTES),
                    timeout=TIMEOUT_SECONDS,
                    max_rss=RENDER_MAX_RSS_BYTES,
                    # BUILT FROM NOTHING, not inherited. DATABASE_URL and every
                    # agent key are simply absent from the child. HOME and a temp
                    # dir are the only things the browser genuinely needs.
                    env={"HOME": workdir, "TMPDIR": workdir, "PATH": "/usr/bin:/bin"},
                    cwd=workdir,
                )
            finally:
                _RENDER_SLOT.release()
        except subprocess.TimeoutExpired:
            log.warning("thumbnail render timed out after %ss", TIMEOUT_SECONDS)
            return None, None
        except RenderTooBig as exc:
            log.warning("thumbnail render killed: browser memory over %s", exc)
            return None, None
        except (OSError, ValueError) as exc:
            log.warning("thumbnail render could not start: %s", exc)
            return None, None
        raw = getattr(done, "stderr", b"") or b""
        log_text = (
            raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        )
        errors = console_errors(log_text)
        seen = {e.split(": ", 1)[-1] for e in errors}
        errors += [
            e
            for e in renderfind.parse_errors(log_text, nonce)
            if e.split(": ", 1)[-1] not in seen
        ][: _MAX_ERRORS - len(errors)]
        check = renderfind.parse_check(log_text, nonce)

        try:
            data = shot.read_bytes()
        except OSError:
            log.warning("thumbnail render produced no file")
            return Report(None, errors, check)

    # A real PNG or nothing. A zero-byte or truncated file would otherwise be
    # served as a broken image, which looks like the PAGE is broken.
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        log.warning("thumbnail render produced %d bytes that are not a PNG", len(data))
        return Report(None, errors, check)
    return Report(data, errors, check)
