"""Answering a form (docs/agent/specs/2026-10-29-form-controls-design.md, 3.3-3.4).

The gallery posts one batch of values for a form; `validate` checks every one against the
field list STORED at publish (never the page's own labels or controls) and `submit` records
the answer on the form's decision, writes the `decision_answered` event in the same
transaction, and nudges the agent.

PURE FIRST. `validate` touches no database and is what the tests pin. `submit` is the one
write.

WHAT A SUBMIT KEEPS. The decision's `answer_record` is the merged record the agent reads:
the later non-blank value wins per field, a blank never erases an earlier answer (a second
sitting that leaves a field alone leaves it alone), and every submit is kept in `history`.
After a republish the record is reconciled with the new field list (`formrecord`, run by
`forms.sync`): an unchanged question keeps his answer, a changed or removed one moves to
`stale`. `relay` stores an answer he gave elsewhere, per field, with its source.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import UTC, date, datetime

from ream import (
    decisions,
    events,
    formrecord,
    forms,
    ids,
    receipts,
    settings,
    store,
)
from ream.db import jsonb

MAX_HISTORY = 50
DEFAULT_TEXT = 2000
MAX_FIELDS_SENT = 200

_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class SubmitRefused(Exception):
    """A batch that cannot be stored. `field` names the control at fault; `status`
    is the HTTP status the gallery answers with (422 a bad value, 409 a form that
    changed under him)."""

    def __init__(self, detail: str, *, field: str | None = None, status: int = 422):
        super().__init__(detail)
        self.detail = detail
        self.field = field
        self.status = status


_blank_of = formrecord.blank_of


def _clean_text(value, name: str, *, multiline: bool) -> str:
    if not isinstance(value, str):
        raise SubmitRefused("a text value is expected", field=name)
    for ch in value:
        if unicodedata.category(ch) in ("Cc", "Cs", "Co") and not (
            ch in "\t" or (multiline and ch in "\r\n")
        ):
            raise SubmitRefused("the value has a control character", field=name)
    return value


def _one(spec: dict, value):
    """(stored value, is_blank) for one field, or SubmitRefused."""
    name, typ = spec["name"], spec["type"]
    options = [o["value"] for o in spec.get("options") or []]
    if typ == "checkbox" and not options:
        if value is None:
            return False, False
        if not isinstance(value, bool):
            raise SubmitRefused("a checkbox is true or false", field=name)
        return value, False
    if typ == "checkbox":
        if value is None:
            return [], True
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SubmitRefused("a checkbox group is a list of values", field=name)
        if len(set(value)) != len(value):
            raise SubmitRefused("a value is chosen twice", field=name)
        bad = [v for v in value if v not in options]
        if bad:
            raise SubmitRefused(
                f"{bad[0][:40]!r} is not one of this field's options", field=name
            )
        # Kept in the order the page lists them, whatever order they arrived in.
        ordered = [v for v in options if v in value]
        return ordered, not ordered
    if value is None or value == "":
        return "", True
    if typ in ("radio", "select"):
        if not isinstance(value, str) or value not in options:
            raise SubmitRefused("not one of this field's options", field=name)
        return value, False
    if typ in ("text", "textarea"):
        text = _clean_text(value, name, multiline=typ == "textarea")
        cap = spec.get("maxlength") or DEFAULT_TEXT
        if len(text) > cap:
            raise SubmitRefused(f"longer than {cap} characters", field=name)
        if not text.strip():
            return "", True
        return text, False
    if typ == "number":
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            raise SubmitRefused("a number is expected", field=name)
        try:
            n = float(value.strip() if isinstance(value, str) else value)
        except (ValueError, OverflowError):
            raise SubmitRefused("not a number", field=name) from None
        if not math.isfinite(n):
            raise SubmitRefused("not a finite number", field=name)
        lo, hi, step = spec.get("min"), spec.get("max"), spec.get("step")
        if lo is not None and n < lo:
            raise SubmitRefused(f"below the minimum of {lo}", field=name)
        if hi is not None and n > hi:
            raise SubmitRefused(f"above the maximum of {hi}", field=name)
        if step:
            ratio = (n - (lo or 0)) / step
            if abs(ratio - round(ratio)) > 1e-9:
                raise SubmitRefused(f"not a multiple of the step {step}", field=name)
        return (int(n) if n == int(n) else n), False
    if typ == "date":
        if not isinstance(value, str) or not _DATE.match(value):
            raise SubmitRefused("a date is YYYY-MM-DD", field=name)
        try:
            date.fromisoformat(value)
        except ValueError:
            raise SubmitRefused("not a real date", field=name) from None
        return value, False
    if typ == "datetime-local":
        if not isinstance(value, str) or not _DATETIME.match(value):
            raise SubmitRefused("a date and time is YYYY-MM-DDTHH:MM", field=name)
        try:
            datetime.fromisoformat(value)
        except ValueError:
            raise SubmitRefused("not a real date and time", field=name) from None
        return value, False
    raise SubmitRefused("an unsupported field type", field=name)  # pragma: no cover


def validate(fields: list[dict], complete_all: bool, values) -> tuple[dict, list[str]]:
    """Check a posted batch against the stored field list. Returns (answers, blank):
    every stored field has an entry, a blank one as "" (a checkbox group as []), and
    `blank` names them. Refuses, naming the field: a name the form does not have, a
    value outside a field's options or limits, a blank `required` field and, when the
    form is `data-complete="all"`, any blank field."""
    if not isinstance(values, dict):
        raise SubmitRefused("values must be an object of field name to value")
    if len(values) > MAX_FIELDS_SENT:
        raise SubmitRefused(f"more than {MAX_FIELDS_SENT} fields sent")
    known = {f["name"]: f for f in fields}
    for name in values:
        if name not in known:
            raise SubmitRefused("this form has no such field", field=str(name)[:100])
    answers: dict = {}
    blank: list[str] = []
    for spec in fields:
        stored, is_blank = _one(spec, values.get(spec["name"]))
        answers[spec["name"]] = stored
        if is_blank:
            blank.append(spec["name"])
            if spec.get("required"):
                raise SubmitRefused("this field is required", field=spec["name"])
            if complete_all:
                raise SubmitRefused(
                    "this form needs every field answered", field=spec["name"]
                )
    return answers, blank


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _still_latest(conn, decision_id, latest) -> None:
    """Called with the decision row locked. The version read before the lock may be gone:
    a republish holds the same lock while `forms.sync` stores the new version and moves the
    record onto it, so a submit or relay that waited on it must not write the old version's
    answers onto the new questions. 409, and the caller reads the form again."""
    now = conn.execute(
        "select max(version_n) from decision_form where decision_id = %s",
        (decision_id,),
    ).fetchone()[0]
    if now != latest:
        raise SubmitRefused(
            f"this form was changed (version {now}); reload the page", status=409
        )


def _on_latest(conn, decision_id, record, latest, fields, complete):
    """The record brought onto the form's latest stored version. `forms.sync` does this
    at a republish; this catches a record left on an older version (one written before
    the carry rules existed). Only ever forwards: a record already past `latest` means the
    caller's read is stale (`_still_latest` refuses that first)."""
    if not record or record.get("version") == latest:
        return record
    if (record.get("version") or 0) > latest:
        raise SubmitRefused(
            f"this form was changed (version {record.get('version')}); reload the page",
            status=409,
        )
    old = forms._fields(conn, decision_id, record.get("version") or 0)
    return formrecord.reconcile(record, old, fields, latest, complete)


def submit(
    db,
    decision_id: str,
    *,
    version: int,
    values,
    by: str | None = None,
) -> dict:
    """Record one submit of a form and return the merged record. `store.NotFound` for
    a form that does not exist or is closed (its agent said it is no longer needed);
    `SubmitRefused` for a batch the stored field list does not allow, including a
    `version` that is no longer the form's latest (409: he answered a question that
    has since changed, and nothing is carried silently)."""
    by = by or settings.owner_id()
    with db.connect() as conn:
        row = conn.execute(
            "select d.artifact_id, d.form_name, d.session, d.answer_record,"
            " a.agent, a.title from decision d"
            " join artifact a on a.id = d.artifact_id"
            " where d.id = %s and d.kind = 'form' and d.closed_at is null"
            " and a.deleted_at is null and a.archived_at is null",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise store.NotFound()
        artifact_id, form_name, asking_session, _record, owner, title = row
        latest = conn.execute(
            "select max(version_n) from decision_form where decision_id = %s",
            (decision_id,),
        ).fetchone()[0]
        if latest is None:
            raise store.NotFound()
        if version != latest:
            raise SubmitRefused(
                f"this form was changed (version {latest}); reload the page",
                status=409,
            )
        complete = bool(
            conn.execute(
                "select complete_all from decision_form"
                " where decision_id = %s and version_n = %s",
                (decision_id, latest),
            ).fetchone()[0]
        )
        fields = forms._fields(conn, decision_id, latest)
        content_hash = conn.execute(
            "select body_sha256 from version where artifact_id = %s and n = %s",
            (artifact_id, latest),
        ).fetchone()
    answers, blank = validate(fields, complete, values)
    # The local secret scanner, as for a comment: a hit refuses, naming the field.
    for spec in fields:
        v = answers[spec["name"]]
        if spec["type"] in ("text", "textarea") and v:
            try:
                store._check_secrets(
                    v, f"field {spec['name']}", agent=by, artifact_id=artifact_id
                )
            except store.SecretFound as exc:
                raise SubmitRefused(
                    "looks like it contains a secret", field=spec["name"]
                ) from exc

    with db.connect() as conn, conn.transaction():
        # A form on an archived or binned page is set aside (or its answer would land
        # on a page nobody can see): lock the row, THEN re-read the page, as
        # `decisions.answer` does, so an archive between the read above and this
        # write refuses the submit (NotFound -> 404).
        decisions._require_live_page(conn, decision_id, artifact_id)
        current = conn.execute(
            "select answer_record, closed_at from decision where id = %s"  # noqa: S608 -- fixed text
            + conn.for_update(" of decision"),
            (decision_id,),
        ).fetchone()
        if current is None or current[1] is not None:
            raise store.NotFound()
        _still_latest(conn, decision_id, latest)
        previous = _on_latest(conn, decision_id, current[0], latest, fields, complete)
        now = datetime.now(UTC)
        submit_id = ids.new_id()
        grown = formrecord.apply_submit(
            previous, latest, fields, complete, answers, by=by, at=now
        )
        total, total_blank = grown["answers"], grown["blank"]
        entry = {
            "submit_id": submit_id,
            "version": latest,
            "answered_by": by,
            "answered_at": _stamp(now),
            "answers": answers,
            "blank": blank,
        }
        history = ((previous or {}).get("history") or [])[-(MAX_HISTORY - 1) :]
        record = {
            **grown,
            "decision_id": decision_id,
            "kind": "form",
            "form": form_name,
            "version": latest,
            "content_hash": content_hash[0] if content_hash else None,
            "submit_id": submit_id,
            "answered_by": by,
            "answered_at": entry["answered_at"],
            "answers": total,
            "blank": total_blank,
            "sent": [n for n in answers if n not in blank],
            "history": [*history, entry],
        }
        conn.execute(
            "update decision set answer_record = %s, answered_at = %s,"
            " answered_by = %s, answer_source = 'page', recorded_by = null,"
            " said_at = null, acknowledged_at = null, acknowledged_by = null"
            " where id = %s and closed_at is null",
            (jsonb(record), now, by, decision_id),
        )
        receipts.void(conn, decision_id)
        answered = len(fields) - len(total_blank)
        summary = f"form {form_name}: {answered} of {len(fields)} answered"
        emission = events.emit(
            conn,
            events.Event(
                kind="decision_answered",
                artifact_id=artifact_id,
                agent=owner,
                actor=by,
                summary=summary,
                ref_id=decision_id,
                session=asking_session,
                data={"title": title},
            ),
        )
    emission.fire()
    return record


def _relay_value(spec: dict, value):
    """One relayed value, coerced from what a CLI can say (strings) to what the field
    stores, then held to the same rules as his own submit. A relay says something, so a
    blank is refused rather than ignored."""
    if isinstance(value, str) and spec["type"] == "checkbox":
        if spec.get("options"):
            value = [v.strip() for v in value.split(",") if v.strip()]
        else:
            word = value.strip().lower()
            if word in ("true", "yes", "on", "checked"):
                value = True
            elif word in ("false", "no", "off", "unchecked"):
                value = False
    stored, is_blank = _one(spec, value)
    if is_blank:
        raise SubmitRefused("a relayed field needs a value", field=spec["name"])
    return stored


def relay(
    db,
    decision_id: str,
    *,
    fields_given,
    note: str,
    source: str,
    recorded_by: str,
    said_at: datetime | None = None,
) -> dict:
    """Store an answer he gave somewhere that is not the page, FIELD BY FIELD, with its
    source (3.4, 3.6). A relayed field is kept as a relay (`sources`), never counts as his
    own submit, and never overwrites a value he submitted later than `said_at`
    (`skipped`). The record's `answers` take it as the current value. Returns
    {"record", "applied", "skipped"}. `store.NotFound` for a form that is not there, is
    closed or sits on an archived page; `SubmitRefused` (422) for a field the stored list
    does not have or a value it does not allow."""
    if not isinstance(fields_given, dict) or not fields_given:
        raise SubmitRefused("send {name: value} for at least one field")
    if len(fields_given) > MAX_FIELDS_SENT:
        raise SubmitRefused(f"more than {MAX_FIELDS_SENT} fields sent")
    if source not in decisions.stored_sources():
        raise SubmitRefused(f"unknown answer source {source!r}")
    with db.connect() as conn:
        row = conn.execute(
            "select d.artifact_id, d.form_name, d.session, a.agent"
            " from decision d join artifact a on a.id = d.artifact_id"
            " where d.id = %s and d.kind = 'form' and d.closed_at is null"
            " and a.deleted_at is null and a.archived_at is null",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise store.NotFound()
        artifact_id, form_name, asking_session, owner = row
        latest = conn.execute(
            "select max(version_n) from decision_form where decision_id = %s",
            (decision_id,),
        ).fetchone()[0]
        if latest is None:
            raise store.NotFound()
        complete = bool(
            conn.execute(
                "select complete_all from decision_form"
                " where decision_id = %s and version_n = %s",
                (decision_id, latest),
            ).fetchone()[0]
        )
        fields = forms._fields(conn, decision_id, latest)
    known = {f["name"]: f for f in fields}
    relayed: dict = {}
    for name, value in fields_given.items():
        if name not in known:
            raise SubmitRefused("this form has no such field", field=str(name)[:100])
        relayed[name] = _relay_value(known[name], value)
    store._check_size(
        note, decisions.MAX_NOTE_BYTES, "note", agent=recorded_by, artifact_id=None
    )
    store._check_secrets(note, "note", agent=recorded_by, artifact_id=artifact_id)
    for name, value in relayed.items():
        if known[name]["type"] in ("text", "textarea"):
            try:
                store._check_secrets(
                    value, f"field {name}", agent=recorded_by, artifact_id=artifact_id
                )
            except store.SecretFound as exc:
                raise SubmitRefused(
                    "looks like it contains a secret", field=name
                ) from exc

    with db.connect() as conn, conn.transaction():
        decisions._require_live_page(conn, decision_id, artifact_id)
        current = conn.execute(
            "select answer_record, closed_at from decision where id = %s"  # noqa: S608 -- fixed text
            + conn.for_update(" of decision"),
            (decision_id,),
        ).fetchone()
        if current is None or current[1] is not None:
            raise store.NotFound()
        _still_latest(conn, decision_id, latest)
        previous = _on_latest(conn, decision_id, current[0], latest, fields, complete)
        now = datetime.now(UTC)
        grown, applied, skipped = formrecord.apply_relay(
            previous,
            latest,
            fields,
            complete,
            relayed,
            source=source,
            by=recorded_by,
            said_at=said_at,
            now=now,
        )
        entry = {
            "submit_id": ids.new_id(),
            "version": latest,
            "kind": "relay",
            "answered_by": settings.owner_id(),
            "source": source,
            "recorded_by": recorded_by,
            "answered_at": _stamp(now),
            "said_at": _stamp(formrecord.as_utc(said_at)) if said_at else None,
            "answers": relayed,
            "applied": applied,
            "skipped": skipped,
            "note": note,
        }
        history = ((previous or {}).get("history") or [])[-(MAX_HISTORY - 1) :]
        content_hash = conn.execute(
            "select body_sha256 from version where artifact_id = %s and n = %s",
            (artifact_id, latest),
        ).fetchone()
        record = {
            "decision_id": decision_id,
            "kind": "form",
            "form": form_name,
            "content_hash": content_hash[0] if content_hash else None,
            "answered_by": settings.owner_id(),
            "sent": [],
            **{k: v for k, v in (previous or {}).items() if k not in grown},
            **grown,
            "version": latest,
            "history": [*history, entry],
        }
        if not applied:
            # Everything was older than a later submit of his: kept in the history, no
            # change to his record and nothing for the agent to wake for.
            if previous is not None:
                conn.execute(
                    "update decision set answer_record = %s where id = %s",
                    (jsonb(record), decision_id),
                )
            return {"record": record, "applied": [], "skipped": skipped}
        record["answered_at"] = entry["answered_at"]
        record["submit_id"] = entry["submit_id"]
        record["sent"] = applied
        conn.execute(
            "update decision set answer_record = %s, answered_at = %s,"
            " answered_by = %s, answer_source = %s, recorded_by = %s,"
            " said_at = %s, acknowledged_at = null, acknowledged_by = null"
            " where id = %s and closed_at is null",
            (
                jsonb(record),
                now,
                settings.owner_id(),
                source,
                recorded_by,
                said_at,
                decision_id,
            ),
        )
        receipts.void(conn, decision_id)
        answered = len(fields) - len(record["blank"])
        summary = (
            f"form {form_name}: {len(applied)} field(s) relayed from {source}, "
            f"{answered} of {len(fields)} answered"
        )[:200]
        events.append(
            conn,
            artifact_id=artifact_id,
            agent=owner,
            actor=recorded_by,
            kind="decision_answered",
            summary=summary,
            ref_id=decision_id,
            note=note,
            session=asking_session,
        )
    return {"record": record, "applied": applied, "skipped": skipped}
