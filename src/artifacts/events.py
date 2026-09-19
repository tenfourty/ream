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

from dataclasses import dataclass

from artifacts.db import Database

_COLUMNS = (
    "event.seq, event.artifact_id, event.agent, event.kind, event.summary,"
    " event.ref_id, event.created_at"
)


@dataclass(frozen=True)
class EventRow:
    seq: int
    artifact_id: str
    agent: str
    kind: str
    summary: str
    ref_id: str | None
    created_at: str


def _row_to_event(row) -> EventRow:
    return EventRow(
        seq=row[0],
        artifact_id=row[1],
        agent=row[2],
        kind=row[3],
        summary=row[4],
        ref_id=row[5],
        created_at=row[6].isoformat(),
    )


def append(
    conn,
    *,
    artifact_id: str,
    agent: str,
    kind: str,
    summary: str,
    ref_id: str | None = None,
) -> int:
    """Insert one event row on `conn`, inside the caller's own transaction --
    see the module docstring. Returns the new row's `seq`, which the caller
    passes to `nudge.emit` as `event_id` only after `conn`'s transaction has
    actually committed: a nudge for a row that then rolls back would tell
    another agent about something that never happened."""
    row = conn.execute(
        "insert into event (artifact_id, agent, kind, summary, ref_id)"
        " values (%s, %s, %s, %s, %s) returning seq",
        (artifact_id, agent, kind, summary, ref_id),
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
