import html as _html
import json
from collections.abc import Sequence
from datetime import datetime

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from artifacts import decisions, frame_pass, hosts, stamp, store, templates
from artifacts.db import Database
from artifacts.store import (
    gallery_origin,  # re-exported: artifacts.content.gallery_origin still resolves
)
from artifacts.text import LEADING_DOCTYPE as _LEADING_DOCTYPE

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


def _viewer_report(missing: Sequence[str] = ()) -> str:
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

    - `height`: this document's own CONTENT height, so the gallery can size the iframe
      to it instead of leaving a second scrollbar inside the frame.

      Measured from `body`, never from `document.documentElement.scrollHeight`, and
      that is load-bearing: documentElement's scrollHeight is floored by the viewport
      (measured -- a 300px page in a 900px frame reports 900). The gallery sizes the
      iframe to whatever arrives, which makes the iframe the viewport, which makes the
      floor the iframe's current height. So the report becomes a ONE-WAY RATCHET: the
      frame can grow and can never shrink again. Every decision slot that releases its
      reserved room, and every section that collapses, leaves permanent dead space
      below the page -- which is exactly what Jeremy reported on 20 Sep 2026 ("a lot of
      empty scroll space at the bottom of the page"). body's scrollHeight has no such
      floor; the max() and the bottom margin cover a page whose body is positioned or
      has margins of its own, and erring a pixel LARGE is right, because a pixel short
      brings back the second scrollbar this is here to remove.
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
        + _missing_notes(missing)
        + "function b(e){var r=e.getBoundingClientRect();"
        'return{id:e.getAttribute("data-artifact-decision"),'
        "top:r.top+scrollY,left:r.left+scrollX,width:r.width}}"
        "function h(){var b=document.body,m=0;"
        "if(!b)return document.documentElement.scrollHeight;try{"
        "m=parseFloat(getComputedStyle(b).marginBottom)||0}catch(e){}"
        "return Math.ceil(Math.max(b.scrollHeight,b.offsetHeight,"
        "b.getBoundingClientRect().bottom+scrollY)+m)}"
        'function send(){parent.postMessage({type:"artifact-state",'
        "height:h(),"
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


def _script_json(value: object) -> str:
    """JSON that is safe to splice into an inline `<script>`.

    `json.dumps` leaves `<`, `>` and `&` alone, and a slot id is the agent's
    own string -- `</script>` inside one would end the shim early and hand the
    rest of it to the HTML parser. `ensure_ascii` already covers U+2028/9.
    """
    return (
        json.dumps(value, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


# What a reader sees where the control should have been. Written for Jeremy,
# who read the blank version as "the service lost my choices" (homelab-ij8.50):
# so it says what is wrong, that nothing of his is gone, and whose fix it is.
_MISSING_BEFORE = "There should be a question here, but the page points to one that doesn\u2019t exist (\u201c"
_MISSING_AFTER = (
    "\u201d). Nothing has been lost; whoever published the page needs to fix it."
)


def _missing_notes(missing: Sequence[str]) -> str:
    """Shim code that writes a note into each slot naming no decision here.

    Empty -- not a no-op script -- when there are none, so an ordinary page is
    served exactly the bytes it was before this existed.

    The ids come from the SERVER, computed from the body at serve time
    (`decisions.unresolved_slots`), because only the server knows which ids are
    real: the shim deliberately has no idea, and the gallery only knows the
    OPEN ones, which would make an answered question -- correctly released --
    indistinguishable from one that never existed. Responses are `no-store`,
    so the list cannot outlive the state it was computed from.

    Only an EMPTY slot is written to. An agent that put its own fallback
    inside the element has already said something there, and overwriting the
    page's own words is not this shim's call. Styles go through `el.style`,
    the same CSSOM path the size leg already uses; `currentColor` and an
    opacity rather than a colour, so it reads on a dark page as well as a light
    one. Runs at DOMContentLoaded because the shim is injected right after the
    doctype, before any slot has been parsed.
    """
    if not missing:
        return ""
    return (
        f"var gone={_script_json(list(missing)[:64])};"
        'addEventListener("DOMContentLoaded",function(){try{'
        "gone.forEach(function(id){"
        "var el=document.querySelector("
        "'[data-artifact-decision=\"'+(window.CSS&&CSS.escape?CSS.escape(id):id)+'\"]');"
        "if(!el||el.textContent.trim())return;"
        f"el.textContent={_script_json(_MISSING_BEFORE)}+id+{_script_json(_MISSING_AFTER)};"
        'var s=el.style;s.display="block";s.padding=".75em 1em";'
        's.border="1px dashed currentColor";s.borderRadius="8px";'
        's.opacity=".75";s.fontSize=".875em";s.lineHeight="1.45"})'
        "}catch(e){}});"
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


def expired_page() -> HTMLResponse:
    """What a frame gets for a pass that is spent, expired, for another page, or
    unknown -- one answer for all four, so a pass cannot be used to learn which
    ids exist (homelab-ij8.101).

    The ordinary way to arrive here is not an attack but a browser restoring a
    frame's last URL: Firefox does it on reload, Chrome on back/forward, and a
    pass is good once. So the page tells the gallery, which asks its own origin
    for a fresh pass and re-points the frame (gallery.js, `artifact-expired`).
    It posts only to the gallery's origin and says nothing but its type. The
    words are for a browser with scripts off, and name what to do.
    """
    origin = json.dumps(gallery_origin())
    body = (
        "<!doctype html><meta charset=utf-8><title>Expired</title>"
        '<p style="font:15px/1.5 system-ui,sans-serif;margin:2em;opacity:.8">'
        "This view of the page has expired. Open it again from "
        f'<a href="{_html.escape(gallery_origin())}" target="_top">the gallery</a>.</p>'
        '<script>try{parent.postMessage({type:"artifact-expired"},'
        f"{origin})}}catch(e){{}}</script>"
    )
    return HTMLResponse(body, status_code=403, headers=headers_for(False))


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

    def _load(artifact_id: str, n: int | None) -> tuple[str, int, datetime] | None:
        with db.connect() as conn:
            row = conn.execute(
                "select current_version from artifact"
                " where id = %s and deleted_at is null",
                (artifact_id,),
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

    def _serve(request: Request, token: str | None) -> Response:
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
        # Spent only AFTER the framed-only check above: a pass opened as a page
        # of its own is redirected to the gallery with the pass still unused,
        # never burned by a request that was never going to be shown it.
        if token is None:
            return Response("not found", status_code=404, media_type="text/plain")
        spent = frame_pass.spend(db, token, artifact_id)
        if spent is None:
            return expired_page()
        n = spent.version
        loaded = _load(artifact_id, n)
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
        if live:
            missing = [
                slot
                for slot, _line in decisions.unresolved_slots(db, artifact_id, body)
            ]
            shim = SHIM + _viewer_report(missing)
        else:
            shim = INERT_SHIM
        html = inject(body, shim)
        return HTMLResponse(html, headers=headers_for(live))

    # THE SHARED LOOK, AS A REAL STYLESHEET (homelab-ij8.21). Jeremy, 19 Sep:
    # serve it instead of 150 lines pasted into every page, while agents stay
    # free to design a page however they like. It is 7.9 KB -- 42% of the
    # project-status template -- and a page links it with one line:
    #
    #     <link rel="stylesheet" href="/_shared.css">
    #
    # ON THE PAGE'S OWN ORIGIN, which is the whole reason this route lives
    # here and not on the gallery: the page CSP is `style-src 'self'`, and
    # 'self' for a published page is <id>.tenfourty.com. A stylesheet served
    # from the gallery would be blocked, silently, in the way this service
    # exists to stop.
    #
    # NO TOKEN, and that is deliberate rather than an oversight: this is the
    # same constant on every page host, it says nothing about any page, and it
    # is fetched by the BROWSER as a subresource -- which arrives with
    # sec-fetch-dest: style and no capability token, because a <link> cannot
    # carry one. Making it token-only would mean no page could ever use it.
    #
    # Not linking it stays a supported choice: every shipped template still
    # carries the block inline, because a template's own promise is that you
    # can open the file in a browser with nothing installed and see the page.
    @app.get("/_shared.css")
    def shared_css(request: Request) -> Response:
        css, etag = templates.shared_css()
        # Revalidate rather than guess: the ETag is content-derived, so a pod
        # serving new CSS answers a stale conditional with 200 and every other
        # one with an empty 304. Five minutes of cache on top of that keeps a
        # reload from asking at all, which matters on a wall of framed pages.
        headers = {
            "content-type": "text/css; charset=utf-8",
            "etag": etag,
            "cache-control": "public, max-age=300",
            **base_headers(),
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(css, media_type="text/css; charset=utf-8", headers=headers)

    @app.get("/")
    def bare(request: Request) -> Response:
        # The address an agent is handed (`Published.url`). Unframed, it is sent to the
        # gallery like any other unframed request; framed, it carries no token and so
        # opens nothing.
        return _serve(request, None)

    # A PAGE SHARED BY LINK (homelab-ij8.101, Jeremy's rulings): the latest
    # version, just the page, full screen -- a top-level document, so no
    # framed-only check. Still on the page's own origin, which holds no session
    # of his, and inert: no runtime, no questions, no viewer channel. Every
    # failure -- wrong token, withdrawn link, deleted page, no such page --
    # gets one answer, so a link cannot be probed for which pages exist.
    @app.get("/s/{token}/")
    def shared(request: Request, token: str) -> Response:
        artifact_id = _artifact_id(request)
        found = store.shared_page(db, artifact_id, token) if artifact_id else None
        if found is None:
            return PlainTextResponse(
                "This link isn't valid, or it has been withdrawn.",
                status_code=404,
                headers={**base_headers(), "cache-control": "no-store"},
            )
        body, published_at = found
        html = inject(stamp.apply(body, published_at), INERT_SHIM)
        return HTMLResponse(
            html,
            headers={**headers_for(False), "x-robots-tag": "noindex, nofollow"},
        )

    # The only way in to a page body (homelab-ij8.101). Which version the pass
    # reads is part of the pass, so there is no version path for a caller to
    # choose: the gallery decided that when it minted it.
    @app.get("/p/{token}/")
    def framed(request: Request, token: str) -> Response:
        return _serve(request, token)

    return app
