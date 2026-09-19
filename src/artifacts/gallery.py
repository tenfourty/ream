"""Jeremy's view: a list of published pages and a detail page that frames each one.

No template engine: two pages of a handful of fields do not need Jinja, so every
interpolation goes through `escape()` explicitly. If this grows past a few fields,
introducing Jinja with `autoescape=True` is a contained change -- do that rather than
adding a sixth hand-escaped field.

No in-app identity check: Authelia gates this host at the ingress, and `/api/v1`
already rejects a `Remote-User` header outright (Task 5). Nothing here reads it.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from markupsafe import escape

from artifacts.db import Database
from artifacts.store import content_domain, content_url


def gallery_headers() -> dict[str, str]:
    """Built per call, because the content domain is read from the environment
    at call time (store.content_domain())."""
    return {
        "content-security-policy": (
            "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
            # frame-src is the content domain only. `https:` would let any bug
            # that reaches this attribute frame anything on the internet,
            # inside the one page that IS logged in.
            f"frame-src https://*.{content_domain()}; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        ),
        "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
    }


def gallery_router(db: Database) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        with db.connect() as conn:
            rows = conn.execute(
                # Bounded: an unpaginated list is fine at ten artifacts and a
                # page nobody can load at ten thousand.
                "select id, title, updated_at from artifact"
                " order by updated_at desc limit 200"
            ).fetchall()
        items = "".join(
            f'<li><a href="/a/{escape(row[0])}">{escape(row[1])}</a>'
            f"<time>{escape(str(row[2]))}</time></li>"
            for row in rows
        )
        return HTMLResponse(
            f"<!doctype html><title>Artifacts</title><ul>{items}</ul>",
            headers=gallery_headers(),
        )

    @router.get("/a/{artifact_id}", response_class=HTMLResponse)
    def detail(artifact_id: str) -> HTMLResponse:
        # Look the artifact up rather than framing whatever the URL said: an
        # unchecked id renders a frame pointing at an invented subdomain, and
        # the title is wanted on the page anyway.
        with db.connect() as conn:
            row = conn.execute(
                "select title, read_token from artifact where id = %s", (artifact_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(404, "no such artifact")
        # The one place the capability token is rendered: a page only Jeremy's Authelia session
        # can load, with Referrer-Policy: no-referrer so it never leaves in a Referer.
        src = escape(content_url(artifact_id, row[1]))
        return HTMLResponse(
            f"<!doctype html><title>{escape(row[0])}</title>"
            f"<h1>{escape(row[0])}</h1>"
            f'<iframe src="{src}" style="width:100%;height:100vh;border:0"></iframe>',
            headers=gallery_headers(),
        )

    return router
