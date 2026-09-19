"""The agent-facing publish API: mint a key with `artifacts.keys.mint`, then
POST/PATCH pages under `/api/v1/artifacts`. Every route is scoped to the
calling key's agent -- `caller` reads the agent from the verified key alone,
never from a header, query parameter or body field, so nothing else can
influence whose artifacts a request sees or changes.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from artifacts import keys, store
from artifacts.db import Database


class PublishBody(BaseModel):
    title: str
    body: str


class PatchBody(BaseModel):
    body: str
    expected_revision: str


def _refused_detail(findings) -> dict:
    """The scanner's findings, already de-duplicated and capped, as the
    structured 422 body: where a shape fired, never what it matched."""
    return {
        "refused": [
            {"shape": finding.shape, "line": finding.line} for finding in findings
        ]
    }


def agent_router(db: Database) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    def caller(request: Request) -> str:
        if "key" in request.query_params:
            raise HTTPException(401, "a key must not be sent in a query parameter")
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "an agent key is required")
        key = keys.verify(db, token.strip())
        if key is None:
            raise HTTPException(401, "unknown or revoked key")
        return key.agent

    @router.post("/artifacts", status_code=201)
    def create(payload: PublishBody, agent: str = Depends(caller)):
        try:
            return store.publish(db, agent, payload.title, payload.body).__dict__
        except store.BodyTooLarge as exc:
            raise HTTPException(413, "body exceeds the size limit") from exc
        except store.SecretFound as found:
            raise HTTPException(422, _refused_detail(found.findings)) from found

    @router.patch("/artifacts/{artifact_id}")
    def update(artifact_id: str, payload: PatchBody, agent: str = Depends(caller)):
        try:
            return store.patch(
                db, agent, artifact_id, payload.body, payload.expected_revision
            ).__dict__
        except store.BodyTooLarge as exc:
            raise HTTPException(413, "body exceeds the size limit") from exc
        except store.SecretFound as found:
            raise HTTPException(422, _refused_detail(found.findings)) from found
        except store.RevisionMismatch as exc:
            raise HTTPException(
                409, "the artifact moved on; read it and patch again"
            ) from exc
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc

    @router.get("/artifacts/{artifact_id}")
    def read(artifact_id: str, agent: str = Depends(caller)):
        try:
            return store.get_owned(db, agent, artifact_id).__dict__
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc

    @router.get("/artifacts")
    def index(agent: str = Depends(caller)):
        return [row.__dict__ for row in store.list_for_agent(db, agent)]

    return router
