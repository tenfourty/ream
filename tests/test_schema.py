import threading

import psycopg
import pytest

from artifacts.db import SCHEMA_VERSION, Database


def test_migrate_creates_every_table(db):
    with db.connect() as conn:
        rows = conn.execute(
            "select table_name from information_schema.tables where table_schema = 'public'"
        ).fetchall()
    names = {row[0] for row in rows}
    assert {"agent_key", "artifact", "version", "file", "state"} <= names


def test_migrate_is_idempotent(db):
    with db.connect() as conn:
        conn.execute(
            "insert into artifact (id, title, agent, read_token) values (%s, %s, %s, %s)",
            ("a1", "t", "agent1", "tok1"),
        )
    db.migrate()
    with db.connect() as conn:
        count = conn.execute("select count(*) from artifact").fetchone()[0]
    # a second migrate() against a populated schema must be a true no-op: the row inserted
    # before it survives, and it doesn't raise (which a non-idempotent DDL change would).
    assert count == 1
    assert SCHEMA_VERSION == 1


def test_file_requires_an_existing_version(db):
    with db.connect() as conn:
        conn.execute(
            "insert into artifact (id, title, agent, read_token) values (%s, %s, %s, %s)",
            ("a1", "t", "agent1", "tok1"),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "insert into file (artifact_id, n, path, content_type, bytes) "
                "values (%s, %s, %s, %s, %s)",
                ("a1", 999, "index.html", "text/html", b"x"),
            )


def test_current_version_must_reference_an_existing_version(db):
    with db.connect() as conn:
        conn.execute(
            "insert into artifact (id, title, agent, read_token) values (%s, %s, %s, %s)",
            ("a1", "t", "agent1", "tok1"),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "update artifact set current_version = %s where id = %s", (42, "a1")
            )


def test_deleting_artifact_with_versions_and_files_succeeds(db):
    with db.connect() as conn:
        conn.execute(
            "insert into artifact (id, title, agent, read_token) values (%s, %s, %s, %s)",
            ("a1", "t", "agent1", "tok1"),
        )
        conn.execute(
            "insert into version (artifact_id, n, body, revision, created_by) "
            "values (%s, %s, %s, %s, %s)",
            ("a1", 1, "<html></html>", "rev1", "agent1"),
        )
        conn.execute(
            "insert into file (artifact_id, n, path, content_type, bytes) "
            "values (%s, %s, %s, %s, %s)",
            ("a1", 1, "index.html", "text/html", b"hi"),
        )
        # current_version now points at a real, existing version row.
        conn.execute(
            "update artifact set current_version = %s where id = %s", (1, "a1")
        )

        conn.execute("delete from artifact where id = %s", ("a1",))

        remaining_versions = conn.execute(
            "select count(*) from version where artifact_id = %s", ("a1",)
        ).fetchone()[0]
        remaining_files = conn.execute(
            "select count(*) from file where artifact_id = %s", ("a1",)
        ).fetchone()[0]
    assert remaining_versions == 0
    assert remaining_files == 0


@pytest.mark.usefixtures("fresh_schema")
def test_concurrent_migrate_calls_on_a_fresh_schema_both_succeed(dsn):
    """Two pods starting together both call migrate() against a schema that has
    never been migrated. Without a lock serialising them, `create table if not
    exists` is not atomic across two concurrent transactions either: both check
    `pg_type` for the table's row type, see it missing, and proceed to create
    it, and the second one's CREATE then fails with a duplicate-key error on
    `pg_type`'s own unique constraint once the first commits -- this is what
    the RED evidence for the advisory lock actually showed, not the `do $$
    ... end $$` block's ALTER TABLE ADD CONSTRAINT (a second, unrelated
    check-then-act the same lock also happens to cover). A barrier makes both
    threads issue their migrate() call at the same instant, giving the race
    the best chance to show up."""
    db = Database(dsn)
    with db.connect() as conn:
        conn.execute("drop schema public cascade; create schema public;")

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run() -> None:
        barrier.wait()
        try:
            Database(dsn).migrate()
        except BaseException as exc:  # noqa: BLE001 -- captured across threads, re-raised below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    with db.connect() as conn:
        rows = conn.execute(
            "select table_name from information_schema.tables where table_schema = 'public'"
        ).fetchall()
    names = {row[0] for row in rows}
    assert {"agent_key", "artifact", "version", "file", "state"} <= names
