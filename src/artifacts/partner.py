"""The outbound half of the partner bridge (runcove-5t3oj.39.4).

A question has one home. When artifacts is the home, the partner service
(quarterdeck, on the homelab) holds a copy that says `source: "artifacts:<id>"`
and is told what happens to the question: asked, answered, withdrawn, replaced.
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
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from artifacts import db as dbmod
from artifacts import ids

if TYPE_CHECKING:
    from artifacts.db import Connection, Database

_log = logging.getLogger("artifacts.partner")

# The one place the header's name is written.
SIGNATURE_HEADER = "X-Partner-Signature"
# The signing key's id, also inside the signed body: the receiver picks the key from
# this header and verifies the raw bytes before parsing them, then refuses a body
# whose key_id differs.
KEY_ID_HEADER = "X-Partner-Key-Id"

ENV_NAME = "ARTIFACTS_PARTNER_NAME"
ENV_URL = "ARTIFACTS_PARTNER_EVENTS_URL"
ENV_KEY_FILE = "ARTIFACTS_PARTNER_SIGNING_KEY_FILE"
ENV_KEY_ID = "ARTIFACTS_PARTNER_KEY_ID"
_ENV_ALL = (ENV_NAME, ENV_URL, ENV_KEY_FILE, ENV_KEY_ID)
# Optional, outside the all-or-none set: plain http only when asked for by name.
ENV_ALLOW_HTTP = "ARTIFACTS_PARTNER_ALLOW_HTTP"

KINDS = ("asked", "answered", "withdrawn", "replaced", "message_added", "edited")

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
    return all(os.environ.get(name, "").strip() for name in _ENV_ALL)


def allow_http() -> bool:
    return os.environ.get(ENV_ALLOW_HTTP, "").strip() == "1"


def config_from_env() -> Config | None:
    """None when the feature is off (no variable set). Raises
    `PartnerConfigError` when some but not all are set, when the URL is not
    http(s), or when the key file is missing or is not an Ed25519 private key in
    PEM PKCS8. Nothing from the key file reaches a message."""
    present = [n for n in _ENV_ALL if os.environ.get(n, "").strip()]
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
    url = os.environ[ENV_URL].strip()
    plain_ok = url.startswith("http://") and allow_http()
    if not url.startswith("https://") and not plain_ok:
        raise PartnerConfigError(
            f"{ENV_URL} must start with https:// (plain http:// only with "
            f"{ENV_ALLOW_HTTP}=1)"
        )
    path = os.environ[ENV_KEY_FILE].strip()
    try:
        with open(path, "rb") as handle:
            pem = handle.read()
    except OSError as exc:
        raise PartnerConfigError(
            f"{ENV_KEY_FILE} cannot be read ({type(exc).__name__})"
        ) from None
    return Config(
        name=os.environ[ENV_NAME].strip(),
        url=url,
        key_id=os.environ[ENV_KEY_ID].strip(),
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
    row = conn.execute(
        "update decision set partner_version = partner_version + 1"
        " where id = %s and kind = 'question'"
        " and (partner_version > 0 or %s)"
        " returning partner_version",
        (decision_id, kind == "asked"),
    ).fetchone()
    if row is None:
        return None
    version = row[0]
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
    name = {"jeremy": "Jeremy"}.get(answered_by, answered_by)
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
    from artifacts import store

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
    one process and `begin immediate` already serialises writers."""
    due = (
        " where o.delivered_at is null and o.parked_at is null"
        " and o.next_attempt_at <= %s"
        " and (o.claimed_until is null or o.claimed_until <= %s)"
        " and not exists (select 1 from partner_outbox p"
        "   where p.decision_id = o.decision_id and p.version < o.version"
        "   and p.delivered_at is null)"
    )
    with db.connect() as conn:
        waiting = conn.execute(
            "select 1 from partner_outbox o" + due + " limit 1",  # noqa: S608 -- fixed text
            (now, now),
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
            (now, now, limit),
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
    return {
        "enabled": enabled(),
        "depth": depth,
        "oldest_undelivered_age_seconds": age(None if first is None else first[0]),
        "parked": len(parked),
        "oldest_parked_age_seconds": age(parked[0][0] if parked else None),
        "parked_codes": sorted({r[1] for r in parked}),
        "auth_errors": auth,
        "last_error": None if error is None else error[0],
        "last_error_age_seconds": age(None if error is None else error[1]),
    }


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
                deliver_due(db, config)
                now = datetime.now(UTC)
                if now - last_prune > _PRUNE_EVERY:
                    prune(db, now)
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
