import logging
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from ream import (
    clientlevel,
    decisions,
    forms,
    hosts,
    onboarding,
    partner_in,
    plugin_api,
    ratelimit,
    settings,
)
from ream.api import agent_card_response, agent_router
from ream.content import create_content_app
from ream.db import Database
from ream.gallery import gallery_router
from ream.mcp import mcp_router
from ream.oauth import oauth_router
from ream.store import gallery_origin


def _refuse_bad_forms(app: FastAPI) -> None:
    """A page whose form cannot be stored is a 422 naming the form and the control,
    from whichever publish route met it (publish, update, section update, restore)."""

    @app.exception_handler(forms.FormRefused)
    async def _on_form_refused(_request: Request, exc: forms.FormRefused):
        return JSONResponse(status_code=422, content={"detail": exc.as_detail()})


def _refuse_failed_answer_hooks(app: FastAPI) -> None:
    """An `on_answer` hook raised, so the answer was rolled back (core-and-plugins design,
    4.3): 503 naming the plug-in, whichever route met it. No exception text, no value."""

    @app.exception_handler(decisions.AnswerHookFailed)
    async def _on_answer_hook_failed(
        _request: Request, exc: decisions.AnswerHookFailed
    ):
        return JSONResponse(
            status_code=503,
            content={"detail": decisions.answer_hook_failed_detail(exc.plugin)},
        )


def _scrub_validation_errors(app: FastAPI) -> None:
    """One 422 shape for every app. FastAPI's default handler echoes the rejected
    value back in `input` -- on a missing field, the *whole* request body -- which
    puts whatever an agent sent straight into its own response and transcript.
    This one reports only where a field went wrong, never what was sent: no
    `input`, no `ctx`, no request body."""

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {"loc": list(error["loc"]), "type": error["type"]} for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})


def create_gallery_app(
    db: Database | None = None, limiter: ratelimit.FailedAuthLimiter | None = None
) -> FastAPI:
    # openapi_url=None turns OFF FastAPI's own unauthenticated schema route.
    # The schema is served instead by the agent router at
    # /api/v1/openapi.json, BEHIND the same agent key as every other route
    # there (see ream.api.agent_router).
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
        title="Ream",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    _refuse_bad_forms(app)
    _scrub_validation_errors(app)
    _refuse_failed_answer_hooks(app)
    _say_what_tools_need(app)

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
            logging.getLogger("ream.gallery.access").info(
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
        limiter = limiter or ratelimit.FailedAuthLimiter()
        app.include_router(agent_router(db, limiter))
        app.include_router(mcp_router(db, limiter))
        app.include_router(oauth_router(db, limiter))
        app.include_router(gallery_router(db))
        _well_known(app)
    return app


def _well_known(app: FastAPI) -> None:
    """/.well-known/agent.json, the conventional place for a discovery file, on
    the root of a host (runcove-5t3oj.28). On the gallery's name it sits behind
    Authelia like everything outside /api/v1, so there an agent reads its twin,
    /api/v1/agent.json; on the API host both answer."""

    @app.get("/.well-known/agent.json")
    def well_known_agent_json(request: Request):
        return agent_card_response(request)


def create_api_app(
    db: Database | None = None, limiter: ratelimit.FailedAuthLimiter | None = None
) -> FastAPI:
    """The face an agent outside the house arrives at: the agent API, the
    onboarding document, the health check, and nothing else.

    NOT the gallery, and that is the whole point of it being a separate app
    rather than a path rule on an ingress. The gallery is Jeremy's — his wall,
    his answers, every seat's questions — and it is protected by Authelia
    annotations on the INTERNAL ingress. A public route to this service need not
    repeat those annotations, and if it does not, a gallery served on that name
    would be open to anyone who found it. Deciding it HERE means no ingress can
    get it wrong: on the API host the gallery routes do not exist.

    /agents.txt is the one unauthenticated document, and the URL Jeremy hands to
    an agent he wants onboarded (22 Sep 2026). Everything else here needs a key,
    exactly as it does on the gallery host.
    """
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    _refuse_bad_forms(app)
    _scrub_validation_errors(app)
    _refuse_failed_answer_hooks(app)
    _say_what_tools_need(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/agents.txt", response_class=PlainTextResponse)
    def agents_txt(request: Request) -> PlainTextResponse:
        # The address is taken from the request, never from configuration: an
        # onboarding document that names a host out of a config file is how a
        # new machine is sent at a name that only resolves inside the house.
        host = hosts.first_host_header(request.scope) or "artifacts.invalid"
        base = onboarding.base_url(
            request.url.scheme, host, request.headers.get("x-forwarded-proto")
        )
        return PlainTextResponse(
            onboarding.agents_txt(base),
            headers={
                "x-content-type-options": "nosniff",
                # Cacheable, because it is the same bytes for everyone and it is
                # the first thing a stranger fetches; short, because it is also
                # the document that tells them how to reach a moving service.
                "cache-control": "public, max-age=300",
            },
        )

    if db is not None:
        limiter = limiter or ratelimit.FailedAuthLimiter()
        app.include_router(agent_router(db, limiter))
        app.include_router(mcp_router(db, limiter))
        app.include_router(oauth_router(db, limiter))
        _well_known(app)
    return app


def _say_what_tools_need(app: FastAPI) -> None:
    """On every /api/v1 response: the oldest tool level wanted, and where tools are
    installed from (when set; a note only), and `Outdated: true` when the caller's own
    level (`X-Ream-Client`, or the old name; none is level 0) is older. The call is
    answered as usual either way. See clientlevel.py and selfupdate.py."""

    @app.middleware("http")
    async def _tools_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/"):
            current = settings.current()
            put = {clientlevel.MIN_HEADER: str(clientlevel.MIN_CLIENT_LEVEL)}
            if current.client_install_source:
                put[clientlevel.SOURCE_HEADER] = current.client_install_source
            sent = request.headers.get(
                clientlevel.CLIENT_HEADER
            ) or request.headers.get(clientlevel.old_name(clientlevel.CLIENT_HEADER))
            if clientlevel.level_of(sent) < clientlevel.MIN_CLIENT_LEVEL:
                put[clientlevel.OUTDATED_HEADER] = "true"
            for name, value in put.items():
                response.headers[name] = value
                response.headers[clientlevel.old_name(name)] = value
        return response


def _former_serves_api(scope) -> bool:
    path = scope.get("path") or ""
    return path == "/healthz" or path.startswith("/api/v1/")


def _forward_to_gallery(scope) -> RedirectResponse:
    """308 (the method is kept) to the same path and query on the gallery origin. The
    Location starts with the CONFIGURED origin and the path is re-encoded from the raw
    bytes, so nothing of the request's Host can reach it and a `//x.example` path stays
    a path on the gallery."""
    raw = (scope.get("raw_path") or scope.get("path", "/").encode()).decode("latin-1")
    raw = raw.replace("\\", "%5C")
    if not raw.startswith("/"):
        raw = "/" + raw
    query = (scope.get("query_string") or b"").decode("latin-1")
    location = gallery_origin() + raw + (f"?{query}" if query else "")
    return RedirectResponse(
        location,
        status_code=308,
        headers={
            "x-content-type-options": "nosniff",
            "cache-control": "public, max-age=3600",
        },
    )


def create_app(db: Database, partner_inbound: partner_in.Inbound | None = None):
    """Fail CLOSED, not open: the content hostname is never behind Authelia (it is
    agent-published HTML), and the gallery app trusts Authelia at ITS ingress instead of
    checking identity itself. A request that reaches the gallery app having entered
    through the content ingress therefore reaches it with no identity check at all -- so
    anything that is not exactly the gallery host, and not exactly a well-formed artifact
    host, gets a 404 from the router itself, never a guess at which app it "probably"
    meant. `hosts.first_host_header` is reused rather than re-read here, so the router
    and the content app can never disagree about what the Host header was.
    """
    # One failed-key count for both faces the agent API is served on, so a
    # caller cannot double its allowance by switching hostnames.
    limiter = ratelimit.FailedAuthLimiter()
    gallery, content = create_gallery_app(db, limiter), create_content_app(db)
    api = create_api_app(db, limiter)
    # The partner's endpoint, when the inbound bridge is configured: its own app on
    # its own host, which is the service's name INSIDE the cluster. Nothing else
    # serves it (see partner_in.create_partner_app).
    partner_app = (
        partner_in.create_partner_app(db, partner_inbound)
        if partner_inbound is not None
        else None
    )

    async def router(scope, receive, send):
        if scope["type"] == "lifespan":
            await gallery(scope, receive, send)
            return
        host = hosts.normalise(hosts.first_host_header(scope))
        # The GALLERY IS CHECKED FIRST and that order is load-bearing: if the
        # two names are ever configured the same by mistake, the request goes to
        # the app that sits behind Authelia, not the one that does not.
        if hosts.is_gallery(host):
            chosen = gallery
        elif hosts.is_api(host):
            chosen = api
        elif hosts.is_former(host):
            if _former_serves_api(scope):
                chosen = api
            else:
                await _forward_to_gallery(scope)(scope, receive, send)
                return
        elif partner_app is not None and host == partner_inbound.host:
            chosen = partner_app
        elif hosts.artifact_id(host) is not None:
            chosen = content
        elif host in (plugin_hosts := plugin_api.routers()):
            # A plug-in's inbound route on its own host (plugin_api.router); it may not
            # take any host above, and comes after them so it can never shadow one.
            chosen = plugin_hosts[host]
        else:
            response = PlainTextResponse(
                "not found",
                status_code=404,
                headers={"x-content-type-options": "nosniff"},
            )
            await response(scope, receive, send)
            return
        await chosen(scope, receive, send)

    return router
