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
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from ream import decisions, partner, partner_in, settings, store
from ream.db import Database

_log = logging.getLogger(__name__)
_lock = threading.Lock()


class AnswerFailed(Exception):
    """A plug-in's `answer` raised something other than a store refusal: the answer was
    not saved. Names the plug-in, never the fault; the gallery answers 503."""

    def __init__(self, plugin: str) -> None:
        super().__init__(f"plug-in {plugin!r} read-side answer failed")
        self.plugin = plugin


@dataclass(frozen=True)
class Contributor:
    plugin: str
    open_copies: Callable | None = None
    settled_copies: Callable | None = None
    answer: Callable | None = None
    enabled: Callable | None = None
    outbox_status: Callable | None = None
    delivered: Callable | None = None


# What a contributor's `answer` may raise on purpose besides NotFound: the store's refusals
# and a wrong letter.
_PASS = (
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


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _moment(value) -> datetime:
    """A copy's stored time (an aware datetime or an ISO string) for ordering; never raises."""
    if isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)


def _text_or_none(value) -> bool:
    return value is None or isinstance(value, str)


def _sound(i) -> bool:
    """Whether a plug-in's value is shaped as the readers need it, so one wrong field
    cannot break a sort or a render."""
    return (
        isinstance(i, partner_in.Copy)
        and isinstance(i.source, str)
        and bool(i.source)
        and isinstance(i.created_at, datetime)
        and isinstance(i.title, str)
        and isinstance(i.asking_office, str)
        and isinstance(i.options, list)
        and all(isinstance(o, dict) for o in i.options)
        and (i.topic is None or isinstance(i.topic, dict))
        and isinstance(i.depends_on, list)
        and isinstance(i.for_offices, list)
        and _text_or_none(i.answered_at)
        and _text_or_none(i.needed_by)
        and _text_or_none(i.pending_at)
    )


def _copies(c: Contributor, got) -> list[partner_in.Copy]:
    """Only well-shaped `partner_in.Copy` values pass (`_sound`), with `created_at` made
    aware (naive means UTC, as `inbox.moment` reads it); anything else is dropped and the
    contributor counted once."""
    if got is None:
        return []
    try:
        items = list(got)
    except TypeError:
        _fail(c.plugin, "NotAList")
        return []
    good = [replace(i, created_at=_aware(i.created_at)) for i in items if _sound(i)]
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
    merged.sort(key=lambda x: _moment(x.answered_at), reverse=True)
    return merged[:limit]


def count_waiting(db: Database, limit: int = 500) -> int:
    """How many open copies wait on him, counted the way the list is built: core's count,
    plus each plug-in copy from `open_copies(waiting_on_him=True)` whose source core and
    earlier plug-ins do not already hold, so the badge and the list never disagree."""
    total = partner_in.count_waiting(db)
    extra: list[partner_in.Copy] = []
    for c in contributors():
        extra += _copies(c, _call(c, c.open_copies, db, limit, waiting_on_him=True))
    if not extra:
        return total
    held = {x.source for x in partner_in.open_copies(db, limit)}
    for copy in extra:
        if copy.source not in held:
            held.add(copy.source)
            total += 1
    return total


def answer_here(
    db: Database, qid: str, *, letter: str, note: str = ""
) -> partner_in.Copy:
    """He answers a copy in the gallery. Core's own copies first; a question core does
    not hold goes to each contributor in turn, which raises `store.NotFound` for one that is
    not its own. `decisions.InvalidAnswer` and the store's refusals pass through from
    whoever owns the question; any other fault in a contributor (or a result that is not a `Copy`) is counted and
    raises `AnswerFailed`, which the gallery answers 503: an outage never reads as 404."""
    try:
        return partner_in.answer_here(db, qid, letter=letter, note=note)
    except store.NotFound:
        pass
    # Core does not hold it. Before any plug-in sees the note, run core's own note gates
    # once (as `partner_in.answer_here` does), so a plug-in's path is held to the same
    # size and secret checks. Core keeps its own gate, so a core-only install keeps its
    # order (404 first) and scans once.
    store._check_size(
        note,
        decisions.MAX_NOTE_BYTES,
        "note",
        agent=settings.owner_id(),
        artifact_id=None,
    )
    store._check_secrets(note, "note", agent=settings.owner_id(), artifact_id=None)
    for c in contributors():
        if c.answer is None:
            continue
        try:
            got = c.answer(db, qid, letter=letter, note=note)
        except store.NotFound:
            continue  # not this plug-in's: the next may own it
        except _PASS:
            raise
        except Exception as exc:  # noqa: BLE001 - a plug-in's fault is isolated
            _fail(c.plugin, type(exc).__name__)
            raise AnswerFailed(c.plugin) from None
        if isinstance(got, partner_in.Copy):
            return got
        _fail(c.plugin, "BadCopy")
        raise AnswerFailed(c.plugin)
    raise store.NotFound()


def bridge_on() -> bool:
    """Whether a bridge is running here: core's own, or any contributor's `enabled()`."""
    if partner.enabled():
        return True
    for c in contributors():
        if _call(c, c.enabled):
            return True
    return False


def delivered(db: Database, decision_ids: list[str]) -> dict[str, bool]:
    """Of these questions (whose home is here), which a contributor's outbox holds an
    `asked` for, and whether it went out (API 1.4): the listening state's "sent" for a
    question announced after the switch to a plug-in's own outbox. Merged by "any says
    delivered". A contributor that raises or answers other than a dict of str to bool is
    counted and ignored; ids not asked for are dropped. Core's own `partner_outbox` is
    read by the caller."""
    asked = set(decision_ids)
    out: dict[str, bool] = {}
    if not asked:
        return out
    for c in contributors():
        got = _call(c, c.delivered, db, list(decision_ids))
        if got is None:
            continue
        if not isinstance(got, dict) or not all(
            isinstance(k, str) and isinstance(v, bool) for k, v in got.items()
        ):
            _fail(c.plugin, "BadDelivered")
            continue
        for key, sent in got.items():
            if key in asked:
                out[key] = out.get(key, False) or sent
    return out


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
