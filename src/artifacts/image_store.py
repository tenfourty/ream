"""Pictures on a comment, in the database and on the wire: staging, attaching, the sweep,
listing, serving, and the token a reader's browser carries.

What is ACCEPTED and what is KEPT is `images` (pure bytes, no service imports); this module
only keeps what that one returned. Spec: docs/agent/specs/2026-10-29-pinned-comments.md section 6.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from artifacts import ids, store
from artifacts.db import Connection, Database, in_list
from artifacts.images import (
    _ID,
    _NOT_ATTACHABLE,
    PER_PAGE,
    STAGED_PER_AUTHOR,
    STAGED_TTL,
    ImageRefused,
    Inspected,
    _refuse,
)

TOKEN_LIFETIME = timedelta(hours=1)


def _meta(row) -> dict:
    return {
        "image_id": row[0],
        "content_type": row[1],
        "width": row[2],
        "height": row[3],
        "byte_size": row[4],
    }


def stage(db: Database, artifact_id: str, author: str, found: Inspected) -> dict:
    """Keep a picture, unattached, for `author` on this page. The same bytes from the same
    author while still staged give the same id. Quotas: 12 staged and unattached per author
    (swept after an hour), 40 pictures per page."""
    cutoff = datetime.now(UTC) - STAGED_TTL
    with db.connect() as conn, conn.transaction():
        conn.advisory_lock(int.from_bytes(hashlib.sha256(author.encode()).digest()[:7]))
        again = conn.execute(
            "select id, content_type, width, height, byte_size from comment_image"
            " where artifact_id = %s and staged_by = %s and sha256 = %s"
            " and comment_id is null and created_at >= %s",
            (artifact_id, author, found.sha256, cutoff),
        ).fetchone()
        if again is not None:
            return _meta(again)
        (waiting,) = conn.execute(
            "select count(*) from comment_image"
            " where staged_by = %s and comment_id is null and created_at >= %s",
            (author, cutoff),
        ).fetchone()
        if waiting >= STAGED_PER_AUTHOR:
            raise _refuse(
                f"{STAGED_PER_AUTHOR} pictures are waiting to be attached: attach them"
                " to a comment, or wait an hour for them to expire"
            )
        (onpage,) = conn.execute(
            "select count(*) from comment_image where artifact_id = %s"
            " and comment_id is not null",
            (artifact_id,),
        ).fetchone()
        if onpage >= PER_PAGE:
            raise _refuse(
                f"this page already has {PER_PAGE} pictures: the most it takes"
            )
        image_id = ids.new_id()
        conn.execute(
            "insert into comment_image (id, artifact_id, staged_by, content_type, width,"
            " height, byte_size, sha256, bytes) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                image_id,
                artifact_id,
                author,
                found.content_type,
                found.width,
                found.height,
                len(found.data),
                found.sha256,
                found.data,
            ),
        )
    return {
        "image_id": image_id,
        "content_type": found.content_type,
        "width": found.width,
        "height": found.height,
        "byte_size": len(found.data),
    }


def attach(
    conn: Connection, artifact_id: str, comment_id: str, author: str, wanted: list[str]
) -> None:
    """Attach staged pictures to a comment in the caller's transaction, in the order named.
    ALL OR NOTHING: one id that is unknown, someone else's, already attached, staged on another
    page or expired refuses the whole comment, and the answer never says which of those it was."""
    cutoff = datetime.now(UTC) - STAGED_TTL
    for position, image_id in enumerate(wanted, start=1):
        done = conn.execute(
            "update comment_image set comment_id = %s, position = %s, staged_by = null"
            " where id = %s and artifact_id = %s and staged_by = %s"
            " and comment_id is null and created_at >= %s",
            (comment_id, position, image_id, artifact_id, author, cutoff),
        )
        if done.rowcount != 1:
            raise ImageRefused(_NOT_ATTACHABLE, field="images")


def sweep(db: Database) -> int:
    """Drop staged pictures nobody attached within the hour."""
    cutoff = datetime.now(UTC) - STAGED_TTL
    with db.connect() as conn:
        return conn.execute(
            "delete from comment_image where comment_id is null and created_at < %s",
            (cutoff,),
        ).rowcount


def with_images(conn: Connection, rows: list) -> list:
    """Each comment row told its pictures (`images`), in one query for the whole listing.
    The row carries the API path to fetch each by, never a token."""
    wanted = sorted({row.id for row in rows})
    if not wanted:
        return rows
    found: dict[str, list[dict]] = {}
    for cid, iid, ctype, width, height, size, digest in conn.execute(
        "select comment_id, id, content_type, width, height, byte_size, sha256"  # noqa: S608 -- placeholders only
        f" from comment_image where comment_id in {in_list(wanted)}"
        " order by comment_id, position",
        tuple(wanted),
    ).fetchall():
        found.setdefault(cid, []).append(
            {
                "id": iid,
                "content_type": ctype,
                "width": width,
                "height": height,
                "byte_size": size,
                "sha256": digest,
                "url": f"/api/v1/comments/{cid}/images/{iid}",
            }
        )
    if not found:
        return rows
    return [
        replace(row, images=found[row.id]) if row.id in found else row for row in rows
    ]


# --- serving -------------------------------------------------------------------------------


def _visible() -> str:
    """The clause that says a comment is shown at all: not withdrawn, nor under a
    withdrawn root. The one definition `comments` lists by."""
    from artifacts import comments

    return comments._VISIBLE


def for_reader(
    db: Database, agent: str, comment_id: str, image_id: str
) -> tuple[str, bytes] | None:
    """(content type, bytes) of an attached picture, if `agent` may read its page and its
    comment is shown to agents (not held, not withdrawn); else None. Every refusal is the same
    None, so a caller cannot tell an unknown picture from one it may not see."""
    if not (_ID.fullmatch(comment_id) and _ID.fullmatch(image_id)):
        return None
    with db.connect() as conn:
        row = conn.execute(
            "select comment_image.artifact_id from comment_image"  # noqa: S608 -- clause from the module, no caller input
            " join comment on comment.id = comment_image.comment_id"
            " where comment_image.id = %s and comment_image.comment_id = %s"
            f" and not comment.held and {_visible()}",
            (image_id, comment_id),
        ).fetchone()
        if row is None or store._owned(conn, row[0], agent, True) is None:
            return None
        return _bytes(conn, image_id)


def for_origin(
    db: Database, artifact_id: str, image_id: str
) -> tuple[str, bytes] | None:
    """(content type, bytes) for the content origin of `artifact_id`: the picture must be
    attached to a shown comment on that very page, and the page not deleted. Whether a reader
    may see it was decided when the gallery minted the token."""
    if not _ID.fullmatch(image_id):
        return None
    with db.connect() as conn:
        row = conn.execute(
            "select 1 from comment_image"  # noqa: S608 -- clause from the module, no caller input
            " join comment on comment.id = comment_image.comment_id"
            " join artifact on artifact.id = comment_image.artifact_id"
            " where comment_image.id = %s and comment_image.artifact_id = %s"
            f" and artifact.deleted_at is null and {_visible()}",
            (image_id, artifact_id),
        ).fetchone()
        return _bytes(conn, image_id) if row is not None else None


def _bytes(conn: Connection, image_id: str) -> tuple[str, bytes] | None:
    row = conn.execute(
        "select content_type, bytes from comment_image where id = %s", (image_id,)
    ).fetchone()
    return (row[0], bytes(row[1])) if row is not None else None


# --- the token a reader's browser carries ---------------------------------------------------


def _secret(conn: Connection) -> bytes:
    name = "image_token"
    row = conn.execute(
        "select value from server_secret where name = %s", (name,)
    ).fetchone()
    if row is None:
        conn.execute(
            "insert into server_secret (name, value) values (%s, %s)"
            " on conflict (name) do nothing",
            (name, secrets.token_hex(32)),
        )
        row = conn.execute(
            "select value from server_secret where name = %s", (name,)
        ).fetchone()
    return bytes.fromhex(row[0])


def _sign(key: bytes, image_id: str, expires: int) -> str:
    mac = hmac.new(key, f"ci:{image_id}:{expires}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def mint_token(db: Database, image_id: str, *, now: float | None = None) -> str:
    """A link token for one picture, good for an hour: HMAC over the id and the expiry. The
    gallery mints one for each picture of a comment it shows a reader who may read the page."""
    expires = int(
        (now if now is not None else time.time()) + TOKEN_LIFETIME.total_seconds()
    )
    with db.connect() as conn:
        return f"{expires}.{_sign(_secret(conn), image_id, expires)}"


def token_valid(
    db: Database, image_id: str, token: str, *, now: float | None = None
) -> bool:
    if not token or len(token) > 120:
        return False
    stamp, _, mac = token.partition(".")
    if not (stamp.isascii() and stamp.isdecimal()) or not mac.isascii():
        return False
    expires = int(stamp)
    if expires < (now if now is not None else time.time()):
        return False
    with db.connect() as conn:
        key = _secret(conn)
    return hmac.compare_digest(_sign(key, image_id, expires).encode(), mac.encode())


def origin_url(artifact_id: str, image_id: str, token: str) -> str:
    """Where a reader's browser fetches the picture: the page's own content origin."""
    return f"https://{artifact_id}.{store.content_domain()}/ci/{image_id}?t={token}"


# What every picture response carries, on either origin: the type from the bytes, never
# sniffed, and nothing the picture could do if it were somehow treated as a document.
SERVE_HEADERS = {
    "x-content-type-options": "nosniff",
    "content-security-policy": "default-src 'none'; sandbox",
    "referrer-policy": "no-referrer",
}
