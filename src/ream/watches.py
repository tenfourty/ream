"""An agent follows a page it can read (agents design, slice 3, docs/agent/specs/
2026-11-26-agents-and-watching-design.md, "Watching").

A watch is a row `(agent, artifact_id)` in `page_watch`. `watch` takes one for a page the agent
can read at that moment (a key: its own pages and pages open to agents; a signed-in app: what
its person can read, `access.reads_sql`), and a page it cannot read answers `store.NotFound`,
the same as one that does not exist. `fan_out` is the write side: called inside the transaction
of a change (a version minted, a comment added), it writes each reading watcher an event of
its own kind through `events.emit`, so the journal, the outbox and the after-commit sinks see it
as they see any event. The two kinds are `watched_page_changed` and `watched_comment_added`
(`events.WATCHED_KINDS`): a feed shows them, and a waiter wakes on them, only for a caller that
asks (`include_watched`).

Who is skipped: the actor, the page's own agent (it hears through its own events), anyone who
can no longer read the page (the watch stays and goes silent), and for a comment the agents that
already receive it as `comment_added` (`skip`). `watched_page_changed` is coalesced: one per
watcher and page every `WINDOW`, claimed with one conditional update of
`page_watch.changed_notified_at`, so two changes racing each other announce once.

A watch on a page the agent lost access to stays but drops out of `listing` and out of the cap,
as if it were never there; deleting the page cascades the rows away. A rename merges them
(`rename._merge_watches`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ream import access, events, store
from ream.db import Database

# At most this many watches per agent (a PUT past it is a 409; a repeat PUT is not past it).
CAP = 200
# At most one `watched_page_changed` per watcher and page in this long (decision C).
WINDOW = timedelta(minutes=10)


class TooMany(Exception):
    """The agent already holds `CAP` watches."""


def _reads(conn, agent: str, table: str = "artifact") -> tuple[str, tuple]:
    """The clause for "`agent` can read this page" with its parameters. An app agent (a row in
    `mcp_agent`) reads what its person reads; any other agent is a key: its own pages and pages
    open to agents."""
    row = conn.execute(
        "select person from mcp_agent where agent = %s", (agent,)
    ).fetchone()
    if row is not None:
        return access.reads_sql(row[0], table)
    return f"({table}.agent = %s or {table}.agents_can_read)", (agent,)


def watch(
    db: Database, agent: str, artifact_id: str, *, session: str | None = None
) -> bool:
    """Follow a page. True when the watch is new, False for a repeat (nothing changes). Raises
    `store.NotFound` for a page the agent cannot read now, and `TooMany` past `CAP`."""
    with db.connect() as conn, conn.transaction():
        if store._owned(conn, artifact_id, agent, as_reader=True) is None:
            raise store.NotFound()
        if conn.execute(
            "select 1 from page_watch where agent = %s and artifact_id = %s",
            (agent, artifact_id),
        ).fetchone():
            return False
        # What counts against the cap is what the agent can see in its list.
        if len(_listing(conn, agent)) >= CAP:
            raise TooMany()
        conn.execute(
            "insert into page_watch (agent, artifact_id, session) values (%s, %s, %s)"
            " on conflict (agent, artifact_id) do nothing",
            (agent, artifact_id, session),
        )
    return True


def unwatch(db: Database, agent: str, artifact_id: str) -> bool:
    """Stop following. True when a watch was removed, False when there was none. A page the
    agent holds no watch on and cannot read raises `store.NotFound`, as everywhere: a page
    it once watched and lost access to can still be let go."""
    with db.connect() as conn, conn.transaction():
        removed = conn.execute(
            "delete from page_watch where agent = %s and artifact_id = %s",
            (agent, artifact_id),
        ).rowcount
        if removed:
            return True
        if store._owned(conn, artifact_id, agent, as_reader=True) is None:
            raise store.NotFound()
    return False


def _listing(conn, agent: str) -> list[dict]:
    clause, params = _reads(conn, agent, "a")
    rows = conn.execute(
        "select a.id, a.title, a.agent, a.current_version, a.updated_at, w.created_at"  # noqa: S608 -- `clause` is built from fixed names
        " from page_watch w join artifact a on a.id = w.artifact_id"
        f" where w.agent = %s and a.deleted_at is null and {clause}"
        " order by w.created_at desc, a.id",
        (agent, *params),
    ).fetchall()
    return [
        {
            "id": r[0],
            "title": r[1],
            "agent": r[2],
            "version": r[3],
            "updated_at": r[4],
            "watching_since": r[5],
            "page_url": store.page_url_for(r[0]),
        }
        for r in rows
    ]


def listing(db: Database, agent: str) -> list[dict]:
    """What `agent` watches and can still read, newest watch first."""
    with db.connect() as conn:
        return _listing(conn, agent)


def fan_out(
    conn,
    page,
    kind: str,
    *,
    actor: str,
    summary: str,
    ref_id: str | None = None,
    skip: tuple[str, ...] = (),
    note: str = "",
    data: dict | None = None,
) -> list[events.Emission]:
    """Write one event of `kind` to each watcher of `page` who may hear it, on `conn` (the
    change's own transaction), and return the emissions for the caller to `fire()` after the
    commit. `page` is a row whose first three columns are id, title and agent (what
    `store._owned` returns). Never raises for a watcher it cannot place."""
    if kind not in events.WATCHED_KINDS:
        raise ValueError(f"not a watching kind: {kind!r}")
    artifact_id, title, owner = page[0], page[1], page[2]
    watchers = conn.execute(
        "select agent, session from page_watch where artifact_id = %s"
        " and agent <> %s order by agent",
        (artifact_id, owner),
    ).fetchall()
    emissions: list[events.Emission] = []
    now = datetime.now(UTC)
    for watcher, session in watchers:
        if watcher == actor or watcher in skip:
            continue
        # Read at the moment of the change, by the same rule `watch` used.
        clause, params = _reads(conn, watcher)
        can = conn.execute(
            "select 1 from artifact where id = %s and deleted_at is null"  # noqa: S608 -- `clause` is built from fixed names
            f" and {clause}",
            (artifact_id, *params),
        ).fetchone()
        if can is None:
            continue
        if kind == "watched_page_changed":
            # One claim, atomic: the window opens for the one change that wins the update.
            claimed = conn.execute(
                "update page_watch set changed_notified_at = %s"
                " where agent = %s and artifact_id = %s"
                " and (changed_notified_at is null or changed_notified_at <= %s)",
                (now, watcher, artifact_id, now - WINDOW),
            ).rowcount
            if not claimed:
                continue
        emissions.append(
            events.emit(
                conn,
                events.Event(
                    kind=kind,
                    artifact_id=artifact_id,
                    agent=watcher,
                    actor=actor,
                    summary=summary,
                    ref_id=ref_id,
                    note=note,
                    session=session,
                    data={"title": title, **(data or {})},
                ),
            )
        )
    return emissions
