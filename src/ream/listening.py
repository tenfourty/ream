"""Where Jeremy's answer to a question goes, and whether anyone is waiting for it.

One source for the question card on a page, `GET /api/v1/listening` and
`artifacts-decide listening`, so the three cannot disagree. Every sentence a
reader sees is in `WORDING`, in one place; `line` is the only thing that fills it.

What this service can know, and what it cannot (docs/agent/specs/
2026-10-29-where-an-answer-goes.md):

  * KNOWN: a session of this agent holds an /events wait right now
    (`seat_read.held_until`); when a session last asked for events
    (`seat_read.last_read_at`); that the question was announced to the partner
    and that the announcement was delivered (`partner_outbox`).
  * NOT KNOWN: which Slack office the partner maps an agent to, whether that
    office is running, and when it acts. So the page says "sent", names the
    artifacts agent, and claims only "at its next check". An office that polls
    `list` and holds no wait leaves no trace here at all.

Session labels are declared by the caller (`api.session`), so this is a display
and never a permission; scoping uses the key's agent alone.
"""

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ream import decisions, events, readside
from ream.db import Database, in_list
from ream.pickup import PICKUP_FRESH

WAITING_NOW = "waiting_now"
SENT_TO_PARTNER = "sent_to_partner"
PARTNER_DELAYED = "partner_delayed"
NOT_SENT = "not_sent"
# The old state ids, emitted beside the new as `legacy_state` for one release
# (core-and-plugins design, section 6).
LEGACY_STATES = {SENT_TO_PARTNER: "sent_to_slack", PARTNER_DELAYED: "slack_delayed"}
# The answer source whose label names the partner in the wording. Still core's
# 'slack' until the Slack plug-in moves out.
PARTNER_SOURCE = "slack"
STATES = (WAITING_NOW, SENT_TO_PARTNER, PARTNER_DELAYED, NOT_SENT)
# Wording keys for the two states that can carry an agent's own pickup line.
SENT_TO_PARTNER_PICKUP = "sent_to_partner_pickup"
NOT_SENT_PICKUP = "not_sent_pickup"

# How far back `recent` looks, and how many it lists.
RECENT_WINDOW = timedelta(hours=24)
RECENT_LIMIT = 20


# THE WORDING. Edit here and nowhere else; {agent} is the artifacts agent that
# asked, {session} the waiting session's label, {seen} the "last seen" sentence.
WORDING = {
    WAITING_NOW: (
        "A session is waiting for your answer now{session}. "
        "It acts as soon as you answer."
    ),
    SENT_TO_PARTNER: (
        "No session is waiting. Your answer is delivered to {agent} in {partner}, "
        "and acted on at its next check.{seen}"
    ),
    # The agent declared its own pickup line (`set_pickup`): its words replace
    # "acted on at its next check". Never a promise of this service.
    SENT_TO_PARTNER_PICKUP: (
        "No session is waiting. Your answer is delivered to {agent} in {partner}. "
        "{says}{seen}"
    ),
    PARTNER_DELAYED: (
        "No session is waiting, and the copy of this question for {agent} in "
        "{partner} has not been delivered yet. Your answer is saved; the agent "
        "reads it when it next looks.{seen}"
    ),
    NOT_SENT: (
        "No session is waiting and this question is not in {partner}. Your answer "
        "is saved; the agent reads it when it next looks.{seen}"
    ),
    NOT_SENT_PICKUP: (
        "No session is waiting and this question is not in {partner}. Your answer "
        "is saved. {says}{seen}"
    ),
}
# What the gallery's strip says of an agent's current receipt (receipts design 4.3).
# {who} is "agent" or "agent, session"; {note} is filled or empty; a link is the gallery's to draw.
# A done carries no note here: its receipt also writes the outcome ("What it led to"), which shows it.
# Slice 3 adds its "{agent} was told in Slack {when}" line beside these.
RECEIPT_WORDING = {
    "seen": "{agent} picked this up {when}.",
    "working": "Working, {who}.{note}",
    "lapsed": "Lapsed: {agent} said working {ago}.{note}",
    "done": "Answered, {who}.",
    "blocked": "Blocked, {who}.{note}",
    "wont_do": "Won't do, {who}.{note}",
}
RECEIPT_TITLE = "Self-declared by the agent; Ream has not checked it"
SAYS = "{agent} says: {pickup}{stop}"
SEEN = " Last seen {age}."
NEVER_SEEN = " It has not been seen asking for events."
SEEN_TITLE = "Last time a session of this agent asked for its events"


@dataclass(frozen=True)
class Route:
    id: str
    agent: str
    state: str
    waiting_session: str | None
    last_seen: str | None
    announced: bool
    delivered: bool
    pickup: str | None = None
    # A line is stored but not shown, because the agent has not checked lately.
    pickup_stale: bool = False


PICKUP_LIMIT = 120


class BadPickup(ValueError):
    """A pickup line the service will not keep, with a reason fit to show."""


# Characters that draw nothing but are not removed from the stored text: a line
# made only of these (and of blanks) is as empty as "" and is refused.
_BLANK = ("Cc", "Cf", "Zs", "Zl", "Zp")
_FILLERS = frozenset("\u115f\u1160\u3164\uffa0\u2800")


def clean_pickup(text) -> str:
    """The agent's one line, or `BadPickup`. Single line only (a line break of
    any kind is refused, not folded); control and format characters (escape
    codes, bidi overrides, zero-width marks) are removed; runs of spaces
    collapse; at most PICKUP_LIMIT characters after that. Plain text: the page
    escapes it."""
    if not isinstance(text, str):
        raise BadPickup("pickup must be text")
    if any(c in text for c in "\n\r\x0b\x0c\x85\u2028\u2029"):
        raise BadPickup("pickup must be a single line")
    kept = "".join(
        c for c in text if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn")
    )
    cleaned = " ".join(kept.split())
    if not any(
        c not in _FILLERS and unicodedata.category(c) not in _BLANK for c in cleaned
    ):
        raise BadPickup("pickup is empty; use clear to remove it")
    if len(cleaned) > PICKUP_LIMIT:
        raise BadPickup(f"pickup is {len(cleaned)} characters; at most {PICKUP_LIMIT}")
    return cleaned


def get_pickup(db: Database, agent: str) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            "select pickup from agent_pickup where agent = %s", (agent,)
        ).fetchone()
    return row[0] if row else None


def set_pickup(db: Database, agent: str, text) -> str:
    """Keep `text` as this agent's pickup line (replacing any); returns the
    cleaned line. Per agent (key), because the service has no office entity."""
    cleaned = clean_pickup(text)
    with db.connect() as conn:
        conn.execute(
            "insert into agent_pickup (agent, pickup, updated_at) values (%s, %s, %s)"
            " on conflict (agent) do update"
            " set pickup = excluded.pickup, updated_at = excluded.updated_at",
            (agent, cleaned, datetime.now(UTC)),
        )
    return cleaned


def clear_pickup(db: Database, agent: str) -> bool:
    """Remove the line; True when there was one."""
    with db.connect() as conn:
        return (
            conn.execute("delete from agent_pickup where agent = %s", (agent,)).rowcount
            > 0
        )


def office_of(label: str | None) -> str:
    return (label or "").split("/", 1)[0].strip()


def _reads_for(label: str | None, agent: str, session: str) -> bool:
    """Whether a reader labelled `session` hears a question asked under `label`.

    A waiter hears its whole office (`decide._belongs`): the label equals the
    office or starts with `office/`. A question with no label goes to the key's
    own office, the one named like the agent. An UNLABELLED reader names no
    office and hears every office's answers under its key, so it counts for all."""
    if session == "":
        return True
    office = office_of(label) or agent
    return session == office or session.startswith(office + "/")


def _moment(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            return _moment(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def _readers(
    db: Database, agent: str
) -> list[tuple[str, datetime | None, datetime | None]]:
    """(session, last_read_at, held_until) for every session of this agent."""
    with db.connect() as conn:
        rows = conn.execute(
            "select session, last_read_at, held_until from seat_read where agent = %s",
            (agent,),
        ).fetchall()
    return [(r[0] or "", _moment(r[1]), _moment(r[2])) for r in rows]


def sessions(db: Database, agent: str, now: datetime | None = None) -> dict:
    """This agent's sessions: `waiting` (a hold that has not lapsed) and
    `recent` (the others read in the last day, newest first, at most 20).
    Only the calling agent's own, ever."""
    now = now or datetime.now(UTC)
    waiting, recent = [], []
    for session, last, held in _readers(db, agent):
        item = {
            "session": session,
            "office": office_of(session),
            "last_read_at": last.isoformat() if last else None,
        }
        if held is not None and held > now:
            waiting.append({**item, "held_until": held.isoformat()})
        elif last is not None and now - last <= RECENT_WINDOW:
            recent.append(item)
    waiting.sort(key=lambda s: s["held_until"], reverse=True)
    recent.sort(key=lambda s: s["last_read_at"], reverse=True)
    return {"waiting": waiting, "recent": recent[:RECENT_LIMIT]}


def _age(then: datetime, now: datetime) -> str:
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def for_decisions(
    db: Database, agent: str, decision_ids: list[str], now: datetime | None = None
) -> dict[str, Route]:
    """The route of each of `agent`'s own decisions among `decision_ids`. An id
    that is not this agent's (or is on a deleted page, or does not exist) is
    simply absent -- the same answer for all three."""
    if not decision_ids:
        return {}
    now = now or datetime.now(UTC)
    ids = list(dict.fromkeys(decision_ids))
    with db.connect() as conn:
        found = conn.execute(
            "select decision.id, decision.session, decision.partner_version"  # noqa: S608 -- placeholders only
            " from decision join artifact on artifact.id = decision.artifact_id"
            f" where decision.id in {in_list(ids)}"
            " and coalesce(decision.asked_by, artifact.agent) = %s and artifact.deleted_at is null",
            (*ids, agent),
        ).fetchall()
        delivered: dict[str, bool] = {}
        if found and readside.bridge_on():
            own = [r[0] for r in found]
            for decision_id, at in conn.execute(
                "select decision_id, delivered_at from partner_outbox"  # noqa: S608 -- placeholders only
                f" where kind = 'asked' and decision_id in {in_list(own)}",
                tuple(own),
            ).fetchall():
                delivered[decision_id] = at is not None
    readers = _readers(db, agent)
    stored = get_pickup(db, agent)
    bridge_on = readside.bridge_on()
    out: dict[str, Route] = {}
    for decision_id, label, version in found:
        mine = [r for r in readers if _reads_for(label, agent, r[0])]
        holding = sorted(
            (r for r in mine if r[2] is not None and r[2] > now),
            key=lambda r: r[2],
            reverse=True,
        )
        seen = [r[1] for r in mine if r[1] is not None]
        fresh = bool(seen) and now - max(seen) <= PICKUP_FRESH
        announced = bridge_on and (version or 0) > 0
        sent = announced and delivered.get(decision_id, False)
        if holding:
            state = WAITING_NOW
        elif sent:
            state = SENT_TO_PARTNER
        elif announced:
            state = PARTNER_DELAYED
        else:
            state = NOT_SENT
        out[decision_id] = Route(
            id=decision_id,
            agent=agent,
            state=state,
            waiting_session=holding[0][0] if holding else None,
            last_seen=max(seen).isoformat() if seen else None,
            announced=announced,
            delivered=sent,
            pickup=stored if fresh else None,
            pickup_stale=bool(stored) and not fresh,
        )
    return out


def for_decision(db: Database, agent: str, decision_id: str) -> Route | None:
    return for_decisions(db, agent, [decision_id]).get(decision_id)


def line(route: Route, now: datetime | None = None) -> str:
    """The sentence for this route, plain text (the page escapes it)."""
    now = now or datetime.now(UTC)
    last = _moment(route.last_seen)
    seen = SEEN.format(age=_age(last, now)) if last else NEVER_SEEN
    session = f" ({route.waiting_session})" if route.waiting_session else ""
    key = route.state
    says = ""
    if route.pickup and key in (SENT_TO_PARTNER, NOT_SENT):
        key = SENT_TO_PARTNER_PICKUP if key == SENT_TO_PARTNER else NOT_SENT_PICKUP
        stop = "" if route.pickup[-1] in ".!?" else "."
        says = SAYS.format(agent=route.agent, pickup=route.pickup, stop=stop)
    return WORDING[key].format(
        agent=route.agent,
        session=session,
        seen=seen,
        says=says,
        partner=decisions.source_label(PARTNER_SOURCE),
    )


def route_view(route: Route, now: datetime | None = None) -> dict:
    """A route as the API and the CLI carry it."""
    legacy = (
        {"legacy_state": LEGACY_STATES[route.state]}
        if route.state in LEGACY_STATES
        else {}
    )
    return {
        "id": route.id,
        "state": route.state,
        **legacy,
        "waiting_session": route.waiting_session,
        "route": {
            "kind": "partner" if route.announced else "none",
            "agent": route.agent,
            "announced": route.announced,
            "delivered": route.delivered,
        },
        "pickup": route.pickup,
        "pickup_stale": route.pickup_stale,
        "last_seen": route.last_seen,
        "line": line(route, now),
    }


def status(db: Database, agent: str, decision_id: str | None = None) -> dict | None:
    """The whole `GET /listening` body, or None when `decision_id` is not this
    agent's (the route answers that exactly as it answers a missing id)."""
    now = datetime.now(UTC)
    body = {
        "agent": agent,
        "now": now.isoformat(),
        "ttl_grace_seconds": events.HOLD_GRACE_SECONDS,
        **sessions(db, agent, now),
        "bridge": {"enabled": readside.bridge_on()},
        "pickup": get_pickup(db, agent),
        "pickup_fresh_hours": PICKUP_FRESH // timedelta(hours=1),
    }
    if decision_id is not None:
        route = for_decision(db, agent, decision_id)
        if route is None:
            return None
        body["decision"] = route_view(route, now)
    return body


def _since(then: datetime, now: datetime) -> str:
    """How long ago, in whole units: '45 minutes ago', '1 hour ago', '5 hours ago'."""
    minutes = max(0, int((now - then).total_seconds()) // 60)
    if minutes < 60:
        n, unit = minutes, "minute"
    elif minutes < 2880:
        n, unit = minutes // 60, "hour"
    else:
        n, unit = minutes // 1440, "day"
    return f"{n} {unit}{'' if n == 1 else 's'} ago"


def receipt_state(receipt: dict) -> str:
    """The strip's key: the receipt's state, or `lapsed` for a working one that ran out."""
    return "lapsed" if receipt.get("lapsed") else receipt["state"]


def receipt_line(receipt: dict, when: str = "", now: datetime | None = None) -> str:
    """The sentence for a current receipt (`receipts.current_map`'s dict), plain text.
    `when` is the caller's own rendering of `receipt["at"]` (the gallery's date style);
    `now` is a parameter so a test never waits."""
    now = now or datetime.now(UTC)
    key = receipt_state(receipt)
    agent = receipt.get("agent") or "the agent"
    session = receipt.get("session")
    note = (receipt.get("note") or "").strip()
    then = _moment(receipt.get("at"))
    return RECEIPT_WORDING[key].format(
        agent=agent,
        who=f"{agent}, {session}" if session else agent,
        note=f" {note}" if note else "",
        when=when,
        ago=_since(then, now) if then else "earlier",
    )
