"""How often a page is read, and by whom (22 Nov 2026, runcove-5t3oj.158.1;
docs/agent/specs/2026-11-22-view-stats-design.md).

Two small tables, never a row per load: `page_view_day` (views per page, UTC day and kind)
and `page_viewer` (one row per page, kind and viewer: the total, the last open, and the last
COUNTED view, from which the dedupe window runs). `record` is the only writer and is called
from a surface where a reader actually receives a page body, never from `store`, which
screenshots and thumbnails read through.

The three kinds: `owner` (the one person who reads the gallery; viewer is '' and never his
id), `external` (a named email recipient, viewer is the `email_share.id`; or the anonymous
link, viewer '') and `agent` (another agent's read of the body, viewer is its name).

`record` is best-effort. Counting is never worth failing a request over, so it catches what
the database throws, logs the exception's TYPE only, and returns False. It emits no event:
`event` rows wake waiting agents, and a reader opening a page is not news.

The first counted view also writes the `service_meta` key `views_since` (its UTC day, once;
the migration seeds nothing, so `migrate.copy` finds the tables empty). A reader of it treats
a missing key as "no views yet".

CALLING IT (slice 2): `record` opens its own connection and transaction, so call it only
AFTER the request's own transaction has closed. On SQLite one writer holds the lock, and a
write attempted while the caller's own is still open waits out the busy timeout and then
fails: a view silently lost (it is best-effort, so nothing says so). It is also synchronous
database work: call it from a sync route or a worker thread, never straight from an `async`
handler's event loop.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ream.db import Database

_log = logging.getLogger("ream.pageviews")

OWNER = "owner"
EXTERNAL = "external"
AGENT = "agent"
KINDS = (OWNER, EXTERNAL, AGENT)

# A view of the same page by the same (kind, viewer) inside this long of the last counted
# one is not counted again; a reload or a polling agent is one reader, not many.
WINDOW = timedelta(minutes=30)
# The anonymous link names no one, so its readers are one viewer: a shorter window, so two
# people opening the link a few minutes apart still count twice.
ANONYMOUS_WINDOW = timedelta(minutes=5)

# The `service_meta` key holding the UTC day of the first counted view (absent: none yet).
VIEWS_SINCE = "views_since"


def window_for(kind: str, viewer: str) -> timedelta:
    return ANONYMOUS_WINDOW if kind == EXTERNAL and viewer == "" else WINDOW


def _valid(kind: str, viewer: str) -> bool:
    if kind not in KINDS:
        return False
    if kind == OWNER:
        return viewer == ""  # never the owner id
    if kind == AGENT:
        return viewer != ""  # an agent has a name
    return True


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def record(
    db: Database,
    artifact_id: str,
    kind: str,
    viewer: str,
    now: datetime | None = None,
) -> bool:
    """Note that `viewer` of `kind` was served the body of page `artifact_id`. True when it
    counted, False when it fell inside the dedupe window or could not be recorded (an
    unknown `kind`, a `viewer` the kind refuses, a page that does not exist, a database
    that failed). Never raises.

    One transaction: insert the viewer's row (a first view, counted); else bump the count
    where the last counted view is at least a window old (a view exactly one window later
    counts); else only move `last_at` forward, never back. A counted view adds one to the
    page's row for the UTC day, and the first ever writes `views_since`."""
    if not _valid(kind, viewer):
        _log.warning("page view not recorded: kind or viewer refused")
        return False
    try:
        at = _utc(now) if now is not None else datetime.now(UTC)
        cutoff = at - window_for(kind, viewer)
        with db.connect() as conn, conn.transaction():
            counted = (
                conn.execute(
                    "insert into page_viewer"
                    " (artifact_id, kind, viewer, views, last_at, counted_at)"
                    " values (%s, %s, %s, 1, %s, %s)"
                    " on conflict (artifact_id, kind, viewer) do nothing"
                    " returning artifact_id",
                    (artifact_id, kind, viewer, at, at),
                ).fetchone()
                is not None
            )
            if not counted:
                counted = (
                    conn.execute(
                        "update page_viewer set views = views + 1,"
                        " last_at = case when last_at < %s then %s else last_at end,"
                        " counted_at = %s"
                        " where artifact_id = %s and kind = %s and viewer = %s"
                        " and counted_at <= %s returning artifact_id",
                        (at, at, at, artifact_id, kind, viewer, cutoff),
                    ).fetchone()
                    is not None
                )
            if not counted:
                conn.execute(
                    "update page_viewer"
                    " set last_at = case when last_at < %s then %s else last_at end"
                    " where artifact_id = %s and kind = %s and viewer = %s",
                    (at, at, artifact_id, kind, viewer),
                )
                return False
            conn.execute(
                "insert into page_view_day (artifact_id, day, kind, views)"
                " values (%s, %s, %s, 1)"
                " on conflict (artifact_id, day, kind) do update"
                " set views = page_view_day.views + 1",
                (artifact_id, at.strftime("%Y-%m-%d"), kind),
            )
            conn.execute(
                "insert into service_meta (key, value) values (%s, %s)"
                " on conflict (key) do nothing",
                (VIEWS_SINCE, at.strftime("%Y-%m-%d")),
            )
        return True
    except Exception as exc:  # noqa: BLE001 - best-effort: the type is logged, the request goes on
        _log.warning("page view not recorded (%s)", type(exc).__name__)
        return False


def record_agent_read(
    db: Database,
    artifact_id: str,
    reader: str,
    writable: Collection[str] | None = None,
) -> bool:
    """`reader` was served the body of page `artifact_id`: count it as an agent view unless
    `reader` is the page's own agent (the publisher reading back what it published, or
    polling, is not a reader; office decision A). `writable` is every agent whose pages the
    caller may change (a signed-in app's `reach.writes()`: its person's app agents are one
    publisher; office decision D); None means `reader` alone, as for a key. Same rules as `record`: AFTER the request's
    own transaction, never raises."""
    try:
        with db.connect() as conn:
            row = conn.execute(
                "select agent from artifact where id = %s", (artifact_id,)
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 - best-effort, as `record`
        _log.warning("page view not recorded (%s)", type(exc).__name__)
        return False
    if row is None or row[0] in (writable if writable else (reader,)):
        return False
    return record(db, artifact_id, AGENT, reader)


@dataclass(frozen=True)
class Viewer:
    kind: str
    viewer: str
    views: int
    last_at: datetime
    counted_at: datetime


def daily(
    db: Database, artifact_id: str, days: int = 30, now: datetime | None = None
) -> list[dict]:
    """The last `days` (clamped to 1..365) UTC days of a page, today last, zero-filled:
    `[{"day": "2026-11-22", "owner": 0, "external": 3, "agent": 1}, ...]`."""
    days = max(1, min(365, days))
    today = (_utc(now) if now is not None else datetime.now(UTC)).date()
    first = today - timedelta(days=days - 1)
    rows = {
        day: {kind: 0 for kind in KINDS}
        for day in ((first + timedelta(days=n)).isoformat() for n in range(days))
    }
    with db.connect() as conn:
        found = conn.execute(
            "select day, kind, views from page_view_day"
            " where artifact_id = %s and day >= %s and day <= %s",
            (artifact_id, first.isoformat(), today.isoformat()),
        ).fetchall()
    for day, kind, views in found:
        if day in rows and kind in rows[day]:
            rows[day][kind] = views
    return [{"day": day, **counts} for day, counts in rows.items()]


def viewers(db: Database, artifact_id: str) -> list[Viewer]:
    """Every (kind, viewer) that has read a page, most recent first."""
    with db.connect() as conn:
        found = conn.execute(
            "select kind, viewer, views, last_at, counted_at from page_viewer"
            " where artifact_id = %s order by last_at desc, kind, viewer",
            (artifact_id,),
        ).fetchall()
    return [Viewer(*row) for row in found]
