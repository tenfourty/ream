"""The agent-facing publish API: mint a key with `artifacts.keys.mint`, then
POST/PATCH pages under `/api/v1/artifacts`. Every route is scoped to the
calling key's agent -- `caller` reads the agent from the verified key alone,
never from a header, query parameter or body field, so nothing else can
influence whose artifacts a request sees or changes.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from artifacts import comments, keys, store
from artifacts.db import Database


class PublishBody(BaseModel):
    title: str
    body: str


class PatchBody(BaseModel):
    body: str
    expected_revision: str


class CommentBody(BaseModel):
    body: str
    quote: str = ""
    anchor: dict | None = None
    thread_root: str | None = None


# A scanner exception means the scanner is broken, not that the caller's
# content is -- 422 would tell a well-behaved agent to rewrite and resubmit
# a page that was never at fault, forever, while the actual defect goes
# unfixed. 503 says plainly that the fault is ours and nothing published;
# the field name is safe to include (it says which field, not what was in
# it), the content never is.
_SCAN_FAILED_DETAIL = (
    "the secret scanner failed -- this is a fault on our side, not yours. "
    "Nothing was published. Do not rewrite and resubmit this content; tell "
    "the operator."
)


def _refused_detail(field: str, findings) -> dict:
    """The scanner's findings, already de-duplicated and capped, as the
    structured 422 body: where a shape fired, and in which field, never what
    it matched."""
    return {
        "refused": [
            {"field": field, "shape": finding.shape, "line": finding.line}
            for finding in findings
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
            raise HTTPException(
                413,
                {
                    "field": exc.field,
                    "size": exc.size,
                    "detail": f"{exc.field} exceeds the size limit",
                },
            ) from exc
        except store.InvalidText as exc:
            raise HTTPException(
                422, {"field": exc.field, "detail": f"{exc.field} is not valid UTF-8"}
            ) from exc
        except store.SecretFound as found:
            raise HTTPException(
                422, _refused_detail(found.field, found.findings)
            ) from found
        except store.ScanFailed as exc:
            raise HTTPException(
                503, {"field": exc.field, "detail": _SCAN_FAILED_DETAIL}
            ) from exc

    @router.patch("/artifacts/{artifact_id}")
    def update(artifact_id: str, payload: PatchBody, agent: str = Depends(caller)):
        try:
            return store.patch(
                db, agent, artifact_id, payload.body, payload.expected_revision
            ).__dict__
        except store.BodyTooLarge as exc:
            raise HTTPException(
                413,
                {
                    "field": exc.field,
                    "size": exc.size,
                    "detail": f"{exc.field} exceeds the size limit",
                },
            ) from exc
        except store.InvalidText as exc:
            raise HTTPException(
                422, {"field": exc.field, "detail": f"{exc.field} is not valid UTF-8"}
            ) from exc
        except store.SecretFound as found:
            raise HTTPException(
                422, _refused_detail(found.field, found.findings)
            ) from found
        except store.ScanFailed as exc:
            raise HTTPException(
                503, {"field": exc.field, "detail": _SCAN_FAILED_DETAIL}
            ) from exc
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

    @router.get("/artifacts/{artifact_id}/comments")
    def list_comments(
        artifact_id: str, include_resolved: bool = False, agent: str = Depends(caller)
    ):
        try:
            store.get_owned(db, agent, artifact_id)
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc
        return [
            row.__dict__
            for row in comments.list_for_artifact(
                db, artifact_id, include_resolved=include_resolved
            )
        ]

    @router.post("/artifacts/{artifact_id}/comments", status_code=201)
    def create_comment(
        artifact_id: str, payload: CommentBody, agent: str = Depends(caller)
    ):
        try:
            return comments.add(
                db,
                artifact_id=artifact_id,
                author_kind="agent",
                author=agent,
                body=payload.body,
                anchor=payload.anchor,
                quote=payload.quote,
                thread_root=payload.thread_root,
                agent=agent,
            ).__dict__
        except store.BodyTooLarge as exc:
            raise HTTPException(
                413,
                {
                    "field": exc.field,
                    "size": exc.size,
                    "detail": f"{exc.field} exceeds the size limit",
                },
            ) from exc
        except store.InvalidText as exc:
            raise HTTPException(
                422, {"field": exc.field, "detail": f"{exc.field} is not valid UTF-8"}
            ) from exc
        except store.SecretFound as found:
            raise HTTPException(
                422, _refused_detail(found.field, found.findings)
            ) from found
        except store.ScanFailed as exc:
            raise HTTPException(
                503, {"field": exc.field, "detail": _SCAN_FAILED_DETAIL}
            ) from exc
        except comments.InvalidAnchor as exc:
            raise HTTPException(
                422, {"field": exc.field, "detail": "not a valid anchor"}
            ) from exc
        except comments.NoSuchThread as exc:
            raise HTTPException(
                422,
                {
                    "field": "thread_root",
                    "detail": "no open root comment with that id on this artifact",
                },
            ) from exc
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc

    @router.post("/comments/{comment_id}/resolve")
    def resolve_comment(comment_id: str, agent: str = Depends(caller)):
        try:
            return comments.resolve(db, comment_id, by=agent).__dict__
        except store.NotFound as exc:
            raise HTTPException(404, "no such comment") from exc

    @router.get("/comments")
    def list_all_comments(include_resolved: bool = False, agent: str = Depends(caller)):
        return [
            row.__dict__
            for row in comments.list_for_agent(
                db, agent, include_resolved=include_resolved
            )
        ]

    return router
