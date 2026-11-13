"""The renderer as its own process: one browser at a time, nothing of the app's environment.

    python -m ream.renderd --port 8091 [--bind 0.0.0.0] [--selfcheck]

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
  * `--selfcheck`: render a built-in page, check the answer, exit 0 or 1.

It refuses to start while its environment carries anything of the app's
(`settings.renderer_foreign_count`): DATABASE_URL, or a `REAM_*`/`ARTIFACTS_*`
variable other than BROWSER and BIND_HOST.

One browser at a time: a request waits up to SLOT_WAIT_SECONDS for the render
slot, then answers 503 with Retry-After. Every render gets its own directory,
browser profile and cache under the work directory, which is emptied at start
and after each render. After each render no process of this uid may be left but
this one and its ancestors; if one is, the process logs it and exits non-zero so
that whatever supervises it starts a fresh one.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

from ream import rendercore, renderwire, settings

log = logging.getLogger("ream.renderd")

# The fixed header every render request carries. Not a secret: it makes any
# request a browser could send without asking first come out as a 403.
HEADER = b"x-ream-render"
HEADER_VALUE = b"1"
# How long a request waits for the one browser before answering 503.
SLOT_WAIT_SECONDS = 5
RETRY_AFTER_SECONDS = 5
# After a render, how long the browser's processes have to be gone.
STRAY_GRACE_SECONDS = 2.0
STRAY_POLL_SECONDS = 0.05
EXIT_REFUSED = 2
EXIT_STRAY = 3
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


def _read_procs() -> list[tuple[int, int, set[int], str]]:
    """Every process in /proc as `(pid, parent pid, uids, state)`. A process that
    goes while being read is skipped."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/status") as f:
                text = f.read()
        except OSError:
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
                    {int(u) for u in fields["Uid"]},
                    fields["State"][0],
                )
            )
        except (KeyError, IndexError, ValueError):
            continue
    return found


def strays(procs, me: int, uid: int) -> list[int]:
    """The pids in `procs` of `uid` other than `me` and its ancestors (the process
    supervising it). A zombie is not counted: it runs nothing and holds nothing."""
    parent = {pid: ppid for pid, ppid, _, _ in procs}
    mine = set()
    pid = me
    while pid and pid not in mine:
        mine.add(pid)
        pid = parent.get(pid, 0)
    return sorted(
        pid
        for pid, _, uids, state in procs
        if uid in uids and pid not in mine and state != "Z"
    )


def _strays_after_render() -> list[int]:
    deadline = time.monotonic() + STRAY_GRACE_SECONDS
    while True:
        left = strays(_read_procs(), os.getpid(), os.getuid())
        if not left or time.monotonic() >= deadline:
            return left
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
    """The ASGI app. `work_root` must already have been prepared."""

    def __init__(self, work_root: Path, *, exit=os._exit):
        self.work_root = Path(work_root)
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
        """True when the render left nothing behind; otherwise logs what, and exits."""
        left = _strays_after_render()
        if left:
            log.error(
                "%d process(es) of this uid left after a render (pids %s): exiting",
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
    work_root = prepare_work_root(work_root)
    app = Renderd(work_root)
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
