import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime

from artifacts.db import Database


@dataclass(frozen=True)
class AgentKey:
    id: str
    agent: str


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
            "select id, agent from agent_key where token_sha256 = %s and revoked_at is null",
            (_hash(token),),
        ).fetchone()
    return AgentKey(id=row[0], agent=row[1]) if row else None


def revoke(db: Database, key_id: str) -> int:
    """Returns the number of rows changed: 0 for an unknown id, or for one
    already revoked -- `revoked_at is null` in the WHERE clause makes both
    cases a no-op instead of overwriting an earlier revocation's timestamp,
    and callers use the row count to tell "revoked" from "nothing to do"."""
    with db.connect() as conn:
        cursor = conn.execute(
            "update agent_key set revoked_at = now() where id = %s and revoked_at is null",
            (key_id,),
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
    with db.connect() as conn:
        conn.execute(
            "update agent_key set last_used_at = now() where id = %s"
            " and (last_used_at is null or last_used_at < now() - interval '5 minutes')",
            (key_id,),
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
            " array(select s from (select v.session as s, max(v.created_at) as at"
            "   from version v join artifact a on a.id = v.artifact_id"
            "   where a.agent = k.agent and v.session is not null"
            "   group by v.session order by at desc limit 5) recent)"
            " from agent_key k"
            " order by k.revoked_at is not null, k.created_at desc"
        ).fetchall()
    return [Machine(*row[:7], sessions=list(row[7] or [])) for row in rows]


def set_label(db: Database, key_id: str, label: str) -> bool:
    with db.connect() as conn:
        done = conn.execute(
            "update agent_key set label = %s where id = %s", (label or None, key_id)
        )
        return done.rowcount > 0
