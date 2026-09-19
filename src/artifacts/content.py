import os
import re

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from artifacts.db import Database
from artifacts.store import content_domain


def gallery_origin() -> str:
    """Read at call time, exactly like store.content_domain(). A module-level
    os.environ.get() is fixed at import, so no test can override it and the
    deployment has to set it before the first import of this module."""
    return os.environ.get("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")

_BASE_CSP = (
    "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; font-src 'self'; form-action 'none'; "
    "base-uri 'none'; worker-src 'none'; "
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


def inject(body: str, shim: str) -> str:
    """Put the shim in the document, wherever the document lets us.

    A plain `body.replace("</head>", ...)` silently does nothing on a page with
    no head -- `<!doctype html><body>...` is ordinary agent-written HTML -- and
    the page then loads with no runtime at all and no error anywhere. Try the
    head close, then the body open, then the front of the document; the page
    always ends up with a runtime. Matching is case-insensitive: `</HEAD>` is
    the same tag.
    """
    for pattern, where in ((r"</head\s*>", "before"), (r"<body\b[^>]*>", "after")):
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            at = match.start() if where == "before" else match.end()
            return body[:at] + shim + body[at:]
    return shim + body


def _artifact_id(request: Request) -> str | None:
    host = request.headers.get("host", "").split(":")[0]
    suffix = "." + content_domain()
    return host[: -len(suffix)] if host.endswith(suffix) else None


def create_content_app(db: Database) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)

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
            return Response("not found", status_code=404)
        # Framed-only is asserted POSITIVELY. Denying just "document" serves the page to
        # any client that sends no Sec-Fetch-Dest at all -- curl, a script's fetch, an old
        # browser -- which is every client that is not a browser honouring the frame.
        # `frame` is the legacy <frame> value; nested iframes also send `iframe`.
        if request.headers.get("sec-fetch-dest") not in ("iframe", "frame"):
            return RedirectResponse(f"{gallery_origin()}/a/{artifact_id}", status_code=303)
        loaded = _load(artifact_id, n)
        if loaded is None:
            return Response("not found", status_code=404)
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
