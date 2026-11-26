"""The owner id a database was written for, and changing it (core-and-plugins design, 3.2).

`owner_id` is stored in rows (`by`, `author`, `answered_by`, `actor`, a folder's owner). A pod
started with another id would read every one of the owner's rows as a stranger's. So the id is
recorded once in `service_meta`, and every start compares:

* nothing recorded yet (a brand-new database, or the first start after this slice): the
  configured id is recorded;
* recorded and equal: carry on;
* recorded and different: `OwnerMismatch`, and the service does not start.

`artifacts-admin change-owner` is the one way to move a database to another id: it rewrites
every column below in one transaction and records the new id. The `person` table holds the
owner as person #1 (`seed_person`); its row moves with the id.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ream import settings
from ream.db import Database, Rollback
from ream.settings import OWNER_ID

META_KEY = "owner_id"

# Fixed, and distinct from rename.py's and db.py's lock keys.
_LOCK_KEY = 0x6F776E65_72000001  # "owner" + 1

# (table.column, the WHERE clause beyond `column = old`). The values the owner's own acts
# stored. Agent names are never the owner's id (`keys.RESERVED_AGENT_NAMES`), so a column an
# agent also writes (`event.actor`, `comment.resolved_by`) still holds only the owner's rows
# where it says the id. Deliberately NOT rewritten: `partner_outbox.body` (the JSON an
# outbound event was sent as: history of what was delivered).
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("comment.author", " and author_kind = 'human'"),
    ("comment.resolved_by", ""),
    ("decision.answered_by", ""),
    ("partner_question.answered_by", ""),
    ("event.actor", ""),
    ("comment_image.staged_by", ""),
    ("folder.owner", ""),
    ("folder_alias.owner", ""),
    # Person #1, the owner (people design, slice 1). The other people's ids are not the owner's.
    ("person.id", ""),
    # Per-person state (slice 2): what the owner has read, prefers and pinned, and the passes
    # minted for them.
    ("page_read.person", ""),
    ("human_pref.person", ""),
    ("page_pin.person", ""),
    ("frame_pass.person", ""),
    # Per-person answers and receipts (slice 3): whom a question is addressed to, each person's
    # own record of a form, and whose answer a receipt is about.
    ("decision.addressed_to", ""),
    ("form_answer.person", ""),
    ("receipt.person", ""),
    # Gallery sign-in (slice 4): the sessions of the people signed in.
    ("person_session.person", ""),
)

log = logging.getLogger("ream.owner")


class OwnerMismatch(Exception):
    """The database was written for another owner id than the one configured."""

    def __init__(self, stored: str, configured: str) -> None:
        super().__init__(
            f"this database was written for owner {stored!r}, but REAM_OWNER_ID is"
            f" {configured!r}: set REAM_OWNER_ID={stored}, or move the database with"
            f" `artifacts-admin change-owner {configured}`"
        )
        self.stored = stored
        self.configured = configured


class IdentityHeld(Exception):
    """Another person already holds the sign-in identity the settings give the owner."""


class Refused(Exception):
    """A change of owner the service will not do, with a reason fit to print."""


def stored(db: Database) -> str | None:
    """The owner id the database was written for, or None before it was ever recorded."""
    with db.connect() as conn:
        row = conn.execute(
            "select value from service_meta where key = %s", (META_KEY,)
        ).fetchone()
    return row[0] if row else None


def check(db: Database, configured: str) -> str:
    """The start-up guard: record `configured` if nothing is recorded, return it if it
    matches, raise `OwnerMismatch` if it does not. Race-safe: two pods starting together
    both insert, and the loser re-reads."""
    with db.connect() as conn:
        conn.execute(
            "insert into service_meta (key, value) values (%s, %s)"
            " on conflict (key) do nothing",
            (META_KEY, configured),
        )
        row = conn.execute(
            "select value from service_meta where key = %s", (META_KEY,)
        ).fetchone()
        if row[0] == configured:
            seed_person(conn, configured)
    if row[0] != configured:
        raise OwnerMismatch(row[0], configured)
    return configured


def seed_person(conn, owner_id: str) -> None:
    """Person #1: the owner's row in `person`, made if it is not there. The issuer and subject
    come from `REAM_OIDC_ISSUER` + `REAM_OIDC_OWNER_SUBJECT` when both are set (the setting
    wins over what the row holds; a WARNING, naming no value, says the row was changed), else
    from the pin the owner's first OIDC sign-in wrote (`oidc.PIN_KEY`, "<issuer> <sub>") into a
    row that has none. `IdentityHeld` when another person holds the pair the settings name."""
    conn.execute(
        "insert into person (id) values (%s) on conflict (id) do nothing", (owner_id,)
    )
    cfg = settings.current()
    if cfg.oidc_issuer and cfg.oidc_owner_subject:
        issuer, subject = cfg.oidc_issuer, cfg.oidc_owner_subject
        holder = conn.execute(
            "select id from person where issuer = %s and subject = %s",
            (issuer, subject),
        ).fetchone()
        if holder is not None and holder[0] != owner_id:
            raise IdentityHeld(
                "another person holds the sign-in identity REAM_OIDC_ISSUER and"
                " REAM_OIDC_OWNER_SUBJECT give the owner: the owner's row cannot take it"
            )
        row = conn.execute(
            "select issuer, subject from person where id = %s", (owner_id,)
        ).fetchone()
        if (row[0], row[1]) != (issuer, subject):
            conn.execute(
                "update person set issuer = %s, subject = %s where id = %s",
                (issuer, subject, owner_id),
            )
            if row[0] is not None or row[1] is not None:
                log.warning(
                    "the owner's stored sign-in identity differed from REAM_OIDC_ISSUER and"
                    " REAM_OIDC_OWNER_SUBJECT; the settings were used"
                )
        return
    pin = conn.execute(
        "select value from service_meta where key = %s", ("oidc_owner_sub",)
    ).fetchone()
    if pin and " " in pin[0]:
        issuer, subject = pin[0].split(" ", 1)
        conn.execute(
            "update person set issuer = %s, subject = %s"
            " where id = %s and issuer is null and subject is null",
            (issuer, subject, owner_id),
        )


def change_owner(db: Database, new: str, *, dry_run: bool) -> dict[str, int]:
    """Move the database to owner id `new`: rewrite every stored column in one transaction
    and record `new`. Returns the rows changed (or that would change) per `table.column`,
    plus `service_meta`. Raises `Refused` for an invalid id, an id some agent already
    holds, an unrecorded database, or `new` equal to the stored one."""
    if not OWNER_ID.fullmatch(new):
        raise Refused(f"invalid owner id {new!r}: must match {OWNER_ID.pattern!r}")
    counts: dict[str, int] = {}
    with db.connect() as conn, conn.transaction():
        conn.advisory_lock(_LOCK_KEY)
        row = conn.execute(
            "select value from service_meta where key = %s", (META_KEY,)
        ).fetchone()
        if row is None:
            raise Refused(
                "no owner is recorded in this database yet (the service records it at"
                " its first start); start the service once, then change it"
            )
        old = row[0]
        if old == new:
            raise Refused(f"the owner is already {new!r}; nothing changed")
        holder = conn.execute(
            "select 1 from agent_key where agent = %s"
            " union select 1 from artifact where agent = %s limit 1",
            (new, new),
        ).fetchone()
        if holder is not None:
            raise Refused(
                f"{new!r} is the name of an agent that already has keys or pages;"
                " an owner id must not be an agent's name"
            )
        for column, extra in _COLUMNS:
            table, col = column.split(".")
            counts[column] = conn.execute(
                f"update {table} set {col} = %s where {col} = %s{extra}",  # noqa: S608 -- fixed identifiers from _COLUMNS
                (new, old),
            ).rowcount
        counts["service_meta"] = conn.execute(
            "update service_meta set value = %s, updated_at = %s where key = %s",
            (new, datetime.now(UTC), META_KEY),
        ).rowcount
        if dry_run:
            raise Rollback()
    return counts
