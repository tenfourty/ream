"""How many scans of a large page one server process runs at once.

Reading a large page (the publish checks, the render findings, the readers of its markup
for forms, sections, text and pins) may hold tens of megabytes and take up to two seconds
(`renderfind.SCAN_SECONDS`). One request thread each, a handful of them at once add up,
and the service is one process under one memory limit. So a scan of a page over
`SMALL_PAGE` characters takes a slot first: `REAM_SCAN_SLOTS` of them (default 4, read at
every scan through `settings.current()`). When all are taken it waits up to
`WAIT_SECONDS`, then raises `ScanBusy`, never waiting for good. What a caller does with
`ScanBusy` is its own to decide, and is written where it is caught:

* the publish checks (`api.page_warnings`) answer the warnings that say they did not run;
* the render findings (`thumbs.scan_for_findings`) leave the page unread, which `assess`
  turns into the verdict `unchecked`;
* the readers that a request needs the answer of (forms on publish and update, sections,
  the text and the headings behind a comment or a pin) let it go up: the two apps answer
  503 with `Retry-After` (`app._refuse_busy_scans`), and an MCP tool answers an error.

A thread that already holds a slot takes no second one, so a scan made of smaller scans
(the publish checks read forms inside theirs) never waits on itself. A slot is held only
across work that waits on nothing else, never across a database or network call, so it is
always given back within the scan's own time. Standard library and `settings` only: the
command-line tools import the readers that call it.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator

from ream import settings

# A page this small is read without a slot: its scan takes milliseconds and a few
# megabytes however many run at once. Counted in characters, as `len(body)` gives them.
SMALL_PAGE = 256 * 1024
WAIT_SECONDS = 5.0
RETRY_AFTER_SECONDS = 5
BUSY = "the server is busy reading other pages: try again in a few seconds"


class ScanBusy(Exception):
    """Every slot stayed taken for `WAIT_SECONDS`: the page was not read."""

    retry_after = RETRY_AFTER_SECONDS

    def __init__(self) -> None:
        super().__init__(BUSY)


class _Mine(threading.local):
    held = False


_mine = _Mine()
_changed = threading.Condition()
_taken = 0


def in_use() -> int:
    """How many slots are taken now (tests)."""
    with _changed:
        return _taken


@contextlib.contextmanager
def slot(size: int | None = None, wait: float | None = None) -> Iterator[None]:
    """Hold one scan slot for the block, or raise `ScanBusy` when none comes free in
    `wait` seconds (`WAIT_SECONDS` when None). `size` is the page's length in
    characters: a page of `SMALL_PAGE` or less takes none. Given back however the block
    ends."""
    global _taken
    if (size is not None and size <= SMALL_PAGE) or _mine.held:
        yield
        return
    limit = settings.current().scan_slot_count()
    deadline = time.monotonic() + (WAIT_SECONDS if wait is None else wait)
    with _changed:
        while _taken >= limit:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ScanBusy
            _changed.wait(left)
        _taken += 1
    _mine.held = True
    try:
        yield
    finally:
        _mine.held = False
        with _changed:
            _taken -= 1
            _changed.notify()
