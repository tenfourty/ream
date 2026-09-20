"""The agent-facing publish API: mint a key with `artifacts.keys.mint`, then
POST/PATCH pages under `/api/v1/artifacts`. Every route is scoped to the
calling key's agent -- `caller` reads the agent from the verified key alone,
never from a header, query parameter or body field, so nothing else can
influence whose artifacts a request sees or changes.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from artifacts import comments, csp, decisions, events, grants, keys, store, templates
from artifacts.db import Database


class PublishBody(BaseModel):
    title: str
    body: str


class RecordBody(BaseModel):
    """An answer Jeremy gave somewhere that is not the page.

    `source` and `note` are both required and neither has a default. A default
    source would let a relay be recorded without anyone choosing what it was,
    and an empty note would throw away the part that usually decides things —
    every one of his notes so far has qualified the letter it came with.
    """

    letter: str
    note: str
    source: str
    said_at: str | None = None


class PatchBody(BaseModel):
    body: str
    expected_revision: str
    # Omitted entirely -> the page keeps the name it has. This is `None` rather
    # than `""` so that clearing a title and not touching it stay distinguishable:
    # an empty string is a rename to nothing, which the title guard refuses.
    title: str | None = None


class CommentBody(BaseModel):
    body: str
    quote: str = ""
    anchor: dict | None = None
    thread_root: str | None = None


class DecisionBody(BaseModel):
    question: str
    # `Any`, not `list[dict]`: a malformed shape (a string, a list of strings, ...)
    # must reach decisions._validate_options and come back as ITS one InvalidOptions
    # -- naming only the field "options" -- rather than being rejected first by
    # Pydantic's own validator, which would report a different error shape for "not a
    # list" than for "a list, but the second entry has a bad letter". One field, one
    # kind of failure, whichever way it fails.
    options: Any
    recommended: str | None = None


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
        # The verified key's public id, for GET /me. Stashed rather than
        # re-derived: the token itself must not be handled twice, and the id is
        # the handle `artifacts-admin list-keys` already prints.
        request.state.agent_key_id = key.id
        return key.agent

    @router.get("/openapi.json")
    def schema(request: Request, agent: str = Depends(caller)):
        """The machine-readable contract, behind the same key as the data.

        It lives here rather than at FastAPI's default /openapi.json for two
        reasons that pull in opposite directions. It must sit under /api/v1,
        because that is the only prefix Authelia bypasses for agent keys --
        outside it, asking for the schema returned a 302 to a login no key can
        complete. And a bypassed prefix is an unauthenticated prefix, so the
        route carries `caller` like everything else: the shape of this API is
        not something any device on the LAN should be able to ask for, and
        every consumer it exists for already holds a key.
        """
        return request.app.openapi()

    @router.get("/me")
    def whoami(request: Request, agent: str = Depends(caller)):
        """Which seat this key is, and what it may do.

        Exists because the answer is not obvious and getting it wrong is silent.
        Every agent on baba reads ~/.config/artifacts/key, which belongs to ONE
        seat, so a key holder's assumption about which seat it is can be wrong
        for weeks without anything saying so — and a seat grant is then applied
        to an identity nobody is actually using. Proposed by the Warpgate seat
        after exactly that cost an afternoon, and it is right: this turns a
        multi-step database dig into one line of output.

        Returns no secret. The key's own bytes are never echoed, and the id is
        the public handle `artifacts-admin list-keys` already prints.
        """
        return {
            "agent": agent,
            "key_id": getattr(request.state, "agent_key_id", None),
            "scopes": grants.for_agent(db, agent),
        }

    @router.get("/templates")
    def template_index(agent: str = Depends(caller)):
        """The templates an agent can start a page from, with what each is for.

        Behind the key like everything else. Not because a template is secret --
        it is checked into a repo -- but because an unauthenticated route on this
        prefix is a route any device on the LAN can reach, and every consumer
        this exists for already holds a key. Consistency here costs nothing and
        removes a thing to reason about.
        """
        return {"templates": templates.index()}

    @router.get("/templates/{name}", response_class=PlainTextResponse)
    def template_body(name: str, agent: str = Depends(caller)):
        """One template, as source.

        text/plain with nosniff, never text/html. The API origin must never
        render agent-adjacent markup: published pages get their own throwaway
        origin precisely so that a page cannot reach anything, and serving the
        same markup as a renderable document from here would quietly undo that.
        The caller is saving it to a file; text/plain is what it actually is.
        """
        try:
            text = templates.body(name)
        except templates.UnknownTemplate:
            raise HTTPException(404, "no template by that name") from None
        return PlainTextResponse(
            text,
            media_type="text/plain; charset=utf-8",
            headers={"x-content-type-options": "nosniff"},
        )

    @router.post("/artifacts", status_code=201)
    def create(payload: PublishBody, agent: str = Depends(caller)):
        try:
            published = store.publish(db, agent, payload.title, payload.body)
            return {**published.__dict__, "warnings": csp.as_json(payload.body)}
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
            published = store.patch(
                db,
                agent,
                artifact_id,
                payload.body,
                payload.expected_revision,
                title=payload.title,
            )
            return {**published.__dict__, "warnings": csp.as_json(payload.body)}
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

    @router.get("/artifacts/{artifact_id}/body")
    def read_body(artifact_id: str, agent: str = Depends(caller)):
        """The publisher reads back what it published.

        The rendered page is unreachable to its own author by design (framed
        requests with a capability token only), so this is the only self-check
        an agent has: it proves what is stored, not what a browser makes of
        it. `warnings` repeats the CSP advisory for the stored body, so a page
        published before that check existed still reports its own problems.
        """
        try:
            stored = store.get_body(db, agent, artifact_id)
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc
        return {**stored.__dict__, "warnings": csp.as_json(stored.body)}

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

    @router.post("/artifacts/{artifact_id}/decisions", status_code=201)
    def create_decision(
        artifact_id: str, payload: DecisionBody, agent: str = Depends(caller)
    ):
        try:
            return decisions.ask(
                db,
                agent=agent,
                artifact_id=artifact_id,
                question=payload.question,
                options=payload.options,
                recommended=payload.recommended,
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
        except decisions.InvalidOptions as exc:
            raise HTTPException(
                422, {"field": exc.field, "detail": "not a valid set of options"}
            ) from exc
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc

    @router.get("/artifacts/{artifact_id}/decisions")
    def list_decisions(
        artifact_id: str, include_answered: bool = False, agent: str = Depends(caller)
    ):
        try:
            store.get_owned(db, agent, artifact_id)
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc
        return [
            row.__dict__
            for row in decisions.list_for_artifact(
                db, artifact_id, include_answered=include_answered
            )
        ]

    @router.get("/decisions")
    def list_all_decisions(
        include_answered: bool = False,
        scope: str = Query("mine", pattern="^(mine|all)$"),
        agent: str = Depends(caller),
    ):
        # This is how an agent learns Jeremy's answer -- across every artifact it
        # owns: a plain poll of this route, with include_answered=true, works
        # everywhere a key does. `/events` (below) is the same fact told sooner,
        # for a session a nudge can actually reach.
        if scope == "all":
            # The operator's view, and the only way the service can answer "what
            # is waiting on Jeremy". A seat sees its own decisions and nothing
            # else, so before this the answer was assembled from what each agent
            # remembered to report -- and on 20 Sep 2026 a question sat unanswered
            # for seventeen hours because the one seat that could see it was not
            # looking.
            #
            # Refused, not silently narrowed, without the grant. A caller that
            # asked for every open question and got its own back would believe the
            # short list, which is the same lie as an empty feed from a broken
            # wait: it reads as "nothing is waiting".
            if not grants.has(db, agent, grants.READ_ALL_DECISIONS):
                # NAME THE SEAT. "this key may not read other seats" sent two
                # sessions looking for a missing grant on 20 Sep 2026; the grant
                # existed and was on a different seat, because the caller was
                # using the unsuffixed ~/.config/artifacts/key, which belongs to
                # baba-operator. Every agent on that box shares that file, so the
                # seat a key authenticates as is not the seat its holder assumes.
                # The message is the cheapest place to end that puzzle.
                raise HTTPException(
                    403,
                    f"this key authenticates as seat '{agent}', which does not "
                    "have decisions:read-all. Check GET /api/v1/me — you may be "
                    "using a different seat's key file than you intended.",
                )
            return [
                {
                    **row.decision.__dict__,
                    "agent": row.agent,
                    "artifact_title": row.artifact_title,
                    "age_seconds": row.age_seconds,
                }
                for row in decisions.open_across_seats(db)
            ]
        return [
            row.__dict__
            for row in decisions.list_for_agent(
                db, agent, include_answered=include_answered
            )
        ]

    @router.post("/decisions/{decision_id}/close")
    def close_decision(decision_id: str, agent: str = Depends(caller)):
        try:
            return decisions.close(db, agent, decision_id).__dict__
        except store.NotFound as exc:
            raise HTTPException(404, "no such decision") from exc

    # "Seen it" is a separate call from "done with it" on purpose: the alert that
    # catches a lost answer needs the first fact, and most answers here are acted on
    # over hours by work that happens somewhere else entirely.
    @router.post("/decisions/{decision_id}/record")
    def record_answer(
        decision_id: str, payload: RecordBody, agent: str = Depends(caller)
    ):
        """Record an answer Jeremy gave off the page, with provenance.

        homelab-ij8.78. The alternative a seat has without this is to press the
        button for him, which mints an answer on the page indistinguishable from
        one he tapped. Two seats refused to do that on 20 Sep 2026 and were
        right to; this exists so refusing does not mean the answer is lost.

        The recording seat is taken from the KEY, never from the body: the
        accountable party is not something a caller gets to nominate. `source`
        cannot be 'page' — that value means he tapped it himself.
        """
        said_at = None
        if payload.said_at:
            try:
                said_at = datetime.fromisoformat(payload.said_at)
            except ValueError:
                raise HTTPException(
                    422, "said_at must be an ISO 8601 timestamp"
                ) from None
        try:
            row = decisions.record_elsewhere(
                db,
                decision_id,
                letter=payload.letter,
                note=payload.note,
                source=payload.source,
                recorded_by=agent,
                said_at=said_at,
            )
        except decisions.InvalidSource:
            raise HTTPException(
                422,
                "source must be one of "
                f"{', '.join(decisions.ANSWER_SOURCES)} — 'page' is refused, "
                "because it means Jeremy answered on the page himself",
            ) from None
        except decisions.InvalidAnswer:
            raise HTTPException(422, "that letter is not one of the options") from None
        except store.NotFound:
            raise HTTPException(404, "no such open decision") from None
        return row.__dict__

    @router.post("/decisions/{decision_id}/acknowledge")
    def acknowledge_decision(decision_id: str, agent: str = Depends(caller)):
        try:
            return decisions.acknowledge(db, agent, decision_id).__dict__
        except store.NotFound as exc:
            raise HTTPException(404, "no such decision") from exc

    @router.get("/decisions/uncollected")
    def list_uncollected(
        stuck_after_seconds: int = Query(
            decisions.STUCK_AFTER_SECONDS, ge=0, le=86_400
        ),
        agent: str = Depends(caller),
    ):
        # Scoped to the caller's own seat, like every other read here. The
        # operator's cross-seat view -- collecting an answer on behalf of an agent
        # that is gone -- needs a key that does not exist yet, and inventing one
        # here would be a privilege nobody asked for.
        return [
            {
                "decision": row.decision.__dict__,
                "state": row.state,
                "last_read_at": row.last_read_at,
            }
            for row in decisions.uncollected(
                db, agent=agent, stuck_after_seconds=stuck_after_seconds
            )
        ]

    @router.get("/events")
    def list_events(
        since: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
        wait: float = Query(0.0, ge=0.0, le=300.0),
        agent: str = Depends(caller),
    ):
        # The mechanism a nudge is only ever an optimisation on top of (see
        # nudge.py's own docstring): an agent anywhere with a key -- not just one
        # a nudge can reach -- keeps its own cursor and polls this with
        # since=<last seq it saw> to learn everything that happened while it
        # was away, in a fixed order that can never skip or repeat a row.
        #
        # `limit` is the only page-size parameter this app exposes over HTTP
        # (/comments and /decisions simply do not offer one), so it is the only
        # one that needs a ceiling -- enforced HERE, by the route, rather than
        # in events.list_for_agent, so a bad value is a 422 naming the field
        # instead of a raw 500 out of Postgres. `ge=1` refuses the negative
        # that used to reach LIMIT unmodified.
        # `wait` is what turns a cursor into something an agent can be WOKEN by.
        # Jeremy, 19 Sep: "the agent that published the artifact needs to get the
        # nudge, not ops." A push needs an address for every agent everywhere,
        # which no agent outside this machine has; a held request needs none --
        # the agent already holds the connection, so answering it IS the nudge,
        # and it works identically for a session on baba and one on a laptop.
        #
        # Held in a worker thread (this route is `def`, not `async def`, so
        # FastAPI already runs it off the event loop) and never holding a
        # database connection while it sleeps: each look is its own short
        # connection, so a waiting agent costs a sleeping thread, not a pooled
        # connection someone else needs.
        # Before the look, not after: this request is listening from now until it
        # answers, and a 280-second hold that recorded its END time would make the
        # most attentive listener look like the most absent one. Never fails the
        # read (events.note_read swallows its own errors) -- the feed is the
        # feature, this is the safety net that watches over it.
        events.note_read(db, agent)
        rows = events.list_for_agent(db, agent, since=since, limit=limit)
        if rows or not wait:
            return [row.__dict__ for row in rows]
        return [
            row.__dict__
            for row in events.wait_for_agent(
                db, agent, since=since, limit=limit, seconds=wait
            )
        ]

    return router
