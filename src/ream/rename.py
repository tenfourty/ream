"""Renaming an agent without breaking anything it has published (6 Oct 2026).

Jeremy renamed every permanent agent from "seat" to "<area> maintainer", and
one of those renames is a MERGE: baba-operator and homelab-ops both become
homelab-maintainer, one agent with two keys. The rule that shapes all of it:
nothing already published may break or change owner. So a rename rewrites
every column whose value is an AGENT NAME -- live rows and history alike --
in one transaction, and leaves an alias so a name somebody types still means
the agent it became.

Deliberately NOT rewritten: SESSION labels (`*.session`,
`review_round.claimed_by` / `released_by`). They read like
"artifacts-seat/5ed2e1c5" but they are what a session called itself at the
time, not a reference to an agent, and nothing resolves them to one.
`connect_request.suggested_name` is what a machine proposed before Jeremy
named it, and `agent_key.replaces` is a key id.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ream.db import Database, Rollback
from ream.keys import (
    AGENT_NAME,
    NAME_LOCK_KEY,
    RESERVED_REASON,
    is_reserved_agent_name,
)

# The one name lock (keys.NAME_LOCK_KEY): two renames, or a rename and a first
# consent or a new key's name, serialise here.
_RENAME_LOCK_KEY = NAME_LOCK_KEY

# (table.column, the WHERE clause beyond `column = old`). Plain columns only:
# the three tables with a key on `agent` are merged separately, below.
_PLAIN: tuple[tuple[str, str], ...] = (
    ("agent_key.agent", ""),
    ("artifact.agent", ""),
    ("version.created_by", ""),
    ("comment.author", " and author_kind = 'agent'"),
    ("comment.resolved_by", ""),
    ("decision.answered_by", ""),
    ("decision.recorded_by", ""),
    ("decision.acknowledged_by", ""),
    ("decision_outcome.agent", ""),
    ("receipt.agent", ""),
    ("event.agent", ""),
    ("event.actor", ""),
    ("connect_request.agent", ""),
)


class Refused(Exception):
    """A rename the service will not do, with a reason fit to print."""


def _resolve(conn, name: str) -> str:
    row = conn.execute("select new from agent_alias where old = %s", (name,)).fetchone()
    return row[0] if row else name


def resolve(db: Database, name: str) -> str:
    """The agent a TYPED name means now: its new name if it was renamed,
    otherwise the name itself. One hop is enough -- a rename re-points every
    alias that pointed at its old name."""
    with db.connect() as conn:
        return _resolve(conn, name)


def release_alias(db: Database, name: str, *, dry_run: bool) -> str:
    """Free `name`: delete the alias that makes it mean another agent, so it can be
    given to a new machine (connect approval and `--to` resolve a typed name through
    aliases). Returns the agent it used to point at. Raises `Refused` when `name` is
    not an alias. Exactly one row goes; anything else rolls back."""
    if not AGENT_NAME.fullmatch(name):
        raise Refused(f"invalid agent name {name!r}: must match {AGENT_NAME.pattern!r}")
    with db.connect() as conn, conn.transaction():
        conn.advisory_lock(_RENAME_LOCK_KEY)
        row = conn.execute(
            "select new from agent_alias where old = %s", (name,)
        ).fetchone()
        if row is None:
            raise Refused(
                f"{name} is not an alias (an old name of another agent); nothing changed"
            )
        gone = conn.execute("delete from agent_alias where old = %s", (name,)).rowcount
        if gone != 1:
            raise Refused(
                f"expected to release 1 alias row, got {gone}; nothing changed"
            )
        if dry_run:
            raise Rollback()
    return row[0]


def rename_agent(
    db: Database, old: str, new: str, *, dry_run: bool, release_name: bool = False
) -> dict[str, int]:
    """Rename agent `old` to `new`, merging into `new` if it already exists.

    With `release_name` no alias is left for `old` (and one `old` already held is
    dropped), so `old` is free for a new machine; aliases that pointed at `old` are
    still re-pointed at `new`.

    Returns the rows changed -- or, in a dry run, that would change -- per
    `table.column`, plus `agent_alias` for the alias rows written. Re-running a
    finished rename changes nothing and succeeds. Raises `Refused` for a bad
    name, `old == new`, a cycle, or an `old` that names nothing at all."""
    for name in (old, new):
        if not AGENT_NAME.fullmatch(name):
            raise Refused(
                f"invalid agent name {name!r}: must match {AGENT_NAME.pattern!r}"
            )
    if is_reserved_agent_name(new):
        raise Refused(f"invalid agent name {new!r}: {RESERVED_REASON}")
    if old == new:
        raise Refused(f"{old} is already called {new}")

    counts: dict[str, int] = {}
    with db.connect() as conn, conn.transaction():
        conn.advisory_lock(_RENAME_LOCK_KEY)
        # A person-and-app agent (MCP design 11.1) is named once, at its first consent,
        # and never changes; no key's agent may take or merge into one's name.
        for name, said in ((old, "renamed"), (new, "taken by a rename")):
            held = conn.execute(
                "select person from mcp_agent where agent = %s", (name,)
            ).fetchone()
            if held is not None:
                raise Refused(
                    f"{name} is a person-and-app agent (an MCP app's): its name cannot "
                    f"be {said}"
                )
        onward = conn.execute(
            "select new from agent_alias where old = %s", (new,)
        ).fetchone()
        if onward is not None:
            if onward[0] == old:
                raise Refused(
                    f"{new} was renamed to {old}; renaming {old} back to "
                    f"{new} would make a cycle"
                )
            raise Refused(
                f"{new} was itself renamed to {onward[0]}; rename {old} "
                f"to {onward[0]} instead"
            )

        for column, extra in _PLAIN:
            table, col = column.split(".")
            counts[column] = conn.execute(
                f"update {table} set {col} = %s where {col} = %s{extra}",  # noqa: S608 -- fixed identifiers from _PLAIN
                (new, old),
            ).rowcount

        counts["folder.owner"], counts["folder_alias.owner"] = _merge_folders(
            conn, old, new
        )
        counts["seat_grant.agent"] = _merge_grants(conn, old, new)
        counts["seat_read.agent"] = _merge_reads(conn, old, new)
        counts["idempotency.agent"] = _merge_idempotency(conn, old, new)
        counts["agent_pickup.agent"] = _merge_pickup(conn, old, new)

        moved = sum(counts.values())
        already = conn.execute(
            "select 1 from agent_alias where old = %s and new = %s",
            (old, new),
        ).fetchone()
        if moved == 0 and already is None:
            raise Refused(f"no agent named {old}; nothing changed")

        if release_name:
            aliased = conn.execute(
                "delete from agent_alias where old = %s", (old,)
            ).rowcount
        else:
            aliased = conn.execute(
                "insert into agent_alias (old, new) values (%s, %s)"
                " on conflict (old) do update"
                " set new = excluded.new, renamed_at = %s"
                " where agent_alias.new <> excluded.new",
                (old, new, datetime.now(UTC)),
            ).rowcount
        aliased += conn.execute(
            "update agent_alias set new = %s where new = %s", (new, old)
        ).rowcount
        counts["agent_alias"] = aliased

        if dry_run:
            raise Rollback()
    return counts


def _merge_grants(conn, old: str, new: str) -> int:
    """The union of both agents' scopes: a scope `new` already holds keeps its
    own row (and its grant time); the rest move."""
    dropped = conn.execute(
        "delete from seat_grant where agent = %s and exists (select 1"
        " from seat_grant n where n.agent = %s and n.scope = seat_grant.scope)",
        (old, new),
    ).rowcount
    moved = conn.execute(
        "update seat_grant set agent = %s where agent = %s", (new, old)
    ).rowcount
    return dropped + moved


def _merge_pickup(conn, old: str, new: str) -> int:
    """The newer of the two declared pickup lines survives; the old row goes."""
    conn.execute(
        "update agent_pickup set pickup = (select o.pickup from agent_pickup o"
        "   where o.agent = %s),"
        " updated_at = (select o.updated_at from agent_pickup o where o.agent = %s)"
        " where agent = %s and exists (select 1 from agent_pickup o"
        "   where o.agent = %s and o.updated_at > agent_pickup.updated_at)",
        (old, old, new, old),
    )
    merged = conn.execute(
        "delete from agent_pickup where agent = %s and exists (select 1"
        " from agent_pickup n where n.agent = %s)",
        (old, new),
    ).rowcount
    moved = conn.execute(
        "update agent_pickup set agent = %s where agent = %s", (new, old)
    ).rowcount
    return merged + moved


def _merge_reads(conn, old: str, new: str) -> int:
    """One row per (agent, session), the unlabelled '' session included: on a
    clash the newest read is kept, because the question this table answers is
    "is anyone still listening", and the later hearing is the truer answer."""
    conn.execute(
        "update seat_read set last_read_at = (select o.last_read_at"
        "   from seat_read o where o.agent = %s and o.session = seat_read.session)"
        " where agent = %s and exists (select 1 from seat_read o"
        "   where o.agent = %s and o.session = seat_read.session"
        "   and o.last_read_at > seat_read.last_read_at)",
        (old, new, old),
    )
    # The later hold wins too, so a rename never shortens a live wait.
    conn.execute(
        "update seat_read set held_until = (select o.held_until"
        "   from seat_read o where o.agent = %s and o.session = seat_read.session)"
        " where agent = %s and exists (select 1 from seat_read o"
        "   where o.agent = %s and o.session = seat_read.session"
        "   and o.held_until is not null"
        "   and (seat_read.held_until is null"
        "        or o.held_until > seat_read.held_until))",
        (old, new, old),
    )
    dropped = conn.execute(
        "delete from seat_read where agent = %s and exists (select 1"
        " from seat_read n where n.agent = %s and n.session = seat_read.session)",
        (old, new),
    ).rowcount
    moved = conn.execute(
        "update seat_read set agent = %s where agent = %s", (new, old)
    ).rowcount
    return dropped + moved


def _merge_idempotency(conn, old: str, new: str) -> int:
    """On a (route, key) clash the newer stored response wins -- the one that
    expires later, since every row lives the same length of time."""
    conn.execute(
        "delete from idempotency where agent = %s and exists (select 1"
        " from idempotency o where o.agent = %s and o.route = idempotency.route"
        " and o.key = idempotency.key and o.expires_at > idempotency.expires_at)",
        (new, old),
    )
    dropped = conn.execute(
        "delete from idempotency where agent = %s and exists (select 1"
        " from idempotency n where n.agent = %s and n.route = idempotency.route"
        " and n.key = idempotency.key)",
        (old, new),
    ).rowcount
    moved = conn.execute(
        "update idempotency set agent = %s where agent = %s", (new, old)
    ).rowcount
    return dropped + moved


def _merge_folders(conn, old: str, new: str) -> tuple[int, int]:
    """`old`'s folders become `new`'s. A slug `new` already has is one folder: `old`'s pages
    are filed in it, its aliases move over (an alias `new` already holds keeps its own row), and
    `old`'s emptied folder row goes. The reserved `jeremy` owner is never touched: it is not an
    agent name, and a rename of it is refused upstream. Returns (folder rows, alias rows)."""
    if is_reserved_agent_name(old):
        return 0, 0
    folders = 0
    clashes = conn.execute(
        "select o.id, n.id from folder o join folder n on n.slug = o.slug"
        " where o.owner = %s and n.owner = %s",
        (old, new),
    ).fetchall()
    for old_id, new_id in clashes:
        conn.execute(
            "update artifact set folder_id = %s where folder_id = %s", (new_id, old_id)
        )
        # The archive marker follows its pages to the surviving folder only when that folder
        # is archived too, so unarchiving it restores them. A surviving LIVE folder takes
        # `old`'s archived pages as they are -- they stay archived, on their own: the marker
        # goes, so a later archive/unarchive of the merged folder never revives them.
        survivor_archived = conn.execute(
            "select archived_at from folder where id = %s", (new_id,)
        ).fetchone()[0]
        if survivor_archived is not None:
            conn.execute(
                "update artifact set archived_via_folder = %s"
                " where archived_via_folder = %s",
                (new_id, old_id),
            )
        else:
            conn.execute(
                "update artifact set archived_via_folder = null"
                " where archived_via_folder = %s",
                (old_id,),
            )
        conn.execute(
            "update folder_alias set folder_id = %s where folder_id = %s",
            (new_id, old_id),
        )
        folders += conn.execute("delete from folder where id = %s", (old_id,)).rowcount
    folders += conn.execute(
        "update folder set owner = %s where owner = %s", (new, old)
    ).rowcount
    # An alias that would collide with one `new` already holds is dropped, not moved.
    aliases = conn.execute(
        "delete from folder_alias where owner = %s and exists (select 1"
        " from folder_alias n where n.owner = %s and n.old_slug = folder_alias.old_slug)",
        (old, new),
    ).rowcount
    aliases += conn.execute(
        "update folder_alias set owner = %s where owner = %s", (new, old)
    ).rowcount
    # An alias that now names a folder's real slug is dead weight (a real slug wins) and would
    # make a later rename to it look like a clash with another folder.
    aliases += conn.execute(
        "delete from folder_alias where owner = %s and exists (select 1 from folder f"
        " where f.owner = %s and f.slug = folder_alias.old_slug)",
        (new, new),
    ).rowcount
    return folders, aliases
