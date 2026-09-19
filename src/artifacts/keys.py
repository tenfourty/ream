import hashlib
import secrets
from dataclasses import dataclass

from artifacts.db import Database


@dataclass(frozen=True)
class AgentKey:
    id: str
    agent: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint(db: Database, agent: str) -> tuple[str, str]:
    """Mint a new bearer token for `agent`. Returns (token, key_id); only the
    token's hash is ever persisted — the plaintext is returned once and not kept."""
    token = "art_" + secrets.token_urlsafe(32)
    key_id = secrets.token_hex(8)
    with db.connect() as conn:
        conn.execute(
            "insert into agent_key (id, agent, token_sha256) values (%s, %s, %s)",
            (key_id, agent, _hash(token)),
        )
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


def revoke(db: Database, key_id: str) -> None:
    with db.connect() as conn:
        conn.execute("update agent_key set revoked_at = now() where id = %s", (key_id,))
