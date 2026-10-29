from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotations only: `artifacts-admin new-key` runs without psycopg
    from artifacts.db import Database

# A rotating key is swapped once it is this old (by the tools), and refused
# once it is this much older without having been (homelab-ij8.34).
ROTATE_AFTER = timedelta(days=7)
ROTATING_MAX_AGE = timedelta(days=14)

# What an agent may be called: `artifacts-admin` checks every name it is typed
# against this, and so does a rename.
AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# Names no agent may take: the gallery stores the reader's own comments, pictures
# and answers under "jeremy", so an agent called that would read as his work.
# The pattern forces lower case, so no case variants arise.
RESERVED_AGENT_NAMES = frozenset({"jeremy"})
RESERVED_REASON = "reserved: it is the reader's own name"


def is_reserved_agent_name(name: str) -> bool:
    """Whether `name` is one no agent may be minted, connected or renamed to."""
    return name in RESERVED_AGENT_NAMES


@dataclass(frozen=True)
class AgentKey:
    id: str
    agent: str
    created_at: datetime | None = None
    rotating: bool = False


@dataclass(frozen=True)
class KeyRow:
    id: str
    agent: str
    created_at: datetime
    revoked_at: datetime | None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def register(db: Database, agent: str, token_sha256: str) -> str:
    """Insert a key from an already-hashed token. Returns the new key_id.
    Never sees, and never could see, the plaintext token: `artifacts-admin
    new-key` generates it on the machine that will use it and hashes it
    there, so only `token_sha256` ever reaches this function or the
    database."""
    key_id = secrets.token_hex(8)
    with db.connect() as conn:
        conn.execute(
            "insert into agent_key (id, agent, token_sha256) values (%s, %s, %s)",
            (key_id, agent, token_sha256),
        )
    return key_id


def mint(db: Database, agent: str) -> tuple[str, str]:
    """Mint a new bearer token for `agent`. Returns (token, key_id); only the
    token's hash is ever persisted — the plaintext is returned once and not kept."""
    token = "art_" + secrets.token_urlsafe(32)
    key_id = register(db, agent, _hash(token))
    return token, key_id


def verify(db: Database, token: str) -> AgentKey | None:
    """Look up the agent owning `token`, or None if it's malformed, unknown or revoked."""
    if not token.startswith("art_"):
        return None
    with db.connect() as conn:
        row = conn.execute(
            "select id, agent, created_at, rotating from agent_key"
            " where token_sha256 = %s and revoked_at is null",
            (_hash(token),),
        ).fetchone()
    if row is None:
        return None
    key = AgentKey(id=row[0], agent=row[1], created_at=row[2], rotating=row[3])
    if key.rotating and datetime.now(UTC) - key.created_at > ROTATING_MAX_AGE:
        return None
    return key


def refusal_reason(db: Database, token: str) -> str:
    """Why a key that did NOT verify was refused, in words for its holder. Only
    ever shown to whoever presented that exact key, so saying it existed tells
    them nothing they did not already hold (homelab-ij8.34)."""
    if not token.startswith("art_"):
        return "unknown or revoked key"
    with db.connect() as conn:
        row = conn.execute(
            "select revoked_at, revoked_reason, rotating, created_at from agent_key"
            " where token_sha256 = %s",
            (_hash(token),),
        ).fetchone()
    if row is None:
        return "unknown or revoked key"
    revoked_at, reason, rotating, created_at = row
    if revoked_at is not None and reason == "replaced":
        return (
            "this key was replaced by a newer one. If this machine did not replace "
            "it, someone else has held its key: tell Jeremy, and connect again with "
            "artifacts-connect"
        )
    if (
        revoked_at is None
        and rotating
        and datetime.now(UTC) - created_at > ROTATING_MAX_AGE
    ):
        return (
            "this key expired: machine keys replace themselves weekly and this one "
            "was not replaced in two weeks. Connect again with artifacts-connect"
        )
    return "unknown or revoked key"


def first_use(db: Database, key: AgentKey) -> None:
    """The first time a rotated key is used, the key it replaced dies. Until
    then both work, so a rotate whose response was lost cannot lock a machine
    out; after it, a copy of the old key is worthless."""
    if not key.rotating:
        return
    now = datetime.now(UTC)
    with db.connect() as conn, conn.transaction():
        row = conn.execute(
            "update agent_key set first_used_at = %s"
            " where id = %s and first_used_at is null and replaces is not null"
            " returning replaces",
            (now, key.id),
        ).fetchone()
        if row:
            conn.execute(
                "update agent_key set revoked_at = %s, revoked_reason = 'replaced'"
                " where id = %s and revoked_at is null",
                (now, row[0]),
            )


class NotRotating(Exception):
    """A house seat's key: vaulted and encrypted at rest, not swapped by tools."""


def rotate(db: Database, key: AgentKey) -> tuple[str, str]:
    """Mint this key's successor: same machine, same name. Returns (token,
    key_id); the plaintext is returned once. A pending successor from an earlier
    rotate that was never used is revoked, so there is only ever one."""
    if not key.rotating:
        raise NotRotating()
    token = "art_" + secrets.token_urlsafe(32)
    key_id = secrets.token_hex(8)
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "update agent_key set revoked_at = %s, revoked_reason = 'superseded'"
            " where replaces = %s and first_used_at is null and revoked_at is null",
            (datetime.now(UTC), key.id),
        )
        conn.execute(
            "insert into agent_key (id, agent, token_sha256, rotating, replaces, label)"
            " select %s, agent, %s, true, id, label from agent_key where id = %s",
            (key_id, _hash(token), key.id),
        )
    return token, key_id


def revoke(db: Database, key_id: str) -> int:
    """Returns the number of rows changed: 0 for an unknown id, or for one
    already revoked -- `revoked_at is null` in the WHERE clause makes both
    cases a no-op instead of overwriting an earlier revocation's timestamp,
    and callers use the row count to tell "revoked" from "nothing to do"."""
    with db.connect() as conn:
        cursor = conn.execute(
            "update agent_key set revoked_at = %s where id = %s and revoked_at is null",
            (datetime.now(UTC), key_id),
        )
        return cursor.rowcount


def agent_of(db: Database, key_id: str) -> str | None:
    """The agent name owning `key_id`, whether or not it's revoked, or None
    if no such key exists -- so `revoke-key` can report which agent it just
    revoked without `revoke` itself returning anything but a row count."""
    with db.connect() as conn:
        row = conn.execute(
            "select agent from agent_key where id = %s", (key_id,)
        ).fetchone()
    return row[0] if row else None


def list_keys(db: Database) -> list[KeyRow]:
    """Every key's id, agent, and timestamps — never `token_sha256`."""
    with db.connect() as conn:
        rows = conn.execute(
            "select id, agent, created_at, revoked_at from agent_key order by created_at"
        ).fetchall()
    return [KeyRow(*row) for row in rows]


def touch(db: Database, key_id: str) -> None:
    """Record that a key was just used -- at most once every five minutes, so a
    busy agent costs an update that matches nothing, not a write per request
    (homelab-ij8.103)."""
    now = datetime.now(UTC)
    with db.connect() as conn:
        conn.execute(
            "update agent_key set last_used_at = %s where id = %s"
            " and (last_used_at is null or last_used_at < %s)",
            (now, key_id, now - timedelta(minutes=5)),
        )


@dataclass(frozen=True)
class Machine:
    id: str
    agent: str
    label: str | None
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    pages: int
    sessions: list[str]
    rotating: bool = False

    @property
    def name(self) -> str:
        return self.label or self.agent


def machines(db: Database) -> list[Machine]:
    """Every key, as the machines page shows it: working ones first, newest
    first. Never the token or its hash."""
    with db.connect() as conn:
        rows = conn.execute(
            "select k.id, k.agent, k.label, k.created_at, k.revoked_at, k.last_used_at,"
            " (select count(*) from artifact a where a.agent = k.agent"
            "   and a.deleted_at is null),"
            " k.rotating"
            " from agent_key k"
            # A key retired by its own weekly swap is not a revocation, and
            # listing one per week per machine would bury the real ones.
            " where coalesce(k.revoked_reason, '') not in ('replaced', 'superseded')"
            " order by k.revoked_at is not null, k.created_at desc"
        ).fetchall()
        # Each agent's five most recent sessions, one query per agent: SQLite
        # has no array(select ...), and an agent has only a few keys.
        sessions = {
            agent: [
                r[0]
                for r in conn.execute(
                    "select v.session, max(v.created_at) as at"
                    " from version v join artifact a on a.id = v.artifact_id"
                    " where a.agent = %s and v.session is not null"
                    " group by v.session order by at desc limit 5",
                    (agent,),
                ).fetchall()
            ]
            for agent in {row[1] for row in rows}
        }
    return [
        Machine(*row[:7], sessions=sessions[row[1]], rotating=bool(row[7]))
        for row in rows
    ]


def set_label(db: Database, key_id: str, label: str) -> bool:
    with db.connect() as conn:
        done = conn.execute(
            "update agent_key set label = %s where id = %s", (label or None, key_id)
        )
        return done.rowcount > 0
