from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from artifacts.api import agent_router
from artifacts.content import create_content_app
from artifacts.db import Database
from artifacts.store import content_domain


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
    return app


def create_app(db: Database):
    gallery, content = create_gallery_app(db), create_content_app(db)

    async def router(scope, receive, send):
        headers = dict(scope.get("headers") or [])
        host = headers.get(b"host", b"").decode().split(":")[0]
        chosen = content if host.endswith("." + content_domain()) else gallery
        await chosen(scope, receive, send)

    return router
