"""The production entrypoint. `Dockerfile`'s CMD runs uvicorn against
`artifacts.serve:app_from_env` with `--factory`, so uvicorn calls this once
at startup instead of importing a module-level `app` object -- the app
cannot exist until `DATABASE_URL` has been read and the schema migrated.
"""

import os

from fastapi import FastAPI

from artifacts.app import create_app
from artifacts.db import Database


def app_from_env() -> FastAPI:
    """Read `DATABASE_URL`, migrate, and return the fail-closed host router
    `create_app` builds. Never includes the DSN -- or anything else read from
    the environment -- in the SystemExit message: a DSN carries the database
    password, and this message is the one thing a broken deploy is guaranteed
    to put in a log."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set")
    db = Database(dsn)
    db.migrate()
    return create_app(db)
