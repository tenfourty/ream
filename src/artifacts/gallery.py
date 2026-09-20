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

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from markupsafe import escape

from artifacts import decisions, events, stamp, store, thumbs
from artifacts import text as text_module
from artifacts.db import Database
from artifacts.store import content_domain, content_url, gallery_origin

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
/* The neutral scale is claude.ai's own, read off the saved page with
   getComputedStyle rather than sampled from a screenshot: page rgb(252,252,251),
   ink rgb(11,11,11), muted rgb(137,135,129), hairline rgba(11,11,11,.1). The
   ACCENT stays the homelab teal -- every template on this service shares it, and
   swapping it here would leave the gallery and the pages it frames looking like
   two different products. */
:root{
  --paper:#fcfcfb; --ink:#0b0b0b; --muted:#898781; --card:#fff;
  --line:rgba(11,11,11,.1);
  --accent:#0f6f63; --accent-soft:rgba(15,111,99,.10); --accent-ink:#0b4f46;
  --ok:#2f7a45; --now:#b06a12;
  /* The waiting badge is SOLID, so its background and its text are their own
     tokens rather than --now plus white. Computed, not chosen: white on
     --now (#b06a12) is 4.27:1, which fails for 11px bold; #9c5e10 takes it to
     5.22:1. In dark mode --now is a LIGHT amber and white on it is 2.18:1 --
     a clear fail -- so the dark scheme flips the text instead of the fill. */
  --now-solid:#9c5e10; --on-now:#fff;
  --line-hover:rgba(11,11,11,.28);
  /* The only red in the interface, and it is used for exactly one thing. */
  --danger:#a8271b; --danger-soft:rgba(168,39,27,.08);
  /* Text on a SOLID accent fill. Its own token for the same reason --on-now
     is: --accent is a light teal in dark mode, where white on it measures
     2.0:1. Third time this shape has come up, so it is now a habit -- any
     token used as a FILL needs a paired ink that flips with the scheme. */
  --on-accent:#fff;
  /* A control sitting ON a card: the dots chip, the pin's hover. It must be a
     step AWAY from the card in whichever direction reads as lifted. In light
     mode that is very slightly darker (the paper colour); in dark mode darker
     is a hole, so the dark block lifts it instead. Using --paper for both made
     the dots chip disappear into the dark card. */
  --chip:#fcfcfb; --chip-hover:#fff;
  --shadow:0 1px 2px rgba(20,20,15,.05), 0 8px 24px -16px rgba(20,20,15,.25);
  --display:ui-serif,"Iowan Old Style",Georgia,"Times New Roman",serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root{
    --paper:#141619; --ink:#e9eae4; --muted:#989a90; --card:#1c1f23; --line:#2b2e34;
    --accent:#5ec9b8; --accent-soft:rgba(94,201,184,.12); --accent-ink:#9fe3d7;
    --ok:#6cc48a; --now:#e0a458;
    /* Light amber, so the text on it goes dark: #2a1d0a on #e0a458 is 7.52:1. */
    --now-solid:#e0a458; --on-now:#2a1d0a;
    --line-hover:#5a606a;
    --danger:#f08b7f; --danger-soft:rgba(240,139,127,.12);
    --on-accent:#0b2723;  /* 7.92:1 on #5ec9b8 */
    --chip:#2b3038; --chip-hover:#343a44;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 30px -20px rgba(0,0,0,.8);
  }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);
     font:16px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent-ink)}
/* 62rem, not 60. The CONTENT box wants to be 960px -- the reference's column
   exactly, 3 x 304 + 2 x 24 -- and `box-sizing:border-box` means max-width
   includes the 1rem padding either side. At 60rem the usable column was 928
   and every card came out 293 instead of 304. Measured by rendering this wall
   beside the reference at the same width; it reads as correct in the
   stylesheet and is out by eleven pixels a card.
   48px above the title is measured off it too -- the old 1rem put the heading
   hard against the top of the window and made the whole page feel cramped
   before a single card had been read. */
.wrap{max-width:62rem;margin:0 auto;padding:3rem 1rem 3.5rem}
@media (max-width:40rem){.wrap{padding-top:1.5rem}}
/* No rule under the title. Measured: the reference's header is a 48px row with
   no border beneath it -- the wall's own top edge is the separation, and a rule
   as well is one line too many on a page that is mostly cards. */
.bar{display:flex;align-items:center;gap:.7rem;flex-wrap:wrap;
     min-height:48px;padding:0;margin:0}
/* An ARTIFACT page's bar: across the window, white, pinned to the top, and no
   rule under it -- the white against the page colour is the separation, which
   is what the reference does and is one line fewer. Measured at 1240x48 with
   `padding: 0 12px 0 46px`. The page below it keeps its own column, because
   the pages this service frames are written for about 52rem and a full-width
   frame would stretch every one of them. */
.bar.bleed{position:sticky;top:0;z-index:40;background:var(--card);
  padding:0 .75rem;margin:0 0 1.25rem;min-height:48px;flex-wrap:nowrap;
  box-shadow:0 1px 0 var(--line)}
.bar.bleed h1{font:500 .875rem/1.25rem ui-sans-serif,system-ui,-apple-system,
  sans-serif}
/* 14px, not the reference's 13: every other control in this interface is 14,
   and a single 13 would be the only one. */
.bar.bleed .menu > summary{font-size:.875rem}
/* On an artifact page this is a plain 28x28 icon button, as in the reference:
   a bordered pill beside a 14px title was the heaviest thing in a bar whose
   whole job is to stay out of the way. It keeps the label on the gallery
   index, where there is room and no title menu competing with it. */
.bar a.home{color:var(--ink);text-decoration:none;display:inline-flex;
  align-items:center;gap:.4rem;border:1px solid var(--line);background:var(--card);
  border-radius:999px;padding:.22rem .66rem;font-size:.8rem;white-space:nowrap}
.bar a.home:hover{border-color:var(--accent)}
.bar.bleed a.home{width:28px;height:28px;padding:0;justify-content:center;
  border:0;border-radius:7px;background:transparent;color:var(--muted)}
.bar.bleed a.home:hover{background:var(--chip);color:var(--ink)}
.bar.bleed a.home svg{width:1rem;height:1rem}
.bar a.home svg{width:.9em;height:.9em;fill:currentColor}
/* 24px/32px at weight 500, measured off the reference's own h1. This was a
   clamp running up to 3.1rem, which on a page of 14px cards read as a magazine
   masthead over a filing cabinet. */
.bar h1{font:500 1.5rem/2rem var(--display);margin:0;letter-spacing:-.01em;
  flex:1 1 12rem}
.bar .meta{display:flex;align-items:center;gap:.6rem;margin-left:auto;
  font:.8125rem/1.25rem ui-sans-serif,system-ui,-apple-system,sans-serif;
  color:var(--muted);white-space:nowrap}
/* What is waiting on him leads the header, and it is a link: one tap and he is
   at the first page asking. The only coloured thing in the bar, because on a
   wall whose whole purpose is decisions it is the only thing there that is
   about him rather than about the service. */
.bar .waiting{display:inline-flex;align-items:center;gap:.35rem;
  text-decoration:none;font-weight:600;color:var(--on-now);
  background:var(--now-solid);border-radius:999px;
  padding:.2rem .6rem .2rem .62rem}
.bar .waiting::after{content:"→";font-weight:400}
.bar .waiting:hover{filter:brightness(.94)}
/* Landing on the tile should not put it under the top edge of the window. */

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
/* A relayed answer is marked wherever it appears. Muted rather than loud: it is
   provenance, not an alarm -- but never absent, because an unmarked entry has
   to be readable as "he tapped this himself". */
.relayed{font:.68rem/1.4 var(--mono);letter-spacing:.06em;text-transform:uppercase;
  color:var(--now);border:1px solid currentColor;border-radius:.3rem;
  padding:.1rem .34rem;margin-left:.45rem;white-space:nowrap}
.relayed-note{font-size:.88rem;color:var(--muted);border-left:2px solid var(--now);
  padding-left:.7rem;margin:.2rem 0 .6rem}
.ask.done{border-left-color:var(--ok)}
.ask.done .q{font-weight:500}
.hint{margin:.1rem 0 0;font-size:.8rem;color:var(--muted)}

/* Already decided: one quiet line each, newest first, opening to the full
   card. <details> so it works with scripts off. */
.settled-list h2{margin-top:0;display:inline}
/* The whole settled block is one collapsed line below the page. Four questions
   Jeremy had already answered took his entire first screen on 20 Sep 2026; the
   page they were attached to began underneath them. */
.settled-list{margin-top:1.8rem}
.settled-list > summary{cursor:pointer;list-style:none;padding:.45rem 0;
  color:var(--muted)}
.settled-list > summary::-webkit-details-marker{display:none}
.settled-list > summary::before{content:"";margin-right:.5rem;display:inline-block;
  width:0;height:0;border:.3rem solid transparent;border-left-color:currentColor;
  vertical-align:.05em;transition:transform .12s}
.settled-list[open] > summary::before{transform:rotate(90deg)}
.settled{border:1px solid var(--line);border-radius:.55rem;background:var(--card);
  margin:0 0 .35rem}
.settled summary{cursor:pointer;list-style:none;padding:.5rem .8rem;display:flex;
  gap:.6rem;align-items:baseline;flex-wrap:wrap;font-size:.92rem}
.settled summary::-webkit-details-marker{display:none}
.settled summary::before{content:"";display:inline-block;width:0;height:0;
  border:.28rem solid transparent;border-left-color:var(--muted);
  vertical-align:.05em;flex:0 0 auto;transition:transform .12s}
.settled[open] summary::before{transform:rotate(90deg)}
.settled summary:hover{border-color:var(--accent)}
.settled .sq{flex:1 1 14rem;color:var(--muted)}
.settled .sa{color:var(--ok);font-weight:600}
.settled .settled-body{padding:0 .55rem .55rem}
.settled .settled-body .ask{margin:0;border-left-color:var(--ok);box-shadow:none}
.settled .note{margin:.2rem .35rem .5rem;font-size:.9rem;white-space:pre-wrap}
/* Whether the agent has actually collected the answer. Shown in the summary line
   ONLY when it has not -- an answer that landed needs no announcement, and the one
   that did not is the whole reason this is on the page. */
.settled .unread{color:var(--now);font-size:.82rem;font-weight:600}
.settled .pickup{margin:.2rem .35rem .5rem;font-size:.82rem;color:var(--muted)}


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
   affected by this rule at all.

   `transform:translateZ(0)` is NOT a cosmetic hint and must not be tidied away.
   It forces this block onto its own compositing layer. WebKit composites an
   iframe separately, and an overlay that shares a layer with the page behind it
   can lose HIT-TESTING to that iframe on iOS even while painting correctly on
   top of it -- the tap goes to the sandboxed page underneath, which does
   nothing at all. Jeremy, 20 Sep 2026, on his iPhone: "no feedback, nothing
   changed it was like my tap wasn't accepted", with the buttons plainly
   visible. Invisible on every desktop browser, which is all of them.

   z-index alone is not enough here: it orders PAINTING within a stacking
   context and does not decide which layer a touch is delivered to. */
.sheet .ask{position:absolute;margin:0;z-index:1;
  transform:translateZ(0);isolation:isolate}


/* MENUS: the title dropdown and the per-tile dots. Modelled on the claude.ai
   artifact bar Jeremy sent through — a compact bar whose title carries the
   actions, rather than controls parked at the bottom of the page.
   <details>, not script: everything else here works with scripts off. */
.menu{position:relative;display:inline-block}
/* SANS, not the display serif. The page bar is chrome and the reference sets
   its title in the UI face; the serif here made a 14px control look like a
   heading that had been shrunk. The serif belongs to the gallery's own <h1>
   and to the pages this service frames, not to a toolbar. */
.menu > summary{cursor:pointer;list-style:none;display:inline-flex;align-items:center;
  gap:.35rem;border-radius:6px;padding:.22rem .5rem;
  font:400 .875rem/1.25rem ui-sans-serif,system-ui,-apple-system,"Segoe UI",
  Roboto,sans-serif;color:var(--ink);white-space:nowrap;
  max-width:min(60vw,26rem);overflow:hidden;text-overflow:ellipsis}
.menu > summary::-webkit-details-marker{display:none}
.menu > summary:hover{background:var(--accent-soft)}
.menu .chev{display:inline-block;width:0;height:0;flex:0 0 auto;
  border:.26rem solid transparent;border-top-color:currentColor;
  margin-top:.28em;opacity:.55}
.menu[open] > summary{background:var(--accent-soft)}
.mpanel{position:absolute;z-index:20;top:calc(100% + .35rem);left:0;min-width:16rem;
  background:var(--card);border:1px solid var(--line);border-radius:.6rem;
  box-shadow:0 2px 6px rgba(20,20,15,.06),0 14px 34px -18px rgba(20,20,15,.45);
  padding:.35rem;text-align:left}
.mpanel.right{left:auto;right:0}
.mhead{padding:.5rem .6rem .55rem;border-bottom:1px solid var(--line);margin-bottom:.3rem}
.mhead b{display:block;font-weight:600;font-size:.88rem}
.mhead span{font-size:.8rem;color:var(--muted)}
/* One row shape for links and submit buttons alike, so a form in the menu is
   indistinguishable from a link — the user should not be able to tell which
   actions happen to need a POST. */
/* Every row in every menu is this: icon, label, one line, one height.
   Rename and Copy link used to be inline forms with a text field, which made a
   four-item menu three different shapes -- they open a dialog now instead. */
.mrow{display:flex;align-items:center;gap:.6rem;width:100%;min-height:32px;
  font:inherit;font-size:.92rem;text-align:left;text-decoration:none;
  color:var(--ink);background:transparent;border:0;border-radius:.4rem;
  padding:.44rem .6rem;cursor:pointer}
.mrow:hover{background:var(--accent-soft);color:var(--accent-ink)}
.mi{width:1em;height:1em;flex:0 0 auto;fill:currentColor;opacity:.75}
.mpanel form{margin:0}

/* The per-tile dots. Sits over the tile rather than inside its link, because an
   interactive control nested in an <a> is invalid and behaves unpredictably. */
.tile-wrap{position:relative}
.tile-wrap .menu{position:absolute;top:12px;right:12px;z-index:5}
/* A pale chip, ALWAYS VISIBLE -- not revealed on hover. Jeremy reads this wall
   on a phone, where there is no hover at all, and a control that only appears
   under a pointer is a control he does not have. It used to fade in on
   :hover with a (hover:none) escape hatch; two rules that disagree about
   whether something exists is one rule too many. */
/* 32x32, radius 8, NO BORDER, on the paper colour -- measured off the
   reference's own "More options" button. This was a bordered white chip with
   padding-derived size, which is most of why it read as stuck on rather than
   part of the card. */
.tile-wrap .menu > summary{display:flex;align-items:center;justify-content:center;
  width:32px;height:32px;background:var(--chip);border:0;border-radius:8px;
  line-height:0;box-shadow:0 1px 3px rgba(20,20,15,.10);transition:background .12s}
.tile-wrap .menu > summary:hover,.tile-wrap .menu[open] > summary,
.tile-wrap .menu > summary:focus-visible{background:var(--chip-hover);
  box-shadow:0 2px 6px rgba(20,20,15,.16)}
/* Three dots drawn in CSS rather than as a character: the unicode vertical
   ellipsis renders as a different width in every font on the box, and at this
   size the difference is the whole control. */
.dots,.dots::before,.dots::after{display:block;width:3px;height:3px;
  border-radius:50%;background:var(--muted)}
.dots{position:relative;margin:4px 1px}
.dots::before,.dots::after{content:"";position:absolute;left:0}
.dots::before{top:-5px} .dots::after{top:5px}
/* The panel must not be clipped by the tile, and must not sit under the tile
   next door. */
.tile-wrap .menu[open]{z-index:30}
/* The panel hangs left from the dots, so on the leftmost column it runs toward
   the edge of the screen. An <input> with no `size` is 20 characters wide by
   default -- two of them made this panel 340px, which at 390px viewport width
   pushed it off the left of the page with the rename field unreachable.
   Measured, not guessed: rendered at 390 and looked at.
   So: bounded by the viewport, and the inputs are told to shrink. */
.tmenu .mpanel{min-width:0;width:15rem;max-width:calc(100vw - 2rem)}
/* No special cases left in here: the two rows that needed a text field are
   dialogs now, so a menu panel is a plain stack of identical rows. */
.mlink{display:flex;align-items:center;gap:.5rem;margin:0}
.mlink input{font:inherit;font-size:.82rem;font-family:var(--mono);flex:1 1 auto;
  min-width:5rem;border:1px solid var(--line);border-radius:.4rem;
  padding:.24rem .4rem;background:var(--paper);color:var(--muted)}
.mcopy{font:inherit;font-size:.82rem;cursor:pointer;border-radius:.4rem;
  border:1px solid var(--line);background:var(--card);color:var(--ink);
  padding:.24rem .5rem;flex:0 0 auto}
.mcopy:hover{border-color:var(--accent);color:var(--accent-ink)}

/* index. Geometry measured off claude.ai's own gallery: a 304x234 card, of
   which the preview is 160 -- so the picture is 68% of the tile and its own
   aspect is 1.9:1, much wider than the 1.4:1 this used, and it shows more of
   the page's first screen. Columns are 304 with a 24px gap inside a 960px
   column, which is exactly 3 x 304 + 2 x 24. Kept responsive rather than
   fixed at 304, because this wall is also read on a phone. */
.tiles{list-style:none;margin:0;padding:0;display:grid;gap:1.5rem;
  grid-template-columns:repeat(auto-fill,minmax(17rem,1fr))}
.tiles li{margin:0}
/* The card clips its own corners so the preview can bleed to three edges
   without needing a radius of its own -- that is how the reference does it, and
   it is why the picture never shows a seam.
   NO SHADOW. Measured: box-shadow on claude.ai's card is `none`, and the card
   is separated from the page by a 1px hairline at 10% ink and nothing else.
   This used a two-layer shadow, which is what made the old wall feel heavy. */
/* `.tiles > li > a`, NOT `.tiles a`. As a descendant selector this matched
   every anchor inside the wall, including the menu rows -- which is why
   Rename and Copy link rendered as bordered boxes with the icon stacked above
   the label while the <button> rows beside them were fine. Measured, not
   guessed: getComputedStyle on the rows showed flex-direction column and a 1px
   border on the anchors only. */
.tiles > li > a{display:flex;flex-direction:column;background:var(--card);
  border:1px solid var(--line);border-radius:12px;overflow:hidden;
  text-decoration:none;color:var(--ink);height:100%;
  transition:border-color .12s}
.tiles > li > a:hover{border-color:var(--line-hover)}
/* The picture. Cropped to the top of the page: a thumbnail is the part a
   reader recognises, not a squashed whole document. A fixed aspect box means
   the grid does not reflow as lazy images arrive.
   It fills the card's width outright now rather than escaping its padding with
   negative margins -- the text block below carries the padding instead, which
   is both the reference's structure and one fewer number to keep in step. */
.tiles .shot{display:block;width:100%;
  /* height:auto is load-bearing. The img carries width/height ATTRIBUTES so the
     grid does not reflow as lazy images arrive, and a height attribute makes the
     used height not-auto — which stops `aspect-ratio` computing anything at all.
     Measured: the box came out 299x640 against a 900x640 image, so `cover`
     scaled to the full height and cropped the sides, giving a hugely magnified
     slice of each page. Rendering it and measuring the box is what found this;
     it reads as correct in the stylesheet. */
  height:auto;aspect-ratio:304/160;object-fit:cover;object-position:top center;
  background:var(--paper);border-bottom:1px solid var(--line)}
/* The same box, with no picture in it. Shares every dimension with the real
   one so a wall of both is a grid rather than a ragged edge -- and it is not
   empty: it carries what the page SAYS it is about, which is the one thing a
   tile without a picture still has. The reference has no description on a tile
   at all, because its preview always renders; here a page published before
   thumbnails existed would otherwise be a blank rectangle with a name. */
.tiles .shot.none{display:block;padding:.85rem .95rem;overflow:hidden;
  background:linear-gradient(160deg,var(--paper),var(--card));
  border-bottom:1px solid var(--line)}
.tiles .shot.none .d{display:-webkit-box;-webkit-line-clamp:4;
  -webkit-box-orient:vertical;overflow:hidden;font-size:.82rem;line-height:1.5;
  color:var(--muted);margin:0}
.tiles .shot.none .mi{width:1.1rem;height:1.1rem;opacity:.25;display:block;
  margin:0 0 .4rem}
/* Everything below the picture, in one padded column. 14px padding and an 8px
   gap are the reference's, measured. */
.tiles .body{display:flex;flex-direction:column;gap:.5rem;padding:14px;
  flex:1 1 auto}
/* The title is SANS at 14/20 and weight 500, not the serif display face. On a
   304px card the serif at 17px wrapped to three lines and fought the page's own
   title showing through the preview right above it.
   Up to TWO LINES. The reference clamps to one, which works because its titles
   are short; a homelab page is called "Warpgate: the gate Jeremy comes through"
   and one line would cut it mid-word.
   The second line is NOT reserved. It was, briefly, to keep every body the
   same height -- but the grid already equalises a row, and reserving a line
   that is usually empty added twenty pixels of nothing to every card. Seen
   only by putting this wall beside the reference at the same width: mine was
   visibly looser and that was most of the reason. What made rows ragged was
   the waiting badge sitting in the body, and that now rides the picture. */
.tiles .t{font:500 .875rem/1.25rem ui-sans-serif,system-ui,
  -apple-system,"Segoe UI",Roboto,sans-serif;
  overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical}
/* Anything waiting on Jeremy is the one thing on a tile that should interrupt
   him, so it is the only coloured element and it sits above the metadata. */
/* The preview and anything sitting on top of it. */
.tiles .frame{position:relative;display:block}
/* The one thing on this wall that should be legible without reading. Solid, on
   the picture, bottom-left where the eye lands after the thumbnail -- and out
   of the body, so it cannot make its tile taller than the row.
   Solid rather than tinted BECAUSE it sits on a page render: a 10% wash over
   arbitrary content is legible on some pages and invisible on others, which is
   the one thing this badge may never be. */
.tiles .wait{position:absolute;left:.6rem;bottom:.6rem;z-index:1;
  font:600 .6875rem/1rem ui-sans-serif,system-ui,-apple-system,sans-serif;
  letter-spacing:.01em;color:var(--on-now);background:var(--now-solid);
  border-radius:.3rem;padding:.24rem .45rem;
  box-shadow:0 1px 4px rgba(20,20,15,.28)}
/* And the card says so at its edge, so a wall scanned at arm's length still
   sorts itself. One hairline, not a thick rule: it should register, not shout
   over the pinned tile next to it. */
.tiles > li.asking > a{border-color:var(--now)}
.tiles .m{display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;
  font:.75rem/1rem ui-sans-serif,system-ui,-apple-system,sans-serif;
  color:var(--muted);margin:auto 0 0}
.tiles time{font:inherit;color:var(--muted);white-space:nowrap}
.tiles .dot{color:var(--muted);opacity:.6}
/* The pin marker ends the meta row, as in the reference. It FLOWS in the row
   rather than being positioned onto it: absolutely positioned at the tile's
   bottom-right it landed below the date's baseline and rendered as a speck of
   dirt on the screen -- rendering the wall and looking at it is what found
   that, and it read as correct in the stylesheet.
   It carries the word as well as the glyph: at this size a lone pin is not
   recognisable as a pin, and the word is also what a screen reader gets. */
/* The pin BUTTON on a tile -- the reference's 24x24, in the meta row. It is
   the indicator and the control at once: this wall used to show a "Pinned"
   label and hide the action in the menu, so pinning took two clicks and a
   read. A sibling of the tile's <a>, like the dots, because a button inside a
   link is invalid and the link swallows the click. */
.pinbtn{position:absolute;margin:0;line-height:0}
/* On a TILE it sits in the meta row; on a ROW it sits beside the dots. */
.tile-wrap > .pinbtn{right:.75rem;bottom:.7rem}
.row-wrap > .pinbtn{right:2.4rem;top:50%;transform:translateY(-50%)}
/* FULL OPACITY. This shipped at .5, which computes to 1.76:1 against a white
   card -- an icon wants 3:1 and that is below even the text-on-decorative bar.
   Jeremy's report was "I still don't see the pin", and he was right: it was
   not subtle, it was invisible. At 1 it is 3.59:1 light and 5.8:1 dark.
   Fading a control to hint that it is secondary only works while it can still
   be seen; past that it is just absent. */
.pinbtn button{display:flex;align-items:center;justify-content:center;
  width:24px;height:24px;padding:0;border:0;border-radius:6px;cursor:pointer;
  background:transparent;color:var(--muted);
  transition:background .12s,color .12s}
.pinbtn button:hover{background:var(--chip);color:var(--ink)}
.pinbtn button .mi{width:.9rem;height:.9rem;opacity:1}
/* Pinned: on, accented, and never faded -- it is state now, not an affordance
   waiting to be noticed. */
.pinbtn.on button{opacity:1;color:var(--accent);background:var(--accent-soft)}
.pinbtn.on button:hover{color:var(--accent-ink)}
/* The meta row leaves room for it rather than running underneath. */
.tiles .m{padding-right:1.9rem}

/* A pinned tile leads the wall; the accent edge says why, without shouting. */
.tiles > li.pinned > a{border-color:var(--accent)}
.empty{color:var(--muted);padding:2rem 0}

/* --- the rename dialog -------------------------------------------------------
   :target, not <dialog>. A real dialog needs showModal() and this gallery works
   with scripts off -- a rename that exists only when JavaScript ran is a rename
   that is missing on the day something else on the page threw. The fragment
   does the whole job and costs nothing in gallery.js, which has a size budget.
   Hidden by default and shown when the URL names it; the backdrop is a link
   back to the page, so clicking away closes it exactly as it should. */
.dlg{display:none}
.dlg:target{display:block;position:fixed;inset:0;z-index:100}
.dlg-back{position:absolute;inset:0;background:rgba(11,11,11,.45);
  backdrop-filter:blur(2px)}
.dlg-card{position:relative;margin:12vh auto 0;max-width:26rem;
  width:calc(100% - 2rem);background:var(--card);border:1px solid var(--line);
  border-radius:12px;box-shadow:0 12px 40px -12px rgba(11,11,11,.4);
  padding:1.15rem 1.25rem 1rem}
.dlg-card h2{margin:0 0 .8rem;font:500 1rem/1.4 ui-sans-serif,system-ui,
  -apple-system,sans-serif;color:var(--ink);letter-spacing:0;text-transform:none}
.dlg-card input{font:inherit;font-size:.95rem;width:100%;padding:.5rem .6rem;
  border:1px solid var(--line);border-radius:.5rem;background:var(--paper);
  color:var(--ink)}
.dlg-card input:focus{outline:2px solid var(--accent);outline-offset:1px}
.dlg-actions{display:flex;justify-content:flex-end;gap:.5rem;margin:1rem 0 0}
.btn{font:inherit;font-size:.875rem;cursor:pointer;text-decoration:none;
  display:inline-flex;align-items:center;border:1px solid var(--line);
  border-radius:.5rem;padding:.4rem .8rem;background:var(--card);color:var(--ink)}
.btn:hover{border-color:var(--line-hover)}
.btn.primary{background:var(--accent);border-color:var(--accent);
  color:var(--on-accent)}
.btn.primary:hover{filter:brightness(1.06)}
.empty a{color:var(--accent-ink)}

/* --- the controls row -------------------------------------------------------
   Its own row beneath the title, 16px below it and 32px tall, which is the
   reference's rhythm measured: title row top 48 height 48, controls top 112
   height 32. They used to share the title's line, which is what made the
   header read as a toolbar someone had run out of room on. */
.controls{display:flex;align-items:center;gap:1rem;flex-wrap:wrap;
  min-height:32px;margin:1rem 0 2rem}
.cright{display:flex;align-items:center;gap:.6rem;margin-left:auto}
.tabs{display:flex;align-items:center;gap:.15rem;flex-wrap:wrap}
.tab{display:inline-flex;align-items:center;gap:.4rem;height:32px;
  padding:0 .75rem;border-radius:8px;text-decoration:none;color:var(--muted);
  font:500 .875rem/1.25rem ui-sans-serif,system-ui,-apple-system,sans-serif}
.tab:hover{color:var(--ink);background:var(--card)}
.tab.on{color:var(--ink);background:var(--card);
  box-shadow:inset 0 0 0 1px var(--line)}
.tab .tn{font-size:.75rem;color:var(--muted);background:var(--paper);
  border-radius:999px;padding:.05rem .36rem;box-shadow:inset 0 0 0 1px var(--line)}
.tab.on .tn{background:var(--paper)}

/* Search. A GET form, so a search is a URL he can keep. */
.find{display:flex;align-items:center;gap:.4rem;height:28px;margin:0;
  padding:0 .5rem;border:1px solid var(--line);border-radius:7px;
  background:var(--card)}
.find:focus-within{border-color:var(--accent)}
.find .mi{width:.85rem;height:.85rem;opacity:.55;flex:0 0 auto}
.find input[type=search]{font:inherit;font-size:.8125rem;border:0;outline:0;
  background:transparent;color:var(--ink);width:9rem;min-width:0;padding:0}
.find input[type=search]::-webkit-search-cancel-button{display:none}
.clearq{text-decoration:none;color:var(--muted);font-size:1rem;line-height:1;
  padding:0 .1rem}
.clearq:hover{color:var(--ink)}
@media (max-width:40rem){
  .find input[type=search]{width:6rem}
  .cright .count{display:none}
}

/* --- archived ---------------------------------------------------------------
   These were a bare <details> in the browser's own styling -- a black triangle
   and unstyled text under a wall that had been designed -- which is exactly how
   it looked. It is a real section now: a rule to separate it from the live
   wall, a summary that behaves like the other controls here, and contents that
   read as SECONDARY, because a page off the wall should not compete with one on
   it. Still counted and still one line until opened: out of the way must never
   become lost. */
.archived-list{margin:2.5rem 0 0;border-top:1px solid var(--line);padding-top:1rem}
.archived-list > summary{cursor:pointer;list-style:none;display:inline-flex;
  align-items:center;gap:.5rem;border-radius:.5rem;padding:.3rem .6rem .3rem .45rem;
  font:500 .8125rem/1.25rem ui-sans-serif,system-ui,-apple-system,sans-serif;
  color:var(--muted);user-select:none}
.archived-list > summary::-webkit-details-marker{display:none}
.archived-list > summary:hover{background:var(--card);color:var(--ink)}
.archived-list > summary .n{color:var(--muted);font-weight:400;opacity:.85}
.archived-list > summary .chev{display:inline-block;width:0;height:0;flex:0 0 auto;
  border-left:5px solid currentColor;border-top:4px solid transparent;
  border-bottom:4px solid transparent;transition:transform .12s}
.archived-list[open] > summary .chev{transform:rotate(90deg)}
.archived-body{padding-top:.9rem}
/* Off the wall, so: quieter. The pictures recede, the type greys, and it comes
   back on hover for anything he actually reaches for. */
.archived-body .shot,.archived-body .rshot{opacity:.55;filter:saturate(.65)}
.archived-body .t,.archived-body .rt{color:var(--muted)}
.archived-body .tiles > li > a,.archived-body .row{background:transparent}
.archived-body .tiles > li > a:hover .t,
.archived-body .row:hover .rt{color:var(--ink)}
.archived-body .tiles > li > a:hover .shot,
.archived-body .row:hover .rshot{opacity:1;filter:none}
.archived-body .daygroup:first-child{margin-top:0}

/* --- the list view ---------------------------------------------------------
   Measured off the reference's own list: a 12px/17px weight-500 muted heading
   with 6px 12px padding, and a 40px row whose title is 14/20 at weight 400 --
   LIGHTER than the card title, because in a list the title is the only thing
   and does not need to win against anything. */
.daygroup{font:500 .75rem/1.0625rem ui-sans-serif,system-ui,-apple-system,
  sans-serif;color:var(--muted);margin:1.3rem 0 .15rem;padding:.375rem .75rem}
.daygroup:first-child{margin-top:0}
.rows{list-style:none;margin:0;padding:0}
.row-wrap{position:relative;margin:0}
/* 52px, measured off the reference's own row -- the 40 this used came from an
   inner element of theirs, not the row. Room on the right for the pin and the
   dots, which sit over it as siblings of the link. */
.row{display:flex;align-items:center;gap:.75rem;min-height:52px;
  padding:.25rem 4.6rem .25rem .5rem;border-radius:.6rem;
  text-decoration:none;color:var(--ink)}
.row:hover{background:var(--card)}
/* The type-icon slot. Types do not exist yet, so it carries the page's own
   thumbnail -- already rendered, and more use than a generic glyph. */
.rshot{flex:0 0 auto;width:36px;height:36px;border-radius:.45rem;
  object-fit:cover;object-position:top center;background:var(--paper);
  border:1px solid var(--line)}
.rshot.none{display:flex;align-items:center;justify-content:center}
.rshot.none .mi{width:1rem;height:1rem;opacity:.3}
.rt{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;font:400 .875rem/1.25rem ui-sans-serif,system-ui,
  -apple-system,sans-serif}
.rmeta{flex:0 1 auto;display:flex;align-items:center;gap:.55rem;
  font:.75rem/1rem ui-sans-serif,system-ui,-apple-system,sans-serif;
  color:var(--muted);white-space:nowrap}
/* In a row the badge is inline rather than laid over a picture, so it goes
   back to being tinted -- there is no arbitrary page render underneath it to
   be illegible against. */
.rows .wait{position:static;box-shadow:none;background:transparent;
  color:var(--now);border:1px solid currentColor;padding:.1rem .36rem}
.row-wrap .menu{position:absolute;top:50%;right:.35rem;transform:translateY(-50%);
  z-index:5}
.row-wrap .menu > summary{display:flex;align-items:center;justify-content:center;
  width:32px;height:32px;background:transparent;border:0;border-radius:8px;
  line-height:0}
.row-wrap .menu > summary:hover,.row-wrap .menu[open] > summary{
  background:var(--chip)}
.row-wrap .menu[open]{z-index:30}
.row-wrap.asking .row{box-shadow:inset 2px 0 0 var(--now-solid)}
/* WHAT GOES FIRST WHEN THERE IS NO ROOM. A list is scanned for titles, so the
   title wins and the metadata yields -- in order: the agent, then the waiting
   badge (the amber edge on the row still says a question is there), then the
   time. Rendered at 390px, where the badge had pushed one row's title out
   COMPLETELY: a row with no title is not a row.
   `.rmeta` may also shrink now; it was flex:0 0 auto, which is what let it
   take the whole width and leave the title nothing. */
.rmeta{min-width:0}
@media (max-width:46rem){.rmeta .who{display:none}}
@media (max-width:34rem){
  .rows .wait{display:none}
  .rmeta time{display:none}
}

/* The view toggle. Sized to the reference's 28px control. */
.vtoggle{display:inline-flex;align-items:center;justify-content:center;
  width:28px;height:28px;border-radius:7px;border:1px solid var(--line);
  color:var(--ink);background:var(--card);text-decoration:none}
.vtoggle:hover{border-color:var(--accent);color:var(--accent-ink);
  background:var(--chip-hover)}
.vtoggle .mi{width:.95rem;height:.95rem;opacity:.85}

/* --- deleted, and the way back ---------------------------------------------
   Its own view, not a fold under the wall: the wall answers "what is there",
   this answers "what am I about to lose", and a deleted page sitting anywhere
   near the live ones is how "deleted" stops meaning anything. */
.binnote{color:var(--muted);font-size:.8125rem;margin:0 0 1rem;max-width:40rem}
.rows.bin .rshot.none .mi{opacity:.4}
.left{font-variant-numeric:tabular-nums}
/* The one about to go says so. Counted DOWN, not up: "deleted 4 days ago" is a
   fact about the past, and this list is read for a decision about the future. */
.left.soon{color:var(--now-solid);font-weight:600}
.restore{display:inline-flex;align-items:center;gap:.3rem;font:inherit;
  font-size:.75rem;cursor:pointer;border:1px solid var(--line);border-radius:.4rem;
  background:var(--card);color:var(--ink);padding:.2rem .45rem}
.restore:hover{border-color:var(--accent);color:var(--accent-ink)}
.restore .mi{width:.8rem;height:.8rem}
.rows.bin form{margin:0}

/* Destroy. The only red in the interface, and the only control that asks you
   to type something -- not because a second click is meaningful, he clicks
   through those, but because this is the one operation in the service that can
   lose something he said, and the words have to be in front of him at the
   moment he commits to it. */
.mdanger{border-top:1px solid var(--line);margin-top:.3rem;padding-top:.3rem}
.mrow.danger{color:var(--danger)}
.mrow.danger:hover{background:var(--danger-soft);color:var(--danger)}
.destroyform{padding:.6rem;display:flex;flex-direction:column;gap:.5rem}
.destroyform p{margin:0;font-size:.8125rem;color:var(--muted);line-height:1.45}
.destroyform input{font:inherit;font-size:.8125rem;padding:.3rem .45rem;
  border:1px solid var(--line);border-radius:.4rem;background:var(--paper);
  color:var(--ink);width:100%}
.destroyform .danger{font:inherit;font-size:.8125rem;font-weight:600;
  cursor:pointer;border:1px solid var(--danger);border-radius:.4rem;
  background:transparent;color:var(--danger);padding:.3rem .5rem}
.destroyform .danger:hover{background:var(--danger);color:#fff}
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

// The menus, upgraded. All of it works with scripts off already -- they are
// <details>, the forms submit, and the link sits in a selectable input -- so
// nothing here is load-bearing. Separate from the frame code above, which
// returns immediately on any page with no iframe, i.e. every gallery index.
(function(){
"use strict";
function shut(e){document.querySelectorAll("details.menu[open]").forEach(function(d){
  if(e&&d.contains(e.target))return;
  d.open=false;
  var b=d.querySelector(".mcopy");if(b)b.textContent="Copy";
})}
// Click away or Escape closes. Clicking another tile's dots is a click away
// from this one, so "only one open at a time" comes free with it.
document.addEventListener("click",shut);
document.addEventListener("keydown",function(e){
  if(e.key!=="Escape")return;
  shut(null);
  // The rename dialog is a :target, so closing it is a navigation. Replace
  // rather than push, or Escape leaves a history entry that Back reopens.
  var d=document.querySelector(".dlg:target");
  if(d)history.replaceState(null,"",location.pathname+location.search);
  if(d)d.id="";
});
// A copy button only where the clipboard exists: one that silently fails is
// worse than none, because the reader believes it. The input underneath is
// the fallback either way, and is what a scriptless reader gets.
document.querySelectorAll(".mlink input").forEach(function(i){
  i.addEventListener("focus",function(){i.select()});
  if(!navigator.clipboard)return;
  var b=document.createElement("button");
  b.type="button";b.className="mcopy";b.textContent="Copy";
  b.addEventListener("click",function(){navigator.clipboard.writeText(i.value).then(
    function(){b.textContent="Copied"},function(){i.focus();i.select()})});
  i.parentNode.appendChild(b);
});
})();
"""


# Menu icons, inline and tiny. Stroke-based so one set works on both themes:
# `currentColor` inherits the row's colour, including the red of a destructive
# row, without a second copy of each path.
_ICON = {
    "rename": '<path d="M2 11.5V14h2.5l7.4-7.4-2.5-2.5L2 11.5Zm11.8-6.9a.7.7 0 0 0 0-1L12.4 2.2a.7.7 0 0 0-1 0l-1.2 1.2 2.5 2.5 1.1-1.3Z"/>',
    "archive": '<path d="M1.5 3.2h13v2.6h-13V3.2Zm1 3.9h11v6.2a.7.7 0 0 1-.7.7H3.2a.7.7 0 0 1-.7-.7V7.1Zm3.2 2v1.3h4.6V9.1H5.7Z"/>',
    "unarchive": '<path d="M8 2.2 3.4 6.8h2.8v4.6h3.6V6.8h2.8L8 2.2ZM2.5 12.8h11v1.3h-11v-1.3Z"/>',
    "open": '<path d="M9.4 1.6v1.3h2.8L6.5 8.6l.9.9 5.7-5.7v2.8h1.3V1.6H9.4ZM2.8 3.3h3.9v1.3H3.3v8.1h8.1V9.3h1.3v4a.7.7 0 0 1-.7.7H2.8a.7.7 0 0 1-.7-.7V4a.7.7 0 0 1 .7-.7Z"/>',
    "all": '<path d="M2 2.5h5v5H2v-5Zm7 0h5v5H9v-5Zm-7 6h5v5H2v-5Zm7 0h5v5H9v-5Z"/>',
    "trash": '<path d="M6.4 1.6h3.2a.8.8 0 0 1 .8.8v.8h2.8a.65.65 0 0 1 0 1.3h-.5l-.7 8.2a1.6 1.6 0 0 1-1.6 1.5H5.6a1.6 1.6 0 0 1-1.6-1.5l-.7-8.2h-.5a.65.65 0 0 1 0-1.3h2.8v-.8a.8.8 0 0 1 .8-.8Zm.5 1.6h2.2v-.3H6.9v.3ZM4.6 4.5l.7 8.1a.3.3 0 0 0 .3.3h4.8a.3.3 0 0 0 .3-.3l.7-8.1H4.6Zm2 1.5h1.2v5.4H6.6V6Zm2.6 0h1.2v5.4H9.2V6Z"/>',
    "restore": '<path d="M8 2.4a5.6 5.6 0 1 0 5.4 7.1.7.7 0 0 0-1.34-.38A4.2 4.2 0 1 1 8 3.8c1.1 0 2.1.42 2.85 1.1l-1.6.02a.65.65 0 0 0 .01 1.3l3.2-.04a.65.65 0 0 0 .64-.66l-.04-3.2a.65.65 0 1 0-1.3.02l.01 1.35A5.58 5.58 0 0 0 8 2.4Z"/>',
    "copy": '<path d="M5.5 1.6h6.3a1.6 1.6 0 0 1 1.6 1.6v6.3h-1.3V3.2a.3.3 0 0 0-.3-.3H5.5V1.6ZM3.2 4.1h6.4a1.6 1.6 0 0 1 1.6 1.6v6.4a1.6 1.6 0 0 1-1.6 1.6H3.2a1.6 1.6 0 0 1-1.6-1.6V5.7a1.6 1.6 0 0 1 1.6-1.6Zm0 1.3a.3.3 0 0 0-.3.3v6.4c0 .17.13.3.3.3h6.4a.3.3 0 0 0 .3-.3V5.7a.3.3 0 0 0-.3-.3H3.2Z"/>',
    "pin": '<path d="M9.6 1.3 8.5 2.4l.4.4-3 2.2-1.6-.3-1.1 1.1 3 3L2 13l3.2-4.2 3 3 1.1-1.1-.3-1.6 2.2-3 .4.4 1.1-1.1-3.1-3.1Z"/>',
    "find": '<path d="M7.1 2.2a4.9 4.9 0 1 0 2.96 8.8l3 3a.7.7 0 0 0 1-1l-3-3A4.9 4.9 0 0 0 7.1 2.2Zm0 1.4a3.5 3.5 0 1 1 0 7 3.5 3.5 0 0 1 0-7Z"/>',
    "list": '<path d="M2 3.2h1.9v1.9H2V3.2Zm3.4 .35h8.6v1.2H5.4V3.55ZM2 7.05h1.9v1.9H2v-1.9Zm3.4 .35h8.6v1.2H5.4V7.4ZM2 10.9h1.9v1.9H2v-1.9Zm3.4 .35h8.6v1.2H5.4v-1.2Z"/>',
    "grid": '<path d="M2.2 2.2h5v5h-5v-5Zm6.6 0h5v5h-5v-5Zm-6.6 6.6h5v5h-5v-5Zm6.6 0h5v5h-5v-5Z"/>',
    "page": '<path d="M4 1.5h5l3 3v10a.5.5 0 0 1-.5.5h-7a.5.5 0 0 1-.5-.5v-13a.5.5 0 0 1 .5-.5Zm4.6 1.4v2.1h2.1L8.6 2.9ZM5.4 7.4h5.2v1H5.4v-1Zm0 2.3h5.2v1H5.4v-1Zm0 2.3h3.4v1H5.4v-1Z"/>',
    "link": '<path d="M6.9 9.1a2.9 2.9 0 0 0 4.3.3l1.7-1.7a2.9 2.9 0 0 0-4.1-4.1l-1 1 .9.9 1-1a1.6 1.6 0 0 1 2.3 2.3L10.3 8.5a1.6 1.6 0 0 1-2.4-.2l-1 .8Zm2.2-2.2a2.9 2.9 0 0 0-4.3-.3L3.1 8.3a2.9 2.9 0 0 0 4.1 4.1l1-1-.9-.9-1 1a1.6 1.6 0 0 1-2.3-2.3l1.7-1.7a1.6 1.6 0 0 1 2.4.2l1-.8Z"/>',
}


def _icon(name: str) -> str:
    return f'<svg class="mi" viewBox="0 0 16 16" aria-hidden="true">{_ICON[name]}</svg>'


def _pin_button(artifact_id: str, pinned: bool) -> str:
    """The pin, on a tile or on a row. The indicator AND the control.

    One function for both, because the two views must not drift into offering
    different actions -- and because this is the control Jeremy noticed was
    missing, twice: first from the card, then from the row.

    A SIBLING of the tile's or row's link, never inside it: a button nested in
    an <a> is invalid and the link swallows the click.
    """
    ident = escape(artifact_id)
    label = "Unpin this page" if pinned else "Pin this page"
    hint = "Pinned — click to unpin" if pinned else "Pin to the front"
    return (
        f'<form class="pinbtn{" on" if pinned else ""}" method="post"'
        f' action="/a/{ident}/pin">'
        f'<input type="hidden" name="pinned" value="{"0" if pinned else "1"}">'
        f'<button type="submit" aria-label="{label}" title="{hint}">'
        f"{_icon('pin')}</button></form>"
    )


def _act(
    action: str,
    icon: str,
    label: str,
    hidden: dict | None = None,
    danger: bool = False,
) -> str:
    """One menu row that POSTs. Uniform with `_go` below -- same height, same
    padding, same icon slot. `danger` is the red one, and there is only ever
    one of those in a menu."""
    fields = "".join(
        f'<input type="hidden" name="{k}" value="{v}">'
        for k, v in (hidden or {}).items()
    )
    cls = "mrow danger" if danger else "mrow"
    return (
        f'<form method="post" action="{action}">{fields}'
        f'<button class="{cls}" type="submit">{_icon(icon)}{label}</button></form>'
    )


def _go(href: str, icon: str, label: str, extra: str = "") -> str:
    """One menu row that navigates."""
    return f'<a class="mrow{extra}" href="{href}">{_icon(icon)}{label}</a>'


def _tile_menu(artifact_id: str, title: str, pinned: bool, origin: str) -> str:
    """The dots on a tile: the three things worth doing without opening a page.

    Deliberately SHORTER than the page's own title menu. A menu on a wall of
    twenty tiles is a thing you glance at, and the reference Jeremy sent has
    this same split -- Pin, Rename, Copy link here, everything else once you
    are on the page.

    The caller must render this as a SIBLING of the tile's <a>, never inside
    it: an interactive control nested in a link is invalid HTML and behaves
    unpredictably -- on some engines the link swallows the click, which on a
    phone means a menu that simply never opens.
    """
    ident = escape(artifact_id)
    # EVERY ROW IS THE SAME SHAPE: icon, label, one line. Rename and Copy link
    # used to be inline mini-forms -- an input plus a button on its own row --
    # which made a four-item menu three different visual weights and twice the
    # height it needed. Jeremy's word for it was janky and he was right.
    #
    # The two that need a text field now open a dialog on the page instead
    # (`#rename`, `#link`), which is the same :target mechanism the reference's
    # own rename modal uses and needs no script. A menu is a list of actions;
    # anything that needs typing is not a menu item.
    rows = [
        _act(
            f"/a/{ident}/pin",
            "pin",
            "Unpin" if pinned else "Pin to the front",
            {"pinned": "0" if pinned else "1"},
        ),
        _go(f"/a/{ident}#rename", "rename", "Rename"),
        _act(f"/a/{ident}/duplicate", "copy", "Duplicate"),
        _go(f"/a/{ident}#link", "link", "Copy link"),
        (
            '<div class="mdanger">'
            + _act(f"/a/{ident}/delete", "trash", "Delete", danger=True)
            + "</div>"
        ),
    ]
    return (
        '<details class="menu tmenu"><summary aria-label="Actions for '
        f'{escape(title)}"><span class="dots" aria-hidden="true"></span></summary>'
        f'<div class="mpanel right">{"".join(rows)}</div></details>'
    )


def _link_dialog(artifact_id: str) -> str:
    """The page's address, selectable, behind the menu's "Copy link".

    A dialog rather than an input wedged into the menu: there is no clipboard
    without script, so the honest scriptless answer is to show the text -- but
    showing it INSIDE the menu made a four-item list two different shapes. Here
    it has room to be read in full, which a 15rem menu panel never gave it.

    gallery.js adds a real Copy button when navigator.clipboard exists. The
    field underneath is what a scriptless reader gets, and is what the button
    falls back to when the clipboard refuses.
    """
    ident = escape(artifact_id)
    return (
        '<div class="dlg" id="link">'
        f'<a class="dlg-back" href="/a/{ident}" aria-label="Close"></a>'
        '<div class="dlg-card"><h2>Link to this page</h2>'
        '<div class="mlink">'
        f'<input readonly value="{escape(gallery_origin())}/a/{ident}"'
        ' aria-label="Link to this page">'
        "</div>"
        f'<p class="dlg-actions"><a class="btn" href="/a/{ident}">Close</a></p>'
        "</div></div>"
    )


def _rename_dialog(artifact_id: str, title: str) -> str:
    """The rename dialog the reference has: dimmed background, the current name
    ready to edit, Cancel and Save.

    :target, not a <dialog>. A real dialog needs `showModal()` to open, and this
    gallery works with scripts off -- a rename that only exists when JavaScript
    ran is a rename that is missing on the day something else on the page threw.
    The fragment does the whole job: the panel appears when the URL matches,
    the backdrop is a link back to the page so clicking away closes it, and
    Cancel is the same link. It costs nothing in gallery.js, which has a size
    budget and better things to spend it on.

    What :target does not give is Escape-to-close, so gallery.js adds that one
    line as an upgrade rather than a requirement.

    `autofocus` puts the cursor in the field when it opens. Not `select()` --
    that needs script; the field is small enough that a reader can select it,
    and the common edit is appending or fixing a word rather than replacing the
    whole name.
    """
    ident = escape(artifact_id)
    return (
        f'<div class="dlg" id="rename">'
        f'<a class="dlg-back" href="/a/{ident}" aria-label="Cancel"></a>'
        f'<form class="dlg-card" method="post" action="/a/{ident}/rename">'
        "<h2>Rename page</h2>"
        f'<input name="title" value="{escape(title)}" aria-label="Page name"'
        ' autofocus autocomplete="off">'
        '<p class="dlg-actions">'
        f'<a class="btn" href="/a/{ident}">Cancel</a>'
        '<button class="btn primary" type="submit">Save</button>'
        "</p></form></div>"
    )


def _page_menu(
    artifact_id: str, title: str, agent: str, archived: bool, updated: str
) -> str:
    """The title's dropdown: what this page is, and what can be done to it.

    A <details>, not a script. Everything else in this gallery works with
    scripts off and this is no exception -- a menu that needs JavaScript to open
    is a menu that is missing on the one day something else on the page has
    thrown.

    Rename lives INSIDE the menu as a real input rather than opening a dialog,
    because a dialog would need script and a second round trip to do what one
    form already does.
    """
    ident = escape(artifact_id)
    rows = [
        (
            '<div class="mhead">'
            f"<b>Page by {escape(agent)}</b>"
            f"<span>Updated {escape(updated)}</span>"
            "</div>"
        ),
        (
            f'<a class="mrow" href="#rename">{_icon("rename")}Rename</a>'
        ),
    ]
    if archived:
        rows.append(
            f'<form method="post" action="/a/{ident}/archive">'
            '<input type="hidden" name="archived" value="0">'
            f'<button class="mrow" type="submit">{_icon("unarchive")}'
            "Put back on the wall</button></form>"
        )
    else:
        rows.append(
            f'<form method="post" action="/a/{ident}/archive">'
            '<input type="hidden" name="archived" value="1">'
            f'<button class="mrow" type="submit">{_icon("archive")}'
            "Archive</button></form>"
        )
    rows.append(
        f'<form method="post" action="/a/{ident}/duplicate">'
        f'<button class="mrow" type="submit">{_icon("copy")}'
        "Duplicate</button></form>"
    )
    rows.append(
        f'<a class="mrow" href="/a/{ident}" target="_blank" rel="noopener">'
        f"{_icon('open')}Open in a new tab</a>"
    )
    rows.append(f'<a class="mrow" href="/">{_icon("all")}All pages</a>')
    # Last, under a divider, and the only red thing in the menu -- as in the
    # reference, and for the reason the reference has it there: it is the one
    # entry whose misfire costs something.
    rows.append(
        f'<form class="mdanger" method="post" action="/a/{ident}/delete">'
        f'<button class="mrow danger" type="submit">{_icon("trash")}'
        "Delete</button></form>"
    )
    return (
        '<details class="menu"><summary aria-label="Page actions">'
        f'{escape(title)}<span class="chev" aria-hidden="true"></span></summary>'
        f'<div class="mpanel">{"".join(rows)}</div></details>'
    )


def _matches(row, needle: str) -> bool:
    """Does this page match? Title, owning agent, and the page's own words.

    The agent is in there because on a wall with a dozen seats "what has
    warpgate-arc published" is a question worth being able to ask, and it is
    the same keystrokes as searching for a title.

    Case-insensitive substring, deliberately: this is a wall of tens of pages,
    not a corpus. Anything cleverer -- stemming, ranking, fuzzy matching --
    would mean a page failing to appear for a reason he cannot see, and the
    honest answer at this size is that everything either contains the string or
    does not.
    """
    artifact_id, title, _updated, agent, _v, _w, body, *_rest = row
    haystacks = (title or "", agent or "", artifact_id or "")
    if any(needle in h.lower() for h in haystacks):
        return True
    # The page's readable words. NOT `describe`, which answers "what is this
    # page about?" and returns "" when the page does not say -- which is most
    # pages, so searching through it silently missed them. `searchable` answers
    # "what words are on it?", which is what a search is actually asking, and
    # it drops script, style and inline SVG first so a query can never match a
    # class name or a path.
    return needle in text_module.searchable(body or "")


def _search_box(query: str, view: str) -> str:
    """A GET form whose result is the same gallery, filtered -- so a search is a
    URL he can keep, and it works with scripts off.

    The view travels with it in a hidden field. Without that, searching would
    silently throw him back to the grid, which reads as the search having done
    something odd rather than as a control losing state."""
    value = escape(query)
    clear = (
        f'<a class="clearq" href="/?view={view}" aria-label="Clear the search">'
        "&times;</a>"
        if query
        else ""
    )
    return (
        '<form class="find" method="get" action="/" role="search">'
        f'<input type="hidden" name="view" value="{escape(view)}">'
        f'{_icon("find")}'
        f'<input type="search" name="q" value="{value}" placeholder="Search"'
        ' aria-label="Search pages">'
        f"{clear}</form>"
    )


def _view_toggle(view: str) -> str:
    """The grid/list switch. The icon shows the view you would SWITCH TO, which
    is what the reference does and what every such control does -- an icon
    showing the state you are already in reads as a label and gets ignored.

    A link, not a button: it changes what you are looking at, not the world, so
    it should be shareable, bookmarkable and reachable with scripts off."""
    to_list = view != "list"
    target = "list" if to_list else "grid"
    label = "Show as a list" if to_list else "Show as a grid"
    return (
        f'<a class="vtoggle" href="/?view={target}" title="{label}"'
        f' aria-label="{label}">{_icon("list" if to_list else "grid")}</a>'
    )


def _left(deleted_at: datetime | None) -> str:
    """How long is left on a deleted page, in his words.

    Counted down rather than up: "deleted 4 days ago" is a fact about the past
    and this list is read for a decision about the future. The one that matters
    is the one about to go, and it should say so in the words that make that
    obvious.
    """
    if deleted_at is None:
        return ""
    if deleted_at.tzinfo is None:
        deleted_at = deleted_at.replace(tzinfo=UTC)
    left = (deleted_at + store.DELETE_WINDOW) - datetime.now(UTC)
    days = left.days
    if left.total_seconds() <= 0:
        # Past the window and not yet swept. Honest about which of the two it
        # is: the row is still recoverable until something actually removes it,
        # and saying "gone" about a page that is still here would be wrong.
        return "due to be destroyed"
    if days == 0:
        return "less than a day left"
    if days == 1:
        return "1 day left"
    return f"{days} days left"


def _deleted_view(rows) -> str:
    """The recycle bin: what is deleted, how long it has, and the way back.

    Its own view rather than a fold at the bottom of the wall, because it is
    read for a different reason -- the wall answers "what is there", this
    answers "what am I about to lose" -- and because a deleted page appearing
    anywhere near the live ones is how "deleted" stops meaning anything.
    """
    if not rows:
        return (
            '<p class="empty">Nothing is deleted. Anything you delete waits '
            "here for thirty days before it goes for good.</p>"
        )
    out = [
        (
            '<p class="binnote">Deleted pages keep their address, their history '
            "and every answer on them. They are destroyed thirty days after "
            "deletion, soonest first.</p>"
        ),
        '<ul class="rows bin">',
    ]
    for row in rows:
        ident = escape(row.id)
        remaining = _left(row.deleted_at)
        urgent = " soon" if remaining in ("less than a day left", "1 day left") else ""
        out.append(
            f'<li class="row-wrap"><span class="row">'
            f'<span class="rshot none">{_icon("trash")}</span>'
            f'<span class="rt">{escape(row.title)}</span>'
            f'<span class="rmeta"><span class="who">{escape(row.agent)}</span>'
            f'<span class="left{urgent}">{escape(remaining)}</span>'
            f'<form method="post" action="/a/{ident}/restore">'
            f'<button class="restore" type="submit">{_icon("restore")}'
            "Put back</button></form>"
            '<details class="menu tmenu"><summary aria-label="Destroy this page">'
            '<span class="dots" aria-hidden="true"></span></summary>'
            '<div class="mpanel right">'
            f'<form class="mdanger destroyform" method="post" '
            f'action="/a/{ident}/destroy">'
            '<p>Destroy for good? Its versions and every answer on it go too. '
            "This cannot be undone.</p>"
            '<input name="confirm" placeholder="type destroy" '
            'aria-label="Type destroy to confirm" autocomplete="off">'
            '<button class="danger" type="submit">Destroy</button>'
            "</form></div></details>"
            "</span></span></li>"
        )
    out.append("</ul>")
    return "".join(out)


def _tabs(active: str, waiting: int, pinned: int, deleted: int = 0) -> str:
    """All / Waiting on you / Pinned.

    The reference's tabs are All / Yours / Shared with you, which do not map:
    every page here is his. These are the questions actually worth asking of
    this wall, and the middle one is the reason the service exists.

    A tab appears only once it has something in it. A "Pinned" tab that is
    always empty teaches him to ignore the row it sits in, and that row is also
    where search lives."""
    out = []
    for key, label, count in (
        ("all", "All", 0),
        ("waiting", "Waiting on you", waiting),
        ("pinned", "Pinned", pinned),
        ("deleted", "Deleted", deleted),
    ):
        if key != "all" and not count:
            continue
        cls = "tab on" if key == active else "tab"
        suffix = f'<span class="tn">{count}</span>' if count else ""
        href = "/" if key == "all" else f"/?filter={key}"
        out.append(f'<a class="{cls}" href="{href}">{label}{suffix}</a>')
    return f'<nav class="tabs">{"".join(out)}</nav>'


def _controls_row(
    view: str,
    query: str,
    active: str,
    waiting: int,
    pinned: int,
    pages: int,
    deleted: int = 0,
) -> str:
    """The row under the title: WHAT to show on the left, how to find it and how
    to see it on the right."""
    pages_text = f"{pages} page" if pages == 1 else f"{pages} pages"
    return (
        '<div class="controls">'
        f"{_tabs(active, waiting, pinned, deleted)}"
        '<div class="cright">'
        f"{_search_box(query, view)}"
        f'<span class="count">{pages_text}</span>'
        f"{_view_toggle(view)}"
        "</div></div>"
    )


def _wall_meta(waiting: int) -> str:
    """What sits beside the title, and it is only ever one thing.

    "5 pages" is a fact about the service and belongs in the controls row below.
    "3 questions waiting on you" is a fact about his afternoon, and it is the
    whole reason this service exists -- his brief was to be able to "interact on
    decisions and not just visualise progress". So the title row carries that
    and nothing else, and carries nothing at all when nothing is waiting.

    It links to the FILTER rather than to an anchor. With three pages asking,
    jumping to the first answers "where is it"; the useful question is "show me
    them", and the filtered wall answers both.
    """
    if not waiting:
        return ""
    noun = "question" if waiting == 1 else "questions"
    return (
        f'<a class="waiting" href="/?filter=waiting">'
        f"{waiting} {noun} waiting on you</a>"
    )


def _shell(
    title: str,
    body: str,
    meta: str = "",
    home: bool = True,
    scripts: str = "",
    title_html: str = "",
    meta_html: str = "",
    controls: str = "",
    bleed: bool = False,
) -> str:
    """Every gallery page, wrapped the same way: one narrow bar carrying the way
    back, the title and a timestamp, then the body. Deliberately quiet -- the
    agent's page is the thing being read, and chrome that shouts competes with
    it. `scripts` (default empty) is raw `<script src=...>` tag markup, never inline
    -- only `detail()` passes one, for the one page that has an iframe to size and
    decisions to place; it lands right before `</body>`, after everything it acts on
    already exists in the DOM."""
    # The way back is an ICON on a page, a labelled pill on the index. On a
    # page the title carries the actions and the bar has to stay narrow.
    back = (
        f'<a class="home" href="/" aria-label="All pages">{_HOME_SVG}</a>'
        if home and title_html
        else f'<a class="home" href="/">{_HOME_SVG} Gallery</a>'
        if home
        else ""
    )
    # `meta` is TEXT and is escaped; `meta_html` is markup the caller built
    # itself and is not. Two parameters rather than one that sometimes escapes:
    # a single field whose safety depends on what a caller happens to pass is
    # how escaping gets dropped by accident, and this one carries a page title
    # via the wall's counts. Same split as `title`/`title_html` above.
    meta_block = meta_html or (f"{escape(meta)}" if meta else "")
    meta_block = f'<span class="meta">{meta_block}</span>' if meta_block else ""
    heading = title_html or f"<h1>{escape(title)}</h1>"
    # `bleed` puts the bar across the whole window with the body in a column
    # beneath it, which is how the reference does an ARTIFACT page: the bar is
    # chrome and should read as chrome. The gallery index keeps its header
    # inside the column, which is also what the reference does there -- the two
    # are different on purpose, and both were measured off the saved pages.
    if bleed:
        top = (
            f'<header class="bar bleed">{back}{heading}{meta_block}</header>'
            '<div class="wrap">'
        )
    else:
        top = (
            f'<div class="wrap"><header class="bar">{back}{heading}'
            f"{meta_block}</header>"
        )
    return (
        '<!doctype html><html lang="en-GB"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body>"
        f"{top}"
        # The controls are their OWN row beneath the title, as the reference
        # has it: a 48px title row, 16px, then a 32px row of controls. They
        # used to share one line with the title, which is what made the header
        # feel like a toolbar someone had run out of room on.
        f"{controls}"
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


# The middot between meta items, as its own element so it can be dimmed.
_SEP = '<span class="dot">&middot;</span>'

# How long the chosen view is remembered. A display preference, so a long life
# and nothing to clean up; it carries no identity and no state beyond "list".
_VIEW_COOKIE = "artifacts_view"
_VIEW_MAX_AGE = 60 * 60 * 24 * 365


def _date_group(moment: datetime | None) -> str:
    """The heading a page sits under in the list: Today, Yesterday, a day, or a
    month.

    Grouped in HIS day, not UTC. A page published at 00:40 CEST is 22:40 the
    previous day in UTC, so grouping on the raw timestamp would file this
    morning's work under yesterday -- the same class of error as the 08:53 UTC
    stamp he caught on a board, and harder to spot because a heading looks
    authoritative.
    """
    if moment is None:
        return "Undated"
    zone = stamp.display_timezone()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    local = moment.astimezone(zone) if zone else moment
    today = datetime.now(zone or UTC).date()
    day = local.date()
    if day == today:
        return "Today"
    if (today - day).days == 1:
        return "Yesterday"
    if day.year == today.year:
        return local.strftime("%-d %b")
    return local.strftime("%B %Y")


def _ago(moment: datetime | None) -> str:
    """"Edited 5h ago", the way the reference says it -- and the way a person
    reads a wall.

    A tile is glanced at, and "20 September 2026, 17:48 CEST" answers a question
    nobody asked of a thumbnail: the useful fact is whether this moved today or
    last week. The exact time is still one click away on the page itself, which
    is where somebody who wants it is going anyway.

    Hours up to a day, then days, then the date. Never "0h ago": under an hour
    says "just now", because a wall that claims a page changed zero hours ago
    reads as broken rather than as recent.
    """
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = (datetime.now(UTC) - moment).total_seconds()
    if seconds < 0:
        # A clock disagreement, not the future. Say nothing rather than
        # "in 3 hours", which would look like a bug in the service.
        return "just now"
    if seconds < 3600:
        return "just now"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    days = int(seconds // 86400)
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    return moment.strftime("%-d %b")


def _provenance(row) -> str:
    """Who wrote a relayed answer down, from where, and when he said it.

    Empty for a tap: the ordinary case needs no explanation, and a badge on
    every entry would make the exceptional one invisible, which is the whole
    point of marking it.
    """
    if not row.answer_source or row.answer_source == "page":
        return ""
    who = escape(row.recorded_by or "an agent")
    where = escape(row.answer_source)
    said = f" You said it {escape(_when(row.said_at))}." if row.said_at else ""
    return (
        f'<p class="relayed-note">You answered this on {where}, not on the page.'
        f"{said} {who} wrote it down. If that is not what you meant, say so and "
        "it will be corrected.</p>"
    )


def _has_looked_since(listening_since: str | None, answered_at: str | None) -> bool:
    """Has this page's agent asked for its events since Jeremy answered?

    Compared as moments, never as strings: both come from the same database and
    would usually compare correctly as text, but "usually" is not a property to
    build a message to Jeremy on. An unparseable stamp reads as "has not looked",
    which is the safe way round -- it points at an agent that may be fine, rather
    than staying silent about one that is gone.
    """
    if not listening_since or not answered_at:
        return False
    try:
        return datetime.fromisoformat(listening_since) > datetime.fromisoformat(
            answered_at
        )
    except ValueError:
        return False


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


def _render_decisions(
    rows: list[decisions.DecisionRow], listening_since: str | None = None
) -> tuple[str, str]:
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
    slot for is simply never moved, and stays in the block above the page -- the
    default, not a failure. Only OPEN decisions render there; settled ones are
    returned as a second, collapsed block the caller puts AFTER the page.

    An answered decision still renders every button: re-answering is allowed (see
    `decisions.answer`'s docstring) precisely so Jeremy can change his mind here, on
    the page, rather than needing a terminal.
    """
    if not rows:
        return "", ""

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

    # TWO blocks, not one, and the caller puts them either side of the page.
    # Jeremy, 20 Sep 2026: "for sure it feels odd to have the questions at the
    # top of the page", looking at his own status board whose first screen was
    # four questions he had ANSWERED the day before -- the page itself began
    # below them. What must be acted on belongs in front of the page; what is
    # already settled is history and belongs after it.
    above: list[str] = []
    below: list[str] = []

    if open_rows:
        hint = (
            '<p class="hint">Pick a letter and it is saved straight away; add a note '
            "if the options do not fit. Your answer is the filled button &mdash; pick "
            "a different one to change it, as often as you like.</p>"
        )
        above.append(
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
            # Jeremy answering and the agent COLLECTING the answer are two different
            # events, and until tonight the page only ever showed the first. An answer
            # that nobody picked up looks exactly like one that was acted on an hour
            # ago, which is the failure this whole listener arc exists to end. So the
            # summary line carries a mark only in the bad case -- an answer sitting
            # uncollected -- and the opened card says plainly who took it and when.
            if row.acknowledged_at:
                unread = ""
                pickup = (
                    f'<p class="pickup">{escape(row.acknowledged_by or "the agent")} '
                    f"picked this up {escape(_when(row.acknowledged_at))}</p>"
                )
            else:
                unread = '<span class="unread">not picked up yet</span>'
                # Two very different situations, and saying which one costs one
                # comparison. An agent that has not looked since he answered is
                # not running; one that has looked and still not taken the answer
                # is running and stuck. Telling him "uncollected" and leaving him
                # to guess which is how an alert earns being ignored.
                pickup = (
                    '<p class="pickup">Nothing has looked for this answer since you '
                    "gave it, so the agent is probably not running. It is waiting "
                    "for them, not for you.</p>"
                    if not _has_looked_since(listening_since, row.answered_at)
                    else '<p class="pickup">The agent has looked since you answered '
                    "and still has not taken it. It is waiting for them, not for "
                    "you.</p>"
                )
            # <details>, not a script: it collapses and expands with scripts off,
            # costs nothing, and keeps the page as fast as Jeremy said he liked.
            # A RELAYED ANSWER IS MARKED, ALWAYS AND VISIBLY (homelab-ij8.78).
            # A seat recorded words Jeremy gave in a terminal or on Telegram; he
            # did not tap this. Saying so is not a detail — it is what lets him
            # notice a relay he disagrees with and contradict it, and what stops
            # a later reader treating someone's transcription as his own click.
            # `answer_source` is never null on an answered row, so an unmarked
            # entry here genuinely means he tapped it.
            relayed = ""
            if row.answer_source and row.answer_source != "page":
                where = escape(row.answer_source)
                who = escape(row.recorded_by or "an agent")
                relayed = (
                    f'<span class="relayed" title="Recorded by {who} from '
                    f'{where}; not tapped on this page">via {where}</span>'
                )
            entries.append(
                '<details class="settled">'
                f'<summary><span class="when">{escape(_when(row.answered_at))}</span>'
                f'<span class="sq">{escape(row.question)}</span>'
                f'<span class="sa">({escape(row.answer)}) {escape(chosen)}</span>'
                f"{relayed}{unread}</summary>"
                f'<div class="settled-body">{_provenance(row)}{pickup}{note}'
                f"{_card(row, anchorable=False)}</div></details>"
            )
        # Collapsed, and counted in its own summary. Four settled questions took
        # the whole first screen of a page whose point was the one still open;
        # closed, the same four are a single line he can open when he wants them.
        n = len(entries)
        noun = "question" if n == 1 else "questions"
        below.append(
            '<details class="asks settled-list"><summary><h2>'
            f"{n} {noun} you have already settled</h2></summary>"
            + "".join(entries)
            + '<p class="hint">Open one to see the options again, or to change your '
            "mind &mdash; nothing here is locked.</p></details>"
        )

    return "".join(above), "".join(below)


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

    def _wall(conn, archived: bool):
        return conn.execute(
            # Bounded: an unpaginated list is fine at ten artifacts and a
            # page nobody can load at ten thousand. `, id desc` breaks a tie
            # on `updated_at` (two rows can share one, e.g. re-touched by
            # the same migration) so the order is fixed, not whatever a
            # plan with no full ORDER BY happens to return today.
            #
            # The live body comes back so the tile can say what the page is
            # ABOUT, taken from the page itself. A separate agent-supplied
            # summary would drift from the page it describes, which is the
            # one failure this service exists to prevent.
            #
            # The counts are subqueries rather than joins on purpose: a join
            # to `version` multiplies the artifact row and a second join to
            # `decision` multiplies it again, so a page with 12 versions and
            # 2 open questions would report 24 of each. That is the classic
            # shape and it is silent -- the numbers merely look large.
            "select a.id, a.title, a.updated_at, a.agent,"
            "       (select count(*) from version v"
            "          where v.artifact_id = a.id) as versions,"
            "       (select count(*) from decision d"
            "          where d.artifact_id = a.id"
            "            and d.answer is null"
            "            and d.closed_at is null) as waiting,"
            "       (select v.body from version v"
            "          where v.artifact_id = a.id and v.status = 'live'"
            "          order by v.n desc limit 1) as body,"
            # The boolean only. Selecting the bytes here would pull every
            # thumbnail on the wall into memory to render a list of links.
            "       exists (select 1 from version v"
            "          where v.artifact_id = a.id and v.n = a.current_version"
            "            and v.thumbnail is not null) as has_thumb,"
            "       a.pinned_at"
            "  from artifact a"
            " where (a.archived_at is null) = %s and a.deleted_at is null"
            # THE WALL SORTS ITSELF BY WHAT NEEDS HIM. Pinned first, because
            # that is him saying so explicitly. Then anything with an
            # unanswered question, because this service exists so he can
            # "interact on decisions and not just visualise progress" -- a page
            # holding a question he has not seen outranks a page that merely
            # changed recently. Then the order the wall has always had.
            #
            # The reference this gallery is modelled on has no equivalent and
            # no reason for one: nobody is waiting on anybody there.
            #
            # NULLS LAST is spelled out rather than left to the default:
            # Postgres sorts NULLs first on DESC, which would put every
            # UNpinned page above every pinned one -- precisely backwards, and
            # it would look like pinning had done nothing rather than like a
            # broken sort.
            " order by a.pinned_at desc nulls last,"
            "          (select count(*) from decision d"
            "             where d.artifact_id = a.id"
            "               and d.answer is null"
            "               and d.closed_at is null) > 0 desc,"
            "          a.updated_at desc, a.id desc limit 200",
            (not archived,),
        ).fetchall()

    def _row_view(row) -> str:
        """One page as a line, for the list view.

        The same facts as a tile in a quarter of the height: the point of the
        list is that twenty pages fit on a screen, so anything that does not
        earn its width is left to the tile.
        """
        (
            artifact_id,
            title,
            updated,
            agent,
            _versions,
            waiting,
            _body,
            has_thumb,
            pinned_at,
        ) = row
        ident = escape(artifact_id)
        # The reference puts a coloured per-type icon here. Types do not exist
        # yet (homelab-ij8.83), so the slot carries the page's own thumbnail
        # instead -- which is more use than a generic glyph would be, and is
        # already rendered. A page without one falls back to the glyph.
        if has_thumb:
            badge = (
                f'<img class="rshot" src="/thumb/{ident}.png" alt=""'
                f' loading="lazy" width="{thumbs.WIDTH}" height="{thumbs.HEIGHT}">'
            )
        else:
            badge = f'<span class="rshot none">{_icon("page")}</span>'

        right = []
        if waiting:
            word = "question" if waiting == 1 else "questions"
            right.append(f'<span class="wait">{waiting} {word} for you</span>')
        right.append(f'<span class="who">{escape(agent)}</span>')
        when = _ago(updated)
        if when:
            exact = stamp.format_for_display(updated) if updated else ""
            right.append(f'<time title="{escape(exact)}">Edited {escape(when)}</time>')

        # The same pin button the tile has -- the reference's row carries one
        # too (24x24, and always visible where the CARD's fades in on hover).
        # This row had only a "Pinned" label, so the list was the one view
        # where pinning meant opening a menu.
        return (
            f'<li class="row-wrap{" asking" if waiting else ""}">'
            f'<a class="row" href="/a/{ident}">'
            f"{badge}<span class=\"rt\">{escape(title)}</span>"
            f'<span class="rmeta">{"".join(right)}</span></a>'
            f"{_pin_button(artifact_id, bool(pinned_at))}"
            f"{_tile_menu(artifact_id, title, bool(pinned_at), gallery_origin())}"
            "</li>"
        )

    @router.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        with db.connect() as conn:
            rows = _wall(conn, archived=False)
            archived_rows = _wall(conn, archived=True)

        # A tile per page rather than a line per page: a wall Jeremy recognises
        # the board he wants in. Jeremy chose a real rendered thumbnail (19 Sep)
        # and qualified it -- "start with (b) but then (a) if we can" -- so this
        # is (b): everything a tile can say without a browser. The thumbnail is
        # tracked separately and needs a decision about where the renderer runs,
        # because the gallery must never load an agent's HTML into this origin.
        def tile(row) -> str:
            (
                artifact_id,
                title,
                updated,
                agent,
                versions,
                waiting,
                body,
                has_thumb,
                pinned_at,
            ) = row
            # Every one of these is agent-controlled text reaching the gallery
            # origin, so every one is escaped here. `describe` strips tags but
            # makes no claim to produce safe markup -- it returns plain text and
            # escaping is this caller's job, as it is for the title.
            about = text_module.describe(body or "")
            # The picture leads when there is one, because recognising a board
            # by its shape is the whole reason he asked for a wall. Everything
            # below it stays: a thumbnail at tile size is recognisable, not
            # readable, and the words are what make it identifiable.
            #
            # `loading="lazy"` so a wall of twenty does not fetch twenty images
            # before the first tile paints, and fixed dimensions so the grid does
            # not reflow as they arrive.
            if has_thumb:
                shot = (
                    f'<img class="shot" src="/thumb/{escape(artifact_id)}.png" '
                    f'alt="" loading="lazy" width="{thumbs.WIDTH}" '
                    f'height="{thumbs.HEIGHT}">'
                )
            else:
                # Every card has a preview area, even when there is nothing to
                # put in it. Without this a row mixing pages with and without a
                # picture came out visibly ragged -- the thumbnail-less tiles
                # stretch to the row's height with their text stranded at the
                # top and a hand's width of nothing under it. Seen only by
                # rendering the LIVE wall: pages published before thumbnails
                # existed have none, and no test fixture had that mixture.
                #
                # A placeholder, not a blank: it says "no picture of this one
                # yet", which is true and temporary -- a page gets one on its
                # next publish.
                # Not an empty box: it carries what the page says it is
                # about. The reference never needs this because its preview
                # always renders; here a page published before thumbnails
                # existed would otherwise be a blank rectangle with a name.
                inner = f'<span class="d">{escape(about)}</span>' if about else ""
                shot = f'<span class="shot none">{_icon("page")}{inner}</span>' 
            # The <li> wraps BOTH the link and the menu; the menu is the link's
            # sibling, never its child. See _tile_menu.
            # Anything waiting on Jeremy rides ON the picture, not in the
            # body below it. Two reasons, and the second is the real one:
            # a badge in the body made that tile taller than its neighbours and
            # the grid equalised the whole row to match, which is what left
            # every other card with a hand's width of nothing in it. And a
            # question for him is the one thing on this wall that should be
            # legible without reading -- the reference has no equivalent,
            # because nobody is waiting on anybody there.
            flag = ""
            if waiting:
                word = "question" if waiting == 1 else "questions"
                flag = f'<span class="wait">{waiting} {word} for you</span>'

            bits = [
                (
                    f'<li class="tile-wrap{" pinned" if pinned_at else ""}'
                    f'{" asking" if waiting else ""}">'
                ),
                f'<a href="/a/{escape(artifact_id)}">',
                f'<span class="frame">{shot}{flag}</span>',
                '<span class="body">',
                f'<span class="t">{escape(title)}</span>',
            ]
            # No description under the title of a tile that HAS a picture: the
            # picture is the description, which is why the reference carries no
            # tile description at all. Dropping it is what takes a card from
            # ~350px to ~234px, so nine fit where three did.
            # (the waiting badge is rendered onto the preview, above)
            # The time a reader can act on, in the reader's zone -- the same rule
            # as the page stamp (homelab-ij8.70). This used to print the raw
            # database value sliced to 16 characters, which is UTC and looked
            # like a local time: exactly the failure Jeremy caught on a page
            # ("your status time is back to UTC"), still live on the index.
            # Relative on the face of it, exact in the attribute. "5h ago"
            # is what a wall is read for, and it is also zone-free, which is
            # the failure `stamp` exists to prevent (Jeremy read 08:53 UTC at
            # 10:53 on his own clock). The precise, zone-correct moment stays
            # one hover or long-press away rather than being thrown out.
            when = _ago(updated)
            exact = stamp.format_for_display(updated) if updated else ""
            # One meta row, as the reference has, but carrying what differs
            # BETWEEN pages here. The reference shows a padlock on every card
            # because some of its artifacts are shared; every page on this
            # service is private, so a padlock on all twenty says nothing at
            # all. Who published it does differ -- there are a dozen seats on
            # this machine -- so the agent keeps the slot the padlock has
            # there, and the pin glyph ends the row exactly as it does in the
            # reference.
            meta_bits = [escape(agent)]
            if versions and versions > 1:
                # "v1" on nearly every tile is a column of noise; a version
                # COUNT only says something once there is more than one.
                meta_bits.append(f"v{versions}")
            # A PIN BUTTON, not a label. The reference has one on every card
            # (24x24, in the meta row) and this had only the menu entry, so
            # pinning took two clicks and a read. It is the indicator AND the
            # control: filled and accented when pinned, quiet when not.
            #
            # Always visible, where the reference fades it in on hover. Same
            # argument as the dots: he reads this wall on a phone, where there
            # is no hover and an invisible control is an absent one.
            #
            # A SIBLING of the tile's <a>, like the dots -- a button inside a
            # link is invalid and the link swallows the click.
            pin_button = _pin_button(artifact_id, bool(pinned_at))
            bits.append(
                # The marker rides the AGENT line, not the date line. On the
                # date line it stole enough width to wrap "CEST" onto a second
                # row, making every pinned tile taller than its neighbours --
                # visible the moment the wall was rendered, invisible in the
                # markup. The agent line is short and has the room.
                f'<span class="m">{_SEP.join(meta_bits)}'
                + (
                    f'{_SEP}<time title="{escape(exact)}">Edited {escape(when)}'
                    "</time>"
                    if when
                    else ""
                )
                + "</span>"
                "</span></a>"
                f"{pin_button}"
                f"{_tile_menu(artifact_id, title, bool(pinned_at), gallery_origin())}"
                "</li>"
            )
            return "".join(bits)

        # Which view. The query parameter wins so a link is shareable and
        # says what it shows; the cookie only supplies the default, so the view
        # he last chose is the one he gets next time without it having to be in
        # every URL.
        # Counted BEFORE filtering: the tabs have to say how many there ARE,
        # not how many survived the tab you are already on -- otherwise the
        # number on a tab changes when you press it, and stops meaning anything.
        waiting_total = sum(r[5] for r in rows)
        pinned_total = sum(1 for r in rows if r[8])

        chosen = request.query_params.get("filter")
        active = chosen if chosen in ("waiting", "pinned", "deleted") else "all"
        deleted_rows = store.list_deleted(db)
        if active == "waiting":
            rows = [r for r in rows if r[5]]
        elif active == "pinned":
            rows = [r for r in rows if r[8]]

        query = (request.query_params.get("q") or "").strip()
        needle = query.lower()
        if needle:
            # Both walls are filtered. Searching only the live one would hide
            # the archived hit AND still count it in the fold's total, which
            # reads as the search having missed something.
            rows = [r for r in rows if _matches(r, needle)]
            archived_rows = [r for r in archived_rows if _matches(r, needle)]

        asked = request.query_params.get("view")
        view = asked if asked in ("grid", "list") else (
            request.cookies.get(_VIEW_COOKIE)
            if request.cookies.get(_VIEW_COOKIE) in ("grid", "list")
            else "grid"
        )

        def listing(these) -> str:
            """The rows, under a heading per day. The headings come from the
            data rather than from a fixed set of buckets, so a wall with a
            three-month gap in it has no empty months in the middle."""
            out, current = [], None
            for row in these:
                group = _date_group(row[2])
                if group != current:
                    if current is not None:
                        out.append("</ul>")
                    out.append(f'<h2 class="daygroup">{escape(group)}</h2><ul class="rows">')
                    current = group
                out.append(_row_view(row))
            if current is not None:
                out.append("</ul>")
            return "".join(out)

        def render(these) -> str:
            if view == "list":
                return listing(these)
            return f'<ul class="tiles">{"".join(tile(r) for r in these)}</ul>'

        # Archived pages, folded and counted. Jeremy asked for archiving so a
        # page stops competing for attention, not so it becomes unreachable —
        # the count in the summary is what keeps "out of the way" from becoming
        # "lost", and the whole thing is one line until he opens it.
        archived_block = ""
        if archived_rows and active != "deleted":
            n = len(archived_rows)
            noun = "page" if n == 1 else "pages"
            archived_block = (
                '<details class="archived-list"><summary>'
                '<span class="chev" aria-hidden="true"></span>'
                f"<span>Archived</span>"
                f'<span class="n">{n} {noun}</span>'
                "</summary>"
                f'<div class="archived-body">{render(archived_rows)}</div>'
                "</details>"
            )
        if active == "deleted":
            main = _deleted_view(deleted_rows)
        elif rows:
            main = render(rows)
        elif query:
            # Never a bare empty wall: with a query in the box that looks
            # exactly like everything having been deleted. Say what was
            # searched for, and give him the way back in the same breath.
            found_archived = (
                " It is not in the archived pages either."
                if not archived_rows
                else " There is a match below, in the archived pages."
            )
            main = (
                f'<p class="empty">Nothing here matches '
                f"<b>{escape(query)}</b>.{found_archived} "
                f'<a href="/?view={view}">Show everything</a>.</p>'
            )
        elif archived_rows:
            main = '<p class="empty">Nothing on the wall. Anything archived is below.</p>'
        else:
            main = '<p class="empty">No pages published yet.</p>'
        body = main + archived_block
        response = HTMLResponse(
            _shell(
                "Artifacts",
                body,
                meta_html=_wall_meta(waiting_total),
                controls=_controls_row(
                    view,
                    query,
                    active,
                    waiting_total,
                    pinned_total,
                    len(deleted_rows) if active == "deleted" else len(rows),
                    len(deleted_rows),
                ),
                home=False,
            ),
            headers=gallery_headers(),
        )
        # Remember the choice only when he MADE one. Writing the cookie on
        # every load would mean the first visit silently pins a default, and a
        # shared link carrying ?view=list would change what the recipient sees
        # everywhere afterwards rather than just on that page.
        if asked in ("grid", "list"):
            response.set_cookie(
                _VIEW_COOKIE,
                asked,
                max_age=_VIEW_MAX_AGE,
                httponly=True,
                samesite="lax",
                secure=True,
                path="/",
            )
        return response

    @router.get("/thumb/{artifact_id}.png")
    def thumbnail(artifact_id: str) -> Response:
        """The picture of the version currently being served, or a 404.

        On the GALLERY origin, behind Jeremy's login like everything else here —
        a page's content is only readable with a capability token, and a picture
        OF that content must not be an easier way to the same thing.

        404 rather than a placeholder image when there is none: the wall's text
        tile is already a complete tile, and a broken-image icon would read as
        the page being broken rather than the picture being absent.

        Cached for a minute and no longer. A thumbnail belongs to a version, so
        it never changes for a given one — but the URL names the PAGE, and the
        page's live version moves. A long cache would show Jeremy last week's
        picture of a board he republished this morning, which is the exact drift
        this service exists to prevent.
        """
        png = store.live_thumbnail(db, artifact_id)
        if png is None:
            raise HTTPException(404, "no thumbnail for that page")
        return Response(
            png,
            media_type="image/png",
            headers={**gallery_headers(), "cache-control": "private, max-age=60"},
        )

    @router.post("/a/{artifact_id}/archive")
    def archive_page(artifact_id: str, request: Request, archived: str = Form("1")):
        """Take a page off the wall, or put it back.

        Jeremy, 20 Sep 2026: "I would also like the ability to archive so that
        they don't appear in my gallery." Not deletion, and reversible: the
        address keeps working, the versions stay, and every decision and answer
        on it stays readable. A page whose work is done should stop competing
        for his attention without the record of it being destroyed.

        Behind the same same-origin guard as answering, for the same reason —
        this is a state change made with his session, so a form on someone
        else's page must not be able to make it.
        """
        _same_origin_write(request)
        if not store.set_archived(db, artifact_id, archived == "1", agent=None):
            raise HTTPException(404, "no such artifact")
        return RedirectResponse(f"/a/{artifact_id}", status_code=303)

    @router.post("/a/{artifact_id}/pin")
    def pin_page(artifact_id: str, request: Request, pinned: str = Form("1")):
        """Pin a page to the front of the wall, or unpin it.

        Behind the same same-origin guard as the rest, for the same reason: a
        state change made with his session.

        Redirects BACK TO THE WALL, not to the page -- unlike archive and
        rename, which are usually done while looking at the page itself. Pinning
        is a statement about the wall's order, and the answer to "did that
        work?" is the wall, reordered, with the page now leading it.
        """
        _same_origin_write(request)
        if not store.set_pinned(db, artifact_id, pinned == "1", agent=None):
            raise HTTPException(404, "no such artifact")
        return RedirectResponse("/", status_code=303)

    @router.post("/a/{artifact_id}/duplicate")
    def duplicate_page(artifact_id: str, request: Request):
        """Copy a page, and go straight to the copy.

        Straight to it, not back to the wall: the only reason to duplicate a
        page is to change the copy, so the next thing he wants is the copy in
        front of him. Archive redirects to the page and pin to the wall for
        the same kind of reason -- where the answer to "did that work?" is.
        """
        _same_origin_write(request)
        try:
            copy, body = store.duplicate(db, artifact_id, agent=None)
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc
        thumbs.render_in_background(db, copy.id, copy.version, body)
        return RedirectResponse(f"/a/{copy.id}", status_code=303)

    @router.post("/a/{artifact_id}/delete")
    def delete_page(artifact_id: str, request: Request):
        """Delete a page. Recoverable for thirty days.

        Destroys nothing: the body, every version and every decision with his
        answers and his notes are all still there. Redirects to the wall,
        because the page he was looking at is no longer somewhere to be.
        """
        _same_origin_write(request)
        if not store.set_deleted(db, artifact_id, True, agent=None):
            raise HTTPException(404, "no such artifact")
        return RedirectResponse("/?filter=deleted", status_code=303)

    @router.post("/a/{artifact_id}/restore")
    def restore_page(artifact_id: str, request: Request):
        """Put a deleted page back, with its address and its history intact."""
        _same_origin_write(request)
        if not store.set_deleted(db, artifact_id, False, agent=None):
            raise HTTPException(404, "no such artifact")
        return RedirectResponse(f"/a/{artifact_id}", status_code=303)

    @router.post("/a/{artifact_id}/destroy")
    def destroy_page(artifact_id: str, request: Request, confirm: str = Form("")):
        """Remove a page and everything under it, for good.

        Takes a typed confirmation. Not because a second click is meaningful --
        he clicks through those -- but because this is the ONE operation in the
        service that can lose something he said, and the words have to be in
        front of him at the moment he commits to it. The form asks for the
        word "destroy"; anything else is refused without doing anything.
        """
        _same_origin_write(request)
        if confirm.strip().lower() != "destroy":
            raise HTTPException(400, "type destroy to confirm")
        if not store.destroy(db, artifact_id, agent=None):
            raise HTTPException(404, "no such artifact")
        return RedirectResponse("/?filter=deleted", status_code=303)

    @router.post("/a/{artifact_id}/rename")
    def rename_page(artifact_id: str, request: Request, title: str = Form(...)):
        """Give a page a different name, without republishing it.

        Renaming has only ever been possible by sending the whole body again
        with --title, which is fine for an agent holding the file and impossible
        for Jeremy in a browser. What a page is FOR is often clear only after a
        few revisions, and a name that turned out wrong should never be a reason
        to abandon an address people already have.
        """
        _same_origin_write(request)
        cleaned = title.strip()
        if not cleaned:
            raise HTTPException(422, "a page needs a name")
        try:
            found = store.rename(db, artifact_id, cleaned, agent=None)
        except (store.BodyTooLarge, store.InvalidText):
            raise HTTPException(422, "that name is too long") from None
        except store.SecretFound:
            raise HTTPException(
                422, "that name looks like it contains a secret"
            ) from None
        if not found:
            raise HTTPException(404, "no such artifact")
        return RedirectResponse(f"/a/{artifact_id}", status_code=303)

    @router.get("/a/{artifact_id}", response_class=HTMLResponse)
    def detail(artifact_id: str) -> HTMLResponse:
        # Look the artifact up rather than framing whatever the URL said: an
        # unchecked id renders a frame pointing at an invented subdomain, and
        # the title is wanted on the page anyway.
        with db.connect() as conn:
            row = conn.execute(
                "select title, read_token, agent, archived_at, updated_at"
                " from artifact where id = %s",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "no such artifact")
        # Open or answered-but-not-closed decisions only: a closed one is the agent
        # saying it no longer needs an answer, so it has nothing left to show here.
        decision_rows = decisions.list_for_artifact(
            db, artifact_id, include_answered=True, include_closed=False
        )
        asks_above, settled_below = _render_decisions(
            decision_rows, listening_since=events.last_read(db, row[2])
        )
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
        # The title carries the actions now, in a menu, the way the reference
        # does. The previous version put a "Rename or archive" fold at the
        # bottom of the page with unstyled form controls in it, which is what
        # Jeremy meant by "wow that ux is ugly" -- raw browser widgets beside a
        # page that had been designed.
        archived = row[3] is not None
        menu = _page_menu(
            artifact_id,
            row[0],
            row[2],
            archived,
            _when(str(row[4])) if row[4] else "just now",
        )
        return HTMLResponse(
            _shell(
                row[0],
                f"{asks_above}"
                f'<div class="sheet" id="artifact-sheet"><iframe id="artifact-frame"'
                f' src="{src}" sandbox="{sandbox}" title="{escape(row[0])}"></iframe></div>'
                f"{settled_below}"
                f"{_rename_dialog(artifact_id, row[0])}"
                f"{_link_dialog(artifact_id)}",
                # gallery.js reads the content origin straight off this iframe's own
                # `src` -- fixed ids, not a class selector, so a future second iframe
                # on this page could never be mistaken for this one.
                scripts='<script src="/gallery.js" defer></script>',
                title_html=menu,
                bleed=True,
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
