"""`Idempotency-Key`: a retried write returns the first response, once.

The first request for an (agent, route, key) runs, and its status and JSON body
are kept for 24 hours. A repeat gets exactly that back and runs nothing -- no
second comment, event or scan. A reused key with a different body is refused.

Two identical requests at once are serialised by a transaction-scoped advisory
lock on a hash of (agent, route, key): the second waits, then finds the first's
row. Only a successful response is stored, so a refusal can be retried.
"""

import hashlib
import json
from collections.abc import Callable

import psycopg
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from artifacts.db import Database

MAX_KEY = 128


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
    action: Callable[[], tuple[int, object]],
) -> JSONResponse:
    """Run `action` -> (status, json body) at most once per key."""
    if key is None:
        status, content = action()
        return JSONResponse(jsonable_encoder(content), status_code=status)
    if not valid_key(key):
        raise HTTPException(
            422, "Idempotency-Key must be 1 to 128 printable ASCII characters"
        )
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    with db.connect() as conn, conn.transaction():
        conn.execute("select pg_advisory_xact_lock(%s)", (_lock_id(agent, route, key),))
        conn.execute("delete from idempotency where expires_at < now()")
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
        status, content = action()
        encoded = jsonable_encoder(content)
        conn.execute(
            "insert into idempotency"
            " (agent, route, key, body_sha256, status, response, expires_at)"
            " values (%s, %s, %s, %s, %s, %s, now() + interval '24 hours')",
            (agent, route, key, digest, status, psycopg.types.json.Jsonb(encoded)),
        )
        return JSONResponse(encoded, status_code=status)
