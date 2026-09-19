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
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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
            # script-src is 'self' ONLY -- no 'unsafe-inline', no CDN. gallery.js is
            # the one file this loads, served by this same router; the viewer script
            # moves and sizes the decisions block, it never becomes the thing that
            # submits one (the plain <form method="post"> below still does that on
            # its own, scripts on or off).
            "script-src 'self'; "
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

/* Already decided: one quiet line each, newest first, opening to the full
   card. <details> so it works with scripts off. */
.settled-list h2{margin-top:1.8rem}
.settled{border:1px solid var(--line);border-radius:.55rem;background:var(--card);
  margin:0 0 .35rem}
.settled summary{cursor:pointer;list-style:none;padding:.5rem .8rem;display:flex;
  gap:.6rem;align-items:baseline;flex-wrap:wrap;font-size:.92rem}
.settled summary::-webkit-details-marker{display:none}
.settled summary::before{content:"\25B8";color:var(--muted);font-size:.7rem;
  transition:transform .12s}
.settled[open] summary::before{transform:rotate(90deg)}
.settled summary:hover{border-color:var(--accent)}
.settled .sq{flex:1 1 14rem;color:var(--muted)}
.settled .sa{color:var(--ok);font-weight:600}
.settled .settled-body{padding:0 .55rem .55rem}
.settled .settled-body .ask{margin:0;border-left-color:var(--ok);box-shadow:none}
.settled .note{margin:.2rem .35rem .5rem;font-size:.9rem;white-space:pre-wrap}


/* the page itself */
/* position:relative -- gallery.js positions a moved decision block against THIS box,
   not the viewport, so its top/left are the iframe's own page coordinates unchanged.
   overflow:hidden stays for the iframe's rounded corners; a slot's rect is always
   inside the iframe's own content box (the height in the same message already sizes
   it), so it stays inside this box too -- there is no case where keeping it clips a
   block that is actually where the page said its slot was. */
.sheet{position:relative;border:1px solid var(--line);border-radius:.7rem;
  overflow:hidden;background:var(--card);box-shadow:var(--shadow)}
/* height:78vh is the SCRIPT-OFF floor: a fixed-height scrolling frame, exactly
   today's behaviour. gallery.js overrides it with an inline style once the framed
   page reports its real height -- inline always wins over this rule's specificity,
   so scripts-on and scripts-off never fight over which height applies. */
.sheet iframe{display:block;width:100%;height:78vh;border:0;background:var(--card)}
/* A decision block gallery.js has moved onto the sheet, positioned by its own
   inline top/left/width. Un-positioned blocks (still inside .asks) are never
   affected by this rule at all. */
.sheet .ask{position:absolute;margin:0;z-index:1}

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

# Served from 'self' at /gallery.js (script-src 'self' above; no inline script, no
# CDN, no framework, no dependency). One file, single-digit KB, so the CSP change
# stays a one-liner and there is nothing to bundle.
#
# What it does, and no more:
#  - Reads the iframe's OWN `src` for the content origin to check incoming messages
#    against -- never trusts a hostname the page reports, only the one the gallery
#    itself already put in that `src` attribute (`content_url`, in `detail()` below).
#  - Accepts a message only from `iframe.contentWindow` at that exact origin: both
#    `event.source` and `event.origin`, not either alone.
#  - Sizes the iframe to a reported height, but ONLY when it is a finite number
#    between 200 and 20000 -- anything else is dropped and the last good height (or
#    the CSS default, .sheet iframe{height:78vh}, before the first one ever arrives)
#    stands. A page that reports a billion pixels gets nothing.
#  - Moves an already-rendered `[data-decision-id]` block onto the sheet, absolutely
#    positioned at a reported slot's rect, but ONLY when that id is already one of
#    the elements the gallery rendered from the decisions IT loaded for this artifact
#    -- a `Map` built once from the DOM, never a lookup keyed on the reported value.
#    A slot for an id not in that map, or a second slot for an id already placed this
#    message, is silently ignored -- first one wins, same as the id check.
#  - A decision never reported drops out of this loop entirely and stays exactly
#    where the server rendered it, in the block above the page: the default, not a
#    failure. One already positioned, whose slot then stops being reported, is left
#    where it last was rather than snapped back -- nothing here ever un-positions a
#    block.
#  - The whole handler is one try/catch: a malformed message must never throw into
#    Jeremy's own signed-in tab, whatever an agent's page sends.
_GALLERY_JS = """(function(){
"use strict";
var iframe=document.getElementById("artifact-frame");
var sheet=document.getElementById("artifact-sheet");
if(!iframe||!sheet)return;
var contentOrigin;
try{contentOrigin=new URL(iframe.src).origin}catch(e){return}
var decisionEls=new Map();var lastSent="";
var asksSection=document.getElementById("open-asks");
document.querySelectorAll("[data-decision-id]").forEach(function(el){
  decisionEls.set(el.getAttribute("data-decision-id"),el);
});
function isNum(v){return typeof v==="number"&&isFinite(v)}
window.addEventListener("message",function(event){
  try{
    if(event.source!==iframe.contentWindow||event.origin!==contentOrigin)return;
    var data=event.data;
    if(!data||typeof data!=="object"||data.type!=="artifact-state")return;
    if(isNum(data.height)&&data.height>=200&&data.height<=20000){
      iframe.style.height=data.height+"px";
    }
    if(Array.isArray(data.slots)&&data.slots.length<=64){
      var seen=new Set();var sizes=[];
      data.slots.forEach(function(slot){
        if(!slot||typeof slot.id!=="string"||seen.has(slot.id))return;
        seen.add(slot.id);
        var el=decisionEls.get(slot.id);
        if(!el||!isNum(slot.top)||!isNum(slot.left)||!isNum(slot.width))return;
        if(el.parentNode!==sheet)sheet.appendChild(el);
        el.style.top=slot.top+"px";
        el.style.left=slot.left+"px";
        el.style.width=slot.width+"px";
        var h=el.offsetHeight;if(isNum(h))sizes.push({id:slot.id,height:h});
      });
      // Tell the page how tall each control it asked for actually turned out,
      // so its slot can reserve exactly that room. Without this the control is
      // positioned over content that never moved aside and the page reads as
      // broken. This is the COMPLETE set of slots filled this round, not just
      // the ones whose number moved: a slot the page still marks but nothing
      // fills any more -- an answered decision, which is no longer anchorable --
      // must collapse rather than leave a reserved gap, and the page can only
      // know that from a list it can diff against its own marks.
      //
      // Sent only when the whole set differs from the last one sent, which is
      // what keeps this from oscillating with the watcher on the page's side:
      // the page reserves room, its height changes, it reports again, the
      // controls measure the same, the set is identical, nothing is sent.
      var payload=JSON.stringify(sizes);
      if(payload!==lastSent){
        lastSent=payload;
        iframe.contentWindow.postMessage(
          {type:"artifact-slot-sizes",sizes:sizes},contentOrigin);
      }
      // A heading with nothing under it reads as a fault. When every open
      // question has been lifted into the page, the section they came from is
      // an empty "Waiting on you" and its hint, stranded above the frame.
      if(asksSection){
        asksSection.style.display =
          asksSection.querySelector("[data-decision-id]") ? "" : "none";
      }
    }
  }catch(e){}
});
})();
"""


def _shell(
    title: str, body: str, meta: str = "", home: bool = True, scripts: str = ""
) -> str:
    """Every gallery page, wrapped the same way: one narrow bar carrying the way
    back, the title and a timestamp, then the body. Deliberately quiet -- the
    agent's page is the thing being read, and chrome that shouts competes with
    it. `scripts` (default empty) is raw `<script src=...>` tag markup, never inline
    -- only `detail()` passes one, for the one page that has an iframe to size and
    decisions to place; it lands right before `</body>`, after everything it acts on
    already exists in the DOM."""
    back = f'<a class="home" href="/">{_HOME_SVG} Gallery</a>' if home else ""
    meta_html = f'<span class="meta">{escape(meta)}</span>' if meta else ""
    return (
        '<!doctype html><html lang="en-GB"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body>"
        f'<div class="wrap"><header class="bar">{back}'
        f"<h1>{escape(title)}</h1>{meta_html}</header>"
        f"{body}</div>{scripts}</body></html>"
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
    an answer or imitate this one. Only this origin holds Jeremy's session.

    Every `.ask` carries `data-decision-id`, which is how `gallery.js` finds THIS
    already-rendered, already-trusted block to move -- never how it looks one up: the
    script builds its own id->element map from exactly these attributes, and a
    reported id that is not in that map (because it names no decision on THIS
    artifact) matches nothing and moves nothing. A decision the page never marks a
    slot for is simply never moved, and stays right here, above the page -- the
    default, not a failure.

    An answered decision still renders every button: re-answering is allowed (see
    `decisions.answer`'s docstring) precisely so Jeremy can change his mind here, on
    the page, rather than needing a terminal.
    """
    if not rows:
        return ""

    open_rows = [r for r in rows if r.answer is None]
    settled_rows = [r for r in rows if r.answer is not None]

    def _card(row: decisions.DecisionRow, anchorable: bool = True) -> str:
        """`anchorable` carries the id the page's script matches a slot against.
        Only an OPEN decision gets one: a settled decision lives inside a collapsed
        <details> in the timeline, and a page that reported a slot for it would
        otherwise yank that card out of its own disclosure and drop it on the page."""
        buttons = "".join(_button(option, row) for option in row.options)
        anchor = f' data-decision-id="{escape(row.id)}"' if anchorable else ""
        return (
            f'<div class="ask"{anchor}>'
            f'<p class="q">{escape(row.question)}</p>'
            f'<form method="post" action="/d/{escape(row.id)}/answer">'
            f"{buttons}"
            '<input name="note" placeholder="note (optional)"></form></div>'
        )

    sections = []

    if open_rows:
        hint = (
            '<p class="hint">Pick a letter and it is saved straight away; add a note '
            "if the options do not fit. Your answer is the filled button &mdash; pick "
            "a different one to change it, as often as you like.</p>"
        )
        sections.append(
            '<section class="asks" id="open-asks"><h2>Waiting on you</h2>'
            + "".join(_card(row) for row in open_rows)
            + hint
            + "</section>"
        )

    if settled_rows:
        # Newest first: what he decided a minute ago is what he is checking, and
        # a decision from last week is history he scrolls to.
        settled_rows = sorted(
            settled_rows, key=lambda r: r.answered_at or "", reverse=True
        )
        entries = []
        for row in settled_rows:
            chosen = next(
                (o["label"] for o in row.options if o["letter"] == row.answer),
                row.answer,
            )
            note = (
                f'<p class="note">{escape(row.answer_note)}</p>'
                if row.answer_note
                else ""
            )
            # <details>, not a script: it collapses and expands with scripts off,
            # costs nothing, and keeps the page as fast as Jeremy said he liked.
            entries.append(
                '<details class="settled">'
                f'<summary><span class="when">{escape(_when(row.answered_at))}</span>'
                f'<span class="sq">{escape(row.question)}</span>'
                f'<span class="sa">({escape(row.answer)}) {escape(chosen)}</span>'
                "</summary>"
                f'<div class="settled-body">{note}'
                f"{_card(row, anchorable=False)}</div></details>"
            )
        sections.append(
            '<section class="asks settled-list"><h2>Already decided</h2>'
            + "".join(entries)
            + '<p class="hint">Open one to see the options again, or to change your '
            "mind &mdash; nothing here is locked.</p></section>"
        )

    return "".join(sections)


def gallery_router(db: Database) -> APIRouter:
    router = APIRouter()

    @router.get("/gallery.js")
    def gallery_script() -> Response:
        # Same header set as every other gallery response (gallery_headers() is cheap
        # and read at call time) -- one hardened profile for the whole origin, rather
        # than a script route that is the one response nobody checked.
        return Response(
            _GALLERY_JS,
            media_type="text/javascript; charset=utf-8",
            headers=gallery_headers(),
        )

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
                f'<div class="sheet" id="artifact-sheet"><iframe id="artifact-frame"'
                f' src="{src}" sandbox="{sandbox}" title="{escape(row[0])}"></iframe></div>',
                # gallery.js reads the content origin straight off this iframe's own
                # `src` -- fixed ids, not a class selector, so a future second iframe
                # on this page could never be mistaken for this one.
                scripts='<script src="/gallery.js" defer></script>',
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
