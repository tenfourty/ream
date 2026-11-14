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

import binascii
import json
import logging
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

from ream import devtools, renderfind, renderwire, settings
from ream.renderwire import Report

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
# A control character that stands for space (tab, line and page breaks) becomes a
# space in an error's text; any other one (C0, DEL, C1), and a lone surrogate,
# becomes U+FFFD.
_SPACELIKE = frozenset("\t\n\v\f\r")


def clean_error_text(text: str) -> str:
    """`text` with every control character (Unicode category Cc) and every lone
    surrogate replaced, one for one, so it passes `renderwire.check_error_text`
    and keeps its length. A page's own error message is the page's to choose, and
    it stays a finding of that page, cleaned, rather than making the whole render
    one that did not run."""
    out = []
    for c in text:
        if c in _SPACELIKE:
            out.append(" ")
        elif unicodedata.category(c) in ("Cc", "Cs"):
            out.append("\ufffd")
        else:
            out.append(c)
    return "".join(out)


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


def _isolation_flags() -> list[str]:
    """The browser's flags every run takes, a render's and a screenshot's alike:
    headless, no extensions, nothing fetched of its own accord, no network."""
    return [
        "--headless",
        "--disable-gpu",
        "--no-sandbox",  # the container is the sandbox
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
    ]


def _read_shot(path: Path) -> bytes:
    """The screenshot's bytes, at most one past `renderwire.PNG_MAX_BYTES` (so a
    larger one is refused as over the limit). Opened without following a symlink
    and without waiting on a FIFO, and read only if it is a regular file; OSError
    otherwise."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("the screenshot is not a regular file")
        want = renderwire.PNG_MAX_BYTES + 1
        parts = []
        while want > 0:
            part = os.read(fd, min(want, 1024 * 1024))
            if not part:
                break
            parts.append(part)
            want -= len(part)
        return b"".join(parts)
    finally:
        os.close(fd)


def screenshot(
    binary: str,
    html: str,
    width: int = WIDTH,
    height: int = HEIGHT,
    *,
    work_root: str | os.PathLike | None = None,
    slot_held: bool = False,
) -> tuple[bytes | None, list[str] | None]:
    """Run `binary` on `html` (already stamped as a reader would see it) and
    return `(PNG or None, script errors or None)`, as a `Report` with the check
    script's numbers once the browser has run.
    `width`/`height` are the viewport, and so the shot. Never raises.

    The render's directory (the page, the shot, and the browser's profile and
    cache) is made fresh under `work_root` (default: the system's temporary
    directory) and removed afterwards. `slot_held` says the caller already holds
    `_RENDER_SLOT` (`ream.renderd`, which answers "busy" itself), so it is not
    taken again here."""
    # The check script goes into the RENDER copy only, never the stored body.
    nonce = renderfind.new_nonce()
    html = renderfind.inject(html, nonce)

    # ignore_cleanup_errors: a straggler writing into the directory while it is
    # removed raised "Directory not empty" (homelab-o88p); the group is killed
    # first, and a leftover is then no reason to fail the render.
    with tempfile.TemporaryDirectory(
        prefix="artifacts-thumb-", dir=work_root, ignore_cleanup_errors=True
    ) as workdir:
        page = Path(workdir) / "page.html"
        shot = Path(workdir) / "shot.png"
        try:
            page.write_text(html, encoding="utf-8")
        except (OSError, UnicodeEncodeError):
            return None, None

        argv = [
            binary,
            *_isolation_flags(),
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
        if not slot_held and not _RENDER_SLOT.acquire(timeout=SLOT_WAIT_SECONDS):
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
                    # dir are the only things the browser genuinely needs. Both are
                    # this render's directory, so the browser's profile (a scoped
                    # directory under TMPDIR) and its cache (under HOME) are made
                    # fresh here and removed with it. With an explicit
                    # --user-data-dir, every render by Playwright's chrome ran into
                    # the timeout (13 Nov 2026), so the browser picks the place.
                    env={"HOME": workdir, "TMPDIR": workdir, "PATH": "/usr/bin:/bin"},
                    cwd=workdir,
                )
            finally:
                if not slot_held:
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
        # Cleaned first (`clean_error_text`), so the page's error stays a finding
        # of the page whatever characters its message holds.
        errors = [clean_error_text(e) for e in console_errors(log_text)]
        seen = {e.split(": ", 1)[-1] for e in errors}
        errors += [
            e
            for e in map(clean_error_text, renderfind.parse_errors(log_text, nonce))
            if e.split(": ", 1)[-1] not in seen
        ][: _MAX_ERRORS - len(errors)]
        check = renderfind.parse_check(log_text, nonce)
        # The same rule for an error's text as a renderer's answer (`renderwire`).
        # Cleaned above, so this holds; it stays so that the rule is one.
        try:
            for error in errors:
                renderwire.check_error_text(error)
        except renderwire.RenderAnswerInvalid as exc:
            log.warning("thumbnail render gave errors that cannot be kept: %s", exc)
            return None, None

        try:
            data = _read_shot(shot)
        except OSError:
            log.warning("thumbnail render produced no file")
            return Report(None, errors, check)

    # A real PNG of the size asked for, or nothing. A zero-byte or truncated
    # file would otherwise be served as a broken image, which looks like the PAGE
    # is broken. The same check as a renderer's answer (`renderwire`), so there is
    # one way a picture is accepted; one that fails is a render that did not run.
    try:
        data = renderwire.validate_png(data, int(width), int(height))
    except renderwire.RenderAnswerInvalid as exc:
        log.warning("thumbnail render gave no usable picture: %s", exc)
        return None, None
    return Report(data, errors, check)


# --- a screenshot on request -------------------------------------------------------
#
# `shoot` takes a picture of a page as a reader sees it, whole or one element or
# section of it, at a width and theme the caller picks (`renderwire.shot_request`),
# and keeps nothing. It runs inside the same envelope as a render: an environment
# built from nothing, the kernel's data limit, the memory sampler, the one render
# slot, a hard timeout that ends the whole process group, the same flags, a fresh
# directory removed afterwards, and every process ended after. It drives the browser
# over its DevTools pipe (`ream.devtools`) rather than `--screenshot`, which takes
# only the window and can neither pick an element nor set a theme.

# How long a screenshot waits for the render slot before its answer is `busy`.
SHOT_SLOT_WAIT_SECONDS = 5
# Late layout settles in this much of the page's own time before the picture, as
# a render's --virtual-time-budget; and at most this much real time is given to it.
SHOT_SETTLE_VIRTUAL_MS = 3000
SHOT_SETTLE_REAL_SECONDS = 6.0
# The longest message read from the browser: the picture's answer, base64, with room.
SHOT_MESSAGE_MAX_BYTES = renderwire.SHOT_PNG_BASE64_MAX_CHARS + 64 * 1024
# The browser reads its commands on fd 3 and writes on fd 4. A child cannot be
# given a descriptor at a chosen number safely from a threaded server (no
# preexec_fn), so it gets them as its stdin and stdout, and this moves them there
# and points stdin and stdout at /dev/null before the browser starts.
_SHELL = "/bin/sh"
_PIPE_SHIM = 'exec 3<&0 4>&1 </dev/null >/dev/null && exec "$0" "$@"'

# Run in a world of its own the page's scripts cannot reach (Page.createIsolatedWorld),
# so a page cannot change how its parts are found. The whole page's size, and the
# box of the element (by id) or the section (by its heading's folded words, to the
# next heading of the same rank or higher, as the pin script's `peers` reads one).
_RESOLVE_JS = r"""(function(el, sec){
var d=document,de=d.documentElement,b=d.body,i,n=0,hit=null,hs,lv,end=null,h,r,t,top,bot;
scrollTo(0,0);
var ph=Math.max(de.scrollHeight,b?b.scrollHeight:0,de.clientHeight);
var vw=de.clientWidth||innerWidth;
function box(r){return{x:r.left+scrollX,y:r.top+scrollY,w:r.width,h:r.height}}
if(el!==null){var l=d.querySelectorAll('[id="'+CSS.escape(el)+'"]');
  if(l.length!==1)return{n:l.length,ph:ph,vw:vw};
  return{n:1,ph:ph,vw:vw,box:box(l[0].getBoundingClientRect())}}
if(sec!==null){hs=d.querySelectorAll("h1,h2,h3,h4,h5,h6");
  for(i=0;i<hs.length;i++){t=(hs[i].textContent||"").replace(/\s+/g," ").trim().toLowerCase();
    if(t===sec){n++;hit=hs[i]}}
  if(n!==1)return{n:n,ph:ph,vw:vw};
  lv=+String(hit.nodeName).charAt(1);
  for(i=0;i<hs.length;i++){h=hs[i];
    if(h!==hit&&!hit.contains(h)&&(hit.compareDocumentPosition(h)&4)&&+String(h.nodeName).charAt(1)<=lv){end=h;break}}
  r=hit.getBoundingClientRect();top=r.top+scrollY;
  bot=end?end.getBoundingClientRect().top+scrollY:ph;
  return{n:1,ph:ph,vw:vw,box:{x:0,y:top,w:vw,h:Math.max(0,bot-top)}}}
return{n:-1,ph:ph,vw:vw}})"""


class _Watch:
    """Ends the browser's process group at the deadline, or when its memory passes
    the ceiling, from a thread of its own while the caller talks to it; `reason`
    then says which. The same rule `_run` applies to a render."""

    def __init__(self, proc: subprocess.Popen, deadline: float) -> None:
        self.proc = proc
        self.deadline = deadline
        self.reason: str | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def _watch(self) -> None:
        while not self._done.wait(RSS_POLL_SECONDS):
            if time.monotonic() >= self.deadline:
                self.reason = "timeout"
            elif _group_rss(self.proc.pid) > RENDER_MAX_RSS_BYTES:
                self.reason = "memory"
            else:
                continue
            _kill_group(self.proc)
            return

    def stop(self) -> None:
        self._done.set()
        self._thread.join()


def _shot_box(found: dict, request) -> tuple[int, int, int, int, bool] | str:
    """`(x, y, width, height, truncated)` of the part to shoot, in whole pixels
    (rounded outwards), from what `_RESOLVE_JS` found, or the outcome why not."""
    n = found.get("n")
    if request.element is None and request.section is None:
        x, y, w, h = 0.0, 0.0, float(request.width), float(found["ph"])
    elif n == 0:
        return "no_match"
    elif n != 1:
        return "ambiguous"
    else:
        box = found["box"]
        x, y, w, h = (float(box[k]) for k in ("x", "y", "w", "h"))
    left, top = max(0, math.floor(x)), max(0, math.floor(y))
    right, bottom = math.ceil(x + w), math.ceil(y + h)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return "empty"
    truncated = width > renderwire.SHOT_MAX_WIDTH or height > renderwire.SHOT_MAX_HEIGHT
    return (
        left,
        top,
        min(width, renderwire.SHOT_MAX_WIDTH),
        min(height, renderwire.SHOT_MAX_HEIGHT),
        truncated,
    )


class _Offline:
    """Lets the page load itself and nothing else. Every target the browser
    starts (the page, a frame of its own, a worker, a window it opens) is held at
    its start (`Target.setAutoAttach` with `waitForDebuggerOnStart`) until every
    request it makes is handed here (`Fetch.enable`), and, where the target takes
    it, it is set offline too; the commands on one target run in order, so this
    is in place before it runs a line. A request for `page_url` goes on; every
    other one fails as if there were no network. An error answer to any of these
    commands fails the shot, and so does a target past SHOT_TARGETS_MAX."""

    def __init__(self, page_url: str) -> None:
        self.page_url = page_url
        self.pipe: devtools.Pipe | None = None
        self.attached: dict[str, str] = {}
        self.refused = 0
        self.held = 0

    def __call__(self, method: str, params: dict, session: str | None) -> None:
        if method == "Fetch.requestPaused":
            self._decide(params, session)
        elif method == "Target.attachedToTarget":
            self._hold(params)

    def _decide(self, params: dict, session: str | None) -> None:
        request_id = params.get("requestId")
        url = (params.get("request") or {}).get("url")
        if not isinstance(request_id, str):
            raise devtools.CommandFailed("the browser paused an unnamed request")
        if url == self.page_url:
            self.pipe.send("Fetch.continueRequest", {"requestId": request_id}, session)
            return
        self.refused += 1
        self.pipe.send(
            "Fetch.failRequest",
            {"requestId": request_id, "errorReason": "InternetDisconnected"},
            session,
        )

    def _hold(self, params: dict) -> None:
        child = params.get("sessionId")
        info = params.get("targetInfo")
        if not isinstance(child, str) or not isinstance(info, dict):
            raise devtools.CommandFailed("the browser attached to something unnamed")
        if len(self.attached) >= SHOT_TARGETS_MAX:
            raise devtools.CommandFailed("the page started too many targets")
        self.attached[child] = str(info.get("targetId"))
        pipe = self.pipe
        if info.get("type") in ("page", "iframe"):
            pipe.send(
                "Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, child, checked=True
            )
            pipe.send("Network.enable", session=child, checked=True)
            pipe.send(
                "Network.emulateNetworkConditions",
                {
                    "offline": True,
                    "latency": 0,
                    "downloadThroughput": -1,
                    "uploadThroughput": -1,
                },
                child,
                checked=True,
            )
        else:
            # A worker can be neither held to the page nor set offline here,
            # so it is never let go: it stays held at its start, and runs nothing.
            self.held += 1
            return
        pipe.send("Target.setAutoAttach", _AUTO_ATTACH, child, checked=True)
        pipe.send("Runtime.runIfWaitingForDebugger", session=child, checked=True)


# The most targets one shot may start (the page, its frames, workers, windows).
SHOT_TARGETS_MAX = 64
_AUTO_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}


def _converse(pipe: devtools.Pipe, page_url: str, request, offline: _Offline):
    """The DevTools conversation of one screenshot, on a browser just started."""
    # Every target from here on is held at its start and taken offline (`_Offline`).
    pipe.call("Target.setAutoAttach", _AUTO_ATTACH)
    target = pipe.call("Target.createTarget", {"url": "about:blank"})["targetId"]
    session = next((s for s, t in offline.attached.items() if t == target), None)
    while session is None:
        pipe.wait_for("Target.attachedToTarget")
        session = next((s for s, t in offline.attached.items() if t == target), None)
    viewport = renderwire.SHOT_VIEWPORTS[request.width]
    pipe.call("Page.enable", session=session)
    pipe.call(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": request.width,
            "height": viewport,
            "deviceScaleFactor": 1,
            "mobile": False,
        },
        session,
    )
    pipe.call(
        "Emulation.setEmulatedMedia",
        {"features": [{"name": "prefers-color-scheme", "value": request.theme}]},
        session,
    )
    navigated = pipe.call("Page.navigate", {"url": page_url}, session)
    if navigated.get("errorText"):
        raise devtools.CommandFailed("the browser could not open the page")
    # The page's load, then its own time run on to let late layout settle (the
    # render's --virtual-time-budget), each within a bound of real time.
    settle = min(pipe.deadline, time.monotonic() + SHOT_SETTLE_REAL_SECONDS)
    pipe.wait_for("Page.loadEventFired", session, until=settle)
    pipe.call(
        "Emulation.setVirtualTimePolicy",
        {"policy": "advance", "budget": SHOT_SETTLE_VIRTUAL_MS},
        session,
    )
    settle = min(pipe.deadline, time.monotonic() + SHOT_SETTLE_REAL_SECONDS)
    pipe.wait_for("Emulation.virtualTimeBudgetExpired", session, until=settle)

    frame = pipe.call("Page.getFrameTree", session=session)["frameTree"]["frame"]["id"]
    world = pipe.call(
        "Page.createIsolatedWorld",
        {"frameId": frame, "worldName": "ream-shot", "grantUniveralAccess": False},
        session,
    )["executionContextId"]
    args = f"{json.dumps(request.element)}, {json.dumps(request.section)}"
    expression = f"{_RESOLVE_JS}({args})"
    evaluated = pipe.call(
        "Runtime.evaluate",
        {"expression": expression, "contextId": world, "returnByValue": True},
        session,
    )
    found = (evaluated.get("result") or {}).get("value")
    if evaluated.get("exceptionDetails") or not isinstance(found, dict):
        raise devtools.CommandFailed("the page's parts could not be read")
    page_height = found.get("ph")
    if not isinstance(page_height, (int, float)) or not 0 <= page_height <= 10**7:
        raise devtools.CommandFailed("the page's height could not be read")
    page_height = math.ceil(page_height)
    box = _shot_box(found, request)
    if isinstance(box, str):
        n = found.get("n")
        matches = None
        if box == "no_match":
            matches = 0
        elif box == "ambiguous":
            matches = min(int(n), renderwire.SHOT_MATCHES_MAX)
        return renderwire.Shot(box, matches=matches)
    x, y, width, height, truncated = box
    try:
        taken = pipe.call(
            "Page.captureScreenshot",
            {
                "format": "png",
                "clip": {"x": x, "y": y, "width": width, "height": height, "scale": 1},
                "captureBeyondViewport": True,
                "fromSurface": True,
            },
            session,
        )
    except devtools.MessageTooBig:
        return renderwire.Shot("picture_too_big")
    data = taken.get("data")
    if not isinstance(data, str) or not data.isascii():
        raise devtools.CommandFailed("the browser gave no picture")
    if len(data) > renderwire.SHOT_PNG_BASE64_MAX_CHARS:
        return renderwire.Shot("picture_too_big")
    try:
        png = binascii.a2b_base64(data.encode("ascii"), strict_mode=True)
    except binascii.Error:
        raise devtools.CommandFailed("the browser's picture is not base64") from None
    if len(png) > renderwire.SHOT_PNG_MAX_BYTES:
        return renderwire.Shot("picture_too_big")
    return renderwire.Shot(
        "shot",
        png,
        width=width,
        height=height,
        page_height=page_height,
        truncated=truncated,
    )


def shoot(
    binary: str | None,
    html: str,
    request,
    *,
    work_root: str | os.PathLike | None = None,
    slot_held: bool = False,
) -> renderwire.Shot:
    """A screenshot of `html` (already stamped as a reader would see it), as
    `request` (a `renderwire.ShotRequest`) asks: a `renderwire.Shot`, whose
    outcome says what came of it. Never raises.

    `binary` None is `no_browser`. A page over RENDER_MAX_BYTES is `page_too_big`
    and no browser starts. The slot is waited for SHOT_SLOT_WAIT_SECONDS (`busy`
    after), unless `slot_held` (`ream.renderd`, which answers busy itself)."""
    if binary is None:
        return renderwire.Shot("no_browser")
    if too_big_to_render(html):
        return renderwire.Shot("page_too_big")
    with tempfile.TemporaryDirectory(
        prefix="artifacts-shot-", dir=work_root, ignore_cleanup_errors=True
    ) as workdir:
        page = Path(workdir) / "page.html"
        try:
            page.write_text(html, encoding="utf-8")
        except (OSError, UnicodeEncodeError):
            return renderwire.Shot("failed")
        viewport = renderwire.SHOT_VIEWPORTS[request.width]
        argv = [
            _SHELL,
            "-c",
            _PIPE_SHIM,
            binary,
            *_isolation_flags(),
            f"--window-size={int(request.width)},{int(viewport)}",
            "--remote-debugging-pipe",
            "about:blank",
        ]
        if not slot_held and not _RENDER_SLOT.acquire(timeout=SHOT_SLOT_WAIT_SECONDS):
            log.warning(
                "screenshot not taken: no turn within %ss", SHOT_SLOT_WAIT_SECONDS
            )
            return renderwire.Shot("busy")
        try:
            return _shoot_in(argv, workdir, page.as_uri(), request)
        finally:
            if not slot_held:
                _RENDER_SLOT.release()


def _shoot_in(argv: list[str], workdir: str, page_url: str, request) -> renderwire.Shot:
    """Start the browser on its pipe, talk to it, and end its whole group, whatever
    happened."""
    to_browser_r, to_browser_w = os.pipe()
    from_browser_r, from_browser_w = os.pipe()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    proc = None
    watch = None
    try:
        try:
            proc = subprocess.Popen(  # noqa: S603 -- argv list, fixed shell text, binary from config
                _data_limited(argv, RENDER_MAX_DATA_BYTES),
                stdin=to_browser_r,
                stdout=from_browser_w,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # its own group, so all of it can be ended
                # BUILT FROM NOTHING, as a render's (see `screenshot`).
                env={"HOME": workdir, "TMPDIR": workdir, "PATH": "/usr/bin:/bin"},
                cwd=workdir,
            )
        except (OSError, ValueError) as exc:
            log.warning("screenshot could not start: %s", exc)
            return renderwire.Shot("failed")
        finally:
            os.close(to_browser_r)
            os.close(from_browser_w)
        watch = _Watch(proc, deadline)
        offline = _Offline(page_url)
        pipe = devtools.Pipe(
            to_browser_w,
            from_browser_r,
            deadline=deadline,
            max_message=SHOT_MESSAGE_MAX_BYTES,
            on_event=offline,
        )
        offline.pipe = pipe
        try:
            shot = _converse(pipe, page_url, request, offline)
        except devtools.MessageTooBig:
            log.warning("screenshot: a message from the browser was too long")
            shot = renderwire.Shot("failed")
        except devtools.Timeout:
            _kill_group(proc)
            shot = renderwire.Shot("timeout")
        except (devtools.DevToolsError, KeyError, TypeError, ValueError) as exc:
            log.warning("screenshot: %s", exc)
            shot = renderwire.Shot("failed")
        watch.stop()
        if watch.reason is not None:
            log.warning("screenshot: the browser was ended (%s)", watch.reason)
            return renderwire.Shot(watch.reason)
        try:
            return renderwire.check_shot(shot)
        except renderwire.RenderAnswerInvalid as exc:
            log.warning("screenshot gave no usable picture: %s", exc)
            return renderwire.Shot("failed")
    finally:
        if watch is not None:
            watch.stop()
        if proc is not None:
            _kill_group(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for fd in (to_browser_w, from_browser_r):
            try:
                os.close(fd)
            except OSError:
                pass
