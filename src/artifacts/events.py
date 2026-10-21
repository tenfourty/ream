r"""The nudge journal: `append` writes the one row every interaction Jeremy has
with a page produces, and `list_for_agent` is how an agent -- on this machine or,
per Jeremy's own requirement, anywhere he gives access to -- reads them back.

`append` takes an open connection, not a `Database`: it is always called from
inside a caller's own `with db.connect() as conn:` block, in the same statement
group as the write the event describes (see `decisions.answer`, the first
caller), so the two commit or roll back together. Calling it with a fresh
connection of its own would defeat the entire point -- an event that outlives a
rolled-back cause, or a cause with no event at all -- so this module never opens
one itself for a write.

`list_for_agent` is the opposite: always a fresh, read-only connection, exactly
like `decisions.list_for_agent`/`comments.list_for_agent`. It is scoped to the
calling agent alone (never another agent's events) and ordered strictly by
`seq`, which is what makes `since` a real cursor -- two events sharing one
`created_at` still come back in a fixed, repeatable order, so a caller polling
with `since=<last seq it saw>` can never skip or repeat a row.
"""

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from artifacts import text
from artifacts.db import TRANSIENT, Database

# The three kinds the design names (schema.sql's own CHECK constraint is the
# enforced copy of this set) -- defined here even though only
# "decision_answered" has a live producer this slice, so wiring up the other
# two later is a line of code at a new call site, never a migration to widen
# what this journal accepts.
KINDS = (
    "decision_answered",
    "comment_added",
    "thread_resolved",
    # The page holding an open question left the wall -- archived or deleted.
    # Its own kind rather than a flavour of the others because it is the only
    # one that means "stop waiting": an agent holding a waiter on that decision
    # will otherwise sit until its own timeout, every time, unable to tell
    # "not yet" from "he is never going to be shown this".
    "decision_shelved",
    # GitGuardian flagged the agent's own comment after it was saved.
    "comment_withdrawn",
)

# A note is his own words and must survive the trip whole. 1000 is the same
# ceiling the decision itself enforces, so nothing can be stored that this
# cannot carry.
MAX_NOTE_IN_EVENT = 1_000

_COLUMNS = (
    "event.seq, event.artifact_id, event.agent, event.actor, event.kind,"
    " event.summary, event.ref_id, event.created_at, event.note, event.session"
)


@dataclass(frozen=True)
class EventRow:
    seq: int
    artifact_id: str
    agent: str
    actor: str
    kind: str
    summary: str
    ref_id: str | None
    created_at: str
    # What Jeremy actually wrote, verbatim. Empty when he wrote nothing.
    # Its own field, never folded into `summary`: a consumer showing the
    # summary in one line has to be able to show this differently, and
    # concatenating them would take that choice away from every consumer.
    note: str
    # Which session this event is ABOUT, when the thing it happened to carried
    # one. None for everything written before sessions existed, and for any
    # caller that did not declare one -- see `api.session`.
    session: str | None


def _row_to_event(row) -> EventRow:
    # `summary` is sanitised on the way in (see `append`) AND again here. Not
    # belt-and-braces for its own sake: rows written before that existed are
    # still in the table, and any future writer that reaches the table another
    # way is covered without having to remember. Sanitising is idempotent, so
    # a clean row is unchanged by it.
    return EventRow(
        seq=row[0],
        artifact_id=row[1],
        agent=row[2],
        actor=row[3],
        kind=row[4],
        summary=text.sanitize(row[5]),
        ref_id=row[6],
        created_at=row[7].isoformat(),
        note=text.sanitize(row[8], limit=MAX_NOTE_IN_EVENT),
        session=row[9],
    )


def append(
    conn,
    *,
    artifact_id: str,
    agent: str,
    actor: str,
    kind: str,
    summary: str,
    ref_id: str | None = None,
    note: str = "",
    session: str | None = None,
) -> int:
    """Insert one event row on `conn`, inside the caller's own transaction --
    see the module docstring. `agent` is the seat the event is FOR (the
    artifact's owner); `actor` is WHO caused it, and the caller must always
    say so explicitly -- this function never infers one from the other or
    from `kind`, because an event means "something happened on your page
    that you did not do", and inferring `actor` is exactly how that stops
    being true the first time it's wrong (see the module docstring, and
    `test_events.py`'s own test for this).

    Returns the new row's `seq`, which the caller passes to `nudge.emit` as
    `event_id` only after `conn`'s transaction has actually committed: a
    nudge for a row that then rolls back would tell another agent about
    something that never happened."""
    # The summary carries viewer-typed free text (a decision note, a comment)
    # and it is stored FLAT: whitespace collapsed, control characters gone,
    # length bounded. A consumer of this feed prints it into a terminal or an
    # agent's context, where an escape sequence is not inert and a newline can
    # fake a second line -- and there is no list of consumers to keep in step,
    # which is why this happens here rather than at each of them.
    # The note is sanitised too, and for the same reason -- but with a much
    # larger bound than the summary. A summary is a line in someone else's list;
    # a note is the thing Jeremy actually said, and truncating it to a line is
    # how "start with (b) but then (a) if we can" becomes "start with (b)".
    # `session` is which SESSION this event concerns -- the one that raised the
    # decision, or published the page -- not who caused it. It exists so a seat
    # sharing a key with two others can narrow its feed to its own work
    # (homelab-ij8.32). A label, never a scope: `agent` remains the only thing
    # that decides which rows a key may see.
    row = conn.execute(
        "insert into event"
        " (artifact_id, agent, actor, kind, summary, ref_id, note, session)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s) returning seq",
        (
            artifact_id,
            agent,
            actor,
            kind,
            text.sanitize(summary),
            ref_id,
            text.sanitize(note, limit=MAX_NOTE_IN_EVENT),
            session,
        ),
    ).fetchone()
    return row[0]


def list_for_agent(
    db: Database,
    agent: str,
    *,
    since: int = 0,
    limit: int = 100,
    session: str | None = None,
    exclude_actor: str | None = None,
) -> list[EventRow]:
    """Every event recorded for `agent`, strictly above `since`, oldest first --
    the order a cursor needs: a caller that stores the last `seq` it saw and asks
    again with `since=<that seq>` sees each row exactly once, in the order it was
    written. `limit` protects an agent that has been away a long time from
    pulling an unbounded backlog in one call.

    `session` narrows to one session's own work. IT IS A CONVENIENCE, NOT A
    BOUNDARY -- every row it hides was already visible to this key, and asking
    without it still returns all of them. The distinction matters because the
    value is caller-declared: treating it as a scope would be a check any key
    holder defeats by sending a different string.

    WHY IT EXISTS. homelab-31, 21 Sep 2026, after two false wakes in ten minutes
    on another seat's decisions: "a watcher which cries wolf trains its reader
    to stop looking, and the one wake that matters is indistinguishable from
    these." Three seats on this machine share one key, so `agent` cannot
    separate them and this can.

    A row with no session is EXCLUDED when filtering. The cautious direction, on
    purpose: everything written before sessions existed has NULL here, and
    folding those into every filtered feed would reproduce exactly the noise the
    filter is for. A caller that wants them omits the filter."""
    sql = f"select {_COLUMNS} from event where event.agent = %s and event.seq > %s"  # noqa: S608 -- _COLUMNS is a fixed module constant, never caller input
    params: tuple = (agent, since)
    if session is not None:
        sql += " and event.session = %s"
        params += (session,)
    # An agent's OWN actions, left out when it asks (homelab-ij8.106): the first
    # outside agent's listener woke on its own `record` of an answer he gave in
    # the terminal. A convenience like `session`, never a boundary.
    if exclude_actor is not None:
        # An answer is never the agent's own action, even when its key relayed
        # it: the decision is his and the relay only carried it. A key is
        # shared by every session on a machine, so leaving it out made a waiter
        # sleep through a verdict another session relayed (live proof of page
        # review step 2, 29 Sep 2026). A duplicate wake costs one look.
        # A comment_added event is only ever raised for a comment that is not the
        # owner's own: on a key several offices share, the actor's agent name is
        # the owner's, and leaving it out would hide the other office's comment.
        sql += (
            " and (event.actor <> %s"
            " or event.kind in ('decision_answered', 'comment_added'))"
        )
        params += (exclude_actor,)
    sql += " order by event.seq asc limit %s"
    params += (limit,)
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_event(row) for row in rows]


def note_read(db: Database, agent: str, session: str | None = None) -> None:
    """Record that this seat -- and this SESSION -- asked for its events, just now.

    Called at the START of a read, before anything is looked up, because a
    request that holds for 280 seconds is listening for every one of them --
    recording the time it finished would make the most attentive listener look
    like the most absent one, which is exactly backwards for the question this
    answers.

    Per SESSION, not merely per seat. Three sessions on this machine share one
    key, so a row keyed on the agent alone answered for whichever of them read
    most recently -- and the alert built on it could report a live listener
    while the session that actually asked had been gone for hours, which is the
    wrong answer in the one case it exists for (homelab-ij8.64).

    That question is "is anybody still listening on this seat?", and it exists
    because an answer Jeremy gave that nobody collected has two opposite causes:
    the agent is gone, so someone else has to collect it, or the agent is alive
    and stuck. Without this, those two are indistinguishable.

    Never raises. This is bookkeeping ALONGSIDE a read, not part of it: an agent
    asking for its events must still get them if the bookkeeping write fails, and
    a failure here degrades the alert, which is a safety net, rather than the feed,
    which is the feature. The cost of being wrong in the other direction is an
    agent that cannot read its answers because a statistics table was unhappy.
    """
    try:
        with db.connect() as conn:
            # An unlabelled caller is stored as session '' (not NULL), so
            # `on conflict (agent, session)` folds every caller that declares
            # no session into one row on both Postgres and SQLite, which has no
            # NULLS NOT DISTINCT.
            conn.execute(
                "insert into seat_read (agent, session, last_read_at)"
                " values (%s, %s, %s)"
                " on conflict (agent, session)"
                " do update set last_read_at = excluded.last_read_at",
                (agent, session or "", datetime.now(UTC)),
            )
    except Exception:  # noqa: BLE001 -- see the docstring: this must never fail a read
        return


def last_read(db: Database, agent: str, session: str | None = None) -> str | None:
    """When this seat last asked for its events, or None if it never has.

    With `session`, when THAT session last did -- which is the question the
    stranded-answer alert actually needs. Without it, the most recent read by
    any session on the seat, which is the older and weaker fact and is still
    what a human asking "is that box alive at all" wants.
    """
    # The alias's bracketed type is how SQLite types a computed column.
    sql = (
        'select max(last_read_at) as "last [TIMESTAMPTZ]" from seat_read'
        " where agent = %s"
    )
    params: tuple = (agent,)
    if session is not None:
        sql += " and session = %s"
        params += (session,)
    with db.connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return row[0].isoformat() if row and row[0] else None


# How many agents may hold a waiting request at once. Each one occupies a worker
# thread for as long as it waits, and FastAPI's thread pool is finite: without a
# ceiling, enough waiters would starve every ordinary request in the app,
# including the gallery Jeremy is reading. Past the ceiling a caller is answered
# immediately with whatever is there, which is exactly the behaviour it had
# before waiting existed -- it degrades to ordinary polling rather than failing.
MAX_WAITERS = 16

# How often a waiting request looks again. A held request is woken by finding a
# row, not by being told, so this is the worst-case delay between Jeremy pressing
# a button and the agent knowing. Half a second is imperceptible to him and costs
# one indexed lookup per waiter per half-second -- the index is (agent, seq), the
# same one an ordinary poll uses.
_LOOK_EVERY = 0.5

# How long a HELD wait rides out looks that cannot reach the database, before
# it gives up and says so. Measured 21 Sep 2026 from the ingress log: the only
# two 500s this route served all day were a single look failing to resolve
# the database's service name (04:59 and 12:59 UTC), each after 90-120s of
# holding, with requests either side of them succeeding. A held request makes
# a look every half second, so it is the request most exposed to a DNS blip,
# and one failed look threw away the whole hold. Long enough for a resolver
# hiccup; short enough that a database that is really gone is reported as an
# error within one hold rather than disguised as silence.
_TOLERATE_FAILED_LOOKS_FOR = 10.0

_waiters = threading.BoundedSemaphore(MAX_WAITERS)

# Set when the process has been asked to stop. A held request must END when
# that happens, promptly and with a normal answer -- it must not sleep on
# until the platform kills it.
#
# Measured on 19 Sep 2026: every deploy that evening silently broke every
# waiting agent. The pod rolls, the old process is asked to stop, a request
# sleeping out a 280-second hold does not finish, and 30 seconds later the
# platform kills it. The client does not see a clean end; it sees a request
# that never answers, and the old client called that silence. Two seats logged
# the identical signature -- "timed out after 300001 milliseconds with 0 bytes
# received" -- at the minute of a roll, while the same service answered
# wait=5, wait=30, wait=70, wait=110 and wait=180 exactly on time.
#
# So the hold waits on THIS, not on the clock: `Event.wait` returns the moment
# it is set, so shutdown ends every held request at once instead of after up
# to `_LOOK_EVERY` more seconds, and the caller gets a 200 with whatever is
# there. An empty 200 already means "nothing yet, ask again", which is exactly
# what a client should do when the server it was talking to is going away.
_shutdown = threading.Event()


def begin_shutdown() -> None:
    """End every held request now. Wired to SIGTERM in `artifacts.serve`."""
    _shutdown.set()


def clear_shutdown() -> None:
    """Undo `begin_shutdown`. For tests, and for a process that is told to stop
    and then is not."""
    _shutdown.clear()


def wait_for_shutdown(timeout: float) -> bool:
    """Sleep up to `timeout` seconds, returning True the instant this process is
    asked to stop.

    The one way anything in this service is allowed to wait. A plain
    `time.sleep` in a held request is what turns a rollout into a pod that
    lingers for its whole grace period and then has its connections cut --
    `begin_shutdown` exists so a hold ends the moment SIGTERM lands, and every
    loop that waits has to honour it. Used by `wait_for_agent` below and by the
    gallery's "has this page been republished" hold (homelab-ij8.74).
    """
    return _shutdown.wait(timeout=timeout)


def wait_for_agent(
    db: Database,
    agent: str,
    *,
    since: int = 0,
    limit: int = 100,
    seconds: float,
    session: str | None = None,
    exclude_actor: str | None = None,
) -> list[EventRow]:
    """Hold until an event lands for `agent` above `since`, or `seconds` pass.

    This is the mechanism by which an agent anywhere hears about an answer.
    A push has to know an address for every agent; a held request does not --
    the agent is already on the other end of it, so answering it IS the
    notification, and an agent on a laptop is served exactly like one on baba.

    Returns whatever is there when it gives up, which may be nothing: an empty
    answer means "nothing yet, ask again", never an error, so a caller loops on
    it forever without special-casing a timeout.

    Never holds a database connection while it sleeps -- each look is its own
    short connection through `list_for_agent` -- so a hundred waiting agents
    cost sleeping threads, not a hundred pooled connections nobody can use.
    """
    if not _waiters.acquire(blocking=False):
        # Too many agents already waiting. Answer now with whatever is there
        # rather than queueing behind them: a caller that gets an empty answer
        # early simply asks again, which is ordinary polling.
        return list_for_agent(
            db,
            agent,
            since=since,
            limit=limit,
            session=session,
            exclude_actor=exclude_actor,
        )
    try:
        deadline = time.monotonic() + seconds
        looked_once = False
        failing_since: float | None = None
        while True:
            if _shutdown.is_set():
                # Asked to stop before this hold even began: answer at once
                # rather than opening a request that cannot be honoured.
                return list_for_agent(
                    db,
                    agent,
                    since=since,
                    limit=limit,
                    session=session,
                    exclude_actor=exclude_actor,
                )
            # The deadline is checked BEFORE the look as well as after. A look
            # is a database round trip, not a free operation, and the caller was
            # promised an answer by a wall-clock moment: starting one more look
            # after the deadline has passed spends time nobody agreed to.
            if time.monotonic() >= deadline:
                return []
            try:
                rows = list_for_agent(
                    db,
                    agent,
                    since=since,
                    limit=limit,
                    session=session,
                    exclude_actor=exclude_actor,
                )
                failing_since = None
            except TRANSIENT:
                # The FIRST look is never excused: a database that is down
                # when the request arrives is an error now, not after a hold.
                # A later one is "nothing seen this time" and is retried, which
                # loses nothing -- an empty answer means "ask again from the
                # same cursor" -- until it has failed for long enough to be an
                # outage, and then the outage is raised, never reported as
                # nobody having answered.
                now = time.monotonic()
                if not looked_once:
                    raise
                if failing_since is None:
                    failing_since = now
                if now - failing_since >= _TOLERATE_FAILED_LOOKS_FOR:
                    raise
                rows = []
            looked_once = True
            if rows:
                return rows
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            # Never overshoot the deadline: a caller that asked for 30 seconds
            # gets at most 30, not 30 rounded up to the next look. And never
            # outlive a shutdown: `Event.wait` returns the instant `_shutdown`
            # is set, so the answer goes out while the process can still send
            # it -- one last look first, so a row that landed in the same
            # moment is not thrown away.
            if _shutdown.wait(timeout=min(_LOOK_EVERY, remaining)):
                return list_for_agent(
                    db,
                    agent,
                    since=since,
                    limit=limit,
                    session=session,
                    exclude_actor=exclude_actor,
                )
    finally:
        _waiters.release()


def latest_seq(db: Database, agent: str) -> int:
    """The newest event number for `agent`, or 0: the cursor that means "from
    now". The first outside agent had to page the whole feed from 0 to find it
    (homelab-ij8.106); every /events answer now carries it in a header."""
    with db.connect() as conn:
        row = conn.execute(
            "select coalesce(max(seq), 0) from event where agent = %s", (agent,)
        ).fetchone()
    return int(row[0])
