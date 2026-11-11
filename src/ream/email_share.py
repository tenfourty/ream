"""Sharing one page with one person by email (homelab-ij8.16).

Jeremy's rulings, 24 Sep 2026: a personal link per recipient, emailed by the
service; the first time it is opened on a device the reader confirms with a
one-time code sent to the same inbox, so a forwarded email lets nobody else
in; he sees who has access and withdraws each person separately. Sent through
the mail backend a plug-in registered (ream.mail).

Three secrets, all stored only as sha256: the link token (in the URL), the
six-digit code (valid ten minutes, five tries), and the device cookie (set on
the page's own origin for thirty days after a correct code). Withdrawing a
person kills all three at once, because every check joins through their
unrevoked share row.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ream.db import Database

CODE_MINUTES = 10
CODE_TRIES = 5
CODES_PER_HOUR = 5
DEVICE_DAYS = 30
_EMAIL = re.compile(r"^[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$")


class Refused(Exception):
    """Something to say to the person, in words."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def clean_email(value: str) -> str | None:
    email = (value or "").strip()
    return email.lower() if _EMAIL.match(email) else None


def mask(email: str) -> str:
    """j***@example.com -- shown to someone who has only the link."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


@dataclass(frozen=True)
class Share:
    id: str
    artifact_id: str
    email: str
    created_at: datetime
    last_opened_at: datetime | None


def create(db: Database, artifact_id: str, email: str) -> tuple[Share, str]:
    """A new personal link for `email` on this page. Returns (share, token);
    the token goes into the email and is never stored. Sharing again with the
    same address replaces their earlier link."""
    token = secrets.token_urlsafe(24)
    share_id = secrets.token_hex(10)
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "update email_share set revoked_at = %s"
            " where artifact_id = %s and email = %s and revoked_at is null",
            (datetime.now(UTC), artifact_id, email),
        )
        row = conn.execute(
            "insert into email_share (id, artifact_id, email, token_sha256)"
            " values (%s, %s, %s, %s) returning id, artifact_id, email, created_at,"
            " last_opened_at",
            (share_id, artifact_id, email, _hash(token)),
        ).fetchone()
    return Share(*row), token


def people(db: Database, artifact_id: str) -> list[Share]:
    with db.connect() as conn:
        rows = conn.execute(
            "select id, artifact_id, email, created_at, last_opened_at from email_share"
            " where artifact_id = %s and revoked_at is null order by created_at",
            (artifact_id,),
        ).fetchall()
    return [Share(*row) for row in rows]


def withdraw(db: Database, artifact_id: str, share_id: str) -> bool:
    with db.connect() as conn:
        done = conn.execute(
            "update email_share set revoked_at = %s"
            " where id = %s and artifact_id = %s and revoked_at is null",
            (datetime.now(UTC), share_id, artifact_id),
        )
        return done.rowcount > 0


def find(db: Database, artifact_id: str, token: str) -> Share | None:
    """The live share this link belongs to, on this page, or None."""
    if not token or len(token) > 128:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "select s.id, s.artifact_id, s.email, s.created_at, s.last_opened_at"
            " from email_share s join artifact a on a.id = s.artifact_id"
            " where s.token_sha256 = %s and s.artifact_id = %s"
            " and s.revoked_at is null and a.deleted_at is null",
            (_hash(token), artifact_id),
        ).fetchone()
    return Share(*row) if row else None


def new_code(db: Database, share: Share) -> str:
    """A fresh six-digit code for this share, limited per hour. The caller
    emails it; only its hash is kept."""
    now = datetime.now(UTC)
    with db.connect() as conn, conn.transaction():
        recent = conn.execute(
            "select count(*) from email_share_code where share_id = %s"
            " and created_at > %s",
            (share.id, now - timedelta(hours=1)),
        ).fetchone()[0]
        if recent >= CODES_PER_HOUR:
            raise Refused(
                "Too many codes have been sent for this link in the last hour. "
                "Try again later."
            )
        code = f"{secrets.randbelow(1_000_000):06d}"
        conn.execute(
            "insert into email_share_code (id, share_id, code_sha256, expires_at)"
            " values (%s, %s, %s, %s)",
            (
                secrets.token_hex(10),
                share.id,
                _hash(share.id + ":" + code),
                now + timedelta(minutes=CODE_MINUTES),
            ),
        )
    return code


def check_code(db: Database, share: Share, typed: str) -> str | None:
    """If `typed` is this share's live code, spend it and return a new device
    cookie value (store it in the browser; only its hash is kept). Every wrong
    try counts against the newest live code."""
    typed = re.sub(r"\s", "", typed or "")
    now = datetime.now(UTC)
    with db.connect() as conn, conn.transaction():
        row = conn.execute(
            "select id, code_sha256, attempts from email_share_code"  # noqa: S608 -- clause from the db layer, no caller input
            " where share_id = %s and used_at is null and expires_at > %s"
            " order by created_at desc limit 1" + conn.for_update(),
            (share.id, now),
        ).fetchone()
        if row is None:
            return None
        code_id, stored, attempts = row
        if attempts >= CODE_TRIES:
            return None
        if not (
            re.fullmatch(r"\d{6}", typed)
            and hmac.compare_digest(stored, _hash(share.id + ":" + typed))
        ):
            conn.execute(
                "update email_share_code set attempts = attempts + 1 where id = %s",
                (code_id,),
            )
            return None
        conn.execute(
            "update email_share_code set used_at = %s where id = %s", (now, code_id)
        )
        cookie = secrets.token_urlsafe(32)
        conn.execute(
            "insert into email_share_device (cookie_sha256, share_id, expires_at)"
            " values (%s, %s, %s)",
            (_hash(cookie), share.id, now + timedelta(days=DEVICE_DAYS)),
        )
    return cookie


def device_ok(db: Database, share: Share, cookie: str | None) -> bool:
    if not cookie or len(cookie) > 128:
        return False
    now = datetime.now(UTC)
    with db.connect() as conn:
        row = conn.execute(
            "select 1 from email_share_device d join email_share s on s.id = d.share_id"
            " where d.cookie_sha256 = %s and d.share_id = %s and d.expires_at > %s"
            " and s.revoked_at is null",
            (_hash(cookie), share.id, now),
        ).fetchone()
        if row:
            conn.execute(
                "update email_share set last_opened_at = %s where id = %s",
                (now, share.id),
            )
    return row is not None
