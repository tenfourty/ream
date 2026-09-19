"""The production entrypoint. `Dockerfile`'s CMD runs uvicorn against
`artifacts.serve:app_from_env` with `--factory`, so uvicorn calls this once
at startup instead of importing a module-level `app` object -- the app
cannot exist until `DATABASE_URL` has been read and the schema migrated.
"""

import os
import signal

from fastapi import FastAPI

from artifacts import events
from artifacts.app import create_app
from artifacts.db import Database


def _arm_shutdown_signals(app: FastAPI) -> None:
    """End every held /events request as soon as this process is asked to stop.

    Why a signal handler and not a shutdown event: uvicorn runs the lifespan
    shutdown AFTER it has waited for in-flight requests to finish, and a
    request sleeping out a 280-second hold is exactly the in-flight request
    being waited for. By the time a shutdown handler ran, the platform would
    already have killed the process -- 30 seconds is the default grace period,
    and every waiting agent would see a request that simply never answers
    (measured 19 Sep 2026; `artifacts.events._shutdown` carries the evidence).

    The handler is installed on STARTUP rather than at import, because uvicorn
    installs its own handlers first and would otherwise replace ours. Ours sets
    the flag and then delegates to whatever was there, so uvicorn's graceful
    shutdown still runs exactly as it did -- it just finds no request left to
    wait for.
    """

    def install() -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous = signal.getsignal(sig)
            except (ValueError, OSError):  # pragma: no cover -- not the main thread
                continue

            def handler(signum, frame, _previous=previous):
                events.begin_shutdown()
                if callable(_previous):
                    _previous(signum, frame)

            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover -- not the main thread
                continue

    app.add_event_handler("startup", install)


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
    app = create_app(db)
    _arm_shutdown_signals(app)
    return app
