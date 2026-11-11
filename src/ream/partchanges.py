"""Which parts a round changed: computed and kept after a publish (runcove-5t3oj.56, slice 2).

`partdiff` does the comparing, in the diff worker. This module reads the two versions, asks it,
and keeps the answer on the new version, in the background so that a publish never waits for it.
The `/changes` route computes the same answer on first ask when it was not kept.
"""

from __future__ import annotations

import logging
import threading

from ream import partdiff, store

log = logging.getLogger(__name__)

_SLOTS = threading.BoundedSemaphore(4)

# Answers worth keeping: the pages were read, or are past a limit that will not change (a diff that
# ran out of time will again, and each try would take a diff slot). A failure
# from load or a crashed worker says nothing about the pages, so it is asked again next time.
_KEEP_REASONS = (
    "too large to mark up changes",
    "this page draws itself with script",
    partdiff.REASON_SLOW,
)


def keepable(found: dict) -> bool:
    return (
        found.get("unknown_reason") is None or found["unknown_reason"] in _KEEP_REASONS
    )


def keepable_from_a_read(found: dict) -> bool:
    """`keepable` for the answer to a reader's request: all of it except a diff that only timed
    out. The worker after a publish keeps that one (`record`); a read asks again, so that asking
    is never what fills the stored column."""
    return keepable(found) and found.get("unknown_reason") != partdiff.REASON_SLOW


def round_of(found: dict, frm: int) -> dict:
    return {"from": frm, **found}


def record(db, artifact_id: str, n: int) -> None:
    """Compute and keep version `n`'s round against `n - 1`. Nothing for a first version."""
    pair = store.bodies_for_changes(db, artifact_id, n)
    if pair is None:
        return
    found = partdiff.changes_safely(*pair)
    if keepable(found):
        store.save_part_changes(db, artifact_id, n, round_of(found, n - 1))


def in_background(db, artifact_id: str, n: int) -> None:
    """`record` on a daemon thread, after the publish has returned. Never raises; with no free
    slot the round is simply computed on the first ask instead."""
    if n < 2 or not _SLOTS.acquire(blocking=False):
        return

    def work() -> None:
        try:
            record(db, artifact_id, n)
        except Exception:  # a convenience may never reach the caller
            log.exception("part changes failed for %s v%s", artifact_id, n)
        finally:
            _SLOTS.release()

    try:
        threading.Thread(target=work, name=f"parts-{artifact_id}", daemon=True).start()
    except RuntimeError:
        _SLOTS.release()
