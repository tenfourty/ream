"""Jeremy's view: a list of published pages and a detail page that frames each one.

No template engine: two pages of a handful of fields do not need Jinja, so every
interpolation goes through `escape()` explicitly. If this grows past a few fields,
introducing Jinja with `autoescape=True` is a contained change -- do that rather than
adding a sixth hand-escaped field.

No in-app identity check: Authelia gates this host at the ingress. `/api/v1` does not
special-case a `Remote-User` header either -- it simply ignores one, since identity
there comes only from the verified key (Task 5). Nothing in this module reads it.
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
        # The sandboxed iframe below has no allow-top-navigation*, but it DOES
        # carry allow-popups-to-escape-sandbox (needed for a page to open a
        # normal, unsandboxed link) -- a popup it opens is no longer sandboxed
        # at all, and without this header that popup keeps a `window.opener`
        # back to THIS tab, letting it run `opener.top.location = "..."` and
        # navigate Jeremy's signed-in gallery out from under him even though
        # the iframe itself could never do that directly. COOP same-origin
        # severs that opener link (a cross-origin popup's `window.opener` is
        # null), which is what actually closes the gap the sandbox leaves
        # open -- without it, allow-popups-to-escape-sandbox would undo the
        # no-top-navigation restriction just by one extra hop.
        "cross-origin-opener-policy": "same-origin",
    }


def gallery_router(db: Database) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        with db.connect() as conn:
            rows = conn.execute(
                # Bounded: an unpaginated list is fine at ten artifacts and a
                # page nobody can load at ten thousand. `, id desc` breaks a tie
                # on `updated_at` (two rows can share one, e.g. re-touched by
                # the same migration) so the order is fixed, not whatever a
                # plan with no full ORDER BY happens to return today.
                "select id, title, updated_at from artifact"
                " order by updated_at desc, id desc limit 200"
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
        # can load. The token then travels in the iframe's src, and it is the CONTENT app's own
        # Referrer-Policy: no-referrer (content.py) that keeps it out of a Referer once that
        # framed page makes requests of its own -- this gallery's no-referrer header covers only
        # this page's own, tokenless /a/<id> URL.
        src = escape(content_url(artifact_id, row[1]))
        # Sandboxed with no allow-top-navigation*, no allow-forms, no allow-modals: an agent's
        # page must never be able to navigate Jeremy's signed-in gallery tab out from under him
        # (e.g. to a fake sign-in page) or pop a dialog over it. allow-same-origin is safe here
        # because the frame is cross-origin to the gallery (its own <id>.<domain> origin), and it
        # needs its own origin for the content app's `connect-src 'self'`.
        sandbox = "allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox"
        return HTMLResponse(
            f"<!doctype html><title>{escape(row[0])}</title>"
            f"<h1>{escape(row[0])}</h1>"
            f'<iframe src="{src}" sandbox="{sandbox}"'
            f' style="width:100%;height:100vh;border:0"></iframe>',
            headers=gallery_headers(),
        )

    return router
