"""A picture of a page, for the gallery wall.

homelab-ij8.22. Jeremy asked for a real rendered thumbnail of each page rather
than a list of links, asked whether a tool existed, and — told that one did —
chose (b), the renderer running inside the service itself, over (a), a separate
renderer the service asks. He chose it with the argument against in front of
him, so this builds (b).

The browser runner itself (the constraints the child runs under, the memory
ceilings, the one render slot, the screenshot and its log) is in
`ream.rendercore`, which imports nothing of the database layer. This module
keeps what touches the store: the background render, its retries and the
saving. The runner's names are re-exported here so callers keep using them.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime

from ream import rendercore, renderfind, scanslots, stamp, store
from ream.rendercore import (  # noqa: F401 -- re-exported for callers and tests
    _PRLIMIT,
    _RENDER_SLOT,
    BROWSER_ENV,
    HEIGHT,
    RENDER_MAX_BYTES,
    RENDER_MAX_DATA_BYTES,
    RENDER_MAX_RSS_BYTES,
    SLOT_WAIT_SECONDS,
    TIMEOUT_SECONDS,
    WIDTH,
    RenderTooBig,
    Report,
    _data_limited,
    _group_rss,
    _pss_bytes,
    _run,
    _stat_pgrp_and_pages,
    available,
    browser_path,
    console_errors,
    too_big_to_render,
)

log = logging.getLogger("ream.thumbs")

# The turn-taking must not become a queue without end: a burst of publishes would
# park one thread per page, each holding its page. At most this many background
# renders wait or run; past that a page gets no picture this time, and its next
# publish tries again.
_BACKGROUND_SLOTS = threading.BoundedSemaphore(4)


def render(html: str) -> bytes | None:
    """A PNG of the top of `html`, or None.

    None on: no browser in this deployment, a crash, a timeout, an empty file,
    or anything that is not a PNG. Never raises — a thumbnail is decoration, and
    a decoration that can take down a publish is a bad trade.
    """
    return render_report(html)[0]


def render_report(
    html: str, width: int = WIDTH, height: int = HEIGHT
) -> tuple[bytes | None, list[str] | None]:
    """(PNG or None, the page's script errors or None when it did not run).
    `width`/`height` are the viewport, and so the shot: the tile's by default,
    a phone's for a preview (homelab-ij8.107)."""
    binary = browser_path()
    if binary is None:
        return None, None
    if too_big_to_render(html):
        log.info("no thumbnail: page is over %d bytes", RENDER_MAX_BYTES)
        return None, None
    # Stamped as a reader would see it. The marker is filled in when a page is
    # SERVED, not stored, so an unstamped render showed "DECISION · · <host label>" --
    # found by the first live preview, 24 Sep 2026, and true of every tile on
    # the wall. A render happens at publish or preview time, so now is the date.
    html = stamp.apply(html, datetime.now(UTC))
    return rendercore.screenshot(binary, html, width, height)


# Bounded retries per artifact version (homelab-o88p). A page whose render keeps
# failing (a timeout is 20 s of CPU and disk) must not be re-rendered forever:
# after MAX_FAILURES the version is given up on and logged once. Between
# attempts the wait doubles from BACKOFF_SECONDS. In memory on purpose: a
# restart is a fresh start, and the count only has to stop a loop within one
# process.
MAX_FAILURES = 3
BACKOFF_SECONDS = 60.0
_MAX_TRACKED = 2000
_failures: dict[tuple[str, int], tuple[int, float]] = {}
_failures_lock = threading.Lock()


def _may_render(key: tuple[str, int]) -> bool:
    with _failures_lock:
        count, last = _failures.get(key, (0, 0.0))
        if count == 0:
            return True
        if count >= MAX_FAILURES:
            return False
        return time.monotonic() - last >= BACKOFF_SECONDS * 2 ** (count - 1)


def _record(key: tuple[str, int], ok: bool) -> None:
    with _failures_lock:
        if ok:
            _failures.pop(key, None)
            return
        count = _failures.get(key, (0, 0.0))[0] + 1
        if len(_failures) >= _MAX_TRACKED and key not in _failures:
            _failures.pop(next(iter(_failures)))
        _failures[key] = (count, time.monotonic())
    if count == MAX_FAILURES:
        log.warning(
            "thumbnail render failed %d times for %s v%s; giving up on this version",
            count,
            key[0],
            key[1],
        )


def scan_for_findings(html: str) -> tuple[list[str] | None, bool | None]:
    """`renderfind.static_scan` for the render findings, under a scan slot. When every
    slot stays taken the page is unread, `(None, None)`: `renderfind.assess` then gives
    the verdict `unchecked` with its note that those checks did not run, never `ok`."""
    try:
        with scanslots.slot(len(html)):
            return renderfind.static_scan(html)
    except scanslots.ScanBusy:
        return None, None


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
    if not available() or too_big_to_render(html):
        return

    key = (artifact_id, version_n)
    if not _may_render(key):
        return
    if not _BACKGROUND_SLOTS.acquire(blocking=False):
        log.warning("thumbnail render skipped for %s v%s: too many waiting", *key)
        return

    def work() -> None:
        try:
            result = render_report(html)
            png, errors = result
            _record(key, png is not None)
            if png is not None:
                store.save_thumbnail(db, artifact_id, version_n, png)
            if errors is not None:
                missing, live = scan_for_findings(html)
                verdict, findings = renderfind.assess(
                    errors, getattr(result, "check", None), missing, live
                )
                store.save_render_errors(
                    db, artifact_id, version_n, errors, verdict, findings
                )
        except Exception:  # a decoration may never reach the caller
            log.exception(
                "thumbnail rendering failed for %s v%s", artifact_id, version_n
            )
        finally:
            _BACKGROUND_SLOTS.release()

    try:
        threading.Thread(target=work, name=f"thumb-{artifact_id}", daemon=True).start()
    except RuntimeError:  # no thread to be had: no picture, and the slot back
        _BACKGROUND_SLOTS.release()
