import psycopg
import pytest

from artifacts.db import SCHEMA_VERSION


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
        conn.execute("update artifact set current_version = %s where id = %s", (1, "a1"))

        conn.execute("delete from artifact where id = %s", ("a1",))

        remaining_versions = conn.execute(
            "select count(*) from version where artifact_id = %s", ("a1",)
        ).fetchone()[0]
        remaining_files = conn.execute(
            "select count(*) from file where artifact_id = %s", ("a1",)
        ).fetchone()[0]
    assert remaining_versions == 0
    assert remaining_files == 0
