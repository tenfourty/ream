"""Jeremy's view: a list of published pages and a detail page that frames each one, and
now the one place on that detail page he can act rather than just look: a plain
server-rendered form per open or answered decision.

No template engine: a handful of fields do not need Jinja, so every interpolation goes
through `escape()` explicitly. If this grows past a few fields, introducing Jinja with
`autoescape=True` is a contained change -- do that rather than adding yet another
hand-escaped field.

No in-app identity check: Authelia gates this host at the ingress. `/api/v1` does not
special-case a `Remote-User` header either -- it simply ignores one, since identity
there comes only from the verified key. Nothing in this module reads it either, which is
exactly why answering a decision hardcodes `by="jeremy"` (see `answer_decision` below)
rather than trusting any identity header: Authelia admits exactly one subject to this
host, but nothing here proves the ingress strips an inbound copy of such a header before
a request reaches this app, so the one subject it ever admits is named outright instead
of read from something a request could forge.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape

from artifacts import decisions, store
from artifacts.db import Database
from artifacts.store import content_domain, content_url

# Mirrors api.py's own wording for the identical fault: the scanner broke, the
# content did not -- 503, not 422, so Jeremy is not left resubmitting a note that was
# never the problem.
_SCAN_FAILED_DETAIL = (
    "the secret scanner failed -- this is a fault on our side, not yours. "
    "Nothing was saved. Tell the operator."
)


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


def _same_origin_write(request: Request) -> None:
    """The one CSRF guard this app needs, and (per the MVP's "no JavaScript, no CSP
    change" rule) the only kind it CAN have: there is no script here to mint or check
    a token, so the browser's own same-origin signals have to do the job instead.

    `Origin` must equal our own gallery origin. A browser sends `Origin` on every
    form POST -- same-origin or not -- so this alone refuses a `<form>` on some other
    site that a victim's browser would otherwise submit carrying Jeremy's Authelia
    session cookie: the cookie rides along, but the Origin it arrives with does not
    match, and the request never reaches `decisions.answer`. A request with no
    `Origin` at all (curl with no headers, an old proxy that strips it) fails the same
    equality check and is refused identically -- there is no separate "missing"
    branch, because "missing" and "wrong" both mean the one thing this guard checks
    for is absent.

    `Sec-Fetch-Site`, when a browser sends it, says the same thing more directly and
    is checked ONLY when present -- never required, so a client that omits it (an
    older browser, a non-browser HTTP client making a legitimate same-origin-style
    call) is judged on `Origin` alone, not refused for silence on a header it never
    had reason to send.
    """
    origin = request.headers.get("origin")
    if origin != store.gallery_origin():
        raise HTTPException(403, "cross-origin form submissions are refused")
    sec_fetch_site = request.headers.get("sec-fetch-site")
    if sec_fetch_site is not None and sec_fetch_site != "same-origin":
        raise HTTPException(403, "cross-origin form submissions are refused")


def _render_decisions(rows: list[decisions.DecisionRow]) -> str:
    """The Decisions block: one `<form>` per open-or-answered decision, each with a
    submit button per lettered option -- the whole interaction the MVP exists to
    offer, and (per the "no JavaScript" rule) EVERY bit of it works with scripts
    disabled, because there are none. Returns "" when `rows` is empty so the block,
    and its `<h2>`, only ever appear on a page that actually has a decision to show.

    Every button shares `name="letter"`; a browser includes only the clicked
    button's own value in the submitted form, which is how a script-free form
    reports WHICH option was chosen. The recommended option (if any) is marked in
    the button's own text, not with a class or a data attribute this app also has no
    CSS or JS reason to add.

    An answered decision still renders every button: re-answering is allowed (see
    `decisions.answer`'s docstring) precisely so Jeremy can change his mind here,
    on the page, rather than needing a terminal.
    """
    if not rows:
        return ""
    items = []
    for row in rows:
        buttons = "".join(
            f'<button type="submit" name="letter" value="{escape(option["letter"])}">'
            f"({escape(option['letter'])}) {escape(option['label'])}"
            f"{' -- recommended' if option['letter'] == row.recommended else ''}"
            "</button>"
            for option in row.options
        )
        answered = ""
        if row.answer is not None:
            chosen = next(
                (o["label"] for o in row.options if o["letter"] == row.answer),
                row.answer,
            )
            note = f" -- {escape(row.answer_note)}" if row.answer_note else ""
            answered = (
                f"<p>Answered: ({escape(row.answer)}) {escape(chosen)}"
                f" at {escape(row.answered_at)}{note}</p>"
            )
        items.append(
            f"<li><p>{escape(row.question)}</p>{answered}"
            f'<form method="post" action="/d/{escape(row.id)}/answer">'
            f"{buttons}"
            f'<input name="note" placeholder="note (optional)"></form></li>'
        )
    return f"<section><h2>Decisions</h2><ul>{''.join(items)}</ul></section>"


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
        # Open or answered-but-not-closed decisions only: a closed one is the agent
        # saying it no longer needs an answer, so it has nothing left to show here.
        decision_rows = decisions.list_for_artifact(
            db, artifact_id, include_answered=True, include_closed=False
        )
        decisions_html = _render_decisions(decision_rows)
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
            f"{decisions_html}"
            f'<iframe src="{src}" sandbox="{sandbox}"'
            f' style="width:100%;height:100vh;border:0"></iframe>',
            headers=gallery_headers(),
        )

    @router.post("/d/{decision_id}/answer")
    def answer_decision(
        decision_id: str,
        letter: str = Form(...),
        note: str = Form(""),
        _guard: None = Depends(_same_origin_write),
    ):
        # `by="jeremy"` is hardcoded, not read from any header -- see the module
        # docstring for why: Authelia admits exactly one subject to this host, and
        # nothing here proves the ingress strips an inbound copy of an identity
        # header before this app ever sees the request.
        try:
            row = decisions.answer(
                db, decision_id, letter=letter, note=note, by="jeremy"
            )
        except store.BodyTooLarge as exc:
            raise HTTPException(413, f"{exc.field} exceeds the size limit") from exc
        except store.InvalidText as exc:
            raise HTTPException(422, f"{exc.field} is not valid UTF-8") from exc
        except store.SecretFound as exc:
            raise HTTPException(
                422, f"{exc.field} looks like it contains a secret"
            ) from exc
        except store.ScanFailed as exc:
            raise HTTPException(503, _SCAN_FAILED_DETAIL) from exc
        except decisions.InvalidAnswer as exc:
            raise HTTPException(422, "not one of this decision's options") from exc
        except store.NotFound as exc:
            raise HTTPException(404, "no such open decision") from exc
        return RedirectResponse(f"/a/{row.artifact_id}", status_code=303)

    return router
