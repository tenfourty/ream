"""The agent-facing publish API: mint a key with `ream.keys.mint`, then
POST/PATCH pages under `/api/v1/pages`. Every route is scoped to the
calling key's agent -- `caller` reads the agent from the verified key alone,
never from a header, query parameter or body field, so nothing else can
influence whose pages a request sees or changes.

`/api/v1/artifacts` is the old name for the same routes and keeps working for
good: every handler is registered under both words (see `_BothWords`), the same
function, not a redirect, because callers POST.
"""

import base64
import html
import logging
import threading
from contextlib import nullcontext
from datetime import UTC, datetime
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
    Response,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ream import (
    anchors,
    clientlevel,
    comments,
    connect,
    csp,
    decisions,
    events,
    folders,
    formanswer,
    forms,
    grants,
    hosts,
    htmlbound,
    idempotency,
    image_store,
    images,
    inbox,
    keys,
    listening,
    markdown,
    onboarding,
    pagediff,
    partchanges,
    partdiff,
    partner_in,
    ratelimit,
    reach,
    readside,
    receipts,
    renderfind,
    renderwire,
    review,
    scanslots,
    sections,
    store,
    templates,
    text,
    textvars,
    thumbs,
    traps,
)
from ream.db import Database

# Failed and refused authentications, one line each: the address and the count,
# never the key. Armed with its own handler in `serve` for the same reason the
# access log is -- an unarmed logger in this process prints nothing.
_auth_log = logging.getLogger("ream.auth")


# The longest a held /events request is kept when it arrived through Cloudflare
# (see list_events). Under nginx's 60 s and Cloudflare's ~100 s, with room.
PUBLIC_MAX_WAIT = 50.0


class TransferBody(BaseModel):
    to: str


class AgentsBody(BaseModel):
    """`can_read` is required: True opens the page to every agent in his space,
    False closes it to him and its publisher (homelab-ij8.105)."""

    can_read: bool


class PickupBody(BaseModel):
    pickup: str = Field(max_length=2000)


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


def _both_headers(headers: dict[str, str]) -> dict[str, str]:
    """Response headers under both names, for good: `x-ream-*` and the old `x-artifacts-*`.
    An agent tool installed before the rename follows `next` and `cursor` by the old name."""
    both = dict(headers)
    for name, value in headers.items():
        both[name.replace("x-ream-", "x-artifacts-", 1)] = value
    return both


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
    # The calling agent's own folder to file the page in; created on first use, and the result
    # says so (`folder.created`). `folder_title` names a NEW folder, once.
    folder: str | None = None
    folder_title: str | None = None
    # How often this publisher promises to vouch for the page, in seconds (60 to a year); a page
    # that goes quiet for 2.5 of them is flagged. Omitted, null or 0 declares nothing.
    refresh_every: int | None = None
    # Whether commenting starts "on" or "off" for a reader of this page (9 Nov 2026). Off: no pill
    # on selected words and nothing inviting a comment, until the reader presses Comment or turns
    # it on. Required on a new page (422 without it); rows from before it was required are null, read as on.
    comments: Literal["on", "off"] | None = None
    # False publishes the page closed to other agents (only the owner and the publisher can read
    # it), from its first version: for embargoed or security content, which must never be open
    # even for the moment before a second call closes it. True, the default, is open to every
    # agent. An update cannot change it (POST /pages/{id}/agents does).
    agents_can_read: bool = True


_BAD_SLUG = {
    "field": "folder",
    "detail": (
        "a folder name is lowercase letters, digits and hyphens, 1 to 40 characters, "
        "starting with a letter or digit"
    ),
}


_BAD_INTERVAL = {
    "field": "refresh_every",
    "detail": "refresh_every is seconds, 60 to 31536000 (a year); 0 or null clears it",
}


def _archived_folder(slug: str) -> dict:
    return {
        "field": "folder",
        "detail": f"folder {slug!r} is archived: unarchive it first",
    }


class RecordBody(BaseModel):
    """An answer Jeremy gave somewhere that is not the page.

    `source` and `note` are both required and neither has a default. A default
    source would let a relay be recorded without anyone choosing what it was,
    and an empty note would throw away the part that usually decides things —
    every one of his notes so far has qualified the letter it came with.
    """

    # A question's lettered answer. Absent for a form, which is relayed per field.
    letter: str | None = None
    note: str
    source: str
    said_at: str | None = None
    # A review's round the verdict is for; a later round refuses it (409).
    round: int | None = None
    # A form's fields, {name: value}: each stored with its source, none of them his own
    # submit (a checkbox group is a list, or "a,b"; a lone checkbox true or false).
    fields: dict | None = None


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
    # Omitted leaves the page's folder; a slug files it (created on first use); "" takes it out.
    folder: str | None = None
    folder_title: str | None = None
    # Omitted keeps the declared interval; a number declares it; null or 0 clears it.
    refresh_every: int | None = None
    # Omitted (or null) keeps the page's setting; "on" or "off" sets it.
    comments: Literal["on", "off"] | None = None


class CommentsBody(BaseModel):
    comments: Literal["on", "off"]


class TouchBody(BaseModel):
    """ "Checked, still current" -- every field optional, so `{}` is a good touch."""

    status: Literal["ok", "failed"] = "ok"
    note: str | None = None
    # Vouch only if the page is still this revision (the hash, or the version number): 409 if not.
    if_revision: str | None = None
    # Omitted keeps the declared interval; a number declares it; null or 0 clears it.
    refresh_every: int | None = None


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


MAX_MOVE_IDS = 200


class MoveBody(BaseModel):
    """`ids` is capped so one request is one bounded transaction; `folder` is required (null
    takes the pages out), so an empty body is a 422 rather than a silent un-filing."""

    ids: list[str] = Field(min_length=1, max_length=MAX_MOVE_IDS)
    folder: str | None
    # As on publish: the display name of a NEW folder, set when this move makes it.
    folder_title: str | None = None


class FolderRenameBody(BaseModel):
    """At least one of the two; a slug rename keeps the old slug working as an alias."""

    slug: str | None = None
    title: str | None = None


class FolderArchiveBody(BaseModel):
    """`archived` is required, as on the page route; archiving changes nothing without
    `confirm: true` and says what it would do."""

    archived: bool
    confirm: bool = False


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


class ReceiptBody(BaseModel):
    """What the agent did with his answer. `state` is seen, working, done, blocked or
    wont-do; blocked and wont-do need a note, only done takes a link, only working
    takes `for` (a duration like 30m or 4h, at most 12h, default 4h)."""

    state: str
    note: str = ""
    link: str | None = None
    for_: str | None = Field(default=None, alias="for")


class ReplyBody(BaseModel):
    """A reply under the note he left when answering a decision."""

    body: str


class CommentBody(BaseModel):
    body: str
    quote: str = ""
    anchor: dict | None = None
    thread_root: str | None = None
    kind: str = "comment"
    # Ids from POST /artifacts/{id}/comment-images, at most four; the comment is
    # refused whole if any is unknown, someone else's, already used or on another page.
    images: list[str] = Field(default_factory=list)


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
    # What a question carries (runcove-5t3oj.39.1). `Any` for the same reason as
    # `options`: a wrong shape reaches decisions._validate_fields and comes back
    # as one 422 that names the field and the rule, not as Pydantic's own text.
    context: Any = ""
    topic: Any = None
    depends_on: Any = None
    replaces: Any = None
    urgency: Any = "when_you_can"
    blocked_on: Any = None
    needed_by: Any = None
    # `audience` is the field; `for` is its old name, accepted for one release.
    # Both sent: `audience` wins (see `audience_sent`).
    audience: Any = None
    for_offices: Any = Field(default=None, alias="for")

    def audience_sent(self) -> tuple[Any, str]:
        """The value and the field name to report errors under."""
        if self.audience is not None:
            return self.audience, "audience"
        return self.for_offices, "for"


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


# Source reads for GET /questions are not cut at the page size: the ranking is
# in Python and needs every open question. Far above any real queue.
_ALL_OPEN = 1_000_000


def _slack_view(c: partner_in.Copy, now: datetime) -> dict:
    """A question whose home is the partner, as an agent reads it: the fields a
    page question has, plus `source` and `home`. Read-only: no route here
    answers it."""
    return {
        "id": c.qid,
        "source": c.source,
        "home": "partner",
        "read_only": True,
        "kind": "question",
        "question": c.title,
        "options": c.options,
        "recommended": c.recommended,
        "context": c.context,
        "link": c.link,
        "topic": c.topic,
        "depends_on": c.depends_on,
        "replaces": c.replaces,
        "urgency": c.urgency,
        "blocked_on": c.blocked_on,
        "needed_by": c.needed_by,
        "audience": c.for_offices,
        "for": c.for_offices,
        "agent": c.asking_office,
        "artifact_id": None,
        "artifact_title": "",
        "answer": None,
        "pending_answer": c.pending_answer,
        "held_by": [],
        "created_at": c.created_at,
        "overdue": inbox.is_overdue(c, now),
    }


def agent_card_response(request: Request) -> JSONResponse:
    """The card for the face it was asked on. Which face is decided by matching
    the Host against the CONFIGURED names (the router already refused any other
    name), so no part of the request's Host is ever written into the card."""
    public = hosts.is_api(hosts.normalise(hosts.first_host_header(request.scope)))
    return JSONResponse(
        onboarding.agent_card(public_only=public),
        headers={
            "x-content-type-options": "nosniff",
            "cache-control": "public, max-age=300",
            # The card differs by face, so a cache must never hand one host's
            # copy to the other.
            "vary": "host",
        },
    )


def _page_word(path: str) -> str | None:
    """The `/pages` spelling of an `/artifacts` route path, or None if it has none."""
    if path == "/artifacts" or path.startswith("/artifacts/"):
        return "/pages" + path[len("/artifacts") :]
    if path.startswith("/folders/") and path.endswith("/artifacts"):
        return path[: -len("artifacts")] + "pages"
    return None


class _BothWords(APIRouter):
    """A router that serves each `/artifacts` route under `/pages` as well.

    `/pages` is the main name (a page is what an agent makes); `/artifacts` is
    the old one and stays for good. Both are registered against the same
    handler, `/pages` first, so a route declared before its `{id}` sibling
    (`/artifacts/deleted`) keeps that order under both words.
    """

    def add_api_route(self, path: str, endpoint, **kwargs):  # type: ignore[override]
        alias = _page_word(path)
        if alias is not None:
            super().add_api_route(alias, endpoint, **kwargs)
        super().add_api_route(path, endpoint, **kwargs)


def _refuse(
    limiter: ratelimit.FailedAuthLimiter, request: Request, address: str, detail: str
) -> None:
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


def authenticate(
    db: Database, limiter: ratelimit.FailedAuthLimiter, request: Request
) -> keys.AgentKey:
    """The agent key a request carries, verified, or an HTTPException (401, or 429 once an
    address has failed too often). One check for `/api/v1` and `/mcp`, sharing `limiter`,
    so switching between them does not double a caller's allowance."""
    address = ratelimit.client_address(
        request.headers, request.client.host if request.client else None
    )
    if "key" in request.query_params:
        _refuse(
            limiter, request, address, "a key must not be sent in a query parameter"
        )
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        _refuse(limiter, request, address, "an agent key is required")
    if token.strip().startswith("rmo_"):  # no lookup: a sign-in token is /mcp's alone
        _refuse(limiter, request, address, "a sign-in token works only at /mcp")
    key = keys.verify(db, token.strip())
    if key is None:
        _refuse(limiter, request, address, keys.refusal_reason(db, token.strip()))
    # The verified key's public id, for GET /me. Stashed rather than
    # re-derived: the token itself must not be handled twice, and the id is
    # the handle `artifacts-admin list-keys` already prints.
    request.state.agent_key_id = key.id
    # For the machines page's "last used" (homelab-ij8.103).
    keys.touch(db, key.id)
    # A rotated key's first use kills the key it replaced (homelab-ij8.34).
    keys.first_use(db, key)
    request.state.agent_key = key
    return key


# How long a declared session label may be. Generous enough for the shape
# the skill recommends -- "<seat>/<short id>" -- and short enough that it
# cannot become a place to smuggle prose into a column meant for a name.
_SESSION_MAX = 120


def declared_session(request: Request) -> str | None:
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
    return session_label(
        request.headers.get("x-ream-session")
        or request.headers.get("x-artifacts-session")
        or ""
    )


def session_label(raw: str, field: str = "x-ream-session") -> str | None:
    """A declared session label, as `declared_session` reads one from the header (the
    MCP write tools read their `session` argument by the same rule, naming `field`)."""
    raw = raw.strip()
    if not raw:
        return None
    if len(raw) > _SESSION_MAX:
        raise HTTPException(
            422,
            f"{field} must be at most {_SESSION_MAX} characters",
        )
    # Sanitised like every other caller-supplied string that reaches
    # storage and then somebody's terminal: control characters gone,
    # whitespace collapsed. text.sanitize is idempotent, so a clean label
    # is unchanged.
    return text.sanitize(raw) or None


def me_view(
    db: Database, request: Request, agent: str, seat_session: str | None
) -> dict:
    """What `GET /me` answers for a request `authenticate` has passed (the MCP `whoami`
    tool answers the same)."""
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


def open_questions(
    db: Database, agent: str, scope: str, include_set_aside: bool = False
) -> list[dict]:
    """Open questions in the order his inbox shows them, the whole ranked list
    (`GET /questions` pages it; the MCP `questions` tool reads it too). `scope="all"`
    needs `decisions:read-all` and is refused with 403 without it."""
    if scope == "all":
        if not grants.has(db, agent, grants.READ_ALL_DECISIONS):
            raise HTTPException(
                403,
                f"this key authenticates as seat '{agent}', which does not "
                "have decisions:read-all. Check GET /api/v1/me — you may be "
                "using a different seat's key file than you intended.",
            )
        found = [
            (r.decision, r.agent, r.artifact_title)
            for r in decisions.across_seats(
                db, limit=_ALL_OPEN, include_set_aside=include_set_aside
            )
        ]
    else:
        # A signed-in MCP app sees the questions on every page it may read, not only
        # those on its own agent's pages (MCP design 11.2). A key's reach widens nothing.
        wide = reach.widened(agent)
        every = wide is not None and wide.reads_all()
        with db.connect() as conn:
            pages = {
                r[0]: (r[1], r[2])
                for r in conn.execute(
                    "select id, title, agent from artifact where agent = %s"
                    " or (%s and deleted_at is null)",
                    (agent, every),
                ).fetchall()
            }
        found = [
            (
                d,
                pages.get(d.artifact_id, ("", agent))[1],
                pages.get(d.artifact_id, ("", agent))[0],
            )
            for d in decisions.list_for_agent(
                db,
                agent,
                limit=_ALL_OPEN,
                kind="question",
                include_set_aside=include_set_aside,
                any_agent=every,
            )
        ]
    now = datetime.now(UTC)
    found = [f for f in found if f[0].kind == "question"]
    ranked = [
        (
            (d.set_aside, bool(d.held_by), inbox.key(d, now)),
            {
                **decisions.public(d),
                "agent": who,
                "artifact_title": title,
                "overdue": inbox.is_overdue(d, now),
            },
        )
        for d, who, title in found
    ]
    if scope == "all":
        # Questions whose home is the partner (asked in Slack), read-only
        # and marked with their source, in the inbox's own order among the
        # ready ones. One he has already answered here shows its pending
        # answer until the home settles it.
        for waiting in (True, False):
            for c in readside.open_copies(db, _ALL_OPEN, waiting_on_him=waiting):
                ranked.append(((False, False, inbox.key(c, now)), _slack_view(c, now)))
    ranked.sort(key=lambda r: r[0])
    return [view for _, view in ranked]


_CHECKS_BUSY = (
    "the server was busy reading other pages and could not read this one in the time "
    "allowed: these checks did not run, so their silence on this page means nothing. "
    "Publish the page again in a few seconds to run them"
)


def _checks_not_run_busy() -> list[dict]:
    """The two warnings a page gets when every scan slot stayed taken: the ones a page
    too large for the checks gets (`csp.check`, `traps.check`), with the reason this time."""
    return [
        csp.CspWarning(shape=shape, line=1, hint=_CHECKS_BUSY).__dict__
        for shape in ("csp_checks_not_run", "trap_checks_not_run")
    ]


def page_warnings(db: Database, artifact_id: str, body: str) -> list[dict]:
    """CSP advisories, plus every decision slot that names no decision here.

    One list because they are one kind of fact: something wrong with the
    page you just published that you cannot see, since the rendered page
    only answers a framed request holding a capability token no agent is
    given (homelab-ij8.35). Publish time is the one moment the author is
    definitely looking (homelab-ij8.50).
    """
    try:
        # A slot for the reading, not for the database look-up below. One budget for the
        # page checks of a request: they draw on it together.
        with scanslots.slot(len(body)):
            budget = renderfind.Budget()
            out = (
                csp.as_json(body, budget)
                + traps.as_json(body, budget)
                + forms.warnings(body)
            )
    except scanslots.ScanBusy:
        out = _checks_not_run_busy()
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


SECTION_REFUSALS = (
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


_COMMENTS_REQUIRED = {
    "field": "comments",
    "detail": (
        'a new page must say whether commenting starts on or off: send comments: "on" or '
        '"off" (ream publish FILE --comments on|off). Choose off for a page a script '
        "redraws or a status board; the reader can still turn commenting on for that page. "
        "If your tool does not know this flag, it is out of date: reinstall it with "
        "the install line in /api/v1/agents.txt, exactly as written there. "
        "An update of an existing page does not need it."
    ),
}


def _saying_who_reads(result: dict) -> dict:
    """`result` plus `agent_read`: whether other agents can read the page, in plain words, beside
    the `agents_can_read` flag. Every publish and update says it (12 Nov 2026), because a page is
    open to agents by default and a publisher cannot see that from the outside."""
    return {
        **result,
        "agent_read": textvars.agent_read_words(bool(result["agents_can_read"])),
    }


def publish_page(
    db: Database, agent: str, seat_session: str | None, payload: PublishBody
) -> dict:
    """POST /pages, for the route and the MCP `publish_page` tool."""
    body, title = payload.body, payload.title
    if payload.comments is None and clientlevel.COMMENTS_REQUIRED:
        raise HTTPException(422, _COMMENTS_REQUIRED)
    if payload.format == "html" and title is None:
        # As before markdown: an HTML publish names its page.
        raise HTTPException(422, {"field": "title", "detail": "title is required"})
    if payload.format == "markdown":
        body, named = _markdown_page(payload.body, title)
        if not named:
            raise HTTPException(422, {"field": "title", "detail": _NO_MARKDOWN_TITLE})
        title = named
    try:
        published = store.publish(
            db,
            agent,
            title,
            body,
            session=seat_session,
            folder=payload.folder,
            folder_title=payload.folder_title,
            refresh_every=payload.refresh_every,
            comments=payload.comments,
            agents_can_read=payload.agents_can_read,
        )
        # After the write, never inside it: a publish must not wait on a
        # browser, nor fail because of one. See thumbs.render_in_background.
        thumbs.render_in_background(db, published.id, published.version, body)
        partchanges.in_background(db, published.id, published.version)
        return _saying_who_reads(
            {
                **published.__dict__,
                "comments": payload.comments,
                "warnings": page_warnings(db, published.id, body),
            }
        )
    except store.BadInterval as exc:
        raise HTTPException(422, _BAD_INTERVAL) from exc
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
    except folders.BadSlug as exc:
        raise HTTPException(422, _BAD_SLUG) from exc
    except folders.FolderArchived as exc:
        raise HTTPException(409, _archived_folder(exc.slug)) from exc


def update_page(
    db: Database,
    agent: str,
    seat_session: str | None,
    artifact_id: str,
    payload: PatchBody,
) -> dict:
    """PATCH /pages/{id}, for the route and the MCP `update_page` tool."""
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
            note=store.page_note(payload.note, agent=agent, artifact_id=artifact_id),
            folder=payload.folder,
            folder_title=payload.folder_title,
            refresh_every=(
                payload.refresh_every
                if "refresh_every" in payload.model_fields_set
                else store.KEEP
            ),
            comments=(payload.comments if payload.comments is not None else store.KEEP),
        )
        # After the write, never inside it: a publish must not wait on a
        # browser, nor fail because of one. See thumbs.render_in_background.
        thumbs.render_in_background(db, published.id, published.version, body)
        partchanges.in_background(db, published.id, published.version)
        return _saying_who_reads(
            {
                **published.__dict__,
                "warnings": page_warnings(db, published.id, body),
            }
        )
    except store.BadInterval as exc:
        raise HTTPException(422, _BAD_INTERVAL) from exc
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
    except folders.BadSlug as exc:
        raise HTTPException(422, _BAD_SLUG) from exc
    except folders.FolderArchived as exc:
        raise HTTPException(409, _archived_folder(exc.slug)) from exc
    except store.RevisionMismatch as exc:
        raise HTTPException(
            409, "the artifact moved on; read it and patch again"
        ) from exc
    except store.NotFound as exc:
        raise HTTPException(404, "no such page") from exc


def update_page_section(
    db: Database,
    agent: str,
    seat_session: str | None,
    artifact_id: str,
    element_id: str,
    payload: SectionBody,
) -> dict:
    """PATCH /pages/{id}/sections/{el}, for the route and the MCP `update_page` tool."""
    from ream import sections

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
    except (sections.SectionError, *SECTION_REFUSALS) as exc:
        raise section_refusal(exc) from exc
    published = result.published
    if not published.unchanged:
        thumbs.render_in_background(db, published.id, published.version, result.body)
        partchanges.in_background(db, published.id, published.version)
    return _saying_who_reads(
        {
            "page_revision": published.revision,
            "section_revision": result.section_revision,
            "version": published.version,
            "unchanged": published.unchanged,
            "section": result.label,
            "page_url": published.page_url,
            "bytes": published.bytes,
            "agents_can_read": published.agents_can_read,
            "warnings": page_warnings(db, published.id, result.body),
        }
    )


def move_to_folder(db: Database, agent: str, payload: MoveBody) -> dict:
    """POST /pages/move, for the route and the MCP `organise_page` tool."""
    try:
        moved, missing, filed = store.move_many(
            db, agent, payload.ids, payload.folder, payload.folder_title
        )
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
    except folders.BadSlug as exc:
        raise HTTPException(422, _BAD_SLUG) from exc
    except folders.FolderArchived as exc:
        raise HTTPException(409, _archived_folder(exc.slug)) from exc
    return {"moved": moved, "missing": missing, "folder": filed}


def archive_page(db: Database, agent: str, artifact_id: str, archived: bool) -> dict:
    """POST /pages/{id}/archive, for the route and the MCP `organise_page` tool."""
    if not store.set_archived(db, artifact_id, archived, agent=agent):
        raise HTTPException(404, "no such page")
    return store.get_owned(db, agent, artifact_id).__dict__


def rename_page(db: Database, agent: str, artifact_id: str, title: str) -> dict:
    """POST /pages/{id}/rename, for the route and the MCP `organise_page` tool."""
    try:
        renamed = store.rename(db, artifact_id, title, agent=agent)
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
        raise HTTPException(404, "no such page")
    return store.get_owned(db, agent, artifact_id).__dict__


def pin_page(db: Database, agent: str, artifact_id: str, pinned: bool) -> dict:
    """POST /pages/{id}/pin, for the route and the MCP `organise_page` tool."""
    if not store.set_pinned(db, artifact_id, pinned, agent=agent):
        raise HTTPException(404, "no such page")
    return store.get_owned(db, agent, artifact_id).__dict__


def comments_page(db: Database, agent: str, artifact_id: str, comments: str) -> dict:
    """POST /pages/{id}/comments, for the route and the MCP `organise_page` tool."""
    if not store.set_comments(db, artifact_id, comments, agent=agent):
        raise HTTPException(404, "no such page")
    return store.get_owned(db, agent, artifact_id).__dict__


def agents_can_read_page(
    db: Database, agent: str, artifact_id: str, can_read: bool
) -> dict:
    """POST /pages/{id}/agents, for the route and the MCP `organise_page` tool (open_to_agents,
    close_to_agents). "Not yours" answers as "no such page"."""
    if not store.set_agents_can_read(db, artifact_id, can_read, agent=agent):
        raise HTTPException(404, "no such page")
    return store.get_owned(db, agent, artifact_id).__dict__


def delete_page(db: Database, agent: str, artifact_id: str) -> dict:
    """DELETE /pages/{id}, for the route and the MCP `delete_page` tool."""
    if not store.set_deleted(db, artifact_id, True, agent=agent):
        raise HTTPException(404, "no such page")
    return {"id": artifact_id, "deleted": True}


def restore_page(db: Database, agent: str, artifact_id: str) -> dict:
    """POST /pages/{id}/restore, for the route and the MCP `organise_page` tool."""
    if not store.set_deleted(db, artifact_id, False, agent=agent):
        raise HTTPException(404, "no such page")
    return store.get_owned(db, agent, artifact_id).__dict__


def comment_resolve(db: Database, agent: str, comment_id: str) -> dict:
    """POST /comments/{id}/resolve, for the route and the MCP `comment` tool."""
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
    except comments.ReplyOwed as exc:
        raise HTTPException(
            409,
            "reply first: an explain, shorten or fix thread is always owed a reply, "
            "even a bare tap. Post your reply under the thread, then settle it.",
        ) from exc


def comment_reopen(db: Database, agent: str, comment_id: str) -> dict:
    """POST /comments/{id}/reopen, for the route and the MCP `comment` tool."""
    try:
        # `agent=` EXPLICITLY, for the reason spelled out on resolve above:
        # the default would skip the ownership check.
        return comments.reopen(db, comment_id, agent=agent, by=agent).__dict__
    except store.NotFound as exc:
        raise HTTPException(404, "no such comment") from exc


def _deleted_view(row: comments.CommentRow) -> dict:
    """What the delete and restore routes answer: the comment (its words blank while it is
    deleted), and until when a deletion may be taken back."""
    out = row.__dict__
    out["restorable_until"] = (
        (datetime.fromisoformat(row.deleted_at) + comments.RESTORE_WINDOW).isoformat()
        if row.deleted_at
        else None
    )
    return out


def comment_delete(db: Database, agent: str, comment_id: str) -> dict:
    """DELETE /comments/{id}, for the route and the MCP `comment` tool. Soft, and only a
    comment this agent wrote: someone else's and a missing one answer alike (404)."""
    try:
        row = comments.delete(db, comment_id, author_kind="agent", author=agent)
    except store.NotFound as exc:
        raise HTTPException(404, "no such comment") from exc
    return _deleted_view(row)


def comment_restore(db: Database, agent: str, comment_id: str) -> dict:
    """POST /comments/{id}/restore, for the route and the MCP `comment` tool: take back a
    deletion of your own comment, within `comments.RESTORE_WINDOW` of it."""
    try:
        row = comments.restore(db, comment_id, author_kind="agent", author=agent)
    except store.NotFound as exc:
        raise HTTPException(404, "no such comment") from exc
    except comments.RestoreExpired as exc:
        raise HTTPException(
            409,
            "too late to restore: a deleted comment can be taken back for "
            f"{int(comments.RESTORE_WINDOW.total_seconds() // 60)} minutes only.",
        ) from exc
    return _deleted_view(row)


def ask_question(
    db: Database,
    agent: str,
    seat_session: str | None,
    artifact_id: str,
    payload: DecisionBody,
) -> dict:
    """POST /pages/{id}/decisions, for the route and the MCP `ask` tool."""
    try:
        row = decisions.ask(
            db,
            agent=agent,
            artifact_id=artifact_id,
            question=payload.question,
            options=payload.options,
            recommended=payload.recommended,
            session=seat_session,
            context=payload.context,
            topic=payload.topic,
            depends_on=payload.depends_on,
            replaces=payload.replaces,
            urgency=payload.urgency,
            blocked_on=payload.blocked_on,
            needed_by=payload.needed_by,
            for_offices=payload.audience_sent()[0],
            audience_field=payload.audience_sent()[1],
        )
        out = decisions.public(row)
        if row.set_aside:
            out["warning"] = (
                f"this page is {row.set_aside_why}; the question is set aside "
                "until it is unarchived"
            )
        return out
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
    except decisions.InvalidField as exc:
        raise HTTPException(422, {"field": exc.field, "detail": exc.detail}) from exc
    except decisions.NotReplaceable as exc:
        raise HTTPException(
            409,
            {
                "field": exc.field,
                "state": exc.state,
                "replaced_by": exc.replaced_by,
                "detail": (
                    f"the decision you replace was already replaced by"
                    f" {exc.replaced_by}: if your earlier ask got no reply,"
                    " that question is the one it created"
                    if exc.replaced_by
                    else f"the decision you replace is already {exc.state}, so"
                    " there is nothing left to replace: ask without"
                    " `replaces`, or read its answer"
                ),
            },
        ) from exc
    except store.NotFound as exc:
        raise HTTPException(404, "no such page") from exc


def close_question(db: Database, agent: str, decision_id: str) -> dict:
    """POST /decisions/{id}/close, for the route and the MCP `close_question` tool."""
    try:
        return decisions.public(decisions.close(db, agent, decision_id))
    except store.NotFound as exc:
        raise HTTPException(404, "no such decision") from exc


def record_relay(
    db: Database,
    agent: str,
    decision_id: str,
    payload: RecordBody,
    *,
    by_app: bool = False,
) -> dict:
    """POST /decisions/{id}/record, for the route and the MCP `record_answer` tool.

    `by_app`: a signed-in app passing on what the person said in its chat (MCP design
    11.5). The source is then `mcp` and only `mcp`, the question may be on any page, and
    it is stored as the same relay a key's is, never as a tap. A key's route never sets it."""
    said_at = None
    if payload.said_at:
        try:
            said_at = datetime.fromisoformat(payload.said_at)
        except ValueError:
            raise HTTPException(422, "said_at must be an ISO 8601 timestamp") from None
    found = (
        decisions.get_any(db, decision_id, within_reach_of=agent)
        if by_app
        else decisions.get_owned(db, agent, decision_id)
    )
    if by_app and found is None:
        # Outside the app's reach (or no such question): one answer for both.
        raise HTTPException(404, "no such open decision")
    if found is not None and found.kind == "form":
        return _record_form(db, found, payload, said_at, agent, by_app=by_app)
    if payload.fields is not None:
        if found is None:
            raise HTTPException(404, "no such open decision")
        raise HTTPException(422, "`fields` is for a form; send a letter")
    if payload.letter is None:
        raise HTTPException(
            422, textvars.render("send the letter {{owner_they}} chose")
        )
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
            by_app=by_app,
        )
    except decisions.InvalidSource:
        raise HTTPException(
            422,
            "source must be one of "
            f"{', '.join(decisions.relay_sources())} — 'page' is refused, "
            + textvars.render(
                "because it means {{owner_name}} answered on the page "
                "{{owner_themselves}}"
            ),
        ) from None
    except decisions.InvalidAnswer:
        raise HTTPException(422, "that letter is not one of the options") from None
    except decisions.TapStands as exc:
        raise HTTPException(
            409,
            {
                "field": "answer",
                "detail": textvars.render(
                    "{{owner_name}} already answered this on the page "
                    f"({exc.row.answer}); an app does not replace it. If that "
                    "changed, let {{owner_them}} answer it again on the page."
                ),
            },
        ) from None
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
    return decisions.public(row)


def section_refusal(exc: Exception) -> HTTPException:
    """A section update's refusals, as the whole-page update words them
    wherever the cause is the same."""
    from ream import sections

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
                    f"{exc.decision_id} ({exc.question!r}), "
                    + textvars.render(
                        "which {{owner_they}} {{owner_has}} not answered. "
                        "Keep its data-artifact-decision slot, "
                        "or close the question first. Nothing was changed."
                    )
                ),
            },
        )
    if isinstance(exc, store.PageChanged):
        return HTTPException(409, exc.message)
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
        return HTTPException(503, {"field": exc.field, "detail": _SCAN_FAILED_DETAIL})
    if isinstance(exc, store.NotFound):
        return HTTPException(404, "no such page")
    raise exc


def _element_anchor(db: Database, artifact_id, anchor, agent) -> tuple[dict, str]:
    """An agent's `element` anchor, `{"kind": "element", "id": "..."}`, into
    the stored shape: the id must be on the current version exactly once
    (`sections.locate` says if it is not), and the server fills the tag,
    the heading it sits under, the words either side and the quote from
    the page, which an agent cannot measure."""
    bad = HTTPException(
        422,
        {
            "field": "anchor",
            "detail": 'an element anchor is {"kind": "element", "id": "<the '
            "element's id, which is on the page exactly once>\"}",
        },
    )
    element_id = anchor.get("id")
    if (
        set(anchor) - {"kind", "id"}
        or not isinstance(element_id, str)
        or not anchors.ID_RE.fullmatch(element_id)
    ):
        raise bad
    current = store.get_body(db, agent, artifact_id, as_reader=True)
    try:
        found = sections.locate(current.body, element_id)
        # A label read with a regular expression, outside the parser, can be too
        # long to read as well (`htmlbound.unescape`): a refusal like the rest.
        return anchors.element_anchor(current.body, element_id, found)
    except sections.SectionError as exc:
        raise HTTPException(
            422, {"field": "anchor", "detail": str(exc), "matches": 0}
        ) from exc
    except htmlbound.TooComplex as exc:
        raise HTTPException(
            422,
            {
                "field": "anchor",
                "detail": "the element's label is too long to read -- name it by a"
                " text anchor instead",
                "matches": 0,
            },
        ) from exc


def _text_anchor(db: Database, artifact_id, payload, agent) -> tuple[dict | None, str]:
    """Turn an agent's `text` anchor into the stored shape: a `quote` anchor
    carrying the heading the server found the passage under, which the
    gallery paints and re-finds after a republish (an agent cannot compute
    the DOM path the browser's range anchor needs). Anything else passes
    through unchanged."""
    anchor = payload.anchor
    if (
        payload.thread_root is not None
        or not isinstance(anchor, dict)
        or anchor.get("kind") not in ("text", "element")
    ):
        return anchor, payload.quote
    if anchor.get("kind") == "element":
        return _element_anchor(db, artifact_id, anchor, agent)
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
    heading, heading_id = anchors.heading_for_quote(current.body, quote, section)
    found: dict = {"kind": "quote"}
    if heading:
        found["heading"] = heading
    if heading_id:
        found["heading_id"] = heading_id
    return found, quote


def refuse_decision_anchor(payload: CommentBody) -> None:
    """A new thread under his answer is started at /decisions/{id}/replies, never by a
    comment: refused on POST /pages/{id}/comments and the MCP `comment` tool alike."""
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
                    textvars.render(
                        "a thread under {{owner_their}} answer is started with "
                    )
                    + "POST /api/v1/decisions/{decision_id}/replies"
                ),
            },
        )


def add_comment(
    db: Database,
    artifact_id: str,
    payload: CommentBody,
    agent: str,
    conn,
    seat_session: str | None,
    later: list,
) -> dict:
    """An agent's comment, as POST /pages/{id}/comments and a reply under his answer
    write it (the MCP `comment` tool too). `later` collects what runs once the write
    has committed (the nudges and the second scan); the caller runs it after answering.
    `conn` is the idempotency transaction, or None."""
    try:
        anchor, quote = _text_anchor(db, artifact_id, payload, agent)
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
            image_ids=payload.images,
        )
        # Where the pin stands on the page now, in the reply: the agent
        # learns at write time, not on its next listing.
        row = comments.with_state(db, row, conn)
        # GitGuardian checks the saved comment straight after the response.
        later.append(partial(comments.second_scan, db, row.id))
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
    except images.ImageRefused as exc:
        raise HTTPException(
            exc.status, {"field": exc.field, "detail": exc.detail}
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
                    "kind must be comment, remove, looks_good, explain, shorten "
                    "or fix, with a quoted range for all but comment"
                ),
            },
        ) from exc
    except comments.VerdictRefused as exc:
        raise HTTPException(
            422,
            {
                "field": exc.field,
                "detail": textvars.render(
                    "on a page you do not own only kind comment is allowed: "
                    "remove, looks_good, explain, shorten and fix are "
                    "{{owner_name}}'s. Say it in the body of a plain comment instead."
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
        raise HTTPException(404, "no such page") from exc


def _record_form(
    db: Database,
    found,
    payload: RecordBody,
    said_at,
    agent: str,
    *,
    by_app: bool = False,
):
    """The per-field relay of a form (formanswer.relay): every field stored with its
    source, none of it his own submit, none over a later submit of his."""
    if (
        payload.source != decisions.MCP_SOURCE
        if by_app
        else (payload.source not in decisions.relay_sources())
    ):
        raise HTTPException(
            422,
            "source must be one of "
            f"{', '.join(decisions.relay_sources())} — 'page' is refused, "
            + textvars.render(
                "because it means {{owner_name}} answered on the page "
                "{{owner_themselves}}"
            ),
        )
    if payload.letter is not None or not payload.fields:
        raise HTTPException(
            422, "a form is relayed per field: send fields {name: value}"
        )
    try:
        done = formanswer.relay(
            db,
            found.id,
            fields_given=payload.fields,
            note=payload.note,
            source=payload.source,
            recorded_by=agent,
            said_at=said_at,
            keep_taps=by_app,
        )
    except formanswer.SubmitRefused as exc:
        # 422 a bad value; 409 the form changed while this was relayed: send it again.
        raise HTTPException(
            exc.status, {"detail": exc.detail, "field": exc.field}
        ) from None
    except store.NotFound:
        raise HTTPException(404, "no such open decision") from None
    except store.BodyTooLarge as exc:
        raise HTTPException(
            413,
            {"field": exc.field, "detail": f"{exc.field} exceeds the size limit"},
        ) from None
    except store.SecretFound as hit:
        raise HTTPException(422, _refused_detail(hit.field, hit.findings)) from hit
    except store.ScanFailed as exc:
        raise HTTPException(
            503, {"field": exc.field, "detail": _SCAN_FAILED_DETAIL}
        ) from exc
    row = decisions.get_any(db, found.id)
    out = decisions.public(row)
    out["relayed"] = {"applied": done["applied"], "skipped": done["skipped"]}
    return out


def agent_router(
    db: Database, limiter: ratelimit.FailedAuthLimiter | None = None
) -> APIRouter:
    router = _BothWords(prefix="/api/v1")

    # One limiter per app, SHARED when `create_app` mounts this router on both
    # the gallery host and the API host: otherwise switching names would double
    # a caller's budget. homelab-ij8.100; the reasoning is in `ratelimit`.
    limiter = limiter or ratelimit.FailedAuthLimiter()

    def caller(request: Request) -> str:
        return authenticate(db, limiter, request).agent

    SESSION_MAX = _SESSION_MAX
    session = declared_session

    def _warnings_for(artifact_id: str, body: str) -> list[dict]:
        return page_warnings(db, artifact_id, body)

    @router.get("/agents.txt", response_class=PlainTextResponse)
    def onboarding_doc(request: Request):
        """The onboarding document, and the ONE route here that takes no key.

        Deliberately unauthenticated, and deliberately under /api/v1 as well as
        at /agents.txt on the public API host: /api/v1 is the only prefix
        Authelia bypasses, so this is the only way an agent on the local network — which
        holds no sign-in session and never will — can read it at all. Asking
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

    @router.get("/agent.json")
    def discovery(request: Request):
        """The onboarding document as data (runcove-5t3oj.28): hosts, auth, the
        open routes and where the keyed full list is, for a client without the tools. Open for the same
        reason as agents.txt. Its hosts come from configuration, never from the
        request, which is what every office asked for."""
        return agent_card_response(request)

    def _base(request: Request) -> str:
        host = hosts.first_host_header(request.scope) or "artifacts.invalid"
        if hosts.is_former(hosts.normalise(host)):
            # The old address never advertises itself: the document teaches the new one.
            return store.gallery_origin()
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
        """The page uv's `--find-links` reads, so an install line never changes between
        deploys (the homelab ops seat, 24 Sep 2026: "a stable alias would make uv tool
        install --force a one-liner"). A redirect or a fixed filename cannot do this --
        measured: uv refuses a URL without `.whl`, and a wheel filename without a version.
        One link to the one package, and a second to the same code under the name `ream`, so
        a tool installed before the install name became `ream-artifacts` can still update
        itself (`onboarding.wheels`). Still never a directory listing: it names only what
        `wheels()` names."""
        built = onboarding.wheels()
        if not built:
            raise HTTPException(404, "no tools are available from this address")
        links = "".join(
            f'<a href="{html.escape(each.name)}">{html.escape(each.name)}</a>\n'
            for each in built
        )
        return HTMLResponse(
            f"<!doctype html><title>tools</title>{links}",
            headers={"x-content-type-options": "nosniff", "cache-control": "no-cache"},
        )

    @router.get("/tools/{name}")
    def tools(name: str):
        """The tool package, open: an agent on another machine installs it before
        it has any key (homelab-ij8.68). Only the files the image built, by exact
        name -- never a path, never a listing. It contains no secret: it is
        this repository's code, which is also in the image."""
        built = {each.name: each for each in onboarding.wheels()}.get(name)
        if built is None:
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
        not something any device on the local network should be able to ask for, and
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

    @router.post("/agents/me/move", status_code=201)
    def move_agent(request: Request, agent: str = Depends(caller)):
        """Start moving THIS agent to a new machine, without the owner (9 Nov 2026). Scoped
        to the calling key's own agent: there is no way to name another. Returns a one-time
        `move_token`; the new machine runs `ream connect --move-token-stdin` with it and
        collects a key for this same agent, so its pages, comments and decisions stay.
        Only the calling key retires, at the new key's first use; the agent's other keys keep
        working. A house seat's vaulted key is refused (409), as for /keys/rotate. The token is the credential: hand it only to the new machine. It lives 15
        minutes, and asking again cancels an earlier unclaimed one."""
        # The same gate as /keys/rotate: a house seat's vaulted key is not swapped by tools,
        # and may not retire itself by a move either.
        if not request.state.agent_key.rotating:
            raise HTTPException(
                409, "this key is not a rotating key (a house seat's vaulted key)"
            )
        address = ratelimit.client_address(
            request.headers, request.client.host if request.client else None
        )
        started = connect.self_move(db, agent, address, request.state.agent_key.id)
        return JSONResponse(
            {
                "agent": agent,
                "move_token": started.poll_token,
                "expires_in": started.expires_in,
                "interval": 5,
            },
            status_code=201,
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
        Every agent on the management machine reads ~/.config/artifacts/key, which belongs to ONE
        seat, so a key holder's assumption about which seat it is can be wrong
        for weeks without anything saying so — and a seat grant is then applied
        to an identity nobody is actually using. Proposed by the Warpgate seat
        after exactly that cost an afternoon, and it is right: this turns a
        multi-step database dig into one line of output.

        Returns no secret. The key's own bytes are never echoed, and the id is
        the public handle `artifacts-admin list-keys` already prints.
        """
        return me_view(db, request, agent, seat_session)

    @router.get("/templates")
    def template_index(agent: str = Depends(caller)):
        """The templates an agent can start a page from, with what each is for.

        Behind the key like everything else. Not because a template is secret --
        it is checked into a repo -- but because an unauthenticated route on this
        prefix is a route any device on the local network can reach, and every consumer
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
        return publish_page(db, agent, seat_session, payload)

    @router.patch("/artifacts/{artifact_id}")
    def update(
        artifact_id: str,
        payload: PatchBody,
        agent: str = Depends(caller),
        seat_session: str | None = Depends(session),
    ):
        return update_page(db, agent, seat_session, artifact_id, payload)

    @router.get("/artifacts/{artifact_id}/sections/{element_id}")
    def read_section(artifact_id: str, element_id: str, agent: str = Depends(caller)):
        """One element of the live page, to edit what is live rather than a
        local copy. Any agent may read it when the page is open to agents, as
        it may read the whole page (10 Oct 2026); only the publisher may update it."""
        from ream import sections

        try:
            outer, rev, live = store.get_section(
                db, agent, artifact_id, element_id, as_reader=True
            )
        except (sections.SectionError, *SECTION_REFUSALS) as exc:
            raise section_refusal(exc) from exc
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
        return update_page_section(
            db, agent, seat_session, artifact_id, element_id, payload
        )

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
            raise HTTPException(404, "no such page") from exc

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
            raise HTTPException(404, "no such page") from exc
        report = store.render_report(db, artifact_id)
        return {
            **stored.__dict__,
            "warnings": _warnings_for(artifact_id, stored.body),
            # What the service's own render made of the live version (runcove-5t3oj.56).
            "render_verdict": report["verdict"],
            "render_findings": report["findings"],
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
        try:
            size = len(body.encode())
        except UnicodeEncodeError as exc:  # a lone surrogate from JSON's \ud800
            raise HTTPException(
                422, {"field": "body", "detail": "body is not valid UTF-8"}
            ) from exc
        if size > store.MAX_BODY_BYTES:
            raise HTTPException(
                413, f"body is larger than {store.MAX_BODY_BYTES} bytes"
            )
        try:
            store.check_body(body, agent=agent)
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
        width = max(320, min(payload.width, 1600))
        height = max(320, min(payload.height, 4000))
        if not thumbs.available():
            return {"rendered": False, "renderer": False, "errors": None}
        if not _PREVIEW_SLOT.acquire(timeout=PREVIEW_WAIT_SECONDS):
            raise HTTPException(
                503, "another preview is rendering; try again in a few seconds"
            )
        try:
            report = thumbs.render_report(body, width, height)
            png, errors = report
        finally:
            _PREVIEW_SLOT.release()
        verdict, findings = "unchecked", []
        reason = getattr(report, "reason", None)
        if reason in renderwire.REASONS:
            verdict, findings = renderfind.renderer_unavailable(reason)
        elif errors is not None:
            missing, live = thumbs.scan_for_findings(body)
            verdict, findings = renderfind.assess(
                errors, getattr(report, "check", None), missing, live
            )
        answer = {
            "rendered": png is not None,
            "renderer": True,
            "width": width,
            "height": height,
            "errors": errors,
            "verdict": verdict,
            "findings": findings,
            "png_base64": base64.b64encode(png).decode() if png else None,
        }
        if reason:
            # Why nothing was rendered, when it is not the page's doing: the
            # renderer (REAM_RENDERER_URL) gave no usable answer, or did not render.
            answer["reason"] = reason
        return answer

    @router.get("/artifacts/{artifact_id}/render")
    def read_render(artifact_id: str, agent: str = Depends(caller)):
        """What the live version did when the service rendered it: its uncaught
        script errors, which its author cannot otherwise see (homelab-ij8.72).
        `rendered: false` means not finished, or no browser here -- never
        "clean"."""
        try:
            store.get_owned(db, agent, artifact_id, as_reader=True)
        except store.NotFound as exc:
            raise HTTPException(404, "no such page") from exc
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
            raise HTTPException(404, "no such page") from exc

    @router.get("/artifacts/{artifact_id}/changes")
    def read_changes(
        artifact_id: str,
        frm: int = Query(..., alias="from", ge=1),
        to: int | None = Query(None, ge=1),
        format: str = Query("parts", pattern="^(parts|marked)$"),
        agent: str = Depends(caller),
    ):
        """Which parts of a page changed from version `from` to `to` (default: the live one)
        (runcove-5t3oj.56). `{from, to, parts: [{id?, label, change}], more, unknown_reason}`:
        `change` is added, changed or removed. A page this cannot read gets `unknown_reason` and
        no parts, never an empty list that reads as "nothing changed". A span of several rounds
        is ONE diff between its two ends. `?format=marked` returns the changes marked up in the
        new body (the reader's view) as `body`, with `reason` when it could not.
        Scoped like `/versions`; a page that is not yours answers like one that is not there."""
        try:
            end, old, new, stored = store.changes_pair(
                db, agent, artifact_id, frm, to, as_reader=True
            )
        except store.NotFound as exc:
            raise HTTPException(404, "no such page or version") from exc
        except store.BadSpan as exc:
            raise HTTPException(422, "from must not be after to") from exc
        if stored is not None:
            found = {k: v for k, v in stored.items() if k != "from"}
        elif frm == end:
            found = {"parts": [], "more": 0, "unknown_reason": None}
        else:
            found = partdiff.changes_safely(old, new)
            if end == frm + 1 and partchanges.keepable_from_a_read(found):
                store.save_part_changes(
                    db, artifact_id, end, partchanges.round_of(found, frm)
                )
        out = {"from": frm, "to": end, **found}
        if format == "marked":
            marked, reason = pagediff.render_changes_safely(old, new)
            out |= {"body": marked, "reason": reason}
        return out

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

    @router.get("/folders")
    def folder_index(agent: str = Depends(caller)):
        """This agent's own folders: slug, title, page count, open questions, archived."""
        return folders.list_for_agent(db, agent)

    @router.get("/folders/{slug}/artifacts")
    def folder_pages(slug: str, agent: str = Depends(caller)):
        """The pages in one of this agent's folders, in the row shape of `GET /artifacts`.
        A folder that is not this agent's answers exactly as one that does not exist."""
        rows = store.list_in_folder(db, agent, slug)
        if rows is None:
            raise HTTPException(404, "no such folder")
        return [row.__dict__ for row in rows]

    @router.post("/folders/{slug}/rename")
    def folder_rename(
        slug: str, payload: FolderRenameBody, agent: str = Depends(caller)
    ):
        """Give one of this agent's folders a new slug and/or title. The old slug keeps
        resolving. A slug already in use (or another folder's old slug) is a 409; a folder
        that is not this agent's is a 404, as is one that does not exist. Never creates."""
        if payload.slug is None and payload.title is None:
            raise HTTPException(
                422,
                {"field": "slug", "detail": "send a new slug, a new title, or both"},
            )
        try:
            got = store.rename_folder(db, agent, slug, payload.slug, payload.title)
        except store.BodyTooLarge as exc:
            # The request's own name for it: this route takes `title`.
            raise HTTPException(
                413,
                {
                    "field": "title",
                    "size": exc.size,
                    "detail": "title exceeds the size limit",
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
        except folders.BadSlug as exc:
            raise HTTPException(422, _BAD_SLUG) from exc
        except folders.SlugTaken as exc:
            raise HTTPException(
                409,
                {
                    "field": "slug",
                    "detail": f"folder name {exc.slug!r} is already in use (as a folder"
                    " or as the old name of another)",
                },
            ) from exc
        if got is None:
            raise HTTPException(404, "no such folder")
        return got

    @router.post("/folders/{slug}/archive")
    def folder_archive(
        slug: str, payload: FolderArchiveBody, agent: str = Depends(caller)
    ):
        """Archive one of this agent's folders and every live page in it (two steps: without
        `confirm: true` it only says what it would do), or unarchive it."""
        try:
            got = store.set_folder_archived(
                db, agent, slug, payload.archived, confirm=payload.confirm
            )
        except folders.BadSlug as exc:
            raise HTTPException(422, _BAD_SLUG) from exc
        if got is None:
            raise HTTPException(404, "no such folder")
        return got

    @router.post("/artifacts/move")
    def move_pages(payload: MoveBody, agent: str = Depends(caller)):
        """File several of this agent's own pages in one of its folders in one go, or take
        them out (`folder: null`). `missing` covers "no such page" and "not yours" alike."""
        return move_to_folder(db, agent, payload)

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
            raise HTTPException(404, "no such page")
        return {"id": artifact_id, "from": old, "to": store.owner(db, artifact_id)}

    @router.post("/artifacts/{artifact_id}/agents")
    def agents_can_read(
        artifact_id: str, payload: AgentsBody, agent: str = Depends(caller)
    ):
        """Open this agent's own page to every agent in his space, or close it to
        just him and this agent (homelab-ij8.105). The publisher's twin of his
        control in the gallery; nobody else's page can be changed from here."""
        return agents_can_read_page(db, agent, artifact_id, payload.can_read)

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
        return archive_page(db, agent, artifact_id, payload.archived)

    @router.post("/artifacts/{artifact_id}/rename")
    def rename(artifact_id: str, payload: RenameBody, agent: str = Depends(caller)):
        """Rename without republishing -- the whole body is not needed to fix a
        name, and an agent that no longer holds the file should not have to."""
        return rename_page(db, agent, artifact_id, payload.title)

    @router.post("/artifacts/{artifact_id}/pin")
    def pin(artifact_id: str, payload: PinBody, agent: str = Depends(caller)):
        """Pin one of this agent's own pages to the front of the wall."""
        return pin_page(db, agent, artifact_id, payload.pinned)

    @router.post("/artifacts/{artifact_id}/comments-setting")
    def comments_setting(
        artifact_id: str, payload: CommentsBody, agent: str = Depends(caller)
    ):
        """Say whether commenting starts on or off for readers of one of this agent's own pages.
        The reader's own choice for that page wins. A page that is not the caller's answers
        exactly as a missing one."""
        return comments_page(db, agent, artifact_id, payload.comments)

    @router.post("/artifacts/{artifact_id}/touch")
    def touch(
        artifact_id: str,
        payload: TouchBody | None = None,
        agent: str = Depends(caller),
        idempotency_key: str | None = Header(default=None),
    ):
        """Vouch for one of this agent's own pages without sending it: "checked, still current",
        or `status: failed` with a note. Makes no version, does not reorder the wall and marks
        nothing unread. `if_revision` refuses (409) a page that has moved on. Declaring
        `refresh_every` here counts as a check. A page that is not the caller's answers
        exactly as a missing one."""
        payload = payload or TouchBody()

        def write(conn) -> tuple[int, object]:
            try:
                done = store.touch(
                    db,
                    agent,
                    artifact_id,
                    status=payload.status,
                    note=payload.note,
                    if_revision=payload.if_revision,
                    refresh_every=(
                        payload.refresh_every
                        if "refresh_every" in payload.model_fields_set
                        else store.KEEP
                    ),
                    conn=conn,
                )
            except store.NotFound as exc:
                raise HTTPException(404, "no such page") from exc
            except store.PageChanged as exc:
                raise HTTPException(409, exc.message) from exc
            except store.BadInterval as exc:
                raise HTTPException(422, _BAD_INTERVAL) from exc
            except store.InvalidText as exc:
                raise HTTPException(
                    422,
                    {"field": exc.field, "detail": f"{exc.field} is not valid UTF-8"},
                ) from exc
            except store.SecretFound as found:
                raise HTTPException(
                    422, _refused_detail(found.field, found.findings)
                ) from found
            except store.ScanFailed as exc:
                raise HTTPException(
                    503, {"field": exc.field, "detail": _SCAN_FAILED_DETAIL}
                ) from exc
            return 200, {
                "checked_at": done.checked_at,
                "changed_at": done.changed_at,
                "revision": done.revision,
                "version": done.version,
                "status": done.status,
                "note": done.note,
                "refresh_every": done.refresh_every,
                "lapsed": done.lapsed,
                "lapses_at": done.lapses_at,
            }

        return idempotency.run(
            db,
            agent,
            f"touch:{artifact_id}",
            idempotency_key,
            payload.model_dump(),
            write,
        )

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
            raise HTTPException(404, "no such page") from exc
        thumbs.render_in_background(db, published.id, published.version, body)
        partchanges.in_background(db, published.id, published.version)
        return _saying_who_reads(published.__dict__)

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
        return delete_page(db, agent, artifact_id)

    @router.post("/artifacts/{artifact_id}/restore")
    def restore(artifact_id: str, agent: str = Depends(caller)):
        """Put one of this agent's deleted pages back, address and history
        intact."""
        return restore_page(db, agent, artifact_id)

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
            raise HTTPException(404, "no such page")
        return {"id": artifact_id, "destroyed": True}

    def _check_anchor_state(value: str | None) -> None:
        if value not in (None, *anchors.STATES):
            raise HTTPException(
                422,
                {
                    "field": "anchor_state",
                    "detail": "anchor_state is current, edited or outdated",
                },
            )

    def _in_state(row, wanted: str | None) -> bool:
        return wanted is None or row.anchor_state == wanted

    @router.get("/artifacts/{artifact_id}/comments")
    def list_comments(
        artifact_id: str,
        include_resolved: bool = False,
        anchor_state: str | None = None,
        agent: str = Depends(caller),
    ):
        """Every comment thread on a page the caller may read. Threads under
        his decision notes (`anchor.kind == "decision"`) are included on
        purpose: they are comments, and `artifacts-decide` labels them.
        Each row says where its pin is on the page as it is now (`anchor_state`:
        current, edited or outdated; `anchor_note` when it moved);
        `?anchor_state=outdated` lists only the spots that are gone."""
        _check_anchor_state(anchor_state)
        # A page the caller may READ: its own, or another agent's that is open
        # to agents. A closed, deleted or unknown page answers the same 404.
        try:
            store.get_owned(db, agent, artifact_id, as_reader=True)
        except store.NotFound as exc:
            raise HTTPException(404, "no such page") from exc
        # Another office's page shows every comment, his included: access to
        # the page is the one control (Jeremy, 21 Oct 2026). A held mark stays
        # out, as it does for the owner.
        rows = comments.list_for_artifact(
            db, artifact_id, include_resolved=include_resolved
        )
        return [row.__dict__ for row in rows if _in_state(row, anchor_state)]

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
        is allowed (`remove`, `looks_good`, `explain`, `shorten` and `fix` are Jeremy's), and a comment never
        answers, acknowledges or closes a decision. `anchor` may be
        `{"kind": "text", "quote": ..., "section": optional heading}`: the
        server finds the passage in the page's current version."""
        refuse_decision_anchor(payload)
        # Run once the keyed transaction has committed; empty on a replay.
        later: list = []

        def write(conn) -> tuple[int, object]:
            later.clear()  # a retried attempt must not send the last one's nudge
            return 201, add_comment(
                db, artifact_id, payload, agent, conn, seat_session, later
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

    @router.post("/artifacts/{artifact_id}/comment-images", status_code=201)
    async def upload_comment_image(
        artifact_id: str, request: Request, agent: str = Depends(caller)
    ):
        """Stage one picture for a comment on a page the caller may read: multipart,
        one file in the field `file`. PNG, JPEG or WebP, decided from the bytes; at most
        5 MB, 4000 px a side and 16 megapixels. The stored file is a rewrite without
        EXIF or text chunks. Returns `{image_id, content_type, width, height, byte_size}`;
        name the id in `images` on a comment within the hour. The same bytes from the same
        caller give the same id. Pictures are not checked for secrets."""

        def readable() -> bool:
            with db.connect() as conn:
                return store._owned(conn, artifact_id, agent, True) is not None

        # Cheapest first: a page the caller cannot read costs no body.
        if not await run_in_threadpool(readable):
            raise HTTPException(404, "no such page")
        try:
            return await image_store.stage_upload(request, db, artifact_id, agent)
        except images.ImageRefused as exc:
            raise HTTPException(
                exc.status, {"field": exc.field, "detail": exc.detail}
            ) from exc

    @router.get("/comments/{comment_id}/images/{image_id}")
    def download_comment_image(
        comment_id: str, image_id: str, agent: str = Depends(caller)
    ):
        """A picture on a comment, for a caller that may read the comment's page and
        sees the comment (not held, not withdrawn). Anything else, an unknown picture
        included, is the same 404. Sent as an attachment with the type taken from the
        bytes, `nosniff` and a sandbox CSP; this origin sets no cookie."""
        found = image_store.for_reader(db, agent, comment_id, image_id)
        if found is None:
            raise HTTPException(404, "no such image")
        return Response(
            found[1],
            media_type=found[0],
            headers={
                **image_store.SERVE_HEADERS,
                "content-disposition": "attachment",
                "cache-control": "private, no-store",
            },
        )

    @router.post("/comments/{comment_id}/resolve")
    def resolve_comment(comment_id: str, agent: str = Depends(caller)):
        return comment_resolve(db, agent, comment_id)

    @router.post("/comments/{comment_id}/reopen")
    def reopen_comment(comment_id: str, agent: str = Depends(caller)):
        return comment_reopen(db, agent, comment_id)

    @router.delete("/comments/{comment_id}")
    def delete_comment(comment_id: str, agent: str = Depends(caller)):
        """Delete a comment you wrote (any page you commented on). Soft: readers stop seeing
        it, and `POST /comments/{id}/restore` takes it back for ten minutes. Under a thread
        others replied in, a "comment deleted" placeholder stays; a thread with nothing live
        left disappears."""
        return comment_delete(db, agent, comment_id)

    @router.post("/comments/{comment_id}/restore")
    def restore_comment(comment_id: str, agent: str = Depends(caller)):
        return comment_restore(db, agent, comment_id)

    @router.get("/comments")
    def list_all_comments(
        include_resolved: bool = False,
        anchor_state: str | None = None,
        agent: str = Depends(caller),
    ):
        """Every comment across the pages the caller owns, newest first.
        Threads under his decision notes (`anchor.kind == "decision"`) are
        included on purpose; `artifacts-decide` labels them. `anchor_state` as
        on a page's comments."""
        _check_anchor_state(anchor_state)
        return [
            row.__dict__
            for row in comments.list_for_agent(
                db, agent, include_resolved=include_resolved
            )
            if _in_state(row, anchor_state)
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
            raise HTTPException(404, "no such page") from exc
        return decisions.public(row)

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
        return ask_question(db, agent, seat_session, artifact_id, payload)

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
        # its open questions. Since 2 Nov that is no longer so: archiving or
        # binning SETS THEM ASIDE (rows carry `set_aside`, `set_aside_since`,
        # `reopened_at`) and they come back on unarchive or restore; a
        # withdrawal is an agent's own `close`.
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
                raise HTTPException(404, "no such page") from exc
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
        form_info = forms.for_decisions(db, [d.id for d in shown if d.kind == "form"])
        return JSONResponse(
            jsonable_encoder(
                [
                    {
                        **decisions.public(row),
                        **(
                            {"form": form_info.get(row.id)}
                            if row.kind == "form"
                            else {}
                        ),
                        "outcomes": [o.__dict__ for o in outcomes.get(row.id, [])],
                        "replies": [r.__dict__ for r in notes.get(row.id, [])],
                    }
                    for row in shown
                ]
            ),
            headers=_both_headers({"x-ream-answered-omitted": str(omitted)}),
        )

    @router.get("/artifacts/{artifact_id}/forms")
    def page_forms(
        artifact_id: str,
        include_closed: bool = False,
        agent: str = Depends(caller),
    ):
        """The forms a page declares (`data-artifact-form`) and the fields each
        has now, as stored at publish: name, type, label, options with their
        values, required, limits. Only the owning key reads this; not yours and
        no such page answer identically."""
        try:
            store.get_owned(db, agent, artifact_id)
        except store.NotFound as exc:
            raise HTTPException(404, "no such page") from exc
        return forms.for_page(db, artifact_id, include_closed=include_closed)

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
        # A question SET ASIDE (open, its page archived or binned) is left out of
        # every list of what waits on him unless asked for (2 Nov 2026). Rows carry
        # `set_aside`, `set_aside_since` and `reopened_at` either way.
        include_set_aside: bool = False,
        scope: str = Query("mine", pattern="^(mine|all)$"),
        # PAGING. The body stays a plain list, so a header carries the news that
        # it was cut. The default is the maximum (1000), not the old 200: the
        # operator's queue reconciles scope=all by DISAPPEARANCE and the rows it
        # needs -- open ones -- number a handful; it is answered history that
        # grows, and that is the caller who asks for it. A list still cut says
        # so with `X-Ream-Truncated: true` and gives the last id returned
        # in `X-Ream-Next`; pass that as `after` for the rest. Every
        # response carries the truncated header, true or false, so a missing
        # header is never read as "complete".
        limit: int = Query(1000, ge=1, le=1000),
        after: str | None = Query(None, max_length=200),
        response: Response = None,
        agent: str = Depends(caller),
    ):
        def _check_cursor(cursor: str | None, owner: str | None) -> None:
            # A cursor naming nothing would page as "the end". Refuse it.
            if cursor is not None and not decisions.exists(db, cursor, agent=owner):
                raise HTTPException(
                    422, {"field": "after", "detail": "no such decision to page after"}
                )

        def _mark_page(last_id: str | None, cut: bool) -> None:
            response.headers.update(
                _both_headers({"x-ream-truncated": "true" if cut else "false"})
            )
            if cut and last_id:
                response.headers.update(_both_headers({"x-ream-next": last_id}))

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
                # one operator seat. Every agent on that box shares that file, so the
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
            _check_cursor(after, None)
            seen = decisions.across_seats(
                db,
                include_answered=bool(include_answered) and not open_only,
                # Deliberately NOT suppressed by open_only, because the
                # per-artifact route above does not suppress it either and
                # one flag pair must mean one thing on every route.
                include_closed=include_closed,
                include_set_aside=include_set_aside,
                limit=limit + 1,
                after=after,
            )
            page = seen[:limit]
            _mark_page(page[-1].decision.id if page else None, len(seen) > limit)
            return [
                {
                    **decisions.public(row.decision),
                    "agent": row.agent,
                    "artifact_title": row.artifact_title,
                    "age_seconds": row.age_seconds,
                }
                for row in page
            ]
        _check_cursor(after, agent)
        mine = decisions.list_for_agent(
            db,
            agent,
            include_answered=(include_answered is not False) and not open_only,
            include_closed=include_closed,
            include_set_aside=include_set_aside,
            limit=limit + 1,
            after=after,
        )
        own = mine[:limit]
        _mark_page(own[-1].id if own else None, len(mine) > limit)
        return [decisions.public(row) for row in own]

    @router.get("/questions")
    def list_open_questions(
        open: bool = Query(True),
        # Set-aside questions (open, page archived or binned) are left out unless asked
        # for, as on GET /decisions; each carries `set_aside: true` and sorts after
        # every question that is waiting.
        include_set_aside: bool = False,
        scope: str = Query("mine", pattern="^(mine|all)$"),
        # PAGING, as on GET /decisions: `limit` (1..1000, default 1000), `after`
        # a question id from the previous page, `X-Ream-Truncated` on every
        # response and `X-Ream-Next` when cut. The order here is the
        # inbox's RANKING (computed in Python, not a column), so the page is a
        # slice of the whole ranked list, past the cursor's own position; the
        # source reads are not capped at the page size, so ranking sees every
        # open question. A cursor that is not in the caller's open list (never
        # existed, not theirs, or answered since the last page) answers 422.
        limit: int = Query(1000, ge=1, le=1000),
        after: str | None = Query(None, max_length=200),
        response: Response = None,
        agent: str = Depends(caller),
    ):
        """Open questions in the order his inbox shows them: overdue first, then
        urgency, then soonest needed-by, then topic, then age. The calling
        agent's own by default; `scope=all` is every office's and needs
        `decisions:read-all`, refused exactly as `GET /decisions?scope=all` is.

        A held question (one waiting on an unanswered prerequisite) is returned,
        marked by a non-empty `held_by`, and sits after every question that is
        ready: his inbox does not show it, but the agent that asked needs to see
        it. Reviews are not questions and are not listed here."""
        if not open:
            raise HTTPException(
                422,
                {
                    "field": "open",
                    "detail": "only open questions are listed here; read answered "
                    "ones from GET /api/v1/decisions",
                },
            )
        views = open_questions(db, agent, scope, include_set_aside)
        start = 0
        if after is not None:
            at = next((n for n, view in enumerate(views) if view["id"] == after), None)
            if at is None:
                raise HTTPException(
                    422,
                    {"field": "after", "detail": "no such open question to page after"},
                )
            start = at + 1
        page = views[start : start + limit]
        cut = len(views) > start + limit
        response.headers.update(
            _both_headers({"x-ream-truncated": "true" if cut else "false"})
        )
        if cut:
            response.headers.update(_both_headers({"x-ream-next": page[-1]["id"]}))
        return page

    @router.post("/decisions/{decision_id}/close")
    def close_decision(decision_id: str, agent: str = Depends(caller)):
        return close_question(db, agent, decision_id)

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
        return record_relay(db, agent, decision_id, payload)

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
                            "detail": textvars.render(
                                "{{owner_they}} {{owner_has}} not answered yet"
                            ),
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
            return 201, add_comment(
                db, artifact_id, body, agent, conn, seat_session, later
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

    @router.post("/decisions/{decision_id}/receipt")
    def declare_receipt(
        decision_id: str,
        payload: ReceiptBody,
        agent: str = Depends(caller),
        seat_session: str | None = Depends(session),
        idempotency_key: str | None = Header(default=None),
    ):
        """Say what you did with his answer: seen, working, done, blocked or
        wont-do. Your own statement, labelled with your agent and session and
        never verified. `done` does not close the decision. Returns the current
        receipt. `seen` is what `acknowledge` does."""

        def write(conn) -> tuple[int, object]:
            try:
                return 200, receipts.declare_on(
                    db,
                    conn,
                    agent,
                    decision_id,
                    state=payload.state,
                    note=payload.note,
                    link=payload.link,
                    lifetime=payload.for_,
                    session=seat_session,
                )
            except receipts.Refused as exc:
                raise HTTPException(422, str(exc)) from exc
            except receipts.NotAnswered as exc:
                raise HTTPException(409, str(exc)) from exc
            except store.SecretFound as found:
                raise HTTPException(
                    422, _refused_detail(found.field, found.findings)
                ) from found
            except store.NotFound as exc:
                raise HTTPException(404, "no such decision") from exc

        return idempotency.run(
            db,
            agent,
            f"receipt:{decision_id}",
            idempotency_key,
            payload.model_dump(),
            write,
        )

    @router.get("/decisions/{decision_id}/receipts")
    def list_receipts(decision_id: str, agent: str = Depends(caller)):
        """Every receipt on one of your decisions, oldest first, each marked
        `current` or history (a new answer from him makes earlier ones history).
        Seen is the decision's own `acknowledged_at`, not a row here."""
        with db.connect() as conn:
            found = receipts.history_owned(conn, agent, decision_id)
        if found is None:
            raise HTTPException(404, "no such decision")
        return found

    @router.post("/decisions/{decision_id}/acknowledge")
    def acknowledge_decision(
        decision_id: str,
        agent: str = Depends(caller),
        seat_session: str | None = Depends(session),
        idempotency_key: str | None = Header(default=None),
    ):
        def write(conn) -> tuple[int, object]:
            try:
                return 200, decisions.public(
                    decisions.acknowledge(
                        db, agent, decision_id, conn=conn, session=seat_session
                    )
                )
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
            # Name the seat -- see /decisions for why. Every agent on the management machine
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
                "decision": decisions.public(row.decision),
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
        # `include=receipts` adds the agent's own `receipt_set` rows. They never end a
        # `wait`, and a plain read does not return them (receipts.py).
        include: str | None = Query(None, pattern="^receipts$"),
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
        # and it works identically for a session on the management machine and one on a laptop.
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
        # Moved below the cap on `wait`: what is stamped as held is what is held.
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
        # Before the first look, never after the hold: see events.safe_cursor.
        # Read before the session is marked as waiting, so that a failure here
        # cannot leave a mark that nothing clears.
        pre = events.latest_seq(db, agent)
        # Still before the first look, as the comment above says. A hold (wait > 0)
        # also marks the session as waiting until it returns: see events.note_read.
        held_until = events.note_read(db, agent, seat_session, hold_seconds=wait)
        try:
            # The look that decides whether to hold never counts a receipt, so a
            # declaration cannot end a wait; with `include=receipts` they are added
            # to whatever comes back.
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
            if include == "receipts":
                rows = events.list_for_agent(
                    db,
                    agent,
                    since=since,
                    limit=limit,
                    session=session_filter,
                    exclude_actor=exclude,
                    include_receipts=True,
                )
        finally:
            if held_until is not None:
                events.note_hold_end(db, agent, seat_session, held_until)
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
            headers=_both_headers(
                {
                    # "From now" is this number; see events.latest_seq.
                    "x-ream-cursor": str(events.latest_seq(db, agent)),
                    # Safe to save as the next `since` after ANY hold, quiet or not.
                    "x-ream-seen": str(events.safe_cursor(since, pre, rows, limit)),
                    "x-ream-max-wait": str(int(PUBLIC_MAX_WAIT)),
                }
            ),
        )

    @router.get("/listening")
    def get_listening(
        decision: str | None = Query(None, max_length=120),
        agent: str = Depends(caller),
    ):
        """Whether a session of THIS key's agent is waiting for Jeremy's answer
        right now, and with `?decision=<id>`, where his answer to that question
        goes. The agent's twin of the line under a question on its page.

        `waiting` is the sessions holding an /events wait that has not lapsed;
        `recent` the others that asked for events in the last day. Scoped to the
        calling agent: no other agent's session appears, and a decision that is
        not this agent's answers exactly as one that does not exist. Read-only,
        and it does not record the caller as a listener.

        It cannot see an office that holds no wait (one that polls `list`), nor
        which Slack office a question maps to, so `state` says "sent", never
        "received"."""
        body = listening.status(db, agent, decision)
        if body is None:
            raise HTTPException(404, "no such decision")
        return body

    @router.put("/listening/pickup")
    def put_pickup(body: PickupBody, agent: str = Depends(caller)):
        """Say, in one short line, when your office acts on his answer ("within
        the hour"). Shown under each of your open questions as "<agent> says:
        ...", in place of "acted on at its next check". Your own words, never
        verified or promised by this service. One line per agent (key); a new
        one replaces the old. Plain text, single line, at most
        `listening.PICKUP_LIMIT` characters after control and format characters
        are removed. The agent-only twin of nothing on his side: he cannot set it."""
        try:
            return {"pickup": listening.set_pickup(db, agent, body.pickup)}
        except listening.BadPickup as exc:
            raise HTTPException(422, str(exc)) from None

    @router.delete("/listening/pickup")
    def delete_pickup(agent: str = Depends(caller)):
        """Remove your pickup line; the questions go back to "at its next check"."""
        return {"pickup": None, "cleared": listening.clear_pickup(db, agent)}

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
        "receipt",
        "receipts",
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
                        textvars.render(
                            "replies -- POST a reply under {{owner_their}} note on an "
                            "answered decision"
                        )
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
                "detail": f"no such route on a page: {unknown!r}",
                "did_you_mean": (
                    "body -- the page's own HTML (GET /api/v1/pages/<id>/body), to the "
                    "agent that published it, and to any agent while the page is "
                    "open to agents"
                    if unknown in ("source", "content", "html", "raw", "page", "text")
                    else None
                ),
                "routes": sorted(_SUBROUTES),
            },
        )

    return router
