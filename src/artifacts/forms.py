"""Form controls inside a page (docs/agent/specs/2026-10-29-form-controls-design.md).

An agent wraps native HTML controls in an element carrying `data-artifact-form="<name>"`.
At publish the server reads those subtrees, stores each form's field list and makes each
form one decision of kind `form`. This module is slice 1: parsing, validating, storing and
reading back. There is no Submit and no answer yet; the controls are plain controls on the
page.

PURE FIRST, THEN STORAGE. `parse` and `warnings` touch no database and are what the tests
pin. `sync` writes inside the publish's own transaction, so the version and its forms commit
together or not at all. `fields_for_page` and `for_decisions` are the reads.

THE STORED LIST IS THE AUTHORITY (design 3.2). A form with a field that cannot be satisfied,
or a control the service does not support, is refused at publish with a `422` naming the
form and the control, so the agent learns when it publishes and not when Jeremy submits.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser

from artifacts import formrecord, ids, text
from artifacts.db import jsonb

# Elements that never have content or an end tag.
_VOID = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    ]
)

# What `<input type=...>` may be inside a form. Anything else is refused by name.
_INPUT_TYPES = frozenset(
    ["radio", "checkbox", "text", "number", "date", "datetime-local"]
)

# Names (of a form and of a field): printable, no whitespace, the shapes agents already use
# ("region", "wt.caps-unit-trim", "rows[3]").
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:\-\[\]]{0,99}\Z")

MAX_FORMS_PER_PAGE = 50
MAX_FIELDS_PER_FORM = 200
MAX_OPTIONS = 200
MAX_LABEL = 200
MAX_VALUE = 200
MAX_MAXLENGTH = 10_000
# Open labels and fieldsets inside one form. Each holds per-element state, so an
# unbounded run of unclosed `<label>`s would cost memory per request; real forms
# nest two or three deep.
MAX_NESTING = 64

# A cheap test before any parse: most pages have no control at all.
_MAYBE = re.compile(
    r"data-artifact-form|<(?:input|textarea|select|button)\b", re.IGNORECASE
)


class FormRefused(Exception):
    """A page's form cannot be stored. Carries what a 422 names: the form, the
    control (field name) and the line, when known, and a fixed-text detail."""

    def __init__(
        self,
        detail: str,
        *,
        form: str | None = None,
        control: str | None = None,
        line: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.form = form
        self.control = control
        self.line = line

    def as_detail(self) -> dict:
        out: dict = {"field": "body", "detail": self.detail}
        if self.form is not None:
            out["form"] = self.form
        if self.control is not None:
            out["control"] = self.control
        if self.line is not None:
            out["line"] = self.line
        return out


@dataclass
class Form:
    name: str
    title: str
    complete_all: bool
    fields: list[dict]


@dataclass
class Parsed:
    forms: list[Form] = field(default_factory=list)
    # Line of the first input/textarea/select outside every form, else None.
    stray_line: int | None = None


# --- the walk -----------------------------------------------------------------


@dataclass
class _Control:
    tag: str
    type: str
    name: str
    line: int
    attrs: dict
    fieldset: object | None
    label_text: str = ""
    options: list = field(default_factory=list)  # select: (value, label)


@dataclass
class _Label:
    for_id: str
    controls: list = field(default_factory=list)
    text: list = field(default_factory=list)


@dataclass
class _Fieldset:
    legend: str = ""


@dataclass
class _FormBuild:
    name: str
    complete_all: bool
    aria_label: str
    root_depth: int
    root_is_fieldset: bool
    controls: list = field(default_factory=list)
    labels_for: dict = field(default_factory=dict)
    legend_of_root: str = ""


class _Walk(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        # Open count per tag, so an unmatched end tag is an O(1) miss: a page of
        # a million stray `</x>` would otherwise scan the stack once each.
        self._open: dict[str, int] = {}
        self.forms: list[_FormBuild] = []
        self.names: set[str] = set()
        self.form: _FormBuild | None = None
        self.stray_line: int | None = None
        self._labels: list[_Label] = []
        self._fieldsets: list[tuple[int, _Fieldset]] = []
        self._legend: list[str] | None = None
        self._legend_depth = 0
        self._in_textarea = False
        self._select: _Control | None = None
        self._option: tuple[str | None, str | None, list[str]] | None = None

    # -- helpers
    def _line(self) -> int:
        return self.getpos()[0]

    def _refuse(self, detail: str, control: str | None = None) -> FormRefused:
        return FormRefused(
            detail,
            form=self.form.name if self.form else None,
            control=control,
            line=self._line(),
        )

    def _finish_option(self) -> None:
        if self._option is None or self._select is None:
            self._option = None
            return
        value, label_attr, parts = self._option
        shown = " ".join("".join(parts).split())
        self._select.options.append(
            (value if value is not None else shown, label_attr or shown)
        )
        self._option = None

    # -- events
    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if "data-artifact-form" in a:
            self._open_form(tag, a)
        if tag in _VOID:
            self._control_or_void(tag, a)
            return
        self.stack.append(tag)
        self._open[tag] = self._open.get(tag, 0) + 1
        if self.form is None:
            # Still count strays among the non-void controls.
            if tag in ("textarea", "select") and self.stray_line is None:
                self.stray_line = self._line()
            return
        if tag in ("label", "fieldset") and (
            len(self._labels) + len(self._fieldsets) >= MAX_NESTING
        ):
            raise self._refuse(
                f"labels and fieldsets nest more than {MAX_NESTING} deep in one form"
            )
        if tag == "label":
            self._labels.append(_Label(a.get("for", "")))
        elif tag == "fieldset":
            self._fieldsets.append((len(self.stack), _Fieldset()))
        elif tag == "legend":
            self._legend = []
            self._legend_depth = len(self.stack)
        elif tag == "textarea":
            self._in_textarea = True
            self._add_control(tag, "textarea", a)
        elif tag == "select":
            if "multiple" in a:
                raise self._refuse(
                    "select multiple is not supported; use checkboxes",
                    a.get("name") or None,
                )
            self._select = self._add_control(tag, "select", a)
        elif tag == "optgroup":
            self._finish_option()
        elif tag == "option" and self._select is not None:
            self._finish_option()
            self._option = (a.get("value"), a.get("label"), [])
        elif tag == "button":
            raise self._refuse(
                "button is not supported inside a form: the gallery draws Submit",
                a.get("name") or None,
            )

    def _open_form(self, tag: str, a: dict) -> None:
        if self.form is not None:
            raise self._refuse("data-artifact-form elements cannot be nested")
        name = a["data-artifact-form"].strip()
        if not _NAME.match(name):
            raise FormRefused(
                "data-artifact-form needs a name of letters, digits and _ . : - "
                "[ ], at most 100 characters",
                line=self._line(),
            )
        if name in self.names:
            raise FormRefused(
                "two forms on one page share a name", form=name, line=self._line()
            )
        self.names.add(name)
        complete = a.get("data-complete", "").strip().lower()
        if "data-complete" in a and complete != "all":
            raise FormRefused(
                'data-complete accepts only "all"', form=name, line=self._line()
            )
        if tag in _VOID:
            raise FormRefused(
                "data-artifact-form cannot sit on a void element",
                form=name,
                line=self._line(),
            )
        self.form = _FormBuild(
            name=name,
            complete_all=complete == "all",
            aria_label=a.get("aria-label", ""),
            # The root itself is pushed right after this returns.
            root_depth=len(self.stack) + 1,
            root_is_fieldset=tag == "fieldset",
        )
        self.forms.append(self.form)
        if len(self.forms) > MAX_FORMS_PER_PAGE:
            raise FormRefused(
                f"more than {MAX_FORMS_PER_PAGE} forms on one page",
                form=name,
                line=self._line(),
            )

    def _add_control(self, tag: str, typ: str, a: dict) -> _Control:
        if self.form is None:
            raise RuntimeError("control outside a form")
        name = a.get("name", "").strip()
        if not name:
            raise self._refuse(f"a {typ} control has no name")
        if not _NAME.match(name):
            raise self._refuse(
                "control names use letters, digits and _ . : - [ ], at most 100 "
                "characters",
                name,
            )
        control = _Control(
            tag=tag,
            type=typ,
            name=name,
            line=self._line(),
            attrs=a,
            fieldset=self._fieldsets[-1][1] if self._fieldsets else None,
        )
        if len(self.form.controls) >= MAX_FIELDS_PER_FORM * 4:
            raise self._refuse(
                f"form has too many controls (over {MAX_FIELDS_PER_FORM} fields)"
            )
        self.form.controls.append(control)
        if self._labels:
            self._labels[-1].controls.append(control)
        return control

    def _control_or_void(self, tag: str, a: dict) -> None:
        if tag != "input":
            return
        if self.form is None:
            if self.stray_line is None:
                self.stray_line = self._line()
            return
        typ = (a.get("type") or "text").strip().lower()
        if typ not in _INPUT_TYPES:
            raise self._refuse(
                f"input type={typ!r} is not supported inside a form (supported: "
                "radio, checkbox, text, textarea, number, date, datetime-local, "
                "select)",
                a.get("name") or None,
            )
        self._add_control(tag, typ, a)

    def handle_data(self, data):
        if self.form is None:
            return
        if self._legend is not None:
            self._legend.append(data)
        if self._option is not None:
            self._option[2].append(data)
        elif self._labels and not self._in_textarea and self._select is None:
            self._labels[-1].text.append(data)

    def handle_endtag(self, tag):
        if tag in _VOID or not self._open.get(tag):
            return
        # Pop to the matching open element: `</td>` closes an unclosed `<p>`.
        while self.stack:
            depth = len(self.stack)
            top = self.stack.pop()
            self._open[top] -= 1
            if self.form is not None:
                self._closed(top, depth)
            if top == tag:
                break

    def _closed(self, tag: str, depth: int) -> None:
        form = self.form
        if form is None:
            raise RuntimeError("element closed outside a form")
        if tag == "label" and self._labels:
            label = self._labels.pop()
            words = " ".join("".join(label.text).split())
            for control in label.controls:
                control.label_text = control.label_text or words
            if label.for_id:
                form.labels_for[label.for_id] = words
        elif tag == "fieldset" and self._fieldsets and self._fieldsets[-1][0] == depth:
            self._fieldsets.pop()
        elif tag == "legend" and self._legend is not None:
            words = " ".join("".join(self._legend).split())
            self._legend = None
            if self._fieldsets:
                self._fieldsets[-1][1].legend = words
            if form.root_is_fieldset and len(self._fieldsets) == 1:
                form.legend_of_root = words
        elif tag == "textarea":
            self._in_textarea = False
        elif tag == "option":
            self._finish_option()
        elif tag == "select":
            self._finish_option()
            self._select = None
        if depth == form.root_depth:
            self.form = None
            self._labels.clear()
            self._fieldsets.clear()
            self._legend = None
            self._in_textarea = False
            self._select = None
            self._option = None


def _num(value: str | None, what: str, form: str, control: _Control):
    if value is None or value.strip() == "":
        return None
    try:
        n = float(value)
    except ValueError:
        raise FormRefused(
            f"{what} is not a number",
            form=form,
            control=control.name,
            line=control.line,
        ) from None
    if not math.isfinite(n):
        raise FormRefused(
            f"{what} is not a finite number",
            form=form,
            control=control.name,
            line=control.line,
        )
    return int(n) if n == int(n) else n


def _label_for(control: _Control, form: _FormBuild) -> str:
    cid = control.attrs.get("id", "")
    raw = (
        control.label_text
        or (form.labels_for.get(cid, "") if cid else "")
        or control.attrs.get("aria-label", "")
    )
    return text.sanitize(raw, MAX_LABEL)


def _build_form(form: _FormBuild) -> Form:
    by_name: dict[str, list[_Control]] = {}
    for control in form.controls:
        by_name.setdefault(control.name, []).append(control)
    fields: list[dict] = []
    for name, group in by_name.items():
        first = group[0]
        kinds = {c.type for c in group}
        if len(kinds) > 1:
            raise FormRefused(
                "the name is used by controls of different types",
                form=form.name,
                control=name,
                line=group[1].line,
            )
        typ = first.type
        required = any("required" in c.attrs for c in group)
        spec: dict = {"name": name, "type": typ, "required": required}
        if typ in ("radio", "checkbox"):
            options = []
            for c in group:
                value = c.attrs.get("value")
                if typ == "radio" or len(group) > 1:
                    if value is None or value == "":
                        raise FormRefused(
                            f"every {typ} in a group needs a non-empty value",
                            form=form.name,
                            control=name,
                            line=c.line,
                        )
                    if len(value) > MAX_VALUE:
                        raise FormRefused(
                            "an option value is too long",
                            form=form.name,
                            control=name,
                            line=c.line,
                        )
                    options.append(
                        {"value": value, "label": _label_for(c, form) or value}
                    )
            values = [o["value"] for o in options]
            if len(set(values)) != len(values):
                raise FormRefused(
                    "two options share a value",
                    form=form.name,
                    control=name,
                    line=first.line,
                )
            if typ == "radio" and len(options) < 2:
                raise FormRefused(
                    "a radio group needs at least two options",
                    form=form.name,
                    control=name,
                    line=first.line,
                )
            if len(options) > MAX_OPTIONS:
                raise FormRefused(
                    f"more than {MAX_OPTIONS} options",
                    form=form.name,
                    control=name,
                    line=first.line,
                )
            if typ == "checkbox" and len(group) == 1:
                # A lone checkbox is yes or no (never blank); its label is the question.
                label = _label_for(first, form)
            else:
                label = text.sanitize(
                    (first.fieldset.legend if first.fieldset else "")
                    or first.attrs.get("aria-label", ""),
                    MAX_LABEL,
                )
            spec["options"] = options
        else:
            if len(group) > 1:
                raise FormRefused(
                    "two controls share a name",
                    form=form.name,
                    control=name,
                    line=group[1].line,
                )
            label = _label_for(first, form)
            if typ == "select":
                options = [
                    {"value": v, "label": text.sanitize(lab, MAX_LABEL) or v}
                    for v, lab in first.options
                    if v != ""
                ]
                values = [o["value"] for o in options]
                if not options:
                    raise FormRefused(
                        "a select needs at least one option with a value",
                        form=form.name,
                        control=name,
                        line=first.line,
                    )
                if len(set(values)) != len(values):
                    raise FormRefused(
                        "two options share a value",
                        form=form.name,
                        control=name,
                        line=first.line,
                    )
                if len(options) > MAX_OPTIONS or any(
                    len(v) > MAX_VALUE for v in values
                ):
                    raise FormRefused(
                        "too many options, or an option value is too long",
                        form=form.name,
                        control=name,
                        line=first.line,
                    )
                spec["options"] = options
            else:
                spec["options"] = []
            if typ in ("text", "textarea"):
                ml = first.attrs.get("maxlength")
                if ml is not None:
                    try:
                        n = int(ml)
                    except ValueError:
                        n = 0
                    if not 1 <= n <= MAX_MAXLENGTH:
                        raise FormRefused(
                            f"maxlength must be a whole number from 1 to {MAX_MAXLENGTH}",
                            form=form.name,
                            control=name,
                            line=first.line,
                        )
                    spec["maxlength"] = n
            if typ == "number":
                lo = _num(first.attrs.get("min"), "min", form.name, first)
                hi = _num(first.attrs.get("max"), "max", form.name, first)
                step_raw = first.attrs.get("step")
                step = (
                    None
                    if step_raw is None or step_raw.strip().lower() in ("", "any")
                    else _num(step_raw, "step", form.name, first)
                )
                if step is not None and step <= 0:
                    raise FormRefused(
                        "step must be above zero",
                        form=form.name,
                        control=name,
                        line=first.line,
                    )
                if lo is not None and hi is not None and lo > hi:
                    raise FormRefused(
                        "min is above max: no value can satisfy the field",
                        form=form.name,
                        control=name,
                        line=first.line,
                    )
                for key, val in (("min", lo), ("max", hi), ("step", step)):
                    if val is not None:
                        spec[key] = val
        spec["label"] = label or name
        fields.append(spec)
    if not fields:
        raise FormRefused("a form needs at least one control", form=form.name)
    if len(fields) > MAX_FIELDS_PER_FORM:
        raise FormRefused(
            f"more than {MAX_FIELDS_PER_FORM} fields in one form", form=form.name
        )
    title = text.sanitize(
        form.aria_label or form.legend_of_root or form.name, MAX_LABEL
    )
    return Form(form.name, title or form.name, form.complete_all, fields)


def parse(body: str) -> Parsed:
    """The page's forms, validated; `FormRefused` for a form that cannot be stored.

    Also reports where the first input/textarea/select outside every form is, for
    `warnings`. A page with no control and no `data-artifact-form` is not parsed."""
    if not _MAYBE.search(body):
        return Parsed()
    walk = _Walk()
    walk.feed(body)
    walk.close()
    return Parsed([_build_form(f) for f in walk.forms], walk.stray_line)


def warnings(body: str) -> list[dict]:
    """Advisory findings, in the shape csp.as_json returns. A refusing page never
    reaches here (it was refused), so a parse error simply reports nothing."""
    try:
        parsed = parse(body)
    except FormRefused:
        return []
    if parsed.stray_line is None:
        return []
    return [
        {
            "shape": "control_outside_form",
            "line": parsed.stray_line,
            "hint": (
                "a form control outside an element carrying data-artifact-form is "
                "inert: it submits nowhere and nothing reads it. Wrap the controls "
                'in <form data-artifact-form="name"> to ask a question inside the '
                "page."
            ),
        }
    ]


# --- storage ------------------------------------------------------------------


def _canon(fields: list) -> str:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)


def sync(conn, artifact_id: str, version_n: int, parsed: Parsed, session) -> None:
    """Make the page's open `form` decisions match `parsed`, inside the caller's
    transaction. A form keeps its decision across republishes (keyed by page and
    name); a new name mints one; a form no longer on the page is closed, its
    answers kept. The field list is stored per version, and only when it changed:
    a republish whose fields for a form are identical to its latest stored version
    keeps that version, so an unrelated page edit does not turn an open submit
    into a 409."""
    existing = dict(
        conn.execute(
            "select form_name, id from decision"
            " where artifact_id = %s and kind = 'form' and closed_at is null",
            (artifact_id,),
        ).fetchall()
    )
    now = datetime.now(UTC)
    for page_position, form in enumerate(parsed.forms):
        decision_id = existing.pop(form.name, None)
        if decision_id is None:
            decision_id = ids.new_id()
            conn.execute(
                "insert into decision"
                " (id, artifact_id, version_n, question, options, session, kind,"
                "  form_name)"
                " values (%s, %s, %s, %s, %s, %s, 'form', %s)",
                (
                    decision_id,
                    artifact_id,
                    version_n,
                    form.title,
                    jsonb([]),
                    session,
                    form.name,
                ),
            )
        else:
            conn.execute(
                "update decision set question = %s where id = %s",
                (form.title, decision_id),
            )
        latest = conn.execute(
            "select f.version_n, f.complete_all, f.position from decision_form f"
            " where f.decision_id = %s order by f.version_n desc limit 1",
            (decision_id,),
        ).fetchone()
        if latest is not None and (
            bool(latest[1]) == bool(form.complete_all)
            and latest[2] == page_position
            and _canon(_fields(conn, decision_id, latest[0]))
            == _canon([spec for spec in form.fields])
        ):
            continue
        conn.execute(
            "insert into decision_form (decision_id, version_n, complete_all, position)"
            " values (%s, %s, %s, %s)",
            (decision_id, version_n, form.complete_all, page_position),
        )
        for position, spec in enumerate(form.fields):
            conn.execute(
                "insert into decision_field (decision_id, version_n, position, name, spec)"
                " values (%s, %s, %s, %s, %s)",
                (decision_id, version_n, position, spec["name"], jsonb(spec)),
            )
        _reconcile_record(conn, decision_id, form, version_n)
    for decision_id in existing.values():
        conn.execute(
            "update decision set closed_at = %s where id = %s and closed_at is null",
            (now, decision_id),
        )


def _reconcile_record(conn, decision_id: str, form: Form, version_n: int) -> None:
    """The form has a new stored version: bring its submitted record onto it (3.5). Each
    answer whose question is unchanged is carried; a changed, renamed or removed one
    becomes stale. The agent is told by the record itself, so when something went stale
    or the form reopened its acknowledgement is cleared and it reads as new again."""
    row = conn.execute(
        "select answer_record from decision where id = %s"  # noqa: S608 -- fixed text
        + conn.for_update(" of decision"),
        (decision_id,),
    ).fetchone()
    record = row[0] if row else None
    if not record or record.get("version") == version_n:
        return
    old = _fields(conn, decision_id, record.get("version") or 0)
    grown = formrecord.reconcile(record, old, form.fields, version_n, form.complete_all)
    news = grown["stale"] != (record.get("stale") or []) or (
        grown["reopened"] and not record.get("reopened")
    )
    conn.execute(
        "update decision set answer_record = %s where id = %s",
        (jsonb(grown), decision_id),
    )
    if news:
        conn.execute(
            "update decision set acknowledged_at = null, acknowledged_by = null"
            " where id = %s",
            (decision_id,),
        )


def _fields(conn, decision_id: str, version_n: int) -> list[dict]:
    return [
        r[0]
        for r in conn.execute(
            "select spec from decision_field where decision_id = %s and version_n = %s"
            " order by position",
            (decision_id, version_n),
        ).fetchall()
    ]


def for_decisions(db, decision_ids: list[str]) -> dict[str, dict]:
    """{decision id: {form, complete, version, fields}} for the form decisions
    among `decision_ids`, at each form's latest stored version."""
    out: dict[str, dict] = {}
    if not decision_ids:
        return out
    with db.connect() as conn:
        for did in decision_ids:
            latest = conn.execute(
                "select max(version_n) from decision_form where decision_id = %s",
                (did,),
            ).fetchone()[0]
            if latest is None:
                continue
            name, complete = conn.execute(
                "select d.form_name, f.complete_all from decision d"
                " join decision_form f on f.decision_id = d.id and f.version_n = %s"
                " where d.id = %s",
                (latest, did),
            ).fetchone()
            out[did] = {
                "form": name,
                "complete": "all" if complete else None,
                "version": latest,
                "fields": _fields(conn, did, latest),
            }
    return out


def for_page(db, artifact_id: str, *, include_closed: bool = False) -> list[dict]:
    """The page's forms with the fields each had at the version it was last
    published in. Ownership is the caller's job."""
    sql = (
        "select id, form_name, question, version_n, created_at, closed_at from decision"
        " where artifact_id = %s and kind = 'form'"
    )
    if not include_closed:
        sql += " and closed_at is null"
    sql += " order by created_at asc, id asc"
    out = []
    with db.connect() as conn:
        for did, name, title, minted, _created, closed in conn.execute(
            sql, (artifact_id,)
        ).fetchall():
            latest = conn.execute(
                "select max(version_n) from decision_form where decision_id = %s",
                (did,),
            ).fetchone()[0]
            complete = conn.execute(
                "select complete_all, position from decision_form"
                " where decision_id = %s and version_n = %s",
                (did, latest),
            ).fetchone()
            out.append(
                {
                    "decision_id": did,
                    "form": name,
                    "title": title,
                    "version": latest,
                    "first_version": minted,
                    "complete": "all" if complete and complete[0] else None,
                    "closed_at": closed.isoformat() if closed is not None else None,
                    "fields": _fields(conn, did, latest) if latest is not None else [],
                    "_order": (closed is not None, complete[1] if complete else 0),
                }
            )
    # Page order: forms minted in one publish share created_at, so the SQL order
    # alone ties them on a random id. Open forms first, each in the order it sits
    # on the page at its latest version.
    out.sort(key=lambda f: f["_order"])
    for f in out:
        del f["_order"]
    return out
