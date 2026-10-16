"""Copy the whole database between Postgres and SQLite, and prove the copy.

The SQLite switch, slice 7 (docs/agent/specs/2026-10-09-sqlite-design.md §5).
`artifacts-admin migrate-from-postgres` and `migrate-to-postgres` both call
`copy(source, destination)`; only the direction differs. The copier works on
any pair of backends (the SQLite test pass copies SQLite to SQLite).

What a copy does, in order:

1. Brings the destination's schema up to date (`Database.migrate()`), then
   refuses unless every table in it is empty: a copy never merges.
2. Reads both schemas from the databases themselves: the same tables with the
   same columns, or it refuses. So a table added to one schema and not the
   other stops the copy instead of being skipped.
3. Copies every table in foreign-key order, derived from the destination's own
   foreign keys. The two references that cannot be satisfied in one pass,
   `artifact.current_version` (a cycle with `version`) and
   `comment.thread_root` (a table referring to itself), are written NULL first
   and filled in once every table is in.
4. Carries the event counter over, so an event number is never reused: a new
   event after the copy gets a number above any the source ever handed out.
5. Verifies, inside the destination's still-open transaction: per table, the
   row count and a checksum over canonical JSON per row (`canonical_row`).
   Any difference raises `Mismatch` naming the table and what differs, and
   the destination's transaction rolls back, leaving it empty.

Read the source with the service stopped: the copy reads it twice (once to
copy, once to verify) and a write in between is reported as a mismatch.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from artifacts.db import Connection, Database, Json, _time_text

# The foreign keys a single ordered pass cannot satisfy. Each column here is
# nullable, written NULL with its row and set once every table is copied.
# A new cycle or self-reference fails the copy until it is named here.
DEFERRED = {("artifact", "current_version"), ("comment", "thread_root")}

# An unlabelled session: NULL in an older Postgres seat_read, '' on SQLite and
# in today's schema.sql. Mapped to '' wherever it is read (spec §5).
_EMPTY_SESSION = ("seat_read", "session")

_BATCH = 500


class Mismatch(Exception):
    """The copy, or the destination's schema, differs from the source."""


@dataclass
class Table:
    name: str
    columns: list[str]
    json_columns: set[str] = field(default_factory=set)
    primary_key: list[str] = field(default_factory=list)


@dataclass
class Schema:
    tables: dict[str, Table]
    # (child, parent, child columns) for every foreign key
    foreign_keys: list[tuple[str, str, tuple[str, ...]]]
    # table -> column of each counter (an AUTOINCREMENT or a serial)
    counters: dict[str, str]


@dataclass
class TableCheck:
    name: str
    rows: int
    checksum: str


# --- canonical form ---------------------------------------------------------


def _canonical_value(value):
    if isinstance(value, datetime):
        return _time_text(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    return value  # bool, int, str, None, and a JSON value as decoded


def canonical_row(table: str, row: dict) -> str:
    """One row as canonical JSON (spec §5): timestamps in the codec's format,
    bytes as hex, booleans true/false, NULL null, JSON values as values, keys
    sorted, fixed separators, and seat_read's NULL session as ''. The one
    function both directions hash, for source and destination alike."""
    out = {name: _canonical_value(value) for name, value in row.items()}
    if table == _EMPTY_SESSION[0] and out.get(_EMPTY_SESSION[1]) is None:
        out[_EMPTY_SESSION[1]] = ""
    return json.dumps(out, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def row_hash(table: str, row: dict) -> bytes:
    return hashlib.sha256(canonical_row(table, row).encode()).digest()


def table_checksum(row_hashes) -> str:
    """The row hashes sorted in Python, then hashed together: no dependence on
    either database's ordering or collation."""
    digest = hashlib.sha256()
    for h in sorted(row_hashes):
        digest.update(h)
    return digest.hexdigest()


# --- reading a schema -------------------------------------------------------


def read_schema(database: Database, conn: Connection) -> Schema:
    if database.sqlite:
        return _sqlite_schema(conn)
    return _postgres_schema(conn)


def _sqlite_schema(conn: Connection) -> Schema:
    tables: dict[str, Table] = {}
    foreign_keys = []
    counters = {}
    for name, sql in conn.execute(
        "select name, sql from sqlite_master where type = 'table'"
        " and name not like 'sqlite_%' order by name"
    ).fetchall():
        info = conn.execute(f'pragma table_info("{name}")').fetchall()
        # (cid, name, type, notnull, default, pk)
        table = Table(
            name,
            [c[1] for c in info],
            {c[1] for c in info if c[2].lower() == "jsonb"},
            [c[1] for c in sorted(info, key=lambda c: c[5]) if c[5]],
        )
        tables[name] = table
        fks: dict[int, tuple[str, list[str]]] = {}
        for fk in conn.execute(f'pragma foreign_key_list("{name}")').fetchall():
            # (id, seq, table, from, to, on_update, on_delete, match)
            fks.setdefault(fk[0], (fk[2], []))[1].append(fk[3])
        foreign_keys += [(name, parent, tuple(cols)) for parent, cols in fks.values()]
        if re.search(r"\bautoincrement\b", sql or "", re.IGNORECASE):
            (counters[name],) = table.primary_key
    return Schema(tables, foreign_keys, counters)


def _postgres_schema(conn: Connection) -> Schema:
    tables: dict[str, Table] = {}
    counters = {}
    for name, column, data_type, default in conn.execute(
        # ::text on every column: information_schema's columns are domains
        # (sql_identifier, character_data) that a driver may hand back as
        # bytes, which made `default.startswith("nextval(")` raise on a real
        # restored database (the rehearsal, 16 Oct 2026).
        "select table_name::text, column_name::text, data_type::text,"
        "       column_default::text"
        " from information_schema.columns"
        " where table_schema = current_schema()"
        "   and table_name in (select tablename from pg_tables"
        "                      where schemaname = current_schema())"
        " order by table_name, ordinal_position"
    ).fetchall():
        table = tables.setdefault(name, Table(name, []))
        table.columns.append(column)
        if data_type in ("jsonb", "json"):
            table.json_columns.add(column)
        if default and default.startswith("nextval("):
            counters[name] = column
    for name, column in conn.execute(
        "select c.relname, a.attname from pg_index i"
        " join pg_class c on c.oid = i.indrelid"
        " join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)"
        " where i.indisprimary and c.relnamespace = current_schema()::regnamespace"
        " order by c.relname, array_position(i.indkey::int2[], a.attnum)"
    ).fetchall():
        if name in tables:
            tables[name].primary_key.append(column)
    foreign_keys = [
        (child, parent, tuple(cols))
        for child, parent, cols in conn.execute(
            "select c.relname, p.relname,"
            "  array(select a.attname from unnest(k.conkey) with ordinality u(n, o)"
            "        join pg_attribute a on a.attrelid = k.conrelid and a.attnum = u.n"
            "        order by u.o)"
            " from pg_constraint k"
            " join pg_class c on c.oid = k.conrelid"
            " join pg_class p on p.oid = k.confrelid"
            " where k.contype = 'f' and k.connamespace = current_schema()::regnamespace"
        ).fetchall()
    ]
    return Schema(tables, foreign_keys, counters)


def copy_order(schema: Schema) -> list[str]:
    """Every table, parents before children, the DEFERRED columns aside.
    Raises on a cycle that DEFERRED does not break."""
    deps: dict[str, set[str]] = {name: set() for name in schema.tables}
    for child, parent, cols in schema.foreign_keys:
        if any((child, c) in DEFERRED for c in cols):
            continue
        if child == parent:
            raise Mismatch(
                f"{child} refers to itself through {', '.join(cols)}:"
                " add the column to migrate.DEFERRED"
            )
        deps[child].add(parent)
    order: list[str] = []
    while deps:
        ready = sorted(t for t, parents in deps.items() if not parents - set(order))
        if not ready:
            raise Mismatch(
                f"a foreign-key cycle among {', '.join(sorted(deps))}:"
                " add a nullable column of it to migrate.DEFERRED"
            )
        for name in ready:
            order.append(name)
            del deps[name]
    return order


def _compare_schemas(src: Schema, dst: Schema) -> None:
    if set(src.tables) != set(dst.tables):
        only_src = sorted(set(src.tables) - set(dst.tables))
        only_dst = sorted(set(dst.tables) - set(src.tables))
        raise Mismatch(
            f"the schemas differ: tables only in the source {only_src},"
            f" only in the destination {only_dst}"
        )
    for name, table in src.tables.items():
        if set(table.columns) != set(dst.tables[name].columns):
            only_src = sorted(set(table.columns) - set(dst.tables[name].columns))
            only_dst = sorted(set(dst.tables[name].columns) - set(table.columns))
            raise Mismatch(
                f"table {name}: columns only in the source {only_src},"
                f" only in the destination {only_dst}"
            )
    if src.counters != dst.counters:
        raise Mismatch(
            f"the counters differ: source {src.counters}, destination {dst.counters}"
        )
    for table, column in DEFERRED:
        if table not in dst.tables or column not in dst.tables[table].columns:
            raise Mismatch(f"migrate.DEFERRED names {table}.{column}, which is gone")


# --- reading and writing rows -----------------------------------------------


def _select(table: Table) -> str:
    cols = ", ".join(f'"{c}"' for c in table.columns)
    return f'select {cols} from "{table.name}"'  # noqa: S608 -- names from the catalogue


def _rows(database: Database, conn: Connection, table: Table) -> Iterator[dict]:
    """A table's rows as dicts, streamed in batches, never all in memory (a
    version body is up to 8 MiB)."""
    raw = conn._raw
    if database.sqlite:
        cur = raw.execute(_select(table))
        while batch := cur.fetchmany(_BATCH):
            for row in batch:
                yield dict(zip(table.columns, row, strict=True))
        return
    # A named (server-side) cursor, which Postgres holds only in a transaction.
    with raw.transaction(), raw.cursor(name=f"migrate_{table.name}") as cur:
        cur.itersize = _BATCH
        cur.execute(_select(table))
        for row in cur:
            yield dict(zip(table.columns, row, strict=True))


def _count(conn: Connection, table: str) -> int:
    return conn.execute(f'select count(*) from "{table}"').fetchone()[0]  # noqa: S608


def counter_mark(database: Database, conn: Connection, table: str, column: str) -> int:
    """The highest number this counter has handed out, whether or not its row
    still exists: the next one must be above it."""
    (top,) = conn.execute(
        f'select coalesce(max("{column}"), 0) from "{table}"'  # noqa: S608
    ).fetchone()
    if database.sqlite:
        row = conn.execute(
            "select seq from sqlite_sequence where name = %s", (table,)
        ).fetchone()
        return max(top, row[0] if row else 0)
    (seq,) = conn.execute(
        "select pg_get_serial_sequence(%s, %s)", (table, column)
    ).fetchone()
    last, called = conn.execute(f"select last_value, is_called from {seq}").fetchone()  # noqa: S608
    return max(top, last if called else last - 1)


def _set_counter(
    database: Database, conn: Connection, table: str, column: str, mark: int
) -> None:
    if database.sqlite:
        conn.execute("delete from sqlite_sequence where name = %s", (table,))
        conn.execute(
            "insert into sqlite_sequence (name, seq) values (%s, %s)", (table, mark)
        )
        return
    (seq,) = conn.execute(
        "select pg_get_serial_sequence(%s, %s)", (table, column)
    ).fetchone()
    conn.execute("select setval(%s::regclass, %s, %s)", (seq, max(mark, 1), mark > 0))


def _value(table: Table, column: str, value):
    if (table.name, column) == _EMPTY_SESSION and value is None:
        return ""
    if column in table.json_columns and value is not None:
        return Json(value)
    return value


# --- the copy ---------------------------------------------------------------


def copy(src_db: Database, dst_db: Database, log=print) -> list[TableCheck]:
    """Copy every row of `src_db` into the empty `dst_db`, then verify. On any
    refusal or mismatch nothing is left in the destination."""
    dst_db.migrate()
    with src_db.connect() as src, dst_db.connect() as dst:
        src_schema = read_schema(src_db, src)
        dst_schema = read_schema(dst_db, dst)
        _compare_schemas(src_schema, dst_schema)
        order = copy_order(dst_schema)
        busy = [t for t in order if _count(dst, t)]
        if busy:
            raise Mismatch(
                f"the destination is not empty ({', '.join(busy)});"
                " a copy only goes into an empty database"
            )
        with dst.transaction():
            for name in order:
                table = dst_schema.tables[name]
                deferred = [c for c in table.columns if (name, c) in DEFERRED]
                cols = ", ".join(f'"{c}"' for c in table.columns)
                marks = ", ".join(["%s"] * len(table.columns))
                insert = f'insert into "{name}" ({cols}) values ({marks})'  # noqa: S608
                n = 0
                for row in _rows(src_db, src, src_schema.tables[name]):
                    values = [
                        None if c in deferred else _value(table, c, row[c])
                        for c in table.columns
                    ]
                    dst.execute(insert, values)
                    n += 1
                log(f"copied {name}: {n} rows")
            for name, column in sorted(DEFERRED):
                _fill_deferred(src_db, src, dst, src_schema.tables[name], column)
            for name, column in sorted(dst_schema.counters.items()):
                mark = counter_mark(src_db, src, name, column)
                _set_counter(dst_db, dst, name, column, mark)
                log(f"counter {name}.{column}: next is above {mark}")
            checks = _verify(src_db, src, src_schema, dst_db, dst, dst_schema, order)
    for check in checks:
        log(f"verified {check.name}: {check.rows} rows, sha256 {check.checksum[:16]}")
    log(f"verified {len(checks)} tables, {sum(c.rows for c in checks)} rows")
    return checks


def _fill_deferred(
    src_db: Database, src: Connection, dst: Connection, table: Table, column: str
) -> None:
    key = table.primary_key
    if not key:
        raise Mismatch(f"{table.name} has no primary key to fill {column} by")
    where = " and ".join(f'"{k}" = %s' for k in key)
    update = f'update "{table.name}" set "{column}" = %s where {where}'  # noqa: S608
    for row in _rows(src_db, src, table):
        if row[column] is not None:
            dst.execute(update, [row[column], *(row[k] for k in key)])


# --- the verifier -----------------------------------------------------------


def verify(src_db: Database, dst_db: Database) -> list[TableCheck]:
    """Row counts and checksums, table by table; raises `Mismatch` naming the
    first table that differs and how."""
    with src_db.connect() as src, dst_db.connect() as dst:
        src_schema = read_schema(src_db, src)
        dst_schema = read_schema(dst_db, dst)
        _compare_schemas(src_schema, dst_schema)
        order = copy_order(dst_schema)
        return _verify(src_db, src, src_schema, dst_db, dst, dst_schema, order)


def _side(database: Database, conn: Connection, table: Table) -> dict:
    """Every row's hash, keyed by its primary key (by the hash itself when the
    table has none), so a mismatch can say which rows differ. Hashes only: a
    table's rows are never all held in memory."""
    hashes: dict = {}
    for row in _rows(database, conn, table):
        h = row_hash(table.name, row)
        hashes[_key(table, row) if table.primary_key else (h,)] = h
    return hashes


def _key(table: Table, row: dict) -> tuple:
    return tuple(_canonical_value(row[k]) for k in table.primary_key)


def _verify(
    src_db: Database,
    src: Connection,
    src_schema: Schema,
    dst_db: Database,
    dst: Connection,
    dst_schema: Schema,
    order: list[str],
) -> list[TableCheck]:
    checks = []
    for name in order:
        a = _side(src_db, src, src_schema.tables[name])
        b = _side(dst_db, dst, dst_schema.tables[name])
        a_sum, b_sum = table_checksum(a.values()), table_checksum(b.values())
        if len(a) != len(b) or a_sum != b_sum:
            changed = [k for k in a if k in b and a[k] != b[k]]
            columns = _changed_columns(
                (src_db, src, src_schema.tables[name]),
                (dst_db, dst, dst_schema.tables[name]),
                set(changed[:3]),
            )
            raise Mismatch(_difference(name, a, b, a_sum, b_sum, changed, columns))
        checks.append(TableCheck(name, len(a), a_sum))
    for name, column in sorted(src_schema.counters.items()):
        mark_a = counter_mark(src_db, src, name, column)
        mark_b = counter_mark(dst_db, dst, name, column)
        if mark_a != mark_b:
            raise Mismatch(
                f"table {name}: counter {column} is {mark_b} in the destination,"
                f" {mark_a} in the source"
            )
    return checks


def _changed_columns(src_side, dst_side, keys: set) -> dict:
    """For a few changed rows, the names of the columns that differ: a second
    read of both sides, keeping only those rows' canonical column values."""
    if not keys:
        return {}
    seen: list[dict] = []
    for database, conn, table in (src_side, dst_side):
        rows = {}
        for row in _rows(database, conn, table):
            if _key(table, row) in keys:
                rows[_key(table, row)] = {
                    c: canonical_row(table.name, {c: v}) for c, v in row.items()
                }
        seen.append(rows)
    a, b = seen
    return {
        k: sorted(c for c in a.get(k, {}) if a[k][c] != b.get(k, {}).get(c))
        for k in keys
    }


def _difference(
    name: str, a: dict, b: dict, a_sum: str, b_sum: str, changed: list, columns: dict
) -> str:
    """What differs, by key and column name only: never a value, which may be
    a page body or a hash a reader should not see in a log."""
    parts = [
        (
            f"table {name}: {len(a)} rows in the source, {len(b)} in the destination;"
            f" checksum {a_sum[:16]} vs {b_sum[:16]}"
        )
    ]
    missing = [k for k in a if k not in b]
    extra = [k for k in b if k not in a]
    if missing:
        parts.append(f"{len(missing)} missing from the destination, e.g. {missing[0]}")
    if extra:
        parts.append(f"{len(extra)} only in the destination, e.g. {extra[0]}")
    for k in changed[:3]:
        cols = ", ".join(columns.get(k) or ["its canonical form"])
        parts.append(f"row {k} differs in {cols}")
    if len(changed) > 3:
        parts.append(f"{len(changed) - 3} more rows differ")
    return "; ".join(parts)


# --- DSNs -------------------------------------------------------------------


def redact(dsn: str) -> str:
    """A DSN fit for a log: the password replaced, in URI and key=value form."""
    if "://" in dsn:
        parts = urlsplit(dsn)
        if parts.password is not None:
            host = parts.netloc.rsplit("@", 1)[1]
            user = parts.username or ""
            parts = parts._replace(netloc=f"{user}:***@{host}")
        query = re.sub(r"(?i)(password=)[^&]*", r"\1***", parts.query)
        return urlunsplit(parts._replace(query=query))
    return re.sub(r"(?i)(password\s*=\s*)('[^']*'|\S+)", r"\1***", dsn)
