"""The record a form keeps (docs/agent/specs/2026-10-29-form-controls-design.md 3.5-3.6).

PURE. No database, no clock but the ones passed in; `formanswer` (a submit, a relay) and
`forms.sync` (a republish) call these inside their own transactions.

THE RECORD'S ANSWERS ARE ALWAYS ABOUT THE FORM'S LATEST STORED VERSION. `answers` has one
entry per current field (blank ones "" or []), `blank` names them, and `sources` says, per
non-blank field, whose answer it is: `source` "page" is HIS submit, anything else is a relay
by an agent (`by`), `at` is when he said it (a relay's `said_at`, else when it was stored),
`version` is the stored form version the answer was GIVEN on, and `label` is the wording it
answered. A relay therefore never counts as his own answer, and the rule for a clash is
one line: a relay never overwrites a value he submitted later than the relay (`apply_relay`).

A REPUBLISH (`reconcile`) walks every field of the new version against the version the record
was on. Same name, type, options, limits AND label text (`required` is not part of the
question: an answer to an optional field is still a valid answer when it becomes required):
CARRIED, still current. Changed or renamed: STALE, the field starts empty and the earlier
answer rides in `stale` with the version it was given on and the wording it answered. Removed:
STALE too. Nothing moves silently. Surrounding prose is never compared, only the controls and
their labels.
"""

from __future__ import annotations

from datetime import UTC, datetime

MAX_STALE = 100


def is_blank(value) -> bool:
    """An empty string or list is blank; False (a lone checkbox, unticked) is an answer."""
    return value == "" or value == []


def blank_of(spec: dict):
    return [] if spec["type"] == "checkbox" and spec.get("options") else ""


def same_question(old: dict, new: dict) -> bool:
    """Whether an answer to `old` is still an answer to `new`: everything the question is
    made of (name, type, options with their labels, limits, label text) but `required`."""
    drop = {"required"}
    return {k: v for k, v in old.items() if k not in drop} == {
        k: v for k, v in new.items() if k not in drop
    }


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def display(spec: dict | None, value) -> str:
    """A stored value as text, with the option labels of the spec it was given against."""
    if is_blank(value):
        return ""
    options = {o["value"]: o["label"] for o in (spec or {}).get("options") or []}
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(options.get(v, str(v)) for v in value)
    return options.get(value, str(value)) if isinstance(value, str) else str(value)


def source_of(record: dict, name: str) -> dict:
    """Who gave the current answer to `name`. A record from before sources were kept
    (slice 2) is all his, on the version it carries."""
    found = (record.get("sources") or {}).get(name)
    if isinstance(found, dict):
        return found
    return {
        "source": "page",
        "by": record.get("answered_by") or "jeremy",
        "at": record.get("answered_at"),
        "version": record.get("version"),
    }


def _stale_entry(name, value, spec, src, reason):
    return {
        "name": name,
        "value": value,
        "text": display(spec, value),
        "label": (spec or {}).get("label") or name,
        "version": src.get("version"),
        "reason": reason,
        "source": src.get("source", "page"),
        "by": src.get("by"),
        "at": src.get("at"),
    }


def _stale_map(record: dict) -> dict[str, dict]:
    return {
        s["name"]: s
        for s in record.get("stale") or []
        if isinstance(s, dict) and "name" in s
    }


def finish(
    record: dict,
    fields: list[dict],
    complete_all: bool,
    answers: dict,
    sources: dict,
    stale: dict[str, dict],
) -> dict:
    """The record with these answers, sources and stale entries: `blank` and `reopened`
    recomputed from the fields. A form is reopened while a required field (or, on a
    `data-complete="all"` form, any field) has no current answer."""
    blank = [f["name"] for f in fields if is_blank(answers[f["name"]])]
    required = {f["name"] for f in fields if f.get("required") or complete_all}
    out = dict(record)
    out.update(
        answers=answers,
        blank=blank,
        sources={n: sources[n] for n in answers if n in sources and n not in blank},
        stale=list(stale.values())[-MAX_STALE:],
        reopened=bool(required & set(blank)),
    )
    return out


def reconcile(
    record: dict,
    old_fields: list[dict],
    new_fields: list[dict],
    version: int,
    complete_all: bool,
) -> dict:
    """The record after the form was republished at stored `version` (3.5): carried,
    stale and removed fields, as the module docstring says."""
    old_by = {f["name"]: f for f in old_fields}
    new_names = {f["name"] for f in new_fields}
    old_answers = record.get("answers") or {}
    stale = _stale_map(record)
    answers: dict = {}
    sources: dict = {}
    for spec in new_fields:
        name = spec["name"]
        old = old_by.get(name)
        had = (
            old is not None and name in old_answers and not is_blank(old_answers[name])
        )
        if had and same_question(old, spec):
            answers[name] = old_answers[name]
            sources[name] = source_of(record, name)
            stale.pop(name, None)
            continue
        answers[name] = blank_of(spec)
        if had:
            stale[name] = _stale_entry(
                name, old_answers[name], old, source_of(record, name), "changed"
            )
    for name, old in old_by.items():
        if (
            name not in new_names
            and name in old_answers
            and not is_blank(old_answers[name])
        ):
            stale[name] = _stale_entry(
                name, old_answers[name], old, source_of(record, name), "removed"
            )
    for name, entry in stale.items():
        if name not in new_names:
            entry["reason"] = "removed"
    return finish(
        {**record, "version": version},
        new_fields,
        complete_all,
        answers,
        sources,
        stale,
    )


def apply_submit(
    record: dict | None,
    version: int,
    fields: list[dict],
    complete_all: bool,
    sent: dict,
    *,
    by: str,
    at: datetime,
) -> dict:
    """The record after his submit of `sent` (validated, one entry per field). The later
    non-blank value wins per field; a blank never erases. `record` is already on
    `version` (or None)."""
    base = record or {}
    previous = base.get("answers") or {}
    stale = _stale_map(base)
    answers: dict = {}
    sources: dict = {}
    for spec in fields:
        name = spec["name"]
        new = sent[name]
        if is_blank(new):
            answers[name] = previous.get(name, new)
            if name in previous and not is_blank(previous[name]):
                sources[name] = source_of(base, name)
            continue
        answers[name] = new
        sources[name] = {
            "source": "page",
            "by": by,
            "at": stamp(at),
            "version": version,
            "label": spec.get("label"),
        }
        stale.pop(name, None)
    return finish(base, fields, complete_all, answers, sources, stale)


def apply_relay(
    record: dict | None,
    version: int,
    fields: list[dict],
    complete_all: bool,
    relayed: dict,
    *,
    source: str,
    by: str,
    said_at: datetime | None,
    now: datetime,
) -> tuple[dict, list[str], list[str]]:
    """The record after an agent relayed `relayed` (validated, non-blank values for some
    fields) as something he said elsewhere. Returns (record, applied, skipped). A relayed
    field is stored with its source and never counts as his own; it never overwrites a
    value he submitted LATER than the relay (`skipped`)."""
    base = record or {}
    at = as_utc(said_at) if said_at else now
    answers = {
        spec["name"]: (base.get("answers") or {}).get(spec["name"], blank_of(spec))
        for spec in fields
    }
    sources = {n: source_of(base, n) for n in answers if not is_blank(answers[n])}
    stale = _stale_map(base)
    applied: list[str] = []
    skipped: list[str] = []
    for spec in fields:
        name = spec["name"]
        if name not in relayed:
            continue
        mine = sources.get(name)
        if (
            mine
            and mine.get("source") == "page"
            and mine.get("at")
            and as_utc(datetime.fromisoformat(mine["at"])) > at
        ):
            skipped.append(name)
            continue
        answers[name] = relayed[name]
        sources[name] = {
            "source": source,
            "by": by,
            "at": stamp(at),
            "stored_at": stamp(now),
            "version": version,
            "label": spec.get("label"),
        }
        stale.pop(name, None)
        applied.append(name)
    return finish(base, fields, complete_all, answers, sources, stale), applied, skipped


def gallery_view(record: dict | None, fields: list[dict]) -> dict:
    """What the gallery draws and replays for one form: `answered` (his own submit, still
    standing; None when the form is reopened or nothing of his is current), `carried`
    (his own current answers, written back into the page's controls), `relayed` (current
    answers somebody relayed, shown but never written into the page) and `stale`."""
    out = {"answered": None, "carried": {}, "relayed": {}, "stale": []}
    if not record:
        return out
    answers = record.get("answers") or {}
    latest = None
    for spec in fields:
        name = spec["name"]
        value = answers.get(name)
        if value is None or is_blank(value):
            continue
        src = source_of(record, name)
        if src.get("source") == "page":
            out["carried"][name] = value
            latest = max(filter(None, [latest, src.get("at")]), default=latest)
        else:
            out["relayed"][name] = {
                "value": value,
                "text": display(spec, value),
                "source": src.get("source"),
                "by": src.get("by"),
                "version": src.get("version"),
            }
    out["stale"] = [
        s for s in record.get("stale") or [] if isinstance(s, dict) and "name" in s
    ]
    if out["carried"] and not record.get("reopened"):
        out["answered"] = {
            "answered_at": latest or record.get("answered_at"),
            "answered_by": record.get("answered_by"),
            "version": record.get("version"),
        }
    return out
