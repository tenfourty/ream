from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from artifacts import hosts
from artifacts.api import agent_router
from artifacts.content import create_content_app
from artifacts.db import Database
from artifacts.gallery import gallery_router


def create_gallery_app(db: Database | None = None) -> FastAPI:
    app = FastAPI(title="artifacts gallery", docs_url=None, redoc_url=None)

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
