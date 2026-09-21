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

import re
from dataclasses import dataclass
from datetime import datetime

from psycopg.types.json import Jsonb

from artifacts import events, ids, nudge, store, text
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
    " decision.acknowledged_at, decision.acknowledged_by,"
    " decision.answer_source, decision.recorded_by, decision.said_at,"
    " decision.session"
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
        answer_source=row[14],
        recorded_by=row[15],
        said_at=row[16].isoformat() if row[16] is not None else None,
        session=row[17],
    )


def ask(
    db: Database,
    *,
    agent: str,
    artifact_id: str,
    question: str,
    options: list[dict],
    recommended: str | None = None,
    session: str | None = None,
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
            # `session` is which SESSION raised the question -- a label the
            # caller declared, never a permission (see api.session). It is what
            # lets a seat sharing a key with two others recognise its own
            # question in a feed, and what lets the answer wake the right one.
            "insert into decision"
            " (id, artifact_id, version_n, question, options, recommended, session)"
            " values (%s, %s, %s, %s, %s, %s, %s)",
            (
                decision_id,
                artifact_id,
                version_n,
                question,
                Jsonb(validated_options),
                recommended,
                session,
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
    db: Database,
    agent: str,
    *,
    include_answered: bool = False,
    include_closed: bool = False,
    limit: int = 200,
) -> list[DecisionRow]:
    """Newest first -- this is the polling surface an agent uses to learn Jeremy's
    answer, so newest matters more than oldest, exactly as `comments.list_for_agent`
    is ordered for the same reason. `limit` protects an agent with a long history
    from pulling its entire back catalogue on every poll.

    `include_closed` exists as of 21 Sep 2026 and did not before. The reason it
    did not was written here in full -- "the agent withdrew it, so there is
    nothing left here for that agent to learn" -- and that reason STOPPED BEING
    TRUE the same morning, when Jeremy ruled that archiving or deleting a page
    withdraws its open questions. The SERVICE withdraws them now, not the agent,
    so the agent did not know and there is a great deal left to learn. A
    withdrawn question was then unreadable on every route a polling agent could
    reach: not wrong, not redacted, simply no longer listed (homelab-ij8.44,
    reported independently by two seats on 19 Sep for a different route).

    Still excluded by DEFAULT, because a withdrawn question is not waiting on
    anybody and a poll asking "what is outstanding" should not be handed it."""
    sql = (
        f"select {_COLUMNS} from decision"  # noqa: S608 -- fixed constant, not caller input
        " join artifact on artifact.id = decision.artifact_id"
        " where artifact.agent = %s"
    )
    if not include_closed:
        sql += " and decision.closed_at is null"
    if not include_answered:
        sql += " and decision.answer is null"
    sql += " order by decision.created_at desc, decision.id desc limit %s"
    with db.connect() as conn:
        rows = conn.execute(sql, (agent, limit)).fetchall()
    return [_row_to_decision(row) for row in rows]


# Where an answer can have come from. 'page' is a tap in the gallery; the rest
# are places Jeremy actually says things. Enumerated rather than free text so a
# reader can switch on it, and so a seat cannot invent a source that sounds more
# authoritative than it is.
ANSWER_SOURCES = ("terminal", "telegram", "in-person", "other")


class InvalidSource(Exception):
    """A relay named a source that is not one of ANSWER_SOURCES, or named 'page'.

    'page' is refused deliberately: it is the one value that means "he tapped
    this himself", and a relay claiming it would be indistinguishable from the
    real thing — which is the entire failure this call exists to prevent.
    """


def record_elsewhere(
    db: Database,
    decision_id: str,
    *,
    letter: str,
    note: str,
    source: str,
    recorded_by: str,
    said_at: datetime | None = None,
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
    agent that closed it said it no longer needs one.
    """
    if source not in ANSWER_SOURCES:
        raise InvalidSource(source)
    if not recorded_by:
        raise InvalidSource("a relayed answer must name the seat recording it")
    store._check_size(note, MAX_NOTE_BYTES, "note", agent=recorded_by, artifact_id=None)

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

        if letter not in {option["letter"] for option in options}:
            raise InvalidAnswer()
        store._check_secrets(note, "note", agent=recorded_by, artifact_id=artifact_id)

        label = next(o["label"] for o in options if o["letter"] == letter)
        summary = f"{letter}: {label} (relayed from {source})"[:200]
        with conn.transaction():
            conn.execute(
                "update decision set answer = %s, answer_note = %s,"
                " answered_at = now(), answered_by = 'jeremy',"
                " answer_source = %s, recorded_by = %s, said_at = %s"
                " where id = %s",
                (letter, note, source, recorded_by, said_at, decision_id),
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
            )
            refreshed = conn.execute(
                f"select {_COLUMNS} from decision where id = %s",  # noqa: S608 -- fixed constant
                (decision_id,),
            ).fetchone()
    return _row_to_decision(refreshed)


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
        # The session that ASKED, taken from inside _COLUMNS (last entry) rather
        # than from the end of the row, which is where the two joined columns
        # live. The event carries this so the seat that raised the question can
        # recognise its own answer in a feed three seats share -- see
        # events.list_for_agent's `session`.
        asking_session = row[len(_COLUMNS.split(",")) - 1]

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
                " answered_by = %s, answer_source = 'page', recorded_by = null,"
                " said_at = null where id = %s",
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
                # HIS WORDS, on the event. Without this the feed says only
                # "a: Yes" and the qualification he typed never leaves the
                # database.
                note=note,
                # The session that ASKED, not the one that answered: Jeremy has
                # no session, and the whole point is that the seat which raised
                # the question can pick its own answer out of a shared feed.
                session=asking_session,
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
    # WHOSE answer this is, and which page. Both are redundant when a seat asks
    # about itself and both are the whole point when the OPERATOR asks about
    # everyone: an alert that says "one answer is stranded" without saying whose
    # or on what sends its reader back to the database to find out, which is how
    # an alert stops being read. `WaitingRow` below carries the same two for the
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
        # `is not distinct from` rather than `=`, because a decision asked
        # before sessions existed has NULL here and must match the row for the
        # unlabelled reader, which also has NULL. Plain `=` is never true for
        # NULL, so every pre-session decision would read as no_reader for ever.
        " left join seat_read on seat_read.agent = artifact.agent"
        "   and seat_read.session is not distinct from decision.session"
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
class WaitingRow:
    """One open question, described so that whoever reads the operator's queue
    knows which page to open and how long it has been sitting there."""

    decision: DecisionRow
    agent: str
    artifact_title: str
    age_seconds: int


def open_across_seats(db: Database, *, limit: int = 200) -> list[WaitingRow]:
    """Every open question on every page, oldest first.

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

    `age_seconds` is computed by the database that stamped `created_at`, so a
    consumer's own clock never disagrees with the service about how old something
    is -- and a queue that reads "17 hours" is telling Jeremy something the
    service is prepared to stand behind.
    """
    sql = (
        f"select {_COLUMNS}, artifact.agent, artifact.title,"  # noqa: S608 -- fixed constant, not caller input
        " extract(epoch from now() - decision.created_at)::bigint from decision"
        " join artifact on artifact.id = decision.artifact_id"
        " where decision.closed_at is null and decision.answer is null"
        " order by decision.created_at asc, decision.id asc limit %s"
    )
    with db.connect() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [
        WaitingRow(
            decision=_row_to_decision(row),
            agent=row[-3],
            artifact_title=row[-2],
            age_seconds=int(row[-1]),
        )
        for row in rows
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
            "select id, decision_id, body, agent, session, created_at"
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
            "select id, decision_id, body, agent, session, created_at"
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


# A whole HTML comment, including the tokenizer's abrupt closes (`<!-->`,
# `<!--->`) and its incorrect one (`--!>`); an unterminated comment runs to the
# end of the document, as it does in a browser.
_HTML_COMMENT = re.compile(r"<!--(?:-?>|.*?(?:--!?>|\Z))", re.DOTALL)


def _outside_comments(body: str) -> str:
    """`body` with every HTML comment blanked, newlines kept so lines still count.

    Every shipped template that has a slot ships it COMMENTED OUT, to be
    uncommented once the question exists -- so counting a commented slot
    warned every agent that used a template exactly as told. Found on 21 Sep
    by sweeping the live pages minutes after the first version of this check
    shipped: the one "dead" slot on the wall was inside a comment, where no
    browser ever sees it. A warning that fires on correct pages trains authors
    to ignore the warning.
    """
    return _HTML_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), body)


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
    body = _outside_comments(body)
    first: dict[str, int] = {}
    for match in _SLOT_IN_BODY.finditer(body):
        slot = match.group(1) or match.group(2)
        if slot not in first:
            first[slot] = body.count("\n", 0, match.start()) + 1
    if not first:
        return []
    with db.connect() as conn:
        rows = conn.execute(
            "select id from decision where artifact_id = %s and id = any(%s)",
            (artifact_id, list(first)),
        ).fetchall()
    real = {r[0] for r in rows}
    return [(slot, line) for slot, line in first.items() if slot not in real]


def outcomes_for_artifact(db: Database, artifact_id: str) -> dict[str, list[OutcomeRow]]:
    """Every outcome on every decision of one page, keyed by decision id.

    One query rather than one per decision: the gallery renders a page with
    several questions on it, and a per-decision read there is a round trip per
    question on every load.
    """
    out: dict[str, list[OutcomeRow]] = {}
    with db.connect() as conn:
        rows = conn.execute(
            "select o.id, o.decision_id, o.body, o.agent, o.session, o.created_at"
            " from decision_outcome o"
            " join decision d on d.id = o.decision_id"
            " where d.artifact_id = %s"
            " order by o.created_at asc, o.id asc",
            (artifact_id,),
        ).fetchall()
    for row in rows:
        out.setdefault(row[1], []).append(_row_to_outcome(row))
    return out
