"""Every test gets a clean database, whether it ran first, last or after a test
that altered the schema.

The `db` fixture empties the tables between tests instead of rebuilding the
schema, so these tests guard that path. The emptiness check runs both before
and after the writer, so either order is covered in one serial run:

    uv run pytest -q -p no:randomly tests/test_isolation.py -k isolation
"""

import os

import pytest
from fastapi.testclient import TestClient

from artifacts import comments, decisions, events, holds, keys, ratelimit, store
from artifacts.app import create_app, create_gallery_app
from artifacts.db import Database

PAGE = "<!doctype html><html><head><title>t</title></head><body>{}</body></html>"


def _tables(conn) -> list[str]:
    rows = conn.execute(
        "select tablename from pg_tables where schemaname = current_schema()"
        " order by tablename"
    ).fetchall()
    return [row[0] for row in rows]


def _row_counts(db) -> dict[str, int]:
    with db.connect() as conn:
        tables = _tables(conn)
        assert tables, "no tables at all: the schema was never built"
        return {
            table: conn.execute(f'select count(*) from "{table}"').fetchone()[0]  # noqa: S608 -- names come from pg_tables
            for table in tables
        }


def _assert_clean(db) -> None:
    counts = _row_counts(db)
    assert {t: n for t, n in counts.items() if n} == {}
    with db.connect() as conn:
        sequences = conn.execute(
            "select sequencename, last_value from pg_sequences"
            " where schemaname = current_schema()"
        ).fetchall()
        assert sequences, "expected at least event.seq's sequence"
        # last_value is NULL until nextval() has been called since the last restart.
        assert {name: value for name, value in sequences if value is not None} == {}
    # ...and the next event number is back at its start.
    published = store.publish(db, "probe", "Probe", PAGE.format("p"))
    with db.connect() as conn:
        seq = events.append(
            conn,
            artifact_id=published.id,
            agent="probe",
            actor="probe",
            kind="comment_added",
            summary="s",
        )
    assert seq == 1


def _write_every_family(db) -> None:
    keys.mint(db, agent="writer")
    page = store.publish(db, "writer", "Board", PAGE.format("b"))
    asked = decisions.ask(
        db,
        agent="writer",
        artifact_id=page.id,
        question="ship?",
        options=[{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}],
    )
    decisions.answer(db, asked.id, letter="a", note="go", by="jeremy")
    comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="hi"
    )
    with db.connect() as conn:
        events.append(
            conn,
            artifact_id=page.id,
            agent="writer",
            actor="jeremy",
            kind="comment_added",
            summary="extra",
        )
        holds.set_mode(conn, "live")
    counts = _row_counts(db)
    for table in ("agent_key", "artifact", "version", "decision", "comment", "event"):
        assert counts[table] > 0, table
    assert counts["human_pref"] == 1
    with db.connect() as conn:
        seq = conn.execute(
            "select last_value from pg_sequences where sequencename = 'event_seq_seq'"
        ).fetchone()[0]
    assert seq is not None and seq >= 2


def test_every_table_is_empty_at_the_start(db):
    assert set(_row_counts(db).values()) == {0}


@pytest.mark.postgres_only  # reads Postgres's catalogue
def test_isolation_empty_before_the_writer(db):
    _assert_clean(db)


@pytest.mark.postgres_only  # reads Postgres's catalogue
def test_isolation_writes_every_table_family(db):
    _write_every_family(db)


@pytest.mark.postgres_only  # reads Postgres's catalogue
def test_isolation_empty_after_the_writer(db):
    _assert_clean(db)


# --- a test that alters the schema must not poison the next one --------------


@pytest.mark.usefixtures("fresh_schema")
@pytest.mark.postgres_only  # reads Postgres's catalogue
@pytest.mark.postgres_only  # reads Postgres's catalogue
def test_isolation_alters_the_schema(db):
    with db.connect() as conn:
        conn.execute("alter table artifact drop column agents_can_read")
        conn.execute("drop table human_pref")
        conn.execute("create table leftover_from_a_test (x int)")
        conn.execute("insert into leftover_from_a_test values (1)")


@pytest.mark.postgres_only  # reads Postgres's catalogue
@pytest.mark.postgres_only  # reads Postgres's catalogue
def test_isolation_the_next_test_sees_the_real_schema(db):
    page = store.publish(db, "owner", "After", PAGE.format("a"))
    with db.connect() as conn:
        can_read = conn.execute(
            "select agents_can_read from artifact where id = %s", (page.id,)
        ).fetchone()[0]
        assert can_read is True
        holds.set_mode(conn, "live")
        assert holds.get_mode(conn) == "live"
        assert "leftover_from_a_test" not in _tables(conn)
    _write_every_family(db)


@pytest.mark.usefixtures("fresh_schema")
@pytest.mark.postgres_only  # reads Postgres's catalogue
@pytest.mark.postgres_only  # reads Postgres's catalogue
def test_isolation_the_fingerprint_sees_a_schema_change(db, _schema):
    """The check the `db` fixture runs after every test not asking for a rebuild."""
    assert _schema.fingerprint() == _schema.built
    with db.connect() as conn:
        conn.execute("alter table human_pref add column extra int")
    assert _schema.fingerprint() != _schema.built


@pytest.mark.postgres_only  # reads Postgres's catalogue
def test_a_dbname_in_the_query_does_not_override_the_workers_database():
    from conftest import _with_dbname

    got = _with_dbname("postgresql://u@/postgres?host=/tmp/x&dbname=postgres", "w1")
    assert got == "postgresql://u@/w1?host=/tmp/x"
    assert _with_dbname("postgresql://u@h:5432/postgres", "w1") == (
        "postgresql://u@h:5432/w1"
    )


# --- an app reused across tests carries nothing between them ----------------
#
# tests/conftest.py (`_ReusedApps`) hands one app per worker from each of
# `create_gallery_app(db)` and `create_app(db)` to one test at a time instead
# of building one per test. These say what that must not change, for both.
# `_next_test()` does what the autouse fixture does between two tests, so each
# of these crosses a test boundary inside one test.

_FACTORIES = pytest.mark.parametrize(
    "factory", [create_gallery_app, create_app], ids=["gallery", "create_app"]
)


def _client(app):
    # The gallery host, which `create_app`'s router needs; any host serves the
    # bare gallery app.
    return TestClient(app, base_url="https://artifacts.tenfourty.site")


def _bad(app):
    return _client(app).get(
        "/api/v1/artifacts",
        headers={"Authorization": "Bearer art_nope", "x-real-ip": "203.0.113.9"},
    )


def _next_test() -> None:
    from conftest import _ReusedApps

    _ReusedApps.handed.clear()


def _reusing() -> bool:
    return os.environ.get("ARTIFACTS_TEST_FRESH_APPS") != "1"


@_FACTORIES
def test_a_reused_app_remembers_no_failed_keys_from_the_test_before(db, factory):
    app = factory(db)
    statuses = [_bad(app).status_code for _ in range(ratelimit.THRESHOLD)]
    assert statuses[-1] == 429, "the limiter never engaged: nothing was proved"
    _next_test()
    again = factory(db)
    if _reusing():
        assert again is app, "not reused, so this proves nothing about reuse"
    assert _bad(again).status_code == 401


@_FACTORIES
def test_a_reused_app_uses_the_database_this_test_patched(db, monkeypatch, factory):
    # An earlier "test" had its own `db`: another object, the same database.
    first = factory(Database(db.dsn))
    _next_test()
    app = factory(db)
    if _reusing():
        assert app is first, "not reused, so this proves nothing about reuse"
    token, _ = keys.mint(db, agent="seat")
    client = _client(app)
    listed = client.get(
        "/api/v1/artifacts", headers={"Authorization": f"Bearer {token}"}
    )
    assert listed.status_code == 200

    def refuse():
        raise RuntimeError("this test's database, patched")

    monkeypatch.setattr(db, "connect", refuse)
    with pytest.raises(RuntimeError, match="this test's database, patched"):
        client.get("/api/v1/artifacts", headers={"Authorization": f"Bearer {token}"})


@_FACTORIES
def test_two_apps_from_one_factory_in_one_test_are_two_apps(db, factory):
    assert factory(db) is not factory(db)
