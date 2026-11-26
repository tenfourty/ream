"""The person behind a gallery request (people design, slice 1).

`current(request)` is the one place that says who a gallery request is for, and `require_owner`
the one that says a route is the owner's alone. Today there is one person, the owner (person #1,
`REAM_OWNER_ID`), so `current` answers the owner from settings whatever the request says: no
identity header, cookie or address is read, and the values are exactly what
`settings.owner_id()` gave before. Per-person sign-in (people design, slice 4) changes only the
body of `current`; the handlers that take it do not change again.

`OPEN_SIGN_IN` stays False until step 4 of the design: nothing here lets a second person in.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from ream import keys, settings
from ream.settings import OWNER_ID

# Precedent: `clientlevel.COMMENTS_REQUIRED`. Whether anyone but the owner may sign in.
OPEN_SIGN_IN = False


@dataclass(frozen=True)
class Person:
    """Who a request is for. `id` is the stored id (`by`, `author`, `answered_by`, `actor`)."""

    id: str
    name: str
    is_owner: bool


def _person(row) -> Person:
    """A `person` row (id, name) as the request's person."""
    s = settings.current()
    if row[0] == s.owner_id:
        return Person(id=row[0], name=s.owner_name, is_owner=True)
    return Person(id=row[0], name=row[1] or row[0], is_owner=False)


def owner_person() -> Person:
    """Person #1, from settings, read at call time."""
    s = settings.current()
    return Person(id=s.owner_id, name=s.owner_name, is_owner=True)


def current(request: Request) -> Person:
    """The person this gallery request is for. A FastAPI dependency; `request` is taken so the
    signature is the one slice 4 needs, and is not read yet."""
    return owner_person()


CURRENT = Depends(current)


def require_owner(person: Person = CURRENT) -> Person:
    """The route is the owner's alone (machines, keys, page ownership). Answers 404, as for a
    page that is not there, to anyone else; today `current` never returns anyone else."""
    if not person.is_owner:
        raise HTTPException(404, "not found")
    return person


# Module-level, so a handler's default is a name, not a call (ruff B008).
REQUIRE_OWNER = Depends(require_owner)


# --- Who a login is (decision D: slice 4) -------------------------------------------------

_ID_ROOM = 60  # the id pattern allows 63 characters; a `-99` suffix needs three


class NameTaken(Exception):
    """No readable id is free for this login, or the pair is held by someone else."""


def normalise_id(raw: str) -> str:
    """`raw` as an id the OWNER_ID pattern accepts: lower case, every run of other characters
    one hyphen, hyphens trimmed, cut to leave room for a suffix. Empty gives `person`."""
    cut = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:_ID_ROOM].strip("-")
    return cut or "person"


def _held_names(conn, name: str) -> bool:
    """Whether `name` is held in the one name space people and agents share: a person's id, an
    agent's key, a page's publisher, a person-and-app agent, or an old agent name a rename left
    behind (as an old name, and as the name an old one became)."""
    for sql in (
        "select 1 from person where id = %s",
        "select 1 from agent_key where agent = %s",
        "select 1 from artifact where agent = %s",
        "select 1 from mcp_agent where agent = %s",
        "select 1 from agent_alias where old = %s",
        "select 1 from agent_alias where new = %s",
    ):
        if conn.execute(sql + " limit 1", (name,)).fetchone() is not None:
            return True
    return False


def _free_id(conn, base: str) -> str:
    if not _held_names(conn, base):
        return base
    for n in range(2, 100):
        if not _held_names(conn, f"{base}-{n}"):
            return f"{base}-{n}"
    raise NameTaken(f"no free id is left for {base!r}")


def _readable_base(
    preferred: str | None, email: str | None, email_verified: bool
) -> str:
    if preferred and preferred.strip():
        return normalise_id(preferred)
    if email and email_verified and "@" in email:
        return normalise_id(email.split("@", 1)[0])
    return "person"


def find_or_make(
    db,
    issuer: str,
    subject: str,
    *,
    preferred: str | None,
    email: str | None,
    email_verified: bool,
    name: str | None,
) -> Person:
    """The person for (issuer, subject), made if there is none. Matched on the pair alone, never
    on an email. A new person's id is the login's `preferred_username`, else the local part of a
    verified email, normalised, with `-2` to `-99` when the name is held; once given it is never
    changed, and person rows are never deleted, so it is never reused. The email is kept only when
    verified. Runs under the name lock, so no agent takes the id meanwhile."""
    if not issuer or not subject:
        raise NameTaken("a login without an issuer and a subject names no one")
    with db.connect() as conn, conn.transaction():
        conn.advisory_lock(keys.NAME_LOCK_KEY)
        row = conn.execute(
            "select id, name from person where issuer = %s and subject = %s",
            (issuer, subject),
        ).fetchone()
        kept_email = email if email_verified and email else None
        if row is not None:
            conn.execute(
                "update person set email = coalesce(%s, email), name = coalesce(%s, name)"
                " where id = %s",
                (kept_email, name or None, row[0]),
            )
            return _person(
                conn.execute(
                    "select id, name from person where id = %s", (row[0],)
                ).fetchone()
            )
        new_id = _free_id(conn, _readable_base(preferred, email, email_verified))
        if not OWNER_ID.fullmatch(
            new_id
        ):  # pragma: no cover -- normalise_id builds to fit
            new_id = _free_id(conn, f"person-{secrets.token_hex(3)}")
        conn.execute(
            "insert into person (id, issuer, subject, email, name) values (%s, %s, %s, %s, %s)",
            (new_id, issuer, subject, kept_email, name or None),
        )
        return _person((new_id, name or None))


def fill_owner_identity(conn, issuer: str, subject: str) -> bool:
    """Give person #1 the (issuer, subject) of the owner's login when the row has none. Returns
    whether it did. A row that has a pair is left alone; a pair another person holds raises
    `NameTaken` (the unique key would refuse it anyway)."""
    owner_id = settings.current().owner_id
    holder = conn.execute(
        "select id from person where issuer = %s and subject = %s", (issuer, subject)
    ).fetchone()
    if holder is not None:
        if holder[0] != owner_id:
            raise NameTaken("another person holds the owner's sign-in identity")
        return False
    done = conn.execute(
        "update person set issuer = %s, subject = %s"
        " where id = %s and issuer is null and subject is null",
        (issuer, subject, owner_id),
    )
    return done.rowcount == 1
