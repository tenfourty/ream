"""The connection layer (the SQLite switch, slice 3): every module talks to the
database through `db.Connection`, which owns transactions, rollback and eager
results, so the backend can change under it."""

import ast
import pathlib

import pytest

from artifacts import db as dbmod


def test_rows_are_fetched_eagerly_and_read_in_order(db):
    with db.connect() as conn:
        result = conn.execute(
            "select 1 union all select 2 union all select 3 order by 1"
        )
        assert result.fetchone() == (1,)
        assert result.fetchall() == [(2,), (3,)]
        assert result.fetchone() is None
        assert list(conn.execute("select 1")) == [(1,)]


def test_a_statement_without_rows_reports_its_row_count(db):
    with db.connect() as conn:
        conn.execute("create temporary table t (n int)")
        inserted = conn.execute("insert into t values (1), (2)")
        assert inserted.rowcount == 2
        assert inserted.fetchall() == []


def test_rollback_undoes_only_the_innermost_transaction(db):
    with db.connect() as conn:
        conn.execute("create temporary table t (n int)")
        with conn.transaction():
            conn.execute("insert into t values (1)")
            with conn.transaction():
                conn.execute("insert into t values (2)")
                raise dbmod.Rollback()
        assert conn.execute("select n from t").fetchall() == [(1,)]


def test_any_other_error_undoes_the_transaction_and_propagates(db):
    with db.connect() as conn:
        conn.execute("create temporary table t (n int)")
        with pytest.raises(ValueError), conn.transaction():
            conn.execute("insert into t values (1)")
            raise ValueError("refused")
        assert conn.execute("select count(*) from t").fetchone() == (0,)


def test_a_duplicate_key_is_caught_by_the_layers_name_for_it(db):
    with db.connect() as conn:
        conn.execute("create temporary table t (n int primary key)")
        conn.execute("insert into t values (1)")
        with pytest.raises(dbmod.DUPLICATE_KEY):
            conn.execute("insert into t values (1)")


def test_no_module_but_db_imports_the_driver():
    src = pathlib.Path(dbmod.__file__).parent
    offenders = []
    for path in sorted(src.glob("*.py")):
        if path.name == "db.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n == "psycopg" or n.startswith("psycopg.") for n in names):
                offenders.append(path.name)
    assert offenders == []


@pytest.mark.parametrize("bad", ["a\x00b", "a\ud800b"])
def test_a_string_no_column_can_hold_is_refused_before_it_is_sent(db, bad):
    with db.connect() as conn:
        conn.execute("create temporary table t (s text)")
        with pytest.raises(dbmod.DataError):
            conn.execute("insert into t values (%s)", (bad,))
        assert conn.execute("select count(*) from t").fetchone() == (0,)


@pytest.mark.parametrize(
    "bad",
    [{"k": "a\x00"}, {"a\ud800": 1}, ["ok", ["a\x00"]], {"n": {"m": "\udfff"}}],
)
def test_a_json_value_holding_one_is_refused_too(bad):
    with pytest.raises(dbmod.DataError):
        dbmod.jsonb(bad)


def test_closing_a_request_connection_does_not_checkpoint_the_wal(tmp_path):
    """runcove-5t3oj.20.10: when the LAST connection to a WAL database closes,
    SQLite checkpoints and deletes the WAL (two fsyncs). Every request opens
    and closes its own connection, so the layer keeps one idle connection open
    and the WAL outlives each request's."""
    path = tmp_path / "a.db"
    database = dbmod.Database(f"sqlite:///{path}")
    database.migrate()
    for n in range(3):
        with database.connect() as conn, conn.transaction():
            conn.execute("create table if not exists t (n int)")
            conn.execute("insert into t values (%s)", (n,))
    wal = pathlib.Path(f"{path}-wal")
    assert wal.exists() and wal.stat().st_size > 0
    with database.connect() as conn:
        assert conn.execute("select count(*) from t").fetchone() == (3,)


# ---- tuned defaults (runcove-5t3oj.34, 20 Oct 2026) ------------------------


def _sqlite(tmp_path) -> dbmod.Database:
    database = dbmod.Database(f"sqlite:///{tmp_path / 'tuned.db'}")
    database.migrate()
    return database


def test_a_request_connection_is_reused_with_its_cache(tmp_path):
    database = _sqlite(tmp_path)
    with database.connect() as conn:
        first = conn._raw
    with database.connect() as conn:
        assert conn._raw is first
    database.close()


def test_reused_connections_carry_the_tuned_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "SQLITE_SYNCHRONOUS", "normal")
    database = _sqlite(tmp_path)
    database.close()  # the migration's connection was opened before the patch
    with database.connect() as conn:
        assert conn.execute("pragma synchronous").fetchone() == (1,)  # NORMAL
        assert conn.execute("pragma cache_size").fetchone() == (
            -dbmod.SQLITE_CACHE_KIB,
        )
        assert conn.execute("pragma mmap_size").fetchone() == (dbmod.SQLITE_MMAP_BYTES,)
        assert conn.execute("pragma busy_timeout").fetchone() == (5000,)
    database.close()


def test_foreign_keys_are_back_on_for_the_next_request(tmp_path):
    database = _sqlite(tmp_path)
    with database.connect() as conn:
        conn.execute("pragma foreign_keys = off")
    with database.connect() as conn:
        assert conn.execute("pragma foreign_keys").fetchone() == (1,)
    database.close()


def test_a_failed_request_never_hands_its_connection_on(tmp_path):
    database = _sqlite(tmp_path)
    with pytest.raises(RuntimeError), database.connect() as conn:
        broken = conn._raw
        raise RuntimeError("boom")
    with database.connect() as conn:
        assert conn._raw is not broken
    database.close()


def test_a_connection_left_in_a_transaction_is_rolled_back_not_reused(tmp_path):
    database = _sqlite(tmp_path)
    with database.connect() as conn:
        conn.execute("create table t (n int)")
    with database.connect() as conn:
        open_one = conn._raw
        conn.execute("begin")
        conn.execute("insert into t values (1)")
    with database.connect() as conn:
        assert conn._raw is not open_one
        assert conn.execute("select count(*) from t").fetchone() == (0,)
    database.close()


def test_the_idle_list_is_bounded(tmp_path):
    database = _sqlite(tmp_path)
    from contextlib import ExitStack

    with ExitStack() as stack:
        for _ in range(dbmod.SQLITE_POOL_MAX + 3):
            stack.enter_context(database.connect())
    assert len(database._idle) == dbmod.SQLITE_POOL_MAX
    database.close()
    assert database._idle == []


def test_optimize_runs(tmp_path):
    database = _sqlite(tmp_path)
    database.optimize()
    database.close()
