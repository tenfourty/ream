from fastapi import FastAPI

from artifacts.api import agent_router
from artifacts.db import Database


def create_gallery_app(db: Database | None = None) -> FastAPI:
    app = FastAPI(title="artifacts gallery", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if db is not None:
        app.include_router(agent_router(db))
    return app
