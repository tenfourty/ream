import logging
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from artifacts import hosts
from artifacts.api import agent_router
from artifacts.content import create_content_app
from artifacts.db import Database
from artifacts.gallery import gallery_router


def create_gallery_app(db: Database | None = None) -> FastAPI:
    # openapi_url=None turns OFF FastAPI's own unauthenticated schema route.
    # The schema is served instead by the agent router at
    # /api/v1/openapi.json, BEHIND the same agent key as every other route
    # there (see artifacts.api.agent_router).
    #
    # It has to live under /api/v1 because that prefix is the only path
    # Authelia bypasses for agent keys -- the default /openapi.json answered
    # 302 to a login no key can complete, which is worse than a 404 while
    # debugging: a 404 says "not here", a redirect reads as one step short of
    # working, over and over. But a bypassed path is an UNAUTHENTICATED path,
    # so serving it from the router rather than from FastAPI is what keeps
    # "what does this API look like" from being answerable by any device on
    # the LAN. Every consumer the schema exists for already holds a key.
    #
    # docs_url/redoc_url stay off: the machine-readable contract is the point,
    # and an interactive UI would be a second surface with no reader.
    app = FastAPI(
        title="artifacts gallery",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """FastAPI's default handler echoes the rejected value back in `input`
        -- on a missing field, the *whole* request body -- which puts whatever
        an agent sent, secret or not, straight into that agent's own response
        and its transcript. This one reports only where a field went wrong,
        never what was sent: no `input`, no `ctx`, no request body.
        """
        errors = [
            {"loc": list(error["loc"]), "type": error["type"]} for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.middleware("http")
    async def _log_gallery_requests(request: Request, call_next):
        """One line per GALLERY request. The content origin is never logged.

        WHY THIS IS NARROW AND NOT `--access-log`. uvicorn runs with
        `--no-access-log` for a real reason: a content-page URL carries its read
        capability token in the PATH, so a blanket access log would write those
        tokens to disk. But the gallery's paths carry no such thing -- `/a/<id>`,
        `/d/<id>/answer`, `/api/v1/...` -- and turning logging off for the whole
        process to protect the other app left this one unobservable.

        That cost showed up on 20 Sep 2026: Jeremy reported that answering a
        question did not work on his phone, and there was no way to tell whether
        his request had even arrived, let alone what it answered. Two sessions
        spent half an hour on competing theories that a single log line would
        have settled. A service whose failures cannot be seen is a service whose
        failures get argued about.

        What is deliberately NOT here: no query string (an agent key must never
        appear in one, and `caller` already refuses a request that puts it
        there -- but a log is the wrong place to find out), no headers beyond
        the two below, no bodies, and no user agent string in full.

        `sec-fetch-site` IS logged, as its bare value, because the form guard
        turns on it and a refusal is otherwise indistinguishable from a request
        that never came. It is a fixed enum, not user data.
        """
        started = time.monotonic()
        response = await call_next(request)
        if not request.url.path.startswith("/healthz"):
            logging.getLogger("artifacts.gallery.access").info(
                "%s %s -> %d in %dms sec-fetch-site=%s",
                request.method,
                request.url.path,
                response.status_code,
                int((time.monotonic() - started) * 1000),
                request.headers.get("sec-fetch-site", "-"),
            )
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if db is not None:
        app.include_router(agent_router(db))
        app.include_router(gallery_router(db))
    return app


def create_app(db: Database):
    """Fail CLOSED, not open: the content hostname is never behind Authelia (it is
    agent-published HTML), and the gallery app trusts Authelia at ITS ingress instead of
    checking identity itself. A request that reaches the gallery app having entered
    through the content ingress therefore reaches it with no identity check at all -- so
    anything that is not exactly the gallery host, and not exactly a well-formed artifact
    host, gets a 404 from the router itself, never a guess at which app it "probably"
    meant. `hosts.first_host_header` is reused rather than re-read here, so the router
    and the content app can never disagree about what the Host header was.
    """
    gallery, content = create_gallery_app(db), create_content_app(db)

    async def router(scope, receive, send):
        if scope["type"] == "lifespan":
            await gallery(scope, receive, send)
            return
        host = hosts.normalise(hosts.first_host_header(scope))
        if hosts.is_gallery(host):
            chosen = gallery
        elif hosts.artifact_id(host) is not None:
            chosen = content
        else:
            response = PlainTextResponse(
                "not found", status_code=404, headers={"x-content-type-options": "nosniff"}
            )
            await response(scope, receive, send)
            return
        await chosen(scope, receive, send)

    return router
