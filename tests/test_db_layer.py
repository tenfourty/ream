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
