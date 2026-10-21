"""The agent-facing publish API: mint a key with `artifacts.keys.mint`, then
POST/PATCH pages under `/api/v1/artifacts`. Every route is scoped to the
calling key's agent -- `caller` reads the agent from the verified key alone,
never from a header, query parameter or body field, so nothing else can
influence whose artifacts a request sees or changes.
"""

import base64
import html
import logging
import threading
from contextlib import nullcontext
from datetime import datetime
from functools import partial
from typing import Any, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)
from pydantic import BaseModel

from artifacts import (
    comments,
    connect,
    csp,
    decisions,
    events,
    grants,
    hosts,
    idempotency,
    keys,
    markdown,
    onboarding,
    ratelimit,
    review,
    store,
    templates,
    text,
    thumbs,
    traps,
)
from artifacts.db import Database

# Failed and refused authentications, one line each: the address and the count,
# never the key. Armed with its own handler in `serve` for the same reason the
# access log is -- an unarmed logger in this process prints nothing.
_auth_log = logging.getLogger("artifacts.auth")


# The longest a held /events request is kept when it arrived through Cloudflare
# (see list_events). Under nginx's 60 s and Cloudflare's ~100 s, with room.
PUBLIC_MAX_WAIT = 50.0


class TransferBody(BaseModel):
    to: str


class AgentsBody(BaseModel):
    """`can_read` is required: True opens the page to every agent in his space,
    False closes it to him and its publisher (homelab-ij8.105)."""

    can_read: bool


class ConnectBody(BaseModel):
    name: str | None = None


class PollBody(BaseModel):
    poll_token: str


class PreviewBody(BaseModel):
    body: str
    format: Literal["html", "markdown"] = "html"
    # A phone by default: the width Jeremy reads most pages at, and the one an
    # author on a desktop never sees.
    width: int = 390
    height: int = 1600


# One preview render at a time. Each is a whole headless browser; a burst from
# one confused agent must queue, not take the pod's memory with it.
_PREVIEW_SLOT = threading.Semaphore(1)
PREVIEW_WAIT_SECONDS = 30

_NO_MARKDOWN_TITLE = (
    "a markdown page needs a name: pass a title, or start the markdown with a "
    "`# heading`"
)


def _markdown_input(text: str) -> str:
    """Refuse markdown over the page size limit BEFORE rendering it. The store
    checks the rendered page too, but only after the work: a large input must
    not get a render for free. (Table padding is capped by markdown-it-py
    itself at 65,536 filled-in cells, so the output cannot run away either.)"""
    try:
        size = len(text.encode())
    except UnicodeEncodeError as exc:  # a lone surrogate from JSON's \ud800
        raise HTTPException(
            422, {"field": "body", "detail": "body is not valid UTF-8"}
        ) from exc
    if size > store.MAX_BODY_BYTES:
        raise HTTPException(
            413,
            {"field": "body", "size": size, "detail": "body exceeds the size limit"},
        )
    return text


def _markdown_page(text: str, title: str | None) -> tuple[str, str | None]:
    """A markdown body rendered to a page: (html, title or None)."""
    return markdown.render_page(_markdown_input(text), title)


class PublishBody(BaseModel):
    # Optional only for markdown, whose first `#` heading names the page when
    # no title is given; an HTML publish without one is refused as before.
    title: str | None = None
    body: str
    # "markdown": the service renders `body` into a page in the house look
    # (markdown.py, runcove-5t3oj.33). The default keeps every existing caller.
    format: Literal["html", "markdown"] = "html"


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
    # A review's round the verdict is for; a later round refuses it (409).
    round: int | None = None


class PatchBody(BaseModel):
    body: str
    expected_revision: str
    # Omitted entirely -> the page keeps the name it has. This is `None` rather
    # than `""` so that clearing a title and not touching it stay distinguishable:
    # an empty string is a rename to nothing, which the title guard refuses.
    title: str | None = None
    # A line for the page's history ("2 slices landed"), as a section update
    # carries one. Stored on the new version only; an unchanged body stores
    # nothing, because it makes no version.
    note: str | None = None
    format: Literal["html", "markdown"] = "html"


class SectionBody(BaseModel):
    """One operation on one element of the live page (6 Oct 2026)."""

    op: Literal["replace", "append", "prepend", "remove"]
    html: str | None = None
    # The page revision (hash) or version number the caller read; refused 409
    # if the page has moved on since.
    if_revision: str | None = None
    # The section revision GET returned; refused 409 if that element changed.
    if_section: str | None = None
    note: str | None = None
    # Off by default (every maintainer asked for that); true marks the tile on
    # his wall for his eye.
    notify: bool = False
    # "markdown": `html` is markdown, rendered to a fragment before it is placed.
    format: Literal["html", "markdown"] = "html"


class ArchiveBody(BaseModel):
    """`archived` is required and has no default.

    One route both ways rather than /archive and /unarchive, because the pair
    would drift: the day a third state appears, one of the two gets it. A
    required boolean also makes an empty body a 422 instead of silently meaning
    "archive it" -- a destructive-looking default nobody typed.
    """

    archived: bool


class RenameBody(BaseModel):
    title: str


class PinBody(BaseModel):
    pinned: bool


class DestroyBody(BaseModel):
    """`confirm` must be the word "destroy", spelled out.

    Not a boolean. A boolean is one character away from being sent by a client
    that meant the opposite, and this is the only call in the service that can
    lose something Jeremy said."""

    confirm: str


class OutcomeBody(BaseModel):
    """What a decision caused, or what changed under it. One field, because an
    outcome is a sentence somebody writes, not a form."""

    body: str


class ReplyBody(BaseModel):
    """A reply under the note he left when answering a decision."""

    body: str


class CommentBody(BaseModel):
    body: str
    quote: str = ""
    anchor: dict | None = None
    thread_root: str | None = None
    kind: str = "comment"


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


class ReviewBody(BaseModel):
    """A review is asked for by page and the four verdicts are fixed; the one
    choice is whether his marks are held until the verdict or live. Absent, it
    is his remembered preference."""

    mode: Literal["held", "live"] | None = None


class ClaimBody(BaseModel):
    """Which session is claiming the round. In the body, not only the header:
    the claim is ABOUT the session, so it is required here."""

    session: str


class ReleaseBody(BaseModel):
    session: str
    reason: str


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


def agent_router(
    db: Database, limiter: ratelimit.FailedAuthLimiter | None = None
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    # One limiter per app, SHARED when `create_app` mounts this router on both
    # the gallery host and the API host: otherwise switching names would double
    # a caller's budget. homelab-ij8.100; the reasoning is in `ratelimit`.
    limiter = limiter or ratelimit.FailedAuthLimiter()

    def _refuse(request: Request, address: str, detail: str) -> None:
        count, blocked, newly = limiter.failed(address)
        if blocked:
            wait = int(limiter.retry_after(address)) + 1
            if newly:
                _auth_log.warning(
                    "auth failed from %s on %s %s (%d in window) -- refusing "
                    "bad keys from this address for %ds",
                    address,
                    request.method,
                    request.url.path,
                    count,
                    wait,
                )
            # 429 and words that say it is a refusal: a client that prints this
            # where rows go must not read as "nothing there" (homelab-ij8.99).
            raise HTTPException(
                429,
                f"REFUSED: too many failed keys from this address. {detail}. "
                f"Stop and fix the key; a working key is still accepted. "
                f"Retry in {wait}s.",
                headers={"retry-after": str(wait)},
            )
        _auth_log.warning(
            "auth failed from %s on %s %s (%d in window)",
            address,
            request.method,
            request.url.path,
            count,
        )
        raise HTTPException(401, detail)

    def caller(request: Request) -> str:
        address = ratelimit.client_address(
            request.headers, request.client.host if request.client else None
        )
        if "key" in request.query_params:
            _refuse(request, address, "a key must not be sent in a query parameter")
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            _refuse(request, address, "an agent key is required")
        key = keys.verify(db, token.strip())
        if key is None:
            _refuse(request, address, keys.refusal_reason(db, token.strip()))
        # The verified key's public id, for GET /me. Stashed rather than
        # re-derived: the token itself must not be handled twice, and the id is
        # the handle `artifacts-admin list-keys` already prints.
        request.state.agent_key_id = key.id
        # For the machines page's "last used" (homelab-ij8.103).
        keys.touch(db, key.id)
        # A rotated key's first use kills the key it replaced (homelab-ij8.34).
        keys.first_use(db, key)
        request.state.agent_key = key
        return key.agent

    # How long a declared session label may be. Generous enough for the shape
    # the skill recommends -- "<seat>/<short id>" -- and short enough that it
    # cannot become a place to smuggle prose into a column meant for a name.
    SESSION_MAX = 120

    def session(request: Request) -> str | None:
        """Which SESSION is calling, as a label it declares about itself.

        homelab-ij8.32. Three seats on this machine share one key, so `agent`
        answers "which machine" and nothing answers "which of you". Jeremy,
        20 Sep: "a key per machine/host plus some sort of uuid for the agent
        sessions". This is that second part.

        A HEADER rather than a body field, deliberately: it applies to reads as
        well as writes, it needs no change to seven request models, and a
        client sets it once for every call it will ever make. A body field
        would have arrived on publish and nowhere else, which is how half a
        feature ships.

        NOT A CREDENTIAL AND NOT A PERMISSION. The caller declares it, so any
        key holder can send any value, and nothing here tries to verify it --
        there is nothing to verify against. It is a label for reading back
        ("which of us did this?") and for a caller narrowing its own feed.
        Anything that scopes access must keep using `agent`, which is derived
        from a token this service holds the hash of. A check built on this
        would be defeated by editing an environment variable, and a check that
        can be defeated that easily is worse than none, because it reads like
        one.

        Absent, blank or whitespace is None, never "" -- so "this caller did
        not say" has exactly one representation in the column, and a reader
        never has to decide whether an empty string means unknown or means a
        session that named itself nothing.
        """
        raw = (request.headers.get("x-artifacts-session") or "").strip()
        if not raw:
            return None
        if len(raw) > SESSION_MAX:
            raise HTTPException(
                422,
                f"x-artifacts-session must be at most {SESSION_MAX} characters",
            )
        # Sanitised like every other caller-supplied string that reaches
        # storage and then somebody's terminal: control characters gone,
        # whitespace collapsed. text.sanitize is idempotent, so a clean label
        # is unchanged.
        return text.sanitize(raw) or None

    def _warnings_for(artifact_id: str, body: str) -> list[dict]:
        """CSP advisories, plus every decision slot that names no decision here.

        One list because they are one kind of fact: something wrong with the
        page you just published that you cannot see, since the rendered page
        only answers a framed request holding a capability token no agent is
        given (homelab-ij8.35). Publish time is the one moment the author is
        definitely looking (homelab-ij8.50).
        """
        out = csp.as_json(body) + traps.as_json(body)
        for slot, line in decisions.unresolved_slots(db, artifact_id, body):
            out.append(
                {
                    "shape": "unresolved_decision_slot",
                    "line": line,
                    "hint": (
                        f"slot {slot!r} names no decision on this page. Readers "
                        "see a note saying the question does not exist, where "
                        "the control should be. Ask the decision first "
                        "(POST /artifacts/<id>/decisions), then publish the "
                        "slot with the id it returns."
                    ),
                }
            )
        return out

    @router.get("/agents.txt", response_class=PlainTextResponse)
    def onboarding_doc(request: Request):
        """The onboarding document, and the ONE route here that takes no key.

        Deliberately unauthenticated, and deliberately under /api/v1 as well as
        at /agents.txt on the public API host: /api/v1 is the only prefix
        Authelia bypasses, so this is the only way an agent on the LAN — which
        holds no Authelia session and never will — can read it at all. Asking
        for it anywhere else on the gallery's hostname answers a login page.

        It is safe to hand to anybody: no key, no token, no database read, no
        per-caller variation, no side effect. That is a property of the text
        (see onboarding.py), not of who is allowed to ask for it, which is why
        this route can afford to be open when its neighbour serving the schema
        cannot: the schema describes what a key can do here, this tells a
        stranger how to ask Jeremy for one.
        """
        return PlainTextResponse(
            onboarding.agents_txt(_base(request)),
            headers={
                "x-content-type-options": "nosniff",
                "cache-control": "public, max-age=300",
            },
        )

    def _base(request: Request) -> str:
        host = hosts.first_host_header(request.scope) or "artifacts.invalid"
        return onboarding.base_url(
            request.url.scheme, host, request.headers.get("x-forwarded-proto")
        )

    @router.get("/skill.md", response_class=PlainTextResponse)
    def skill(request: Request):
        """A skill file in the open SKILL.md format, for any agent (homelab-ij8.68,
        Jeremy 23 Sep: "a generic skill that could work for any agent"). Open for
        the same reason as agents.txt and carrying the same promises: no key, no
        database, the same text for everyone at a given address."""
        return PlainTextResponse(
            onboarding.skill_md(_base(request)),
            media_type="text/markdown",
            headers={
                "x-content-type-options": "nosniff",
                "cache-control": "public, max-age=300",
            },
        )

    @router.get("/tools/")
    def tools_index():
        """One link, to the one package: the page uv's `--find-links` reads, so
        an install line never changes between deploys (the homelab ops seat, 24
        Sep 2026: "a stable alias would make uv tool install --force a
        one-liner"). A redirect or a fixed filename cannot do this -- measured:
        uv refuses a URL without `.whl`, and a wheel filename without a version.
        Still never a directory listing: it names only what `wheel()` names."""
        built = onboarding.wheel()
        if built is None:
            raise HTTPException(404, "no tools are available from this address")
        name = html.escape(built.name)
        return HTMLResponse(
            f'<!doctype html><title>tools</title><a href="{name}">{name}</a>\n',
            headers={"x-content-type-options": "nosniff", "cache-control": "no-cache"},
        )

    @router.get("/tools/{name}")
    def tools(name: str):
        """The tool package, open: an agent on another machine installs it before
        it has any key (homelab-ij8.68). Only the one file the image built, by
        exact name -- never a path, never a listing. It contains no secret: it is
        this repository's code, which is also in the image."""
        built = onboarding.wheel()
        if built is None or name != built.name:
            raise HTTPException(404, "no such file")
        return FileResponse(
            built,
            media_type="application/zip",
            # no-cache, not max-age: the filename carries a fixed version, so a
            # cache must ask again every time (FileResponse sends an ETag) or an
            # agent's uvx would keep running yesterday's tools.
            headers={"x-content-type-options": "nosniff", "cache-control": "no-cache"},
        )

    @router.post("/connect", status_code=201)
    def connect_request(request: Request, body: ConnectBody):
        """A machine asks to join (homelab-ij8.102). Open, because a stranger has
        nothing yet; limited, because it is open. Returns a code for Jeremy to
        type and a poll token for the machine to collect its key with. Nothing
        here grants anything until he approves the code in the gallery."""
        address = ratelimit.client_address(
            request.headers, request.client.host if request.client else None
        )
        try:
            started = connect.request(db, body.name, address)
        except connect.Refused as refused:
            raise HTTPException(429, f"REFUSED: {refused}") from None
        return JSONResponse(
            {
                "code": started.code,
                "poll_token": started.poll_token,
                "expires_in": started.expires_in,
                "interval": 5,
                "approve_at": f"{_base(request)}/connect",
            },
            status_code=201,
            headers={"cache-control": "no-store"},
        )

    @router.post("/connect/poll")
    def connect_poll(body: PollBody):
        """The asking machine, waiting. `approved` carries the key, ONCE; every
        later poll says `delivered`. An unknown token says `expired`, exactly
        like a real one that ran out, so a token cannot be tested for being
        real."""
        return JSONResponse(
            connect.collect(db, body.poll_token), headers={"cache-control": "no-store"}
        )

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

    @router.post("/keys/rotate")
    def rotate_key(request: Request, agent: str = Depends(caller)):
        """Swap this machine's key for a new one (homelab-ij8.34, his (3a)). The
        tools call it weekly. The old key keeps working until the new one is
        first used, then dies. House seats' vaulted keys are not rotated here."""
        try:
            token, key_id = keys.rotate(db, request.state.agent_key)
        except keys.NotRotating:
            raise HTTPException(
                409, "this key is not a rotating key (a house seat's vaulted key)"
            ) from None
        return JSONResponse(
            {"key": token, "key_id": key_id, "agent": agent},
            headers={"cache-control": "no-store"},
        )

    @router.get("/me")
    def whoami(
        request: Request,
        agent: str = Depends(caller),
        seat_session: str | None = Depends(session),
    ):
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
        key = getattr(request.state, "agent_key", None)
        return {
            "agent": agent,
            "key_id": getattr(request.state, "agent_key_id", None),
            # Whether this key replaces itself, and from when (homelab-ij8.34).
            "rotating": bool(key and key.rotating),
            "rotate_after": (
                (key.created_at + keys.ROTATE_AFTER).isoformat()
                if key and key.rotating and key.created_at
                else None
            ),
            "scopes": grants.for_agent(db, agent),
            # ECHOED BACK, not looked up: this is what the caller just told us
            # it was, so a seat can check its own plumbing in one call. null
            # means "you are not declaring a session", which on this machine
            # means your writes are indistinguishable from the other two seats
            # sharing this key. That is exactly the question /me exists to
            # answer, one layer further in than the one it was built for.
            "session": seat_session,
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

    @router.post("/markdown")
    def render_markdown(payload: PublishBody, agent: str = Depends(caller)):
        """Render markdown to the page a markdown publish would store, without
        publishing it: for an agent that wants the HTML (to keep, to diff, or
        to finish by hand), and for the CLI's `--check` and `--matches` on a
        `.md` file. `title` names the page as on a publish."""
        body, title = _markdown_page(payload.body, payload.title)
        return {"html": body, "title": title}

    @router.post("/artifacts", status_code=201)
    def create(
        payload: PublishBody,
        agent: str = Depends(caller),
        seat_session: str | None = Depends(session),
    ):
        body, title = payload.body, payload.title
        if payload.format == "html" and title is None:
            # As before markdown: an HTML publish names its page.
            raise HTTPException(422, {"field": "title", "detail": "title is required"})
        if payload.format == "markdown":
            body, named = _markdown_page(payload.body, title)
            if not named:
                raise HTTPException(
                    422, {"field": "title", "detail": _NO_MARKDOWN_TITLE}
                )
            title = named
        try:
            published = store.publish(db, agent, title, body, session=seat_session)
            # After the write, never inside it: a publish must not wait on a
            # browser, nor fail because of one. See thumbs.render_in_background.
            thumbs.render_in_background(db, published.id, published.version, body)
            return {
                **published.__dict__,
                "warnings": _warnings_for(published.id, body),
            }
        except store.NotAPage as exc:
            # 422, not 413: the body is not too much, it is not a page. Named
            # like every other refusal so a caller can tell them apart without
            # reading prose (homelab-ij8.28 -- a builder script's "wrote
            # <path>" log line was published as a status page).
            raise HTTPException(
                422,
                {
                    "field": exc.field,
                    "detail": (
                        "body does not look like an HTML page: no doctype and "
                        "no HTML tag anywhere in it. If a command's output "
                        "reached this call instead of your file, that is the "
                        "usual cause."
                    ),
                },
            ) from exc
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
    def update(
        artifact_id: str,
        payload: PatchBody,
        agent: str = Depends(caller),
        seat_session: str | None = Depends(session),
    ):
        body = payload.body
        if payload.format == "markdown":
            # The title keeps its usual rule on update: omitted keeps the name
            # the page has. The masthead and <title> follow the markdown.
            body, _ = _markdown_page(payload.body, payload.title)
        try:
            published = store.patch(
                db,
                agent,
                artifact_id,
                body,
                payload.expected_revision,
                title=payload.title,
                session=seat_session,
                note=store.page_note(
                    payload.note, agent=agent, artifact_id=artifact_id
                ),
            )
            # After the write, never inside it: a publish must not wait on a
            # browser, nor fail because of one. See thumbs.render_in_background.
            thumbs.render_in_background(db, published.id, published.version, body)
            return {
                **published.__dict__,
                "warnings": _warnings_for(published.id, body),
            }
        except store.NotAPage as exc:
            # 422, not 413: the body is not too much, it is not a page. Named
            # like every other refusal so a caller can tell them apart without
            # reading prose (homelab-ij8.28 -- a builder script's "wrote
            # <path>" log line was published as a status page).
            raise HTTPException(
                422,
                {
                    "field": exc.field,
                    "detail": (
                        "body does not look like an HTML page: no doctype and "
                        "no HTML tag anywhere in it. If a command's output "
                        "reached this call instead of your file, that is the "
                        "usual cause."
                    ),
                },
            ) from exc
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

    def _section_refusal(exc: Exception) -> HTTPException:
        """A section update's refusals, as the whole-page update words them
        wherever the cause is the same."""
        from artifacts import sections

        if isinstance(exc, sections.SectionChanged):
            return HTTPException(409, "section changed, re-fetch")
        if isinstance(exc, sections.SectionError):
            return HTTPException(422, {"field": "section", "detail": str(exc)})
        if isinstance(exc, store.OpenDecisionDropped):
            return HTTPException(
                422,
                {
                    "field": "section",
                    "decision": exc.decision_id,
                    "detail": (
                        f"this would remove the slot of open question "
                        f"{exc.decision_id} ({exc.question!r}), which he has "
                        "not answered. Keep its data-artifact-decision slot, "
                        "or close the question first. Nothing was changed."
                    ),
                },
            )
        if isinstance(exc, store.PageChanged):
            return HTTPException(
                409, f"page changed since revision {exc.given}, re-fetch"
            )
        if isinstance(exc, store.RevisionMismatch):
            return HTTPException(409, "page changed, re-fetch")
        if isinstance(exc, store.NotAPage):
            return HTTPException(
                422,
                {
                    "field": exc.field,
                    "detail": "the resulting page does not look like an HTML page",
                },
            )
        if isinstance(exc, store.BodyTooLarge):
            return HTTPException(
                413,
                {
                    "field": exc.field,
                    "size": exc.size,
                    "detail": f"{exc.field} exceeds the size limit",
                },
            )
        if isinstance(exc, store.InvalidText):
            return HTTPException(
                422, {"field": exc.field, "detail": f"{exc.field} is not valid UTF-8"}
            )
        if isinstance(exc, store.SecretFound):
            return HTTPException(422, _refused_detail(exc.field, exc.findings))
        if isinstance(exc, store.ScanFailed):
            return HTTPException(
                503, {"field": exc.field, "detail": _SCAN_FAILED_DETAIL}
            )
        if isinstance(exc, store.NotFound):
            return HTTPException(404, "no such artifact")
        raise exc

    _SECTION_REFUSALS = (
        store.NotFound,
        store.RevisionMismatch,
        store.PageChanged,
        store.OpenDecisionDropped,
        store.NotAPage,
        store.BodyTooLarge,
        store.InvalidText,
        store.SecretFound,
        store.ScanFailed,
    )

    @router.get("/artifacts/{artifact_id}/sections/{element_id}")
    def read_section(artifact_id: str, element_id: str, agent: str = Depends(caller)):
        """One element of the live page, to edit what is live rather than a
        local copy. Any agent may read it when the page is open to agents, as
        it may read the whole page (10 Oct 2026); only the publisher may update it."""
        from artifacts import sections

        try:
            outer, rev, live = store.get_section(
                db, agent, artifact_id, element_id, as_reader=True
            )
        except (sections.SectionError, *_SECTION_REFUSALS) as exc:
            raise _section_refusal(exc) from exc
        return {
            "html": outer,
            "section_revision": rev,
            "page_revision": live.revision,
            "version": live.version,
        }

    @router.patch("/artifacts/{artifact_id}/sections/{element_id}")
    def update_section(
        artifact_id: str,
        element_id: str,
        payload: SectionBody,
        agent: str = Depends(caller),
        seat_session: str | None = Depends(session),
    ):
        """Apply one operation to one element of the LIVE page and store the
        whole result as a new version. Every check a whole-page update runs,
        runs on the whole resulting page."""
        from artifacts import sections

        fragment = payload.html
        if payload.format == "markdown" and fragment is not None:
            fragment = markdown.render_fragment(_markdown_input(fragment))
        try:
            result = store.patch_section(
                db,
                agent,
                artifact_id,
                element_id,
                payload.op,
                fragment,
                if_revision=payload.if_revision,
                if_section=payload.if_section,
                note=payload.note,
                notify=payload.notify,
                session=seat_session,
            )
        except (sections.SectionError, *_SECTION_REFUSALS) as exc:
            raise _section_refusal(exc) from exc
        published = result.published
        if not published.unchanged:
            thumbs.render_in_background(
                db, published.id, published.version, result.body
            )
        return {
            "page_revision": published.revision,
            "section_revision": result.section_revision,
            "version": published.version,
            "unchanged": published.unchanged,
            "section": result.label,
            "page_url": published.page_url,
            "bytes": published.bytes,
            "warnings": _warnings_for(published.id, result.body),
        }

    # DECLARED BEFORE /artifacts/{artifact_id}, and it has to be. FastAPI
    # matches in declaration order, so with the parameterised route first this
    # would resolve as artifact_id="deleted" and answer 404 -- a route that
    # exists, is tested in isolation, and is unreachable in the running app.
    @router.get("/artifacts/deleted")
    def deleted(agent: str = Depends(caller)):
        """This agent's own deleted pages, soonest to expire first."""
        return [row.__dict__ for row in store.list_deleted(db, agent)]

    @router.get("/artifacts/{artifact_id}")
    def read(artifact_id: str, agent: str = Depends(caller)):
        # Any agent may READ a page open to agents (homelab-ij8.105); a closed
        # page, and one that does not exist, still answer the same 404.
        try:
            return store.get_owned(db, agent, artifact_id, as_reader=True).__dict__
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
            stored = store.get_body(db, agent, artifact_id, as_reader=True)
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc
        return {
            **stored.__dict__,
            "warnings": _warnings_for(artifact_id, stored.body),
        }

    @router.post("/preview")
    def preview(payload: PreviewBody, agent: str = Depends(caller)):
        """Render a page that is NOT published and hand back what a reader would
        see: a screenshot at the given viewport and the page's script errors.

        The service's own origin rule means an agent can never open its own
        page (homelab-ij8.35), and every trap in `traps.py` exists because of
        that. Asked for by the seats on 24 Sep 2026 (homelab-ij8.107): look
        before publishing, at phone width. Nothing is stored and nothing is
        published; the same size limit as a publish applies. `rendered: false`
        means this deployment has no browser or the render failed -- never
        "clean"."""
        body = payload.body
        if payload.format == "markdown":
            body, _ = _markdown_page(payload.body, None)
        if len(body.encode()) > store.MAX_BODY_BYTES:
            raise HTTPException(
                413, f"body is larger than {store.MAX_BODY_BYTES} bytes"
            )
        try:
            store.check_body(body, agent=agent)
        except store.SecretFound as found:
            raise HTTPException(
                422, _refused_detail(found.field, found.findings)
            ) from found
        except store.ScanFailed as exc:
            raise HTTPException(
                503, {"field": exc.field, "detail": _SCAN_FAILED_DETAIL}
            ) from exc
        width = max(320, min(payload.width, 1600))
        height = max(320, min(payload.height, 4000))
        if not thumbs.available():
            return {"rendered": False, "renderer": False, "errors": None}
        if not _PREVIEW_SLOT.acquire(timeout=PREVIEW_WAIT_SECONDS):
            raise HTTPException(
                503, "another preview is rendering; try again in a few seconds"
            )
        try:
            png, errors = thumbs.render_report(body, width, height)
        finally:
            _PREVIEW_SLOT.release()
        return {
            "rendered": png is not None,
            "renderer": True,
            "width": width,
            "height": height,
            "errors": errors,
            "png_base64": base64.b64encode(png).decode() if png else None,
        }

    @router.get("/artifacts/{artifact_id}/render")
    def read_render(artifact_id: str, agent: str = Depends(caller)):
        """What the live version did when the service rendered it: its uncaught
        script errors, which its author cannot otherwise see (homelab-ij8.72).
        `rendered: false` means not finished, or no browser here -- never
        "clean"."""
        try:
            store.get_owned(db, agent, artifact_id, as_reader=True)
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc
        # `renderer`: whether this deployment renders at all, so a client never
        # waits for a render that cannot come.
        return {**store.render_report(db, artifact_id), "renderer": thumbs.available()}

    @router.get("/artifacts/{artifact_id}/versions")
    def read_versions(artifact_id: str, agent: str = Depends(caller)):
        """A page's history: what versions exist, when, and who wrote each.

        homelab-ij8.30. Publish returns a revision hash and the skill says a
        page keeps its history, but nothing listed one -- so the hash was a
        number the agent was told and could do nothing with.

        NEVER the bodies. A history is read to choose a version, and shipping
        every body would make this the largest response the service produces.
        `bytes` is what a reader actually compares.
        """
        try:
            return [
                row.__dict__
                for row in store.list_versions(db, agent, artifact_id, as_reader=True)
            ]
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc

    @router.get("/artifacts/{artifact_id}/versions/{n}")
    def read_version_body(artifact_id: str, n: int, agent: str = Depends(caller)):
        """One version's stored body (homelab-50ze).

        Rolling back is this plus an ordinary update: fetch the old bytes,
        publish them. No new verb, and the roll-back lands as a NEW version
        rather than rewriting history -- the only honest shape for a service
        whose argument is that the record is not edited.
        """
        try:
            stored = store.get_version_body(db, agent, artifact_id, n, as_reader=True)
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact or version") from exc
        return {
            **stored.__dict__,
            "warnings": _warnings_for(artifact_id, stored.body),
        }

    @router.get("/artifacts")
    def index(
        agent: str = Depends(caller),
        scope: str = Query("mine", pattern="^(mine|all)$"),
    ):
        """This agent's pages, or with `scope=all` every page it may read: its
        own plus every other agent's page open to agents (homelab-ij8.105).
        `mine` stays the default so no existing caller's listing changes."""
        rows = (
            store.list_readable(db, agent)
            if scope == "all"
            else store.list_for_agent(db, agent)
        )
        return [row.__dict__ for row in rows]

    @router.post("/artifacts/{artifact_id}/transfer")
    def transfer(artifact_id: str, payload: TransferBody, agent: str = Depends(caller)):
        """Give this agent's own page to another agent, same address
        (homelab-ij8.87). One-way: afterwards this key cannot see it to give it
        back, unless the page is open to agents -- and even then only to read."""
        try:
            old = store.transfer(db, artifact_id, payload.to.strip(), agent=agent)
        except store.NoSuchAgent:
            raise HTTPException(
                422,
                f"{payload.to!r} is not a connected machine with a working key. "
                "Use the agent's name as the page list shows it (homelab-ops); "
                "the vault's seat name (homelab_ops) is also accepted.",
            ) from None
        if old is None:
            raise HTTPException(404, "no such artifact")
        return {"id": artifact_id, "from": old, "to": store.owner(db, artifact_id)}

    @router.post("/artifacts/{artifact_id}/agents")
    def agents_can_read(
        artifact_id: str, payload: AgentsBody, agent: str = Depends(caller)
    ):
        """Open this agent's own page to every agent in his space, or close it to
        just him and this agent (homelab-ij8.105). The publisher's twin of his
        control in the gallery; nobody else's page can be changed from here."""
        if not store.set_agents_can_read(
            db, artifact_id, payload.can_read, agent=agent
        ):
            raise HTTPException(404, "no such artifact")
        return store.get_owned(db, agent, artifact_id).__dict__

    # --- the shelf: what Jeremy can do to a page in the gallery, an agent can
    # do to its own pages here. His rule, 20 Sep 2026: "ideally agents can take
    # all the same actions a human can." Every one of these has a twin in
    # gallery.py; the difference is only who is allowed to aim it. His controls
    # are unscoped because the wall is his, and are proved by the same-origin
    # form guard; these are scoped to the calling key's own agent and proved by
    # the key. Neither reaches the other's pages.
    #
    # None of them touch the body, so none of them make a version. Archiving,
    # renaming and pinning are all shelf state -- where a page sits and what it
    # is called -- and folding them into the version history would put entries
    # in it that say nothing about what the page said.

    @router.post("/artifacts/{artifact_id}/archive")
    def archive(artifact_id: str, payload: ArchiveBody, agent: str = Depends(caller)):
        """Take one of this agent's own pages off the wall, or put it back."""
        if not store.set_archived(db, artifact_id, payload.archived, agent=agent):
            raise HTTPException(404, "no such artifact")
        return store.get_owned(db, agent, artifact_id).__dict__

    @router.post("/artifacts/{artifact_id}/rename")
    def rename(artifact_id: str, payload: RenameBody, agent: str = Depends(caller)):
        """Rename without republishing -- the whole body is not needed to fix a
        name, and an agent that no longer holds the file should not have to."""
        try:
            renamed = store.rename(db, artifact_id, payload.title, agent=agent)
        except store.NotAPage as exc:
            # 422, not 413: the body is not too much, it is not a page. Named
            # like every other refusal so a caller can tell them apart without
            # reading prose (homelab-ij8.28 -- a builder script's "wrote
            # <path>" log line was published as a status page).
            raise HTTPException(
                422,
                {
                    "field": exc.field,
                    "detail": (
                        "body does not look like an HTML page: no doctype and "
                        "no HTML tag anywhere in it. If a command's output "
                        "reached this call instead of your file, that is the "
                        "usual cause."
                    ),
                },
            ) from exc
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
        if not renamed:
            raise HTTPException(404, "no such artifact")
        return store.get_owned(db, agent, artifact_id).__dict__

    @router.post("/artifacts/{artifact_id}/pin")
    def pin(artifact_id: str, payload: PinBody, agent: str = Depends(caller)):
        """Pin one of this agent's own pages to the front of the wall."""
        if not store.set_pinned(db, artifact_id, payload.pinned, agent=agent):
            raise HTTPException(404, "no such artifact")
        return store.get_owned(db, agent, artifact_id).__dict__

    @router.post("/artifacts/{artifact_id}/duplicate", status_code=201)
    def duplicate(artifact_id: str, agent: str = Depends(caller)):
        """Copy one of this agent's own pages to a new address.

        The body only -- no versions, no decisions, no answers. See
        store.duplicate: copying an answered decision would manufacture a
        record of Jeremy answering a question he has never seen.
        """
        try:
            published, body = store.duplicate(db, artifact_id, agent=agent)
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc
        thumbs.render_in_background(db, published.id, published.version, body)
        return published.__dict__

    @router.delete("/artifacts/{artifact_id}")
    def delete(artifact_id: str, agent: str = Depends(caller)):
        """Delete one of this agent's own pages. Recoverable for thirty days.

        Destroys nothing: the body, every version and every decision with his
        answers and his notes all survive the window. An agent deletes a page
        in error as readily as a human does, and it is usually the agent that
        notices -- within seconds, and while Jeremy is asleep -- which is why
        the recovery below is an agent-facing route and not only a button in
        his gallery.
        """
        if not store.set_deleted(db, artifact_id, True, agent=agent):
            raise HTTPException(404, "no such artifact")
        return {"id": artifact_id, "deleted": True}

    @router.post("/artifacts/{artifact_id}/restore")
    def restore(artifact_id: str, agent: str = Depends(caller)):
        """Put one of this agent's deleted pages back, address and history
        intact."""
        if not store.set_deleted(db, artifact_id, False, agent=agent):
            raise HTTPException(404, "no such artifact")
        return store.get_owned(db, agent, artifact_id).__dict__

    @router.post("/artifacts/{artifact_id}/destroy")
    def destroy(artifact_id: str, payload: DestroyBody, agent: str = Depends(caller)):
        """Remove a page and everything under it, for good. No window, no undo.

        Requires `confirm: "destroy"` spelled out. Separate from DELETE rather
        than a flag on it, because "tidy this away" and "this must not exist
        anywhere" are different intentions and an interface where the second is
        a parameter of the first is one where it happens by accident.
        """
        if payload.confirm.strip().lower() != "destroy":
            raise HTTPException(422, {"detail": 'confirm must be the word "destroy"'})
        if not store.destroy(db, artifact_id, agent=agent):
            raise HTTPException(404, "no such artifact")
        return {"id": artifact_id, "destroyed": True}

    @router.get("/artifacts/{artifact_id}/comments")
    def list_comments(
        artifact_id: str, include_resolved: bool = False, agent: str = Depends(caller)
    ):
        """Every comment thread on a page the caller may read. Threads under
        his decision notes (`anchor.kind == "decision"`) are included on
        purpose: they are comments, and `artifacts-decide` labels them."""
        # A page the caller may READ: its own, or another agent's that is open
        # to agents. A closed, deleted or unknown page answers the same 404.
        try:
            store.get_owned(db, agent, artifact_id, as_reader=True)
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc
        # Another office's page shows every comment, his included: access to
        # the page is the one control (Jeremy, 21 Oct 2026). A held mark stays
        # out, as it does for the owner.
        rows = comments.list_for_artifact(
            db, artifact_id, include_resolved=include_resolved
        )
        return [row.__dict__ for row in rows]

    @router.post("/artifacts/{artifact_id}/comments", status_code=201)
    def create_comment(
        artifact_id: str,
        payload: CommentBody,
        background: BackgroundTasks,
        agent: str = Depends(caller),
        idempotency_key: str | None = Header(default=None),
        seat_session: str | None = Depends(session),
    ):
        """Comment on a page the caller may read: its own, or another agent's
        that is open to agents. On another agent's page only a plain `comment`
        is allowed (`remove` and `looks_good` are Jeremy's), and a comment never
        answers, acknowledges or closes a decision. `anchor` may be
        `{"kind": "text", "quote": ..., "section": optional heading}`: the
        server finds the passage in the page's current version."""
        if (
            payload.thread_root is None
            and isinstance(payload.anchor, dict)
            and payload.anchor.get("kind") == "decision"
        ):
            raise HTTPException(
                422,
                {
                    "field": "anchor",
                    "detail": (
                        "a thread under his answer is started with "
                        "POST /api/v1/decisions/{decision_id}/replies"
                    ),
                },
            )
        # Run once the keyed transaction has committed; empty on a replay.
        later: list = []

        def write(conn) -> tuple[int, object]:
            later.clear()  # a retried attempt must not send the last one's nudge
            return 201, _add_comment(
                artifact_id, payload, background, agent, conn, seat_session, later
            )

        response = idempotency.run(
            db,
            agent,
            f"comments:{artifact_id}",
            idempotency_key,
            payload.model_dump(),
            write,
        )
        for send in later:
            background.add_task(send)
        return response

    def _text_anchor(artifact_id, payload, agent) -> tuple[dict | None, str]:
        """Turn an agent's `text` anchor into the stored shape: a page-level
        anchor carrying the quote, which the gallery shows as a quoted comment
        (an agent cannot compute the DOM path the browser's range anchor needs).
        Anything else passes through unchanged."""
        anchor = payload.anchor
        if (
            payload.thread_root is not None
            or not isinstance(anchor, dict)
            or anchor.get("kind") != "text"
        ):
            return anchor, payload.quote
        quote, section = anchor.get("quote"), anchor.get("section")
        if (
            set(anchor) - {"kind", "quote", "section"}
            or not isinstance(quote, str)
            or not (section is None or isinstance(section, str))
        ):
            raise HTTPException(
                422,
                {
                    "field": "anchor",
                    "detail": 'a text anchor is {"kind": "text", "quote": "...", '
                    '"section": "optional heading text"}',
                },
            )
        store._check_size(
            quote,
            comments.MAX_QUOTE_BYTES,
            "quote",
            agent=agent,
            artifact_id=artifact_id,
        )
        current = store.get_body(db, agent, artifact_id, as_reader=True)
        try:
            comments.resolve_text_anchor(current.body, quote, section)
        except comments.TextAnchorError as exc:
            raise HTTPException(
                422,
                {
                    "field": "anchor",
                    "detail": exc.detail,
                    "matches": exc.matches,
                },
            ) from exc
        return {"kind": "page"}, quote

    def _add_comment(
        artifact_id,
        payload,
        background,
        agent,
        conn=None,
        seat_session=None,
        later=None,
    ):
        try:
            anchor, quote = _text_anchor(artifact_id, payload, agent)
            row = comments.add(
                db,
                conn=conn,
                artifact_id=artifact_id,
                author_kind="agent",
                author=agent,
                body=payload.body,
                anchor=anchor,
                quote=quote,
                thread_root=payload.thread_root,
                agent=agent,
                kind=payload.kind,
                session=seat_session,
                after_commit=later,
            )
            # GitGuardian checks the saved comment straight after the response.
            if later is not None:
                later.append(partial(comments.second_scan, db, row.id))
            else:
                background.add_task(comments.second_scan, db, row.id)
            return row.__dict__
        except store.NotAPage as exc:
            # 422, not 413: the body is not too much, it is not a page. Named
            # like every other refusal so a caller can tell them apart without
            # reading prose (homelab-ij8.28 -- a builder script's "wrote
            # <path>" log line was published as a status page).
            raise HTTPException(
                422,
                {
                    "field": exc.field,
                    "detail": (
                        "body does not look like an HTML page: no doctype and "
                        "no HTML tag anywhere in it. If a command's output "
                        "reached this call instead of your file, that is the "
                        "usual cause."
                    ),
                },
            ) from exc
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
        except comments.InvalidKind as exc:
            raise HTTPException(
                422,
                {
                    "field": exc.field,
                    "detail": (
                        "kind must be comment, remove or looks_good, with a "
                        "quoted range for the last two"
                    ),
                },
            ) from exc
        except comments.VerdictRefused as exc:
            raise HTTPException(
                422,
                {
                    "field": exc.field,
                    "detail": (
                        "on a page you do not own only kind comment is allowed: "
                        "remove and looks_good are Jeremy's. Say it in the body "
                        "of a plain comment instead."
                    ),
                },
            ) from exc
        except comments.NoSuchThread as exc:
            raise HTTPException(
                422,
                {
                    "field": "thread_root",
                    "detail": "no open root comment with that id on this artifact",
                },
            ) from exc
        except comments.NoSuchDecision as exc:
            raise HTTPException(404, "no such decision") from exc
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc

    @router.post("/comments/{comment_id}/resolve")
    def resolve_comment(comment_id: str, agent: str = Depends(caller)):
        try:
            # `agent=` EXPLICITLY, never left to the default. The default is
            # None, which means "the gallery, acting for Jeremy" and skips the
            # ownership check entirely -- correct there, and a privilege
            # escalation here, where any key could then resolve any seat's
            # threads. Spelled out so the keyed path cannot acquire the
            # gallery's authority by someone changing a default.
            return comments.resolve(db, comment_id, by=agent, agent=agent).__dict__
        except store.NotFound as exc:
            raise HTTPException(404, "no such comment") from exc

    @router.get("/comments")
    def list_all_comments(include_resolved: bool = False, agent: str = Depends(caller)):
        """Every comment across the pages the caller owns, newest first.
        Threads under his decision notes (`anchor.kind == "decision"`) are
        included on purpose; `artifacts-decide` labels them."""
        return [
            row.__dict__
            for row in comments.list_for_agent(
                db, agent, include_resolved=include_resolved
            )
        ]

    @router.post("/artifacts/{artifact_id}/reviews", status_code=201)
    def ask_review(
        artifact_id: str,
        payload: ReviewBody | None = None,
        agent: str = Depends(caller),
        seat_session: str | None = Depends(session),
    ):
        """Ask Jeremy to review this page (page review step 2). A decision of
        kind 'review' with four fixed verdicts; answered in the gallery like any
        question, and read back with GET /reviews/{id}."""
        try:
            row = decisions.ask_review(
                db,
                agent=agent,
                artifact_id=artifact_id,
                session=seat_session,
                mode=payload.mode if payload else None,
            )
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc
        return row.__dict__

    @router.get("/reviews/{review_id}")
    def read_review(review_id: str, agent: str = Depends(caller)):
        """The verdict and the one message built from it. Not yours, and not a
        review, answer exactly like a missing id."""
        row, current = decisions.get_owned_with_round(db, agent, review_id)
        if row is None or row.kind != "review":
            raise HTTPException(404, "no such review")
        round_n = current.n if current is not None else None
        # When this round opened: a waiter counts only marks made since, so a
        # round-2 wait is not woken by the marks round 1 already answered.
        round_opened_at = current.opened_at if current is not None else None
        if row.answer is None:
            return {
                "id": row.id,
                "artifact_id": row.artifact_id,
                "created_at": row.created_at,
                "review_mode": row.review_mode,
                "round": round_n,
                "round_opened_at": round_opened_at,
                "verdict": None,
                "answered_at": None,
                "message": None,
                "data": None,
            }
        try:
            message, data = review.render(db, review_id, agent=agent)
        except store.NotFound as exc:
            raise HTTPException(404, "no such review") from exc
        return {
            "id": row.id,
            "artifact_id": row.artifact_id,
            "created_at": row.created_at,
            "review_mode": row.review_mode,
            "round": round_n,
            "round_opened_at": round_opened_at,
            "verdict": decisions.VERDICTS[row.answer],
            "answered_at": row.answered_at,
            "message": message,
            "data": data,
        }

    def _claim_session(raw: str) -> str:
        label = text.sanitize((raw or "").strip())
        if not label:
            raise HTTPException(
                422, {"field": "session", "detail": "session is required"}
            )
        if len(label) > SESSION_MAX:
            raise HTTPException(
                422,
                {
                    "field": "session",
                    "detail": f"session must be at most {SESSION_MAX} characters",
                },
            )
        return label

    def _claimed(exc: decisions.Claimed) -> JSONResponse:
        # The holder at the top level, like the 200, so a waiter reads the
        # same three fields either way; `detail` for anything that only prints.
        return JSONResponse(
            status_code=409,
            content={
                "claimed_by": exc.claim.claimed_by,
                "claim_expires_at": exc.claim.claim_expires_at,
                "round": exc.claim.round,
                "detail": "another session is acting on this round",
            },
        )

    @router.post("/reviews/{review_id}/claim")
    def claim_review(review_id: str, payload: ClaimBody, agent: str = Depends(caller)):
        """Claim the review's current round for one session, for five minutes
        (page review step 5). 409 names the session holding it. Not yours
        answers exactly like a missing id."""
        label = _claim_session(payload.session)
        try:
            claim = decisions.claim_round(db, agent, review_id, label)
        except store.NotFound as exc:
            raise HTTPException(404, "no such review") from exc
        except decisions.Claimed as exc:
            return _claimed(exc)
        return {
            "claimed_by": claim.claimed_by,
            "claim_expires_at": claim.claim_expires_at,
            "round": claim.round,
            # So a client can time its renewal without trusting its own wall
            # clock against the server's.
            "lease_seconds": claim.lease_seconds,
        }

    @router.post("/reviews/{review_id}/release")
    def release_review(
        review_id: str, payload: ReleaseBody, agent: str = Depends(caller)
    ):
        """Hand the round back with a reason: the session cannot act on it.
        409 in the claim's shape when another session holds the lease."""
        label = _claim_session(payload.session)
        try:
            claim = decisions.release_round(db, agent, review_id, label, payload.reason)
        except decisions.MissingReason as exc:
            raise HTTPException(
                422, {"field": "reason", "detail": "reason is required"}
            ) from exc
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
        except store.NotFound as exc:
            raise HTTPException(404, "no such review") from exc
        except decisions.Claimed as exc:
            return _claimed(exc)
        return {
            "claimed_by": None,
            "claim_expires_at": None,
            "round": claim.round,
            "released_reason": payload.reason,
        }

    @router.post("/artifacts/{artifact_id}/decisions", status_code=201)
    def create_decision(
        artifact_id: str,
        payload: DecisionBody,
        agent: str = Depends(caller),
        seat_session: str | None = Depends(session),
    ):
        try:
            return decisions.ask(
                db,
                agent=agent,
                artifact_id=artifact_id,
                question=payload.question,
                options=payload.options,
                recommended=payload.recommended,
                session=seat_session,
            ).__dict__
        except store.NotAPage as exc:
            # 422, not 413: the body is not too much, it is not a page. Named
            # like every other refusal so a caller can tell them apart without
            # reading prose (homelab-ij8.28 -- a builder script's "wrote
            # <path>" log line was published as a status page).
            raise HTTPException(
                422,
                {
                    "field": exc.field,
                    "detail": (
                        "body does not look like an HTML page: no doctype and "
                        "no HTML tag anywhere in it. If a command's output "
                        "reached this call instead of your file, that is the "
                        "usual cause."
                    ),
                },
            ) from exc
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

    # ANSWERED DECISIONS COME BACK BY DEFAULT (homelab-ij8.33). This route used
    # to hide them, so an agent reading its page back after Jeremy had answered
    # got an EMPTY LIST -- which reads exactly like "the answers went nowhere",
    # and the first outside agent to use this service was about to conclude
    # answers were write-only on the strength of it. "What still needs him" is
    # a real question, but "what did he say" is the commoner one, and a default
    # that silently hides the thing you came for is the wrong default. Every
    # caller that existed had already learned to pass include_answered=true,
    # and the skill carried a paragraph insisting it was "not optional" --
    # documentation patching over a default rather than fixing it.
    #
    # Open-only is now the explicit ask: ?open_only=true. include_answered
    # stays accepted so the callers that learned the hard way keep working.
    @router.get("/artifacts/{artifact_id}/decisions")
    def list_decisions(
        artifact_id: str,
        include_answered: bool = True,
        open_only: bool = False,
        # A withdrawn question is part of a page's record and was unreadable on
        # every route an agent could reach (homelab-ij8.44). It matters more
        # since 21 Sep, when archiving or deleting a page started withdrawing
        # its open questions -- so most withdrawals are now the service's doing
        # and the agent never chose them.
        include_closed: bool = False,
        agent: str = Depends(caller),
    ):
        try:
            # A page open to agents shows its questions and his answers to any
            # agent reading it -- Jeremy's choice (4b), homelab-ij8.105.
            store.get_owned(db, agent, artifact_id, as_reader=True)
            can_read = True
        except store.NotFound as exc:
            # The grant reads questions, answers and outcomes, never the page's
            # conversation: replies under his notes are comments, and comments
            # are for whoever can read the page.
            can_read = False
            # A key holding decisions:read-all reads any seat's questions HERE
            # too, not only on /decisions?scope=all (homelab-9gj). The operator
            # consumed the grant on 20 Sep and found it half a feature: it could
            # see a cross-seat question while it was open, and infer from its
            # disappearance that Jeremy had answered, but the enrichment step
            # that fetches the actual letter and note comes through this route
            # and answered 404 -- so two of that day's three live decisions were
            # closed in the operator's queue with "answered on its page" instead
            # of what he actually said.
            #
            # Still 404 without the grant, and 404 for a page that does not
            # exist, so "not yours" and "no such page" stay indistinguishable to
            # every caller that has not been granted the difference.
            if not grants.has(db, agent, grants.READ_ALL_DECISIONS) or not store.exists(
                db, artifact_id
            ):
                raise HTTPException(404, "no such artifact") from exc
        # Outcomes come back WITH the decisions, in one extra query for the
        # whole page rather than one per question. A caller reading "what
        # happened here" should not have to know to ask twice, and the one
        # thing an outcome is for is being read next to the decision it is
        # about.
        outcomes = decisions.outcomes_for_artifact(db, artifact_id)
        shown = decisions.list_for_artifact(
            db,
            artifact_id,
            include_answered=include_answered and not open_only,
            include_closed=include_closed,
        )
        # SAY WHAT WAS LEFT OUT (homelab-ij8.45): two agents in one day read an
        # empty open-only answer as "my answers went nowhere". The count of
        # answered questions this view omitted rides in a header, so the body
        # keeps its shape for every existing caller.
        omitted = 0
        if not (include_answered and not open_only):
            every = decisions.list_for_artifact(
                db, artifact_id, include_answered=True, include_closed=include_closed
            )
            omitted = sum(1 for d in every if d.answer) - sum(
                1 for d in shown if d.answer
            )
        notes = comments.note_threads(db, artifact_id) if can_read else {}
        return JSONResponse(
            jsonable_encoder(
                [
                    {
                        **row.__dict__,
                        "outcomes": [o.__dict__ for o in outcomes.get(row.id, [])],
                        "replies": [r.__dict__ for r in notes.get(row.id, [])],
                    }
                    for row in shown
                ]
            ),
            headers={"x-artifacts-answered-omitted": str(omitted)},
        )

    @router.get("/decisions")
    def list_all_decisions(
        # None, not True, because the right default DIFFERS BY SCOPE and both
        # defaults were set by a consumer getting hurt.
        #
        # scope=mine defaults to True (homelab-ij8.33): an agent polling its own
        # feed is asking "what did he say", and a plain poll that hid his answer
        # taught agents to pass a flag to see the thing they came for.
        #
        # scope=all defaults to False, and that is load-bearing rather than
        # merely cautious: the operator's queue reconciles against this feed by
        # DISAPPEARANCE -- a row it is holding that stops being returned is
        # taken as proof the decision is no longer open, and closed. Answered
        # rows appearing here by default would leave those rows open forever.
        include_answered: bool | None = None,
        open_only: bool = False,
        include_closed: bool = False,
        scope: str = Query("mine", pattern="^(mine|all)$"),
        agent: str = Depends(caller),
    ):
        # This is how an agent learns Jeremy's answer -- across every artifact
        # it owns: a plain poll of this route works everywhere a key does, and
        # since homelab-ij8.33 a plain poll is enough (answers come back by
        # default; ?open_only=true is the explicit ask for what still needs
        # him). `/events` (below) is the same fact told sooner,
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
            # The grant now covers what he SAID, not only that he was asked
            # (homelab-9gj): ?scope=all&include_answered=true carries answer,
            # answer_note and answered_at for every seat, and
            # &include_closed=true carries the withdrawn ones. Both stay off
            # unless asked for -- see the signature for why that matters more
            # than it looks.
            return [
                {
                    **row.decision.__dict__,
                    "agent": row.agent,
                    "artifact_title": row.artifact_title,
                    "age_seconds": row.age_seconds,
                }
                for row in decisions.across_seats(
                    db,
                    include_answered=bool(include_answered) and not open_only,
                    # Deliberately NOT suppressed by open_only, because the
                    # per-artifact route above does not suppress it either and
                    # one flag pair must mean one thing on every route.
                    include_closed=include_closed,
                )
            ]
        return [
            row.__dict__
            for row in decisions.list_for_agent(
                db,
                agent,
                include_answered=(include_answered is not False) and not open_only,
                include_closed=include_closed,
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
                round=payload.round,
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
        except decisions.AlreadyDecided as exc:
            named, current = exc.round, exc.current
            raise HTTPException(
                409,
                {
                    "field": "answer",
                    # The verdict that counted, from its round when rounds
                    # exist: a stale relay's round has been cleared off the row.
                    "verdict": (
                        named.verdict
                        if named is not None and named.verdict is not None
                        else decisions.VERDICTS.get(exc.row.answer)
                    ),
                    "answered_at": (
                        named.closed_at
                        if named is not None and named.closed_at is not None
                        else exc.row.answered_at
                    ),
                    "round": current.n if current is not None else None,
                    "verdict_round": named.n if named is not None else None,
                    "message": (
                        f"This review already has a verdict for round {named.n}: "
                        f"{named.verdict}"
                        if named is not None and named.verdict is not None
                        else None
                    ),
                },
            ) from None
        except store.NotFound:
            raise HTTPException(404, "no such open decision") from None
        return row.__dict__

    @router.post("/decisions/{decision_id}/outcome", status_code=201)
    def add_outcome(
        decision_id: str,
        payload: OutcomeBody,
        agent: str = Depends(caller),
        seat_session: str | None = Depends(session),
    ):
        """Append what this decision caused, or what changed under it.

        homelab-ij8.44. Answering was the end of the model, so what a decision
        PRODUCED -- a PR, a closed bead, a correction to the agent's own
        framing -- lived only in prose somebody happened to rewrite.

        Deliberately allowed on an UNANSWERED decision. The operator corrected
        two promises on a page while its question was still open, and that is
        the case with teeth: a question whose premise moved after Jeremy read
        it and before he answered is one he can answer wrongly through no fault
        of his own.

        Append only -- there is no PUT and no DELETE here, and that is the
        point rather than an omission.
        """
        try:
            return decisions.record_outcome(
                db,
                decision_id,
                agent=agent,
                body=payload.body,
                session=seat_session,
            ).__dict__
        except store.NotAPage as exc:
            # 422, not 413: the body is not too much, it is not a page. Named
            # like every other refusal so a caller can tell them apart without
            # reading prose (homelab-ij8.28 -- a builder script's "wrote
            # <path>" log line was published as a status page).
            raise HTTPException(
                422,
                {
                    "field": exc.field,
                    "detail": (
                        "body does not look like an HTML page: no doctype and "
                        "no HTML tag anywhere in it. If a command's output "
                        "reached this call instead of your file, that is the "
                        "usual cause."
                    ),
                },
            ) from exc
        except store.BodyTooLarge as exc:
            raise HTTPException(
                413,
                {
                    "field": exc.field,
                    "size": exc.size,
                    "detail": f"{exc.field} exceeds the size limit",
                },
            ) from exc
        except store.SecretFound as found:
            raise HTTPException(
                422, _refused_detail(found.field, found.findings)
            ) from found
        except store.ScanFailed as exc:
            raise HTTPException(
                503, {"field": exc.field, "detail": _SCAN_FAILED_DETAIL}
            ) from exc
        except store.NotFound as exc:
            raise HTTPException(404, "no such decision") from exc

    @router.post("/decisions/{decision_id}/replies", status_code=201)
    def reply_to_note(
        decision_id: str,
        payload: ReplyBody,
        background: BackgroundTasks,
        agent: str = Depends(caller),
        idempotency_key: str | None = Header(default=None),
        seat_session: str | None = Depends(session),
    ):
        """Reply under the note he left when answering this decision.

        Any agent that can read the page may: its owner, or any agent when the
        page is open to agents. The reply is a comment in a thread anchored to
        the decision; it never touches the decision row (no answer, note,
        acknowledgement or closing). "Not yours" answers exactly like "no such
        decision". A decision he has not answered has no note to reply to."""
        later: list = []

        def write(conn) -> tuple[int, object]:
            later.clear()
            # Scope by the same join as list_outcomes, read inside the keyed
            # transaction when there is one.
            with nullcontext(conn) if conn is not None else db.connect() as c:
                found = c.execute(
                    "select decision.artifact_id, decision.answer from decision"
                    " join artifact on artifact.id = decision.artifact_id"
                    " where decision.id = %s"
                    "   and (artifact.agent = %s or artifact.agents_can_read)"
                    "   and artifact.deleted_at is null",
                    (decision_id, agent),
                ).fetchone()
                if found is None:
                    raise HTTPException(404, "no such decision")
                artifact_id, answer = found
                if answer is None:
                    raise HTTPException(
                        422,
                        {
                            "field": "decision",
                            "detail": "he has not answered yet",
                        },
                    )
                root = c.execute(
                    "select id from comment where artifact_id = %s"
                    " and thread_root is null and withdrawn_at is null"
                    " and not held and anchor->>'kind' = 'decision'"
                    " and anchor->>'decision_id' = %s"
                    " order by created_at asc, id asc limit 1",
                    (artifact_id, decision_id),
                ).fetchone()
            body = (
                CommentBody(body=payload.body, thread_root=root[0])
                if root is not None
                else CommentBody(
                    body=payload.body,
                    anchor={"kind": "decision", "decision_id": decision_id},
                )
            )
            return 201, _add_comment(
                artifact_id, body, background, agent, conn, seat_session, later
            )

        response = idempotency.run(
            db,
            agent,
            f"replies:{decision_id}",
            idempotency_key,
            payload.model_dump(),
            write,
        )
        for send in later:
            background.add_task(send)
        return response

    @router.get("/decisions/{decision_id}/outcomes")
    def list_outcomes(decision_id: str, agent: str = Depends(caller)):
        # Ownership checked here rather than in the listing helper, which
        # trusts its id like every other listing helper in that module. Same
        # join as the write, so "not yours" and "no such decision" answer
        # identically.
        with db.connect() as conn:
            owned = conn.execute(
                "select 1 from decision join artifact"
                " on artifact.id = decision.artifact_id"
                " where decision.id = %s"
                "   and (artifact.agent = %s or artifact.agents_can_read)"
                "   and artifact.deleted_at is null",
                (decision_id, agent),
            ).fetchone()
        if owned is None:
            raise HTTPException(404, "no such decision")
        return [row.__dict__ for row in decisions.outcomes_for(db, decision_id)]

    @router.post("/decisions/{decision_id}/acknowledge")
    def acknowledge_decision(
        decision_id: str,
        agent: str = Depends(caller),
        idempotency_key: str | None = Header(default=None),
    ):
        def write(conn) -> tuple[int, object]:
            try:
                return 200, decisions.acknowledge(
                    db, agent, decision_id, conn=conn
                ).__dict__
            except store.NotFound as exc:
                raise HTTPException(404, "no such decision") from exc

        return idempotency.run(
            db, agent, f"acknowledge:{decision_id}", idempotency_key, {}, write
        )

    @router.get("/decisions/uncollected")
    def list_uncollected(
        stuck_after_seconds: int = Query(
            decisions.STUCK_AFTER_SECONDS, ge=0, le=86_400
        ),
        scope: str = Query("mine", pattern="^(mine|all)$"),
        agent: str = Depends(caller),
    ):
        # `scope=all` is the operator's view, and it is the half of ij8.64 that
        # sat unbuilt since 19 Sep behind the note "needs a key that does not
        # exist yet". IT EXISTS NOW: decisions:read-all was added for
        # /decisions?scope=all and nobody came back to this route. The blocker
        # had been gone for a day and the comment saying otherwise was the only
        # thing still enforcing it -- which is its own small lesson about
        # comments that record a state of the world rather than a reason.
        #
        # Gated identically to /decisions, deliberately: two spellings of the
        # same privilege is how one of them ends up laxer than the other.
        if scope == "all" and not grants.has(db, agent, grants.READ_ALL_DECISIONS):
            # Name the seat -- see /decisions for why. Every agent on baba
            # shares one key file, so the seat a key authenticates as is not
            # the seat its holder assumes.
            raise HTTPException(
                403,
                f"this key authenticates as seat '{agent}', which does not "
                "have decisions:read-all. Check GET /api/v1/me — you may be "
                "using a different seat's key file than you intended.",
            )
        return [
            {
                "decision": row.decision.__dict__,
                "state": row.state,
                "last_read_at": row.last_read_at,
                # Whose, and on what. Redundant when a seat asks about itself
                # and the entire point when the operator asks about everyone.
                "agent": row.agent,
                "artifact_title": row.artifact_title,
            }
            for row in decisions.uncollected(
                db,
                agent=None if scope == "all" else agent,
                stuck_after_seconds=stuck_after_seconds,
            )
        ]

    @router.get("/events")
    def list_events(
        request: Request,
        since: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
        wait: float = Query(0.0, ge=0.0, le=300.0),
        # Narrow the feed to one session's own work. A CONVENIENCE, not a
        # boundary -- every row it hides was already visible to this key, and
        # omitting it still returns all of them. Taken from the QUERY here, not
        # from the header: a caller may legitimately poll for a session other
        # than the one it is declaring itself to be, and conflating "who I am"
        # with "what I want to see" would remove that for no gain.
        #
        # homelab-31, 21 Sep, after two false wakes in ten minutes on another
        # seat's decisions: "a watcher which cries wolf trains its reader to
        # stop looking, and the one wake that matters is indistinguishable from
        # these."
        session_filter: str | None = Query(None, alias="session", max_length=120),
        # Leave out this agent's own actions (homelab-ij8.106).
        exclude_self: bool = False,
        agent: str = Depends(caller),
        seat_session: str | None = Depends(session),
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
        # The SESSION the caller declared, not just the seat: "is anybody
        # listening" has to mean "is the session that asked listening", or the
        # stranded-answer alert reports a live reader while the asker is gone.
        # Recorded for whatever the caller declared, including nothing -- an
        # unlabelled reader is a real reader and shares one row.
        events.note_read(db, agent, seat_session)
        # THROUGH CLOUDFLARE, NEVER HOLD PAST 50 SECONDS (homelab-ij8.106).
        # Measured 23 Sep 2026: wait=60 came back as Cloudflare's 524 after 125 s.
        # The public ingress gives up on an upstream after nginx's default 60 s
        # and retries once; this service answered each copy at 60.08 s, so both
        # were cut and Cloudflare's own ~100 s limit fired. Inside the house the
        # ingress allows an hour, which is why no waiter here ever saw it.
        # `cf-ray` is set by Cloudflare on every request it forwards.
        if request.headers.get("cf-ray"):
            wait = min(wait, PUBLIC_MAX_WAIT)
        exclude = agent if exclude_self else None
        rows = events.list_for_agent(
            db,
            agent,
            since=since,
            limit=limit,
            session=session_filter,
            exclude_actor=exclude,
        )
        if not rows and wait:
            rows = events.wait_for_agent(
                db,
                agent,
                since=since,
                limit=limit,
                seconds=wait,
                session=session_filter,
                exclude_actor=exclude,
            )
        # Free text goes out without terminal control characters (homelab-ij8.55):
        # a comment body is anybody's words, and it lands in an agent's terminal.
        cleaned = [
            {
                **row.__dict__,
                "summary": text.strip_controls(row.summary),
                "note": text.strip_controls(row.note),
            }
            for row in rows
        ]
        return JSONResponse(
            jsonable_encoder(cleaned),
            headers={
                # "From now" is this number; see events.latest_seq.
                "x-artifacts-cursor": str(events.latest_seq(db, agent)),
                "x-artifacts-max-wait": str(int(PUBLIC_MAX_WAIT)),
            },
        )

    # --- the 404 that tells you what DOES exist -----------------------------
    #
    # Declared LAST on purpose: FastAPI matches in declaration order, so this
    # can only ever catch a path none of the real routes above claimed.
    #
    # WHY IT IS HERE. On 21 Sep a seat lost a page's source, guessed /source,
    # /content, /html, /versions, /revisions and /history, got a bare 404 from
    # every one, and concluded the capability did not exist -- leaving a live
    # page it owned unupdated. `/body` had been there since 19 Sep. Its own
    # diagnosis is the design note for this handler: "the failure mode of
    # guessing is indistinguishable from the failure mode of a missing
    # capability -- every wrong guess returns exactly what a missing feature
    # returns."
    #
    # So a wrong guess stops looking like an absent feature. The first 404 now
    # carries the answer, which is the only moment the reader is guaranteed to
    # be looking.
    _SUBROUTES = (
        "body",
        "versions",
        "comments",
        "decisions",
        "archive",
        "rename",
        "pin",
        "duplicate",
        "restore",
        "destroy",
    )

    # The same treatment for a decision's sub-routes, added 21 Sep after the
    # operator's first `artifacts-decide outcome` answered a bare 404 and could
    # not tell which of two things it meant: the route was not deployed yet
    # (it was not -- the push was four minutes old), or the key did not own the
    # decision. Those need opposite responses and looked identical.
    #
    # They are in fact distinguishable by BODY -- FastAPI's own {"detail":
    # "Not Found"} against this module's "no such decision" -- but only to
    # someone who already knows that, which is folklore rather than a feature.
    # Naming the real sub-routes turns it into something the first 404 says
    # out loud, at the only moment the reader is guaranteed to be looking.
    _DECISION_SUBROUTES = (
        "outcome",
        "outcomes",
        "record",
        "acknowledge",
        "close",
        "replies",
    )

    @router.api_route(
        "/decisions/{decision_id}/{unknown:path}",
        methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        include_in_schema=False,
    )
    def unknown_decision_subroute(
        decision_id: str, unknown: str, agent: str = Depends(caller)
    ):
        raise HTTPException(
            404,
            {
                "detail": f"no such route on a decision: {unknown!r}",
                "did_you_mean": (
                    "outcome -- append what this decision caused, or what "
                    "changed under it"
                    if unknown in ("outcomes/", "result", "results", "caused")
                    else (
                        "replies -- POST a reply under his note on an answered decision"
                        if unknown in ("reply", "note")
                        else None
                    )
                ),
                "routes": sorted(_DECISION_SUBROUTES),
                # The distinction that cost the operator two turns. A route that
                # exists but is not yours answers "no such decision"; a route
                # that does not exist answers THIS.
                "note": (
                    "this names the routes THIS BUILD serves. If the one you "
                    "want is missing, the service may be older than the client "
                    "-- check GET /api/v1/me against the version you expect."
                ),
            },
        )

    @router.api_route(
        "/artifacts/{artifact_id}/{unknown:path}",
        methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        include_in_schema=False,
    )
    def unknown_subroute(artifact_id: str, unknown: str, agent: str = Depends(caller)):
        raise HTTPException(
            404,
            {
                "detail": f"no such route on an artifact: {unknown!r}",
                "did_you_mean": (
                    "body -- the page's own HTML, to the agent that published it"
                    if unknown in ("source", "content", "html", "raw", "page", "text")
                    else None
                ),
                "routes": sorted(_SUBROUTES),
            },
        )

    return router
