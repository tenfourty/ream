import re

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from artifacts import hosts
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
        "content-security-policy": _BASE_CSP + connect + f"frame-ancestors {gallery_origin()}",
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

# An optional BOM, then leading whitespace and whole HTML comments, then the leading
# doctype -- everything up to and including it is one match, so the shim can be spliced
# in right after it.
_LEADING_DOCTYPE = re.compile(
    r"^(﻿?\s*(?:<!--.*?-->\s*)*<!doctype[^>]*>)", re.IGNORECASE | re.DOTALL
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


def _first_host_header(scope: dict) -> str | None:
    """The FIRST `host` header from a raw ASGI scope, decoded latin-1 (HTTP header
    bytes are not guaranteed UTF-8). `app.py`'s router reads Host the exact same way,
    so a host is never judged one way by the router and another way here."""
    for key, value in scope.get("headers") or []:
        if key == b"host":
            return value.decode("latin-1")
    return None


def _artifact_id(request: Request) -> str | None:
    return hosts.artifact_id(hosts.normalise(_first_host_header(request.scope)))


def create_content_app(db: Database) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _base_headers(request: Request, call_next):
        """Every content response carries at least the minimum header set, not just
        the 200 page: FastAPI's own 404/405/422 and the router-adjacent responses
        below all pass through here too. A response that already carries the page's
        full CSP (`headers_for`) is left untouched -- this only fills the gap."""
        response = await call_next(request)
        if "content-security-policy" not in response.headers:
            response.headers["x-content-type-options"] = "nosniff"
            response.headers["referrer-policy"] = "no-referrer"
            response.headers["content-security-policy"] = (
                f"default-src 'none'; frame-ancestors {gallery_origin()}"
            )
        return response

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        """Never the submitted value, never its location -- a version path takes one
        int, so the only way to invalidate it is to send something that isn't one."""
        return JSONResponse(status_code=422, content={"detail": "invalid request"})

    def _load(artifact_id: str, n: int | None) -> tuple[str, int] | None:
        with db.connect() as conn:
            row = conn.execute(
                "select current_version from artifact where id = %s", (artifact_id,)
            ).fetchone()
            if row is None:
                return None
            wanted = n if n is not None else row[0]
            # `and status = 'live'` is a no-op today -- nothing in this arc writes a
            # 'pending' version row, because pending-on-widening (design D11) is deferred.
            # It is here so that the day D11 lands, the content server does not silently
            # begin serving bodies that were written precisely because they had NOT yet
            # been cleared to be served. A filter that costs nothing now and closes a hole
            # later beats a comment in the deferral list that a future task has to notice.
            body = conn.execute(
                "select body from version where artifact_id = %s and n = %s "
                "and status = 'live'",
                (artifact_id, wanted),
            ).fetchone()
        return (body[0], wanted) if body else None

    def _serve(request: Request, n: int | None) -> Response:
        artifact_id = _artifact_id(request)
        if artifact_id is None:
            return Response("not found", status_code=404, media_type="text/plain")
        # Framed-only is asserted POSITIVELY. Denying just "document" serves the page to
        # any client that sends no Sec-Fetch-Dest at all -- curl, a script's fetch, an old
        # browser -- which is every client that is not a browser honouring the frame.
        # `frame` is the legacy <frame> value; nested iframes also send `iframe`.
        if request.headers.get("sec-fetch-dest") not in ("iframe", "frame"):
            return RedirectResponse(f"{gallery_origin()}/a/{artifact_id}", status_code=303)
        loaded = _load(artifact_id, n)
        if loaded is None:
            return Response("not found", status_code=404, media_type="text/plain")
        body, _served = loaded
        live = n is None
        html = inject(body, SHIM if live else INERT_SHIM)
        return HTMLResponse(html, headers=headers_for(live))

    @app.get("/")
    def current(request: Request) -> Response:
        return _serve(request, None)

    @app.get("/v/{n}")
    def version(request: Request, n: int) -> Response:
        return _serve(request, n)

    return app
