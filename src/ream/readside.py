"""The read side of the partner bridge, one place (core-and-plugins design, step 9e-2).

The inbox, the gallery, the question list, the listening state and the outbox report read
"copies" (questions whose home is a chat partner), the bridge's on/off state and its outbox
from here, never from `partner_in` or `partner` directly. Core's own partner code is the
built-in contributor and always answers first; a plug-in adds its own through
`api.read_side(...)` (API 1.3) and its answers are merged in behind core's.

A contributor is isolated like every plug-in call: one that raises, or returns something of
the wrong shape, is logged by exception type only, counted as `<plug-in>:read_side`, and
treated as if it had contributed nothing, so a page still renders.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ream import decisions, partner, partner_in, settings, store
from ream.db import Database

_log = logging.getLogger(__name__)
_lock = threading.Lock()


@dataclass(frozen=True)
class Contributor:
    plugin: str
    open_copies: Callable | None = None
    settled_copies: Callable | None = None
    count_waiting: Callable | None = None
    answer: Callable | None = None
    enabled: Callable | None = None
    outbox_status: Callable | None = None


# What a contributor's `answer` may raise on purpose: the store's refusals and a wrong letter.
_PASS = (
    store.NotFound,
    store.BodyTooLarge,
    store.InvalidText,
    store.SecretFound,
    store.ScanFailed,
    decisions.InvalidAnswer,
)

# Registration order. Tests replace the list.
_CONTRIBUTORS: list[Contributor] = []


def register(contributor: Contributor) -> None:
    with _lock:
        _CONTRIBUTORS.append(contributor)


def contributors() -> list[Contributor]:
    with _lock:
        return list(_CONTRIBUTORS)


def _fail(plugin: str, why: str) -> None:
    from ream import plugin_api

    plugin_api.count_failure(plugin, "read_side")
    _log.warning("plug-in %s: read side failed: %s", plugin, why)


def _call(c: Contributor, fn: Callable | None, *args, **kwargs):
    """`fn`'s answer, or None when it is absent or raised (counted)."""
    if fn is None:
        return None
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a plug-in's fault is isolated
        _fail(c.plugin, type(exc).__name__)
        return None


def _copies(c: Contributor, got) -> list[partner_in.Copy]:
    """Only real `partner_in.Copy` values with a text `source` and a datetime
    `created_at` pass; anything else is dropped and the contributor counted once."""
    if got is None:
        return []
    try:
        items = list(got)
    except TypeError:
        _fail(c.plugin, "NotAList")
        return []
    good = [
        i
        for i in items
        if isinstance(i, partner_in.Copy)
        and isinstance(i.source, str)
        and i.source
        and isinstance(i.created_at, datetime)
    ]
    if len(good) != len(items):
        _fail(c.plugin, "BadCopy")
    return good


def _merge(core: list, extra: list[partner_in.Copy]) -> list:
    """Core's copies first; a plug-in's copy whose source core already holds is dropped
    (and one plug-in's repeat of another's), so a source appears once."""
    seen = {c.source for c in core}
    out = list(core)
    for copy in extra:
        if copy.source not in seen:
            seen.add(copy.source)
            out.append(copy)
    return out


def open_copies(
    db: Database, limit: int = 500, *, waiting_on_him: bool = True
) -> list[partner_in.Copy]:
    """Open copies, oldest first, from core and every contributor."""
    core = partner_in.open_copies(db, limit, waiting_on_him=waiting_on_him)
    extra: list[partner_in.Copy] = []
    for c in contributors():
        extra += _copies(
            c, _call(c, c.open_copies, db, limit, waiting_on_him=waiting_on_him)
        )
    if not extra:
        return core
    merged = _merge(core, extra)
    merged.sort(key=lambda x: (x.created_at, x.source))
    return merged[:limit]


def settled_copies(
    db: Database, since: datetime, limit: int = 50
) -> list[partner_in.Copy]:
    """Copies answered since `since`, the latest answer first."""
    core = partner_in.settled_copies(db, since, limit)
    extra: list[partner_in.Copy] = []
    for c in contributors():
        extra += _copies(c, _call(c, c.settled_copies, db, since, limit))
    if not extra:
        return core
    merged = _merge(core, extra)
    merged.sort(key=lambda x: x.answered_at or "", reverse=True)
    return merged[:limit]


def count_waiting(db: Database) -> int:
    """How many open copies wait on him, across core and every contributor. A
    contributor's count is trusted only as a non-negative integer."""
    total = partner_in.count_waiting(db)
    for c in contributors():
        got = _call(c, c.count_waiting, db)
        if got is None:
            continue
        if isinstance(got, bool) or not isinstance(got, int) or got < 0:
            _fail(c.plugin, "BadCount")
            continue
        total += got
    return total


def answer_here(
    db: Database, qid: str, *, letter: str, note: str = ""
) -> partner_in.Copy:
    """He answers a copy in the gallery. Core's own copies first; a question core does
    not hold goes to each contributor in turn, which raises `store.NotFound` for one that is
    not its own. `decisions.InvalidAnswer` and the store's refusals pass through from
    whoever owns the question; any other fault in a contributor is counted and that
    contributor is skipped, so the last answer is the same 404 as for a missing id."""
    # The note's gates run here once, before core's call or any plug-in's, so a plug-in's
    # answer path is held to the same size and secret checks as core's (partner_in.answer_here).
    store._check_size(
        note,
        decisions.MAX_NOTE_BYTES,
        "note",
        agent=settings.owner_id(),
        artifact_id=None,
    )
    store._check_secrets(note, "note", agent=settings.owner_id(), artifact_id=None)
    try:
        return partner_in.answer_here(db, qid, letter=letter, note=note)
    except store.NotFound:
        pass
    for c in contributors():
        if c.answer is None:
            continue
        try:
            got = c.answer(db, qid, letter=letter, note=note)
        except _PASS:
            raise
        except Exception as exc:  # noqa: BLE001 - a plug-in's fault is isolated
            _fail(c.plugin, type(exc).__name__)
            continue
        if isinstance(got, partner_in.Copy):
            return got
        _fail(c.plugin, "BadCopy")
    raise store.NotFound()


def bridge_on() -> bool:
    """Whether a bridge is running here: core's own, or any contributor's `enabled()`."""
    if partner.enabled():
        return True
    for c in contributors():
        if _call(c, c.enabled):
            return True
    return False


def outbox_statuses(db: Database) -> dict[str, dict]:
    """Each contributor's own outbox report, by plug-in: whatever small dict of counts and
    codes it returns (never a body or a key). One that is not a dict is dropped."""
    out: dict[str, dict] = {}
    for c in contributors():
        got = _call(c, c.outbox_status, db)
        if got is None:
            continue
        if not isinstance(got, dict):
            _fail(c.plugin, "BadStatus")
            continue
        out[c.plugin] = got
    return out
