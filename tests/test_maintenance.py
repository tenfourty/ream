"""The nightly backup and purge, by wall clock (the SQLite switch, slice 6)."""

import os
from datetime import UTC, datetime, timedelta

from artifacts import maintenance, store
from artifacts.db import Database

PAGE = "<!doctype html><html><head><title>t</title></head><body>x</body></html>"


def _sqlite(tmp_path) -> Database:
    db = Database(f"sqlite:///{tmp_path}/artifacts.db")
    db.migrate()
    return db


def test_the_next_run_is_the_next_four_oclock_utc():
    at = datetime(2026, 10, 13, 3, 59, tzinfo=UTC)
    assert maintenance.next_run(at) == datetime(2026, 10, 13, 4, 0, tzinfo=UTC)
    at = datetime(2026, 10, 13, 4, 0, tzinfo=UTC)
    assert maintenance.next_run(at) == datetime(2026, 10, 14, 4, 0, tzinfo=UTC)


def test_a_snapshot_is_a_database_holding_the_rows(tmp_path):
    db = _sqlite(tmp_path)
    page = store.publish(db, "seat", "kept", PAGE)
    made = maintenance.backup(db, tmp_path / "backups")
    assert made.name == f"artifacts-sqlite-{datetime.now(UTC):%Y-%m-%d}.db"
    copy = Database(f"sqlite:///{made}")
    with copy.connect() as conn:
        assert conn.execute("select id from artifact").fetchall() == [(page.id,)]
    assert not list((tmp_path / "backups").glob(".tmp-*"))


def test_seven_snapshots_are_kept_and_nothing_else_is_touched(tmp_path):
    db = _sqlite(tmp_path)
    folder = tmp_path / "backups"
    folder.mkdir()
    (folder / "artifacts-db-2026-01-01.sql.gz").write_text("a pg_dump")
    start = datetime(2026, 10, 1, 4, tzinfo=UTC)
    for day in range(9):
        maintenance.backup(db, folder, now=start + timedelta(days=day))
    names = sorted(p.name for p in folder.iterdir())
    assert names == ["artifacts-db-2026-01-01.sql.gz"] + [
        f"artifacts-sqlite-2026-10-{d:02d}.db" for d in range(3, 10)
    ]


def test_start_up_runs_the_pass_when_the_newest_snapshot_is_stale(tmp_path):
    folder = tmp_path / "backups"
    now = datetime.now(UTC)
    assert maintenance.due_at_start(folder, now)  # none yet
    db = _sqlite(tmp_path)
    made = maintenance.backup(db, folder)
    assert not maintenance.due_at_start(folder, now)
    old = (now - timedelta(hours=25)).timestamp()
    os.utime(made, (old, old))
    assert maintenance.due_at_start(folder, now)


def test_postgres_takes_no_snapshot_here():
    pg = Database("postgresql://nowhere/none")
    assert maintenance.backup_folder(pg) is None
    assert not maintenance.due_at_start(None, datetime.now(UTC))


def test_the_backup_runs_before_the_purge(tmp_path, monkeypatch):
    db = _sqlite(tmp_path)
    order = []
    monkeypatch.setattr(
        maintenance, "backup", lambda *a, **k: order.append("backup") or tmp_path
    )
    monkeypatch.setattr(store, "purge_expired", lambda _db: order.append("purge") or [])
    maintenance.nightly(db)
    assert order == ["backup", "purge"]


def test_a_failed_backup_purges_nothing(tmp_path, monkeypatch):
    db = _sqlite(tmp_path)

    def broken(*_a, **_k):
        raise OSError("disk full")

    purged = []
    monkeypatch.setattr(maintenance, "backup", broken)
    monkeypatch.setattr(store, "purge_expired", lambda _db: purged.append(1) or [])
    try:
        maintenance.nightly(db)
    except OSError:
        pass
    assert purged == []
