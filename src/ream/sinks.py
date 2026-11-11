"""Event sinks and the core outbox (core-and-plugins design, 4.1 and 4.4).

`events.emit` is the one place an action says "this happened". This module is what
it fans out to: a registry of sinks, the `outbox` table that holds a delivery
until it has happened, and the sender thread that drains it.

A sink names the kinds it wants and how it is delivered:

- `delivery="outbox"` with no `stage`: a row in `outbox` is written in the action's
  transaction, and the sender thread calls `deliver(event)` after the commit,
  retrying with backoff (at-least-once; `event.id` is the idempotency key).
- `delivery="outbox"` with a `stage`: the sink keeps its own queue and `stage(conn,
  event)` writes to it in the action's transaction. The partner bridge is this:
  its per-question version counter and ordering live in `partner_outbox`, which
  its own sender still drains, so rows written before this module existed keep
  delivering untouched.
- `delivery="after_commit"`: `deliver(event)` is called once, best effort, after
  the commit, and never raises into the caller (the nudge).

Nothing here loads a plug-in (slice 7). The two sinks below are the code that
was already in core, registered the way a plug-in later will be.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ream import db as dbmod
from ream import ids

if TYPE_CHECKING:
    from ream.db import Connection, Database
    from ream.events import Event

_log = logging.getLogger(__name__)

DELIVERIES = ("outbox", "after_commit")
LEASE_SECONDS = 60
# One `deliver` call gets this long (less than the lease, so a row is never
# delivered by a second sender while the first is still inside its call). The
# sink protocol: `deliver` must return within it. A call that does not is
# abandoned as a failure ("TimeoutError"): retried with backoff, parked after
# MAX_ATTEMPTS. It runs in its own daemon thread, which cannot be killed, so a
# sink that really hangs leaks one thread per attempt; a late success of an
# abandoned call is a duplicate, which at-least-once delivery already allows.
DELIVER_TIMEOUT_SECONDS = 30.0
POLL_SECONDS = 2.0
BATCH = 20
BACKOFF_BASE_SECONDS = 5
BACKOFF_CAP_SECONDS = 15 * 60
# Parked, not dropped, after this many failed attempts: it waits for a release.
MAX_ATTEMPTS = 20
# The circuit breaker (4.5): a sink that fails (a raise or a timeout) this many
# deliveries running is skipped for BREAKER_COOLDOWN_SECONDS. Outbox rows of a skipped
# sink wait untouched (no attempt counted, nothing parked); an after-commit delivery is
# skipped. When the cooldown ends ONE delivery is let through as a trial: a success
# closes the breaker, a failure opens it for another cooldown. The state is shared by
# every process on the database (`bind`); without one it is per process.
BREAKER_THRESHOLD = 5
BREAKER_COOLDOWN_SECONDS = 60
KEEP_DELIVERED = timedelta(days=30)
_PRUNE_EVERY = timedelta(hours=1)


@dataclass(frozen=True)
class Sink:
    name: str
    kinds: tuple[str, ...]
    deliver: Callable[[Event], None] | None = None
    delivery: str = "outbox"
    stage: Callable[[Connection, Event], None] | None = None
    # Human words for wording ("waiting for quarterdeck"); None shows `name`.
    label: str | None = None

    def __post_init__(self) -> None:
        if self.delivery not in DELIVERIES:
            raise ValueError(f"unknown delivery {self.delivery!r}")
        if self.deliver is None and self.stage is None:
            raise ValueError("a sink needs deliver or stage")
        if self.delivery == "after_commit" and self.deliver is None:
            raise ValueError("an after_commit sink needs deliver")


_lock = threading.RLock()
_sinks: dict[str, Sink] = {}
_builtins_loaded = False
# The plug-in sink that takes over core's doorbell (step 9d); logged once per process.
DOORBELL_PLUGIN_SINK = "doorbell"
_doorbell_stood_down_logged = False
_late_failures = 0
# sink name -> [consecutive failures, open until (None: closed), the distinct rows that
# failed (capped at 2), when it last opened (None: never)]. This is the state of a
# process with no database bound (tools, unit tests) and the fallback when the shared
# copy cannot be reached.
_breakers: dict[str, list] = {}
# The database that holds the shared copy of the breaker state and the plug-in
# failure counts (tables `sink_breaker`, `plugin_failure`), so every worker process of
# a deployment sees one breaker per sink. `serve.py` binds it, `stop()` unbinds it.
_shared: Database | None = None


def bind(db: Database | None) -> None:
    """Keep breaker state and plug-in failure counts in `db` (None: in memory again)."""
    global _shared
    _shared = db


def shared() -> Database | None:
    """The database the shared state lives in, or None when it is per process."""
    return _shared


def _fail_state(
    state: list | None, ok: bool, now: datetime, started, key: str | None
) -> tuple[list | None, bool]:
    """One step of the breaker's rules on `state` (see `breaker_record`). Returns the
    new state (None: closed, nothing to keep) and whether the breaker just opened."""
    if ok:
        if (
            state is not None
            and state[3] is not None
            and started is not None
            and started < state[3]
        ):
            return state, False
        return None, False
    state = [0, None, set(), None] if state is None else state
    state[0] += 1
    if len(state[2]) < 2:
        state[2].add(key if key is not None else f"anon-{state[0]}")
    first = False
    if state[0] >= BREAKER_THRESHOLD and len(state[2]) >= 2:
        first = state[3] is None or state[1] is None
        state[1] = now + timedelta(seconds=BREAKER_COOLDOWN_SECONDS)
        state[3] = now
    return state, first


def _read_state(conn: Connection, name: str) -> list | None:
    row = conn.execute(
        "select failures, open_until, failed_keys, opened_at from sink_breaker"  # noqa: S608
        " where sink = %s" + conn.for_update(),
        (name,),
    ).fetchone()
    if row is None:
        return None
    return [row[0], row[1], {k for k in row[2].split("\n") if k}, row[3]]


def _write_state(conn: Connection, name: str, state: list | None) -> None:
    if state is None:
        conn.execute("delete from sink_breaker where sink = %s", (name,))
        return
    conn.execute(
        "insert into sink_breaker (sink, failures, open_until, failed_keys, opened_at)"
        " values (%s, %s, %s, %s, %s)"
        " on conflict (sink) do update set failures = excluded.failures,"
        " open_until = excluded.open_until, failed_keys = excluded.failed_keys,"
        " opened_at = excluded.opened_at",
        (name, state[0], state[1], "\n".join(sorted(state[2])), state[3]),
    )


def _shared_or_none(work):
    """Run `work(conn)` in a transaction on the shared database; None if none is bound
    or it cannot be reached (the caller then uses the in-memory copy)."""
    db = _shared
    if db is None:
        return None
    try:
        with db.connect() as conn, conn.transaction():
            return (work(conn),)
    except Exception:  # noqa: BLE001 -- a breaker must never stop a delivery
        _log.warning("sink breaker state: shared copy unreachable", exc_info=True)
        return None


def _shared_read_or_none(work):
    """Like `_shared_or_none` for a read: a plain connection, no transaction (on SQLite
    `transaction()` is BEGIN IMMEDIATE, a writer lock taken for a read)."""
    db = _shared
    if db is None:
        return None
    try:
        with db.connect() as conn:
            return (work(conn),)
    except Exception:  # noqa: BLE001 -- a breaker must never stop a delivery
        _log.warning("sink breaker state: shared copy unreachable", exc_info=True)
        return None


def breaker_peek(name: str, now: datetime) -> bool:
    """Whether a delivery to `name` could be tried now: closed, or the cooldown has
    passed. Read-only: it spends no trial, so it is safe to ask about every sink."""

    def work(conn):
        row = conn.execute(
            "select open_until from sink_breaker where sink = %s", (name,)
        ).fetchone()
        return row is None or row[0] is None or now >= row[0]

    got = _shared_read_or_none(work)
    if got is not None:
        return got[0]
    with _lock:
        state = _breakers.get(name)
        return state is None or state[1] is None or now >= state[1]


def breaker_reserve(name: str, now: datetime) -> bool:
    """Take the right to deliver to `name` now. Closed: yes. Open: no. Once the cooldown
    has passed the first caller (in any process) gets it and the breaker re-arms at once
    for another cooldown, so only one trial runs; `breaker_record` settles it. Call this
    only for a delivery that is about to run."""

    def work(conn):
        state = _read_state(conn, name)
        if state is None or state[1] is None:
            return True
        if now < state[1]:
            return False
        state[1] = now + timedelta(seconds=BREAKER_COOLDOWN_SECONDS)
        _write_state(conn, name, state)
        return True

    got = _shared_or_none(work)
    if got is not None:
        return got[0]
    with _lock:
        state = _breakers.get(name)
        if state is None or state[1] is None:
            return True
        if now < state[1]:
            return False
        state[1] = now + timedelta(seconds=BREAKER_COOLDOWN_SECONDS)
        return True


def breaker_allows(name: str, now: datetime) -> bool:
    """Peek and reserve in one (for a caller that delivers at once)."""
    return breaker_reserve(name, now)


def breaker_record(
    name: str,
    ok: bool,
    now: datetime,
    *,
    started: datetime | None = None,
    key: str | None = None,
) -> None:
    """The outcome of a delivery: success closes the breaker and clears the count;
    the THRESHOLD-th failure running (and each failed trial after) opens it, but only
    when the failures come from at least two different rows (`key`; None counts as a
    row of its own), so one poison event retried again and again cannot open the
    breaker of a healthy sink. A success of a call that `started` before the breaker
    last opened is ignored: it says nothing about the sink now. With a database bound
    the count and the state are shared by every process using it."""

    def work(conn):
        state, first = _fail_state(_read_state(conn, name), ok, now, started, key)
        _write_state(conn, name, state)
        return state, first

    got = _shared_or_none(work)
    if got is not None:
        state, first = got[0]
    else:
        with _lock:
            state, first = _fail_state(_breakers.get(name), ok, now, started, key)
            if state is None:
                _breakers.pop(name, None)
            else:
                _breakers[name] = state
    if first and state is not None:
        _log.warning(
            "sink %s: circuit open for %ds after %d failures running",
            name,
            BREAKER_COOLDOWN_SECONDS,
            state[0],
        )


def open_breakers(now: datetime | None = None) -> dict[str, str]:
    """Sinks whose breaker is open now, with when it next lets a trial through."""
    when = now or datetime.now(UTC)

    def work(conn):
        rows = conn.execute(
            "select sink, open_until from sink_breaker where open_until is not null"
            " order by sink"
        ).fetchall()
        return {r[0]: r[1].isoformat() for r in rows if r[1] > when}

    got = _shared_read_or_none(work)
    if got is not None:
        return got[0]
    with _lock:
        return {
            n: st[1].isoformat()
            for n, st in sorted(_breakers.items())
            if st[1] is not None and st[1] > when
        }


def register(sink: Sink) -> None:
    """Add a sink. A name already in use, a built-in's included, is a ValueError here, at
    start-up, never later in an event-emitting call. (`_ensure_builtins` writes the
    registry itself, not through here, so there is no recursion.)"""
    _ensure_builtins()
    with _lock:
        if sink.name in _sinks:
            raise ValueError(f"sink {sink.name!r} is already registered")
        _sinks[sink.name] = sink


def _ensure_builtins() -> None:
    """Register the two in-core sinks. All under the lock, and the flag is set
    last: a concurrent `emit` either waits here or sees the full registry, never
    an empty one."""
    global _builtins_loaded
    with _lock:
        if _builtins_loaded:
            return
        for sink in _builtin_sinks():
            if sink.name in _sinks:
                raise ValueError(f"sink {sink.name!r} is already registered")
            _sinks[sink.name] = sink
        _builtins_loaded = True


def label_of(name: str) -> str:
    """A sink's label for a sentence, else its name; an unknown sink shows its name."""
    for sink in registered():
        if sink.name == name:
            return sink.label or sink.name
    return name


def registered() -> list[Sink]:
    _ensure_builtins()
    with _lock:
        return list(_sinks.values())


def matching(kind: str) -> list[Sink]:
    return [s for s in registered() if kind in s.kinds]


@contextmanager
def isolated(*sinks: Sink) -> Iterator[None]:
    """Tests: swap the registry for exactly `sinks`, restore it after."""
    global _sinks, _builtins_loaded
    _ensure_builtins()
    with _lock:
        saved, saved_loaded = _sinks, _builtins_loaded
        _sinks, _builtins_loaded = {}, True
    with _lock:
        _breakers.clear()
    try:
        for sink in sinks:
            register(sink)
        yield
    finally:
        with _lock:
            _sinks, _builtins_loaded = saved, saved_loaded
            _breakers.clear()


def late_failures() -> int:
    """After-commit deliveries that raised, since the process started."""
    return _late_failures


def deliver_late(sinks: list[Sink], event: Event) -> None:
    """Run the after-commit sinks. Never raises; a failure is a log line (the
    exception's type only) and a count."""
    global _late_failures
    for sink in sinks:
        started = datetime.now(UTC)
        if sink.deliver is None or not breaker_reserve(sink.name, started):
            continue
        try:
            sink.deliver(event)
            breaker_record(sink.name, True, datetime.now(UTC), started=started)
        except Exception as exc:  # noqa: BLE001 -- the action already committed
            breaker_record(sink.name, False, datetime.now(UTC))
            with _lock:
                _late_failures += 1
            _log.warning(
                "sink %s failed after commit: %s", sink.name, type(exc).__name__
            )


# --- the outbox table ------------------------------------------------------------


def write_outbox(conn: Connection, sink: str, event: Event) -> str:
    """One pending delivery, in the caller's open transaction."""
    row_id = ids.new_id()
    when = datetime.now(UTC)
    conn.execute(
        "insert into outbox (id, sink, event_json, created_at, next_at)"
        " values (%s, %s, %s, %s, %s)",
        (row_id, sink, dbmod.jsonb(event.as_json()), when, when),
    )
    return row_id


def _backoff(attempts: int) -> timedelta:
    return timedelta(
        seconds=min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempts - 1))
    )


def _deliverable() -> dict[str, Sink]:
    return {
        s.name: s
        for s in registered()
        if s.delivery == "outbox" and s.stage is None and s.deliver is not None
    }


def _claim(db: Database, now: datetime, names: list[str]) -> dict | None:
    """The oldest due row of a registered sink, leased. A row of a sink that is
    not registered is never claimed: it waits for the plug-in to come back."""
    if not names:
        return None
    due = (
        " where delivered_at is null and parked_at is null and next_at <= %s"
        " and (claimed_until is null or claimed_until <= %s)"
        " and sink in " + dbmod.in_list(names)
    )
    params = (now, now, *names)
    with db.connect() as conn:
        waiting = conn.execute(
            "select 1 from outbox" + due + " limit 1",  # noqa: S608 -- placeholders only
            params,
        ).fetchone()
    if waiting is None:
        return None
    lease = now + timedelta(seconds=LEASE_SECONDS)
    with db.connect() as conn, conn.transaction():
        row = conn.execute(
            "select id, sink, event_json, attempts from outbox"  # noqa: S608 -- placeholders only
            + due
            + " order by created_at, id limit 1"
            + conn.for_update(" skip locked"),
            params,
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "update outbox set claimed_until = %s where id = %s", (lease, row[0])
        )
    return {
        "id": row[0],
        "sink": row[1],
        "event_json": row[2],
        "attempts": row[3],
        "lease": lease,
    }


def _call_bounded(
    deliver: Callable[[Event], None], event: Event, name: str = "sink"
) -> None:
    """`deliver(event)` within DELIVER_TIMEOUT_SECONDS, else raise TimeoutError.
    What `deliver` raises is raised here."""
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            deliver(event)
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001 -- handed to the caller
            outcome.append(exc)

    worker = threading.Thread(target=run, name="outbox-deliver", daemon=True)
    worker.start()
    worker.join(DELIVER_TIMEOUT_SECONDS)
    if not outcome:
        raise TimeoutError(f"sink {name}: deliver exceeded {DELIVER_TIMEOUT_SECONDS}s")
    if outcome[0] is not None:
        raise outcome[0]


def deliver_due(
    db: Database, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> tuple[int, int]:
    """One pass over the core outbox. Returns (delivered, failed)."""
    from ream.events import Event

    sinks = _deliverable()
    delivered = failed = 0
    for _ in range(BATCH):
        # A sink with an open breaker is left out: its rows wait, uncounted. The list
        # is a read-only peek; the trial is reserved only for the sink whose row is
        # claimed, so a sink with nothing due keeps its trial.
        now = clock()
        row = _claim(db, now, [n for n in sinks if breaker_peek(n, now)])
        if row is None:
            break
        if not breaker_reserve(row["sink"], now):
            # Another sender took the trial between peek and claim: give the row back.
            with db.connect() as conn:
                conn.execute(
                    "update outbox set claimed_until = null where id = %s"
                    " and delivered_at is null and claimed_until = %s",
                    (row["id"], row["lease"]),
                )
            continue
        error = None
        try:
            deliver = sinks[row["sink"]].deliver
            assert deliver is not None  # noqa: S101 -- _deliverable filters
            _call_bounded(deliver, Event.from_json(row["event_json"]), row["sink"])
        except Exception as exc:  # noqa: BLE001 -- a sink must not stop the loop
            # The type name; an exception class that sets `stored_message = True`
            # promises a fixed, value-free message (WebhookError), kept as well.
            error = type(exc).__name__
            if getattr(type(exc), "stored_message", False):
                error = f"{error}: {str(exc)[:200]}"
        finished = clock()
        breaker_record(row["sink"], error is None, finished, started=now, key=row["id"])
        # Only a row this sender still leases: if the lease ran out and another
        # sender took the row (or finished it), these updates touch nothing.
        mine = " where id = %s and delivered_at is null and claimed_until = %s"
        with db.connect() as conn:
            if error is None:
                done = conn.execute(
                    "update outbox set delivered_at = %s, claimed_until = null,"  # noqa: S608 -- fixed clause
                    " attempts = attempts + 1" + mine,
                    (finished, row["id"], row["lease"]),
                )
                if done.rowcount:
                    delivered += 1
                continue
            attempts = row["attempts"] + 1
            parked = attempts >= MAX_ATTEMPTS
            lost = (
                conn.execute(
                    "update outbox set attempts = %s, next_at = %s, claimed_until = null,"  # noqa: S608 -- fixed clause
                    " last_error = %s, last_error_at = %s, parked_at = %s" + mine,
                    (
                        attempts,
                        finished + _backoff(attempts),
                        error,
                        finished,
                        finished if parked else None,
                        row["id"],
                        row["lease"],
                    ),
                ).rowcount
                == 0
            )
            if lost:
                continue
            failed += 1
            _log.warning(
                "sink %s %s (attempt %d): %s",
                row["sink"],
                "PARKED" if parked else "not delivered",
                attempts,
                error,
            )
    return delivered, failed


def prune(db: Database, now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(UTC)) - KEEP_DELIVERED
    with db.connect() as conn:
        return conn.execute(
            "delete from outbox where delivered_at is not null and delivered_at < %s",
            (cutoff,),
        ).rowcount


def _plugin_failures() -> dict[str, int]:
    from ream import plugin_api

    return plugin_api.failure_counts()


def status(db: Database) -> dict:
    """Pending and parked rows by sink. Counts only, never an event."""
    with db.connect() as conn:
        pending = conn.execute(
            "select sink, count(*) from outbox where delivered_at is null"
            " and parked_at is null group by sink"
        ).fetchall()
        parked = conn.execute(
            "select sink, count(*) from outbox where parked_at is not null"
            " and delivered_at is null group by sink"
        ).fetchall()
    with db.connect() as conn:
        delivered = conn.execute(
            "select sink, count(*) from outbox where delivered_at is not null"
            " group by sink"
        ).fetchall()
    return {
        "pending": {r[0]: r[1] for r in pending},
        "parked": {r[0]: r[1] for r in parked},
        "delivered": {r[0]: r[1] for r in delivered},
        "late_failures": late_failures(),
        "plugin_failures": _plugin_failures(),
        "open_breakers": open_breakers(),
    }


_thread: threading.Thread | None = None
_stop: threading.Event | None = None


def start(db: Database) -> threading.Thread:
    """The core sender: one daemon thread, always on (a sink may be registered
    with the partner bridge off). It never dies of an exception. Idempotent: while
    a sender is running, a second call returns it and starts nothing. Each sender
    has its own stop flag, so a restart can never revive one that was told to stop."""
    global _thread, _stop
    with _lock:
        if (
            _thread is not None
            and _thread.is_alive()
            and _stop is not None
            and not _stop.is_set()
        ):
            return _thread
        stop_flag = threading.Event()

        def loop() -> None:
            last_prune = datetime.min.replace(tzinfo=UTC)
            while not stop_flag.is_set():
                try:
                    deliver_due(db)
                    now = datetime.now(UTC)
                    if now - last_prune > _PRUNE_EVERY:
                        prune(db, now)
                        last_prune = now
                except Exception:  # noqa: BLE001 -- the loop must outlive one bad pass
                    _log.error("outbox delivery pass failed", exc_info=True)
                stop_flag.wait(POLL_SECONDS)

        thread = threading.Thread(target=loop, name="outbox-sender", daemon=True)
        _stop, _thread = stop_flag, thread
        thread.start()
        return thread


def stop(timeout: float = 5.0) -> None:
    """Ask the sender to stop and wait briefly for it (a deliver in flight is
    bounded by DELIVER_TIMEOUT_SECONDS, so it may outlast `timeout`)."""
    global _thread, _stop
    from ream import plugin_api

    bind(None)
    plugin_api.stop_writer(timeout)
    with _lock:
        thread, flag = _thread, _stop
        if flag is not None:
            flag.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout)
    with _lock:
        if _thread is thread and (thread is None or not thread.is_alive()):
            _thread = None
            _stop = None


# --- the two sinks core already had -----------------------------------------------


def _builtin_sinks() -> list[Sink]:
    from ream import events, nudge, partner

    def deliver_nudge(event: Event) -> None:
        # Step 9d: the house plug-in's after-commit sink `doorbell` rings instead, so the
        # two never both ring. Checked at delivery (the plug-ins load after core's sinks);
        # `registered()` takes the RLock itself, so nothing here holds it.
        global _doorbell_stood_down_logged
        if any(s.name == DOORBELL_PLUGIN_SINK for s in registered()):
            if not _doorbell_stood_down_logged:
                _doorbell_stood_down_logged = True
                _log.info(
                    "core doorbell stands down: a plug-in sink named doorbell is registered"
                )
            return
        data = event.data
        nudge.emit(
            kind=event.kind,
            artifact_id=event.artifact_id,
            title=data.get("title", ""),
            agent=event.agent,
            summary=event.summary,
            event_id=event.id or "",
            # The journal row and the nudge usually carry the same note; a producer
            # whose journal row has words the nudge never did (comment_added) says
            # so with `nudge_note`.
            note=data.get("nudge_note", event.note),
            letter=data.get("letter"),
            recommended=data.get("recommended"),
        )

    def stage_partner(conn: Connection, event: Event) -> None:
        # The body is already the partner's own shape (partner.*_body). `at` is an
        # ISO string in `data` because an event is JSON.
        at = event.data.get("at")
        partner.enqueue(
            conn,
            event.ref_id or "",
            event.kind,
            event.data["body"],
            at=datetime.fromisoformat(at) if at else None,
        )

    return [
        Sink(
            "nudge",
            tuple(nudge._ALERTNAMES),
            deliver=deliver_nudge,
            delivery="after_commit",
        ),
        Sink("partner", events.BRIDGE_KINDS, stage=stage_partner, label="quarterdeck"),
    ]
