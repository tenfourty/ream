"""The nudge transport: `emit` tells a LOCAL session -- the operator's inbox on
baba -- to go read the journal (`events.py`) sooner, rather than waiting for its
next poll. It is an optimisation layered on top of that journal, never the
mechanism itself: an agent with no inbound path to it (anywhere Jeremy gives it
access, per his own requirement) still gets every event through `GET
/api/v1/events?since=<seq>`, and this module's entire job is to make the LAN
case faster, never to be the only way an event reaches anyone.

The one rule that matters more than the shape: a nudge must never turn a
successful request into a failed one. Answering a decision is what Jeremy came
to do; telling some other system about it afterwards is a courtesy, not a
precondition. So `emit`:

- never raises -- the whole function body is one `except Exception`, matching
  the guarantee this module has always made;
- never blocks the caller -- the actual HTTP call happens on a background
  thread (a module-level `ThreadPoolExecutor`), so `emit` returns as soon as
  the work is handed off, not once it is done;
- never changes what the caller returns -- nothing here is awaited inline in
  the request path, and no exception this module can produce ever reaches it.

Configuration is read at call time, never at import, so a test (or a redeployed
pod) can change it without restarting the process:

- `ARTIFACTS_NUDGE_URL` -- unset or empty means `emit` stays a no-op. That is
  the correct state for a developer laptop and for the test suite, and for any
  cluster that simply hasn't been given an inbox to nudge yet.
- `ARTIFACTS_NUDGE_TOKEN` -- the bearer credential. Set while the URL is unset
  does nothing (there is nowhere to send it); unset while the URL IS set means
  nudging cannot work yet -- logged ONCE per process, at INFO (not WARNING: a
  cluster with `envFrom: [{secretRef: {name: artifacts-nudge, optional: true}}]`
  and no such Secret is a normal, expected state, not a fault), never once per
  event.
- `ARTIFACTS_NUDGE_TIMEOUT` -- seconds, default 2.0.

The token itself is never logged, never put in an exception message, and never
put on a URL or command line -- it travels exactly once, as the `Authorization`
header on the one POST this module ever makes.
"""

import json
import logging
import os
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from artifacts import text as text_module
from artifacts.events import MAX_NOTE_IN_EVENT

_log = logging.getLogger("artifacts.nudge")

# Two workers is plenty for a LAN POST that times out in a couple of seconds --
# this is a courtesy notification, not a queue anything depends on. The bounded
# semaphore in front of them (`_slots`) is the "bounded queue" the design calls
# for: raw ThreadPoolExecutor queues work unboundedly, and an operator inbox
# that is down for a while must make this module drop events, not pile up an
# ever-growing backlog of stale HTTP calls behind it.
_MAX_WORKERS = 2
_MAX_QUEUED = 64
_executor = ThreadPoolExecutor(
    max_workers=_MAX_WORKERS, thread_name_prefix="artifacts-nudge"
)
_slots = threading.BoundedSemaphore(_MAX_WORKERS + _MAX_QUEUED)

_dropped_lock = threading.Lock()
_dropped_count = 0

_token_warned_lock = threading.Lock()
_token_warned = False

# One `alertname` per kind, fixed here -- never derived from caller-supplied
# text, which is exactly what would let a page forge a different mailbox line
# than the one this module intends for its own kind.
_ALERTNAMES = {
    "decision_answered": "Jeremy answered a decision",
    "comment_added": "Jeremy left a comment",
    "thread_resolved": "Jeremy resolved a thread",
}

# Sanitising lives in `artifacts.text`, not here. It used to live here and
# only here, which meant the nudge path was clean and every other way viewer
# text leaves this service -- the event feed most of all -- was not
# (homelab-ij8.55). A second copy of this function is how the two drift.
_SUMMARY_LIMIT = text_module.SUMMARY_LIMIT
_sanitize = text_module.sanitize

# His note travels at the same size it travels on the journal row, imported
# rather than restated: two opinions about how much of what Jeremy wrote is
# worth carrying is how a consumer ends up with a differently-truncated copy of
# the same sentence.
_NOTE_LIMIT = MAX_NOTE_IN_EVENT


def _warn_missing_token_once() -> None:
    global _token_warned
    with _token_warned_lock:
        if _token_warned:
            return
        _token_warned = True
    _log.info(
        "nudge: ARTIFACTS_NUDGE_URL is set but ARTIFACTS_NUDGE_TOKEN is not -- "
        "nudging stays inert until both are set (logged once per process)"
    )


def _note_dropped(artifact_id: str, kind: str) -> None:
    with _dropped_lock:
        global _dropped_count
        _dropped_count += 1
    _log.warning(
        "nudge dropped: the send queue is full, artifact=%s kind=%s", artifact_id, kind
    )


def _send(url: str, token: str, timeout: float, body: dict, artifact_id: str) -> None:
    """Runs on a background thread. Never raises out of here -- a connection
    refused, a timeout, or a non-2xx response are all routine (the receiver is
    down, or slow, or unhappy), not a bug in this module, so each is caught and
    logged the same way: the exception's TYPE and the artifact id, never the
    body, never a header, and never the token."""
    try:
        request = urllib.request.Request(  # noqa: S310 -- a fixed LAN URL from our own env, never caller input
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout):  # noqa: S310
            pass
    except Exception as exc:  # noqa: BLE001 -- see module docstring, "never raises"
        _log.warning(
            "nudge: send failed, artifact=%s error=%s", artifact_id, type(exc).__name__
        )
    finally:
        _slots.release()


def emit(
    kind: str,
    *,
    artifact_id: str,
    title: str,
    agent: str,
    summary: str,
    event_id: str,
    note: str = "",
    letter: str | None = None,
    recommended: str | None = None,
) -> None:
    """Record that something worth telling the operator about happened.
    `event_id` must be the journal row's `seq` (as a string) -- the receiver on
    baba fingerprints an event on artifact id + kind + event id, so a retried
    send folds into one mailbox line while two different answers on the same
    page stay two lines. `title`/`summary` are caller-prepared, viewer-typed
    text and are sanitised again here (see `_sanitize`) regardless of what the
    caller already did to them -- this function's "never forge structure"
    guarantee is made once, here, not trusted to every future call site.

    `note` IS WHAT JEREMY WROTE, VERBATIM, AND IT TRAVELS IN ITS OWN FIELD.
    Never folded into `summary`, for the reason `events.EventRow` gives for the
    same field: a receiver showing the summary as one line has to be able to
    show this differently, and concatenating them takes that choice away from
    every consumer. It cost a real afternoon on 20 Sep 2026 (homelab-873): an
    answer of "(a)" reached the operator's mailbox as "a: <label>" while the
    244 words qualifying it stayed in the database, and four and a half hours
    of work ran on the letter alone.

    `has_note` is sent beside it so a receiver can raise the line's prominence
    without parsing prose, and `letter`/`recommended`/`differs_from_recommended`
    so it can say "he did NOT take the recommendation" -- the other half of the
    same failure, where a letter reads as agreement until someone checks.

    A receiver that does not know these fields ignores them; today the
    operator's inbox rebuilds its payload from a fixed list of keys, so these
    arrive and are dropped until that list grows. That is the right order: the
    data has to be at the boundary before the other side can choose to keep it.
    """
    try:
        url = os.environ.get("ARTIFACTS_NUDGE_URL", "")
        if not url:
            return
        # Jeremy, 19 Sep: "the agent that published the artifact needs to get the
        # nudge, not ops." This URL is ONE mailbox belonging to ONE seat, so it
        # is only ever the right destination for that seat's own pages. Firing
        # it for every page made the operator the recipient of everyone's
        # answers and the owning agent the recipient of none -- which is how an
        # agent sat unaware of an answer Jeremy had already given it.
        #
        # Every other agent is served by the held request on /events?wait=,
        # which needs no address at all and therefore works for an agent that
        # is not on this machine. This push stays only because it reaches a
        # seat that is NOT sitting on such a request: it is an optimisation for
        # one mailbox, never the mechanism.
        for_agent = os.environ.get("ARTIFACTS_NUDGE_AGENT", "")
        if not for_agent or agent != for_agent:
            return
        token = os.environ.get("ARTIFACTS_NUDGE_TOKEN", "")
        if not token:
            _warn_missing_token_once()
            return
        try:
            timeout = float(os.environ.get("ARTIFACTS_NUDGE_TIMEOUT", "2.0"))
        except ValueError:
            timeout = 2.0

        body = {
            "alertname": _ALERTNAMES.get(kind, "Artifacts event"),
            "instance": artifact_id,
            "state": "message",
            "severity": "info",
            "kind": kind,
            "event_id": event_id,
            "artifact_id": artifact_id,
            "title": _sanitize(title),
            "agent": agent,
            "summary": _sanitize(summary),
            "note": _sanitize(note, limit=_NOTE_LIMIT),
            # A boolean, not "is the note field truthy", because that is the
            # question a receiver is actually asking and the answer must not
            # change with how the note sanitises.
            "has_note": bool(_sanitize(note, limit=_NOTE_LIMIT)),
            "answer": letter,
            "recommended": recommended,
            # Only ever true when there WAS a recommendation to depart from. A
            # decision posed without one has nothing to disagree with, and a
            # receiver told otherwise would flag every answer on such a page.
            "differs_from_recommended": bool(
                recommended and letter and letter != recommended
            ),
        }

        if not _slots.acquire(blocking=False):
            _note_dropped(artifact_id, kind)
            return
        try:
            _executor.submit(_send, url, token, timeout, body, artifact_id)
        except Exception:
            _slots.release()
            raise
    except Exception as exc:  # noqa: BLE001 -- deliberately swallowed, not re-raised: see
        # module docstring, "never raises" is the one guarantee this function makes.
        # Only the exception's TYPE is logged (never its message or a value it might
        # carry), matching store.py's own rule for a scanner failure.
        _log.debug("nudge: emit() itself failed: %s", type(exc).__name__)
