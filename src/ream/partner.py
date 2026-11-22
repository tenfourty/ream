"""The outbound half of the partner bridge (runcove-5t3oj.39.4).

A question has one home. When artifacts is the home, the partner service
(quarterdeck, on the homelab) holds a copy that says `source: "artifacts:<id>"`
and is told what happens to the question: asked, answered, withdrawn, replaced,
set aside, reopened.
This module is how it is told, and nothing else: the receiving side, and what
the partner sends back, are another slice.

The shape, in four pieces:

  * CONFIG, all optional. Off unless every variable is set; set only some and
    the service refuses to start (`config_from_env`). Off means no rows are
    written and no thread runs: the service behaves exactly as it did.
  * THE OUTBOX, a table written in the SAME transaction as the change that
    causes an event (`enqueue`), so an event exists if and only if its cause
    does. A rolled-back ask leaves nothing behind.
  * THE SENDER, one daemon thread (`start`), the same shape as the comment
    sweeper and the nightly maintenance. Never on a request's path.
  * THE SIGNATURE: Ed25519 over the exact request body bytes, base64 in
    `SIGNATURE_HEADER`. The private key is read once from a file; it is never
    logged, never put in an error, never stored.

Delivery is at least once. Each attempt is re-signed with a fresh `sent_at`
(the receiver refuses a body more than five minutes old), keeping the same
`event_id` and `version`; the receiver answers a duplicate with success.
Versions count per question and a later version is never delivered before an
earlier one has been.
"""

from __future__ import annotations

import base64
import contextvars
import json
import logging
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ream import db as dbmod
from ream import ids, settings
from ream.events import BRIDGE_KINDS

if TYPE_CHECKING:
    from ream.db import Connection, Database

_log = logging.getLogger("ream.partner")

# The one place the header's name is written.
SIGNATURE_HEADER = "X-Partner-Signature"
# The signing key's id, also inside the signed body: the receiver picks the key from
# this header and verifies the raw bytes before parsing them, then refuses a body
# whose key_id differs.
KEY_ID_HEADER = "X-Partner-Key-Id"

ENV_NAME = "REAM_PARTNER_NAME"
ENV_URL = "REAM_PARTNER_EVENTS_URL"
ENV_KEY_FILE = "REAM_PARTNER_SIGNING_KEY_FILE"
ENV_KEY_ID = "REAM_PARTNER_KEY_ID"
_ENV_ALL = (ENV_NAME, ENV_URL, ENV_KEY_FILE, ENV_KEY_ID)
# Optional, outside the all-or-none set: plain http only when asked for by name.
ENV_ALLOW_HTTP = "REAM_PARTNER_ALLOW_HTTP"

KINDS = BRIDGE_KINDS  # one list, defined with the journal's (events.py)

REQUEST_TIMEOUT_SECONDS = 10
# How long a claimed row is the claimer's. Longer than a request can take, short
# enough that a crashed process's rows are picked up again within the minute.
LEASE_SECONDS = 60
POLL_SECONDS = 2.0
BATCH = 20
BACKOFF_BASE_SECONDS = 5
BACKOFF_CAP_SECONDS = 15 * 60
KEEP_DELIVERED = timedelta(days=30)
_PRUNE_EVERY = timedelta(hours=1)


class PartnerConfigError(Exception):
    """The partner settings are half there, or the key cannot be used. The
    message names variables, never a value."""


@dataclass(frozen=True)
class Config:
    name: str
    url: str
    key_id: str
    private_key: object  # an Ed25519PrivateKey; never printed

    def __repr__(self) -> str:  # a stray log of the config shows no key
        return f"Config(name={self.name!r}, key_id={self.key_id!r})"


def enabled() -> bool:
    """Whether events are written. Read at call time, like `mail.enabled`. True
    only when every variable is set; a half-configured process never gets as far
    as running (`config_from_env` stops it at start-up)."""
    c = settings.current()
    return all(
        (
            c.partner_name,
            c.partner_events_url,
            c.partner_signing_key_file,
            c.partner_key_id,
        )
    )


def allow_http() -> bool:
    return settings.current().partner_allow_http == "1"


def config_from_env() -> Config | None:
    """None when the feature is off (no variable set). Raises
    `PartnerConfigError` when some but not all are set, when the URL is not
    http(s), or when the key file is missing or is not an Ed25519 private key in
    PEM PKCS8. Nothing from the key file reaches a message."""
    cfg = settings.current()
    given = {
        ENV_NAME: cfg.partner_name,
        ENV_URL: cfg.partner_events_url,
        ENV_KEY_FILE: cfg.partner_signing_key_file,
        ENV_KEY_ID: cfg.partner_key_id,
    }
    present = [n for n in _ENV_ALL if given[n]]
    if not present:
        return None
    missing = [n for n in _ENV_ALL if n not in present]
    if missing:
        raise PartnerConfigError(
            "the partner bridge is half-configured: set all of "
            + ", ".join(_ENV_ALL)
            + " or none; missing "
            + ", ".join(missing)
        )
    url = cfg.partner_events_url
    plain_ok = url.startswith("http://") and allow_http()
    if not url.startswith("https://") and not plain_ok:
        raise PartnerConfigError(
            f"{ENV_URL} must start with https:// (plain http:// only with "
            f"{ENV_ALLOW_HTTP}=1)"
        )
    path = cfg.partner_signing_key_file
    try:
        with open(path, "rb") as handle:
            pem = handle.read()
    except OSError as exc:
        raise PartnerConfigError(
            f"{ENV_KEY_FILE} cannot be read ({type(exc).__name__})"
        ) from None
    return Config(
        name=cfg.partner_name,
        url=url,
        key_id=cfg.partner_key_id,
        private_key=load_private_key(pem),
    )


def load_private_key(pem: bytes):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    try:
        key = load_pem_private_key(pem, password=None)
    except Exception:  # noqa: BLE001 -- the library's message can quote the input
        raise PartnerConfigError(
            f"{ENV_KEY_FILE} is not a PEM private key (PKCS8, unencrypted)"
        ) from None
    if not isinstance(key, Ed25519PrivateKey):
        raise PartnerConfigError(f"{ENV_KEY_FILE} is not an Ed25519 key")
    return key


def sign(private_key, body: bytes) -> str:
    """The base64 Ed25519 signature over exactly `body`."""
    return base64.b64encode(private_key.sign(body)).decode("ascii")


def source_of(decision_id: str) -> str:
    return f"artifacts:{decision_id}"


# --- writing events -----------------------------------------------------------


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def enqueue(
    conn: Connection,
    decision_id: str,
    kind: str,
    body: dict,
    *,
    at: datetime | None = None,
) -> int | None:
    """Add one event to the outbox, in the caller's open transaction. Returns
    its version, or None when nothing was written: the feature is off, or the
    question was never announced to the partner (a `withdrawn` for a question
    whose `asked` predates the bridge would tell it about something it never
    saw).

    The version comes from one UPDATE of the decision's counter. That statement
    takes the row's lock (SQLite: the whole write lock, from `begin immediate`),
    so two events for the same question can never read the same number, and the
    unique (decision_id, version) is a second guard. The counter lives on the
    decision, so it survives restarts and is the same number whichever process
    wrote the event.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown partner event kind {kind!r}")
    if not enabled():
        return None
    version = next_version(conn, decision_id, kind)
    if version is None:
        return None
    when = at or datetime.now(UTC)
    conn.execute(
        "insert into partner_outbox"
        " (event_id, decision_id, version, kind, body, created_at, next_attempt_at)"
        " values (%s, %s, %s, %s, %s, %s, %s)",
        (
            ids.new_id(),
            decision_id,
            version,
            kind,
            dbmod.jsonb(body),
            when,
            when,
        ),
    )
    return version


def next_version(conn: Connection, decision_id: str, kind: str) -> int | None:
    """Bump and return the question's partner version, in the caller's open
    transaction; None for a question that is not a plain question, or that was never
    announced (`kind` other than `asked` while the counter is 0). Core's own stage
    calls it through `enqueue`; while the house plug-in stands in (step 9e-3) its stage
    calls it too, so the partner sees one unbroken run of versions across the switch
    and back. It does not read core's partner settings: the caller decided to send.

    **`decision.partner_version` is the one counter of record, and this is the only way
    to take a number from it**, also once the plug-in holds the inbound half (step
    9e-4): a plug-in that sends a partner event takes its version here and keeps no
    counter of its own. Two things rest on that. Core's stage guard
    (`would_enqueue`) reads this column to know a question was announced, so a count
    kept elsewhere would leave it 0 and let a process without the plug-in's stage drop
    a `withdrawn` or `answered` in silence; and a release has no counter to copy back,
    so there is nothing for it to get wrong against a bump in flight.

    The statement takes the row's lock (SQLite: the whole write lock), so two events
    for one question never share a number."""
    if kind not in KINDS:
        raise ValueError(f"unknown partner event kind {kind!r}")
    row = conn.execute(
        "update decision set partner_version = partner_version + 1"
        " where id = %s and kind = 'question'"
        " and (partner_version > 0 or %s)"
        " returning partner_version",
        (decision_id, kind == "asked"),
    ).fetchone()
    return None if row is None else row[0]


def asked_body(
    *,
    asking_office: str,
    title: str,
    options: list[dict],
    recommended: str | None,
    context: str,
    link: str,
    topic: dict | None,
    depends_on: list,
    replaces: str | None,
    urgency: str,
    blocked_on: str | None,
    needed_by: datetime | str | None,
    for_offices: list,
) -> dict:
    if isinstance(needed_by, datetime):
        needed_by = _iso(needed_by)
    return {
        "asking_office": asking_office,
        "title": title,
        "options": [{"letter": o["letter"], "text": o["label"]} for o in options],
        "recommended": recommended,
        "context": context,
        "link": link,
        "topic": topic,
        "depends_on": list(depends_on),
        "replaces": replaces,
        "urgency": urgency,
        "blocked_on": blocked_on,
        "needed_by": needed_by,
        "for": list(for_offices),
    }


def answered_body(
    *,
    letter: str,
    note: str,
    answered_by: str,
    answered_at: datetime,
    answer_source: str,
    recorded_by: str | None = None,
    said_at: datetime | None = None,
) -> dict:
    """He answered on the page (`answer_source` 'page', verified by artifacts),
    or a seat recorded words he gave elsewhere (verified_by 'agent': artifacts
    saw only the seat's word, named in `recorded_by`)."""
    current = settings.current()
    name = current.owner_name if answered_by == current.owner_id else answered_by
    verified = {"page": "artifacts", "slack": "quarterdeck"}.get(answer_source, "agent")
    body = {
        "answer": letter,
        "note": note,
        "answered_by": {"name": name, "verified_by": verified},
        "answered_at": _iso(answered_at),
        "answer_source": answer_source,
    }
    if recorded_by is not None:
        body["recorded_by"] = recorded_by
    if said_at is not None:
        body["said_at"] = _iso(said_at)
    return body


FOREIGN_PREFIX = "qd:"


def foreign_source(question_id: str) -> str:
    """The source of a question whose home is the partner."""
    return f"{FOREIGN_PREFIX}{question_id}"


def enqueue_foreign(
    conn: Connection,
    source: str,
    version: int,
    kind: str,
    body: dict,
    *,
    at: datetime | None = None,
) -> None:
    """Add one event about a question whose home is the PARTNER (his answer given
    here, on its copy). The outbox's `decision_id` column holds the whole source
    ("qd:<id>"), so `unique (decision_id, version)` is a version space of our
    own per copy, which never meets the home's: the home counts its events, we
    count ours. `version` comes from `partner_question.out_version`, bumped by
    the caller in the same transaction."""
    if kind not in KINDS:
        raise ValueError(f"unknown partner event kind {kind!r}")
    if not source.startswith(FOREIGN_PREFIX):
        raise ValueError("a foreign event names a partner-home source")
    when = at or datetime.now(UTC)
    conn.execute(
        "insert into partner_outbox"
        " (event_id, decision_id, version, kind, body, created_at, next_attempt_at)"
        " values (%s, %s, %s, %s, %s, %s, %s)",
        (ids.new_id(), source, version, kind, dbmod.jsonb(body), when, when),
    )


def link_for(artifact_id: str, decision_id: str) -> str:
    from ream import store

    return f"{store.gallery_origin().rstrip('/')}/a/{artifact_id}#q-{decision_id}"


# --- the envelope, signed per attempt ------------------------------------------


def envelope(row: dict, key_id: str, sent_at: datetime) -> bytes:
    """The exact request body for one attempt. `sent_at` and `key_id` are inside
    it, so the one signature covers them. Compact JSON, UTF-8."""
    held = row["decision_id"]
    foreign = held.startswith(FOREIGN_PREFIX)
    event = {
        "event_id": row["event_id"],
        "kind": row["kind"],
        "source": held if foreign else source_of(held),
        "question": held.removeprefix(FOREIGN_PREFIX),
        "version": row["version"],
        "key_id": key_id,
        "at": _iso(row["created_at"]),
        "sent_at": _iso(sent_at),
        "body": row["body"],
    }
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()


class DeliveryFailed(Exception):
    """One attempt failed. The text is short and safe to store: a status code or
    a class of failure, never a response body."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    """POST and return the status. Standard library, as mail.py does: the
    service has no HTTP client dependency. Redirects are not followed (a signed
    body is not forwarded anywhere it was not addressed). Raises
    `DeliveryFailed` for a transport failure."""
    request = urllib.request.Request(  # noqa: S310 -- scheme checked in config_from_env
        url, data=body, method="POST", headers=headers
    )
    # No proxy from the environment: a signed body goes only where it is addressed.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except TimeoutError:
        raise DeliveryFailed("timed out") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise DeliveryFailed("timed out") from None
        raise DeliveryFailed("could not connect") from None
    except OSError:
        raise DeliveryFailed("connection failed") from None


PostFn = Callable[[str, bytes, dict[str, str], float], int]


# --- delivering -----------------------------------------------------------------


def _backoff(attempts: int) -> timedelta:
    return timedelta(
        seconds=min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempts - 1))
    )


def _claim(db: Database, now: datetime, limit: int = 1) -> list[dict]:
    """Rows to deliver now, claimed by a lease. Only the OLDEST undelivered
    version of each question is eligible, so version N+1 waits for N whatever
    N's own retry time, and a parked N holds its later versions. A read-only
    look comes first, so an empty queue never takes SQLite's writer lock. On
    Postgres the select takes `for update of o skip locked`, so two processes
    claiming together get different rows; the lease (`claimed_until`) then holds
    the row without holding a transaction open across the network. SQLite has
    one process and `begin immediate` already serialises writers. Nothing is due
    while a plug-in holds the bridge (step 9e-3), read in the same statement."""
    due = (
        " where o.delivered_at is null and o.parked_at is null"
        " and o.next_attempt_at <= %s"
        " and (o.claimed_until is null or o.claimed_until <= %s)"
        " and not exists (select 1 from partner_outbox p"
        "   where p.decision_id = o.decision_id and p.version < o.version"
        "   and p.delivered_at is null)"
        # Step 9e-3: the owner is read here, in the statement that takes the row, so a
        # plug-in that claimed the bridge after the pass began gets every later row.
        " and not exists (select 1 from service_meta m where m.key = %s)"
    )
    with db.connect() as conn:
        waiting = conn.execute(
            "select 1 from partner_outbox o" + due + " limit 1",  # noqa: S608 -- fixed text
            (now, now, BRIDGE_OWNER_KEY),
        ).fetchone()
    if waiting is None:
        return []
    lease = now + timedelta(seconds=LEASE_SECONDS)
    with db.connect() as conn, conn.transaction():
        rows = conn.execute(
            "select o.event_id, o.decision_id, o.version, o.kind, o.body,"  # noqa: S608 -- clause from the db layer, no caller input
            " o.created_at, o.attempts from partner_outbox o"
            + due
            + " order by o.created_at, o.version limit %s"
            + conn.for_update(" of o skip locked"),
            (now, now, BRIDGE_OWNER_KEY, limit),
        ).fetchall()
        for row in rows:
            conn.execute(
                "update partner_outbox set claimed_until = %s where event_id = %s",
                (lease, row[0]),
            )
    return [
        {
            "event_id": r[0],
            "decision_id": r[1],
            "version": r[2],
            "kind": r[3],
            "body": r[4],
            "created_at": r[5],
            "attempts": r[6],
        }
        for r in rows
    ]


# Statuses that retrying cannot change: the receiver understood the request and
# refused THIS event. Everything else is retried with backoff, 401 and 403
# included: the partner answers 401 for a bad signature, an unknown key_id or a
# stale sent_at, all of which a key rotation or a clock fix cures, so the queue
# drains by itself once that is done.
_PARK_ON = (400, 404, 409, 410, 413, 415, 422)
# A run of these in the outbox means a credential or clock problem; status counts them.
_AUTH_ERRORS = ("HTTP 401", "HTTP 403")


def _permanent(error: str) -> bool:
    return error.startswith("HTTP ") and int(error[5:]) in _PARK_ON


def _attempt(config: Config, row: dict, post: PostFn, now: datetime) -> str | None:
    """One signed attempt. None on success, else a short reason."""
    body = envelope(row, config.key_id, now)
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign(config.private_key, body),
        KEY_ID_HEADER: config.key_id,
        "User-Agent": "artifacts-service/1",
    }
    try:
        status = post(config.url, body, headers, REQUEST_TIMEOUT_SECONDS)
    except DeliveryFailed as exc:
        return str(exc)[:200]
    except Exception as exc:  # noqa: BLE001 -- a bug in a transport must not stop the loop
        return f"unexpected {type(exc).__name__}"
    if 200 <= status < 300:
        return None
    return f"HTTP {status}"


def deliver_due(
    db: Database,
    config: Config,
    post: PostFn = http_post,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[int, int]:
    """One pass: claim what is due ONE ROW AT A TIME (so a row's lease is taken
    just before its own attempt and a slow neighbour cannot let it lapse), attempt
    it, record the outcome. Returns (delivered, failed). Safe to call from two
    processes at once. A pass is bounded by BATCH rows."""
    delivered = failed = 0
    for _ in range(BATCH):
        claimed = _claim(db, clock(), 1)
        if not claimed:
            break
        row = claimed[0]
        # Fresh each attempt: the receiver refuses a body whose sent_at is stale.
        error = _attempt(config, row, post, clock())
        finished = clock()
        with db.connect() as conn:
            if error is None:
                conn.execute(
                    "update partner_outbox set delivered_at = %s,"
                    " claimed_until = null, attempts = attempts + 1"
                    " where event_id = %s",
                    (finished, row["event_id"]),
                )
                delivered += 1
                continue
            attempts = row["attempts"] + 1
            parked = _permanent(error)
            conn.execute(
                "update partner_outbox set attempts = %s, next_attempt_at = %s,"
                " claimed_until = null, last_error = %s, last_error_at = %s,"
                " parked_at = %s where event_id = %s",
                (
                    attempts,
                    finished + _backoff(attempts),
                    error,
                    finished,
                    finished if parked else None,
                    row["event_id"],
                ),
            )
            failed += 1
            _log.warning(
                "partner event v%s %s %s (attempt %d): %s",
                row["version"],
                row["kind"],
                "PARKED, will not be retried until released"
                if parked
                else "not delivered",
                attempts,
                error,
            )
    return delivered, failed


def unpark(
    db: Database, event_id: str | None = None, now: datetime | None = None
) -> int:
    """Release one parked event (or all, with no id) for another go: attempts and
    the retry time are reset. Returns how many were released."""
    now = now or datetime.now(UTC)
    with db.connect() as conn:
        return conn.execute(
            "update partner_outbox set parked_at = null, attempts = 0,"  # noqa: S608 -- fixed text
            " next_attempt_at = %s, claimed_until = null"
            " where parked_at is not null and delivered_at is null"
            + (" and event_id = %s" if event_id is not None else ""),
            (now, event_id) if event_id is not None else (now,),
        ).rowcount


# --- the hand-over to the house plug-in (step 9e-3; goes with this module in 9e-4) ---

# The switch is a database fact, not a per-process registry: every process of the new
# image reads it in the transaction it stages in, and once per sender pass.
BRIDGE_OWNER_KEY = "partner_bridge_owner"
# The key names a plug-in whose bridge stage is not loaded in this process (step 9e-3,
# second review round): said once per process, at WARNING.
_owner_absent_warned = False


class BridgeHeld(Exception):
    """The bridge is held by another owner (claim), or not by this one (release)."""


# One event's dispatch to its stage sinks reads the owner once (runcove-5t3oj.129): core's
# stage and the owner's stage each ask `bridge_owner(conn)`, and under READ COMMITTED a
# claim or release committing between two separate reads would let them disagree (no
# stage takes the event, or two do). `events.emit` opens `one_owner_per_dispatch`; inside
# it, every `bridge_owner` call on that same connection gets the first read's answer.
# [connection, answer or _UNREAD], or None outside a dispatch.
_dispatch: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "ream_partner_dispatch_owner", default=None
)
_UNREAD = object()


@contextmanager
def one_owner_per_dispatch(conn: Connection) -> Iterator[None]:
    """While one event is handed to its sinks on `conn`: `bridge_owner(conn)` reads the
    database once, on first use, and gives that same answer to every later call on `conn`
    until the block ends. Another connection still reads afresh."""
    token = _dispatch.set([conn, _UNREAD])
    try:
        yield
    finally:
        _dispatch.reset(token)


def _read_bridge_owner(conn: Connection) -> str | None:
    row = conn.execute(
        "select value from service_meta where key = %s", (BRIDGE_OWNER_KEY,)
    ).fetchone()
    return None if row is None else row[0]


def bridge_owner(conn: Connection) -> str | None:
    """Who holds the outbound bridge: a plug-in's name, or None while core does. Called
    by a stage sink while one event is being staged (on the connection the stage was
    given), it answers what was read once for that event, the same for every stage."""
    pin = _dispatch.get()
    if pin is not None and pin[0] is conn:
        if pin[1] is _UNREAD:
            pin[1] = _read_bridge_owner(conn)
        return pin[1]
    return _read_bridge_owner(conn)


def claim_bridge(conn: Connection, owner: str) -> None:
    """Take the outbound bridge from core, in the caller's transaction: the plug-in calls
    it in the same transaction as its first `hand_over`. `owner` must be the plug-in's own
    entry-point name (the name `api.sink` records on its sinks): core stands down only
    where a stage sink of that plug-in is loaded, and a failing stage fails the action
    (`plugin_api.BridgeStageFailed`) only when the stored owner is that name. From its
    commit, core's sender stands down in every process, and core's stage wherever the
    owner's stage is loaded; elsewhere core's stage goes on writing core's outbox for the
    owner's `hand_over` to move. The same owner again changes nothing; another owner is
    refused (`BridgeHeld`), also when two claim at once (the insert does nothing on a
    conflict and the owner is read again)."""
    held = bridge_owner(conn)
    if held == owner:
        return
    if held is not None:
        raise BridgeHeld(f"the partner bridge is held by {held!r}")
    conn.execute(
        "insert into service_meta (key, value) values (%s, %s) on conflict do nothing",
        (BRIDGE_OWNER_KEY, owner),
    )
    held = bridge_owner(conn)
    if held != owner:
        raise BridgeHeld(f"the partner bridge is held by {held!r}")


_COLUMN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?")


def core_outbox_clear(column: str) -> str:
    """A WHERE fragment, true when core's outbox has no undelivered row for the question
    `column` names (`o.decision_id`, say). The plug-in's claim statement must carry it:
    `hand_over`'s `held` is a snapshot, and on Postgres a staging transaction that has not
    committed yet (core's stage in a process without the owner's stage) can add a row
    after it, so the claim checks again in the statement that takes the row. `column`
    is a column name, optionally qualified; anything else is a ValueError."""
    if not _COLUMN.fullmatch(column):
        raise ValueError("core_outbox_clear takes a column name")
    return (
        "not exists (select 1 from partner_outbox core_p"  # noqa: S608 -- a checked column name only
        f" where core_p.decision_id = {column} and core_p.delivered_at is null)"
    )


def _owner_here(owner: str) -> bool:
    from ream import sinks

    return sinks.bridge_stage_of(owner)


def note_owner_absent(owner: str) -> None:
    """Say once per process, at WARNING, that the bridge's owner has no bridge stage
    loaded here. Its name only, no value."""
    global _owner_absent_warned
    if _owner_absent_warned:
        return
    _owner_absent_warned = True
    _log.warning(
        "partner bridge: held by plug-in %r, whose bridge stage is not loaded in this"
        " process; its partner events wait in core's outbox for that plug-in's sender."
        " If the plug-in was removed, run `artifacts-admin partner-bridge --release`",
        owner,
    )


def warn_if_owner_absent(db: Database) -> None:
    """At server start, after the plug-ins load: `note_owner_absent` if the key names a
    plug-in whose bridge stage this process did not load."""
    with db.connect() as conn:
        owner = bridge_owner(conn)
    if owner is not None and not _owner_here(owner):
        note_owner_absent(owner)


def release_bridge(conn: Connection, owner: str, rows: list[dict]) -> int:
    """The undo, run before the plug-in is removed: give `rows` (the owner's undelivered
    outbox rows, `hand_over`'s shape) back to core's outbox and the bridge back to core,
    in one transaction, so there is no moment with two senders or none. Refused
    (`BridgeHeld`) unless `owner` holds it, and (`InboundHeld`) while a plug-in still
    holds the inbound half: `release_inbound` comes first. Returns how many rows went
    back."""
    if bridge_owner(conn) != owner:
        raise BridgeHeld(f"the partner bridge is not held by {owner!r}")
    # Step 9e-4: inbound goes back first. The row is locked, so an inbound claim cannot
    # commit between this read and the delete below (it reads the bridge's owner under
    # the same lock, and finds core).
    holder = lock_inbound(conn)
    if holder != CORE:
        raise InboundHeld(holder)
    put = take_back(conn, rows)
    conn.execute("delete from service_meta where key = %s", (BRIDGE_OWNER_KEY,))
    return put


# --- the inbound half's own switch (step 9e-4; goes with this module at the deletion) ---

# A second database fact, apart from the outbound one: an image that knows only
# `partner_bridge_owner` could not stand its endpoint down, so the outbound claim must
# not flip inbound. No row means core (nothing seeds it: the first writer to lock it
# inserts 'core'), and a release sets it back to 'core'.
INBOUND_OWNER_KEY = "partner_inbound_owner"
CORE = "core"


class InboundHeld(Exception):
    """A plug-in holds the inbound half, so core may not do this: write a partner event
    to core's outbox (it takes no new row once inbound is claimed; what was there before,
    the holder's sender drains with `hand_over`), or give the outbound bridge back.
    Names the plug-in, never a value."""

    def __init__(self, owner: str) -> None:
        super().__init__(f"the partner bridge's inbound half is held by {owner!r}")
        self.owner = owner


# What a caller waits before it tries again after `InboundHeld` (seconds): the same
# number the partner endpoint gives with its own 503.
INBOUND_HELD_RETRY_AFTER = 30


def inbound_held_detail(owner: str) -> str:
    """What a caller outside the partner endpoint is told when core's stage refused
    (`InboundHeld`): the plug-in's name, that nothing was saved, and what to do."""
    return (
        f"this would send an event to the partner, and that is the {owner} plug-in's"
        " to send now, but it is not loaded in the process that took this request."
        " Nothing was saved. Try again later or tell the operator (run it where"
        f" REAM_PLUGINS selects {owner})."
    )


def inbound_owner(conn: Connection) -> str:
    """Who holds the inbound half: `CORE`, or a plug-in's name. A plain read, no lock: for
    routing and a first look before a transaction. A writer uses `lock_inbound`, and so
    does core's stage where it would write core's outbox."""
    row = conn.execute(
        "select value from service_meta where key = %s", (INBOUND_OWNER_KEY,)
    ).fetchone()
    return CORE if row is None else row[0]


def lock_inbound(conn: Connection, *, share: bool = False) -> str:
    """The inbound holder, with its row locked for the rest of the caller's transaction.
    Every inbound writer calls it first in its own transaction and writes only if the
    answer is its own name; a claim or release calls it too, so a switch waits for the
    writers in flight and they for it. `share=True` (a writer) lets other writers pass
    and holds off only a switch. Lock order where a decision row is also written: the
    decision row first, then this. SQLite's writer lock already serialises."""
    conn.execute(
        "insert into service_meta (key, value) values (%s, %s) on conflict do nothing",
        (INBOUND_OWNER_KEY, CORE),
    )
    lock = conn.for_share() if share else conn.for_update()
    row = conn.execute(
        "select value from service_meta where key = %s" + lock,  # noqa: S608 -- fixed text and the lock clause
        (INBOUND_OWNER_KEY,),
    ).fetchone()
    return row[0]


def claim_inbound(conn: Connection, owner: str) -> None:
    """Take the inbound half from core, in the caller's transaction: the plug-in calls it,
    then `hand_over_inbound`, then writes what that returned into its own tables, and
    commits once. Refused (`BridgeHeld`) unless `owner` already holds the outbound bridge
    (`claim_bridge`), or when another plug-in holds inbound. The same owner again changes
    nothing. From the commit core's endpoint answers 503 with `Retry-After`, core's own
    answer on a partner-home copy says "not mine", and core's stage refuses to write
    core's outbox."""
    if not owner or owner == CORE:
        raise ValueError("claim_inbound takes a plug-in's name")
    held = lock_inbound(conn)
    if bridge_owner(conn) != owner:
        raise BridgeHeld(
            f"the partner bridge is not held by {owner!r}: claim the outbound half first"
        )
    if held == owner:
        return
    if held != CORE:
        raise BridgeHeld(f"the partner bridge's inbound half is held by {held!r}")
    conn.execute(
        "update service_meta set value = %s where key = %s", (owner, INBOUND_OWNER_KEY)
    )


# Core's three inbound tables, every column, in the order `hand_over_inbound` reads and
# `release_inbound` writes them.
INBOUND_TABLES: dict[str, tuple[str, ...]] = {
    "partner_question": (
        "source",
        "version",
        "asking_office",
        "title",
        "options",
        "recommended",
        "context",
        "link",
        "topic_id",
        "topic_title",
        "depends_on",
        "replaces",
        "replaced_by",
        "urgency",
        "blocked_on",
        "needed_by",
        "for_offices",
        "state",
        "answer",
        "answer_note",
        "answered_by",
        "answered_at",
        "pending_answer",
        "pending_note",
        "pending_at",
        "out_version",
        "created_at",
        "updated_at",
    ),
    "partner_version_seen": ("source", "version", "updated_at"),
    "partner_event_seen": (
        "event_id",
        "source",
        "version",
        "kind",
        "outcome",
        "detail",
        "seen_at",
    ),
}
_INBOUND_JSON = ("options", "depends_on", "for_offices", "detail")


@dataclass(frozen=True)
class InboundHandover:
    """What `hand_over_inbound` moved: `rows`, each core inbound table's name to its rows
    (dicts of `INBOUND_TABLES[name]`, JSON columns decoded). No version counter moves:
    `decision.partner_version` stays the counter of record (`next_version`)."""

    rows: dict[str, list[dict]]


def _decoded(value):
    return json.loads(value) if isinstance(value, str | bytes) else value


def hand_over_inbound(conn: Connection) -> InboundHandover:
    """Move every row out of core's three inbound tables, in the caller's open
    transaction, after `claim_inbound` in that same transaction: the rows are returned and
    deleted, so the caller inserts them into its own tables before it commits and a row is
    in exactly one table set (a rollback keeps them here; a row left here would shadow the
    plug-in's copy on the read side). Refused (`BridgeHeld`) while core holds inbound.

    It reads and locks no decision row (a core answer in flight holds its decision row
    and waits for the inbound row this transaction holds, so locking decisions here would
    deadlock with it), and it hands over no version counter: the plug-in takes every
    version from `next_version`, before the claim and after it."""
    if lock_inbound(conn) == CORE:
        raise BridgeHeld("the partner bridge's inbound half is held by core")
    rows: dict[str, list[dict]] = {}
    for table, cols in INBOUND_TABLES.items():
        found = conn.execute(
            f"select {', '.join(cols)} from {table} order by {cols[0]}"  # noqa: S608 -- fixed names
        ).fetchall()
        moved = [dict(zip(cols, r, strict=True)) for r in found]
        for row in moved:
            for col in _INBOUND_JSON:
                if col in row:
                    row[col] = _decoded(row[col])
        rows[table] = moved
        conn.execute(f"delete from {table}")  # noqa: S608 -- fixed names
    return InboundHandover(rows=rows)


def release_inbound(
    conn: Connection,
    owner: str,
    read_rows: Callable[[Connection], dict[str, list[dict]]],
) -> int:
    """The undo of the inbound claim, run before `release_bridge`, in one transaction:
    put the plug-in's rows back into core's inbound tables and give inbound back to
    core. Refused (`BridgeHeld`) unless `owner` holds it; the caller's transaction must
    roll back then. A row already here is skipped. Returns how many rows went back.

    `read_rows(conn)` returns the rows (`hand_over_inbound`'s shape) from the plug-in's
    own tables, and should delete them there. It is a function, not the rows, so that
    it is called only AFTER the switch's row is locked: every inbound writer of the
    plug-in waits on that lock or has committed, so the rows it reads are all of them.
    Rows read before the lock could miss a write still in flight, which would then be
    left behind in the plug-in's tables.

    It locks the switch row first and touches no decision row, so it cannot deadlock
    with an answer in flight (which holds its decision row and waits for this row), and
    it carries no version numbers: `decision.partner_version` never left core
    (`next_version`), so there is no copy that a bump landing meanwhile could overtake."""
    if lock_inbound(conn) != owner:
        raise BridgeHeld(f"the partner bridge's inbound half is not held by {owner!r}")
    rows = read_rows(conn)
    unknown = set(rows) - set(INBOUND_TABLES)
    if unknown:
        raise ValueError(f"not an inbound table: {min(unknown)!r}")
    put = 0
    for table, cols in INBOUND_TABLES.items():
        for row in rows.get(table, []):
            put += conn.execute(
                f"insert into {table} ({', '.join(cols)})"  # noqa: S608 -- fixed names
                f" values ({', '.join(['%s'] * len(cols))}) on conflict do nothing",
                tuple(
                    dbmod.jsonb(_decoded(row[c]))
                    if c in _INBOUND_JSON and row.get(c) is not None
                    else row.get(c)
                    for c in cols
                ),
            ).rowcount
    conn.execute(
        "update service_meta set value = %s where key = %s", (CORE, INBOUND_OWNER_KEY)
    )
    return put


def would_enqueue(conn: Connection, decision_id: str, kind: str) -> bool:
    """Whether `enqueue` would write a row for this event: the bridge is configured and
    the question is a plain question already announced (or this is its `asked`). A read
    only; the counter is not touched. "Announced" is `decision.partner_version > 0`,
    which is exact because every sender, core's or a plug-in's, takes its versions from
    `next_version`."""
    if not enabled():
        return False
    return (
        conn.execute(
            "select 1 from decision where id = %s and kind = 'question'"
            " and (partner_version > 0 or %s)",
            (decision_id, kind == "asked"),
        ).fetchone()
        is not None
    )


_OUTBOX_COLS = (
    "event_id",
    "decision_id",
    "version",
    "kind",
    "body",
    "created_at",
    "attempts",
    "next_attempt_at",
    "delivered_at",
    "last_error",
    "last_error_at",
    "parked_at",
)


@dataclass(frozen=True)
class Handover:
    """What `hand_over` moved: `rows` (each a dict of `_OUTBOX_COLS`, `body` a dict),
    and `held`, every question core's outbox still has an undelivered row for after
    the move. The caller must send none of a held question's rows yet, or a later
    version could reach the partner before an earlier one."""

    rows: list[dict]
    held: set[str]


def _body(value) -> dict:
    return json.loads(value) if isinstance(value, str | bytes) else value


def _lock_rows(conn: Connection, picked: list[str], now: datetime) -> list[dict]:
    """The undelivered, unleased rows of `picked`, locked for the rest of the caller's
    transaction (Postgres: `for update skip locked`; SQLite's writer lock already holds
    everything). The lease is checked here, under the lock, not only at the pick."""
    found = conn.execute(
        f"select {', '.join(_OUTBOX_COLS)} from partner_outbox"  # noqa: S608 -- fixed columns, placeholders only
        f" where delivered_at is null and decision_id in {dbmod.in_list(picked)}"
        " and (claimed_until is null or claimed_until <= %s)"
        " order by created_at, version" + conn.for_update(" skip locked"),
        (*picked, now),
    ).fetchall()
    return [dict(zip(_OUTBOX_COLS, r, strict=True)) for r in found]


def hand_over(conn: Connection, now: datetime, limit: int = 200) -> Handover:
    """Move undelivered rows out of core's outbox, in the caller's open transaction:
    the rows are returned and deleted, so the caller inserts them into its own outbox
    in the same transaction and a row is in exactly one outbox, never both and never
    neither (a rollback keeps them here). Whole questions only, oldest first, at most
    `limit` questions; a question with a row under a live lease (a sender elsewhere
    is attempting it now) is skipped whole and comes back in `held`. Parked rows move
    parked. Delivered rows stay (the listening state still reads them). Postgres locks
    the rows it takes, skipping ones another hand-over holds; SQLite's writer lock
    already serialises. Called repeatedly by the plug-in's sender: core's inbound half
    still writes the partner-home answers here until 9e-4."""
    free = (
        " where o.delivered_at is null and not exists (select 1 from partner_outbox p"
        "   where p.decision_id = o.decision_id and p.delivered_at is null"
        "   and p.claimed_until is not null and p.claimed_until > %s)"
    )
    picked = [
        r[0]
        for r in conn.execute(
            "select o.decision_id from partner_outbox o"  # noqa: S608 -- fixed text
            + free
            + " group by o.decision_id order by min(o.created_at), o.decision_id"
            " limit %s",
            (now, limit),
        ).fetchall()
    ]
    rows: list[dict] = []
    if picked:
        locked = _lock_rows(conn, picked, now)
        # Whole questions only: a question whose undelivered rows were not all locked
        # here (Postgres: another transaction holds one, or a sender leased one since the
        # pick) is given back untouched and stays in `held`.
        wanted: dict[str, int] = dict(
            conn.execute(
                "select decision_id, count(*) from partner_outbox"  # noqa: S608 -- placeholders only
                f" where delivered_at is null and decision_id in {dbmod.in_list(picked)}"
                " group by decision_id",
                tuple(picked),
            ).fetchall()
        )
        got: dict[str, int] = {}
        for row in locked:
            got[row["decision_id"]] = got.get(row["decision_id"], 0) + 1
        rows = [
            r for r in locked if got[r["decision_id"]] == wanted.get(r["decision_id"])
        ]
        for row in rows:
            row["body"] = _body(row["body"])
            conn.execute(
                "delete from partner_outbox where event_id = %s", (row["event_id"],)
            )
    held = {
        r[0]
        for r in conn.execute(
            "select distinct decision_id from partner_outbox where delivered_at is null"
        ).fetchall()
    }
    return Handover(rows=rows, held=held)


def take_back(conn: Connection, rows: list[dict]) -> int:
    """The undo of `hand_over`: put rows (the same dicts) back into core's outbox, in
    the caller's transaction. A row whose event id or (question, version) is already
    here is skipped. Returns how many went in. Leases are not restored."""
    put = 0
    for row in rows:
        put += conn.execute(
            f"insert into partner_outbox ({', '.join(_OUTBOX_COLS)})"  # noqa: S608 -- fixed columns
            f" values ({', '.join(['%s'] * len(_OUTBOX_COLS))})"
            " on conflict do nothing",
            tuple(
                dbmod.jsonb(_body(row[c])) if c == "body" else row.get(c)
                for c in _OUTBOX_COLS
            ),
        ).rowcount
    return put


def prune(db: Database, now: datetime | None = None) -> int:
    """Forget delivered events older than KEEP_DELIVERED."""
    cutoff = (now or datetime.now(UTC)) - KEEP_DELIVERED
    with db.connect() as conn:
        # The inbound side keeps event ids and settled copies as long.
        conn.execute("delete from partner_event_seen where seen_at < %s", (cutoff,))
        conn.execute(
            "delete from partner_question where state <> 'open' and updated_at < %s",
            (cutoff,),
        )
        return conn.execute(
            "delete from partner_outbox where delivered_at is not null"
            " and delivered_at < %s",
            (cutoff,),
        ).rowcount


def status(db: Database, now: datetime | None = None) -> dict:
    """Outbox depth, the age of the oldest undelivered event, parked rows and the
    most recent error of a row still waiting. Counts, codes and a short reason;
    never a body, never a key."""
    now = now or datetime.now(UTC)

    def age(then: datetime | None) -> int | None:
        return None if then is None else max(0, int((now - then).total_seconds()))

    with db.connect() as conn:
        (depth,) = conn.execute(
            "select count(*) from partner_outbox where delivered_at is null"
        ).fetchone()
        # Plain columns, not min(): SQLite hands an aggregate back as text.
        first = conn.execute(
            "select created_at from partner_outbox where delivered_at is null"
            " order by created_at limit 1"
        ).fetchone()
        parked = conn.execute(
            "select parked_at, last_error from partner_outbox"
            " where parked_at is not null and delivered_at is null"
            " order by parked_at"
        ).fetchall()
        (auth,) = conn.execute(
            "select count(*) from partner_outbox where delivered_at is null"
            " and parked_at is null and last_error in (%s, %s)",
            _AUTH_ERRORS,
        ).fetchone()
        error = conn.execute(
            "select last_error, last_error_at from partner_outbox"
            " where last_error is not null and delivered_at is null"
            " order by last_error_at desc limit 1"
        ).fetchone()
        owner = bridge_owner(conn)
    return {
        "enabled": enabled(),
        # Step 9e-3: who holds the bridge, and whether its stage is loaded here.
        "bridge_owner": owner,
        "bridge_owner_loaded": owner is not None and _owner_here(owner),
        "depth": depth,
        "oldest_undelivered_age_seconds": age(None if first is None else first[0]),
        "parked": len(parked),
        "oldest_parked_age_seconds": age(parked[0][0] if parked else None),
        "parked_codes": sorted({r[1] for r in parked}),
        "auth_errors": auth,
        "last_error": None if error is None else error[0],
        "last_error_age_seconds": age(None if error is None else error[1]),
    }


_sender_stood_down_logged = False


def sender_pass(
    db: Database,
    config: Config,
    post: PostFn | None = None,
    *,
    prune_due: bool = False,
) -> tuple[int, int] | None:
    """One pass of the sender: deliver what is due, and prune when `prune_due`. Step
    9e-3: once a plug-in holds the bridge (`claim_bridge`, a database fact, so every
    process sees it whatever plug-ins it loaded), the plug-in sends instead (its sender
    takes this outbox's rows with `hand_over`), so this delivers nothing, logs that once
    at INFO and returns None; pruning goes on, since the inbound tables are still
    core's. Read each pass, and again per row inside `_claim`, so a claim landing
    mid-pass stops it before the next row."""
    global _sender_stood_down_logged
    result: tuple[int, int] | None = None
    with db.connect() as conn:
        holder = bridge_owner(conn)
    if holder is not None:
        if not _sender_stood_down_logged:
            _sender_stood_down_logged = True
            _log.info("core partner sender stands down: a plug-in holds the bridge")
        if not _owner_here(holder):
            note_owner_absent(holder)
    else:
        result = (
            deliver_due(db, config) if post is None else deliver_due(db, config, post)
        )
    if prune_due:
        prune(db)
    return result


def start(db: Database, config: Config | None) -> threading.Thread | None:
    """The sender: one daemon thread, started only when configured. It polls
    every POLL_SECONDS, never dies of an exception and never blocks start-up.
    `stop` ends it; the process ending does too (it is a daemon, and an
    undelivered row is simply still in the outbox next time)."""
    if config is None:
        return None
    if config.url.startswith("http://"):
        _log.warning("partner bridge: plain HTTP is in use (%s=1)", ENV_ALLOW_HTTP)
    _stop.clear()

    def loop() -> None:
        last_prune = datetime.min.replace(tzinfo=UTC)
        while not _stop.is_set():
            try:
                now = datetime.now(UTC)
                due = now - last_prune > _PRUNE_EVERY
                sender_pass(db, config, prune_due=due)
                if due:
                    last_prune = now
            except Exception:  # noqa: BLE001 -- the loop must outlive one bad pass
                _log.error("partner delivery pass failed", exc_info=True)
            _stop.wait(POLL_SECONDS)

    thread = threading.Thread(target=loop, name="partner-sender", daemon=True)
    thread.start()
    return thread


_stop = threading.Event()


def stop() -> None:
    _stop.set()
