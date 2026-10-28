"""The Postgres <-> SQLite copier and its verifier (the SQLite switch, slice 7).

The round trip and the both-directions mismatch tests need Postgres AND SQLite,
so they run in the Postgres pass (`postgres_only`), each with a temporary SQLite
file of its own. The copier itself is backend-neutral, so the SQLite pass still
proves it SQLite -> SQLite.
"""

import os
import re
from datetime import UTC, datetime, timedelta, timezone
from importlib import resources
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg import sql

from artifacts import admin, migrate
from artifacts.db import Database, jsonb

T0 = datetime(2026, 10, 15, 9, 30, 15, 123456, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)


def _schema_tables(filename: str) -> set[str]:
    text = resources.files("artifacts").joinpath(filename).read_text()
    if filename == "schema_sqlite.sql":
        # SQLite's later tables arrive as numbered steps, never as edits to
        # this file (db._SQLITE_STEPS).
        from artifacts import db as dbmod

        text += "\n".join(
            s
            for step in dbmod._SQLITE_STEPS.values()
            for s in step
            if isinstance(s, str)
        )
    return set(
        re.findall(r"^create table (?:if not exists )?(\w+)", text, re.MULTILINE)
    )


def _seed(database: Database) -> None:
    """At least one row in every table, with the awkward values: a NULL, a
    zero-microsecond time and a non-UTC one, booleans both ways, nested and
    scalar-holding JSON, bytes with a zero byte, non-ASCII text, and a
    `current_version` and a `thread_root` that the copier must defer."""
    j = jsonb
    rows = [
        (
            "agent_key",
            (
                "id, agent, token_sha256, created_at, revoked_at, label, last_used_at,"
                " rotating, replaces, first_used_at, revoked_reason"
            ),
            ["k1", "alpha", "a" * 64, T0, None, "laptop é", T1, True, None, T0, None],
        ),
        (
            "agent_key",
            "id, agent, token_sha256, created_at, revoked_at, rotating, revoked_reason",
            ["k2", "alpha", "b" * 64, T0.replace(microsecond=0), T1, False, "lost"],
        ),
        (
            "artifact",
            (
                "id, title, agent, visibility, read_token, current_version, created_at,"
                " updated_at, archived_at, pinned_at, deleted_at, session, agents_can_read"
            ),
            [
                "a1",
                "Page ☃",
                "alpha",
                "link",
                "rt1",
                None,
                T0,
                T1,
                None,
                T1,
                None,
                "alpha/s1",
                False,
            ],
        ),
        (
            "version",
            (
                "artifact_id, n, body, revision, status, created_by, created_at, thumbnail,"
                " session, render_errors, rendered_at, note, section, notify"
            ),
            [
                "a1",
                1,
                "<h1>one</h1>",
                "r1",
                "live",
                "alpha",
                T0,
                b"\x89PNG\x00\x01",
                "alpha/s1",
                j([{"line": 1, "msg": "x"}]),
                T0,
                "first",
                None,
                True,
            ],
        ),
        (
            "version",
            (
                "artifact_id, n, body, revision, created_by, created_at, render_errors, notify,"
                " packed, codec, base_n, body_bytes, body_sha256"
            ),
            # A packed, non-current version: its plain body is '' and its bytes are a zstd
            # frame with a zero byte in it.
            [
                "a1",
                2,
                "",
                "r2",
                "alpha",
                T1,
                None,
                False,
                b"\x28\xb5\x2f\xfd\x00\xff",
                "zstd1-delta",
                1,
                12,
                "ab" * 32,
            ],
        ),
        (
            "file",
            "artifact_id, n, path, content_type, bytes",
            ["a1", 1, "img/x.png", "image/png", bytes(range(256))],
        ),
        (
            "state",
            "artifact_id, path, doc, updated_at",
            ["a1", "/", j({"b": [1, 2.5, None], "a": {"z": True, "y": "ü"}}), T0],
        ),
        ("artifact_share", "artifact_id, token, created_at", ["a1", "share1", T0]),
        (
            "comment",
            (
                "id, artifact_id, version_n, thread_root, anchor, quote, author_kind, author,"
                " body, created_at, kind, held, review_round"
            ),
            [
                "c1",
                "a1",
                1,
                None,
                j({"kind": "page"}),
                "",
                "human",
                "jeremy",
                "root",
                T0,
                "comment",
                False,
                None,
            ],
        ),
        (
            "comment",
            (
                "id, artifact_id, version_n, thread_root, anchor, quote, author_kind, author,"
                " body, created_at, resolved_at, resolved_by, kind, held, withdrawn_at,"
                " withdrawn_kind, gg_scanned_at, gg_attempted_at, review_round"
            ),
            [
                "c2",
                "a1",
                2,
                "c1",
                j({"kind": "text", "start": 3}),
                "q",
                "agent",
                "alpha",
                "reply",
                T1,
                T1,
                "jeremy",
                "looks_good",
                True,
                None,
                None,
                T1,
                T1,
                1,
            ],
        ),
        (
            "decision",
            (
                "id, artifact_id, version_n, question, options, recommended, answer,"
                " answer_note, answered_at, answered_by, created_at, answer_source,"
                " recorded_by, said_at, session, kind, review_mode, context, topic_id,"
                " topic_title, depends_on, replaces, replaced_by, urgency, blocked_on,"
                " needed_by, for_offices"
            ),
            [
                "d1",
                "a1",
                1,
                "Which?",
                j(["a", "b"]),
                "a",
                "a",
                "",
                T1,
                "jeremy",
                T0,
                "page",
                None,
                None,
                "alpha/s1",
                "question",
                None,
                "why it matters, in Zürich",
                "infra",
                "Infrastructure",
                j(["d0"]),
                "d00",
                "d9",
                "blocking",
                "the deploy",
                T1,
                j(["beta", "gamma"]),
            ],
        ),
        (
            "decision",
            "id, artifact_id, version_n, question, options, created_at, kind, review_mode",
            ["d2", "a1", 2, "Review", j([]), T0, "review", "held"],
        ),
        (
            "decision_outcome",
            "id, decision_id, body, agent, session, created_at",
            ["o1", "d1", "done", "alpha", None, T1],
        ),
        (
            "review_round",
            (
                "review_id, n, version_n, base_version, opened_at, closed_at, verdict,"
                " reviewed_version, claimed_by, claim_expires_at"
            ),
            ["d2", 1, 2, 1, T0, None, None, None, "alpha/s1", T1],
        ),
        (
            "event",
            (
                "seq, artifact_id, agent, actor, kind, summary, ref_id, created_at, note,"
                " session"
            ),
            [1, "a1", "alpha", "jeremy", "decision_answered", "a", "d1", T1, "", None],
        ),
        (
            "event",
            "seq, artifact_id, agent, actor, kind, created_at, session",
            [2, "a1", "alpha", "jeremy", "comment_added", T1, "alpha/s1"],
        ),
        ("seat_read", "agent, last_read_at, session", ["alpha", T0, ""]),
        (
            "seat_read",
            "agent, last_read_at, session, held_until",
            ["alpha", T1, "alpha/s1", T1],
        ),
        ("seat_grant", "agent, scope, granted_at", ["alpha", "reviewer", T0]),
        (
            "frame_pass",
            (
                "token_sha256, artifact_id, version, expires_at, used_at, created_at,"
                " changes_from"
            ),
            ["c" * 64, "a1", 1, T1, None, T0, None],
        ),
        (
            "connect_request",
            (
                "id, code, poll_sha256, suggested_name, from_address, created_at,"
                " expires_at, approved_at, agent, denied_at, delivered_at, key_id"
            ),
            [
                "r1",
                "ABCD",
                "d" * 64,
                "beta",
                "10.0.0.1",
                T0,
                T1,
                T0,
                "beta",
                None,
                None,
                "k1",
            ],
        ),
        ("page_read", "artifact_id, read_version, read_at", ["a1", 2, T1]),
        (
            "email_share",
            (
                "id, artifact_id, email, token_sha256, created_at, revoked_at,"
                " last_opened_at"
            ),
            ["e1", "a1", "x@example.org", "e" * 64, T0, None, T1],
        ),
        (
            "email_share_code",
            "id, share_id, code_sha256, created_at, expires_at, attempts, used_at",
            ["ec1", "e1", "f" * 64, T0, T1, 2, None],
        ),
        (
            "email_share_device",
            "cookie_sha256, share_id, created_at, expires_at",
            ["0" * 64, "e1", T0, T1],
        ),
        ("human_pref", "key, value, updated_at", ["theme", "dark", T0]),
        (
            "idempotency",
            "agent, route, key, body_sha256, status, response, expires_at",
            ["alpha", "publish", "i1", "1" * 64, 201, j({"id": "a1", "n": 1}), T1],
        ),
        ("agent_alias", "old, new, renamed_at", ["oldname", "alpha", T0]),
        (
            "partner_outbox",
            (
                "event_id, decision_id, version, kind, body, created_at, attempts,"
                " next_attempt_at, claimed_until, delivered_at, last_error,"
                " last_error_at"
            ),
            [
                "pe1",
                "d1",
                1,
                "asked",
                j({"title": "Zürich?"}),
                T0,
                2,
                T1,
                None,
                None,
                "HTTP 502",
                T0,
            ],
        ),
        (
            "partner_question",
            (
                "source, version, asking_office, title, options, context, state,"
                " created_at, updated_at"
            ),
            [
                "qd:q1",
                3,
                "warpgate",
                "Zürich?",
                j([{"letter": "a", "label": "yes"}]),
                "ctx",
                "open",
                T0,
                T1,
            ],
        ),
        ("partner_version_seen", "source, version, updated_at", ["qd:q1", 3, T1]),
        (
            "partner_event_seen",
            "event_id, source, version, kind, outcome, detail, seen_at",
            ["ev1", "qd:q1", 3, "asked", "applied", j({"slack_id": "U1"}), T1],
        ),
    ]
    with database.connect() as conn, conn.transaction():
        for table, cols, values in rows:
            marks = ", ".join(["%s"] * len(values))
            conn.execute(f"insert into {table} ({cols}) values ({marks})", values)  # noqa: S608
        conn.execute("update artifact set current_version = 2 where id = 'a1'")
        # An event number handed out and then deleted: the copy must carry the
        # counter past it, not just past the highest surviving row.
        conn.execute(
            "insert into event (seq, artifact_id, agent, actor, kind, created_at)"
            " values (7, 'a1', 'alpha', 'jeremy', 'thread_resolved', %s)",
            (T1,),
        )
        conn.execute("delete from event where seq = 7")
        if not database.sqlite:
            conn.execute("select setval(pg_get_serial_sequence('event', 'seq'), 7)")


def _counts(database: Database) -> dict[str, int]:
    with database.connect() as conn:
        schema = migrate.read_schema(database, conn)
        return {
            t: conn.execute(f'select count(*) from "{t}"').fetchone()[0]  # noqa: S608
            for t in schema.tables
        }


def _new_event_seq(database: Database) -> int:
    with database.connect() as conn:
        return conn.execute(
            "insert into event (artifact_id, agent, actor, kind, created_at)"
            " values ('a1', 'alpha', 'jeremy', 'comment_added', %s) returning seq",
            (T1,),
        ).fetchone()[0]


@pytest.fixture()
def sqlite_db(tmp_path):
    return Database(f"sqlite:///{tmp_path}/migrated.db")


@pytest.fixture()
def pg_scratch(dsn):
    """A second, empty Postgres database on the same server."""
    from conftest import _with_dbname

    name = "artifacts_migrate_" + os.environ.get("PYTEST_XDIST_WORKER", "master")
    drop = sql.SQL("drop database if exists {} with (force)").format(
        sql.Identifier(name)
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(drop)
        conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    try:
        yield Database(_with_dbname(dsn, name))
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(drop)


# --- the table list cannot drift from the schemas ---------------------------


def test_every_table_in_both_schema_files_is_copied_in_foreign_key_order(db):
    db.migrate()
    with db.connect() as conn:
        schema = migrate.read_schema(db, conn)
    order = migrate.copy_order(schema)
    assert (
        set(order)
        == _schema_tables("schema.sql")
        == _schema_tables("schema_sqlite.sql")
    )
    assert len(order) == len(set(order))
    for child, parent, cols in schema.foreign_keys:
        if not any((child, c) in migrate.DEFERRED for c in cols):
            assert order.index(parent) < order.index(child), (child, parent)


def test_the_seed_fills_every_table(db):
    _seed(db)
    empty = [t for t, n in _counts(db).items() if n == 0]
    assert empty == [], f"add a row for {empty} to _seed"


# --- the canonical form -----------------------------------------------------


def test_canonical_json_is_the_specs_form():
    row = {
        "t": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "local": datetime(2026, 1, 2, 5, 4, 5, 7, tzinfo=timezone(timedelta(hours=1))),
        "b": True,
        "f": False,
        "n": None,
        "j": {"z": 1, "a": [True, None]},
        "x": b"\x00\xff",
        "s": "é",
    }
    assert migrate.canonical_row("t", row) == (
        '{"b":true,"f":false,"j":{"a":[true,null],"z":1},'
        '"local":"2026-01-02 04:04:05.000007+00:00","n":null,"s":"\\u00e9",'
        '"t":"2026-01-02 03:04:05.000000+00:00","x":"00ff"}'
    )


def test_seat_read_null_session_hashes_as_empty():
    a = migrate.canonical_row("seat_read", {"agent": "x", "session": None})
    b = migrate.canonical_row("seat_read", {"agent": "x", "session": ""})
    assert a == b
    assert migrate.canonical_row("other", {"session": None}) != migrate.canonical_row(
        "other", {"session": ""}
    )


def test_redact_hides_a_password():
    assert migrate.redact("postgresql://app:s3cret@h:5432/db?sslmode=require") == (
        "postgresql://app:***@h:5432/db?sslmode=require"
    )
    assert "s3cret" not in migrate.redact("host=h password=s3cret dbname=d")
    assert (
        migrate.redact("sqlite:////data/artifacts.db") == "sqlite:////data/artifacts.db"
    )


# --- backend-neutral: runs in both passes -----------------------------------


def test_copy_and_verify_into_a_fresh_sqlite_file(db, sqlite_db):
    _seed(db)
    checks = migrate.copy(db, sqlite_db, log=lambda _: None)
    assert {c.name: c.rows for c in checks} == _counts(db)
    assert migrate.verify(db, sqlite_db) == checks
    # The counter carried over: 7 was handed out (and deleted), so 8 is next.
    assert _new_event_seq(sqlite_db) == 8


def test_a_non_empty_destination_is_refused(db, sqlite_db):
    _seed(db)
    migrate.copy(db, sqlite_db, log=lambda _: None)
    with pytest.raises(migrate.Mismatch, match="not empty"):
        migrate.copy(db, sqlite_db, log=lambda _: None)


def test_a_mismatch_inside_the_copy_refuses_and_keeps_nothing(
    db, sqlite_db, monkeypatch
):
    _seed(db)
    real = migrate._value

    def corrupt(table, column, value):
        if (table.name, column) == ("decision", "question") and value == "Which?":
            value = "Which!"
        return real(table, column, value)

    monkeypatch.setattr(migrate, "_value", corrupt)
    with pytest.raises(migrate.Mismatch) as caught:
        migrate.copy(db, sqlite_db, log=lambda _: None)
    assert "table decision" in str(caught.value)
    assert "('d1',) differs in question" in str(caught.value)
    assert set(_counts(sqlite_db).values()) == {0}


# --- Postgres <-> SQLite ----------------------------------------------------


@pytest.mark.postgres_only
def test_round_trip_postgres_to_sqlite_to_postgres(db, sqlite_db, pg_scratch, capsys):
    _seed(db)
    sqlite_path = sqlite_db.dsn.removeprefix("sqlite:///")
    admin.main(["migrate-from-postgres", "--from", db.dsn, "--to", sqlite_path])
    admin.main(["migrate-to-postgres", "--from", sqlite_path, "--to", pg_scratch.dsn])
    tables = len(_schema_tables("schema.sql"))
    assert capsys.readouterr().out.count(f"verified {tables} tables") == 2
    # The far end equals the start, by the same verifier and by plain counts.
    checks = migrate.verify(db, pg_scratch)
    assert {c.name: c.rows for c in checks} == _counts(db)
    assert all(n > 0 for n in _counts(pg_scratch).values())
    with db.connect() as a, pg_scratch.connect() as b:
        q = "select * from comment order by id"
        assert a.execute(q).fetchall() == b.execute(q).fetchall()
        q = "select current_version from artifact"
        assert b.execute(q).fetchall() == [(2,)]
    assert _new_event_seq(pg_scratch) == 8
    assert _new_event_seq(sqlite_db) == 8


@pytest.mark.postgres_only
@pytest.mark.parametrize("direction", ["to_sqlite", "to_postgres"])
def test_the_verifier_catches_a_changed_row(
    direction, db, sqlite_db, pg_scratch, capsys
):
    _seed(db)
    if direction == "to_sqlite":
        src, dst = db, sqlite_db
    else:
        migrate.copy(db, sqlite_db, log=lambda _: None)
        src, dst = sqlite_db, pg_scratch
    migrate.copy(src, dst, log=lambda _: None)
    assert migrate.verify(src, dst)
    with dst.connect() as conn:
        conn.execute("update comment set held = %s where id = 'c2'", (False,))
    with pytest.raises(migrate.Mismatch) as caught:
        migrate.verify(src, dst)
    message = str(caught.value)
    assert message.startswith("table comment:"), message
    assert "('c2',) differs in held" in message


@pytest.mark.postgres_only
def test_the_cli_refuses_with_a_non_zero_exit_and_no_password(db, sqlite_db, capsys):
    _seed(db)
    sqlite_path = sqlite_db.dsn.removeprefix("sqlite:///")
    admin.main(["migrate-from-postgres", "--from", db.dsn, "--to", sqlite_path])
    with pytest.raises(SystemExit) as caught:
        admin.main(["migrate-from-postgres", "--from", db.dsn, "--to", sqlite_path])
    assert "REFUSED" in str(caught.value) and "not empty" in str(caught.value)
    err = capsys.readouterr().err
    assert "copying" in err
    password = urlsplit(db.dsn).password
    if password:
        assert password not in err


def test_a_pg_dsn_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("SOME_PG_URI", "postgresql://u@h/d")
    assert admin._postgres_dsn("env:SOME_PG_URI") == "postgresql://u@h/d"
    with pytest.raises(SystemExit):
        admin._postgres_dsn("env:NOT_SET_ANYWHERE_X")
    with pytest.raises(SystemExit):
        admin._postgres_dsn("sqlite:///x.db")
    assert admin._sqlite_dsn("/data/artifacts.db") == "sqlite:////data/artifacts.db"


def test_the_restore_drill_restores_a_snapshot_and_counts_its_rows(
    db, sqlite_db, tmp_path, capsys
):
    _seed(db)
    migrate.copy(db, sqlite_db, log=lambda _: None)
    snapshot = tmp_path / "artifacts-sqlite-2026-10-15.db"
    with sqlite_db.connect() as conn:
        conn.execute("vacuum into %s", (str(snapshot),))
    admin.main(["restore-drill", "--snapshot", str(snapshot)])
    out = capsys.readouterr().out
    assert "integrity_check ok" in out
    assert "comment\t2\n" in out
    tables = len(_schema_tables("schema.sql"))
    assert f"restored and verified {tables} tables" in out


def test_the_restore_drill_refuses_a_corrupt_snapshot(tmp_path):
    snapshot = tmp_path / "bad.db"
    snapshot.write_bytes(b"SQLite format 3\x00" + b"\x01" * 4096)
    with pytest.raises(SystemExit, match="REFUSED"):
        admin.main(["restore-drill", "--snapshot", str(snapshot)])
