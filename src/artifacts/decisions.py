r"""Decisions: an agent poses a lettered question on its page, Jeremy answers it there,
and the agent reads the answer back. This is the whole MVP the artifacts service exists
for -- everything else (comments, anchors, a live nudge) is a page to look at; this is
the first thing on a page Jeremy can actually use to move an agent's work along.

`question` and each option's `label` are content exactly like a page body or a comment
(`comments.py`) -- agent-authored, displayed verbatim, and just as able to carry a
pasted secret -- so both pass through the SAME two guards `store.publish` and
`comments.add` use: this module invents no guard machinery of its own, calling straight
into `store`'s private `_check_size`/`_check_secrets`/`_owned` and raising `store`'s own
`BodyTooLarge`, `InvalidText`, `SecretFound`, `ScanFailed` and `NotFound` wherever they
apply. Jeremy's `answer_note` gets exactly the same two guards as an agent's question --
trusting the human is the bug this would otherwise be: a homelab's own operator is not
a lower-risk source of a pasted secret than an agent is, and a page that scanned only
one side of a conversation would be scanning for the wrong threat model.

`options`' SHAPE (how many, what keys, letter format, uniqueness, label length) is a
different kind of guard: it is never content that could carry a secret worth scanning
for its own sake (a malformed shape is refused before the scanner ever sees it), and its
failure mode is a single exception, `InvalidOptions`, that names only the field
`options` -- never a key or value the caller sent. This repo already has an open finding
about exactly that leak (homelab-ij8.4, in `comments.py`'s `InvalidAnchor`); the fix
there was one bare exception raised from every branch, and this module repeats it
rather than inventing a second way to get the same thing wrong.

Guard order in every write, matching `store.publish`'s own docstring: the cheapest
checks first, spending no CPU or database round trip on a caller who has no right to
it -- sizes (which also, via UTF-8 encoding, catch a lone surrogate a JSON body can
carry but Postgres `text` cannot hold) and the options shape, both in memory; then
ownership (or, for `answer`, the equivalent existence-and-not-closed check, since
answering has no ownership concept at all -- see its own docstring); the secret scanner
always LAST, once nothing cheaper could have refused the call, and only ever run on the
value that is actually about to be stored.
"""

import dataclasses
import re
import types
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from artifacts import (
    csp,
    events,
    holds,
    ids,
    keys,
    partner,
    receipts,
    settings,
    store,
    text,
)
from artifacts.db import Connection, Database, in_list, jsonb

# A decision's question is a single line Jeremy reads on a card, not a page body -- 500
# bytes is generous for a real question and small enough that a wall of prose is refused
# rather than rendered.
MAX_QUESTION_BYTES = 500

# An option's label sits on a button, not in a paragraph -- shorter than the question
# itself, because "(a) <label>" needs to read at a glance.
MAX_LABEL_BYTES = 200

# Jeremy's note travels back to the agent as-is; it is a short aside ("go ahead, but
# watch the disk"), not a reply. Small enough that answering never becomes its own
# comment thread -- that already exists, on purpose, as a separate feature.
MAX_NOTE_BYTES = 1_000

# Six is a full row of lettered buttons a phone screen can show without scrolling, and
# nobody has ever needed a seventh: past that, the question is really several
# decisions and should be asked as such.
MAX_OPTIONS = 6

# 'form' (2 Nov 2026): one decision per `data-artifact-form` subtree (forms.py). An
# unsubmitted form is NOT a question waiting on Jeremy: every queue of "what is waiting"
# below leaves it out; once he submits (formanswer.py) it is an answer and shows to the
# agent. `answer` stays null for a form: its answer is `answer_record`, so a test of
# "open" written as `answer is null` misclassifies a submitted one (use `answered_at`).
# A form can be read back and closed like any decision; it cannot be a question's
# prerequisite or replacement (`_owned_row` stays question-only on purpose) and an agent
# cannot answer it with a letter (no options to pick).
KINDS = ("question", "review", "form")

# The four verdicts, in the order his buttons show them. A review asks
# nothing he must choose between: there is no recommended letter.
REVIEW_OPTIONS = [
    {"letter": "a", "label": "Approved"},
    {"letter": "b", "label": "Approved with notes"},
    {"letter": "c", "label": "Changes requested"},
    {"letter": "d", "label": "Dismissed"},
]
VERDICTS = {
    "a": "approved",
    "b": "approved_with_notes",
    "c": "changes_requested",
    "d": "dismissed",
}

# Every read of a decision row, in the fixed column order `_row_to_decision` expects.
# Qualified with `decision.` throughout, exactly as comments.py's `_COLUMNS` is --
# `list_for_agent` and `close` both join `artifact`, which also has an `id` and a
# `created_at`, and an unqualified name there is ambiguous to Postgres. Qualifying it
# here even where nothing else is joined means one constant serves every query in this
# file rather than two near-identical ones.
_COLUMNS = (
    "decision.id, decision.artifact_id, decision.version_n, decision.question,"
    " decision.options, decision.recommended, decision.answer, decision.answer_note,"
    " decision.answered_at, decision.answered_by, decision.created_at, decision.closed_at,"
    " decision.acknowledged_at, decision.acknowledged_by,"
    " decision.answer_source, decision.recorded_by, decision.said_at,"
    " decision.session, decision.kind, decision.review_mode,"
    " decision.context, decision.topic_id, decision.topic_title,"
    " decision.depends_on, decision.replaces, decision.replaced_by,"
    " decision.urgency, decision.blocked_on, decision.needed_by,"
    " decision.for_offices, decision.reopened_at,"
    # The page's own state, read here so a decision row can say whether it is SET
    # ASIDE (open on an archived or binned page) wherever it is read, joined or not.
    # No comma inside either subquery: `len(_COLUMNS.split(","))` is the row's width.
    # The aliases carry the SQLite column-type tag, since an expression has no
    # declared type to convert by.
    " (select x.archived_at from artifact x where x.id = decision.artifact_id)"
    ' as "archived_at [TIMESTAMPTZ]",'
    " (select x.deleted_at from artifact x where x.id = decision.artifact_id)"
    ' as "deleted_at [TIMESTAMPTZ]",'
    " decision.answer_record"
)
# Positions in `_COLUMNS` that are read by index outside `_row_to_decision`.
# Tied to its order: `session` is the 18th column, `kind` the 19th. Appending
# a column leaves both alone; inserting one before them moves them.
_SESSION_IDX = 17
_KIND_IDX = 18
_REVIEW_MODE_IDX = 19


@dataclass(frozen=True)
class DecisionRow:
    id: str
    artifact_id: str
    version_n: int
    question: str
    options: list[dict]
    recommended: str | None
    answer: str | None
    answer_note: str
    answered_at: str | None
    answered_by: str | None
    created_at: str
    closed_at: str | None
    acknowledged_at: str | None
    acknowledged_by: str | None
    # 'page' when Jeremy tapped it; 'terminal'/'telegram'/'in-person'/'other'
    # when a seat recorded words he gave elsewhere. Never null once `answer` is
    # set, so no reader has to guess what a missing value means.
    answer_source: str | None
    # The seat that wrote a relayed answer down. Null for a page tap.
    recorded_by: str | None
    # When HE said it, which is not when it was recorded.
    said_at: str | None
    # Which SESSION raised this question. A caller-declared label, never a
    # permission -- see api.session. None for anything asked before sessions
    # existed, and for a caller that did not declare one.
    session: str | None
    # 'question' for every lettered question; 'review' for a review request,
    # whose four verdicts are fixed and of which only the first counts.
    kind: str = "question"
    # 'held' or 'live' for a review (see holds.py); None for a question.
    review_mode: str | None = None
    # What a question carries (runcove-5t3oj.39.1). A review keeps the defaults.
    # `topic` is {"id", "title"} or None; `for_offices` goes out as `for` (see
    # `public`), the one JSON name that is not a Python identifier.
    context: str = ""
    topic: dict | None = None
    depends_on: list = dataclasses.field(default_factory=list)
    replaces: str | None = None
    # The decision that replaced this one, set when it was asked.
    replaced_by: str | None = None
    urgency: str = "when_you_can"
    blocked_on: str | None = None
    needed_by: str | None = None
    for_offices: list = dataclasses.field(default_factory=list)
    # A form decision's submitted answers (forms.submit); None for every other kind.
    answer_record: dict | None = None
    # The still-unanswered questions this one waits on, in `depends_on` order;
    # empty when it is not held (see `held_by`). Derived on read, never stored,
    # so a held question is released the moment its prerequisite is answered.
    held_by: list = dataclasses.field(default_factory=list)
    # SET ASIDE (2 Nov 2026): open, and its page is archived or binned. Derived on
    # read from the page's own state, never stored (see
    # docs/agent/specs/2026-11-02-archive-sets-aside-design.md). `set_aside_since` is
    # when the page went away, `set_aside_why` is "archived" or "binned"; both None
    # unless set aside. `reopened_at` is when the page came back and the question
    # with it (None until a reopen).
    set_aside: bool = False
    set_aside_since: str | None = None
    set_aside_why: str | None = None
    reopened_at: str | None = None
    # What the owning agent says it did with his answer (receipts.py): the current
    # receipt as {state, note, link, agent, session, comment_id, at, expires_at,
    # lapsed}, or None when it has said nothing. Derived on read, like `held_by`.
    receipt: dict | None = None


def public(row: DecisionRow) -> dict:
    """The JSON an agent reads for a decision: the row's fields, with
    `for_offices` under its contract name `for`."""
    out = dict(row.__dict__)
    out["for"] = out.pop("for_offices")
    return out


class InvalidOptions(Exception):
    """Any malformed `options` or `recommended` value -- wrong count, wrong keys, a
    non-letter, a duplicate letter, an empty or over-long label, a value that is not
    even a list, a `recommended` that names no option. Every one of those branches
    raises this SAME bare exception (see module docstring): it carries no detail about
    which check failed or what was sent, on purpose. `field` names only "options" so a
    caller mapping this to a 422 can say which top-level field was wrong without ever
    being tempted to also say why -- that "why" is exactly the oracle homelab-ij8.4
    warns against handing a hostile caller for free, one probe at a time.
    """

    def __init__(self) -> None:
        super().__init__("options")
        self.field = "options"


@dataclass(frozen=True)
class RoundRow:
    """One round of a review (`review_round`). `version_n` is the version under
    review; `base_version` the one the previous round's changes were requested
    on (None in round 1)."""

    review_id: str
    n: int
    version_n: int
    base_version: int | None
    opened_at: str
    closed_at: str | None
    verdict: str | None
    # The version his page view showed when he gave the verdict, when known.
    reviewed_version: int | None = None


class AlreadyDecided(Exception):
    """A second verdict on a review. The first one counts (page review design:
    'a verdict from a stale second device is refused, and he is told which
    verdict counted and when'). `row` is the review as it stands.

    `round` is the round whose verdict counted -- the one the refused verdict
    was meant for -- and `current` the review's round now. They differ when a
    stale tab answers a round a republish has since moved past. Both are None
    for a decision with no rounds."""

    def __init__(
        self,
        row: "DecisionRow",
        round: "RoundRow | None" = None,
        current: "RoundRow | None" = None,
    ) -> None:
        super().__init__("answer")
        self.field = "answer"
        self.row = row
        self.round = round
        self.current = current


class InvalidAnswer(Exception):
    """`letter` named in an `answer()` call that is not one of the decision's own
    option letters. Unlike `InvalidOptions`, there is only one way to get this wrong,
    so there is nothing to avoid leaking -- but it still carries nothing beyond the
    field name, for the same reason: consistency with every other guard in this
    module, not because this particular one needs it."""

    def __init__(self) -> None:
        super().__init__("answer")
        self.field = "answer"


def _validate_options(options: object) -> tuple[list[dict], set[str]]:
    """Normalise and validate `options`. Returns the validated list (in the caller's
    order -- this is a lettered sequence of buttons, not a set) and the set of letters
    it uses, so `ask` can check `recommended` against exactly what was just validated
    rather than re-deriving it. Raises `InvalidOptions` for anything that doesn't
    match: not a list, too few or too many entries, an entry that isn't a dict with
    exactly `letter` and `label`, a `letter` that isn't one lowercase ASCII character,
    a `letter` repeated within the same decision, or a `label` that is empty or over
    `MAX_LABEL_BYTES`."""
    if not isinstance(options, list) or not (2 <= len(options) <= MAX_OPTIONS):
        raise InvalidOptions()

    validated: list[dict] = []
    letters: set[str] = set()
    for option in options:
        if not isinstance(option, dict) or option.keys() != {"letter", "label"}:
            raise InvalidOptions()
        letter, label = option["letter"], option["label"]
        if not (isinstance(letter, str) and len(letter) == 1 and "a" <= letter <= "z"):
            raise InvalidOptions()
        if letter in letters:
            raise InvalidOptions()
        if not isinstance(label, str) or not label:
            raise InvalidOptions()
        try:
            # A lone surrogate survives JSON decoding but neither `utf-8` nor
            # Postgres `text` can hold one -- exactly the case store._check_size
            # guards against for a top-level field, folded in here because a
            # malformed label is an InvalidOptions concern end to end, never a
            # second, differently-shaped error for the same underlying "options
            # was wrong" fact.
            label_size = len(label.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise InvalidOptions() from exc
        if label_size > MAX_LABEL_BYTES:
            raise InvalidOptions()
        letters.add(letter)
        validated.append({"letter": letter, "label": label})
    return validated, letters


URGENCIES = ("blocking", "by", "when_you_can")
MAX_CONTEXT_CHARS = 1_000
MAX_TOPIC_TITLE_CHARS = 120
MAX_BLOCKED_ON_CHARS = 200
MAX_DEPENDS_ON = 10
MAX_FOR = 5
TOPIC_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class InvalidField(Exception):
    """A malformed question field (context, topic, depends_on, replaces,
    urgency, blocked_on, needed_by, for). Unlike `InvalidOptions` it says what
    to change: these are short, structured values the agent just wrote, and the
    message names the rule, never anything the caller did not send."""

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(field)
        self.field = field
        self.detail = detail


class NotReplaceable(Exception):
    """`replaces` names the caller's own decision, but it is no longer open."""

    def __init__(
        self, decision_id: str, state: str, replaced_by: str | None = None
    ) -> None:
        super().__init__("replaces")
        self.field = "replaces"
        self.decision_id = decision_id
        self.state = state  # 'answered' or 'closed'
        # Set when it was closed BY a replacement (always the caller's own
        # question): a retried ask whose first answer was lost can tell it landed.
        self.replaced_by = replaced_by


def _ids_list(value: object, field: str, limit: int, what: str) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise InvalidField(field, f"{field} must be a list of at most {limit} {what}")
    if not all(isinstance(i, str) and i for i in value) or len(set(value)) != len(
        value
    ):
        raise InvalidField(field, f"{field} must hold distinct, non-empty {what}")
    return list(value)


def _parse_needed_by(value: object) -> datetime:
    parsed = None
    if isinstance(value, str) and "T" in value.upper():
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise InvalidField(
            "needed_by",
            "needed_by must be an ISO-8601 date-time with a timezone,"
            " e.g. 2026-10-22T15:00:00Z or 2026-10-22T17:00:00+02:00",
        )
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class _Fields:
    context: str
    topic_id: str | None
    topic_title: str | None  # None with a topic id: reuse the agent's latest title
    depends_on: list[str]
    replaces: str | None
    urgency: str
    blocked_on: str | None
    needed_by: datetime | None
    for_offices: list[str]


def _validate_fields(
    agent: str,
    *,
    context: object,
    topic: object,
    depends_on: object,
    replaces: object,
    urgency: object,
    blocked_on: object,
    needed_by: object,
    for_offices: object,
) -> _Fields:
    """Shape checks only, in memory. Ownership of depends_on/replaces and the
    topic title lookup need the database and happen in `ask`."""
    if not isinstance(context, str) or len(context) > MAX_CONTEXT_CHARS:
        raise InvalidField(
            "context",
            f"context must be text of at most {MAX_CONTEXT_CHARS} characters",
        )
    topic_id = topic_title = None
    if topic is not None:
        if not isinstance(topic, dict) or not set(topic) <= {"id", "title"}:
            raise InvalidField(
                "topic", 'topic must be {"id": ..., "title": ...} or null'
            )
        tid = topic.get("id")
        if not isinstance(tid, str) or not TOPIC_ID.fullmatch(tid):
            raise InvalidField(
                "topic",
                "topic.id must be a lowercase slug: letters, digits and hyphens,"
                " starting with a letter or digit, at most 64 characters",
            )
        topic_id = tid
        if topic.get("title") is not None:
            title = topic["title"]
            if (
                not isinstance(title, str)
                or not title.strip()
                or len(title) > MAX_TOPIC_TITLE_CHARS
            ):
                raise InvalidField(
                    "topic",
                    f"topic.title must be 1-{MAX_TOPIC_TITLE_CHARS} characters",
                )
            topic_title = title
    deps = _ids_list(depends_on, "depends_on", MAX_DEPENDS_ON, "decision ids")
    if replaces is not None and (not isinstance(replaces, str) or not replaces):
        raise InvalidField("replaces", "replaces must be one decision id or null")
    if urgency not in URGENCIES:
        raise InvalidField("urgency", f"urgency must be one of {', '.join(URGENCIES)}")
    if urgency == "blocking":
        if blocked_on is None:
            raise InvalidField(
                "blocked_on", "urgency 'blocking' needs blocked_on: what has stopped"
            )
    elif blocked_on is not None:
        raise InvalidField("blocked_on", "blocked_on is only for urgency 'blocking'")
    if blocked_on is not None and (
        not isinstance(blocked_on, str)
        or not blocked_on.strip()
        or len(blocked_on) > MAX_BLOCKED_ON_CHARS
    ):
        raise InvalidField(
            "blocked_on", f"blocked_on must be 1-{MAX_BLOCKED_ON_CHARS} characters"
        )
    if urgency == "by" and needed_by is None:
        raise InvalidField(
            "needed_by", "urgency 'by' needs needed_by: when you need the answer"
        )
    needed = _parse_needed_by(needed_by) if needed_by is not None else None
    offices = _ids_list(for_offices, "for", MAX_FOR, "office names")
    for office in offices:
        if not keys.AGENT_NAME.fullmatch(office):
            raise InvalidField(
                "for", "for must list office names: lowercase letters, digits, hyphens"
            )
        if office == agent:
            raise InvalidField("for", "for lists OTHER offices, not the one asking")
    return _Fields(
        context,
        topic_id,
        topic_title,
        deps,
        replaces,
        urgency,
        blocked_on,
        needed,
        offices,
    )


def _row_to_decision(row) -> DecisionRow:
    return DecisionRow(
        id=row[0],
        artifact_id=row[1],
        version_n=row[2],
        question=row[3],
        options=row[4],
        recommended=row[5],
        answer=row[6],
        answer_note=row[7],
        answered_at=row[8].isoformat() if row[8] is not None else None,
        answered_by=row[9],
        created_at=row[10].isoformat(),
        closed_at=row[11].isoformat() if row[11] is not None else None,
        acknowledged_at=row[12].isoformat() if row[12] is not None else None,
        acknowledged_by=row[13],
        answer_source=row[14],
        recorded_by=row[15],
        said_at=row[16].isoformat() if row[16] is not None else None,
        session=row[_SESSION_IDX],
        kind=row[_KIND_IDX],
        review_mode=row[_REVIEW_MODE_IDX],
        context=row[20],
        topic={"id": row[21], "title": row[22]} if row[21] is not None else None,
        depends_on=row[23],
        replaces=row[24],
        replaced_by=row[25],
        urgency=row[26],
        blocked_on=row[27],
        needed_by=(
            row[28].astimezone(UTC).isoformat() if row[28] is not None else None
        ),
        for_offices=row[29],
        reopened_at=row[30].isoformat() if row[30] is not None else None,
        **_set_aside_fields(row),
        answer_record=row[33],
    )


def _set_aside_fields(row) -> dict:
    """`set_aside`, `set_aside_since` and `set_aside_why` for a row read with
    `_COLUMNS`: set aside exactly when it is open and its page is archived or binned
    (binned wins when both)."""
    archived_at, deleted_at = row[31], row[32]
    if (
        row[6] is not None
        or row[8] is not None  # answered_at: a submitted form has no `answer`
        or row[11] is not None
        or (archived_at is None and deleted_at is None)
    ):
        return {"set_aside": False, "set_aside_since": None, "set_aside_why": None}
    since, why = (
        (deleted_at, "binned") if deleted_at is not None else (archived_at, "archived")
    )
    return {
        "set_aside": True,
        "set_aside_since": since.isoformat(),
        "set_aside_why": why,
    }


def held_map(conn, rows: list[DecisionRow]) -> dict[str, list[str]]:
    """For each OPEN question in `rows` that waits on something, the ids it still
    waits on (runcove-5t3oj.39.2). Absent from the result means not held.

    A prerequisite holds only while it is itself open and unanswered. Answered
    releases it. CLOSED WITHOUT AN ANSWER (withdrawn or replaced) also releases it,
    and so does a prerequisite SET ASIDE (its page archived or binned: nobody can
    answer it while the page is away) and an id that no longer resolves: an answer
    that will not come must not hold a question for ever.
    """
    waiting = {
        r.id: r.depends_on
        for r in rows
        if r.depends_on
        and r.answer is None
        and r.closed_at is None
        and r.kind == "question"
    }
    wanted = sorted({pid for ids_ in waiting.values() for pid in ids_})
    if not wanted:
        return {}
    found = conn.execute(
        "select d.id from decision d join artifact a on a.id = d.artifact_id"  # noqa: S608 -- placeholders only
        " where d.answer is null and d.closed_at is null"
        " and a.archived_at is null and a.deleted_at is null"
        f" and d.id in {in_list(wanted)}",
        tuple(wanted),
    ).fetchall()
    unanswered = {r[0] for r in found}
    out = {}
    for rid, deps in waiting.items():
        still = [d for d in deps if d in unanswered]
        if still:
            out[rid] = still
    return out


def _with_held(conn, rows: list[DecisionRow]) -> list[DecisionRow]:
    held = held_map(conn, rows)
    got = receipts.current_map(conn, rows)
    return [
        dataclasses.replace(r, held_by=held.get(r.id, r.held_by), receipt=got.get(r.id))
        for r in rows
    ]


def raw_in(conn, decision_id: str):
    """One decision's raw `_COLUMNS` row on the caller's connection, or None.
    Ownership is the caller's job."""
    return conn.execute(
        f"select {_COLUMNS} from decision where decision.id = %s",  # noqa: S608 -- fixed constant, not caller input
        (decision_id,),
    ).fetchone()


def get_in(conn, decision_id: str) -> DecisionRow:
    """One decision as a row, with its receipt, on the caller's connection."""
    return _with_held(conn, [_row_to_decision(raw_in(conn, decision_id))])[0]


class Held(Exception):
    """An answer to a question that waits on an earlier, unanswered one."""

    def __init__(self, by: list[str]) -> None:
        super().__init__("held")
        self.by = by


# One message for "no such decision" and "not yours", for depends_on and replaces.
_NOT_YOURS = "an id here is not one of your own decisions (unknown ids look the same)"


def _owned_row(conn, agent: str, decision_id: str, *, lock: bool = False):
    """The decision's raw row, only if its page belongs to `agent`; None for a
    missing id and someone else's identically. `lock` takes the row lock a
    close needs, so two askers cannot both replace the same question."""
    return conn.execute(
        f"select {_COLUMNS} from decision"  # noqa: S608 -- fixed constant, not caller input
        " join artifact on artifact.id = decision.artifact_id"
        " where decision.id = %s and artifact.agent = %s"
        # Only questions: a review id answers like an unknown one.
        " and decision.kind = 'question'"
        + (conn.for_update(" of decision") if lock else ""),
        (decision_id, agent),
    ).fetchone()


def ask(
    db: Database,
    *,
    agent: str,
    artifact_id: str,
    question: str,
    options: list[dict],
    recommended: str | None = None,
    session: str | None = None,
    context: str = "",
    topic: dict | None = None,
    depends_on: list | None = None,
    replaces: str | None = None,
    urgency: str = "when_you_can",
    blocked_on: str | None = None,
    needed_by: str | None = None,
    for_offices: list | None = None,
) -> DecisionRow:
    """Pose a lettered question on `artifact_id`. Refuses (`store.NotFound`) an
    artifact `agent` does not own, exactly as `store.patch` does -- someone else's
    page and a missing one must look identical, never a hint either way.

    The question's own fields (context, topic, depends_on, replaces, urgency,
    blocked_on, needed_by, for_offices) are validated here (`InvalidField`).
    `depends_on` and `replaces` must be the caller's own decisions: an unknown id
    and someone else's raise the SAME `InvalidField`. `replaces` must also still
    be open (`NotReplaceable`); asking closes it in the same transaction."""
    store._check_size(
        question, MAX_QUESTION_BYTES, "question", agent=agent, artifact_id=artifact_id
    )
    validated_options, letters = _validate_options(options)
    if recommended is not None and recommended not in letters:
        raise InvalidOptions()
    fields = _validate_fields(
        agent,
        context=context,
        topic=topic,
        depends_on=[] if depends_on is None else depends_on,
        replaces=replaces,
        urgency=urgency,
        blocked_on=blocked_on,
        needed_by=needed_by,
        for_offices=[] if for_offices is None else for_offices,
    )
    # Every free-text field is size-checked (also catching a lone surrogate)
    # before any database work; the scan comes last, below.
    for value, name in (
        (fields.context, "context"),
        (fields.blocked_on or "", "blocked_on"),
        (fields.topic_title or "", "topic"),
    ):
        store._check_size(value, 4_000, name, agent=agent, artifact_id=artifact_id)

    with db.connect() as conn:
        owned = store._owned(conn, artifact_id, agent)
        if owned is None:
            raise store.NotFound()
        version_n = owned[4]

        # The scanner last, on the question and every label that is actually about to
        # be stored -- ownership is confirmed above, so this cost is never spent on
        # behalf of a caller with no right to the artifact.
        store._check_secrets(question, "question", agent=agent, artifact_id=artifact_id)
        for option in validated_options:
            store._check_secrets(
                option["label"], "options", agent=agent, artifact_id=artifact_id
            )
        for value, name in (
            (fields.context, "context"),
            (fields.blocked_on, "blocked_on"),
            (fields.topic_title, "topic"),
        ):
            if value:
                store._check_secrets(value, name, agent=agent, artifact_id=artifact_id)

        decision_id = ids.new_id()
        emissions: list[events.Emission] = []
        # The artifact row lock a section update holds while it checks that
        # it drops no open question's slot (store.patch_section): taken here
        # so a question created mid-update is seen by that check or created
        # after the write -- never between the two.
        with conn.transaction():
            conn.execute(
                "select 1 from artifact where id = %s" + conn.for_update(),  # noqa: S608 -- clause from the db layer, no caller input
                (artifact_id,),
            )
            title = fields.topic_title
            if fields.topic_id is not None and title is None:
                # A topic id this agent used before keeps its latest title.
                found = conn.execute(
                    "select decision.topic_title from decision"
                    " join artifact on artifact.id = decision.artifact_id"
                    " where artifact.agent = %s and decision.topic_id = %s"
                    " and decision.topic_title is not null"
                    " order by decision.created_at desc, decision.id desc limit 1",
                    (agent, fields.topic_id),
                ).fetchone()
                if found is None:
                    raise InvalidField(
                        "topic",
                        "a topic id you have not used before needs a title:"
                        ' {"id": ..., "title": ...}',
                    )
                title = found[0]
            # The same refusal for an unknown id and someone else's.
            for dep in fields.depends_on:
                if _owned_row(conn, agent, dep) is None:
                    raise InvalidField("depends_on", _NOT_YOURS)
            old = None
            if fields.replaces is not None:
                old = _owned_row(conn, agent, fields.replaces, lock=True)
                if old is None:
                    raise InvalidField("replaces", _NOT_YOURS)
                if old[6] is not None:  # answer
                    raise NotReplaceable(fields.replaces, "answered")
                if old[11] is not None:  # closed_at
                    raise NotReplaceable(fields.replaces, "closed", old[25])
            conn.execute(
                # `session` is which SESSION raised the question -- a label the
                # caller declared, never a permission (see api.session). It is what
                # lets a seat sharing a key with two others recognise its own
                # question in a feed, and what lets the answer wake the right one.
                "insert into decision"
                " (id, artifact_id, version_n, question, options, recommended, session,"
                " context, topic_id, topic_title, depends_on, replaces, urgency,"
                " blocked_on, needed_by, for_offices)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    decision_id,
                    artifact_id,
                    version_n,
                    question,
                    jsonb(validated_options),
                    recommended,
                    session,
                    fields.context,
                    fields.topic_id,
                    title if fields.topic_id is not None else None,
                    jsonb(fields.depends_on),
                    fields.replaces,
                    fields.urgency,
                    fields.blocked_on,
                    fields.needed_by,
                    jsonb(fields.for_offices),
                ),
            )
            # The partner is told in the same transaction (partner.py): a rolled
            # back ask leaves no event. Nothing at all when the bridge is off.
            asked_body = partner.asked_body(
                asking_office=agent,
                title=question,
                options=validated_options,
                recommended=recommended,
                context=fields.context,
                link=partner.link_for(artifact_id, decision_id),
                topic=(
                    {"id": fields.topic_id, "title": title}
                    if fields.topic_id is not None
                    else None
                ),
                depends_on=fields.depends_on,
                replaces=fields.replaces,
                urgency=fields.urgency,
                blocked_on=fields.blocked_on,
                needed_by=fields.needed_by,
                for_offices=fields.for_offices,
            )
            emissions.append(
                events.emit(
                    conn,
                    events.Event(
                        kind="asked",
                        artifact_id=artifact_id,
                        agent=agent,
                        actor=agent,
                        ref_id=decision_id,
                        session=session,
                        data={"body": asked_body},
                    ),
                )
            )
            # Asked on a page that is already archived or binned: set aside from
            # birth. The hub is told straight after `asked` (and takes the Slack
            # buttons away), and the agent gets the same `decision_set_aside` event
            # an archive would have sent, or the unarchive's `decision_reopened`
            # would arrive for a question it was never told was parked.
            away = conn.execute(
                "select archived_at is not null, deleted_at is not null"
                " from artifact where id = %s",
                (artifact_id,),
            ).fetchone()
            if away and (away[0] or away[1]):
                what = "binned" if away[1] else "archived"
                emissions.append(
                    events.emit(
                        conn,
                        events.Event(
                            kind="set_aside",
                            artifact_id=artifact_id,
                            agent=agent,
                            actor=agent,
                            ref_id=decision_id,
                            data={
                                "body": {
                                    "reason": f"The page holding this question was {what}."
                                }
                            },
                        ),
                    )
                )
                events.append(
                    conn,
                    artifact_id=artifact_id,
                    agent=agent,
                    actor=agent,
                    kind="decision_set_aside",
                    summary=f"{what}: {question}",
                    ref_id=decision_id,
                    note=(
                        f"The page holding this question is {what}, so the question "
                        f"is SET ASIDE from the start -- {settings.current().owner_name} "
                        "is not shown it and it "
                        "cannot be answered. It is NOT closed: if the page is "
                        "unarchived or restored it comes back open with the same id, "
                        "and you will get a decision_reopened event."
                    ),
                    session=session,
                )
            if old is not None:
                closed = _close_row(conn, agent, old, replaced_by=decision_id)
                if closed is not None:
                    emissions.append(closed)
        for emission in emissions:
            emission.fire()
        created = conn.execute(
            f"select {_COLUMNS} from decision where decision.id = %s",  # noqa: S608 -- fixed constant, not caller input
            (decision_id,),
        ).fetchone()
        return _with_held(conn, [_row_to_decision(created)])[0]


def ask_review(
    db: Database,
    *,
    agent: str,
    artifact_id: str,
    session: str | None = None,
    mode: str | None = None,
) -> DecisionRow:
    """Ask Jeremy to review `artifact_id`: a decision of kind 'review' with the
    four fixed verdicts. Same ownership rule as `ask`: not yours and missing
    look identical.

    `mode` is 'held' or 'live': the agent's own choice for this review. Left
    out, the review takes Jeremy's remembered preference."""
    if mode is not None and mode not in holds.MODES:
        raise ValueError(mode)
    with db.connect() as conn:
        owned = store._owned(conn, artifact_id, agent)
        if owned is None:
            raise store.NotFound()
        mode = mode or holds.get_mode(conn)
        # The page's own stored title, scanned when it was published, so it needs
        # no second scan here.
        title, version_n = owned[1], owned[4]
        question = f"Review requested: {title}"[:MAX_QUESTION_BYTES]
        decision_id = ids.new_id()
        with conn.transaction():
            # The same artifact row lock `ask` takes (see there).
            conn.execute(
                "select 1 from artifact where id = %s" + conn.for_update(),  # noqa: S608 -- clause from the db layer, no caller input
                (artifact_id,),
            )
            conn.execute(
                "insert into decision (id, artifact_id, version_n, question, options,"
                " recommended, session, kind, review_mode)"
                " values (%s, %s, %s, %s, %s, null, %s, 'review', %s)",
                (
                    decision_id,
                    artifact_id,
                    version_n,
                    question,
                    jsonb(REVIEW_OPTIONS),
                    session,
                    mode,
                ),
            )
            # Round 1 is born with the review, at the version he will see.
            conn.execute(
                "insert into review_round (review_id, n, version_n, base_version)"
                " values (%s, 1, %s, null)",
                (decision_id, version_n),
            )
        created = conn.execute(
            f"select {_COLUMNS} from decision where decision.id = %s",  # noqa: S608 -- fixed constant, not caller input
            (decision_id,),
        ).fetchone()
    return _row_to_decision(created)


def list_for_artifact(
    db: Database,
    artifact_id: str,
    *,
    include_answered: bool = False,
    include_closed: bool = False,
) -> list[DecisionRow]:
    """Oldest first -- the order Jeremy reads them on the page, top to bottom in the
    order they were asked, not the order they were last touched. `include_answered`
    hides an already-answered decision by default, the same shape
    `comments.list_for_artifact`'s `include_resolved` uses for a resolved thread;
    `include_closed` hides a withdrawn one regardless. Ownership is the caller's job
    -- this function trusts `artifact_id` outright, exactly as `store`'s and
    `comments`'s own listing helpers do."""
    sql = f"select {_COLUMNS} from decision where decision.artifact_id = %s"  # noqa: S608 -- fixed constant, not caller input
    if not include_answered:
        sql += " and decision.answer is null and decision.answer_record is null"
    if not include_closed:
        sql += " and decision.closed_at is null"
    sql += " order by decision.created_at asc, decision.id asc"
    with db.connect() as conn:
        rows = conn.execute(sql, (artifact_id,)).fetchall()
        return _with_held(conn, [_row_to_decision(row) for row in rows])


# The one clause every "what waits on him" list adds: not (open on an archived or
# binned page). `artifact` must be joined. See `_set_aside_fields` for the same rule
# read off a row.
_NOT_SET_ASIDE = (
    " and not (decision.answered_at is null and decision.closed_at is null"
    " and (artifact.archived_at is not null or artifact.deleted_at is not null))"
)

# The paging key of the two decision lists: (created_at, id), the order each is
# already sorted in, so a page boundary can neither skip nor repeat a row even
# when two questions share a timestamp. A row-value comparison against the
# cursor row's own columns, so no timestamp is ever formatted or parsed here and
# the SQL reads the same on Postgres and SQLite. A cursor naming no row would
# compare as NULL and return an empty page -- indistinguishable from "the end" --
# so the route checks it with `exists` first.
_AFTER = (
    " and (decision.created_at, decision.id) {op}"
    " (select cur.created_at, cur.id from decision cur where cur.id = %s)"
)


def exists(db: Database, decision_id: str, *, agent: str | None = None) -> bool:
    """Does this decision exist (and, with `agent`, sit on a page that agent
    owns)? For validating a paging cursor; 'not yours' reads as 'no such'."""
    sql = (
        "select 1 from decision"
        " join artifact on artifact.id = decision.artifact_id"
        " where decision.id = %s"
    )
    params: list[object] = [decision_id]
    if agent is not None:
        sql += " and artifact.agent = %s"
        params.append(agent)
    with db.connect() as conn:
        return conn.execute(sql, tuple(params)).fetchone() is not None


def list_for_agent(
    db: Database,
    agent: str,
    *,
    include_answered: bool = False,
    include_closed: bool = False,
    limit: int = 200,
    kind: str | None = None,
    after: str | None = None,
    include_set_aside: bool = False,
) -> list[DecisionRow]:
    """Newest first -- this is the polling surface an agent uses to learn Jeremy's
    answer, so newest matters more than oldest, exactly as `comments.list_for_agent`
    is ordered for the same reason. `limit` protects an agent with a long history
    from pulling its entire back catalogue on every poll.

    `include_closed` exists as of 21 Sep 2026 and did not before. The reason it
    did not was written here in full -- "the agent withdrew it, so there is
    nothing left here for that agent to learn" -- and that reason STOPPED BEING
    TRUE the same morning, when Jeremy ruled that archiving or deleting a page
    withdraws its open questions. (On 2 Nov 2026 he changed that rule: they are SET
    ASIDE now, not closed, and `include_set_aside` is how an agent reads them.) The
    SERVICE withdrew them, not the agent, so the agent did not know and there was a
    great deal left to learn. A
    withdrawn question was then unreadable on every route a polling agent could
    reach: not wrong, not redacted, simply no longer listed (homelab-ij8.44,
    reported independently by two seats on 19 Sep for a different route).

    Still excluded by DEFAULT, because a withdrawn question is not waiting on
    anybody and a poll asking "what is outstanding" should not be handed it. A SET
    ASIDE question (open, its page archived) is left out the same way unless
    `include_set_aside`: a poll for "what is waiting on him" must not count it.

    `after` is a decision id from the previous page: the rows that come after it
    in this order (older than it, here). The route asks for `limit + 1` to learn
    whether it cut the list, and pages with the last id it returned."""
    sql = (
        f"select {_COLUMNS} from decision"  # noqa: S608 -- fixed constant, not caller input
        " join artifact on artifact.id = decision.artifact_id"
        # A deleted page is gone from the agent's view (ij8.110, as ij8.97 did
        # for the uncollected alert): listing its questions while their
        # outcome route answered 404 was the list and the route disagreeing.
        " where artifact.agent = %s and artifact.deleted_at is null"
    )
    if include_set_aside:
        # Except its OPEN questions, which `artifacts-decide list` shows in its SET
        # ASIDE section as "page binned" (2 Nov 2026); answered or closed rows on a
        # binned page stay out as before.
        sql = sql.replace(
            "artifact.deleted_at is null",
            "(artifact.deleted_at is null or (decision.answered_at is null"
            " and decision.closed_at is null))",
        )
    if not include_closed:
        # A submitted form the agent has not acknowledged stays listed even when its
        # page was republished without the form (which closes it): the answer must
        # not vanish from the queue before the agent has seen it.
        sql += (
            " and (decision.closed_at is null or (decision.kind = 'form'"
            " and decision.answer_record is not null"
            " and decision.acknowledged_at is null))"
        )
    if not include_answered:
        sql += " and decision.answer is null and decision.answer_record is null"
    if not include_set_aside:
        sql += _NOT_SET_ASIDE
    params: list[object] = [agent]
    if kind is not None:
        # In the query, so `limit` counts only this kind.
        sql += " and decision.kind = %s"
        params.append(kind)
    else:
        # An unsubmitted form is not a question waiting on him (design
        # 2026-10-29 §6): it reads back through the page's forms. A submitted one
        # is an answer, and `wait` finds it here.
        sql += " and (decision.kind <> 'form' or decision.answered_at is not null)"
    if after is not None:
        sql += _AFTER.format(op="<")
        params.append(after)
    sql += " order by decision.created_at desc, decision.id desc limit %s"
    with db.connect() as conn:
        rows = conn.execute(sql, (*params, limit)).fetchall()
        return _with_held(conn, [_row_to_decision(row) for row in rows])


# Where an answer can have come from. 'page' is a tap in the gallery; the rest
# are places Jeremy actually says things. Enumerated rather than free text so a
# reader can switch on it, and so a seat cannot invent a source that sounds more
# authoritative than it is.
ANSWER_SOURCES = ("terminal", "telegram", "in-person", "other")
# 'slack' is stored (the partner bridge, inbound) but is NOT in the set above: a
# relay is an agent's word, and 'slack' means quarterdeck verified who pressed.
# Only `answer(..., source='slack')`, called from partner_in, writes it.
STORED_SOURCES = ("page", *ANSWER_SOURCES, "slack")


def require_stored_source(source: str) -> None:
    """Refuse an `answer_source` that is not one of `STORED_SOURCES`. The column had a
    CHECK naming these until slice 5 of the core-and-plugins design; the check lives here
    now, where a plug-in's value can join the set without a migration. Called at every
    write of the column, before the SQL, so a bad value never reaches the table."""
    if source not in STORED_SOURCES:
        raise InvalidSource(source)


class InvalidSource(Exception):
    """A relay named a source that is not one of ANSWER_SOURCES, or named 'page'.

    'page' is refused deliberately: it is the one value that means "he tapped
    this himself", and a relay claiming it would be indistinguishable from the
    real thing — which is the entire failure this call exists to prevent.
    """


_ROUND_COLUMNS = (
    "review_id, n, version_n, base_version, opened_at, closed_at, verdict,"
    " reviewed_version"
)
_R_ROUND_COLUMNS = ", ".join("r." + c.strip() for c in _ROUND_COLUMNS.split(","))


def _row_to_round(row) -> RoundRow:
    return RoundRow(
        review_id=row[0],
        n=row[1],
        version_n=row[2],
        base_version=row[3],
        opened_at=row[4].isoformat(),
        closed_at=row[5].isoformat() if row[5] else None,
        verdict=row[6],
        reviewed_version=row[7],
    )


def current_round(conn, review_id: str) -> RoundRow | None:
    """The review's latest round, open or closed. None for a question."""
    found = conn.execute(
        f"select {_ROUND_COLUMNS} from review_round"  # noqa: S608 -- fixed constant
        " where review_id = %s order by n desc limit 1",
        (review_id,),
    ).fetchone()
    return _row_to_round(found) if found is not None else None


def _round(conn, review_id: str, n: int) -> RoundRow | None:
    found = conn.execute(
        f"select {_ROUND_COLUMNS} from review_round"  # noqa: S608 -- fixed constant
        " where review_id = %s and n = %s",
        (review_id, n),
    ).fetchone()
    return _row_to_round(found) if found is not None else None


def current_round_numbers(db: Database, review_ids: list[str]) -> dict[str, int]:
    """{review id: its current round's n}, for the verdict forms."""
    if not review_ids:
        return {}
    with db.connect() as conn:
        return dict(
            conn.execute(
                "select review_id, max(n) from review_round"  # noqa: S608 -- placeholders only
                f" where review_id in {in_list(review_ids)} group by review_id",
                tuple(review_ids),
            ).fetchall()
        )


def _already(conn, decision_row, asked_round: int | None = None) -> AlreadyDecided:
    """The refusal for a verdict that does not count, naming the round whose
    verdict did: the one the form was for when that round is over, else the
    current one."""
    row = _row_to_decision(decision_row[: len(_COLUMNS.split(","))])
    current = current_round(conn, row.id)
    named = current
    if (
        asked_round is not None
        and current is not None
        and asked_round != current.n
        and (earlier := _round(conn, row.id, asked_round)) is not None
    ):
        named = earlier
    return AlreadyDecided(row, round=named, current=current)


def _require_live_page(conn, decision_id: str, artifact_id: str) -> None:
    """Take the decision row `for update`, THEN read the page in a fresh statement,
    and refuse if it is archived or binned. Archive and bin lock the page's open
    decision rows first (`store._lock_open_decisions`) and the artifact after, so
    this takes them in the same order: row, then artifact, never the reverse. The
    point is the order of the read: the write's own `exists (...)` subselect keeps
    the snapshot the UPDATE began with, and under READ COMMITTED an UPDATE that
    waited on a concurrent archive's row lock rechecks only its target row, so
    that subselect still sees the page live. A new statement issued after the lock
    is granted sees the archive's commit."""
    conn.execute(
        "select id from decision where id = %s" + conn.for_update(),  # noqa: S608 -- clause from the db layer, no caller input
        (decision_id,),
    ).fetchall()
    page = conn.execute(
        "select archived_at, deleted_at from artifact where id = %s",
        (artifact_id,),
    ).fetchone()
    if page is None or page[0] is not None or page[1] is not None:
        raise store.NotFound()


def _lock_review_and_settle_letter(
    conn,
    decision_id: str,
    artifact_id: str,
    letter: str,
    asked_round: int | None = None,
) -> str:
    """First step of a review verdict's transaction. Takes the decision row
    `for update`, so a flip to live (which takes the same lock) and a verdict
    serialise on it, then re-tests the row under the lock. Approved becomes
    Approved-with-notes when marks of his are still held: his marks reach the
    agent with the verdict, so an Approved that carried them must not read as
    "nothing to say".

    `asked_round` is the round the verdict was given for (the form's hidden
    field, or a relay's `round`). A republish that opened a later round has
    cleared the answer, so without this a stale tab's verdict for round 1
    would land on round 2; tested under the same lock the republish takes."""
    current = conn.execute(
        f"select {_COLUMNS} from decision where decision.id = %s"  # noqa: S608 -- fixed constant, not caller input
        + conn.for_update(),
        (decision_id,),
    ).fetchone()
    if current[11] is not None:  # closed_at
        raise store.NotFound()
    if current[6] is not None:  # answer
        raise _already(conn, current, asked_round)
    if asked_round is not None:
        now = current_round(conn, decision_id)
        if now is not None and now.n != asked_round:
            raise _already(conn, current, asked_round)
    return holds.approved_letter(conn, artifact_id, letter)


def _close_round(
    conn, decision_id: str, letter: str, version: int | None = None
) -> None:
    """The verdict closes the open round, inside the verdict's transaction.
    `version` is the one his page view showed, when the form said.

    A session holding a LIVE lease is the one that acts on the verdict, so its
    lease is stretched to CLOSED_CLAIM_SECONDS here: a five-minute lease left
    alone would lapse mid-revision and let a second session take the closed
    round and act on the same verdict. A lapsed or absent lease is left as is."""
    now = datetime.now(UTC)
    stretched = now + timedelta(seconds=CLOSED_CLAIM_SECONDS)
    conn.execute(
        "update review_round set closed_at = %s, verdict = %s,"
        " reviewed_version = %s,"
        " claim_expires_at = case"
        "   when claimed_by is null or claim_expires_at <= %s"
        "   then claim_expires_at"
        # The later of the two, without greatest() (SQLite's max() differs).
        "   when claim_expires_at > %s then claim_expires_at"
        "   else %s end"
        " where review_id = %s and closed_at is null",
        (now, VERDICTS[letter], version, now, stretched, stretched, decision_id),
    )


def _advance_rounds(conn, artifact_id: str, version_n: int) -> None:
    """A real new version of the page has just been written, in the caller's
    transaction. For each open review on the page, locked `for update` first
    (decision, then round -- the order a verdict takes):

    * latest round closed with changes requested: open round n+1 at this
      version, based on the version the changes were asked on, and clear the
      decision's answer so the review waits on him again. Mode carries over.
    * latest round still open: it now reviews this version; its base stays.
    * otherwise (approved, with notes, dismissed): the review is over.

    During a rollout an older pod may have asked a review without writing its
    round 1, or given a verdict without closing the round. Both are repaired
    here from the decision row first, then the rule above applies.
    """
    reviews = conn.execute(
        "select id, answer, version_n from decision where artifact_id = %s"  # noqa: S608 -- clause from the db layer, no caller input
        " and kind = 'review' and closed_at is null order by id" + conn.for_update(),
        (artifact_id,),
    ).fetchall()
    for review_id, answered, asked_on in reviews:
        latest = current_round(conn, review_id)
        if latest is None:
            conn.execute(
                "insert into review_round (review_id, n, version_n, base_version)"
                " values (%s, 1, %s, null)",
                (review_id, asked_on),
            )
            latest = current_round(conn, review_id)
        if latest.closed_at is None and answered is not None:
            _close_round(conn, review_id, answered)
            latest = current_round(conn, review_id)
        if latest.closed_at is None:
            conn.execute(
                "update review_round set version_n = %s"
                " where review_id = %s and n = %s",
                (version_n, review_id, latest.n),
            )
        elif latest.verdict == "changes_requested":
            conn.execute(
                "insert into review_round (review_id, n, version_n, base_version)"
                " values (%s, %s, %s, %s)",
                (
                    review_id,
                    latest.n + 1,
                    version_n,
                    # The version he was shown when he sent it back; a relay
                    # did not say, so the round's own version stands in.
                    latest.reviewed_version
                    if latest.reviewed_version is not None
                    else latest.version_n,
                ),
            )
            # The earlier verdict lives on in review_round and the journal.
            conn.execute(
                "update decision set answer = null, answer_note = '',"
                " answered_at = null, answered_by = null, answer_source = null,"
                " recorded_by = null, said_at = null, acknowledged_at = null,"
                " acknowledged_by = null where id = %s",
                (review_id,),
            )
            receipts.void(conn, review_id)


# CLAIMS (page review step 5). One session acts on a round: `wait --review`
# claims it with a lease and renews it while it runs, so a second session
# waiting on the same review is told who has it instead of acting twice. Five
# minutes: long enough that a renewal every few holds never lapses, short
# enough that a waiter that died frees the round before anyone is waiting on it.
CLAIM_SECONDS = 300

# A round that already has its verdict is held for an hour instead. The holder
# is revising, and a revision routinely outlasts five minutes; with the short
# lease a second session could take the closed round and act on the same
# verdict. An hour covers a normal revision and still frees itself if the
# holder dies, whereas a claim that never expired would jam the review. The
# holder's renewals, a release or the next round free it earlier.
CLOSED_CLAIM_SECONDS = 3600

# A handed-back round says why in a line on his card, not a paragraph.
MAX_RELEASE_REASON_BYTES = 500


@dataclass(frozen=True)
class ClaimRow:
    """Who holds a round's lease, and until when. `claimed_by` is None for a
    round nobody holds."""

    claimed_by: str | None
    claim_expires_at: str | None
    round: int
    # How long the lease just taken lasts, for a client timing its renewal.
    lease_seconds: int | None = None


class Claimed(Exception):
    """Another session holds a live lease on the round."""

    def __init__(self, claim: ClaimRow) -> None:
        super().__init__("claimed by another session")
        self.claim = claim


class MissingReason(Exception):
    """A release with no reason: handing a round back says why, or his card
    would say only that somebody walked away."""


def _lock_current_round(conn, review_id: str):
    """The review's latest round, locked: (n, claimed_by, claim_expires_at,
    lease live, has a verdict). Called under the decision row's lock (decision,
    then round). A review asked before rounds existed gets its round 1 here,
    repaired from the decision row exactly as `_advance_rounds` does."""
    sql = (
        "select n, claimed_by, claim_expires_at,"  # noqa: S608 -- clause from the db layer, no caller input
        " claimed_by is not null and claim_expires_at > %s,"
        " closed_at is not null"
        " from review_round where review_id = %s order by n desc limit 1"
        + conn.for_update()
    )
    found = conn.execute(sql, (datetime.now(UTC), review_id)).fetchone()
    if found is not None:
        return found
    answered, asked_on = conn.execute(
        "select answer, version_n from decision where id = %s", (review_id,)
    ).fetchone()
    conn.execute(
        "insert into review_round (review_id, n, version_n, base_version)"
        " values (%s, 1, %s, null)",
        (review_id, asked_on),
    )
    if answered is not None:
        _close_round(conn, review_id, answered)
    return conn.execute(sql, (datetime.now(UTC), review_id)).fetchone()


def _lock_owned_review(conn, agent: str, review_id: str) -> str:
    """Take the decision row `for update`, only if it is a review on a page
    `agent` owns; its page id. NotFound covers missing, not a review and not
    yours alike."""
    found = conn.execute(
        "select decision.artifact_id from decision"  # noqa: S608 -- clause from the db layer, no caller input
        " join artifact on artifact.id = decision.artifact_id"
        " where decision.id = %s and artifact.agent = %s"
        " and decision.kind = 'review'" + conn.for_update(" of decision"),
        (review_id, agent),
    ).fetchone()
    if found is None:
        raise store.NotFound()
    return found[0]


def _holder(row) -> ClaimRow:
    return ClaimRow(
        claimed_by=row[1],
        claim_expires_at=row[2].isoformat() if row[2] else None,
        round=row[0],
    )


def claim_round(db: Database, agent: str, review_id: str, session: str) -> ClaimRow:
    """Claim the review's current round for `session`, for CLAIM_SECONDS.

    Succeeds when the round is unclaimed, its lease has run out, or `session`
    already holds it (a restarted waiter re-claims its own lease); raises
    `Claimed` naming the holder otherwise. Atomic under the decision row's
    lock, so two sessions racing get exactly one winner. Clears any
    handed-back reason: somebody is acting on it again."""
    with db.connect() as conn, conn.transaction():
        _lock_owned_review(conn, agent, review_id)
        current = _lock_current_round(conn, review_id)
        if current[3] and current[1] != session:
            raise Claimed(_holder(current))
        seconds = CLOSED_CLAIM_SECONDS if current[4] else CLAIM_SECONDS
        row = conn.execute(
            "update review_round set claimed_by = %s,"
            " claim_expires_at = %s,"
            " released_reason = null, released_by = null"
            " where review_id = %s and n = %s"
            " returning n, claimed_by, claim_expires_at",
            (
                session,
                datetime.now(UTC) + timedelta(seconds=seconds),
                review_id,
                current[0],
            ),
        ).fetchone()
    return dataclasses.replace(_holder(row), lease_seconds=seconds)


def release_round(
    db: Database, agent: str, review_id: str, session: str, reason: str
) -> ClaimRow:
    """Hand the current round back with a reason: the lease is cleared and the
    reason kept for his card. Refused with `Claimed` when ANOTHER session holds
    a live lease; releasing an unheld round just records why. No event: the
    agent released it itself, so there is nobody to tell."""
    store._check_size(
        reason, MAX_RELEASE_REASON_BYTES, "reason", agent=agent, artifact_id=None
    )
    if not reason.strip():
        raise MissingReason()
    with db.connect() as conn:
        artifact_id = conn.execute(
            "select decision.artifact_id from decision"
            " join artifact on artifact.id = decision.artifact_id"
            " where decision.id = %s and artifact.agent = %s"
            " and decision.kind = 'review'",
            (review_id, agent),
        ).fetchone()
        if artifact_id is None:
            raise store.NotFound()
        # Scanned like an outcome: it is read on his page.
        store._check_secrets(reason, "reason", agent=agent, artifact_id=artifact_id[0])
        with conn.transaction():
            _lock_owned_review(conn, agent, review_id)
            current = _lock_current_round(conn, review_id)
            if current[3] and current[1] != session:
                raise Claimed(_holder(current))
            row = conn.execute(
                "update review_round set claimed_by = null,"
                " claim_expires_at = null, released_reason = %s,"
                " released_by = %s"
                " where review_id = %s and n = %s"
                " returning n, claimed_by, claim_expires_at",
                (reason, session, review_id, current[0]),
            ).fetchone()
    return _holder(row)


def record_elsewhere(
    db: Database,
    decision_id: str,
    *,
    letter: str,
    note: str,
    source: str,
    recorded_by: str,
    said_at: datetime | None = None,
    round: int | None = None,
) -> DecisionRow:
    """Record an answer Jeremy gave somewhere that is not the page.

    homelab-ij8.78. He answers in a terminal or on Telegram as often as on a
    page. Before this there was nothing honest a seat could do with those words:
    pressing the button for him mints an answer attributed to him, ON THE PAGE,
    indistinguishable from one he tapped — the kind of record that gets believed
    later by an agent that was not there. Two seats hit exactly this on 20 Sep
    2026 and both refused, correctly, leaving four real answers stranded.

    So the answer is recorded and the decision closes honestly, but it is
    recorded as WHAT IT IS. Three things make that true rather than merely
    intended:

      * `source` is a value from a fixed set and 'page' is refused. A relay
        cannot claim to be a tap, by accident or otherwise.
      * `recorded_by` is required. An anonymous relay is the same forgery
        wearing a different hat; the accountable seat is part of the record.
      * `note` carries HIS WORDS, and the caller is expected to paste them
        verbatim. Every one of his notes so far has qualified the letter it came
        with — one "(a)" read "start with (b) but then (a) if we can" — so a
        paraphrase here is a paraphrase of a decision.

    `said_at` is when he said it; `answered_at` stays "when this row gained its
    answer", so the uncollected alert and the event journal need no special case.

    Refuses a closed decision, like `answer` does, and for the same reason: the
    agent that closed it said it no longer needs one. A relay on an already-answered
    REVIEW is refused too: the first verdict counts, relayed or tapped.
    `round`, when given, must be the review's current round (see `answer`).
    """
    if source not in ANSWER_SOURCES:
        raise InvalidSource(source)
    require_stored_source(source)
    if not recorded_by:
        raise InvalidSource("a relayed answer must name the seat recording it")
    store._check_size(note, MAX_NOTE_BYTES, "note", agent=recorded_by, artifact_id=None)

    emissions: list[events.Emission] = []
    with db.connect() as conn:
        row = conn.execute(
            f"select {_COLUMNS}, artifact.title, artifact.agent from decision"  # noqa: S608 -- fixed constant, not caller input
            " join artifact on artifact.id = decision.artifact_id"
            " where decision.id = %s and decision.closed_at is null",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise store.NotFound()
        artifact_id, options, owning_agent = row[1], row[4], row[-1]
        # The session that ASKED, exactly as a tapped answer carries it (see
        # `answer`). Missing until 23 Sep 2026: a relayed answer's event had no
        # session, so a waiter listening for its own session never heard it --
        # found by the first outside agent (homelab-ij8.106).
        asking_session = row[_SESSION_IDX]

        if _set_aside_fields(row)["set_aside"]:
            raise store.NotFound()
        if letter not in {option["letter"] for option in options}:
            raise InvalidAnswer()
        store._check_secrets(note, "note", agent=recorded_by, artifact_id=artifact_id)

        with conn.transaction():
            _require_live_page(conn, decision_id, artifact_id)
            if row[_KIND_IDX] == "review":
                letter = _lock_review_and_settle_letter(
                    conn, decision_id, artifact_id, letter, round
                )
            label = next(o["label"] for o in options if o["letter"] == letter)
            summary = f"{letter}: {label} (relayed from {source})"[:200]
            # One verdict counts on a review, relayed or tapped: the same
            # atomic guard `answer` uses, so a relay cannot overwrite it.
            answered_at = datetime.now(UTC)
            written = conn.execute(
                "update decision set answer = %s, answer_note = %s,"
                " answered_at = %s, answered_by = %s,"
                " answer_source = %s, recorded_by = %s, said_at = %s"
                " where id = %s and (kind <> 'review' or answer is null)"
                # Live page at the write, as in `answer`: a relay is no more
                # storable on a set-aside question than a tap is.
                " and exists (select 1 from artifact a"
                " where a.id = decision.artifact_id"
                " and a.archived_at is null and a.deleted_at is null)",
                (
                    letter,
                    note,
                    answered_at,
                    settings.owner_id(),
                    source,
                    recorded_by,
                    said_at,
                    decision_id,
                ),
            )
            if written.rowcount == 0:
                current = conn.execute(
                    f"select {_COLUMNS} from decision where decision.id = %s",  # noqa: S608 -- fixed constant, not caller input
                    (decision_id,),
                ).fetchone()
                if current is None or _set_aside_fields(current)["set_aside"]:
                    raise store.NotFound()
                raise _already(conn, current, round)
            # A new human act: what was declared about an earlier answer (or before
            # any) is history, and the acknowledgement reads as new again.
            conn.execute(
                "update decision set acknowledged_at = null, acknowledged_by = null"
                " where id = %s",
                (decision_id,),
            )
            receipts.void(conn, decision_id)
            if row[_KIND_IDX] == "review":
                _close_round(conn, decision_id, letter)
                # His verdict carries every mark in its message, so releasing
                # the held ones writes no events of their own.
                holds.release(conn, artifact_id, emit=False)
            else:
                emissions.append(
                    events.emit(
                        conn,
                        events.Event(
                            kind="answered",
                            artifact_id=artifact_id,
                            agent=owning_agent,
                            actor=recorded_by,
                            ref_id=decision_id,
                            session=asking_session,
                            data={
                                "body": partner.answered_body(
                                    letter=letter,
                                    note=note,
                                    answered_by=settings.owner_id(),
                                    answered_at=answered_at,
                                    answer_source=source,
                                    recorded_by=recorded_by,
                                    said_at=said_at,
                                ),
                                "at": answered_at.isoformat(),
                            },
                        ),
                    )
                )
            # Journalled like a tapped answer, because to the OWNING agent the
            # fact is the same one it has been waiting for: Jeremy decided. The
            # summary names the relay so a reader of the feed is not misled, and
            # the row itself carries the full provenance for anyone who looks.
            events.append(
                conn,
                agent=owning_agent,
                actor=recorded_by,
                kind="decision_answered",
                artifact_id=artifact_id,
                ref_id=decision_id,
                summary=summary,
                # HIS WORDS TRAVEL WITH THE EVENT. Without this the feed says
                # only "b: Let it go" and every consumer learns the letter while
                # losing the thing that qualifies it. That cost a real afternoon
                # on 20 Sep 2026.
                note=note,
                session=asking_session,
            )
            refreshed = conn.execute(
                f"select {_COLUMNS} from decision where id = %s",  # noqa: S608 -- fixed constant
                (decision_id,),
            ).fetchone()
    for emission in emissions:
        emission.fire()
    return _row_to_decision(refreshed)


def answer(
    db: Database,
    decision_id: str,
    *,
    letter: str,
    note: str = "",
    by: str,
    round: int | None = None,
    version: int | None = None,
    source: str = "page",
    recorded_by: str | None = None,
    in_transaction: Callable[[Connection], None] | None = None,
) -> DecisionRow:
    """The gallery, acting for Jeremy, answers a decision. Takes no `agent`: this is
    not a call an agent ever makes, so there is no ownership to check against a
    caller-supplied identity -- only whether `decision_id` names an open (not closed)
    decision at all. `by` is always the owner id (`settings.owner_id`); the gallery hardcodes it rather
    than trusting any identity header (see `gallery.py`), and this function stores
    whatever it is given without questioning it further.

    Re-answering a QUESTION is allowed and REPLACES the previous answer -- Jeremy may
    change his mind, and a 409 here would send him to a terminal to do it, which is
    exactly the kind of friction this whole feature exists to remove. `answered_at`
    moves with every call, including a second one that picks the same letter again.
    A REVIEW is the exception: its first verdict counts and a second is refused
    (`AlreadyAnswered`, a 409 at the gallery), so an agent never acts on one verdict
    and then finds another. Refuses
    (`store.NotFound`) a decision that does not exist or is already closed: the agent
    that closed it said it no longer needs an answer, and a stale form resubmission
    must not manufacture one anyway.

    `round` is the review round the verdict form was drawn for. A republish
    after changes requested opens the next round and clears the answer, so a
    tab left open on round 1 would otherwise land its verdict on round 2; a
    `round` that is not the current one is refused (`AlreadyDecided`, naming
    both rounds). None skips the check, as every caller before rounds did.
    `version` is the page version the form was drawn over; it is kept on the
    round it closes, and the next round's changes are shown from it.

    `source` is 'page' for his tap. The partner bridge passes 'slack' (and
    `recorded_by='quarterdeck'`, the party that vouched for the press) for an
    answer that came back from Slack; everything else is identical, the event
    and the outbound `answered` included. `in_transaction`, when given, is
    called with the connection inside the answer's own transaction, so the
    caller's own records commit or roll back with it.
    """
    if source not in ("page", "slack"):
        raise InvalidSource(source)
    require_stored_source(source)
    store._check_size(note, MAX_NOTE_BYTES, "note", agent=by, artifact_id=None)

    emissions: list[events.Emission] = []
    with db.connect() as conn:
        # Joins `artifact` for the nudge (title, owning agent) as well as the
        # existence-and-not-closed check itself -- one query doing both jobs, exactly
        # as comments.resolve()'s join does ownership and lookup together.
        row = conn.execute(
            f"select {_COLUMNS}, artifact.title, artifact.agent from decision"  # noqa: S608 -- fixed constant, not caller input
            " join artifact on artifact.id = decision.artifact_id"
            " where decision.id = %s and decision.closed_at is null",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise store.NotFound()
        # Counted from the END, deliberately. These two come from the join, not from
        # `_COLUMNS`, so their position is "last two" and nothing else; written as
        # row[12], row[13] they were correct only for as long as `_COLUMNS` had
        # exactly twelve entries. Adding the two acknowledgement columns silently
        # turned `owning_agent` into `acknowledged_by` -- always None -- and every
        # answer then tried to write an event with no agent.
        artifact_id, options, title, owning_agent = row[1], row[4], row[-2], row[-1]
        # The session that ASKED, taken from inside _COLUMNS (last entry) rather
        # than from the end of the row, which is where the two joined columns
        # live. The event carries this so the seat that raised the question can
        # recognise its own answer in a feed three seats share -- see
        # events.list_for_agent's `session`.
        asking_session = row[_SESSION_IDX]

        # SET ASIDE (open, its page archived or binned): he cannot answer it, from the
        # page or from Slack. Indistinguishable from "no such decision", as closed is.
        if _set_aside_fields(row)["set_aside"]:
            raise store.NotFound()

        letters = {option["letter"] for option in options}
        if letter not in letters:
            raise InvalidAnswer()
        # A held question is not answerable: a page drawn before its prerequisite
        # was settled must not be able to answer it. Only a first answer is
        # refused; one already given stands and may be changed.
        if row[_KIND_IDX] == "question" and row[6] is None:
            current = _with_held(
                conn, [_row_to_decision(row[: len(_COLUMNS.split(","))])]
            )[0]
            if current.held_by:
                raise Held(current.held_by)
        # One verdict counts on a review: a second device, or a stale form, is
        # refused and told what counted. A QUESTION may still be re-answered.
        if row[_KIND_IDX] == "review" and row[6] is not None:
            raise _already(conn, row, round)

        store._check_secrets(note, "note", agent=by, artifact_id=artifact_id)

        # One transaction: the answer and the event that describes it commit or
        # roll back together (events.append's own docstring) -- a nudge must
        # never be sent for an answer that a later failure in this block undid,
        # and the journal must never gain a row whose cause never happened.
        with conn.transaction():
            _require_live_page(conn, decision_id, artifact_id)
            if row[_KIND_IDX] == "review":
                letter = _lock_review_and_settle_letter(
                    conn, decision_id, artifact_id, letter, round
                )
            label = next(
                option["label"] for option in options if option["letter"] == letter
            )
            summary = f"{letter}: {label}"[:200]
            if source == "slack":
                summary = f"{letter}: {label} (answered in Slack)"[:200]
            # The write itself is conditional for a review, not only the read
            # above: two devices can both pass that read before either writes,
            # and only an atomic `answer is null` test in the update keeps the
            # first verdict. A second writer waits on the row lock, re-tests
            # and updates nothing.
            answered_at = datetime.now(UTC)
            written = conn.execute(
                "update decision set answer = %s, answer_note = %s, answered_at = %s,"
                " answered_by = %s, answer_source = %s, recorded_by = %s,"
                " said_at = null where id = %s and closed_at is null"
                " and (kind <> 'review' or answer is null)"
                # The page must still be live AT THE WRITE: the set-aside check
                # above read the row before this transaction, and an archive in
                # between must not let an answer land on a parked question.
                " and exists (select 1 from artifact a"
                " where a.id = decision.artifact_id"
                " and a.archived_at is null and a.deleted_at is null)",
                (letter, note, answered_at, by, source, recorded_by, decision_id),
            )
            if written.rowcount == 0:
                # Lost the race: no event, and the row as it now stands.
                current = conn.execute(
                    f"select {_COLUMNS} from decision where decision.id = %s",  # noqa: S608 -- fixed constant, not caller input
                    (decision_id,),
                ).fetchone()
                if (
                    current is None
                    or current[11] is not None
                    or _set_aside_fields(current)["set_aside"]
                ):
                    # Closed (withdrawn, or replaced) between the read above
                    # and this write: answered exactly as a closed decision is.
                    raise store.NotFound()
                raise _already(conn, current, round)
            # A new human act: what was declared about an earlier answer (or before
            # any) is history, and the acknowledgement reads as new again.
            conn.execute(
                "update decision set acknowledged_at = null, acknowledged_by = null"
                " where id = %s",
                (decision_id,),
            )
            receipts.void(conn, decision_id)
            if row[_KIND_IDX] == "review":
                _close_round(conn, decision_id, letter, version)
                # His verdict carries every mark in its message, so releasing
                # the held ones writes no events of their own.
                holds.release(conn, artifact_id, emit=False)
            else:
                # A changed answer is a new version: the partner keeps the latest.
                emissions.append(
                    events.emit(
                        conn,
                        events.Event(
                            kind="answered",
                            artifact_id=artifact_id,
                            agent=owning_agent,
                            actor=by,
                            ref_id=decision_id,
                            session=asking_session,
                            data={
                                "body": partner.answered_body(
                                    letter=letter,
                                    note=note,
                                    answered_by=by,
                                    answered_at=answered_at,
                                    answer_source=source,
                                ),
                                "at": answered_at.isoformat(),
                            },
                        ),
                    )
                )
            if in_transaction is not None:
                in_transaction(conn)
            # Read back INSIDE the transaction, while it still holds the row:
            # after the commit a republish that was waiting on the lock can
            # open the next round and clear the answer, and this call would
            # return (and nudge with) None instead of the verdict it wrote.
            updated = conn.execute(
                f"select {_COLUMNS} from decision where decision.id = %s",  # noqa: S608 -- fixed constant, not caller input
                (decision_id,),
            ).fetchone()
            # Read back as a row rather than indexed by position, because the fields
            # below are exactly the ones a positional read gets wrong: the comment
            # above about row[12] silently becoming `acknowledged_by` is the same
            # mistake one column earlier, and this one would mis-send what Jeremy
            # wrote.
            answered = _row_to_decision(updated)
            # The journal row and the nudge, as one event. The nudge fires after the
            # commit (below): it must never be mistaken for an answer that failed
            # (nudge.py's docstring), and the journal row must be durable before any
            # other agent is told to go read it. HIS WORDS TRAVEL WITH IT: `note` on
            # the journal (the feed would say only "a: Yes" otherwise) and in the
            # mailbox (homelab-873), with the recommendation, because a letter that
            # departs from it reads as agreement until somebody checks.
            emissions.append(
                events.emit(
                    conn,
                    events.Event(
                        kind="decision_answered",
                        artifact_id=artifact_id,
                        agent=owning_agent,
                        actor=by,
                        ref_id=decision_id,
                        summary=summary,
                        note=note,
                        # The session that ASKED, not the one that answered: Jeremy
                        # has no session, and the whole point is that the seat which
                        # raised the question can pick its own answer out of a shared
                        # feed.
                        session=asking_session,
                        data={
                            "title": title,
                            "letter": answered.answer,
                            "recommended": answered.recommended,
                        },
                    ),
                )
            )

    for emission in emissions:
        emission.fire()
    return answered


def get_owned(db: Database, agent: str, decision_id: str) -> DecisionRow | None:
    """One decision, only if its page belongs to `agent`. None covers a missing id
    and someone else's identically, so a key cannot be walked to learn which ids
    are real."""
    with db.connect() as conn:
        row = conn.execute(
            f"select {_COLUMNS} from decision"  # noqa: S608 -- fixed constant, not caller input
            " join artifact on artifact.id = decision.artifact_id"
            " where decision.id = %s and artifact.agent = %s",
            (decision_id, agent),
        ).fetchone()
        return None if row is None else _with_held(conn, [_row_to_decision(row)])[0]


def get_owned_with_round(
    db: Database, agent: str, decision_id: str
) -> tuple[DecisionRow | None, RoundRow | None]:
    """`get_owned` and the review's current round, read in ONE statement so
    the verdict and the round it belongs to can never come from either side
    of a republish that opens the next round."""
    width = len(_COLUMNS.split(","))
    with db.connect() as conn:
        row = conn.execute(
            f"select {_COLUMNS}, {_R_ROUND_COLUMNS} from decision"  # noqa: S608 -- fixed constant, not caller input
            " join artifact on artifact.id = decision.artifact_id"
            # The latest round, as a correlated subquery (SQLite has no lateral).
            " left join review_round r on r.review_id = decision.id"
            "   and r.n = (select max(n) from review_round rr"
            "   where rr.review_id = decision.id)"
            " where decision.id = %s and artifact.agent = %s",
            (decision_id, agent),
        ).fetchone()
    if row is None:
        return None, None
    found = row[width:]
    with db.connect() as conn:
        decision = _with_held(conn, [_row_to_decision(row[:width])])[0]
    return (
        decision,
        _row_to_round(found) if found[0] is not None else None,
    )


def _close_row(
    conn, agent: str, row, *, replaced_by: str | None = None
) -> events.Emission | None:
    """Close the decision `row` (raw, from `_COLUMNS`) inside the caller's
    transaction. `replaced_by` records which question took its place. Returns the
    Emission of the `replaced`/`withdrawn` bridge event, if one was made; the caller
    fires it after its transaction commits."""
    # Closing implies acknowledgement -- an agent that finished the work
    # necessarily heard the answer -- but only when nothing has acknowledged
    # it yet, so an earlier, truer "seen at" is never overwritten by a later
    # close. `coalesce` does both in one statement.
    now = datetime.now(UTC)
    # Conditional on still being open, and the answer read back from the update
    # itself: `row` was read earlier and may be stale. A second close, or one
    # that lost to an answer, changes nothing and tells the partner nothing.
    closed = conn.execute(
        "update decision set closed_at = %s,"
        " acknowledged_at = coalesce(acknowledged_at, %s),"
        " acknowledged_by = coalesce(acknowledged_by, %s),"
        " replaced_by = coalesce(%s, replaced_by)"
        " where id = %s and closed_at is null returning answer",
        (now, now, agent, replaced_by, row[0]),
    ).fetchone()
    if closed is None:
        return None
    emission = None
    if row[_KIND_IDX] == "question" and closed[0] is None:
        # Closed with no answer: withdrawn, or replaced by the question that
        # took its place. An answered question that is then closed is the
        # agent finishing its work, which the partner has no use for.
        if replaced_by is not None:
            kind = "replaced"
            body = {
                "replaced_by": replaced_by,
                "replaced_by_source": partner.source_of(replaced_by),
            }
        else:
            kind = "withdrawn"
            body = {"reason": "The asking agent closed the question without an answer."}
        emission = events.emit(
            conn,
            events.Event(
                kind=kind,
                artifact_id=row[1],
                agent=agent,
                actor=agent,
                ref_id=row[0],
                data={"body": body, "at": now.isoformat()},
            ),
        )
    if row[_KIND_IDX] == "review" and holds.page_mode(conn, row[1]) != "held":
        # The review is over, so his held marks would never be
        # seen -- unless another open review on the page still
        # holds them.
        holds.release(conn, row[1], emit=True)
    return emission


def close(db: Database, agent: str, decision_id: str) -> DecisionRow:
    """The agent withdrawing a question it no longer needs answered. Idempotent: a
    second close returns the row unchanged, `closed_at` untouched, exactly as
    `comments.resolve` treats a second resolve of the same thread -- a repeat call
    (a retried request, an agent that closes defensively) is a no-op, not a fresh
    timestamp. `NotFound` covers both a decision that does not exist and one owned by
    a different agent -- the same artifact-join shape `comments.resolve` uses, so the
    two look identical from outside exactly as `store.patch`'s ownership check makes
    them look identical there too."""
    if get_owned(db, agent, decision_id) is None:
        raise store.NotFound()
    with db.connect() as conn:
        row = conn.execute(
            f"select {_COLUMNS} from decision where decision.id = %s",  # noqa: S608 -- fixed constant, not caller input
            (decision_id,),
        ).fetchone()
        if row[11] is None:  # closed_at
            with conn.transaction():
                emission = _close_row(conn, agent, row)
            if emission is not None:
                emission.fire()
            row = conn.execute(
                f"select {_COLUMNS} from decision where decision.id = %s",  # noqa: S608 -- fixed constant, not caller input
                (decision_id,),
            ).fetchone()
    return _row_to_decision(row)


def acknowledge(
    db: Database,
    agent: str,
    decision_id: str,
    *,
    conn=None,
    session: str | None = None,
) -> DecisionRow:
    """ "I have seen this answer" -- a different fact from "the work is done".

    The alert that catches a lost answer keys on answered-and-not-acknowledged, and it
    has to tell a slow owner from a dead one. Neither of the facts already in this
    table can do that: `answered_at` is about Jeremy, and `closed_at` is about the work
    being finished, which here usually means a commit or a playbook or an SSH session
    hours later. An alert that waits for completion fires on decisions that are being
    handled correctly, gets muted, and then misses the one that was genuinely lost.

    Idempotent, and deliberately not a "latest seen" timestamp: the first
    acknowledgement is the one that answers the question the alert asks, so a second
    call returns the row unchanged rather than moving the time forward. Same shape as
    `close`, including `NotFound` covering both a decision that does not exist and one
    owned by another agent.

    Acknowledging an unanswered decision is allowed and means nothing yet -- the alert
    only ever reads it alongside `answered_at`. Refusing it would be a guard whose only
    effect is to make a retry after an answer arrives fail for the wrong reason.

    `conn`, when given, is the caller's transaction (an `Idempotency-Key`'d
    request), so the acknowledgement commits with the stored response.
    """
    if conn is not None:
        return _acknowledge(conn, agent, decision_id, session=session)
    with db.connect() as own:
        return _acknowledge(own, agent, decision_id, session=session)


def _acknowledge(
    conn, agent: str, decision_id: str, *, session: str | None = None
) -> DecisionRow:
    row = conn.execute(
        f"select {_COLUMNS} from decision"  # noqa: S608 -- fixed constant, not caller input
        " join artifact on artifact.id = decision.artifact_id"
        " where decision.id = %s and artifact.agent = %s",
        (decision_id, agent),
    ).fetchone()
    if row is None:
        raise store.NotFound()
    first = (
        row[12] is None  # acknowledged_at
        and conn.execute(
            # Conditional, so two simultaneous acks write one seen row.
            "update decision set acknowledged_at = %s, acknowledged_by = %s"
            " where id = %s and acknowledged_at is null",
            (datetime.now(UTC), agent, decision_id),
        ).rowcount
        > 0
    )
    if first:
        # Seen is a receipt: one journal row, only when this call is what set it.
        # `receipt_set` never wakes a waiter (events.list_for_agent).
        receipts.insert(conn, decision_id, "seen", agent=agent, session=session)
        events.append(
            conn,
            artifact_id=row[1],
            agent=agent,
            actor=agent,
            kind="receipt_set",
            summary=f"{agent} seen",
            ref_id=decision_id,
            session=session,
        )
        row = conn.execute(
            f"select {_COLUMNS} from decision where decision.id = %s",  # noqa: S608 -- fixed constant, not caller input
            (decision_id,),
        ).fetchone()
    return _with_held(conn, [_row_to_decision(row)])[0]


# How long an answer may sit uncollected before it is worth saying so. Ten
# minutes is chosen to be far longer than any healthy round trip -- a held
# request returns within a second of an answer, and the agent acknowledges as
# soon as it reads -- and far shorter than Jeremy's patience. It is a parameter
# rather than a constant at the call site so a caller watching a slow link can
# raise it without editing this module.
STUCK_AFTER_SECONDS = 600


@dataclass(frozen=True)
class UncollectedRow:
    """An answer that Jeremy gave and nobody has taken, with the one fact that
    says whose problem it is."""

    decision: DecisionRow
    state: str  # "no_reader" | "stuck"
    last_read_at: str | None
    # WHOSE answer this is, and which page. Both are redundant when a seat asks
    # about itself and both are the whole point when the OPERATOR asks about
    # everyone: an alert that says "one answer is stranded" without saying whose
    # or on what sends its reader back to the database to find out, which is how
    # an alert stops being read. `CrossSeatRow` below carries the same two for the
    # same reason.
    agent: str
    artifact_title: str


def uncollected(
    db: Database,
    *,
    agent: str | None = None,
    stuck_after_seconds: int = STUCK_AFTER_SECONDS,
) -> list[UncollectedRow]:
    """Answers that were given and never collected, each labelled with WHY.

    The two states need opposite responses, which is the whole reason this
    distinguishes them:

    - `no_reader` -- nobody has asked this seat for its events since the answer
      was given. The agent is not listening: it exited, it was never armed, or
      its machine is off. Nothing will collect this answer until a person or
      another agent does.
    - `stuck` -- the seat HAS read its feed since the answer, and still has not
      acknowledged it. Something is alive and not acting. That is a bug to go
      and look at, not an absence to work around.

    An alert that reported only "uncollected" would mix those two together, and
    whoever read it would learn to ignore it -- which is how the original failure
    (a deploy silently cutting off every waiting agent) went unnoticed for a day.

    Both states wait out `stuck_after_seconds` first: an answer given ten seconds
    ago has not been lost, it is in flight. `agent` scopes to one seat; omitted,
    every seat, which is the operator's view.
    """
    sql = (
        f"select {_COLUMNS}, artifact.agent, artifact.title,"  # noqa: S608 -- fixed constant, not caller input
        " seat_read.last_read_at from decision"
        " join artifact on artifact.id = decision.artifact_id"
        # Joined on the ASKING SESSION as well as the seat. The question is not
        # "has anything on that machine read the feed" but "has the session
        # that raised THIS question read it since it was answered" -- three
        # sessions share one key, so the first answers yes while the asker is
        # long gone, which is the wrong answer in the case this exists for.
        #
        # A decision asked without a session has NULL here and must match the
        # unlabelled reader's row, whose session is stored as ''. Plain `=` is
        # never true for NULL, so without the coalesce every pre-session
        # decision would read as no_reader for ever.
        " left join seat_read on seat_read.agent = artifact.agent"
        "   and seat_read.session = coalesce(decision.session, '')"
        " where decision.answered_at is not null"
        " and decision.acknowledged_at is null"
        " and decision.answered_at < %s"
        # A DELETED page's answers owe nobody anything (homelab-ij8.97): the page
        # is gone and nothing on it can be acted on, so reporting it here for
        # ever made the one queue meant to be empty never empty. Restoring the
        # page brings it back, because the answer itself is untouched.
        " and artifact.deleted_at is null"
    )
    params: list[object] = [datetime.now(UTC) - timedelta(seconds=stuck_after_seconds)]
    if agent is not None:
        sql += " and artifact.agent = %s"
        params.append(agent)
    sql += " order by decision.answered_at asc"

    with db.connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()

    out = []
    for row in rows:
        answered_at, last_read_at = row[8], row[-1]
        owning_agent, artifact_title = row[-3], row[-2]
        # "Since the answer" is the comparison, not "recently": a seat read an
        # hour before the answer and never again is exactly as absent as one that
        # has never been read at all, and saying otherwise would send someone
        # looking for a live agent that is not there.
        listening = last_read_at is not None and last_read_at > answered_at
        out.append(
            UncollectedRow(
                decision=_row_to_decision(row),
                state="stuck" if listening else "no_reader",
                last_read_at=last_read_at.isoformat() if last_read_at else None,
                agent=owning_agent,
                artifact_title=artifact_title,
            )
        )
    return out


@dataclass(frozen=True)
class CrossSeatRow:
    """One question from any seat, described so that whoever reads it knows
    which page to open, whose page it is, and how long it has been sitting
    there.

    Called `WaitingRow` until 21 Sep 2026, when this view stopped being only
    about what is waiting: a granted key can now ask for answered and withdrawn
    questions too (homelab-9gj), and a row carrying Jeremy's answer is not
    waiting on anybody.
    """

    decision: DecisionRow
    agent: str
    artifact_title: str
    age_seconds: int


def across_seats(
    db: Database,
    *,
    include_answered: bool = False,
    include_closed: bool = False,
    limit: int = 200,
    after: str | None = None,
    include_set_aside: bool = False,
) -> list[CrossSeatRow]:
    """Every question on every page, oldest first -- by default only the ones
    still waiting on Jeremy.

    THE DEFAULTS ARE LOAD-BEARING, and not merely conservative. The operator's
    queue (`ops-waiting-feed.sh` in the homelab repo) reconciles itself against
    this feed by DISAPPEARANCE: a decision it has a row for that is no longer
    returned is taken as proof the decision is no longer open, and the row is
    closed. An answered decision that kept showing up here would therefore
    never be closed in that queue, and one that vanished for any other reason
    would be closed wrongly. So "what is waiting" stays the default, and
    answered and withdrawn rows are an explicit ask.

    `include_answered` is that explicit ask (homelab-9gj, reported by the
    operator on 20 Sep 2026 consuming this feature): the read-all grant let a
    granted seat see a cross-seat question while it was open, and infer from
    its disappearance that it had been answered, but never read the letter or
    the note Jeremy wrote -- so two of that day's three live decisions were
    closed in the operator's queue with a generic "answered on its page"
    instead of his actual answer. Two of three is the common case, not an edge.

    The operator's view, and the answer to the only question the service could
    not previously answer: what is waiting on Jeremy? Until now a seat could see
    its own decisions and nothing else, so "what is waiting" was assembled from
    what each agent remembered to report -- and on 20 Sep 2026 a question sat
    unanswered for seventeen hours because the one seat that could see it was not
    looking.

    OLDEST first, the opposite of `list_for_agent`. An agent polling its own feed
    wants the newest answer; a queue put in front of Jeremy wants the thing that
    has been waiting longest, because that is the one most likely to have been
    forgotten.

    `age_seconds` is computed by the service from the `created_at` it stamped, so a
    consumer's own clock never disagrees with the service about how old something
    is -- and a queue that reads "17 hours" is telling Jeremy something the
    service is prepared to stand behind.

    `limit` cuts the NEWEST rows (this is oldest first), the dangerous end for a
    disappearance reconciler: the route therefore asks for one row more than it
    will return and says so, in a header, whenever it cut the list. `after` is
    the last id of the previous page.
    """
    sql = (
        f"select {_COLUMNS}, artifact.agent, artifact.title,"  # noqa: S608 -- fixed constant, not caller input
        " decision.created_at from decision"
        " join artifact on artifact.id = decision.artifact_id"
        " where decision.kind <> 'form'"
    )
    if not include_closed:
        sql += " and decision.closed_at is null"
    if not include_answered:
        sql += " and decision.answer is null"
    if not include_set_aside:
        # A question set aside (open, its page archived or binned) is not waiting on
        # him: it is off every list of what is, exactly as the reconciler wants it.
        sql += _NOT_SET_ASIDE
    params: list[object] = []
    if after is not None:
        sql += _AFTER.format(op=">")
        params.append(after)
    sql += " order by decision.created_at asc, decision.id asc limit %s"
    with db.connect() as conn:
        rows = conn.execute(sql, (*params, limit)).fetchall()
        decided = _with_held(conn, [_row_to_decision(row) for row in rows])
    now = datetime.now(UTC)
    return [
        CrossSeatRow(
            decision=decision,
            agent=row[-3],
            artifact_title=row[-2],
            age_seconds=round((now - row[-1]).total_seconds()),
        )
        for row, decision in zip(rows, decided, strict=True)
    ]


# What an outcome note may carry. The same ceiling as an answer note, and for
# the same reason: this is somebody explaining what happened, not a label, and
# truncating it to a line is how "we shipped (a), but the framing was wrong and
# he should look again" becomes "we shipped (a)".
MAX_OUTCOME_BYTES = 1_000


@dataclass(frozen=True)
class OutcomeRow:
    id: str
    decision_id: str
    body: str
    agent: str
    session: str | None
    created_at: str
    # Set when the outcome is a `done` receipt's (receipts.py); None otherwise.
    link: str | None = None


def _row_to_outcome(row) -> OutcomeRow:
    return OutcomeRow(
        id=row[0],
        decision_id=row[1],
        # Sanitised on the way out as well as in. An outcome is written by an
        # agent and read into a terminal and into Jeremy's page, and `sanitize`
        # is idempotent, so a clean row costs nothing.
        body=text.sanitize(row[2], limit=MAX_OUTCOME_BYTES),
        agent=row[3],
        session=row[4],
        created_at=row[5].isoformat(),
        link=row[6],
    )


def record_outcome(
    db: Database,
    decision_id: str,
    *,
    agent: str,
    body: str,
    session: str | None = None,
) -> OutcomeRow:
    """Append what a decision caused, or what changed under it.

    homelab-ij8.44. The model stopped at the answer, so the durable record of
    what a decision PRODUCED -- a PR, a closed bead, a correction to the agent's
    own framing -- lived only in prose somebody happened to rewrite.

    NOT GATED ON THE DECISION BEING ANSWERED, deliberately. The operator's case,
    21 Sep 2026: they corrected two promises on a page while the question was
    still open. That is the one with teeth -- a question whose premise moved
    after Jeremy read it and before he answered is one he can answer wrongly
    through no fault of his own, and until now nothing on the page said so.

    Append only. There is no update and no delete, and no "current outcome"
    field anywhere, because the failure this fixes IS a summary that keeps being
    rewritten until what actually happened is gone.

    Scoped like every other write here: the decision must be on a page this
    agent owns, and `NotFound` covers both "no such decision" and "not yours",
    which are deliberately indistinguishable.
    """
    store._check_size(body, MAX_OUTCOME_BYTES, "outcome", agent=agent, artifact_id=None)
    with db.connect() as conn:
        owned = conn.execute(
            "select decision.artifact_id from decision"
            " join artifact on artifact.id = decision.artifact_id"
            " where decision.id = %s and artifact.agent = %s"
            "   and artifact.deleted_at is null",
            (decision_id, agent),
        ).fetchone()
        if owned is None:
            raise store.NotFound()
        # The same guard every other text reaching storage gets. "It is only a
        # note" is how a field skips its checks.
        store._check_secrets(body, "outcome", agent=agent, artifact_id=owned[0])
        outcome_id = ids.new_id()
        conn.execute(
            "insert into decision_outcome (id, decision_id, body, agent, session)"
            " values (%s, %s, %s, %s, %s)",
            (outcome_id, decision_id, body, agent, session),
        )
        row = conn.execute(
            "select id, decision_id, body, agent, session, created_at, link"
            " from decision_outcome where id = %s",
            (outcome_id,),
        ).fetchone()
    return _row_to_outcome(row)


def outcomes_for(db: Database, decision_id: str) -> list[OutcomeRow]:
    """Every outcome on one decision, oldest first -- the order they happened.

    Trusts `decision_id` outright, exactly as this module's other listing
    helpers do; ownership is the caller's job.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "select id, decision_id, body, agent, session, created_at, link"
            " from decision_outcome where decision_id = %s"
            " order by created_at asc, id asc",
            (decision_id,),
        ).fetchall()
    return [_row_to_outcome(row) for row in rows]


# Double- or single-quoted, because both are HTML and the shim's own
# querySelectorAll finds either. An unquoted value is also legal HTML and is
# NOT matched: missing one is a slot we fail to warn about, which is what
# happened before this existed, never a false alarm.
_SLOT_IN_BODY = re.compile(
    r"""data-artifact-decision\s*=\s*(?:"([^"]{1,64})"|'([^']{1,64})')"""
)


def unresolved_slots(
    db: Database, artifact_id: str, body: str
) -> list[tuple[str, int]]:
    """Decision slots in `body` that name no decision on THIS page, as
    (id, line of its first appearance).

    homelab-ij8.50. Jeremy, reading the migrated decisions page: "some
    questions didn't give me the option". A slot whose id does not resolve --
    a typo, a stale id, one copied from another page, a template's
    placeholder left in -- renders as NOTHING, so "still arriving", "no such
    decision of mine" and "there was never a question here" are the same
    pixels. He read it as a question that had lost his choices, which is the
    worst of the three readings.

    AND THE AUTHOR CANNOT SEE IT EITHER. The rendered page only answers a
    framed request holding a capability token no agent is given, so a wrong
    slot id is invisible on both sides and nobody catches it. So both sides
    are told: the publisher in its publish warnings, the reader by a note the
    page shim writes into the empty slot.

    A decision that EXISTS here resolves whatever its state. An answered one
    releases its room, which is correct, and must stay distinguishable from a
    slot that never had a question -- that distinction is the bead's second
    criterion, and filtering on `status = 'open'` would erase it.

    ORDER IS THE BODY'S, de-duplicated: a slot repeated twice is one mistake,
    and reporting it twice invites fixing it once and believing it is done.
    """
    body = csp.outside_comments(body)
    first: dict[str, int] = {}
    for match in _SLOT_IN_BODY.finditer(body):
        slot = match.group(1) or match.group(2)
        if slot not in first:
            first[slot] = body.count("\n", 0, match.start()) + 1
    if not first:
        return []
    with db.connect() as conn:
        rows = conn.execute(
            "select id from decision where artifact_id = %s"  # noqa: S608 -- placeholders only
            f" and id in {in_list(first)}",
            (artifact_id, *first),
        ).fetchall()
    real = {r[0] for r in rows}
    return [(slot, line) for slot, line in first.items() if slot not in real]


def outcomes_for_artifact(
    db: Database, artifact_id: str
) -> dict[str, list[OutcomeRow]]:
    """Every outcome on every decision of one page, keyed by decision id.

    One query rather than one per decision: the gallery renders a page with
    several questions on it, and a per-decision read there is a round trip per
    question on every load.
    """
    out: dict[str, list[OutcomeRow]] = {}
    with db.connect() as conn:
        rows = conn.execute(
            "select o.id, o.decision_id, o.body, o.agent, o.session, o.created_at, o.link"
            " from decision_outcome o"
            " join decision d on d.id = o.decision_id"
            " where d.artifact_id = %s"
            " order by o.created_at asc, o.id asc",
            (artifact_id,),
        ).fetchall()
    for row in rows:
        out.setdefault(row[1], []).append(_row_to_outcome(row))
    return out


def outcomes_for_decisions(
    db: Database, decision_ids: list[str]
) -> dict[str, list[OutcomeRow]]:
    """Outcomes on the given decisions, keyed by decision id, oldest first: one
    query for a list of questions that live on different pages."""
    if not decision_ids:
        return {}
    out: dict[str, list[OutcomeRow]] = {}
    with db.connect() as conn:
        rows = conn.execute(
            "select id, decision_id, body, agent, session, created_at, link"  # noqa: S608 -- placeholders only
            f" from decision_outcome where decision_id in {in_list(decision_ids)}"
            " order by created_at asc, id asc",
            tuple(decision_ids),
        ).fetchall()
    for row in rows:
        out.setdefault(row[1], []).append(_row_to_outcome(row))
    return out


def open_counts(db: Database) -> tuple[int, int, int]:
    """(open reviews, open questions that are not held, every open row), counted without reading
    any question text: the same rows `across_seats` returns by default, and the
    same held rule, so a count and a list cannot disagree. Set-aside questions (their
    page archived or binned) are not open here."""
    with db.connect() as conn:
        found = conn.execute(
            "select d.id, d.kind, d.depends_on from decision d"
            " join artifact a on a.id = d.artifact_id"
            " where d.closed_at is null and d.answer is null and d.kind <> 'form'"
            " and a.archived_at is null and a.deleted_at is null"
        ).fetchall()
        reviews = sum(1 for r in found if r[1] == "review")
        waiting = [
            types.SimpleNamespace(
                id=r[0], depends_on=r[2], answer=None, closed_at=None, kind=r[1]
            )
            for r in found
            if r[1] == "question"
        ]
        held = held_map(conn, waiting)
    return reviews, len(waiting) - len(held), len(found)


def round_presence(db: Database, agent: str, review_id: str) -> dict | None:
    """What his card says about who is on a review's current round:
    {"live": True, "session", "seen_seconds"} for a live lease, {"live": False,
    "session", "reason"} for a hand-back, None otherwise. `seen_seconds` is
    the newest `seat_read` for THIS agent and session, or None. Scoped to the
    owning agent: another agent's session can never be named here.

    The current round counts while it is open, and also once it is closed with
    changes requested: that is when a session is revising (or has handed the
    revision back), and the card is where he sees why the review stalled. Any
    other verdict ends the review, and nobody is on it."""
    with db.connect() as conn:
        found = conn.execute(
            "select claimed_by, claim_expires_at > %s, released_reason,"
            " released_by from review_round where review_id = %s"
            " and (closed_at is null or verdict = 'changes_requested')"
            " and n = (select max(n) from review_round where review_id = %s)",
            (datetime.now(UTC), review_id, review_id),
        ).fetchone()
        if found is None:
            return None
        claimed_by, live, reason, released_by = found
        if claimed_by is not None and live:
            seen = conn.execute(
                'select max(last_read_at) as "seen [TIMESTAMPTZ]"'
                " from seat_read where agent = %s and session = %s",
                (agent, claimed_by),
            ).fetchone()
            return {
                "live": True,
                "session": claimed_by,
                "seen_seconds": (datetime.now(UTC) - seen[0]).total_seconds()
                if seen and seen[0] is not None
                else None,
            }
    if claimed_by is None and reason:
        return {"live": False, "session": released_by or "an agent", "reason": reason}
    return None
