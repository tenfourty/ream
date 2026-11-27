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

import hashlib
from datetime import UTC, datetime, timedelta

from ream import access, agents, events, store
from ream.db import Database

# At most this many watches per agent (a PUT past it is a 409; a repeat PUT is not past it).
# A soft limit: a page that was closed to agents and opens again counts against its watcher's
# cap once more, so an agent can sit above CAP; further new watches are refused until it is back
# under (nothing is removed).
CAP = 200
# At most this many watchers per page (a PUT past it is a 409; a repeat PUT by a watcher is not).
# The page's own agent never counts toward it and is never refused by it (`fan_out` skips it),
# and when the page looks full, watchers who can no longer read it are dropped before a new
# watch is refused, so `fan_out` stays bounded and a stale row cannot hold a place. What is left:
# 200 distinct live agents that can read the page can still fill it. An agent is an admin-minted
# key or a signed-in app (while `people.OPEN_SIGN_IN` is off), so that takes the owner's own doing.
# Dropped watches are gone for good, a page merely closed to agents included: reopening the page
# does not bring them back.
PAGE_CAP = 200
# At most one `watched_page_changed` per watcher and page in this long (decision C).
WINDOW = timedelta(minutes=10)


class TooMany(Exception):
    """The agent already holds `CAP` watches."""


class PageFull(Exception):
    """The page already has `PAGE_CAP` watchers."""


def _lock_key(namespace: str, name: str) -> int:
    """A stable signed 64-bit advisory-lock key for `name` within `namespace`. The namespaces
    here ("ream.watch.agent", "ream.watch.page") are hashed in, so neither can meet
    `db._MIGRATE_LOCK_KEY` or another module's key except by a 2**-64 accident."""
    digest = hashlib.blake2b(f"{namespace}\0{name}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def agent_lock_key(agent: str) -> int:
    return _lock_key("ream.watch.agent", agent)


def page_lock_key(artifact_id: str) -> int:
    return _lock_key("ream.watch.page", artifact_id)


def _reads(conn, agent: str, table: str = "artifact") -> tuple[str, tuple]:
    """The clause for "`agent` can read this page" with its parameters. A personal agent (an app
    agent, or a key with an `agent_person` row: `agents.person_of`) reads what its person reads, the
    same clause `store._owned` uses for it; a shared key reads its own pages and pages open to
    agents."""
    person = agents.person_of(conn, agent)
    if person is not None:
        return access.reads_sql(person, table)
    return f"({table}.agent = %s or {table}.agents_can_read)", (agent,)


def _can_read(conn, agent: str, artifact_id: str) -> bool:
    """Whether `agent` can read the live page `artifact_id` now: the rule `fan_out` applies."""
    clause, params = _reads(conn, agent)
    return (
        conn.execute(
            "select 1 from artifact where id = %s and deleted_at is null"  # noqa: S608 -- `clause` is built from fixed names
            f" and {clause}",
            (artifact_id, *params),
        ).fetchone()
        is not None
    )


def _page_is_full(conn, artifact_id: str, owner: str) -> bool:
    """Whether the page holds `PAGE_CAP` watchers besides its own agent. Only when it looks full
    are the others re-checked, and those who can no longer read the page are dropped (their
    watch was silent anyway), then it is counted again."""
    count = "select count(*) from page_watch where artifact_id = %s and agent <> %s"
    (watchers,) = conn.execute(count, (artifact_id, owner)).fetchone()
    if watchers < PAGE_CAP:
        return False
    others = conn.execute(
        "select agent from page_watch where artifact_id = %s and agent <> %s",
        (artifact_id, owner),
    ).fetchall()
    for (other,) in others:
        if not _can_read(conn, other, artifact_id):
            conn.execute(
                "delete from page_watch where agent = %s and artifact_id = %s",
                (other, artifact_id),
            )
    (watchers,) = conn.execute(count, (artifact_id, owner)).fetchone()
    return watchers >= PAGE_CAP


def watch(
    db: Database, agent: str, artifact_id: str, *, session: str | None = None
) -> bool:
    """Follow a page. True when the watch is new, False for a repeat (nothing changes). Raises
    `store.NotFound` for a page the agent cannot read now, `PageFull` when the page already has
    `PAGE_CAP` watchers (the page's own agent not counted, and never refused), and `TooMany`
    past `CAP`. A page that looks full first loses the watches of agents who can no longer read
    it, for good: those watches do not come back when the page reopens. Past that, 200 distinct live agents that can read the page can still fill it; agents are
    admin-minted keys or signed-in apps while `people.OPEN_SIGN_IN` is off.

    The counts and the insert run under two transaction locks, always taken page first, then
    agent, so two PUTs racing at the edge cannot both pass (a no-op on SQLite, whose writers
    are serialised). `CAP` is a soft limit: pages that reopen to agents count again, so an agent
    can end up above it, and then every new watch is refused until it is back under."""
    with db.connect() as conn, conn.transaction():
        conn.advisory_lock(page_lock_key(artifact_id))
        conn.advisory_lock(agent_lock_key(agent))
        page = store._owned(conn, artifact_id, agent, as_reader=True)
        if page is None:
            raise store.NotFound()
        if conn.execute(
            "select 1 from page_watch where agent = %s and artifact_id = %s",
            (agent, artifact_id),
        ).fetchone():
            return False
        if agent != page[2] and _page_is_full(conn, artifact_id, page[2]):
            raise PageFull()
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
    commit. The watchers it walks are bounded by `PAGE_CAP`, so the transaction stays small.
    `page` is a row whose first three columns are id, title and agent (what
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
        if not _can_read(conn, watcher, artifact_id):
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
