"""Connecting a machine: it asks, Jeremy approves its code, it collects a key.

homelab-ij8.102. His ruling, confirmed 23 Sep 2026: an arriving agent shows him
a short code; he types that code on the gallery's connect page, names the
machine and approves; the key goes straight to the machine. He never sees it,
no prompt carries it, no chat transcript ever holds it.

THE THREE STEPS AND WHO MAY TAKE THEM

  request  -- anyone, no key (POST /api/v1/connect). It is how a stranger
              starts, so it is open, and so it is limited: a limited number
              of pending requests per address (every waiting request counts; a
              repeat never touches an earlier one), a ceiling across all of
              them, and every request dies unapproved after `LIFETIME_SECONDS`.
  approve  -- Jeremy only, in the gallery behind his login, by typing the code.
              Deliberately no agent equivalent: this is the gate itself, the
              same asymmetry as answering a question as him.
  collect  -- the machine that asked, by its poll token (POST
              /api/v1/connect/poll). The key is minted HERE, returned once,
              and never again: a second collect says "delivered", not the key.

MOVING AN AGENT (9 Nov 2026). When the requested name already belongs to an agent, the
approve page offers "same agent, new machine": the new key joins that agent, and its
earlier keys retire at the new key's first use (`keys.first_use`). A machine can never
choose this for itself; only the owner's approval can, or the agent's own key via
`self_move` (POST /agents/me/move), which is scoped to the calling key's agent.

WHY HE TYPES THE CODE rather than picking a request from a list: the request
endpoint is open, so anyone can put a request in front of him. Typing the code
his own agent is showing him approves exactly that one and no other.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ream import keys, rename
from ream.db import Database

LIFETIME_SECONDS = 15 * 60
# Several machines can share one address (five office VMs behind one NAT), so the
# per-address cap is generous; the global ceiling is what bounds a flood.
MAX_PENDING_PER_ADDRESS = 10
MAX_PENDING_TOTAL = 50

# No 0/O, 1/I/L, 2/Z, 5/S, 8/B: a human reads this off one screen and types it
# on another, often a phone.
_ALPHABET = "34679ACDEFGHJKMNPQRTUVWXY"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")


class Refused(Exception):
    """A request the service will not take, with a reason fit to show."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalise_code(typed: str) -> str:
    """What he typed, forgiving case, spaces and a missing hyphen."""
    bare = re.sub(r"[^0-9A-Za-z]", "", typed or "").upper()
    return f"{bare[:4]}-{bare[4:]}" if len(bare) == 8 else bare


def clean_name(name: str | None) -> str | None:
    """A machine name he can read on the keys page: lower-case letters, digits
    and hyphens. None when nothing usable was offered."""
    if not name:
        return None
    slug = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:40].strip("-")
    return slug if _NAME.match(slug) else None


@dataclass(frozen=True)
class Started:
    code: str
    poll_token: str
    expires_in: int


def request(db: Database, suggested_name: str | None, from_address: str) -> Started:
    now = datetime.now(UTC)
    name = clean_name(suggested_name)
    with db.connect() as conn:
        mine, total = conn.execute(
            "select count(*) filter (where from_address = %s), count(*)"
            " from connect_request"
            " where expires_at > %s and approved_at is null and denied_at is null",
            (from_address, now),
        ).fetchone()
        if mine >= MAX_PENDING_PER_ADDRESS:
            raise Refused(
                "this address already has requests waiting for approval; "
                "use one of those, or wait for them to expire"
            )
        if total >= MAX_PENDING_TOTAL:
            raise Refused("too many requests are waiting for approval; try later")
        poll = secrets.token_urlsafe(32)
        for _attempt in range(5):
            code = _code()
            inserted = conn.execute(
                "insert into connect_request"
                " (id, code, poll_sha256, suggested_name, from_address, expires_at)"
                " values (%s, %s, %s, %s, %s, %s)"
                " on conflict (code) do nothing returning id",
                (
                    secrets.token_hex(12),
                    code,
                    _hash(poll),
                    name,
                    from_address,
                    now + timedelta(seconds=LIFETIME_SECONDS),
                ),
            ).fetchone()
            if inserted:
                return Started(code=code, poll_token=poll, expires_in=LIFETIME_SECONDS)
    raise Refused("could not issue a code; try again")  # pragma: no cover


@dataclass(frozen=True)
class Pending:
    id: str
    code: str
    suggested_name: str | None
    from_address: str
    created_at: datetime


def find(db: Database, typed_code: str) -> Pending | None:
    """The live, undecided request with this code, or None."""
    with db.connect() as conn:
        row = conn.execute(
            "select id, code, suggested_name, from_address, created_at"
            " from connect_request where code = %s and expires_at > %s"
            " and approved_at is null and denied_at is null",
            (normalise_code(typed_code), datetime.now(UTC)),
        ).fetchone()
    return Pending(*row) if row else None


def name_in_use(db: Database, agent: str) -> bool:
    """A name already carried by a working key belongs to another machine. A
    name whose keys are all revoked is free again, so a machine can be
    reconnected under its old name and keep its pages."""
    with db.connect() as conn:
        row = conn.execute(
            "select 1 from agent_key where agent = %s and revoked_at is null", (agent,)
        ).fetchone()
    return row is not None


def app_agent_name(db: Database, agent: str) -> bool:
    """Whether a person-and-app agent (an MCP app's) holds `agent`: no machine may
    take it (`keys.refuse_app_agent_name` refuses its key too)."""
    with db.connect() as conn:
        row = conn.execute(
            "select 1 from mcp_agent where agent = %s", (agent,)
        ).fetchone()
    return row is not None


def existing_agent(db: Database, suggested: str | None) -> str | None:
    """The existing agent a requested name belongs to, or None: the name (after an
    alias is followed, as the approve page does) is carried by a working key, is not
    reserved and is not an app's. Those names are refused outright, so only a real
    machine agent is ever offered "same agent, new machine"."""
    name = clean_name(suggested)
    if name is None:
        return None
    name = rename.resolve(db, name)
    if keys.is_reserved_agent_name(name) or app_agent_name(db, name):
        return None
    return name if name_in_use(db, name) else None


def approve(db: Database, request_id: str, agent: str, move: bool = False) -> bool:
    """Record the owner's approval. `move`: the new key joins `agent`, which already
    exists, and its earlier keys retire at the new key's first use (`collect`)."""
    now = datetime.now(UTC)
    with db.connect() as conn:
        row = conn.execute(
            "update connect_request set approved_at = %s, agent = %s, move = %s"
            " where id = %s and expires_at > %s"
            " and approved_at is null and denied_at is null returning id",
            (now, agent, "owner" if move else None, request_id, now),
        ).fetchone()
    return row is not None


def self_move(db: Database, agent: str, from_address: str, origin_key: str) -> Started:
    """An agent starts its own move (POST /agents/me/move): a request born approved for
    `agent` itself, so the machine that holds the returned poll token collects a key for
    that agent and nobody else. The `code` is never shown; the poll token is the
    credential, valid once and for `LIFETIME_SECONDS`. An earlier unclaimed move of this
    agent is cancelled, so at most one is ever open. `agent` is always the CALLING key's
    own agent, never a name the caller typed. `origin_key` is the calling key: it must stay
    live until the move is collected (revoking it cancels the move), and it alone retires."""
    now = datetime.now(UTC)
    poll = secrets.token_urlsafe(32)
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "update connect_request set denied_at = %s where move = 'self' and agent = %s"
            " and delivered_at is null and denied_at is null",
            (now, agent),
        )
        for _attempt in range(5):
            code = _code()
            inserted = conn.execute(
                "insert into connect_request"
                " (id, code, poll_sha256, suggested_name, from_address, expires_at,"
                " approved_at, agent, move, origin_key)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, 'self', %s)"
                " on conflict (code) do nothing returning id",
                (
                    secrets.token_hex(12),
                    code,
                    _hash(poll),
                    agent,
                    from_address,
                    now + timedelta(seconds=LIFETIME_SECONDS),
                    now,
                    agent,
                    origin_key,
                ),
            ).fetchone()
            if inserted:
                return Started(code=code, poll_token=poll, expires_in=LIFETIME_SECONDS)
    raise Refused("could not issue a move; try again")  # pragma: no cover


def deny(db: Database, request_id: str) -> bool:
    with db.connect() as conn:
        row = conn.execute(
            "update connect_request set denied_at = %s"
            " where id = %s and approved_at is null and denied_at is null returning id",
            (datetime.now(UTC), request_id),
        ).fetchone()
    return row is not None


def collect(db: Database, poll_token: str) -> dict:
    """What the asking machine learns on a poll. The key appears in exactly one
    response, minted inside the same transaction that marks it delivered, so
    two racing polls cannot both receive one."""
    if not poll_token or len(poll_token) > 128:
        return {"status": "expired"}
    now = datetime.now(UTC)
    with db.connect() as conn, conn.transaction():
        # The name lock comes before the request's row lock (keys.NAME_LOCK_KEY).
        conn.advisory_lock(keys.NAME_LOCK_KEY)
        row = conn.execute(
            "select id, approved_at, agent, denied_at, delivered_at, move, origin_key,"  # noqa: S608 -- clause from the db layer, no caller input
            " expires_at > %s from connect_request where poll_sha256 = %s"
            + conn.for_update(),
            (now, _hash(poll_token)),
        ).fetchone()
        if row is None:
            return {"status": "expired"}
        (
            request_id,
            approved_at,
            agent,
            denied_at,
            delivered_at,
            move,
            origin_key,
            alive,
        ) = row
        if delivered_at is not None:
            return {"status": "delivered"}
        if denied_at is not None:
            return {"status": "denied"}
        if not alive:  # never a key for a request past its life, approved or not
            return {"status": "expired"}
        if approved_at is None:
            return {"status": "pending"}
        if move == "self":
            origin = conn.execute(
                "select 1 from agent_key where id = %s and revoked_at is null",
                (origin_key,),
            ).fetchone()
            if origin is None:  # the key that started this move is gone
                conn.execute(
                    "update connect_request set denied_at = %s where id = %s",
                    (now, request_id),
                )
                return {"status": "denied"}
        try:
            keys.refuse_app_agent_name(conn, agent)
        except keys.NameHeld:
            # A person-and-app agent took the name after it was approved: no key
            # for it, ever; the machine is told no and can ask again.
            conn.execute(
                "update connect_request set denied_at = %s where id = %s",
                (now, request_id),
            )
            return {"status": "denied"}
        token = "art_" + secrets.token_urlsafe(32)
        key_id = secrets.token_hex(8)
        conn.execute(
            # rotating: a connected machine's key replaces itself weekly
            # (homelab-ij8.34, his (3a)).
            "insert into agent_key (id, agent, token_sha256, rotating) values (%s, %s, %s, true)",
            (key_id, agent, keys._hash(token)),
        )
        conn.execute(
            "update connect_request set delivered_at = %s, key_id = %s where id = %s",
            (now, key_id, request_id),
        )
        if move:
            # The keys that retire when this one is first used (`keys.first_use`); until
            # then both work. The owner's approval retires every other live key of the
            # agent; an agent's own move retires only the key that started it.
            if move == "self":
                marked = conn.execute(
                    "update agent_key set retire_after = %s"
                    " where id = %s and revoked_at is null returning id",
                    (key_id, origin_key),
                ).fetchall()
            else:
                marked = conn.execute(
                    "update agent_key set retire_after = %s"
                    " where agent = %s and id <> %s and revoked_at is null returning id",
                    (key_id, agent, key_id),
                ).fetchall()
            keys.note_on_latest_page(
                conn,
                agent,
                kind="agent_moved",
                summary=(
                    f"{agent} moved to a new machine; {len(marked)} earlier key(s) "
                    "retire when the new key is first used"
                ),
                ref_id=key_id,
            )
    return {"status": "approved", "agent": agent, "key": token}
