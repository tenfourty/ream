import hmac
import json
import re
from datetime import datetime

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from artifacts import hosts, stamp
from artifacts.db import Database
from artifacts.store import (
    gallery_origin,  # re-exported: artifacts.content.gallery_origin still resolves
)

_BASE_CSP = (
    "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; font-src 'self'; form-action 'none'; "
    "base-uri 'none'; worker-src 'none'; webrtc 'block'; "
)


def headers_for(live: bool) -> dict[str, str]:
    connect = "connect-src 'self'; " if live else "connect-src 'none'; "
    return {
        "content-security-policy": _BASE_CSP
        + connect
        + f"frame-ancestors {gallery_origin()}",
        "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
        "x-dns-prefetch-control": "off",
        "cross-origin-opener-policy": "same-origin",
        "cross-origin-resource-policy": "same-origin",
        "permissions-policy": "geolocation=(), camera=(), microphone=(), payment=()",
        "cache-control": "no-store",
    }


SHIM = "<script>window.claude={use:async(n)=>null};</script>"  # replaced in Task 7

# An older version is inert, which is NOT the same as having no runtime. The
# design says every name RESOLVES null on a version path; a page written to the
# contract does `const db = await window.claude.use("db")` and branches on null,
# so removing window.claude entirely makes that page throw a TypeError instead
# of degrading. Serve the null shim there and let the page render read-only.
INERT_SHIM = "<script>window.claude={use:async(n)=>null};window.storage=null;</script>"


def _viewer_report() -> str:
    """The other half of what gets injected on a LIVE page -- unrelated to the
    `window.claude` contract above, so it is a separate `<script>`, concatenated onto
    `SHIM` only in `_serve` below, never folded into the `SHIM` constant itself: `SHIM`
    is asserted byte-for-byte in `inject()`'s own unit tests, and this half needs a
    value (`gallery_origin()`) that has to be read at CALL time, exactly like
    `headers_for` reads it -- a module-level constant computed once at import would go
    stale the moment a test (or a future config reload) changes
    ARTIFACTS_GALLERY_ORIGIN afterwards.

    Reports two things in one message, on the same three triggers -- `load`, `resize`,
    and a `ResizeObserver` on `documentElement` (never a poll, per the design doc):

    - `height`: this document's own scrollHeight, so the gallery can size the iframe to
      it instead of leaving a second scrollbar inside the frame.
    - `slots`: the page-coordinate rectangle (id, top, left, width) of every element the
      agent marked with `data-artifact-decision="<id>"` -- never the element's own
      contents. What a reported id is even allowed to mean is entirely the gallery's
      call: this shim has no idea which ids are real decisions and does not need to.

    Posts only to `parent`, and only to the gallery's own origin -- never `"*"`, or a
    page that also happens to load inside some OTHER frame could read it. Wrapped in one
    top-level try/catch: a browser old enough to lack `ResizeObserver` (or anything else
    here) must never throw into the page's own console, and must never stop
    `window.claude` (in `SHIM`, already run by the time this executes) from having been
    assigned.
    """
    origin = json.dumps(gallery_origin())
    return (
        "<script>try{(function(){"
        "function b(e){var r=e.getBoundingClientRect();"
        'return{id:e.getAttribute("data-artifact-decision"),'
        "top:r.top+scrollY,left:r.left+scrollX,width:r.width}}"
        'function send(){parent.postMessage({type:"artifact-state",'
        "height:document.documentElement.scrollHeight,"
        'slots:Array.from(document.querySelectorAll("[data-artifact-decision]"),b)},'
        f"{origin})}}"
        'addEventListener("load",send);addEventListener("resize",send);'
        'if("ResizeObserver"in window){'
        "new ResizeObserver(send).observe(document.documentElement)}"
        # The return leg. The gallery lifts the control out of the frame and places it
        # over the slot, so the page must be told how tall it turned out and reserve
        # that room -- otherwise the control sits on top of whatever came next and the
        # page reads as broken. Only `min-height` on the slot the page itself marked
        # is ever touched: no other property, no other element, and nothing at all is
        # read out of the message beyond an id and a number.
        'addEventListener("message",function(e){'
        f"if(e.origin!=={origin})return;"
        "var d=e.data;"
        'if(!d||d.type!=="artifact-slot-sizes"||!Array.isArray(d.sizes))return;'
        # The message carries the COMPLETE set of slots the gallery filled, so a
        # mark the gallery said nothing about is one nothing fills any more -- an
        # answered question, which stops being placeable. Its reserved room is
        # released here rather than left as a gap the page cannot explain. Every
        # marked element is reset first and then the reported ones are set, so a
        # slot that keeps its size is never momentarily collapsed.
        "var want={};d.sizes.slice(0,64).forEach(function(s){"
        # `!s` first: a `sizes` array carrying a null throws on the property read,
        # and the top-level try/catch wraps this listener's REGISTRATION, not its
        # later invocations -- so an exception here would surface in the agent's own
        # page console. Found by executing the shim, not by reading it.
        'if(!s||typeof s.id!=="string"||typeof s.height!=="number")return;'
        "if(!(s.height>=0&&s.height<=20000))return;"
        "var el=document.querySelector("
        "'[data-artifact-decision=\"'+(window.CSS&&CSS.escape?CSS.escape(s.id):s.id)+'\"]');"
        "if(el)want[s.id]=s.height});"
        "Array.prototype.forEach.call("
        'document.querySelectorAll("[data-artifact-decision]"),function(el){'
        'var id=el.getAttribute("data-artifact-decision");'
        'el.style.minHeight=(want[id]||0)+"px"})});'
        "})()}catch(e){}</script>"
    )


# An optional BOM, then leading whitespace and whole HTML comments, then the leading
# doctype -- everything up to and including it is one match, so the shim can be spliced
# in right after it. The comment alternative is not a lazy `.*?-->`: under backtracking
# that can span from the first `<!--` all the way to the LAST `-->` before a later
# doctype, treating a genuine `<script>` (and anything else) in between as if it were
# commented out, and landing the shim after it, or inside it.
#
# The WHATWG HTML tokenizer ends a comment at more than plain `-->`: also at `<!-->` and
# `<!--->` (abrupt closing of an empty comment) and at `--!>` (incorrectly closed
# comment). Missing those lets a comment appear to "end" only at the FIRST real `-->`
# that follows, which can be well past a page's own `<script>` -- so the alternative
# below is `-?>` (the abrupt-close forms, immediately after `<!--`) or a body that
# contains neither `-->` nor `--!>`, terminated by either. The negative lookahead right
# after `<!--` is load-bearing: without it, backtracking can still reopen `<!-->`.
_LEADING_DOCTYPE = re.compile(
    r"^(﻿?\s*(?:<!--(?:-?>|(?!-?>)(?:(?!--!?>).)*--!?>)\s*)*<!doctype[^>]*>)",
    re.IGNORECASE | re.DOTALL,
)


def inject(body: str, shim: str) -> str:
    """Put the shim immediately after the document's leading doctype (skipping an
    optional BOM, leading whitespace and any whole HTML comments before it), or at
    the very start of the document when there is no such doctype.

    The previous approach searched for `</head>`, which (a) ran the shim AFTER any
    classic head script that itself calls `window.claude`, (b) was captured by a
    `</head>` hidden inside an HTML comment, leaving the page with no runtime at
    all, and (c) could land INSIDE a `<script>` whose own text contains the literal
    string `</head>`, where the injected `</script>` closed that script early.
    Anchoring on the doctype instead runs the shim before any of the page's own
    script and is immune to a `</head>` anywhere in the document's content -- in a
    comment, in a string, or nowhere at all.
    """
    match = _LEADING_DOCTYPE.match(body)
    at = match.end() if match else 0
    return body[:at] + shim + body[at:]


def _artifact_id(request: Request) -> str | None:
    return hosts.artifact_id(hosts.normalise(hosts.first_host_header(request.scope)))


def _token_matches(expected: str, given: str | None) -> bool:
    """hmac.compare_digest raises TypeError on a non-ASCII str, and `given` is whatever the
    request path carried, so compare bytes: a crafted path is a mismatch, never a 500."""
    return given is not None and hmac.compare_digest(expected.encode(), given.encode())


def base_headers() -> dict[str, str]:
    """The minimum header set every content response carries, even one FastAPI or
    Starlette generates on its own rather than a route handler here (404/422/500).
    Read at call time, exactly like `headers_for` -- one function, so the middleware and
    the 500 handler (which sits OUTSIDE that middleware; see below) can never drift apart
    into two copies of the same literal."""
    return {
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "content-security-policy": f"default-src 'none'; frame-ancestors {gallery_origin()}",
    }


def create_content_app(db: Database) -> FastAPI:
    app = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False
    )

    @app.middleware("http")
    async def _base_headers(request: Request, call_next):
        """Every content response carries at least the minimum header set, not just
        the 200 page: FastAPI's own 404/405/422 and the router-adjacent responses
        below all pass through here too. A response that already carries the page's
        full CSP (`headers_for`) is left untouched -- this only fills the gap."""
        response = await call_next(request)
        if "content-security-policy" not in response.headers:
            for key, value in base_headers().items():
                response.headers[key] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        """Never the submitted value, never its location -- a version path takes one
        int, so the only way to invalidate it is to send something that isn't one."""
        return JSONResponse(status_code=422, content={"detail": "invalid request"})

    @app.exception_handler(Exception)
    async def _on_unhandled_error(
        _request: Request, _exc: Exception
    ) -> PlainTextResponse:
        """Starlette's ServerErrorMiddleware sits OUTSIDE the `@app.middleware("http")`
        layer above, so a 500 never passes through `_base_headers` and must carry its own
        minimum header set directly, or an unhandled exception would be the one response
        this origin ever serves with no CSP at all."""
        return PlainTextResponse(
            "internal error", status_code=500, headers=base_headers()
        )

    def _load(
        artifact_id: str, token: str | None, n: int | None
    ) -> tuple[str, int, datetime] | None:
        with db.connect() as conn:
            row = conn.execute(
                "select current_version, read_token from artifact where id = %s",
                (artifact_id,),
            ).fetchone()
            # A wrong token and a missing artifact are the same answer: nothing here may confirm
            # that an id exists.
            if row is None or not _token_matches(row[1], token):
                return None
            wanted = n if n is not None else row[0]
            # `and status = 'live'` is a no-op today -- nothing in this arc writes a
            # 'pending' version row, because pending-on-widening (design D11) is deferred.
            # It is here so that the day D11 lands, the content server does not silently
            # begin serving bodies that were written precisely because they had NOT yet
            # been cleared to be served. A filter that costs nothing now and closes a hole
            # later beats a comment in the deferral list that a future task has to notice.
            # `created_at` comes back with the body because the page's own
            # "updated" stamp is THIS version's publication time, not the
            # artifact's latest -- an older version served at /v/N must say when
            # IT was published, or an inert snapshot would claim to be as fresh
            # as the live page.
            body = conn.execute(
                "select body, created_at from version"
                " where artifact_id = %s and n = %s and status = 'live'",
                (artifact_id, wanted),
            ).fetchone()
        return (body[0], wanted, body[1]) if body else None

    def _serve(request: Request, token: str | None, n: int | None) -> Response:
        artifact_id = _artifact_id(request)
        if artifact_id is None:
            return Response("not found", status_code=404, media_type="text/plain")
        # Framed-only is asserted POSITIVELY. Denying just "document" serves the page to
        # any client that sends no Sec-Fetch-Dest at all -- curl, a script's fetch, an old
        # browser -- which is every client that is not a browser honouring the frame.
        # `frame` is the legacy <frame> value; nested iframes also send `iframe`. This
        # redirect never carries the token: its Location is the gallery's own address for
        # the artifact, not the tokenised one.
        if request.headers.get("sec-fetch-dest") not in ("iframe", "frame"):
            return RedirectResponse(
                f"{gallery_origin()}/a/{artifact_id}", status_code=303
            )
        loaded = _load(artifact_id, token, n)
        if loaded is None:
            return Response("not found", status_code=404, media_type="text/plain")
        body, _served, published_at = loaded
        # The service fills in when this version was published, so no agent has
        # to write a time down (homelab-ij8.70). Done here rather than at store
        # time: the stored body is hashed to make `revision`, so folding a clock
        # into it would mint a new revision on every identical republish.
        body = stamp.apply(body, published_at)
        live = n is None
        # Only the LIVE page reports geometry to the gallery: an inert snapshot is
        # never framed by the gallery today (there is no route for it), and inert is
        # meant to mean "read-only", not "read-only except for a viewer channel".
        shim = (SHIM + _viewer_report()) if live else INERT_SHIM
        html = inject(body, shim)
        return HTMLResponse(html, headers=headers_for(live))

    @app.get("/")
    def bare(request: Request) -> Response:
        # The address an agent is handed (`Published.url`). Unframed, it is sent to the
        # gallery like any other unframed request; framed, it carries no token and so
        # opens nothing.
        return _serve(request, None, None)

    @app.get("/t/{token}/")
    def current(request: Request, token: str) -> Response:
        return _serve(request, token, None)

    @app.get("/t/{token}/v/{n}")
    def version(request: Request, token: str, n: int) -> Response:
        return _serve(request, token, n)

    return app
