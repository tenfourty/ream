"""The internal port: an optional second listener that serves only `/healthz` and the
partner inbound endpoint.

Why it exists: port 8000 picks the app by the Host header alone (`app.create_app`), and
only the ingress guards the gallery. Anything inside the cluster that can reach 8000 can
send the gallery's Host and be served as the owner. `REAM_INTERNAL_PORT` gives in-cluster
callers (the kubelet's probes, the partner's server) a port that cannot reach the gallery
at all, so the network policy can keep everything else off 8000.

How a request is kept off the gallery: the internal listener has its OWN ASGI app object,
built here, that holds no reference to the gallery, API, content or MCP apps. It is not a
port check inside the shared router, which would be one forgotten branch from serving the
gallery; there is nothing here to forget. Which listener a request came in on is decided
by which socket the server accepted it on, so no header or client-supplied value takes
part. What it answers:

* `GET /healthz` on any Host (probes carry the pod's address): the same body as the
  gallery's, a static `{"status": "ok"}`;
* the partner inbound endpoint (`partner_in.PATH` only, exact match), on exactly the Host(s) the router serves it on, through
  the same `app.create_partner_dispatch` code (proxy-header refusal, body cap, plug-in
  route and all), so the two ports cannot drift;
* everything else, whatever the Host: a plain 404. A WebSocket is closed.

How it is served: `InternalListener` binds the socket in the factory (so a port already
taken stops start-up with a clear message, never a half-started pod) and runs a second
`uvicorn.Server` as a task on the MAIN event loop, started when the main app's lifespan
startup has completed and stopped, awaited, when its shutdown runs (`serve._arm_shutdown_signals`).
One loop on purpose: a probe of `/healthz` here must be able to fail when the main server's
loop is wedged, and the handler runs in the worker threads (as the old probe on 8000 did), so a
starved thread pool shows too. The server's `capture_signals` is a no-op, or uvicorn would take
SIGTERM and SIGINT from the main server. The main listener, started by the image's unchanged
CMD, is untouched.
"""

import asyncio
import contextlib
import logging
import socket

import uvicorn
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from ream import hosts, partner_in, settings
from ream.app import create_partner_dispatch
from ream.db import Database

log = logging.getLogger("ream.internal_port")


def create_internal_app(db: Database, partner_inbound=None):
    """The ASGI app of the internal listener. See the module docstring."""
    partner_hosts, serve_partner = create_partner_dispatch(db, partner_inbound)

    async def not_found(scope, receive, send):
        await PlainTextResponse(
            "not found", status_code=404, headers={"x-content-type-options": "nosniff"}
        )(scope, receive, send)

    async def app(scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            # Normally switched off for this server; answer it if one ever arrives.
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if kind != "http":
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        # An allow-list of exact paths. The path must be the literal one both decoded and
        # as sent (`raw_path`, which excludes the query), so an encoded or dotted variant
        # (`%2e%2e`, `/healthz/../x`, `//x`, a trailing `;`) is not a match.
        path = scope["path"]
        raw = scope.get("raw_path")
        if raw is not None and raw != path.encode("latin-1", "replace"):
            await not_found(scope, receive, send)
            return
        if path == "/healthz" and scope["method"] == "GET":
            # A sync handler in the worker threads, as `/healthz` on the main port is.
            body = await run_in_threadpool(lambda: {"status": "ok"})
            await JSONResponse(body)(scope, receive, send)
            return
        if path == partner_in.PATH:
            host = hosts.normalise(hosts.first_host_header(scope))
            if host is not None and host in partner_hosts():
                await serve_partner(scope, receive, send, host)
                return
        await not_found(scope, receive, send)

    return app


class _Server(uvicorn.Server):
    """A server that leaves the process's signal handlers to the main one."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


class InternalListener:
    """A second uvicorn server on its own socket, run as a task on the running loop."""

    def __init__(self, app, host: str, port: int) -> None:
        self._sock = socket.socket(
            socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM
        )
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.bind((host, port))
        except OSError as exc:
            self._sock.close()
            raise SystemExit(
                f"REAM_INTERNAL_PORT: cannot listen on {host}:{port} ({exc.strerror})"
            ) from None
        config = uvicorn.Config(
            app,
            lifespan="off",
            access_log=False,
            proxy_headers=False,
            log_config=None,
            timeout_graceful_shutdown=15,
        )
        self._server = _Server(config)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Serve, on the running loop; returns once it accepts connections."""
        self._task = asyncio.create_task(self._server.serve(sockets=[self._sock]))
        while not self._server.started and not self._task.done():
            await asyncio.sleep(0.01)
        if self._task.done():
            self._task.result()  # raises what stopped it
            raise RuntimeError("the internal listener stopped at start-up")
        log.info("internal port: serving /healthz and the partner endpoint only")

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await self._task
        self._sock.close()


def maybe_create(db: Database, partner_inbound, configured: settings.Settings):
    """Bind the internal listener when `REAM_INTERNAL_PORT` is set and return it (not yet
    serving: `start` runs it); None when unset, and then no socket is opened."""
    port = configured.internal_port_number()
    if port is None:
        return None
    return InternalListener(
        create_internal_app(db, partner_inbound), configured.bind_host, port
    )
