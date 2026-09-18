def test_migrate_creates_every_table(db):
    with db.connect() as conn:
        rows = conn.execute(
            "select table_name from information_schema.tables where table_schema = 'public'"
        ).fetchall()
    names = {row[0] for row in rows}
    assert {"agent_key", "artifact", "version", "file", "state"} <= names


def test_migrate_is_idempotent(db):
    db.migrate()
    db.migrate()
    with db.connect() as conn:
        count = conn.execute("select count(*) from artifact").fetchone()[0]
    assert count == 0
