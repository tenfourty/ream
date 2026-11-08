"""`Idempotency-Key`: a retried write returns the first response, once.

The first request for an (agent, route, key) runs, and its status and JSON body
are kept for 24 hours. A repeat gets exactly that back and runs nothing -- no
second comment, event or scan. A reused key with a different body is refused.

Two identical requests at once are serialised by a transaction-scoped advisory
lock on a hash of (agent, route, key): the second waits, then finds the first's
row. Only a successful response is stored, so a refusal can be retried.

The action is handed the lock's connection and writes inside the same
transaction as the stored response, so the two commit together or not at all:
a crash between them can never leave a written comment with no stored response
for the retry to find. Lock order: the advisory lock first, then whatever rows
the action locks -- nothing takes this advisory lock after a row lock.
"""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ream.db import Connection, Database, jsonb

MAX_KEY = 128

# Expired rows removed per keyed request: bounded, so the held transaction
# never waits on a scan of a backlog, and still far faster than rows expire.
PRUNE_BATCH = 100


def valid_key(key: str) -> bool:
    return 1 <= len(key) <= MAX_KEY and all(0x20 <= ord(c) <= 0x7E for c in key)


def _lock_id(agent: str, route: str, key: str) -> int:
    digest = hashlib.sha256(f"{agent}\0{route}\0{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def run(
    db: Database,
    agent: str,
    route: str,
    key: str | None,
    body: object,
    action: Callable[[Connection | None], tuple[int, object]],
) -> JSONResponse:
    """Run `action(conn)` -> (status, json body) at most once per key. `conn`
    is the transaction the response is stored in, or None with no key (the
    action then opens its own connection, exactly as before)."""
    if key is None:
        status, content = action(None)
        return JSONResponse(jsonable_encoder(content), status_code=status)
    if not valid_key(key):
        raise HTTPException(
            422, "Idempotency-Key must be 1 to 128 printable ASCII characters"
        )
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    now = datetime.now(UTC)
    with db.connect() as conn, conn.transaction():
        conn.advisory_lock(_lock_id(agent, route, key))
        # By primary key, not ctid (SQLite has none). Without `skip locked`:
        # a row another request is pruning blocks this one for a moment, which
        # a batch of PRUNE_BATCH bounds; SQLite serialises writers anyway.
        conn.execute(
            "delete from idempotency where (agent, route, key) in ("
            "select agent, route, key from idempotency"
            " where expires_at < %s limit %s)",
            (now, PRUNE_BATCH),
        )
        seen = conn.execute(
            "select body_sha256, status, response from idempotency"
            " where agent = %s and route = %s and key = %s",
            (agent, route, key),
        ).fetchone()
        if seen is not None:
            if seen[0] != digest:
                raise HTTPException(
                    422, "idempotency key reused with a different request"
                )
            return JSONResponse(seen[2], status_code=seen[1])
        status, content = action(conn)
        encoded = jsonable_encoder(content)
        conn.execute(
            "insert into idempotency"
            " (agent, route, key, body_sha256, status, response, expires_at)"
            " values (%s, %s, %s, %s, %s, %s, %s)",
            (
                agent,
                route,
                key,
                digest,
                status,
                jsonb(encoded),
                now + timedelta(hours=24),
            ),
        )
        return JSONResponse(encoded, status_code=status)
