"""The renderer as its own process: one browser at a time, nothing of the app's environment.

    python -m ream.renderd --end-all-after-render --port 8091 [--bind 0.0.0.0]
    python -m ream.renderd --dev --port 8091      # on a shared machine
    python -m ream.renderd --dev --selfcheck --work-dir /tmp/rd-check

A bare ASGI app under uvicorn (no FastAPI), serving the wire `ream.renderwire`
describes. The app sends a page already stamped as a reader would see it; this
process stamps nothing, makes the check script's nonce, runs the browser through
`ream.rendercore.screenshot` and answers with `renderwire.encode_answer`.

  * `POST /v1/render?w=&h=&check=`: the body is the HTML (`text/html`, with a
    `Content-Length` no larger than `rendercore.RENDER_MAX_BYTES`, refused with
    413 before any of it is read). The request must carry `X-Ream-Render: 1`;
    without it the answer is a bare 403. `OPTIONS` is answered the same way, and
    nothing here ever sends a CORS header. `check=0` answers `check: null`.
  * `GET /healthz`: the process is up. `GET /readyz`: the browser, prlimit and a
    writable work directory are there.
  * `--selfcheck`: render a built-in page, check the answer, exit 0 or 1. It
    empties its work directory and, armed, ends every process of its uid, so
    beside a running renderer it takes `--dev` and a work directory of its own.

It refuses to start while its environment carries anything of the app's
(`settings.renderer_foreign_count`): DATABASE_URL, or a `REAM_*`/`ARTIFACTS_*`
variable other than BROWSER and BIND_HOST.

One browser at a time: a request waits up to SLOT_WAIT_SECONDS for the render
slot, then answers 503 with Retry-After. Every render gets its own directory,
browser profile and cache under the work directory, which is emptied at start
and after each render. The process is a child subreaper
(`prctl(PR_SET_CHILD_SUBREAPER)`, Linux only), so a browser helper orphaned by a
render is reparented to it rather than to pid 1, and it is not dumpable
(`prctl(PR_SET_DUMPABLE, 0)`), so a process of its uid cannot trace it or read its
memory; it refuses to start without either.

It runs in one of two modes, and refuses to start in neither (or both):

  * `--end-all-after-render`, for its own container: after every render it ends
    every process of its uid (`kill(-1, SIGKILL)`: every process it may signal but
    pid 1 and itself), reaps its children, and empties the work directory. Armed
    only where that is safe: it refuses to start as pid 1, without a `/proc/1`, or
    where it looks like a whole machine rather than a container (`host_signs`: pid
    1's command is `systemd` or `init`, or cannot be read, or `kthreadd` is pid 2).
    Anything else of its uid in the container ends with each render, so a probe
    is httpGet or tcpSocket on its port, never exec.
  * `--dev`, on a shared machine (a developer's, the tests): nothing is ended. It
    logs that at start, and after each render no process descended from it (nor
    one of its uid whose parent went while the list was read) may be left alive, on
    two lists of processes read apart; if one is, it logs it and exits non-zero so
    that whatever supervises it starts a fresh one.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import os
import shutil
import signal
import stat
import sys
import tempfile
import time
from pathlib import Path

from ream import rendercore, renderwire, settings

log = logging.getLogger("ream.renderd")

# The fixed header every render request carries. Not a secret: it makes any
# request a browser could send without asking first come out as a 403.
HEADER = renderwire.RENDER_HEADER.lower().encode("ascii")
HEADER_VALUE = renderwire.RENDER_HEADER_VALUE.encode("ascii")
# How long a request waits for the one browser before answering 503.
SLOT_WAIT_SECONDS = 5
RETRY_AFTER_SECONDS = 5
# After a render, how long the browser's processes have to be gone.
STRAY_GRACE_SECONDS = 2.0
STRAY_POLL_SECONDS = 0.05
# Clean means two empty lists of what is left, read this far apart.
STRAY_CONFIRM_SECONDS = 0.1
# A process listed in /proc but gone before it could be read.
GONE = "gone"
# A process listed in /proc whose entry is there but could not be read: alive.
UNREADABLE = "unreadable"
EXIT_REFUSED = 2
EXIT_STRAY = 3
# prctl(2) options: make this process the reaper of its orphaned descendants.
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
# ... and make it not dumpable: no ptrace, no /proc/<pid>/mem, by its own uid.
PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4
# Armed (--end-all-after-render): how long its children have to be reaped after
# every process of its uid was ended.
REAP_GRACE_SECONDS = 2.0
# Pid 1 on a whole machine rather than in a container.
HOST_INITS = frozenset({"systemd", "init"})
WORK_DIR_NAME = "ream-renderd"
# The browser makes its profile, and a socket inside it, under each render's
# directory, and a socket's path is short (108 bytes on Linux). Playwright's chrome
# rendered under a work directory of 36 characters and made no picture under one
# of 40 (13 Nov 2026), so a longer one is refused at start.
WORK_ROOT_MAX_CHARS = 32

_BUSY = object()
_LEFT = object()

# The page --selfcheck renders: some text, so the check script has something to count.
SELFCHECK_PAGE = (
    "<!doctype html><html><head><meta charset=utf-8><title>selfcheck</title>"
    "<style>body{background:#fff;font:16px sans-serif}</style></head>"
    "<body><h1>Ream renderer</h1><p>The self-check page.</p></body></html>"
)


# --- the work directory ----------------------------------------------------------------


def default_work_root() -> Path:
    return Path(tempfile.gettempdir()) / WORK_DIR_NAME


def prepare_work_root(path: Path) -> Path:
    """`path` as an empty directory of this uid's alone, whatever was there before.
    A symlink there is removed, never followed."""
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    path.mkdir(mode=0o700)
    info = path.lstat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("work directory is not this process's own")
    return path


def _empty(path: Path) -> bool:
    """Remove everything inside `path`; True when it is then empty."""
    for entry in list(path.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                pass
    return not any(path.iterdir())


# --- what is left of a render ----------------------------------------------------------


def _read_procs() -> list[tuple[int, int, str, int]]:
    """Every process in /proc as `(pid, parent pid, state, real uid)`. Threads are
    not listed: /proc lists only processes, so uvicorn's and asyncio's worker
    threads never appear here. The entries are read one by one, so the list is not
    one instant: a process listed in /proc that is gone before its own entry could
    be read (ENOENT, ESRCH) appears as `(pid, -1, GONE, -1)`, so that a child read
    just before it went, still naming it as parent, can be told apart (see
    `leftovers`). One whose entry is there but cannot be read (a permission refused,
    a status without its fields) is alive, and appears as `(pid, -1, UNREADABLE, -1)`."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/status") as f:
                text = f.read()
        except (FileNotFoundError, ProcessLookupError):
            found.append((int(entry), -1, GONE, -1))
            continue
        except OSError:  # alive but not ours to read (hidepid, another uid)
            found.append((int(entry), -1, UNREADABLE, -1))
            continue
        fields = {}
        for line in text.splitlines():
            key, _, value = line.partition(":")
            fields[key] = value.split()
        try:
            found.append(
                (
                    int(entry),
                    int(fields["PPid"][0]),
                    fields["State"][0],
                    int(fields["Uid"][0]),
                )
            )
        except (KeyError, IndexError, ValueError):
            found.append((int(entry), -1, UNREADABLE, -1))
    return found


def leftovers(procs, me: int, uid: int) -> list[int]:
    """What a render left running, in `procs` (`(pid, ppid, state, uid)`), alive
    and not zombies:

      * every process descended from `me` (following PPid links down from it). As
        a subreaper this process inherits whatever the browser orphaned, so an
        orphaned grandchild is a child of `me` by the time it is read here;
      * every process of `uid` whose parent went while the list was being read
        (listed, but GONE), unless it is one of `me`'s own ancestors: a browser
        process read just before its parent exited still names that parent, and
        so looks cut off from the tree. It is counted rather than missed.

    A parent never listed at all, or listed but UNREADABLE, is not that case: /proc may hide other users'
    processes (`hidepid`), and a process whose parent is hidden is not one this
    process started. A process merely of the same uid that this process did not
    start (a pod's exec probe, the user's shell, another job on a shared machine)
    does not count."""
    children: dict[int, list[int]] = {}
    parent = {}
    state = {}
    for pid, ppid, st, _ in procs:
        children.setdefault(ppid, []).append(pid)
        parent[pid] = ppid
        state[pid] = st
    found: set[int] = set()
    todo = list(children.get(me, ()))
    while todo:
        pid = todo.pop()
        if pid in found or pid == me:
            continue
        found.add(pid)
        todo.extend(children.get(pid, ()))
    ancestors: set[int] = set()
    up = parent.get(me)
    while up in parent and up not in ancestors:
        ancestors.add(up)
        up = parent[up]
    for pid, ppid, _, owner in procs:
        if (
            owner == uid
            and state.get(ppid) == GONE
            and pid != me
            and pid not in ancestors
        ):
            found.add(pid)
    return sorted(pid for pid in found if state[pid] not in ("Z", GONE, UNREADABLE))


def _reap_zombies(procs, me: int) -> None:
    """Wait on every zombie child of `me`, without blocking: a subreaper inherits
    the browser's orphans, and nothing else would ever reap them."""
    for pid, ppid, st, _ in procs:
        if ppid == me and st == "Z":
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass


def _strays_after_render() -> list[int]:
    """What the last render left running, after up to STRAY_GRACE_SECONDS for it
    to go. Clean only when two lists read STRAY_CONFIRM_SECONDS apart both show
    nothing: one list is not one instant, so a single empty one is not proof."""
    me, uid = os.getpid(), os.getuid()
    deadline = time.monotonic() + STRAY_GRACE_SECONDS
    clean_once = False
    while True:
        procs = _read_procs()
        _reap_zombies(procs, me)
        left = leftovers(procs, me, uid)
        if not left:
            if clean_once:
                return []
            clean_once = True
            time.sleep(STRAY_CONFIRM_SECONDS)
            continue
        clean_once = False
        if time.monotonic() >= deadline:
            return left
        time.sleep(STRAY_POLL_SECONDS)


def _prctl(option: int, arg) -> int:
    """prctl(2) through libc, with one argument, and what it returns; raises OSError
    on failure, and on anything but Linux."""
    if not sys.platform.startswith("linux"):
        raise OSError("prctl needs Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(
        ctypes.c_int(option),
        arg,
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
    )
    if result < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return result


def set_subreaper(on: bool = True) -> None:
    """Make this process (or stop it being) a child subreaper: an orphaned
    descendant is then reparented to it, not to pid 1. OSError when it cannot."""
    _prctl(PR_SET_CHILD_SUBREAPER, ctypes.c_ulong(1 if on else 0))


def is_subreaper() -> bool:
    value = ctypes.c_int(0)
    _prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(value))
    return value.value != 0


def set_not_dumpable() -> None:
    """Make this process not dumpable: a process of its uid (the browser it starts
    among them) can then neither trace it nor read its memory. OSError when it
    cannot, or when the setting did not take."""
    _prctl(PR_SET_DUMPABLE, ctypes.c_ulong(0))
    if is_dumpable():
        raise OSError("still dumpable")


def is_dumpable() -> bool:
    return _prctl(PR_GET_DUMPABLE, ctypes.c_ulong(0)) != 0


# --- ending every process of its uid (--end-all-after-render) ---------------------------


def host_signs(proc: Path = Path("/proc")) -> str | None:
    """Why this looks like a whole machine rather than a container, or None. Pid 1's
    command (the last part of its first argument) is `systemd` or `init`, or cannot
    be read or is empty; or pid 2 is `kthreadd`, the kernel's own, which a pid
    namespace never holds. Read from `proc` (a test's own directory)."""
    try:
        argv0 = (proc / "1" / "cmdline").read_bytes().split(b"\0", 1)[0]
    except OSError:
        return "pid 1's command cannot be read"
    name = os.path.basename(argv0.decode("utf-8", "replace"))
    if not name:
        return "pid 1's command is empty"
    if name in HOST_INITS:
        return f"pid 1 is {name}"
    try:
        status = (proc / "2" / "status").read_text()
    except OSError:
        return None
    if status.startswith("Name:\tkthreadd\n"):
        return "pid 2 is kthreadd"
    return None


def end_all_refusal(proc: Path = Path("/proc")) -> str | None:
    """Why --end-all-after-render may not be armed here, or None."""
    if os.getpid() == 1:
        return "it is pid 1"
    if not (proc / "1").exists():
        return "there is no /proc/1"
    return host_signs(proc)


def end_all() -> None:
    """End every process this one may signal but pid 1 and itself."""
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:  # nothing else to signal
        pass


def reap_children(grace: float = REAP_GRACE_SECONDS) -> bool:
    """Wait on every child of this process (as a subreaper, every orphan of what was
    ended is one) until there is none; False if one is still there after `grace`."""
    deadline = time.monotonic() + grace
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if pid:
            continue
        if time.monotonic() >= deadline:
            return False
        time.sleep(STRAY_POLL_SECONDS)


# --- one render --------------------------------------------------------------------------


def _fit(report):
    """`report` cut to what the wire carries: at most ERRORS_MAX_COUNT errors of at
    most ERROR_MAX_CHARS, and check numbers out of bounds dropped."""
    png, errors = report[0], report[1]
    check = getattr(report, "check", None)
    if errors is not None:
        errors = [e[: renderwire.ERROR_MAX_CHARS] for e in errors][
            : renderwire.ERRORS_MAX_COUNT
        ]
    try:
        check = renderwire._check(check)
    except renderwire.RenderAnswerInvalid:
        check = None
    return renderwire.Report(png, errors, check)


class Renderd:
    """The ASGI app. `work_root` must already have been prepared. `end_all`: after
    every render, end every process of its uid (`--end-all-after-render`); otherwise
    check that nothing descended from it is left (`--dev`)."""

    def __init__(self, work_root: Path, *, end_all: bool = False, exit=os._exit):
        self.work_root = Path(work_root)
        self.end_all = end_all
        self._exit = exit

    # -- the render, on a worker thread, holding the slot for all of it --

    def render(self, html: str, width: int, height: int, check: bool):
        """The answer's bytes, _BUSY when the browser is taken, or _LEFT when the
        render left something behind (the process has then been told to exit).
        Takes and releases the slot itself, on the thread it runs on, so a
        cancelled request cannot leave it held."""
        if not rendercore._RENDER_SLOT.acquire(timeout=SLOT_WAIT_SECONDS):
            return _BUSY
        try:
            binary = rendercore.browser_path()
            if binary is None:
                log.warning("render not run: no browser")
                report = renderwire.Report(None, None, None)
            else:
                report = rendercore.screenshot(
                    binary,
                    html,
                    width,
                    height,
                    work_root=self.work_root,
                    slot_held=True,
                )
            if not self._after_render():
                return _LEFT
        finally:
            rendercore._RENDER_SLOT.release()
        report = _fit(report)
        if not check and report[1] is not None:
            report = renderwire.Report(report[0], report[1], None)
        try:
            return renderwire.encode_answer(report)
        except renderwire.RenderAnswerInvalid as exc:
            log.warning("render answer refused: %s", exc)
            return renderwire.encode_answer(renderwire.Report(None, None, None))

    def _after_render(self) -> bool:
        """True when the render left nothing behind; otherwise logs what, and exits.
        Armed, every process of its uid is ended first and its children reaped."""
        if self.end_all:
            end_all()
            if not reap_children():
                log.error(
                    "a child was still there after every process was ended: exiting"
                )
                self._exit(EXIT_STRAY)
                return False
            left = []
        else:
            left = _strays_after_render()
        if left:
            log.error(
                "%d process(es) descended from this one left after a render"
                " (pids %s): exiting",
                len(left),
                ",".join(map(str, left[:20])),
            )
            self._exit(EXIT_STRAY)
            return False
        if not _empty(self.work_root):
            log.error("the work directory could not be emptied after a render: exiting")
            self._exit(EXIT_STRAY)
            return False
        return True

    def ready(self) -> list[str]:
        """What is missing for a render, by name; empty when ready."""
        missing = []
        binary = rendercore.browser_path()
        if binary is None or not os.access(binary, os.X_OK):
            missing.append("browser")
        if rendercore._PRLIMIT is None:
            missing.append("prlimit")
        try:
            with tempfile.TemporaryFile(dir=self.work_root):
                pass
        except OSError:
            missing.append("work directory")
        return missing

    # -- HTTP --

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        status = await self._handle(scope, receive, send)
        log.info("%s %s %d", scope["method"], scope["path"], status)

    async def _handle(self, scope, receive, send) -> int:
        method, path = scope["method"], scope["path"]
        if path == "/healthz":
            if method != "GET":
                return await _answer(send, 405, b"", allow=b"GET")
            return await _answer(send, 200, b"ok\n")
        if path == "/readyz":
            if method != "GET":
                return await _answer(send, 405, b"", allow=b"GET")
            missing = self.ready()
            if missing:
                return await _answer(
                    send, 503, ("not ready: " + ", ".join(missing) + "\n").encode()
                )
            return await _answer(send, 200, b"ready\n")
        if path != "/v1/render":
            return await _answer(send, 404, b"")
        if method == "OPTIONS":
            return await _answer(send, 403, b"")
        if method != "POST":
            return await _answer(send, 405, b"", allow=b"POST")

        headers = {}
        for name, value in scope["headers"]:
            headers.setdefault(name.lower(), []).append(value)
        if headers.get(HEADER) != [HEADER_VALUE]:
            return await _answer(send, 403, b"")
        try:
            width, height, check = renderwire.parse_request_query(
                scope.get("query_string", b"")
            )
        except renderwire.RenderRequestInvalid as exc:
            return await _answer(send, 400, f"{exc}\n".encode())
        if not _is_html(headers.get(b"content-type")):
            return await _answer(send, 415, b"text/html only\n")
        length = _content_length(headers)
        if length is None or length > rendercore.RENDER_MAX_BYTES:
            return await _answer(send, 413, b"a Content-Length within the limit\n")

        body = await _read_exactly(receive, length)
        if body is None:
            return await _answer(send, 400, b"the body is not its Content-Length\n")
        try:
            html = body.decode("utf-8")
        except UnicodeDecodeError:
            return await _answer(send, 400, b"the body is not UTF-8\n")

        answer = await asyncio.to_thread(self.render, html, width, height, check)
        if answer is _BUSY:
            return await _answer(
                send,
                503,
                b"busy\n",
                retry_after=str(RETRY_AFTER_SECONDS).encode(),
            )
        if answer is _LEFT:
            return await _answer(send, 500, b"")
        return await _answer(send, 200, answer, content_type=b"application/json")


def _is_html(values) -> bool:
    if not values or len(values) != 1:
        return False
    kind, *params = (p.strip().lower() for p in values[0].decode("latin-1").split(";"))
    if kind != "text/html":
        return False
    return all(p in ("charset=utf-8", 'charset="utf-8"') for p in params)


def _content_length(headers) -> int | None:
    if b"transfer-encoding" in headers:
        return None
    values = headers.get(b"content-length")
    if not values or len(values) != 1:
        return None
    raw = values[0]
    if not raw.isdigit() or len(raw) > 10:
        return None
    return int(raw)


async def _read_exactly(receive, length: int) -> bytes | None:
    body = bytearray()
    more = length > 0
    while more:
        message = await receive()
        if message["type"] != "http.request":
            return None
        body += message.get("body", b"")
        if len(body) > length:
            return None
        more = message.get("more_body", False)
    return bytes(body) if len(body) == length else None


async def _answer(
    send,
    status: int,
    body: bytes,
    *,
    content_type: bytes = b"text/plain; charset=utf-8",
    allow: bytes | None = None,
    retry_after: bytes | None = None,
) -> int:
    headers = [
        (b"content-type", content_type),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
    ]
    if allow is not None:
        headers.append((b"allow", allow))
    if retry_after is not None:
        headers.append((b"retry-after", retry_after))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
    return status


# --- the command ---------------------------------------------------------------------------


def selfcheck(app: Renderd) -> bool:
    """Render SELFCHECK_PAGE through the same path as a request and check the answer."""
    width, height = rendercore.WIDTH, rendercore.HEIGHT
    answer = app.render(SELFCHECK_PAGE, width, height, True)
    if answer is _BUSY or answer is _LEFT:
        log.error("selfcheck: the render did not complete")
        return False
    try:
        report = renderwire.parse_answer(answer, width, height)
    except renderwire.RenderAnswerInvalid as exc:
        log.error("selfcheck: the answer failed its checks: %s", exc)
        return False
    if report[0] is None:
        log.error("selfcheck: no picture")
        return False
    if report.check is None or report.check["text"] <= 0:
        log.error("selfcheck: the check script reported nothing")
        return False
    log.info("selfcheck: rendered %dx%d", width, height)
    return True


def refusal() -> str | None:
    """The start-up refusal, or None. Names the count and the rule, never a name or value."""
    count = settings.renderer_foreign_count()
    if not count:
        return None
    return (
        f"ream.renderd refuses to start: the environment carries {count} variable(s) it"
        " does not take. Only REAM_BROWSER and REAM_BIND_HOST (or their ARTIFACTS_"
        " forms) are taken; DATABASE_URL and every other REAM_*/ARTIFACTS_* variable"
        " are refused."
    )


def mode_refusal(end_all_after_render: bool, dev: bool) -> str | None:
    """The start-up refusal for the mode flags, or None."""
    if end_all_after_render == dev:
        return (
            "ream.renderd refuses to start: give exactly one of --end-all-after-render"
            " (in its own container) and --dev"
        )
    if end_all_after_render:
        why = end_all_refusal()
        if why:
            return (
                "ream.renderd refuses to start: --end-all-after-render is for its own"
                f" container, and {why}"
            )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ream.renderd",
        description="The renderer as its own process (see the module's docstring).",
    )
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--bind", default=None, help="default: REAM_BIND_HOST, else 127.0.0.1"
    )
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--selfcheck", action="store_true")
    parser.add_argument(
        "--end-all-after-render",
        action="store_true",
        help="after every render, end every process of this uid (its own container only)",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="end nothing; check only what descends from this process",
    )
    args = parser.parse_args(argv)
    if args.port is None and not args.selfcheck:
        parser.error("--port is required")

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    message = refusal()
    if message:
        print(message, file=sys.stderr)
        return EXIT_REFUSED
    work_root = Path(os.path.abspath(args.work_dir or default_work_root()))
    if len(str(work_root)) > WORK_ROOT_MAX_CHARS:
        print(
            f"ream.renderd refuses to start: the work directory's path is over"
            f" {WORK_ROOT_MAX_CHARS} characters (--work-dir, or TMPDIR)",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    message = mode_refusal(args.end_all_after_render, args.dev)
    if message:
        print(message, file=sys.stderr)
        return EXIT_REFUSED
    try:
        set_subreaper()
    except OSError as exc:
        log.error(
            "ream.renderd refuses to start: it cannot be a child subreaper: %s", exc
        )
        return EXIT_REFUSED
    try:
        set_not_dumpable()
    except OSError as exc:
        log.error(
            "ream.renderd refuses to start: it cannot be made not dumpable: %s", exc
        )
        return EXIT_REFUSED
    if args.dev:
        log.warning(
            "--dev: processes a render leaves are not ended; what descends from"
            " this process is checked instead"
        )
    work_root = prepare_work_root(work_root)
    app = Renderd(work_root, end_all=args.end_all_after_render)
    if args.selfcheck:
        return 0 if selfcheck(app) else 1

    import uvicorn

    uvicorn.run(
        app,
        host=args.bind or settings.current().bind_host,
        port=args.port,
        lifespan="on",
        access_log=False,
        log_config=None,
        server_header=False,
        date_header=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
