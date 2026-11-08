"""Slice 5, part 1 (core-and-plugins design, section 5): the CHECKs naming house values
(`decision.answer_source`, `event.kind`) are gone, and code validates in their place.

The first half runs on whichever driver the suite runs; the second builds a SQLite file
at schema version 20 (the CHECKs still there), writes rows under them and migrates it."""

import re
from importlib import resources

import pytest

from ream import db as dbmod
from ream import decisions, events, store
from ream.db import Database

PAGE = "<h1>x</h1>"


def test_an_unknown_event_kind_is_refused_in_code(db):
    published = store.publish(db, "ops-seat", "Board", PAGE)
    with db.connect() as conn, pytest.raises(ValueError, match="unknown event kind"):
        events.append(
            conn,
            artifact_id=published.id,
            agent="ops-seat",
            actor="jeremy",
            kind="not_a_kind",
            summary="s",
        )
    assert events.list_for_agent(db, "ops-seat", include_receipts=True) == []


@pytest.mark.parametrize("source", decisions.stored_sources())
def test_every_stored_source_passes(source):
    decisions.require_stored_source(source)


def test_an_unknown_answer_source_is_refused_in_code():
    with pytest.raises(decisions.InvalidSource):
        decisions.require_stored_source("carrier-pigeon")


@pytest.mark.postgres_only
def test_postgres_has_no_check_on_either_column(db):
    with db.connect() as conn:
        defs = [
            r[0]
            for r in conn.execute(
                "select pg_get_constraintdef(oid) from pg_constraint"
                " where contype = 'c' and conrelid in"
                " ('decision'::regclass, 'event'::regclass)"
            ).fetchall()
        ]
    assert not [
        d
        for d in defs
        if re.search(r"\((answer_source|kind) ", d)
        and "ANY" in d
        and "question" not in d
    ]
    assert any("question" in d for d in defs)  # decision.kind's CHECK stays


def _check_defs(conn) -> list[tuple[str, str]]:
    return [
        (r[0], r[1])
        for r in conn.execute(
            "select conname, pg_get_constraintdef(oid) from pg_constraint"
            " where contype = 'c' and conrelid in"
            " ('decision'::regclass, 'event'::regclass)"
        ).fetchall()
    ]


@pytest.mark.postgres_only
@pytest.mark.usefixtures("fresh_schema")
def test_postgres_upgrade_drops_the_old_checks_and_keeps_the_rest(db):
    """A database from before the slice still has both CHECKs (inline and re-added
    names); the next start's migrate drops exactly those two."""
    with db.connect() as conn:
        before = {name for name, _ in _check_defs(conn)}
        conn.execute(
            "alter table event add constraint event_kind_check"
            " check (kind in ('decision_answered', 'comment_added'))"
            " not valid"
        )
        conn.execute(
            "alter table decision add constraint decision_answer_source_check"
            " check (answer_source in ('page', 'terminal', 'slack'))"
            " not valid"
        )
        added = {name for name, _ in _check_defs(conn)}
    assert {"event_kind_check", "decision_answer_source_check"} <= added
    db.migrate()
    with db.connect() as conn:
        after = _check_defs(conn)
    assert {name for name, _ in after} == before  # both dropped, every other CHECK kept
    assert any("question" in d for _, d in after)  # decision.kind's CHECK stays


# ---- SQLite: a v20 file, then the step -------------------------------------------------


def _build_v20(path) -> Database:
    database = Database(f"sqlite:///{path}")
    script = resources.files("ream").joinpath("schema_sqlite.sql").read_text()
    with database.connect() as conn, conn.transaction():
        for statement in dbmod._statements(script):
            conn.execute(statement)
        for number in range(2, 21):
            for statement in dbmod._SQLITE_STEPS[number]:
                if callable(statement):
                    statement(conn)
                else:
                    conn.execute(statement)
        conn.execute("pragma user_version = 20")
    return database


def _tables(database) -> dict[str, str]:
    with database.connect() as conn:
        return dict(
            conn.execute(
                "select name, sql from sqlite_master where type = 'table'"
                " and name in ('decision', 'event')"
            ).fetchall()
        )


def _seed_old(database) -> None:
    with database.connect() as conn, conn.transaction():
        conn.execute(
            "insert into artifact (id, title, agent, read_token, created_at,"
            " updated_at) values ('a', 't', 'x', 'r', datetime('now'), datetime('now'))"
        )
        conn.execute(
            "insert into decision (id, artifact_id, version_n, question, options,"
            " answer_source) values ('d-old', 'a', 1, 'q', '[]', 'telegram')"
        )
        conn.execute(
            "insert into event (artifact_id, agent, actor, kind, created_at)"
            " values ('a', 'x', 'y', 'receipt_set', datetime('now'))"
        )


def test_the_step_removes_both_checks_and_old_rows_read_unchanged(tmp_path):
    database = _build_v20(tmp_path / "v20.db")
    before = _tables(database)
    assert re.search(r"answer_source\s+in", before["decision"])
    assert re.search(r"\bkind\s+in", before["event"])
    _seed_old(database)
    database.migrate()
    after = _tables(database)
    assert not re.search(r"answer_source\s+in", after["decision"])
    assert not re.search(r"\bkind\s+in", after["event"])
    # decision.kind's CHECK is another constraint and stays
    assert "'question', 'review', 'form'" in after["decision"]
    with database.connect() as conn:
        assert conn.execute("pragma user_version").fetchone()[0] == (
            dbmod.SQLITE_SCHEMA_VERSION
        )
        assert conn.execute("pragma writable_schema").fetchone()[0] == 0
        assert conn.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "select answer_source from decision where id = 'd-old'"
        ).fetchone() == ("telegram",)
        assert conn.execute("select kind from event").fetchone() == ("receipt_set",)
    # a row written after the migration reads the same way
    with database.connect() as conn, conn.transaction():
        conn.execute(
            "insert into decision (id, artifact_id, version_n, question, options,"
            " answer_source) values ('d-new', 'a', 1, 'q', '[]', 'slack')"
        )
    with database.connect() as conn:
        assert conn.execute(
            "select answer_source from decision order by id"
        ).fetchall() == [("slack",), ("telegram",)]


def test_the_step_is_idempotent(tmp_path):
    database = _build_v20(tmp_path / "v20.db")
    database.migrate()
    first = _tables(database)
    with database.connect() as conn, conn.transaction():
        dbmod._drop_house_checks(conn)
    assert _tables(database) == first
    database.migrate()
    assert _tables(database) == first


def test_a_shape_it_does_not_know_fails_loudly_and_stays_at_v20(tmp_path):
    database = _build_v20(tmp_path / "odd.db")
    with database.connect() as conn:
        conn.execute("pragma writable_schema = on")
        conn.execute(
            "update sqlite_master set sql = replace(sql, 'check (kind in (',"
            " 'check (kind is not null and kind in (') where name = 'event'"
        )
        conn.execute("pragma writable_schema = off")
    odd = _tables(database)["event"]
    assert "kind is not null and kind in" in odd
    reopened = Database(f"sqlite:///{tmp_path / 'odd.db'}")
    with pytest.raises(RuntimeError, match="step 21"):
        reopened.migrate()
    with reopened.connect() as conn:
        assert conn.execute("pragma user_version").fetchone()[0] == 20
        assert conn.execute("pragma writable_schema").fetchone()[0] == 0
    assert _tables(reopened)["event"] == odd


# Slice 5, part 2: the source registry.


def test_core_and_slack_are_stored_sources():
    assert decisions.stored_sources()[: len(decisions.CORE_SOURCES)] == (
        decisions.CORE_SOURCES
    )
    assert "slack" in decisions.stored_sources()


def test_a_registered_source_can_be_written_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(decisions, "_REGISTERED", list(decisions._REGISTERED))
    with pytest.raises(decisions.InvalidSource):
        decisions.require_stored_source("teams")
    decisions.register_source("teams")
    decisions.register_source("teams")
    decisions.require_stored_source("teams")
    assert decisions.stored_sources().count("teams") == 1


@pytest.mark.parametrize("name", ["page", "terminal", "Teams", "a b", "", "x" * 33, 7])
def test_a_core_or_malformed_source_cannot_be_registered(name, monkeypatch):
    monkeypatch.setattr(decisions, "_REGISTERED", list(decisions._REGISTERED))
    with pytest.raises(ValueError):
        decisions.register_source(name)
    assert decisions.stored_sources() == (*decisions.CORE_SOURCES, "slack")
