"""The inbound half of the partner bridge (runcove-5t3oj.39.4, second half).

The partner (quarterdeck) tells artifacts what happens to questions in Slack:

  * a question whose home is ARTIFACTS (`source: "artifacts:<decision id>"`) was
    answered by a press on its Slack copy. Quarterdeck checked that the person
    holds approve on the asking office and says so (`verified_by:
    "quarterdeck"`); artifacts trusts that verified identity (Jeremy, 9 Oct
    2026) and settles the page question as `answer_source = 'slack'`.
  * a question whose home is QUARTERDECK (`source: "qd:<id>"`, asked in Slack)
    is stored as a copy (`partner_question`) that appears in his inbox. When he
    answers it here the answer is PENDING until the home's own `answered` comes
    back: artifacts never settles a question it is not the home of.

THE ENDPOINT (`create_partner_app`) is a separate small app, served only on the
host named by `ARTIFACTS_PARTNER_INBOUND_HOST` (the service's in-cluster name):
see `app.create_app`. It authenticates by signature, not by agent key.

VERIFICATION runs cheapest first, each step with its own status, on the RAW
request bytes (never re-serialised JSON):

    size > 64 KiB ................ 400    (413 would park the event: it cannot
                                            be retried into a smaller one anyway,
                                            but 400 is what the contract says)
    not JSON, or not an object ... 400
    a field missing or mistyped .. 422
    key_id not known ............. 401
    signature does not verify .... 401
    sent_at not within -60 s .. +5 min ... 401
    already applied / not newer .. 2xx (nothing changes)
    applied ...................... 2xx

Nothing of the body, the signature or any key reaches a response or a log: a
response is `{"error": "<fixed word>"}` or `{"result": "<fixed word>"}`; a log
line names a reason and, for a 422, the NAME of the field that failed (a fixed
string from our code, never its value), and, once the signature has verified, the
event id.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from artifacts import db as dbmod
from artifacts import decisions, hosts, partner, settings, store
from artifacts.db import Connection, Database

_log = logging.getLogger("artifacts.partner.inbound")

ENV_KEYS_FILE = "ARTIFACTS_PARTNER_INBOUND_KEYS_FILE"
ENV_HOST = "ARTIFACTS_PARTNER_INBOUND_HOST"
_ENV_ALL = (ENV_KEYS_FILE, ENV_HOST)

PATH = "/partner/v1/events"
MAX_BODY = 64 * 1024
MAX_AGE = timedelta(minutes=5)
MAX_FUTURE = timedelta(seconds=60)

# A request that came through a proxy or an ingress says so in these headers; a
# call straight to the service name carries none of them. The route is refused
# (404) when any is present, so an ingress added by mistake fails closed.
_PROXY_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "forwarded",
    "x-real-ip",
    "cf-connecting-ip",
    "cf-ray",
    "true-client-ip",
)

# What the partner may SEND us. `set_aside` and `reopened` run only outbound (2 Nov 2026):
# artifacts is the one that knows when a page is archived.
KINDS = tuple(k for k in partner.KINDS if k not in ("set_aside", "reopened"))
_ID = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_EVENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
_SOURCE = re.compile(r"(artifacts|qd):([A-Za-z0-9_.-]{1,100})")
_LETTER = re.compile(r"[A-Za-z0-9]{1,3}")
URGENCIES = ("blocking", "by", "when_you_can")


class PartnerInboundConfigError(Exception):
    """The inbound settings are half there, or a key cannot be used. The message
    names variables, never a value."""


@dataclass(frozen=True)
class Inbound:
    host: str
    keys: dict  # key_id -> Ed25519PublicKey; never printed

    def __repr__(self) -> str:
        return f"Inbound(host={self.host!r}, key_ids={sorted(self.keys)!r})"


def config_from_env(outbound: partner.Config | None) -> Inbound | None:
    """None when off (neither variable set). Raises when only one is set, when
    the outbound bridge is not configured too (one partner: the answers we send
    back go to the same place), when the host would also be the gallery, the
    API host or a page origin, or when the key file is unreadable or holds no
    usable Ed25519 public key. Nothing from the file reaches a message."""
    cfg = settings.current()
    given = {
        ENV_KEYS_FILE: cfg.partner_inbound_keys_file,
        ENV_HOST: cfg.partner_inbound_host,
    }
    present = [n for n in _ENV_ALL if given[n]]
    if not present:
        return None
    missing = [n for n in _ENV_ALL if n not in present]
    if missing:
        raise PartnerInboundConfigError(
            "the inbound partner bridge is half-configured: set both of "
            + ", ".join(_ENV_ALL)
            + " or neither; missing "
            + ", ".join(missing)
        )
    if outbound is None:
        raise PartnerInboundConfigError(
            "the inbound partner bridge needs the outbound one: set "
            + ", ".join(partner._ENV_ALL)
        )
    host = hosts.normalise(cfg.partner_inbound_host)
    if host is None:
        raise PartnerInboundConfigError(f"{ENV_HOST} is not a host name")
    if (
        hosts.is_gallery(host)
        or hosts.is_api(host)
        or hosts.artifact_id(host) is not None
        or host.endswith("." + store.content_host())
    ):
        raise PartnerInboundConfigError(
            f"{ENV_HOST} must be a name of its own, not the gallery, the API "
            "host or a page origin"
        )
    path = cfg.partner_inbound_keys_file
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise PartnerInboundConfigError(
            f"{ENV_KEYS_FILE} cannot be read ({type(exc).__name__})"
        ) from None
    return Inbound(host=host, keys=load_public_keys(raw))


def load_public_keys(raw: bytes) -> dict:
    """A JSON object mapping key_id to a public key, each either PEM text
    (SubjectPublicKeyInfo) or the 32 raw bytes in base64. Several ids are
    accepted at once, for a rotation."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    try:
        table = json.loads(raw)
    except ValueError:
        raise PartnerInboundConfigError(
            f"{ENV_KEYS_FILE} is not a JSON object of key id to public key"
        ) from None
    if not isinstance(table, dict) or not table:
        raise PartnerInboundConfigError(
            f"{ENV_KEYS_FILE} is not a JSON object of key id to public key"
        )
    keys = {}
    for key_id, text in table.items():
        try:
            if not isinstance(text, str) or not _ID.fullmatch(key_id):
                raise ValueError
            if "BEGIN" in text:
                key = load_pem_public_key(text.encode())
            else:
                key = Ed25519PublicKey.from_public_bytes(
                    base64.b64decode(text, validate=True)
                )
            if not isinstance(key, Ed25519PublicKey):
                raise TypeError
        except Exception:  # noqa: BLE001 -- the library's message can quote the input
            raise PartnerInboundConfigError(
                f"{ENV_KEYS_FILE} holds an entry that is not an Ed25519 public key"
            ) from None
        keys[key_id] = key
    return keys


# --- the request: parse, then authenticate ----------------------------------------


class _Refused(Exception):
    """A request refused. `field` names the input that failed (a fixed string
    from our code, never a value); it goes in the log, not in the response."""

    def __init__(self, status: int, word: str, field: str | None = None) -> None:
        self.status = status
        self.word = word
        self.field = field


@dataclass(frozen=True)
class Event:
    event_id: str
    kind: str
    source: str
    home: str  # 'artifacts' | 'qd'
    question: str
    version: int
    key_id: str
    at: datetime
    sent_at: datetime
    body: dict


def _text(value, limit: int, field: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise _Refused(422, "malformed", field)
    return value[:limit]


def _moment(value, field: str, *, required: bool = False) -> datetime | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise _Refused(422, "malformed", field)
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        raise _Refused(422, "malformed", field) from None
    if when.tzinfo is None:
        raise _Refused(422, "malformed", field)
    return when.astimezone(UTC)


def parse(raw: bytes) -> Event:
    """The request body as an `Event`; raises `_Refused(400)` for a body that is
    not a JSON object and `_Refused(422)` for one with a field missing or of the
    wrong type. Extra fields are ignored."""
    if len(raw) > MAX_BODY:
        raise _Refused(400, "too_large")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        # Not JSON, or nested so deeply the parser gives up: both malformed.
        raise _Refused(400, "malformed") from None
    if not isinstance(data, dict):
        raise _Refused(400, "malformed")
    event_id = data.get("event_id")
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
        raise _Refused(422, "malformed", "event_id")
    kind = data.get("kind")
    if kind not in KINDS:
        raise _Refused(422, "malformed", "kind")
    source = data.get("source")
    match = _SOURCE.fullmatch(source) if isinstance(source, str) else None
    if match is None:
        raise _Refused(422, "malformed", "source")
    question = data.get("question")
    if question != match.group(2):
        raise _Refused(422, "malformed", "question")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise _Refused(422, "malformed", "version")
    key_id = data.get("key_id")
    if not isinstance(key_id, str) or not _ID.fullmatch(key_id):
        raise _Refused(422, "malformed", "key_id")
    body = data.get("body")
    if not isinstance(body, dict):
        raise _Refused(422, "malformed", "body")
    return Event(
        event_id=event_id,
        kind=kind,
        source=source,
        home=match.group(1),
        question=question,
        version=version,
        key_id=key_id,
        at=_moment(data.get("at"), "at", required=True),
        sent_at=_moment(data.get("sent_at"), "sent_at", required=True),
        body=body,
    )


def authenticate(inbound: Inbound, event: Event, raw: bytes, signature, now) -> None:
    """key_id known, then the signature over the raw bytes, then freshness. All
    three are a 401, and a log line says which, so a rotation or a clock drift is
    diagnosable without a body in the log."""
    from cryptography.exceptions import InvalidSignature

    key = inbound.keys.get(event.key_id)
    if key is None:
        _log.warning("refused: unknown key id")
        raise _Refused(401, "unauthorized")
    try:
        sig = base64.b64decode(signature or "", validate=True)
        key.verify(sig, raw)
    except (InvalidSignature, binascii.Error, ValueError):
        _log.warning("refused: bad signature")
        raise _Refused(401, "unauthorized") from None
    age = now - event.sent_at
    if age > MAX_AGE or age < -MAX_FUTURE:
        _log.warning("refused: sent_at outside the allowed window")
        raise _Refused(401, "unauthorized")


def process(
    db: Database,
    inbound: Inbound,
    raw: bytes,
    signature: str | None,
    now: datetime | None = None,
) -> tuple[int, dict]:
    """One request to one status and one fixed-word body."""
    now = now or datetime.now(UTC)
    try:
        try:
            event = parse(raw)
        except _Refused as exc:
            if exc.status == 422:
                # Before the signature: no event id (the body is not trusted yet).
                _log.warning("refused: malformed %s", exc.field or "?")
            raise
        authenticate(inbound, event, raw, signature, now)
        try:
            result = apply(db, event, now)
        except _Refused as exc:
            # Authenticated and malformed: now the id may be named.
            if exc.status == 422:
                _log.warning(
                    "event %s refused: %s %s",
                    event.event_id,
                    exc.word,
                    exc.field or "?",
                )
            else:
                _log.warning("event %s refused: %s", event.event_id, exc.word)
            raise
        _log.info("event %s %s", event.event_id, result)
        return 200, {"result": result}
    except _Refused as exc:
        return exc.status, {"error": exc.word}
    except dbmod.DataError:
        # A string no column can hold (a NUL, a lone surrogate): malformed.
        _log.warning("refused: malformed (a value no column can hold)")
        return 422, {"error": "malformed"}
    except Exception as exc:  # noqa: BLE001 -- a 500 is retried by the sender; never echo
        _log.error("inbound event failed: %s", type(exc).__name__)
        return 500, {"error": "failed"}


# --- applying ----------------------------------------------------------------------


class _NotApplied(Exception):
    def __init__(self, result: str) -> None:
        self.result = result


def _mark(
    conn: Connection,
    event: Event,
    outcome: str,
    now: datetime,
    detail: dict | None = None,
    *,
    advance: bool = True,
) -> str | None:
    """Write the dedupe records, in the caller's open transaction. None when the
    event is new and newer; 'duplicate' (this event id was seen) or 'stale' (a
    version not newer than the last applied for this question) otherwise. The
    conflict clauses make both tests atomic, so two requests racing on one
    event cannot both pass."""
    inserted = conn.execute(
        "insert into partner_event_seen"
        " (event_id, source, version, kind, outcome, detail, seen_at)"
        " values (%s, %s, %s, %s, %s, %s, %s) on conflict (event_id) do nothing",
        (
            event.event_id,
            event.source,
            event.version,
            event.kind,
            outcome,
            dbmod.jsonb(detail) if detail is not None else None,
            now,
        ),
    )
    if inserted.rowcount == 0:
        return "duplicate"
    if advance:
        moved = conn.execute(
            "insert into partner_version_seen (source, version, updated_at)"
            " values (%s, %s, %s) on conflict (source) do update"
            " set version = excluded.version, updated_at = excluded.updated_at"
            " where partner_version_seen.version < excluded.version",
            (event.source, event.version, now),
        )
        if moved.rowcount == 0:
            conn.execute(
                "update partner_event_seen set outcome = 'stale' where event_id = %s",
                (event.event_id,),
            )
            return "stale"
    return None


def _record_only(
    db: Database,
    event: Event,
    outcome: str,
    now: datetime,
    *,
    advance: bool = True,
    detail: dict | None = None,
) -> str:
    """The event changes nothing: say so, once. Returns what to tell the sender."""
    with db.connect() as conn, conn.transaction():
        again = _mark(conn, event, outcome, now, detail, advance=advance)
    return again or "ignored"


def apply(db: Database, event: Event, now: datetime) -> str:
    """Apply one authenticated event. Returns 'applied', 'duplicate', 'stale' or
    'ignored' (understood and deliberately not acted on)."""
    with db.connect() as conn:
        if conn.execute(
            "select 1 from partner_event_seen where event_id = %s", (event.event_id,)
        ).fetchone():
            return "duplicate"
        last = conn.execute(
            "select version from partner_version_seen where source = %s",
            (event.source,),
        ).fetchone()
    if last is not None and event.version <= last[0]:
        _record_only(db, event, "stale", now, advance=False)
        return "stale"
    if event.home == "artifacts":
        return _apply_artifacts(db, event, now)
    return _apply_foreign(db, event, now)


# --- a question whose home is artifacts ---------------------------------------------


def _answer_fields(body: dict) -> tuple[str, str, dict]:
    """(letter, note, audit) from an `answered` body. The audit holds who
    quarterdeck says pressed and what it says it verified; it is stored on the
    event record and never shown, logged or sent on."""
    letter = body.get("answer")
    if not isinstance(letter, str) or not _LETTER.fullmatch(letter):
        raise _Refused(422, "malformed", "body.answer")
    note = body.get("note", "")
    if not isinstance(note, str):
        raise _Refused(422, "malformed", "body.note")
    who = body.get("answered_by")
    if not isinstance(who, dict) or who.get("verified_by") != "quarterdeck":
        raise _Refused(422, "malformed", "body.answered_by")
    name = who.get("name")
    slack_id = who.get("slack_id", who.get("slack_user_id"))
    if not isinstance(name, str) or not name.strip():
        raise _Refused(422, "malformed", "body.answered_by.name")
    if slack_id is not None and not (
        isinstance(slack_id, str) and _ID.fullmatch(slack_id)
    ):
        raise _Refused(422, "malformed", "body.answered_by.slack_id")
    return (
        letter,
        note,
        {"name": name[:200], "slack_id": slack_id, "verified_by": "quarterdeck"},
    )


def _apply_artifacts(db: Database, event: Event, now: datetime) -> str:
    """Only `answered` means anything for a question artifacts is the home of:
    the other events describe what artifacts itself told the partner."""
    if event.kind != "answered":
        return _record_only(db, event, "ignored_kind", now)
    letter, note, audit = _answer_fields(event.body)
    with db.connect() as conn:
        row = conn.execute(
            "select d.kind, d.closed_at, d.partner_version, d.answer, d.answer_note,"
            " (a.archived_at is not null or a.deleted_at is not null)"
            " from decision d join artifact a on a.id = d.artifact_id"
            " where d.id = %s",
            (event.question,),
        ).fetchone()
    # An id that is not ours, not a question, closed, SET ASIDE (its page is archived
    # or binned, answered or not: he cannot answer it from Slack either) or never announced to the
    # partner: indistinguishable from "no such decision", and not recorded as a
    # version (the partner has no business advancing a counter on it).
    if (
        row is None
        or row[0] != "question"
        or row[1] is not None
        or row[2] < 1
        or row[5]
    ):
        return _record_only(db, event, "unknown", now, advance=False)
    if row[3] == letter and row[4] == note:
        return _record_only(db, event, "unchanged", now)

    def hook(conn: Connection) -> None:
        again = _mark(conn, event, "applied", now, audit)
        if again is not None:
            raise _NotApplied(again)

    try:
        decisions.answer(
            db,
            event.question,
            letter=letter,
            note=note,
            by=settings.owner_id(),
            source="slack",
            recorded_by="quarterdeck",
            in_transaction=hook,
        )
    except _NotApplied as exc:
        return exc.result
    except decisions.Held:
        # A held question waits on an earlier one he has not answered. A press
        # on its Slack copy is refused, and the refusal is recorded; the
        # partner is told 2xx so it stops retrying a press that can never apply.
        return _record_only(db, event, "held", now)
    except (
        decisions.InvalidAnswer,
        decisions.AlreadyDecided,
        store.NotFound,
        store.BodyTooLarge,
        store.InvalidText,
        store.SecretFound,
    ):
        return _record_only(db, event, "refused", now)
    return "applied"


# --- a question whose home is the partner ------------------------------------------

_SETTLED = {"answered": "answered", "withdrawn": "withdrawn", "replaced": "replaced"}


def _options(value, field: str) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= 26:
        raise _Refused(422, "malformed", field)
    out, seen = [], set()
    for option in value:
        if not isinstance(option, dict):
            raise _Refused(422, "malformed", field)
        letter = option.get("letter")
        label = option.get("text", option.get("label"))
        if (
            not isinstance(letter, str)
            or not _LETTER.fullmatch(letter)
            or letter in seen
            or not isinstance(label, str)
            or not label.strip()
        ):
            raise _Refused(422, "malformed", field)
        seen.add(letter)
        out.append({"letter": letter, "label": label[:1000]})
    return out


def _strings(value, field: str, limit: int = 50) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise _Refused(422, "malformed", field)
    if not all(isinstance(v, str) for v in value):
        raise _Refused(422, "malformed", field)
    return [v[:200] for v in value]


def _safe_link(value) -> str | None:
    """The link to the home, shown as an href: http(s) only."""
    if not isinstance(value, str):
        return None
    parts = urlsplit(value.strip())
    if parts.scheme in ("http", "https") and parts.netloc:
        return value.strip()[:2000]
    return None


def _asked_fields(body: dict) -> dict:
    options = _options(body.get("options"), "body.options")
    letters = {o["letter"] for o in options}
    recommended = _text(body.get("recommended"), 3, "body.recommended")
    urgency = body.get("urgency", "when_you_can")
    if urgency not in URGENCIES:
        raise _Refused(422, "malformed", "body.urgency")
    topic = body.get("topic")
    if topic is not None and not (
        isinstance(topic, dict)
        and isinstance(topic.get("id"), str)
        and (topic.get("title") is None or isinstance(topic.get("title"), str))
    ):
        raise _Refused(422, "malformed", "body.topic")
    return {
        "asking_office": _text(
            body.get("asking_office"), 200, "body.asking_office", required=True
        ),
        "title": _text(body.get("title"), 1000, "body.title", required=True),
        "options": options,
        "recommended": recommended if recommended in letters else None,
        "context": _text(body.get("context", ""), 4000, "body.context") or "",
        "link": _safe_link(body.get("link")),
        "topic_id": topic["id"][:200] if topic else None,
        "topic_title": topic["title"][:500] if topic and topic.get("title") else None,
        "depends_on": _strings(body.get("depends_on"), "body.depends_on"),
        "replaces": _text(body.get("replaces"), 200, "body.replaces"),
        "urgency": urgency,
        "blocked_on": _text(body.get("blocked_on"), 500, "body.blocked_on"),
        "needed_by": _moment(body.get("needed_by"), "body.needed_by"),
        "for_offices": _strings(body.get("for"), "body.for", 20),
    }


def _apply_foreign(db: Database, event: Event, now: datetime) -> str:
    fields = _asked_fields(event.body) if event.kind == "asked" else None
    answer = None
    if event.kind == "answered":
        letter = event.body.get("answer")
        who = event.body.get("answered_by")
        note = event.body.get("note", "")
        name = who.get("name") if isinstance(who, dict) else who
        if (
            not isinstance(letter, str)
            or not _LETTER.fullmatch(letter)
            or not isinstance(note, str)
            or not (name is None or isinstance(name, str))
        ):
            bad = (
                "body.answer"
                if not isinstance(letter, str) or not _LETTER.fullmatch(letter)
                else "body.note"
                if not isinstance(note, str)
                else "body.answered_by"
            )
            raise _Refused(422, "malformed", bad)
        answer = (
            letter,
            note[:20000],
            (name or "")[:200],
            _moment(event.body.get("answered_at"), "body.answered_at") or event.at,
        )
    edited = None
    if event.kind == "edited":
        edited = _text(event.body.get("context"), 4000, "body.context", required=True)
    replaced_by = None
    if event.kind == "replaced":
        replaced_by = _text(
            event.body.get("replaced_by_source", event.body.get("replaced_by")),
            200,
            "body.replaced_by",
        )

    with db.connect() as conn, conn.transaction():
        existing = conn.execute(
            "select 1 from partner_question where source = %s", (event.source,)
        ).fetchone()
        if existing is None and event.kind not in ("asked", "message_added"):
            # The `asked` that makes this meaningful has not landed. Nothing is
            # recorded and the answer is retryable: 503, the sender tries again.
            raise _Refused(503, "not_yet")
        if existing is None and event.kind == "message_added":
            again = _mark(conn, event, "unknown", now, advance=False)
            return again or "ignored"
        if event.kind == "message_added":
            again = _mark(conn, event, "ignored_kind", now)
            return again or "ignored"
        again = _mark(conn, event, "applied", now)
        if again is not None:
            return again
        if event.kind == "asked":
            _upsert_copy(conn, event, fields, now, existing is not None)
        elif event.kind == "answered":
            conn.execute(
                "update partner_question set state = 'answered', answer = %s,"
                " answer_note = %s, answered_by = %s, answered_at = %s,"
                " pending_answer = null, pending_note = '', pending_at = null,"
                " version = %s, updated_at = %s where source = %s",
                (*answer, event.version, now, event.source),
            )
        elif event.kind == "edited":
            conn.execute(
                "update partner_question set context = %s, version = %s,"
                " updated_at = %s where source = %s",
                (edited, event.version, now, event.source),
            )
        else:  # withdrawn, replaced
            conn.execute(
                "update partner_question set state = %s, replaced_by = %s,"
                " version = %s, updated_at = %s where source = %s",
                (_SETTLED[event.kind], replaced_by, event.version, now, event.source),
            )
    return "applied"


def _upsert_copy(conn: Connection, event: Event, f: dict, now: datetime, exists: bool):
    if exists:
        conn.execute(
            "update partner_question set version = %s, asking_office = %s,"
            " title = %s, options = %s, recommended = %s, context = %s, link = %s,"
            " topic_id = %s, topic_title = %s, depends_on = %s, replaces = %s,"
            " urgency = %s, blocked_on = %s, needed_by = %s, for_offices = %s,"
            " updated_at = %s where source = %s",
            (
                event.version,
                f["asking_office"],
                f["title"],
                dbmod.jsonb(f["options"]),
                f["recommended"],
                f["context"],
                f["link"],
                f["topic_id"],
                f["topic_title"],
                dbmod.jsonb(f["depends_on"]),
                f["replaces"],
                f["urgency"],
                f["blocked_on"],
                f["needed_by"],
                dbmod.jsonb(f["for_offices"]),
                now,
                event.source,
            ),
        )
        return
    conn.execute(
        "insert into partner_question (source, version, asking_office, title,"
        " options, recommended, context, link, topic_id, topic_title, depends_on,"
        " replaces, urgency, blocked_on, needed_by, for_offices, created_at,"
        " updated_at) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
        " %s, %s, %s, %s, %s, %s)",
        (
            event.source,
            event.version,
            f["asking_office"],
            f["title"],
            dbmod.jsonb(f["options"]),
            f["recommended"],
            f["context"],
            f["link"],
            f["topic_id"],
            f["topic_title"],
            dbmod.jsonb(f["depends_on"]),
            f["replaces"],
            f["urgency"],
            f["blocked_on"],
            f["needed_by"],
            dbmod.jsonb(f["for_offices"]),
            event.at,
            now,
        ),
    )


# --- reading the copies, and answering one here ------------------------------------

_COLS = (
    "source, version, asking_office, title, options, recommended, context, link,"
    " topic_id, topic_title, depends_on, replaces, replaced_by, urgency,"
    " blocked_on, needed_by, for_offices, state, answer, answer_note, answered_by,"
    " answered_at, pending_answer, pending_note, pending_at, created_at"
)


@dataclass(frozen=True)
class Copy:
    """A question whose home is the partner. Carries the attributes
    `inbox.key` reads from a decision (id, created_at, needed_by, urgency,
    topic), so one order serves both."""

    source: str
    version: int
    asking_office: str
    title: str
    options: list
    recommended: str | None
    context: str
    link: str | None
    topic: dict | None
    depends_on: list
    replaces: str | None
    replaced_by: str | None
    urgency: str
    blocked_on: str | None
    needed_by: str | None
    for_offices: list
    state: str
    answer: str | None
    answer_note: str
    answered_by: str | None
    answered_at: str | None
    pending_answer: str | None
    pending_note: str
    pending_at: str | None
    created_at: datetime

    @property
    def id(self) -> str:
        return self.source

    @property
    def qid(self) -> str:
        return self.source.removeprefix(partner.FOREIGN_PREFIX)


def _iso(value) -> str | None:
    """A stored time as the ISO text a decision row carries, so one renderer
    serves both."""
    return value.isoformat() if isinstance(value, datetime) else value


def _copy(r) -> Copy:
    return Copy(
        source=r[0],
        version=r[1],
        asking_office=r[2],
        title=r[3],
        options=r[4],
        recommended=r[5],
        context=r[6],
        link=r[7],
        topic={"id": r[8], "title": r[9]} if r[8] else None,
        depends_on=r[10],
        replaces=r[11],
        replaced_by=r[12],
        urgency=r[13],
        blocked_on=r[14],
        needed_by=_iso(r[15]),
        for_offices=r[16],
        state=r[17],
        answer=r[18],
        answer_note=r[19],
        answered_by=r[20],
        answered_at=_iso(r[21]),
        pending_answer=r[22],
        pending_note=r[23],
        pending_at=_iso(r[24]),
        created_at=r[25],
    )


def open_copies(db: Database, limit: int = 500, *, waiting_on_him: bool = True):
    """Open copies, oldest first. `waiting_on_him` leaves out one he has already
    answered here (pending); False gives only those."""
    cond = "is null" if waiting_on_him else "is not null"
    with db.connect() as conn:
        rows = conn.execute(
            f"select {_COLS} from partner_question"  # noqa: S608 -- fixed text
            f" where state = 'open' and pending_answer {cond}"
            " order by created_at, source limit %s",
            (limit,),
        ).fetchall()
    return [_copy(r) for r in rows]


def settled_copies(db: Database, since: datetime, limit: int = 50):
    """Copies the home has answered since `since`: the final state he sees after
    a pending answer comes back."""
    with db.connect() as conn:
        rows = conn.execute(
            f"select {_COLS} from partner_question"  # noqa: S608 -- fixed text
            " where state = 'answered' and updated_at >= %s"
            " order by updated_at desc limit %s",
            (since, limit),
        ).fetchall()
    return [_copy(r) for r in rows]


def count_waiting(db: Database) -> int:
    with db.connect() as conn:
        return conn.execute(
            "select count(*) from partner_question"
            " where state = 'open' and pending_answer is null"
        ).fetchone()[0]


def answer_here(db: Database, qid: str, *, letter: str, note: str = "") -> Copy:
    """He answers a Slack-home question in artifacts. The answer is PENDING: the
    home settles the question and artifacts only says what he chose. The event
    that carries it goes out with `source: "qd:<id>"` and OUR version counter for
    this copy (`out_version`), which is its own number space: the home's versions
    are the home's, and ours are never compared with them. Raises
    `store.NotFound` for a copy that is not there or not open, and
    `decisions.InvalidAnswer` for a letter that is not an option."""
    if not _ID.fullmatch(qid):
        raise store.NotFound()
    source = partner.foreign_source(qid)
    store._check_size(
        note,
        decisions.MAX_NOTE_BYTES,
        "note",
        agent=settings.owner_id(),
        artifact_id=None,
    )
    store._check_secrets(note, "note", agent=settings.owner_id(), artifact_id=None)
    now = datetime.now(UTC)
    with db.connect() as conn, conn.transaction():
        row = conn.execute(
            "select options from partner_question where source = %s and state = 'open'",
            (source,),
        ).fetchone()
        if row is None:
            raise store.NotFound()
        if letter not in {o["letter"] for o in row[0]}:
            raise decisions.InvalidAnswer()
        bumped = conn.execute(
            "update partner_question set pending_answer = %s, pending_note = %s,"
            " pending_at = %s, out_version = out_version + 1, updated_at = %s"
            " where source = %s and state = 'open' returning out_version",
            (letter, note, now, now, source),
        ).fetchone()
        if bumped is None:
            raise store.NotFound()
        partner.enqueue_foreign(
            conn,
            source,
            bumped[0],
            "answered",
            partner.answered_body(
                letter=letter,
                note=note,
                answered_by=settings.owner_id(),
                answered_at=now,
                answer_source="page",
            ),
            at=now,
        )
        fresh = conn.execute(
            f"select {_COLS} from partner_question where source = %s",  # noqa: S608 -- fixed text
            (source,),
        ).fetchone()
    return _copy(fresh)


# --- the endpoint ------------------------------------------------------------------


def create_partner_app(db: Database, inbound: Inbound) -> FastAPI:
    """The one route, on the in-cluster host only (`app.create_app` routes to it
    by Host). No agent key: the signature is the credential."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.post(PATH)
    async def receive(request: Request) -> JSONResponse:
        # Came through a proxy or an ingress: this route is not for that path.
        if any(h in request.headers for h in _PROXY_HEADERS):
            _log.warning("refused: request arrived through a proxy")
            return JSONResponse({"error": "not found"}, status_code=404)
        declared = request.headers.get("content-length")
        if declared is not None and (
            not declared.isdigit() or int(declared) > MAX_BODY
        ):
            return JSONResponse({"error": "too_large"}, status_code=400)
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_BODY:
                return JSONResponse({"error": "too_large"}, status_code=400)
        status, payload = await run_in_threadpool(
            process, db, inbound, raw, request.headers.get(partner.SIGNATURE_HEADER)
        )
        headers = {"Retry-After": "30"} if status == 503 else None
        return JSONResponse(payload, status_code=status, headers=headers)

    return app
