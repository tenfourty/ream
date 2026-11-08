"""The nightly backup and purge, run inside the service by wall clock.

Every push to main restarts the pod, so a timer counted from start-up ("every
24 h") might never fire. Instead one daemon thread wakes at 04:00 UTC each day:
first a snapshot of the SQLite database, then the purge of pages whose
thirty-day recovery window has run out -- in that order, so anything purged is
in a backup first. At start-up, if the newest snapshot is more than a day old
(or there is none), the same pass runs at once: a pod that restarted across
04:00 does not skip a night. On Postgres there is no snapshot here (its dumps
are taken elsewhere) and the thread only purges.

A snapshot is `VACUUM INTO` a temporary name, then a rename to
`artifacts-sqlite-<UTC date>.db`, uncompressed (restic, which copies them
off the volume, compresses and deduplicates; gzip would defeat that). Seven
are kept, matched only by that prefix. A homelab CronJob copies new ones to
the NAS (docs/agent/specs/2026-10-09-sqlite-design.md).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from ream import settings

if TYPE_CHECKING:
    from ream.db import Database

_log = logging.getLogger("ream.maintenance")

RUN_HOUR_UTC = 4
KEEP = 2  # on the data volume; off-site holds the history (Jeremy, 26 Oct 2026)
PREFIX = "artifacts-sqlite-"
STALE_AFTER = timedelta(hours=24)


def backup_folder(db: Database) -> Path | None:
    """Where snapshots go: REAM_BACKUP_DIR, else `backups/` beside the
    database file. None on Postgres."""
    if not db.sqlite:
        return None
    configured = settings.current().backup_dir
    if configured:
        return Path(configured)
    return Path(db.dsn.removeprefix("sqlite:///")).parent / "backups"


def snapshots(folder: Path) -> list[Path]:
    """This service's snapshots, oldest first (the date in the name sorts)."""
    return sorted(folder.glob(f"{PREFIX}*.db"))


def backup(db: Database, folder: Path, now: datetime | None = None) -> Path:
    """Snapshot the database to `folder`, then keep only the newest KEEP. A
    second snapshot on the same UTC date replaces the first."""
    now = now or datetime.now(UTC)
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"{PREFIX}{now:%Y-%m-%d}.db"
    temporary = folder / f".tmp-{now:%Y%m%dT%H%M%S}-{os.getpid()}.db"
    try:
        with db.connect() as conn:
            conn.execute("vacuum into %s", (str(temporary),))
        os.replace(temporary, final)
    finally:
        temporary.unlink(missing_ok=True)
    for old in snapshots(folder)[:-KEEP]:
        old.unlink()
    return final


def due_at_start(folder: Path | None, now: datetime) -> bool:
    """True when start-up should run the nightly pass at once: on SQLite, no
    snapshot yet or the newest is over a day old."""
    if folder is None:
        return False
    existing = snapshots(folder) if folder.exists() else []
    if not existing:
        return True
    newest = datetime.fromtimestamp(existing[-1].stat().st_mtime, UTC)
    return now - newest > STALE_AFTER


def next_run(now: datetime) -> datetime:
    """The next 04:00 UTC strictly after `now`."""
    today = now.astimezone(UTC).replace(
        hour=RUN_HOUR_UTC, minute=0, second=0, microsecond=0
    )
    return today if today > now else today + timedelta(days=1)


def nightly(db: Database) -> None:
    """The snapshot (SQLite only), then the purge. A failed snapshot stops the
    purge: nothing is destroyed that is not in a backup."""
    from ream import image_store, store

    folder = backup_folder(db)
    if folder is not None:
        made = backup(db, folder)
        _log.info("backup written: %s", made.name)
    gone = store.purge_expired(db)
    image_store.sweep(db)
    # After the purge, so the statistics describe what is left.
    db.optimize()
    _log.info("purge: %d destroyed%s", len(gone), "".join(f" {g}" for g in gone))


def start(db: Database) -> threading.Thread:
    """One daemon thread. It never blocks start-up and never dies of an
    exception: a failed night is logged at ERROR and the next one still runs."""

    def run_once() -> None:
        try:
            nightly(db)
        except Exception:  # noqa: BLE001 -- the loop must outlive one bad night
            _log.error("nightly backup and purge failed", exc_info=True)

    def loop() -> None:
        if due_at_start(backup_folder(db), datetime.now(UTC)):
            run_once()
        while True:
            now = datetime.now(UTC)
            time.sleep(max(1.0, (next_run(now) - now).total_seconds()))
            run_once()

    thread = threading.Thread(target=loop, name="nightly-maintenance", daemon=True)
    thread.start()
    return thread


IMAGE_SWEEP_EVERY_SECONDS = 3600


def start_image_sweep(db: Database) -> threading.Thread:
    """Once an hour, drop pictures staged for a comment and never attached (an hour after
    upload they no longer count against anyone's quota; this frees their bytes). Never dies
    of an exception."""
    from ream import image_store

    def loop() -> None:
        while True:
            time.sleep(IMAGE_SWEEP_EVERY_SECONDS)
            try:
                image_store.sweep(db)
            except Exception:  # noqa: BLE001 -- the loop must outlive one bad hour
                _log.error("staged picture sweep failed", exc_info=True)

    thread = threading.Thread(target=loop, name="staged-image-sweep", daemon=True)
    thread.start()
    return thread
