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

from dataclasses import dataclass

from psycopg.types.json import Jsonb

from artifacts import events, ids, nudge, store
from artifacts.db import Database

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
    " decision.acknowledged_at, decision.acknowledged_by"
)


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
    )


def ask(
    db: Database,
    *,
    agent: str,
    artifact_id: str,
    question: str,
    options: list[dict],
    recommended: str | None = None,
) -> DecisionRow:
    """Pose a lettered question on `artifact_id`. Refuses (`store.NotFound`) an
    artifact `agent` does not own, exactly as `store.patch` does -- someone else's
    page and a missing one must look identical, never a hint either way."""
    store._check_size(
        question, MAX_QUESTION_BYTES, "question", agent=agent, artifact_id=artifact_id
    )
    validated_options, letters = _validate_options(options)
    if recommended is not None and recommended not in letters:
        raise InvalidOptions()

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

        decision_id = ids.new_id()
        conn.execute(
            "insert into decision (id, artifact_id, version_n, question, options, recommended)"
            " values (%s, %s, %s, %s, %s, %s)",
            (
                decision_id,
                artifact_id,
                version_n,
                question,
                Jsonb(validated_options),
                recommended,
            ),
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
        sql += " and decision.answer is null"
    if not include_closed:
        sql += " and decision.closed_at is null"
    sql += " order by decision.created_at asc, decision.id asc"
    with db.connect() as conn:
        rows = conn.execute(sql, (artifact_id,)).fetchall()
    return [_row_to_decision(row) for row in rows]


def list_for_agent(
    db: Database, agent: str, *, include_answered: bool = False, limit: int = 200
) -> list[DecisionRow]:
    """Newest first -- this is the polling surface an agent uses to learn Jeremy's
    answer, so newest matters more than oldest, exactly as `comments.list_for_agent`
    is ordered for the same reason. Always excludes a closed decision, with no
    `include_closed` escape hatch at all: the agent withdrew it, so there is nothing
    left here for that agent to learn. `limit` protects an agent with a long history
    from pulling its entire back catalogue on every poll."""
    sql = (
        f"select {_COLUMNS} from decision"  # noqa: S608 -- fixed constant, not caller input
        " join artifact on artifact.id = decision.artifact_id"
        " where artifact.agent = %s and decision.closed_at is null"
    )
    if not include_answered:
        sql += " and decision.answer is null"
    sql += " order by decision.created_at desc, decision.id desc limit %s"
    with db.connect() as conn:
        rows = conn.execute(sql, (agent, limit)).fetchall()
    return [_row_to_decision(row) for row in rows]


def answer(
    db: Database, decision_id: str, *, letter: str, note: str = "", by: str
) -> DecisionRow:
    """The gallery, acting for Jeremy, answers a decision. Takes no `agent`: this is
    not a call an agent ever makes, so there is no ownership to check against a
    caller-supplied identity -- only whether `decision_id` names an open (not closed)
    decision at all. `by` is always `"jeremy"` today; the gallery hardcodes it rather
    than trusting any identity header (see `gallery.py`), and this function stores
    whatever it is given without questioning it further.

    Re-answering is allowed and REPLACES the previous answer -- Jeremy may change his
    mind, and a 409 here would send him to a terminal to do it, which is exactly the
    kind of friction this whole feature exists to remove. `answered_at` moves with
    every call, including a second one that picks the same letter again. Refuses
    (`store.NotFound`) a decision that does not exist or is already closed: the agent
    that closed it said it no longer needs an answer, and a stale form resubmission
    must not manufacture one anyway.
    """
    store._check_size(note, MAX_NOTE_BYTES, "note", agent=by, artifact_id=None)

    with db.connect() as conn:
        # Joins `artifact` for the nudge below (title, owning agent) as well as the
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

        letters = {option["letter"] for option in options}
        if letter not in letters:
            raise InvalidAnswer()

        store._check_secrets(note, "note", agent=by, artifact_id=artifact_id)

        label = next(
            option["label"] for option in options if option["letter"] == letter
        )
        summary = f"{letter}: {label}"[:200]
        # One transaction: the answer and the event that describes it commit or
        # roll back together (events.append's own docstring) -- a nudge must
        # never be sent for an answer that a later failure in this block undid,
        # and the journal must never gain a row whose cause never happened.
        with conn.transaction():
            conn.execute(
                "update decision set answer = %s, answer_note = %s, answered_at = now(),"
                " answered_by = %s where id = %s",
                (letter, note, by, decision_id),
            )
            event_seq = events.append(
                conn,
                artifact_id=artifact_id,
                agent=owning_agent,
                actor=by,
                kind="decision_answered",
                summary=summary,
                ref_id=decision_id,
            )
        updated = conn.execute(
            f"select {_COLUMNS} from decision where decision.id = %s",  # noqa: S608 -- fixed constant, not caller input
            (decision_id,),
        ).fetchone()

    # Fire-and-forget, after the transaction above has actually committed: a nudge
    # that fails must never be mistaken for an answer that failed (see nudge.py's
    # own docstring), and `event_seq` must name a row that is durable before any
    # other agent is told to go read it.
    nudge.emit(
        kind="decision_answered",
        artifact_id=artifact_id,
        title=title,
        agent=owning_agent,
        summary=summary,
        event_id=str(event_seq),
    )
    return _row_to_decision(updated)


def close(db: Database, agent: str, decision_id: str) -> DecisionRow:
    """The agent withdrawing a question it no longer needs answered. Idempotent: a
    second close returns the row unchanged, `closed_at` untouched, exactly as
    `comments.resolve` treats a second resolve of the same thread -- a repeat call
    (a retried request, an agent that closes defensively) is a no-op, not a fresh
    timestamp. `NotFound` covers both a decision that does not exist and one owned by
    a different agent -- the same artifact-join shape `comments.resolve` uses, so the
    two look identical from outside exactly as `store.patch`'s ownership check makes
    them look identical there too."""
    with db.connect() as conn:
        row = conn.execute(
            f"select {_COLUMNS} from decision"  # noqa: S608 -- fixed constant, not caller input
            " join artifact on artifact.id = decision.artifact_id"
            " where decision.id = %s and artifact.agent = %s",
            (decision_id, agent),
        ).fetchone()
        if row is None:
            raise store.NotFound()
        if row[11] is None:  # closed_at
            # Closing implies acknowledgement -- an agent that finished the work
            # necessarily heard the answer -- but only when nothing has acknowledged
            # it yet, so an earlier, truer "seen at" is never overwritten by a later
            # close. `coalesce` does both in one statement.
            conn.execute(
                "update decision set closed_at = now(),"
                " acknowledged_at = coalesce(acknowledged_at, now()),"
                " acknowledged_by = coalesce(acknowledged_by, %s)"
                " where id = %s",
                (agent, decision_id),
            )
            row = conn.execute(
                f"select {_COLUMNS} from decision where decision.id = %s",  # noqa: S608 -- fixed constant, not caller input
                (decision_id,),
            ).fetchone()
    return _row_to_decision(row)


def acknowledge(db: Database, agent: str, decision_id: str) -> DecisionRow:
    """"I have seen this answer" -- a different fact from "the work is done".

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
    """
    with db.connect() as conn:
        row = conn.execute(
            f"select {_COLUMNS} from decision"  # noqa: S608 -- fixed constant, not caller input
            " join artifact on artifact.id = decision.artifact_id"
            " where decision.id = %s and artifact.agent = %s",
            (decision_id, agent),
        ).fetchone()
        if row is None:
            raise store.NotFound()
        if row[12] is None:  # acknowledged_at
            conn.execute(
                "update decision set acknowledged_at = now(), acknowledged_by = %s"
                " where id = %s",
                (agent, decision_id),
            )
            row = conn.execute(
                f"select {_COLUMNS} from decision where decision.id = %s",  # noqa: S608 -- fixed constant, not caller input
                (decision_id,),
            ).fetchone()
    return _row_to_decision(row)


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
        f"select {_COLUMNS}, artifact.agent, seat_read.last_read_at from decision"  # noqa: S608 -- fixed constant, not caller input
        " join artifact on artifact.id = decision.artifact_id"
        " left join seat_read on seat_read.agent = artifact.agent"
        " where decision.answered_at is not null"
        " and decision.acknowledged_at is null"
        " and decision.answered_at < now() - make_interval(secs => %s)"
    )
    params: list[object] = [stuck_after_seconds]
    if agent is not None:
        sql += " and artifact.agent = %s"
        params.append(agent)
    sql += " order by decision.answered_at asc"

    with db.connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()

    out = []
    for row in rows:
        answered_at, last_read_at = row[8], row[-1]
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
            )
        )
    return out
