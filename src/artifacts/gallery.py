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

from datetime import datetime

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


# The gallery's own look. It carries the SAME token names as the page design
# system in .claude/skills/lab-artifacts/design-system.md, so the chrome and the
# page it frames read as one document rather than two products -- Jeremy, 19 Sep
# 2026, on the first framed board: "page is ugly ... I don't see why questions are
# in the header and not on the page". Inline because the gallery CSP allows
# style-src 'self' 'unsafe-inline'; serving it as a file from 'self' is the next
# step (it is the same stylesheet an agent's page will be able to link).
_CSS = """
:root{
  --paper:#faf9f6; --ink:#191a17; --muted:#6c6d64; --card:#fff; --line:#e4e3db;
  --accent:#0f6f63; --accent-soft:rgba(15,111,99,.10); --accent-ink:#0b4f46;
  --ok:#2f7a45; --now:#b06a12;
  --shadow:0 1px 2px rgba(20,20,15,.05), 0 8px 24px -16px rgba(20,20,15,.25);
  --display:ui-serif,"Iowan Old Style",Georgia,"Times New Roman",serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root{
    --paper:#141619; --ink:#e9eae4; --muted:#989a90; --card:#1c1f23; --line:#2b2e34;
    --accent:#5ec9b8; --accent-soft:rgba(94,201,184,.12); --accent-ink:#9fe3d7;
    --ok:#6cc48a; --now:#e0a458;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 30px -20px rgba(0,0,0,.8);
  }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);
     font:16px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent-ink)}
.wrap{max-width:60rem;margin:0 auto;padding:1rem 1rem 2.5rem}
.bar{display:flex;align-items:baseline;gap:.7rem;flex-wrap:wrap;
     padding:.1rem 0 .9rem;border-bottom:1px solid var(--line);margin-bottom:1.3rem}
.bar a.home{color:var(--accent-ink);text-decoration:none;display:inline-flex;align-items:center;
  gap:.4rem;border:1px solid var(--line);background:var(--card);border-radius:999px;
  padding:.22rem .66rem;font-size:.8rem;white-space:nowrap}
.bar a.home:hover{border-color:var(--accent)}
.bar svg{width:.9em;height:.9em;fill:currentColor}
.bar h1{font:400 1.32rem/1.2 var(--display);margin:0;letter-spacing:-.01em;flex:1 1 12rem}
.bar .meta{font:.74rem/1.4 var(--mono);color:var(--muted);letter-spacing:.06em;
  text-transform:uppercase;white-space:nowrap}

/* asks */
.asks{margin:0 0 1.3rem}
.asks h2{font:600 .74rem/1.4 var(--mono);letter-spacing:.14em;text-transform:uppercase;
  color:var(--accent);margin:0 0 .55rem}
.ask{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--now);
  border-radius:.7rem;padding:.95rem 1.1rem;margin:0 0 .6rem;box-shadow:var(--shadow)}
.ask .q{margin:0 0 .75rem;font-size:1.04rem;font-weight:600}
.ask form{margin:0;display:flex;flex-wrap:wrap;gap:.45rem;align-items:center}
.ask button{font:inherit;font-size:.92rem;cursor:pointer;border-radius:.45rem;
  padding:.44rem .8rem;border:1px solid var(--line);background:transparent;color:var(--ink);
  text-align:left}
.ask button:hover{border-color:var(--accent);color:var(--accent-ink)}
/* RECOMMENDED is a hint: tinted, outlined, never solid. */
.ask button.rec{border-color:var(--accent);background:var(--accent-soft);
  color:var(--accent-ink)}
/* CHOSEN is a fact, and must outrank the hint: solid, so the eye lands on the
   answer Jeremy actually gave rather than on the one the agent suggested. The
   first version filled the RECOMMENDED button instead, and he read the filled
   button as his selection -- "better but I can't change selection", 19 Sep. */
.ask button.chosen,.ask button.rec.chosen{background:var(--accent);
  border-color:var(--accent);color:#fff;font-weight:600}
@media (prefers-color-scheme:dark){
  .ask button.chosen,.ask button.rec.chosen{color:#10201d}}
.ask .tag{font:.68rem/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;
  opacity:.8;margin-left:.45rem;white-space:nowrap}
.ask input[name=note]{font:inherit;font-size:.9rem;flex:1 1 11rem;min-width:8rem;
  padding:.42rem .6rem;border:1px solid var(--line);border-radius:.45rem;
  background:var(--paper);color:var(--ink)}
.ask .answered{margin:0 0 .7rem;font-size:.9rem;color:var(--ok);
  display:flex;gap:.4rem;align-items:baseline;flex-wrap:wrap}
.ask .answered b{font-weight:600}
.ask .answered .when{font:.72rem/1.4 var(--mono);color:var(--muted)}
.ask.done{border-left-color:var(--ok)}
.ask.done .q{font-weight:500}
.hint{margin:.1rem 0 0;font-size:.8rem;color:var(--muted)}

/* the page itself */
.sheet{border:1px solid var(--line);border-radius:.7rem;overflow:hidden;
  background:var(--card);box-shadow:var(--shadow)}
.sheet iframe{display:block;width:100%;height:78vh;border:0;background:var(--card)}

/* index */
.tiles{list-style:none;margin:0;padding:0;display:grid;gap:.8rem;
  grid-template-columns:repeat(auto-fill,minmax(15rem,1fr))}
.tiles li{margin:0}
.tiles a{display:block;background:var(--card);border:1px solid var(--line);
  border-radius:.7rem;padding:.95rem 1.05rem;text-decoration:none;color:var(--ink);
  box-shadow:var(--shadow);height:100%}
.tiles a:hover{border-color:var(--accent)}
.tiles .t{display:block;font:400 1.06rem/1.3 var(--display);margin-bottom:.45rem}
.tiles time{font:.72rem/1.4 var(--mono);color:var(--muted);letter-spacing:.05em}
.empty{color:var(--muted)}
"""

_HOME_SVG = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M7.1 1.3 1.4 6.2c-.3.2-.4.6-.2.9.2.3.6.4.9.2l.3-.3V14c0 .4.3.7.7.7h3.2c.4 0 .7-.3.7-.7v-3h2.1v3c0 .4.3.7.7.7h3.2c.4 0 .7-.3.7-.7V7l.3.3c.3.2.7.1.9-.2.2-.3.1-.7-.2-.9L8.9 1.3a1.4 1.4 0 0 0-1.8 0Z"/></svg>'


def _shell(title: str, body: str, meta: str = "", home: bool = True) -> str:
    """Every gallery page, wrapped the same way: one narrow bar carrying the way
    back, the title and a timestamp, then the body. Deliberately quiet -- the
    agent's page is the thing being read, and chrome that shouts competes with
    it."""
    back = f'<a class="home" href="/">{_HOME_SVG} Gallery</a>' if home else ""
    meta_html = f'<span class="meta">{escape(meta)}</span>' if meta else ""
    return (
        '<!doctype html><html lang="en-GB"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body>"
        f'<div class="wrap"><header class="bar">{back}'
        f"<h1>{escape(title)}</h1>{meta_html}</header>"
        f"{body}</div></body></html>"
    )


def _same_origin_write(request: Request) -> None:
    """The one CSRF guard this app needs, and (per the MVP's "no JavaScript, no CSP
    change" rule) the only kind it CAN have: there is no script here to mint or check
    a token, so the browser's own same-origin signals have to do the job instead.

    `Sec-Fetch-Site` is the primary signal when the browser sends it. The browser
    sets it from the request's initiator, page script cannot forge it, and -- unlike
    `Origin` -- it is not derived from the referrer policy. `same-origin` means this
    request was started by a document on this very origin, which is exactly and only
    what this route permits.

    `Origin` CANNOT be required alongside it. Firefox derives the `Origin` header
    from `Referrer-Policy`, and `gallery_headers()` sends `no-referrer` (it has to:
    that is what keeps a page's capability token out of a `Referer`). So a genuine
    click on this app's own form arrives from Firefox with `Origin: null`, and an
    equality check refuses the one user the whole page exists for -- observed live,
    2026-09-19, on the first real answer Jeremy tried to give. When `Sec-Fetch-Site`
    has already proved same-origin, `Origin` is therefore accepted as the gallery
    origin, as absent, or as the literal `null`, and refused for any OTHER value,
    which would mean the two headers disagree.

    When `Sec-Fetch-Site` is absent entirely (an older browser, a non-browser client)
    there is no Fetch Metadata to judge on, and the guard falls back to the strict
    `Origin` equality check -- where "missing" and "wrong" both fail, because both
    mean the one thing this guard looks for is not there.

    A non-browser client can of course send either header freely. That is not what
    this guard defends against: CSRF needs a victim's browser to attach Jeremy's
    Authelia session cookie automatically, and a client writing its own headers has
    no such cookie unless it already stole one.
    """
    refusal = HTTPException(403, "cross-origin form submissions are refused")
    sec_fetch_site = request.headers.get("sec-fetch-site")
    origin = request.headers.get("origin")

    if sec_fetch_site is not None:
        if sec_fetch_site != "same-origin":
            raise refusal
        if origin is not None and origin not in (store.gallery_origin(), "null"):
            raise refusal
        return

    if origin != store.gallery_origin():
        raise refusal


def _when(iso: str) -> str:
    """An ISO timestamp is a machine's format. Jeremy reads "19 Sep, 15:28".

    Falls back to the raw string if it will not parse: a stamp that looks odd is a
    cosmetic problem, an exception here would take down the whole page."""
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return moment.strftime("%-d %b, %H:%M")


def _button(option: dict, row: decisions.DecisionRow) -> str:
    """One option, carrying two independent marks whose ORDER matters.

    "You chose this" is what Jeremy is looking for when he comes back to a page, so
    it takes the solid fill and is the only tag shown when both apply. The first
    version filled the RECOMMENDED button instead, and he read that filled button as
    his own selection -- "better but I can't change selection", 19 Sep 2026, on a
    decision he had in fact already answered with a different letter.
    """
    chosen = option["letter"] == row.answer
    recommended = option["letter"] == row.recommended
    classes = " ".join(
        c for c in ("rec" if recommended else "", "chosen" if chosen else "") if c
    )
    if chosen:
        tag = '<span class="tag">&check; your answer</span>'
    elif recommended:
        tag = '<span class="tag">recommended</span>'
    else:
        tag = ""
    attr = f' class="{classes}"' if classes else ""
    return (
        f'<button type="submit" name="letter" value="{escape(option["letter"])}"{attr}>'
        f"({escape(option['letter'])}) {escape(option['label'])}{tag}</button>"
    )


def _render_decisions(rows: list[decisions.DecisionRow]) -> str:
    """The asks block: one `<form>` per open-or-answered decision, each with a submit
    button per lettered option -- the whole interaction the MVP exists to offer, and
    (per the "no JavaScript" rule) EVERY bit of it works with scripts off.

    Returned empty when there is nothing to ask, so the block, its heading and its
    hint only ever appear on a page that actually has a decision to show.

    Every button shares `name="letter"`; a browser includes only the clicked button's
    own value in the submitted form, which is how a script-free form reports WHICH
    option was chosen. The recommended option is marked BOTH in the button's own text
    (which survives a stylesheet that never loads) and with a class that makes it the
    filled, primary-looking one -- Jeremy answers by reaching for the recommendation
    first, so it should look like the default, not like the leftmost of a row of
    identical grey boxes.

    This block is drawn by the GALLERY, not by the agent's page, and that is a trust
    boundary rather than a layout choice: the page is sandboxed on its own origin and
    holds whatever HTML an agent published, so a button rendered inside it could forge
    an answer or imitate this one. Only this origin holds Jeremy's session. What is
    still owed (2026-09-19) is placement -- the page should be able to say WHERE its
    question belongs, and have this trusted control appear there, instead of the
    control always sitting above the page's own heading.

    An answered decision still renders every button: re-answering is allowed (see
    `decisions.answer`'s docstring) precisely so Jeremy can change his mind here, on
    the page, rather than needing a terminal.
    """
    if not rows:
        return ""
    items = []
    for row in rows:
        buttons = "".join(_button(option, row) for option in row.options)
        answered = ""
        done = ""
        if row.answer is not None:
            done = " done"
            chosen = next(
                (o["label"] for o in row.options if o["letter"] == row.answer),
                row.answer,
            )
            note = f" &middot; {escape(row.answer_note)}" if row.answer_note else ""
            answered = (
                f'<p class="answered">You chose <b>({escape(row.answer)})'
                f" {escape(chosen)}</b>"
                f'<span class="when">{escape(_when(row.answered_at))}{note}'
                "</span></p>"
            )
        items.append(
            f'<div class="ask{done}"><p class="q">{escape(row.question)}</p>{answered}'
            f'<form method="post" action="/d/{escape(row.id)}/answer">'
            f"{buttons}"
            '<input name="note" placeholder="note (optional)"></form></div>'
        )
    hint = (
        '<p class="hint">Pick a letter and it is saved straight away; add a note if '
        "the options do not fit. Your answer is the filled button &mdash; pick a "
        "different one to change it, as often as you like.</p>"
    )
    return (
        f'<section class="asks"><h2>The agent is asking you</h2>'
        f"{''.join(items)}{hint}</section>"
    )


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
        # A tile per page rather than a line per page: a wall of titles is how
        # Jeremy recognises the board he wants. A real preview of each page's
        # own content is the next step (it needs a rendering the gallery can
        # show without loading an agent's HTML into this origin).
        items = "".join(
            f'<li><a href="/a/{escape(row[0])}">'
            f'<span class="t">{escape(row[1])}</span>'
            f"<time>{escape(str(row[2])[:16])}</time></a></li>"
            for row in rows
        )
        body = (
            f'<ul class="tiles">{items}</ul>'
            if rows
            else '<p class="empty">No pages published yet.</p>'
        )
        return HTMLResponse(
            _shell("Artifacts", body, meta=f"{len(rows)} pages", home=False),
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
            _shell(
                row[0],
                f"{decisions_html}"
                f'<div class="sheet"><iframe src="{src}" sandbox="{sandbox}"'
                f' title="{escape(row[0])}"></iframe></div>',
            ),
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
