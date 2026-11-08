"""The plug-in API and its loader (core-and-plugins design, 4.1, 4.2, 4.5, 4.6).

A plug-in is a Python package with an entry point in the group `ream.plugins` (or the old
`artifacts.plugins`, kept for good)
naming a function `register(api: PluginAPI) -> None`. The server calls each once at
start-up, in name order. Registrations: `sink`, `answer_source`, `on_answer`, `scanner`, `mail_backend`,
`cli`, `client_defaults` (the last two matter to the command-line tools: `plugin_cli.py`).

A plug-in that raises while registering, names a major API version this core does not
speak, or is named in `REAM_PLUGINS` but not installed, stops start-up
(`PluginError`, naming it): a source or sink silently absent is worse than a pod
that does not start.

A sink a plug-in registers is wrapped: when `deliver` raises, the failure is logged with the
plug-in and interface and counted per plug-in (`failure_counts()`; also under
`plugin_failures` in `sinks.status()`), and then raised on, so the outbox retries and an
after-commit failure is dropped by the machinery that already does both. A timeout is
not counted there: the outbox gives up waiting while the plug-in's `deliver` is still
running, so the wrapper sees no failure; the outbox row's `last_error` says
`TimeoutError`, and the circuit breaker counts it.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from importlib import metadata

from ream import (
    decisions,
    hosts,
    mail,
    plugin_cli,
    scanners,
    settings,
    sinks,
    store,
    textvars,
)

# Shared with the command-line side, which cannot import this module (plugin_cli.py).
API_VERSION = plugin_cli.API_VERSION
GROUP = plugin_cli.GROUP
OLD_GROUP = plugin_cli.OLD_GROUP
PluginError = plugin_cli.PluginError

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_failures: dict[tuple[str, str], int] = {}
_loaded: list[str] = []
# Inbound routes plug-ins mounted, by normalised host: (plug-in, guarded ASGI app).
_ROUTERS: dict[str, tuple[str, Callable]] = {}


def failure_counts() -> dict[str, int]:
    """Failures caught since the process started, keyed `<plug-in>:<interface>`."""
    with _lock:
        return {f"{p}:{i}": n for (p, i), n in sorted(_failures.items())}


def loaded() -> list[str]:
    """The plug-ins `load` registered, in the order it called them."""
    return list(_loaded)


def count_failure(plugin: str, interface: str) -> None:
    """Count one caught failure of `plugin`'s `interface` (see `failure_counts`)."""
    _count(plugin, interface)


def _count(plugin: str, interface: str) -> None:
    with _lock:
        _failures[(plugin, interface)] = _failures.get((plugin, interface), 0) + 1


class PluginAPI:
    """What `register` is handed. One per plug-in, so a registration knows whose it is."""

    version = API_VERSION

    def __init__(self, plugin: str) -> None:
        self._plugin = plugin
        # The answer sources this plug-in registered: `on_answer` needs one, and
        # runs only for answers stored with one of them.
        self._sources: set[str] = set()

    def sink(
        self,
        name: str,
        kinds: tuple[str, ...] | list[str],
        deliver: Callable,
        delivery: str = "outbox",
        *,
        label: str | None = None,
    ) -> None:
        """Register an event sink (4.2, 1). `deliver(event)` returns on success and
        raises to retry (outbox) or be logged (after commit)."""
        plugin = self._plugin

        def guarded(event):
            try:
                deliver(event)
            except Exception as exc:
                _count(plugin, "sink")
                _log.warning(
                    "plug-in %s: sink %s failed: %s", plugin, name, type(exc).__name__
                )
                raise

        sinks.register(
            sinks.Sink(
                name, tuple(kinds), deliver=guarded, delivery=delivery, label=label
            )
        )

    def answer_source(
        self,
        name: str,
        label: str,
        *,
        relay: bool = False,
        verified_label: str | None = None,
        route_label: str | None = None,
    ) -> None:
        """Declare a stored `answer_source` value and its wording (4.2, 2)."""
        self._sources.add(name)
        decisions.register_source(
            name,
            label,
            relay=relay,
            verified_label=verified_label,
            route_label=route_label,
        )

    def on_answer(self, name: str, fn: Callable) -> None:
        """Register an answer hook (4.2, 3). `fn(conn, decision, answer)` runs inside the
        answer's transaction, after the row is written and before the commit, for answers
        stored with a source THIS plug-in registered (`answer_source`, called first: a
        plug-in with none is refused, so the hook stays with the plug-in that vouches
        for its own inbound answers). It is for answers the person gave through the
        plug-in's own inbound path (`decisions.answer`), NOT for relays: an agent's
        `record_elsewhere` never runs it. `answer` is a `decisions.Answer`. A raise aborts
        the whole answer (`decisions.AnswerHookFailed`, naming the plug-in)."""
        if not self._sources:
            raise ValueError(
                f"plug-in {self._plugin!r}: on_answer needs an answer_source"
                " registered first"
            )
        # The live set, so a source registered after the hook counts too.
        decisions.register_answer_hook(self._plugin, name, fn, self._sources)

    def scanner(self, name: str, scan: Callable, on_error: str = "skip") -> None:
        """Register a scanner (4.2, 4). `scan(text) -> list[scan.Finding]` is run where
        the core takes its second opinion (after a clean pattern scan: bodies, section
        notes, agent comments' second scan). `on_error="skip"`: a raise is logged and
        counted and the text is judged on the other scanners; `"block"`: it fails the
        action (`scanners.ScannerBlocked`). Under `block` an agent comment not yet
        cleared is hidden (no event), so replies to it fail as for a withdrawn comment
        until a clean scan shows it again. Each call is bounded by
        `scanners.SCAN_TIMEOUT_SECONDS`; a scanner that hangs costs one daemon thread
        per call, never reclaimed."""
        scanners.register(self._plugin, name, scan, on_error)

    def mail_backend(self, name: str, send: Callable, enabled: Callable) -> None:
        """Register the mail backend (4.2, 5). `send(to, subject, text)` raises to fail;
        `enabled()` says whether mail is on. One backend at most; it replaces the
        built-in Mailgun code. `name` is only for the log."""
        mail.register_backend(self._plugin, send, enabled)

    def text_vars(self, values: dict) -> None:
        """Add `{{name}}` template variables (4.2, text). Each name must start with this
        plug-in's own prefix (its name, lower-case, `_` for anything but letters and
        digits, then `_`: plug-in `beads-page` -> `beads_page_...`), so a plug-in can never
        take a core name or another's. A value is a string or a function of no arguments
        read each time a text is served; one that raises or returns a non-string renders
        as nothing and is counted as `<plug-in>:text`. Core served texts name none of
        them: they are for a plug-in's own `text_section` words and templates."""
        for name, value in dict(values).items():
            textvars.register_var(self._plugin, name, value)

    def text_section(self, slot: str, fn: Callable[[str], str]) -> None:
        """Add words to a named place in the served documents (4.2, text). `slot` is one of
        `textvars.SLOTS`: `proxy` and `tools` (after the tools paragraph and the proxy note
        in `agents.txt` only) and `integrations` (the end of `agents.txt` and of `skill.md`).
        `fn(doc)` gets `"agents.txt"` or `"skill.md"` and returns the paragraph for it, or
        "" for none. Several plug-ins' sections follow in registration order, each its own
        paragraph. One section per slot per plug-in."""
        textvars.register_section(self._plugin, slot, fn)

    def router(self, router, host: str) -> None:
        """Mount an inbound route on a dedicated host (4.2, routes). `router` is a FastAPI
        `APIRouter`; it is served only for requests whose Host is exactly `host`, never on
        the gallery, API, partner or content hosts, which a plug-in may not take (nor one
        another's): such a registration stops start-up. The route has no gallery or key
        authentication: it checks its own caller. A fault inside it answers a bare 500,
        counted as `<plug-in>:router` and logged by exception type only. Every response
        carries `x-content-type-options: nosniff`. The core applies NO body-size cap and no
        authentication to the route: it must bound what it reads (as the partner inbound
        route does with MAX_BODY) and check its caller itself. The host must be a plain
        dotted name (a non-ASCII one is IDNA-encoded first); anything else stops start-up.
        It may be a sibling of the gallery host: core cookies are host-only and gallery
        writes need the same origin, but a plug-in must not set `Domain=` cookies on the
        parent domain."""
        from fastapi import APIRouter, FastAPI
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse

        if not isinstance(router, APIRouter):
            raise TypeError(
                f"plug-in {self._plugin!r}: router needs a FastAPI APIRouter"
            )
        name = hosts.normalise(host) if isinstance(host, str) else None
        if name is None or not name.strip("."):
            raise ValueError(f"plug-in {self._plugin!r}: router needs a host")
        if not _HOST_NAME.fullmatch(name):
            try:
                name = name.encode("idna").decode("ascii")
            except UnicodeError:
                name = ""
            if not _HOST_NAME.fullmatch(name):
                raise ValueError(
                    f"plug-in {self._plugin!r}: router host is not a plain host name"
                )
        why = _reserved_host(name)
        if why is not None:
            raise ValueError(f"plug-in {self._plugin!r}: host {name!r} is {why}")
        if name in _ROUTERS:
            raise ValueError(
                f"plug-in {self._plugin!r}: host {name!r} is taken by {_ROUTERS[name][0]!r}"
            )
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.exception_handler(RequestValidationError)
        async def _no_echo(_request, exc):
            # Where a field went wrong, never what was sent (as the core's own apps do).
            errors = [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
            return JSONResponse(status_code=422, content={"detail": errors})

        app.include_router(router)
        _ROUTERS[name] = (self._plugin, _guard_app(self._plugin, app))

    def cli(self, name: str, main: Callable, help: str) -> None:
        """Register `ream <name>` (4.2, 6). `main()` is called as a console script is: it
        reads `sys.argv` (argv[0] is `ream <name>`) and returns the exit status. `help` is
        one printable line for `ream --help`. A verb the core owns, or one already taken, is
        refused (start-up stops). Used by the command-line process; the server only checks
        the registration."""
        plugin_cli.register_cli(self._plugin, name, main, help)

    def client_defaults(
        self,
        api_url: str | None = None,
        session_envs: list[str] | tuple[str, ...] = (),
        news: list[tuple[str, str]] | tuple[tuple[str, str], ...] = (),
    ) -> None:
        """The command-line tools' defaults (4.2, 6): `api_url` is the endpoint when
        `REAM_API` is unset (one plug-in at most); `session_envs` are environment variables
        holding the office part of the default session label, asked before the core's own
        `QD_OFFICE` and seat; `news` is `(YYYY-MM-DD[a-z], text)` lines printed once per
        machine with the core's. Used by the command-line process; the server only checks
        the registration."""
        plugin_cli.register_client_defaults(self._plugin, api_url, session_envs, news)


_HOST_NAME = re.compile(
    r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+"
)


def routers() -> dict[str, Callable]:
    """The ASGI app each plug-in route is served by, keyed by its normalised host. `create_app`
    reads this when it builds the host router; empty with no plug-in."""
    return {host: app for host, (_p, app) in _ROUTERS.items()}


def _reserved_host(host: str) -> str | None:
    """Why a plug-in may not serve `host`, or None. Read at call time, from the settings."""
    if hosts.is_gallery(host):
        return "the gallery host"
    if hosts.is_api(host):
        return "the API host"
    if host == hosts.normalise(settings.current().partner_inbound_host):
        return "the partner inbound host"
    content = store.content_host()
    if host == content or host.endswith("." + content):
        return "the content domain"
    return None


def _guard_app(plugin: str, app: Callable) -> Callable:
    """Wrap a plug-in's ASGI app so a fault in it answers a bare 500 and is counted and
    logged by exception type only (the message may hold a request's content), instead of
    reaching the server's traceback log."""
    import anyio
    from starlette.requests import ClientDisconnect
    from starlette.responses import PlainTextResponse

    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                # No plug-in websockets: refuse the handshake cleanly.
                await receive()
                await send({"type": "websocket.close", "code": 1008})
            else:
                await app(scope, receive, send)
            return
        started = False
        disconnected = False

        async def tracking(message):
            nonlocal started, disconnected
            if message["type"] == "http.response.start":
                started = True
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"x-content-type-options"
                ]
                headers.append((b"x-content-type-options", b"nosniff"))
                message = {**message, "headers": headers}
            try:
                await send(message)
            except OSError:
                disconnected = True
                raise

        try:
            await app(scope, receive, tracking)
        except Exception as exc:  # noqa: BLE001 - a plug-in's fault is isolated
            if disconnected or isinstance(
                exc,
                (
                    ClientDisconnect,
                    anyio.ClosedResourceError,
                    anyio.BrokenResourceError,
                ),
            ):
                return  # the client went away: not the plug-in's fault
            _count(plugin, "router")
            _log.warning("plug-in %s: route failed: %s", plugin, type(exc).__name__)
            if not started:
                await PlainTextResponse(
                    "plug-in error",
                    status_code=500,
                    headers={"x-content-type-options": "nosniff"},
                )(scope, receive, send)

    return guarded


def _entry_points() -> list:
    """The installed `ream.plugins` and (for good) `artifacts.plugins` entry points. Where one
    name is in both groups the `ream.plugins` one wins. Tests replace this."""
    found = {ep.name: ep for ep in metadata.entry_points(group=OLD_GROUP)}
    found.update({ep.name: ep for ep in metadata.entry_points(group=GROUP)})
    return list(found.values())


_requires_major = plugin_cli.requires_major


def load() -> list[str]:
    """Register every plug-in `REAM_PLUGINS` selects, once, in name order.
    Raises PluginError (naming the plug-in) and loads nothing further on a failure."""
    done = plugin_cli.register_all(
        _entry_points(), settings.current().plugins, PluginAPI, _log
    )
    _loaded[:] = done
    return done
