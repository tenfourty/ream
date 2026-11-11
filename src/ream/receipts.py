"""Delivery receipts: what an agent says it did with Jeremy's answer
(docs/agent/specs/2026-11-04-receipts-design.md, slice 1: decisions).

Five states an agent may declare, each optional: SEEN, WORKING, DONE, BLOCKED, WONT_DO.
Every one is the agent's own statement about itself, labelled with the agent (from the key)
and the session (a label), never verified and never a permission.

Where each lives:
- Whether a decision is SEEN is `decision.acknowledged_at/by`, first wins
  (`decisions.acknowledge`); a `seen` row is written beside it to carry the session.
- Every state is an append-only row in `receipt`. The CURRENT receipt of a decision is the
  latest live row that is not `seen`, else the seen row, else (acknowledged by `close`, or
  before receipts existed) seen from the columns, else nothing.
- A new human act on a decision (a form submit or relay, a review sent back) VOIDS every
  row: they stay as history and stop being current, so last week's "done" never covers this
  morning's answer. The acknowledge columns are cleared at the same sites.
- `working` expires (default 4 hours, at most 12). A lapsed one is computed on read.

Two choices pending Jeremy's ruling are one-line constants below so they are easy to change:
`DONE_CLOSES` (does "done" also close the decision; no) and `STORE_SESSION` (is the session
kept on every receipt; yes).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from ream import events, ids, store, text, textvars
from ream.db import in_list

# Design 9.5: "answered" shown while the question stays open; closing is a separate act.
DONE_CLOSES = False
# Design 9.3: the agent AND the session on every state.
STORE_SESSION = True

STATES = ("seen", "working", "done", "blocked", "wont_do")
# What the verb and the API accept for the last state.
_ALIASES = {"wont-do": "wont_do", "wont_do": "wont_do"}
NEEDS_NOTE = ("blocked", "wont_do")
MAX_NOTE = 1_000
MAX_LINK = 500
WORKING_DEFAULT = timedelta(hours=4)
WORKING_MAX = timedelta(hours=12)

# fullmatch, and no control character or space anywhere: `$` would let a trailing newline by.
_LINK = re.compile(r"https?://[^\x00-\x20\x7f]+")
_DURATION = re.compile(r"^(\d+)\s*([mh])$")


class Refused(Exception):
    """A receipt the service will not take, with a reason fit to print (422)."""


class NotAnswered(Exception):
    """`done` on a decision he has not answered yet (409)."""


def normalise_state(state: str) -> str:
    state = _ALIASES.get(state, state)
    if state not in STATES:
        raise Refused("state must be one of: seen, working, done, blocked, wont-do")
    return state


def parse_lifetime(value: str | None) -> timedelta:
    """`--for` as a lifetime: '30m', '4h'. Default 4 hours, at most 12."""
    if value is None or value == "":
        return WORKING_DEFAULT
    m = _DURATION.match(value.strip().lower())
    if m is None:
        raise Refused("for must be a duration like 30m or 4h")
    span = timedelta(minutes=int(m[1])) if m[2] == "m" else timedelta(hours=int(m[1]))
    if span <= timedelta(0) or span > WORKING_MAX:
        raise Refused("for must be more than 0 and at most 12h")
    return span


def _check_link(link: str | None) -> str | None:
    if link is None or link == "":
        return None
    if len(link) > MAX_LINK or not _LINK.fullmatch(link):
        raise Refused("link must be an http(s) address, at most 500 characters")
    return link


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def is_lapsed(state: str, expires_at, now: datetime) -> bool:
    """The one rule for a lapsed claim, for the API and the gallery alike: a `working`
    whose `expires_at` is strictly before `now` (at the instant itself it still stands),
    with nothing after it (a later state would have replaced it as the current receipt).
    Computed on read, never stored. `expires_at` may be a datetime or an ISO string;
    a naive one is UTC."""
    if state != "working" or expires_at in (None, ""):
        return False
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except ValueError:
            return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at < now


def _shape(row, now: datetime) -> dict:
    """One `receipt` row (id, state, note, link, agent, session, comment_id, created_at,
    expires_at, voided_at) as the JSON an agent reads."""
    expires = row[8]
    return {
        "state": row[1],
        "note": text.sanitize(row[2], limit=MAX_NOTE),
        "link": row[3],
        "agent": row[4],
        "session": row[5],
        "comment_id": row[6],
        "at": _iso(row[7]),
        "expires_at": _iso(expires),
        # Computed, never stored: the claim ran out and nothing came after it.
        "lapsed": is_lapsed(row[1], expires, now),
    }


_COLUMNS = (
    "id, state, note, link, agent, session, comment_id, created_at, expires_at,"
    " voided_at"
)


def current_map(conn, decisions: list, now: datetime | None = None) -> dict[str, dict]:
    """The current receipt of each `DecisionRow` in `decisions`, by decision id; a
    decision with none is absent. One query for the rows and none for seen, which is on
    the row already."""
    if not decisions:
        return {}
    now = now or datetime.now(UTC)
    ids_ = [d.id for d in decisions]
    found: dict[str, dict] = {}
    rows = conn.execute(
        f"select subject_id, {_COLUMNS} from receipt"  # noqa: S608 -- fixed text, placeholders only
        f" where subject_kind = 'decision' and voided_at is null"
        f" and subject_id in {in_list(ids_)}"
        " order by created_at asc, id asc",
        tuple(ids_),
    ).fetchall()
    seen: dict[str, dict] = {}
    for row in rows:
        # The latest overwrites; a `seen` row never replaces a later claim of work.
        (seen if row[2] == "seen" else found)[row[0]] = _shape(row[1:], now)
    for did, shaped in seen.items():
        found.setdefault(did, shaped)
    for d in decisions:
        if d.id not in found and d.acknowledged_at is not None:
            found[d.id] = {
                "state": "seen",
                "note": "",
                "link": None,
                "agent": d.acknowledged_by,
                "session": None,
                "comment_id": None,
                "at": d.acknowledged_at,
                "expires_at": None,
                "lapsed": False,
            }
    return found


def void(conn, decision_id: str) -> None:
    """A new human act reopened this decision: every receipt so far becomes history."""
    conn.execute(
        "update receipt set voided_at = %s"
        " where subject_kind = 'decision' and subject_id = %s and voided_at is null",
        (datetime.now(UTC), decision_id),
    )


def history_owned(conn, agent: str, decision_id: str) -> list[dict] | None:
    """`history` for `agent`'s own decision on a live page; None (a 404) for a missing
    one, someone else's, or one whose page is in the bin, as the POST answers."""
    found = conn.execute(
        "select 1 from decision join artifact on artifact.id = decision.artifact_id"
        " where decision.id = %s and coalesce(decision.asked_by, artifact.agent) = %s"
        "   and artifact.deleted_at is null",
        (decision_id, agent),
    ).fetchone()
    return None if found is None else history(conn, decision_id)


def history(conn, decision_id: str, now: datetime | None = None) -> list[dict]:
    """Every receipt row on a decision, oldest first, with whether it is still current.
    Seen is not here: it is the decision's own `acknowledged_at`."""
    now = now or datetime.now(UTC)
    rows = conn.execute(
        f"select {_COLUMNS} from receipt"  # noqa: S608 -- fixed text
        " where subject_kind = 'decision' and subject_id = %s"
        " order by created_at asc, id asc",
        (decision_id,),
    ).fetchall()
    out = []
    latest_live = None
    for row in rows:
        shaped = _shape(row, now)
        shaped["current"] = False
        if row[9] is None and (row[1] != "seen" or latest_live is None):
            latest_live = shaped
        out.append(shaped)
    if latest_live is not None:
        latest_live["current"] = True
    return out


def insert(
    conn,
    decision_id: str,
    state: str,
    *,
    agent: str,
    note: str = "",
    link: str | None = None,
    session: str | None = None,
    comment_id: str | None = None,
    span: timedelta | None = None,
) -> None:
    """Append one receipt row. No checks: `declare` makes them."""
    now = datetime.now(UTC)
    conn.execute(
        "insert into receipt (id, subject_kind, subject_id, state, note, link, agent,"
        " session, comment_id, created_at, expires_at)"
        " values (%s, 'decision', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            ids.new_id(),
            decision_id,
            state,
            note,
            link,
            agent,
            session if STORE_SESSION else None,
            comment_id,
            now,
            now + span if span is not None else None,
        ),
    )


def declare(
    conn,
    agent: str,
    decision_id: str,
    *,
    state: str,
    note: str = "",
    link: str | None = None,
    comment_id: str | None = None,
    lifetime: str | None = None,
    session: str | None = None,
) -> dict:
    """Record one receipt on `agent`'s own decision and return the current receipt.
    `store.NotFound` for a decision that is not theirs or does not exist, identically;
    `Refused` for a receipt that breaks a rule. All on the caller's `conn`, so an
    Idempotency-Key'd request commits it with the stored response."""
    from ream import decisions  # late: decisions reads this module

    state = normalise_state(state)
    note = note.strip() if note else ""
    link = _check_link(link)
    if state == "seen" and (note or link or lifetime):
        raise Refused("seen takes no note, link or duration")
    if state in NEEDS_NOTE and not note:
        raise Refused(f"{state.replace('_', '-')} needs a note saying why")
    if link and state != "done":
        raise Refused("only done takes a link")
    if lifetime and state != "working":
        raise Refused("only working takes a duration")
    if len(note) > MAX_NOTE:
        raise Refused(f"note is over {MAX_NOTE} characters")
    span = parse_lifetime(lifetime) if state == "working" else None

    # Locked, so a declare cannot interleave with a concurrent void (a new answer).
    owned = conn.execute(
        "select decision.artifact_id, decision.answered_at from decision"  # noqa: S608 -- fixed text, placeholders only
        " join artifact on artifact.id = decision.artifact_id"
        " where decision.id = %s and coalesce(decision.asked_by, artifact.agent) = %s"
        "   and artifact.deleted_at is null" + conn.for_update(" of decision"),
        (decision_id, agent),
    ).fetchone()
    if owned is None:
        raise store.NotFound()
    artifact_id = owned[0]
    if state == "done" and owned[1] is None:
        raise NotAnswered(
            textvars.render(
                "{{owner_they}} {{owner_has}} not answered this yet, so there is "
                "nothing to be done"
            )
        )

    if state == "seen":
        row = decisions._acknowledge(conn, agent, decision_id, session=session)
        return row.receipt

    for field, body in (("note", note), ("link", link or "")):
        if body:
            store._check_secrets(
                body, f"receipt {field}", agent=agent, artifact_id=artifact_id
            )
    insert(
        conn,
        decision_id,
        state,
        agent=agent,
        note=note,
        link=link,
        session=session,
        comment_id=comment_id,
        span=span,
    )
    if state == "done":
        # The card and the inbox already show outcomes; the receipt's note is its body.
        conn.execute(
            "insert into decision_outcome (id, decision_id, body, agent, session, link)"
            " values (%s, %s, %s, %s, %s, %s)",
            (ids.new_id(), decision_id, note or "done", agent, session, link),
        )
        if DONE_CLOSES:
            decisions._close_row(conn, agent, decisions.raw_in(conn, decision_id))
    summary = f"{agent} {state.replace('_', ' ')}" + (f": {note}" if note else "")
    events.append(
        conn,
        artifact_id=artifact_id,
        agent=agent,
        actor=agent,
        kind="receipt_set",
        summary=summary,
        ref_id=decision_id,
        note=note,
        session=session,
    )
    return current_map(conn, [decisions.get_in(conn, decision_id)])[decision_id]


def declare_on(db, conn, agent: str, decision_id: str, **fields) -> dict:
    """`declare` on the caller's transaction when it has one (an Idempotency-Key'd
    request), else on one of its own."""
    if conn is not None:
        return declare(conn, agent, decision_id, **fields)
    with db.connect() as own, own.transaction():
        return declare(own, agent, decision_id, **fields)
