"""Passes that let a browser read one page body, once, within minutes.

homelab-ij8.101. The gallery sits behind Jeremy's login; the page bodies sit on
their own origins (`<id>.<content domain>`) precisely so that an agent's page
can never touch that login. So the login cannot follow a page into its frame --
it is carried there instead by one of these, minted by the gallery each time it
shows the page and spent by the content origin on the first framed load.

WHY SINGLE USE AND NOT A COOKIE AFTER IT (claude.ai trades its pass for a
partitioned cookie): a cookie set inside a frame whose top page is another site
is a third-party cookie, and Safari -- Jeremy's phone -- is the browser most
likely to drop it. A pass that serves the body directly on its one use needs no
cookie in any browser. The price is that a frame which asks again (a reload
that restores the frame's last URL, which Firefox does and Chrome does on
back/forward) presents a spent pass. That case is answered with a small page
that asks the gallery for a fresh pass (`content.expired_page`, and the
`artifact-expired` handler in gallery.js) rather than with a dead frame.

WHY THIS LIFETIME: long enough for a phone on a poor connection to get from the
gallery's HTML to the frame's request; short enough that a pass copied out of a
page source is worthless by the time anyone could use it. Spent or not, it is
refused after this.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from artifacts.db import Database

LIFETIME_SECONDS = 120

# How long a spent or expired row is kept before `mint` sweeps it: long enough
# that a support question ("why did that frame say expired?") can still be
# answered from the table, short enough that the table never grows.
_KEEP_SECONDS = 3600


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Spent:
    artifact_id: str
    version: int | None


def mint(db: Database, artifact_id: str, version: int | None = None) -> str:
    """A fresh pass for one page (the live one when `version` is None). The
    plaintext is returned once and never stored."""
    token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.execute(
            "delete from frame_pass where expires_at < now() - make_interval(secs => %s)",
            (_KEEP_SECONDS,),
        )
        conn.execute(
            "insert into frame_pass (token_sha256, artifact_id, version, expires_at)"
            " values (%s, %s, %s, now() + make_interval(secs => %s))",
            (_hash(token), artifact_id, version, LIFETIME_SECONDS),
        )
    return token


def spend(db: Database, token: str, artifact_id: str) -> Spent | None:
    """Use the pass, if it is unused, unexpired, and for THIS page. One
    statement, so two requests racing with the same pass cannot both win.

    A pass for another page is refused and NOT spent: presenting it on the
    wrong host must not burn it for the right one. It is also indistinguishable
    from an unknown pass, so a pass cannot be used to learn which ids exist."""
    if not token or len(token) > 128:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "update frame_pass set used_at = now()"
            " where token_sha256 = %s and artifact_id = %s"
            " and used_at is null and expires_at > now()"
            " returning artifact_id, version",
            (_hash(token), artifact_id),
        ).fetchone()
    return Spent(artifact_id=row[0], version=row[1]) if row else None
