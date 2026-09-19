r"""The nudge journal: `append` writes the one row every interaction Jeremy has
with a page produces, and `list_for_agent` is how an agent -- on this machine or,
per Jeremy's own requirement, anywhere he gives access to -- reads them back.

`append` takes an open connection, not a `Database`: it is always called from
inside a caller's own `with db.connect() as conn:` block, in the same statement
group as the write the event describes (see `decisions.answer`, the first
caller), so the two commit or roll back together. Calling it with a fresh
connection of its own would defeat the entire point -- an event that outlives a
rolled-back cause, or a cause with no event at all -- so this module never opens
one itself for a write.

`list_for_agent` is the opposite: always a fresh, read-only connection, exactly
like `decisions.list_for_agent`/`comments.list_for_agent`. It is scoped to the
calling agent alone (never another agent's events) and ordered strictly by
`seq`, which is what makes `since` a real cursor -- two events sharing one
`created_at` still come back in a fixed, repeatable order, so a caller polling
with `since=<last seq it saw>` can never skip or repeat a row.
"""

import threading
import time
from dataclasses import dataclass

from artifacts.db import Database

# The three kinds the design names (schema.sql's own CHECK constraint is the
# enforced copy of this set) -- defined here even though only
# "decision_answered" has a live producer this slice, so wiring up the other
# two later is a line of code at a new call site, never a migration to widen
# what this journal accepts.
KINDS = ("decision_answered", "comment_added", "thread_resolved")

_COLUMNS = (
    "event.seq, event.artifact_id, event.agent, event.actor, event.kind,"
    " event.summary, event.ref_id, event.created_at"
)


@dataclass(frozen=True)
class EventRow:
    seq: int
    artifact_id: str
    agent: str
    actor: str
    kind: str
    summary: str
    ref_id: str | None
    created_at: str


def _row_to_event(row) -> EventRow:
    return EventRow(
        seq=row[0],
        artifact_id=row[1],
        agent=row[2],
        actor=row[3],
        kind=row[4],
        summary=row[5],
        ref_id=row[6],
        created_at=row[7].isoformat(),
    )


def append(
    conn,
    *,
    artifact_id: str,
    agent: str,
    actor: str,
    kind: str,
    summary: str,
    ref_id: str | None = None,
) -> int:
    """Insert one event row on `conn`, inside the caller's own transaction --
    see the module docstring. `agent` is the seat the event is FOR (the
    artifact's owner); `actor` is WHO caused it, and the caller must always
    say so explicitly -- this function never infers one from the other or
    from `kind`, because an event means "something happened on your page
    that you did not do", and inferring `actor` is exactly how that stops
    being true the first time it's wrong (see the module docstring, and
    `test_events.py`'s own test for this).

    Returns the new row's `seq`, which the caller passes to `nudge.emit` as
    `event_id` only after `conn`'s transaction has actually committed: a
    nudge for a row that then rolls back would tell another agent about
    something that never happened."""
    row = conn.execute(
        "insert into event (artifact_id, agent, actor, kind, summary, ref_id)"
        " values (%s, %s, %s, %s, %s, %s) returning seq",
        (artifact_id, agent, actor, kind, summary, ref_id),
    ).fetchone()
    return row[0]


def list_for_agent(
    db: Database, agent: str, *, since: int = 0, limit: int = 100
) -> list[EventRow]:
    """Every event recorded for `agent`, strictly above `since`, oldest first --
    the order a cursor needs: a caller that stores the last `seq` it saw and asks
    again with `since=<that seq>` sees each row exactly once, in the order it was
    written. `limit` protects an agent that has been away a long time from
    pulling an unbounded backlog in one call."""
    sql = (
        f"select {_COLUMNS} from event where event.agent = %s and event.seq > %s"  # noqa: S608 -- _COLUMNS is a fixed module constant, never caller input
        " order by event.seq asc limit %s"
    )
    with db.connect() as conn:
        rows = conn.execute(sql, (agent, since, limit)).fetchall()
    return [_row_to_event(row) for row in rows]


# How many agents may hold a waiting request at once. Each one occupies a worker
# thread for as long as it waits, and FastAPI's thread pool is finite: without a
# ceiling, enough waiters would starve every ordinary request in the app,
# including the gallery Jeremy is reading. Past the ceiling a caller is answered
# immediately with whatever is there, which is exactly the behaviour it had
# before waiting existed -- it degrades to ordinary polling rather than failing.
MAX_WAITERS = 16

# How often a waiting request looks again. A held request is woken by finding a
# row, not by being told, so this is the worst-case delay between Jeremy pressing
# a button and the agent knowing. Half a second is imperceptible to him and costs
# one indexed lookup per waiter per half-second -- the index is (agent, seq), the
# same one an ordinary poll uses.
_LOOK_EVERY = 0.5

_waiters = threading.BoundedSemaphore(MAX_WAITERS)


def wait_for_agent(
    db: Database,
    agent: str,
    *,
    since: int = 0,
    limit: int = 100,
    seconds: float,
) -> list[EventRow]:
    """Hold until an event lands for `agent` above `since`, or `seconds` pass.

    This is the mechanism by which an agent anywhere hears about an answer.
    A push has to know an address for every agent; a held request does not --
    the agent is already on the other end of it, so answering it IS the
    notification, and an agent on a laptop is served exactly like one on baba.

    Returns whatever is there when it gives up, which may be nothing: an empty
    answer means "nothing yet, ask again", never an error, so a caller loops on
    it forever without special-casing a timeout.

    Never holds a database connection while it sleeps -- each look is its own
    short connection through `list_for_agent` -- so a hundred waiting agents
    cost sleeping threads, not a hundred pooled connections nobody can use.
    """
    if not _waiters.acquire(blocking=False):
        # Too many agents already waiting. Answer now with whatever is there
        # rather than queueing behind them: a caller that gets an empty answer
        # early simply asks again, which is ordinary polling.
        return list_for_agent(db, agent, since=since, limit=limit)
    try:
        deadline = time.monotonic() + seconds
        while True:
            rows = list_for_agent(db, agent, since=since, limit=limit)
            if rows:
                return rows
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            # Never overshoot the deadline: a caller that asked for 30 seconds
            # gets at most 30, not 30 rounded up to the next look.
            time.sleep(min(_LOOK_EVERY, remaining))
    finally:
        _waiters.release()
