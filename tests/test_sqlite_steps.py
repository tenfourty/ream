"""A step missing from the SQLite schema steps can never ship: two lanes each took
the next number and a rebase kept one (runcove-5t3oj.56)."""

from ream import db as dbmod


def test_sqlite_steps_are_contiguous_from_two_to_the_schema_version():
    # Step 1 is the base file, `schema_sqlite.sql`; every later step is numbered here.
    assert sorted(dbmod._SQLITE_STEPS) == list(
        range(2, dbmod.SQLITE_SCHEMA_VERSION + 1)
    )
