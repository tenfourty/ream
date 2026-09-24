"""A picture of a page, for the gallery wall.

homelab-ij8.22. Jeremy asked for a real rendered thumbnail of each page rather
than a list of links, asked whether a tool existed, and — told that one did —
chose (b), the renderer running inside the service itself, over (a), a separate
renderer the service asks. He chose it with the argument against in front of
him, so this builds (b).

THE ARGUMENT AGAINST, STATED ONCE AND THEN ACTED ON. A thumbnail means something
executes HTML an agent wrote. Every page on this service is otherwise sandboxed
on its own throwaway origin precisely so that it CANNOT reach anything, and a
renderer is the first thing that deliberately runs it. (b) puts that execution
in the service's own pod, next to the database credentials and the session
cookie. That is a real cost and it is not paid off by care in this file alone;
it is paid off by the list below, every item of which is here because the
alternative is worse:

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
  * A FAILED RENDER PRODUCES NO THUMBNAIL. Never a stale one, never a partial
    one. The wall is allowed to have gaps; it is not allowed to show Jeremy a
    picture of a page that is not what the page says now.

`render` returns None for every failure, because every failure has the same
correct response and a caller that has to distinguish them is a caller that will
get one of them wrong.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from artifacts import store

log = logging.getLogger("artifacts.thumbs")

# Overridable so the container can say where it put the browser, and so a
# developer machine can point at Playwright's copy. Absent means no thumbnails,
# which is a degraded wall and not a broken service.
BROWSER_ENV = "ARTIFACTS_BROWSER"

TIMEOUT_SECONDS = 20
# The tile is rendered at this width and cropped to this height: a thumbnail is
# the TOP of a page, the part a reader would recognise, not a squashed whole
# page. A full-page shot of a long status board is an unreadable grey smear.
WIDTH = 900
HEIGHT = 640


def browser_path() -> str | None:
    """Where the headless browser is, or None if this deployment has none."""
    explicit = os.environ.get(BROWSER_ENV)
    if explicit:
        return explicit if Path(explicit).is_file() else None
    return shutil.which("chrome-headless-shell") or shutil.which("chromium")


def available() -> bool:
    return browser_path() is not None


# A console line the browser prints with --enable-logging=stderr, e.g.
#   [9:9:0924/120000.1:INFO:CONSOLE(12)] "Uncaught ReferenceError: x is not defined", source: file:///t/page.html (12)
_CONSOLE = re.compile(r':CONSOLE\((\d+)\)\] "(.*)", source: ')
_MAX_ERRORS = 10


def console_errors(stderr: str) -> list[str]:
    """The page's uncaught script errors, from the browser's own log, as
    "line N: message". Only errors: an ordinary console.log is the author's
    business. Capped, and each line bounded, because this goes back to the
    author in an API response (homelab-ij8.72)."""
    found = []
    for match in _CONSOLE.finditer(stderr or ""):
        line, message = match.group(1), match.group(2)
        if "Uncaught" in message or "Error" in message.split(":", 1)[0]:
            found.append(f"line {line}: {message[:300]}")
        if len(found) >= _MAX_ERRORS:
            break
    return found


def render(html: str) -> bytes | None:
    """A PNG of the top of `html`, or None.

    None on: no browser in this deployment, a crash, a timeout, an empty file,
    or anything that is not a PNG. Never raises — a thumbnail is decoration, and
    a decoration that can take down a publish is a bad trade.
    """
    return render_report(html)[0]


def render_report(html: str) -> tuple[bytes | None, list[str] | None]:
    """(PNG or None, the page's script errors or None when it did not run)."""
    binary = browser_path()
    if binary is None:
        return None, None

    with tempfile.TemporaryDirectory(prefix="artifacts-thumb-") as workdir:
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
            f"--window-size={WIDTH},{HEIGHT}",
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
        try:
            done = subprocess.run(  # noqa: S603 -- argv list, no shell, binary from config
                argv,
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
                # BUILT FROM NOTHING, not inherited. DATABASE_URL and every
                # agent key are simply absent from the child. HOME and a temp
                # dir are the only things the browser genuinely needs.
                env={"HOME": workdir, "TMPDIR": workdir, "PATH": "/usr/bin:/bin"},
                cwd=workdir,
                start_new_session=True,  # its own process group, so a timeout kills all of it
            )
        except subprocess.TimeoutExpired:
            log.warning("thumbnail render timed out after %ss", TIMEOUT_SECONDS)
            return None, None
        except (OSError, ValueError) as exc:
            log.warning("thumbnail render could not start: %s", exc)
            return None, None
        raw = getattr(done, "stderr", b"") or b""
        errors = console_errors(
            raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        )

        try:
            data = shot.read_bytes()
        except OSError:
            log.warning("thumbnail render produced no file")
            return None, errors

    # A real PNG or nothing. A zero-byte or truncated file would otherwise be
    # served as a broken image, which looks like the PAGE is broken.
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        log.warning("thumbnail render produced %d bytes that are not a PNG", len(data))
        return None, errors
    return data, errors


def render_in_background(db, artifact_id: str, version_n: int, html: str) -> None:
    """Render and store a thumbnail after the publish has already returned.

    A PUBLISH MUST NEVER WAIT ON A BROWSER, and must never fail because of one.
    A render takes the better part of a second on a good day and twenty on a bad
    one, and an agent publishing a page is usually mid-task with a waiter armed;
    making it pay for a decoration would be a poor trade even if the browser
    were reliable, which is the one thing a browser is not.

    So: a daemon thread, no result, no retry, everything swallowed. If it fails
    the page simply has no picture and the wall falls back to its text tile —
    and the next publish of that page tries again, which is the only retry this
    needs. A thumbnail that is missing is a tile that is slightly plainer; a
    publish that hung is an agent that stopped.

    Daemon so it can never hold up shutdown: SIGTERM starts a fifteen-second
    grace period, and a half-finished picture is worth none of it.
    """
    if not available():
        return

    def work() -> None:
        try:
            png, errors = render_report(html)
            if png is not None:
                store.save_thumbnail(db, artifact_id, version_n, png)
            if errors is not None:
                store.save_render_errors(db, artifact_id, version_n, errors)
        except Exception:  # a decoration may never reach the caller
            log.exception(
                "thumbnail rendering failed for %s v%s", artifact_id, version_n
            )

    threading.Thread(target=work, name=f"thumb-{artifact_id}", daemon=True).start()
