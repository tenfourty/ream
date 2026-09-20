"""The production entrypoint. `Dockerfile`'s CMD runs uvicorn against
`artifacts.serve:app_from_env` with `--factory`, so uvicorn calls this once
at startup instead of importing a module-level `app` object -- the app
cannot exist until `DATABASE_URL` has been read and the schema migrated.
"""

import logging
import os
import signal
import sys

from artifacts import events
from artifacts.app import create_app
from artifacts.db import Database


def _arm_shutdown_signals(app):
    """Wrap `app` so every held /events request ends as soon as this process is
    asked to stop.

    Why a signal handler and not a shutdown event: uvicorn runs the lifespan
    shutdown AFTER it has waited for in-flight requests to finish, and a
    request sleeping out a 280-second hold is exactly the in-flight request
    being waited for. By the time a shutdown handler ran, the platform would
    already have killed the process -- 30 seconds is the default grace period,
    and every waiting agent would see a request that simply never answers
    (measured 19 Sep 2026; `artifacts.events._shutdown` carries the evidence).

    The handler is installed when the LIFESPAN STARTUP arrives rather than at
    import, because uvicorn installs its own handlers first and would otherwise
    replace ours. Ours sets the flag and then delegates to whatever was there,
    so uvicorn's graceful shutdown still runs exactly as it did -- it just finds
    no request left to wait for.

    Arming happens through the ASGI scope, not through `add_event_handler`,
    because what `create_app` returns is a bare host-routing function and not a
    FastAPI object. The first version of this called `add_event_handler` on it
    and failed at startup with an AttributeError; the test that was supposed to
    cover it passed a stand-in that HAD that method, so the fake was the bug.
    A wrapper works against whatever `create_app` returns and cannot drift from
    it.
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

    async def armed(scope, receive, send):
        if scope["type"] == "lifespan":
            install()
        await app(scope, receive, send)

    return armed


def _arm_gallery_access_log() -> None:
    """Give the gallery access logger a handler, at INFO, on stdout.

    Required, not decorative. uvicorn runs with `--no-access-log` (a
    content-page URL carries its capability token in the path), and its logging
    config leaves the ROOT logger with no handlers at WARNING — so a plain
    `logging.getLogger(...).info(...)` anywhere in this app emits absolutely
    nothing. Measured, not assumed: applying uvicorn's own LOGGING_CONFIG and
    asking gives `effective level: WARNING, handlers on root: []`.

    That is the same shape as the bug this logging exists to fix — a thing that
    looks present and produces silence — so it gets its own handler and its own
    level rather than inheriting whatever the process happens to have.

    `propagate = False` keeps these lines out of any root handler a future
    change adds, so they cannot be duplicated or accidentally re-routed into a
    file alongside content-origin logging.
    """
    logger = logging.getLogger("artifacts.gallery.access")
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def app_from_env():
    """Read `DATABASE_URL`, migrate, and return the fail-closed host router
    `create_app` builds. Never includes the DSN -- or anything else read from
    the environment -- in the SystemExit message: a DSN carries the database
    password, and this message is the one thing a broken deploy is guaranteed
    to put in a log."""
    _arm_gallery_access_log()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set")
    db = Database(dsn)
    db.migrate()
    return _arm_shutdown_signals(create_app(db))
