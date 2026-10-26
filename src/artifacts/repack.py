"""The backfill: pack every version written before deltas existed, then blank the old bodies.

`artifacts-admin pack-versions`, and a background thread at start-up
(docs/agent/specs/2026-10-26-delta-versions-design.md, section 5). Page by page, in `n` order:

1. pack every version that has no `packed` yet, committing every `BATCH` versions, so a
   crash leaves only rows whose `packed` is complete or absent;
2. rebuild every version of the page from what is now stored and compare it with the text
   still held in `body`;
3. only then, in one transaction holding the page's row lock, blank `body` on the versions
   that are not current.

Idempotent: a finished page has no unpacked version and no stored old body, and is skipped.
A page that cannot be verified is logged, counted as failed, and left exactly as it was
(nothing blanked); the other pages go on.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from artifacts import packing, store

if TYPE_CHECKING:
    from artifacts.db import Database

_log = logging.getLogger("artifacts.repack")

BATCH = 10


@dataclass
class Report:
    pages: int = 0
    packed: int = 0
    blanked: int = 0
    failed: int = 0
    saved: int = 0  # bytes of plain text no longer stored, less the bytes packed


def _pages_with_work(db: Database) -> list[str]:
    with db.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                "select v.artifact_id from version v"
                " join artifact a on a.id = v.artifact_id"
                " where v.packed is null"
                "    or (v.body <> '' and v.n <> a.current_version)"
                " group by v.artifact_id order by v.artifact_id"
            ).fetchall()
        ]


def _pack_page(db: Database, artifact_id: str) -> int:
    with db.connect() as conn:
        todo = [
            r[0]
            for r in conn.execute(
                "select n from version where artifact_id = %s and packed is null"
                " order by n",
                (artifact_id,),
            ).fetchall()
        ]
        if not todo:
            return 0
        prev_n, prev = None, None
        done = 0
        pending: list[tuple] = []

        def flush() -> None:
            if not pending:
                return
            with conn.transaction():
                for row in pending:
                    conn.execute(
                        "update version set packed = %s, codec = %s, base_n = %s,"
                        " body_bytes = %s, body_sha256 = %s"
                        " where artifact_id = %s and n = %s and packed is null",
                        row,
                    )
            pending.clear()

        for n in todo:
            body = conn.execute(
                "select body from version where artifact_id = %s and n = %s",
                (artifact_id, n),
            ).fetchone()[0]
            if body == "":
                raise packing.PackError(
                    f"version {n} has neither a body nor a packed copy"
                )
            base = None
            if not packing.is_keyframe(n):
                if prev_n == n - 1:
                    base = prev
                else:
                    base = store.version_bodies(conn, artifact_id, [n - 1])[n - 1]
            packed = packing.pack(n, body, base)
            pending.append(
                (
                    packed.data,
                    packed.codec,
                    packed.base_n,
                    len(body.encode()),
                    packing.sha256(body),
                    artifact_id,
                    n,
                )
            )
            prev_n, prev = n, body
            done += 1
            if len(pending) >= BATCH:
                flush()
        flush()
        return done


def _verify_and_blank(db: Database, artifact_id: str) -> tuple[int, int]:
    """(versions blanked, bytes saved). Raises PackError, changing nothing, if any version
    of the page does not rebuild to the text held in `body`.

    The decoding (tens of seconds on a big page) happens OUTSIDE any transaction, so the
    write lock is never held while it runs. The lock is taken only to blank, and each row is
    blanked only if it is still the row that was verified (same hash and codec) and is still
    not the current version."""
    verified: list[
        tuple[int, str, str, int, int]
    ] = []  # n, sha, codec, packed, plain bytes
    with db.connect() as conn:
        current = conn.execute(
            "select current_version from artifact where id = %s", (artifact_id,)
        ).fetchone()
        current = current[0] if current else None
        rows = conn.execute(
            "select n from version where artifact_id = %s order by n", (artifact_id,)
        ).fetchall()
        prev = None
        for (n,) in rows:
            body, codec, data, sha = conn.execute(
                "select body, codec, packed, body_sha256 from version"
                " where artifact_id = %s and n = %s",
                (artifact_id, n),
            ).fetchone()
            if data is None:
                raise packing.PackError(f"version {n} is still unpacked")
            text = packing.unpack(codec, data, prev, sha)
            if body != "":
                if body != text:
                    raise packing.PackError(
                        f"version {n} does not rebuild to its stored text"
                    )
                if n != current:
                    verified.append((n, sha, codec, len(data), len(body.encode())))
            prev = text
    blanked = saved = 0
    if not verified:
        return 0, 0
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "select 1 from artifact where id = %s" + conn.for_update(),  # noqa: S608 -- clause from the db layer, no caller input
            (artifact_id,),
        )
        now = conn.execute(
            "select current_version from artifact where id = %s", (artifact_id,)
        ).fetchone()
        now = now[0] if now else None
        for n, sha, codec, packed_len, plain_len in verified:
            if n == now:
                continue
            done = conn.execute(
                "update version set body = '' where artifact_id = %s and n = %s"
                " and body <> '' and body_sha256 = %s and codec = %s"
                " and packed is not null",
                (artifact_id, n, sha, codec),
            ).rowcount
            if done:
                blanked += 1
                saved += plain_len - packed_len
    return blanked, saved


def pack_versions(db: Database) -> Report:
    report = Report()
    for artifact_id in _pages_with_work(db):
        try:
            packed = _pack_page(db, artifact_id)
            blanked, saved = _verify_and_blank(db, artifact_id)
        except packing.PackError as exc:
            report.failed += 1
            _log.error("pack-versions: page %s left as it was: %s", artifact_id, exc)
            continue
        report.pages += 1
        report.packed += packed
        report.blanked += blanked
        report.saved += saved
    store.forget_rebuilt()
    _log.info(
        "pack-versions: %d pages, %d packed, %d blanked, %d failed, %d bytes saved",
        report.pages,
        report.packed,
        report.blanked,
        report.failed,
        report.saved,
    )
    return report


def start(db: Database) -> threading.Thread:
    """The backfill in a daemon thread at start-up. It never blocks start-up and never
    dies of an exception; a page it cannot finish is retried at the next start."""

    def run() -> None:
        try:
            pack_versions(db)
        except Exception:  # noqa: BLE001 -- the service must outlive a failed backfill
            _log.error("pack-versions failed", exc_info=True)

    thread = threading.Thread(target=run, name="pack-versions", daemon=True)
    thread.start()
    return thread
