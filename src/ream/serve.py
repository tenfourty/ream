"""The production entrypoint. `Dockerfile`'s CMD runs uvicorn against
`ream.serve:app_from_env` with `--factory`, so uvicorn calls this once
at startup instead of importing a module-level `app` object -- the app
cannot exist until `DATABASE_URL` has been read and the schema migrated.
"""

import logging
import os
import signal
import sys

from ream import (
    comments,
    envnames,
    events,
    maintenance,
    owner,
    partner,
    partner_in,
    plugin_api,
    repack,
    settings,
    sinks,
)
from ream.app import create_app
from ream.db import Database

log = logging.getLogger("ream.serve")


def _arm_shutdown_signals(app):
    """Wrap `app` so every held /events request ends as soon as this process is
    asked to stop.

    Why a signal handler and not a shutdown event: uvicorn runs the lifespan
    shutdown AFTER it has waited for in-flight requests to finish, and a
    request sleeping out a 280-second hold is exactly the in-flight request
    being waited for. By the time a shutdown handler ran, the platform would
    already have killed the process -- 30 seconds is the default grace period,
    and every waiting agent would see a request that simply never answers
    (measured 19 Sep 2026; `ream.events._shutdown` carries the evidence).

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

            async def watching(message):
                # The app has finished shutting down: stop the core outbox sender.
                if message["type"].startswith("lifespan.shutdown"):
                    sinks.stop(timeout=2.0)
                await send(message)

            await app(scope, receive, watching)
            return
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
    # `ream.auth` rides along: failed agent keys, one line each, never the
    # key (homelab-ij8.100). A burst of them is the thing worth seeing after the
    # fact, and it must not depend on logging's last-resort stderr handler.
    for name in ("ream.gallery.access", "ream.auth"):
        logger = logging.getLogger(name)
        if logger.handlers:
            continue
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
    # Every problem with the settings at once (owner, origins, mail, the partner groups),
    # before any connection is made; the message names variables and never a value.
    configured = settings.validate_for_serve()
    defaulted = settings.defaulted()
    if defaulted:
        log.warning(
            "fresh-install defaults in use for: %s (a deployment sets these)",
            ", ".join(defaulted),
        )
    if (
        not settings.is_loopback(configured.bind_host)
        and not configured.trusted_ip_header
    ):
        log.warning(
            "REAM_TRUSTED_IP_HEADER is not set and the server binds to a non-loopback"
            " address: rate limiting will key on the proxy's address, not the visitor's."
            " Behind a proxy, set it to the header your proxy writes (for example X-Real-IP);"
            " with no proxy, ignore this"
        )
    # The key files: a missing or malformed one stops the start the same way.
    try:
        partner_config = partner.config_from_env()
        inbound = partner_in.config_from_env(partner_config)
    except (partner.PartnerConfigError, partner_in.PartnerInboundConfigError) as exc:
        raise SystemExit(str(exc)) from None
    db = Database(dsn)
    db.migrate()
    # The owner id is stored in rows: a pod started with another one refuses to start
    # rather than read the owner's rows as a stranger's (owner.py).
    try:
        owner.check(db, configured.owner_id)
    except owner.OwnerMismatch as exc:
        raise SystemExit(str(exc)) from None
    # Plug-ins register before anything serves; one that cannot stops the start.
    try:
        plugin_api.load()
    except plugin_api.PluginError as exc:
        raise SystemExit(str(exc)) from None
    comments.start_sweeper(db)
    # The nightly backup and purge (maintenance.py). REAM_MAINTENANCE=off
    # turns it off, for tests that build the real app.
    if envnames.get("MAINTENANCE") != "off":
        maintenance.start(db)
        maintenance.start_image_sweep(db)
    # The backfill of old versions into deltas, in the background, only when asked for
    # (REAM_PACK_VERSIONS=startup): the first run is watched, not automatic
    # (docs/agent/specs/2026-10-26-delta-versions-design.md, rollout step 2).
    if envnames.get("PACK_VERSIONS") == "startup":
        repack.start(db)
    # The partner bridge's sender: only when fully configured (partner.py).
    partner.start(db, partner_config)
    # The core outbox's sender, for every sink that delivers through it (sinks.py).
    # Breaker state and plug-in failure counts in the database: one view over all workers.
    sinks.bind(db)
    sinks.start(db)
    return _arm_shutdown_signals(create_app(db, inbound))
