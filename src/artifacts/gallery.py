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

import json
import logging
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, NamedTuple
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from markupsafe import escape
from starlette.concurrency import run_in_threadpool

from artifacts import (
    comments,
    connect,
    decisions,
    email_share,
    events,
    folders,
    forms_ui,
    frame_pass,
    holds,
    image_store,
    images,
    inbox,
    keys,
    listening,
    mail,
    pagediff,
    partner_in,
    rename,
    stamp,
    store,
    thumbs,
)
from artifacts import text as text_module
from artifacts.content import _script_json
from artifacts.db import Database, in_list
from artifacts.store import content_domain, content_url, gallery_origin

# Mirrors api.py's own wording for the identical fault: the scanner broke, the
# content did not -- 503, not 422, so Jeremy is not left resubmitting a note that was
# never the problem.
_SCAN_FAILED_DETAIL = (
    "the secret scanner failed -- this is a fault on our side, not yours. "
    "Nothing was saved. Tell the operator."
)


# How long the gallery will hold a "has this page changed" read, and how many
# it will hold at once. Twenty-five seconds sits under every proxy timeout in
# front of this service and under the browser's own patience; sixteen is the
# same bound the agent-facing waiter uses, and one person's open tabs will
# never approach it. Past it, the read answers immediately -- which is
# ordinary polling, and correct, just chattier.
_MAX_WATCH_SECONDS = 25.0
_MAX_WATCHERS = 16
_watchers = threading.BoundedSemaphore(_MAX_WATCHERS)


def gallery_headers() -> dict[str, str]:
    """Built per call, because the content domain is read from the environment
    at call time (store.content_domain())."""
    return {
        "content-security-policy": (
            "default-src 'none'; style-src 'self' 'unsafe-inline'; "
            # Pictures on a comment are shown from the page's own content origin
            # (one host per page), never from here: this origin holds his session.
            f"img-src 'self' https://*.{content_domain()}; "
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
            # connect-src 'self' and nothing else: gallery.js asks THIS origin
            # whether the page in front of him has been republished
            # (homelab-ij8.74). It is the narrowest thing that lets the wall
            # notice, and it can reach nowhere but here.
            "connect-src 'self'; "
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
  /* The only red in the interface: destructive actions and marks -- the
     archive/destroy controls below, and the "Remove this" mark label
     (page review step 1, homelab-ij8.108) reuse it rather than mint a
     second, uncalibrated red. */
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
main{display:block}
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
.bar .acts{display:flex;align-items:center;gap:.25rem;margin-left:auto}
.bar .acts + .profile{margin-left:0}
.sharebtn,.fsbtn{display:inline-flex;align-items:center;gap:.3rem;height:1.75rem;
  padding:0 .5rem;border:0;border-radius:.45rem;background:transparent;
  color:var(--muted);text-decoration:none;font:inherit;font-size:.8125rem;
  cursor:pointer}
.sharebtn:hover,.fsbtn:hover{background:var(--chip-hover);color:var(--ink)}
.fsbtn[hidden]{display:none}
/* Pin mode (pinned comments, slice 2): a button like the bar's others, and a
   filled one while the mode is on. */
.pinmodebtn{display:inline-flex;align-items:center;height:1.75rem;padding:0 .5rem;
  border:0;border-radius:.45rem;background:transparent;color:var(--muted);
  font:inherit;font-size:.8125rem;cursor:pointer}
.pinmodebtn:hover{background:var(--chip-hover);color:var(--ink)}
.pinmodebtn.on{background:var(--chip);color:var(--ink)}
.pinmodebtn[hidden]{display:none}
.acts .mi{width:1rem;height:1rem;opacity:1}
@media (max-width:34rem){.sharebtn span{display:none}}
/* The PAGE view only (the wall and the lists keep .wrap as it was): the agent's
   page is flush under the bar at the window's full width, like claude.ai. The
   questions and the settled history keep a centred 52rem column, because they
   are prose and a full-width sentence is unreadable. */
.bar.bleed.flush{margin-bottom:0}
.wrap.full{max-width:none;margin:0;padding:0 0 3.5rem}
.col{max-width:52rem;margin:0 auto;padding:0 1rem}
.col.top{padding-top:1.25rem}
.full .sheet{border:0;border-radius:0;box-shadow:none}
.sheet:fullscreen iframe{height:100vh !important}
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
.rtag{font:600 .7rem var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--accent)}
.rhint{font-size:.85rem;color:var(--muted);margin:.2rem 0}
.htag{font:600 .7rem var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-left:.5rem}
.rmode{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem;font-size:.85rem;color:var(--muted);margin:.2rem 0}
.rmode form{display:inline;margin:0}
.rmode .rheld{font:600 .7rem var(--mono);letter-spacing:.06em;color:var(--ink)}
.verdict{margin:.3rem 0}
.rround{display:flex;flex-wrap:wrap;align-items:baseline;gap:.5rem;font-size:.85rem;color:var(--muted);margin:.2rem 0}
.rround a{color:var(--accent)}
.carried{font-size:.85rem;margin:.3rem 0}
.carried ul{margin:.2rem 0;padding-left:1.1rem}
.carried .cstate{font:600 .68rem var(--mono);text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-right:.4rem}
.carried .cstate.still{color:var(--accent)}
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
.ask.held{border-left-color:var(--line);box-shadow:none;background:transparent}
.ask.held .q{font-weight:500;color:var(--muted);margin:0 0 .3rem}
.ask .qroute{margin:0 0 .65rem;font-size:.82rem;color:var(--muted)}
.ask .qroute.waiting_now{color:var(--ok)}
.ask .qctx{margin:0 0 .6rem;font-size:.9rem;color:var(--muted);white-space:pre-line}
.ask .qmeta{margin:0 0 .65rem;font:.74rem/1.5 var(--mono);color:var(--muted);
  display:flex;flex-wrap:wrap;gap:.2rem .7rem}
.ask .qmeta .urg-blocking,.openlist .urg-blocking,.ask .qmeta .over,.openlist .over{
  color:var(--now-solid)}
.openlist{margin:0 0 1rem;padding:.7rem 1.1rem;border:1px solid var(--line);
  border-radius:.7rem;background:var(--card)}
.openlist h2{font:600 .74rem/1.4 var(--mono);letter-spacing:.14em;text-transform:uppercase;
  color:var(--accent);margin:0 0 .4rem}
.openlist ul{list-style:none;margin:0;padding:0}
.openlist li{margin:.15rem 0;font-size:.9rem}
.openlist .lmeta{font:.74rem/1.4 var(--mono);color:var(--muted);margin-left:.5rem}
.bar .inboxlink{font-size:.84rem;color:var(--muted);text-decoration:none;
  border:1px solid var(--line);border-radius:999px;padding:.18rem .6rem}
.bar .inboxlink.some{color:var(--on-now);background:var(--now-solid);
  border-color:var(--now-solid);font-weight:600}
.inbox{list-style:none;margin:0;padding:0}
.inbox>li{margin:0 0 .9rem}
.inbox .ihead{margin:0 0 .35rem;font:.74rem/1.5 var(--mono);color:var(--muted);
  display:flex;flex-wrap:wrap;gap:.2rem .7rem}
.inbox .ihead a{color:inherit}
.inbox .ibody{margin:0 0 .6rem;font-size:.92rem;white-space:pre-line}
.inbox .ilink{font-size:.84rem}
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
.part{display:block;font-size:.8rem;color:var(--now);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.part.flagged{font-weight:700}
.rmeta .part{display:inline}
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
/* What has happened to a question since it was asked (homelab-ij8.44).
   Set apart from the question above it and the buttons below, because on an
   OPEN card it is a warning -- something moved under this question and he must
   read it before choosing. The left rule is --now on an open card for the same
   reason the card itself carries one; on a settled card the parent is already
   --ok and this inherits the quieter treatment. */
/* THE CONVERSATION ON A PAGE (homelab-ij8.18). Quiet by construction: it sits
   after the page, it borrows the card surface rather than inventing one, and a
   settled thread folds into a <details> --
   out of the way must never become gone, so the count is on the summary.

   An agent bubble and a human bubble differ by a left rule and nothing else.
   Colouring whole bubbles would turn a two-line exchange into a decorated
   thing; the only question a reader has is "who said this", and the name
   answers it. */
/* The bar control, sized and shaped like the other bar buttons rather than
   like a new kind of thing. `.has` is the only state: there is something to
   read. A count with no unread is a number for its own sake. */
.cbtn{display:inline-flex;align-items:center;gap:.3rem;height:1.75rem;
  padding:0 .5rem;border-radius:.45rem;color:var(--muted);text-decoration:none}
.cbtn:hover{background:var(--chip-hover);color:var(--ink)}
.cbtn.has{color:var(--ink)}
.cn{font:600 .6875rem/1 var(--mono);color:var(--on-accent);
  background:var(--accent);border-radius:.6rem;padding:.1rem .3rem}

/* A PANEL, opened from the bar -- the reference keeps the conversation beside
   the page rather than under it, and so does this: DOCKED on the right, below
   the 48px bar, and the page area narrows by exactly its width so nothing is
   covered and the frame reflows. It used to be a `:target` overlay with a
   blurred, dimmed backdrop over a page that can be tens of thousands of pixels
   tall, opened by navigating to #comments; both were slow. There is no
   backdrop, no dimming and no blur on it anywhere.

   With scripts, comments.js marks <body> `.cjs` and toggles `.cpanel-open`,
   so no click navigates. Without them the fragment still does the whole job:
   `:target` shows the same docked panel, and `:has()` narrows the page. */
.cpanel{display:none;position:fixed;top:48px;right:0;bottom:0;z-index:30;
  width:min(24rem,100%);background:var(--paper);border-left:1px solid var(--line);
  padding:.9rem 1.1rem 1.6rem;overflow-y:auto;overscroll-behavior:contain}
body:not(.cjs) .cpanel:target,body.cpanel-open .cpanel{display:block}
body.cpanel-open .wrap.full{margin-right:min(24rem,100%)}
body:not(.cjs) .wrap.full:has(.cpanel:target){margin-right:min(24rem,100%)}
.chead{display:flex;align-items:center;justify-content:space-between;
  gap:.5rem;margin:0 0 .9rem}
.cpanel h2{font:600 .74rem/1.4 var(--mono);letter-spacing:.14em;
  text-transform:uppercase;color:var(--muted);margin:0}
.cclose{display:inline-flex;align-items:center;justify-content:center;
  width:1.75rem;height:1.75rem;border:0;border-radius:.45rem;background:transparent;
  color:var(--muted);font:400 1.2rem/1 ui-sans-serif,system-ui,sans-serif;
  text-decoration:none;cursor:pointer}
.cclose:hover{background:var(--chip-hover);color:var(--ink)}
.cbtn[aria-expanded="true"]{background:var(--chip);color:var(--ink)}
/* THE PANEL ON A PHONE: a bottom sheet, 70vh, sliding up over the page. The one
   place it may cover the page, because a 24rem column beside a 390px page is
   two columns of two-word lines -- and it still has no backdrop and no blur. */
@media (max-width:34rem){
  .cpanel{top:auto;left:0;width:auto;height:70vh;border-left:0;
    border-top:1px solid var(--line);border-radius:12px 12px 0 0;
    box-shadow:var(--shadow);z-index:45;animation:cup .18s ease-out}
  body.cpanel-open .wrap.full,
  body:not(.cjs) .wrap.full:has(.cpanel:target){margin-right:0}
}
@keyframes cup{from{transform:translateY(100%)}}
@media (prefers-reduced-motion:reduce){.cpanel{animation:none}}
/* END THE PANEL ON A PHONE */
.cempty{font-size:.875rem;line-height:1.55;color:var(--muted);margin:0 0 .9rem}
/* This page / All pages. The wall's own .tab, so the two switches in this
   product read as one idea; only the spacing is local to the panel. */
.cscope{margin:0 0 .9rem}
/* Which page a conversation belongs to, in the all-pages view. Quieter than
   the thread itself: it is a label on the group, not the thing being read. */
.cpage{font:600 .7rem/1.4 var(--mono);letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);margin:1.1rem 0 .5rem;display:flex;align-items:center;gap:.5rem}
.cpage:first-of-type{margin-top:0}
.cpage a{color:var(--muted);text-decoration:none}
.cpage a:hover{color:var(--ink)}
.carch{font:500 .62rem/1 ui-sans-serif,system-ui,sans-serif;letter-spacing:.06em;
  color:var(--muted);background:var(--paper);border-radius:999px;
  padding:.16rem .4rem;box-shadow:inset 0 0 0 1px var(--line)}
.threads{list-style:none;margin:0 0 1rem;padding:0}
.thread{background:var(--card);border:1px solid var(--line);
  border-radius:.6rem;padding:.7rem .85rem;margin:0 0 .7rem}
.thread.settled{background:transparent}
.bubbles{list-style:none;margin:0;padding:0}
/* What a thread is ABOUT, above what was said about it -- the passage is the
   subject of the conversation, so it reads first, set apart and quieter. */
.cquote{margin:0 0 .5rem;padding:.35rem 0 .35rem .7rem;
  border-left:3px solid var(--chip);font-size:.8125rem;line-height:1.5;
  color:var(--muted);font-style:italic;overflow-wrap:anywhere}
/* Said plainly rather than in a warning colour: the comment is not wrong and
   nothing is broken -- the page moved on, and he is being told which version
   the words were on. */
.cmoved{display:block;margin-top:.3rem;font-style:normal;font-size:.75rem;
  color:var(--muted)}
.cwhere{margin-right:.4rem}
.coutdated-list{margin:0 0 .75rem;padding:.5rem .7rem;border:1px solid var(--line);
  border-radius:.4rem;font-size:.8125rem}
.coutdated-list h3{margin:0 0 .3rem;font-size:.8125rem}
.coutdated-list ul{margin:0;padding-left:1rem}
.coutdated-list span{color:var(--muted);font-size:.75rem}
.cmt{padding:.35rem 0;border-left:2px solid var(--line);padding-left:.7rem;
  margin:0 0 .35rem}
.cmt.human{border-left-color:var(--accent)}
/* An agent's words are plainly not his: a dashed rule, a tinted ground, and the
   writer named with "(agent)". The office after "via session" is only what the
   session called itself; one key can be shared by several offices. */
.cmt.agent{border-left-style:dashed;border-left-color:var(--muted);
  background:var(--chip);padding-right:.5rem;border-radius:0 .35rem .35rem 0}
.cvia{margin-left:.5rem;font-size:.75rem;color:var(--muted)}
.cwho{font:600 .8125rem/1.4 ui-sans-serif,system-ui,sans-serif;color:var(--ink)}
.cwhen{margin-left:.5rem;font-size:.75rem;color:var(--muted)}
.cmt p{margin:.15rem 0 0;font-size:.875rem;line-height:1.55;color:var(--ink);
  white-space:pre-wrap;overflow-wrap:anywhere}
.cnew,.creply,.nreply{display:flex;gap:.4rem;margin:.5rem 0 0}
.cnew input,.creply input,.nreply input{flex:1 1 auto;min-width:0;padding:.4rem .6rem;
  border:1px solid var(--line);border-radius:.45rem;background:var(--paper);
  color:var(--ink);font:.875rem/1.4 ui-sans-serif,system-ui,sans-serif}
.cnew button,.creply button,.nreply button,.cresolve button{padding:.4rem .7rem;cursor:pointer;
  border:1px solid var(--line);border-radius:.45rem;background:var(--chip);
  color:var(--ink);font:.8125rem/1.4 ui-sans-serif,system-ui,sans-serif}
.cnew button{background:var(--accent);color:var(--on-accent);border-color:var(--accent)}
.cresolve{margin:.5rem 0 0}
.cnew,.creply{flex-wrap:wrap}
.cattach{display:none;flex:1 1 100%;flex-wrap:wrap;align-items:center;gap:.4rem}
.cpics-on .cattach{display:flex}
.cattach .cattachbtn{background:var(--chip);color:var(--ink);border-color:var(--line)}
.cnote{font-size:.75rem;color:var(--muted)}
.cpics{display:flex;flex:1 1 100%;flex-wrap:wrap;gap:.4rem}
.cpics:empty{display:none}
.cpic{position:relative;display:inline-block}
.cpic canvas,.cpic .cpicname{display:block;width:3.5rem;height:3.5rem;border:1px solid var(--line);
  border-radius:.35rem;background:var(--chip);font-size:.65rem;overflow:hidden;overflow-wrap:anywhere}
.cpic .cpicx{position:absolute;top:-.4rem;right:-.4rem;padding:0 .35rem;line-height:1.2;border-radius:1rem}
.cpics-say{flex:1 1 100%;margin:0;font-size:.75rem;color:var(--now)}
.cpics-say:empty{display:none}
.cpicsrow{display:flex;flex-wrap:wrap;gap:.4rem;margin:.35rem 0 0}
.cpicsrow .cimg{display:inline-block;line-height:0}
.cthumb{max-width:6rem;max-height:6rem;object-fit:cover;border:1px solid var(--line);
  border-radius:.35rem;cursor:zoom-in}
.cthumb.big{max-width:100%;max-height:none;object-fit:contain;cursor:zoom-out}
.cpicsrow .cimg.big{flex:1 1 100%}
.nthread{margin:.2rem .35rem .6rem}
.nthread .nhead{font-size:.8125rem;color:var(--muted);margin:0 0 .3rem}
.nthread .ntag{margin-left:.5rem;color:var(--now);font-weight:600}
.settled-cmts{margin:.6rem 0 0}
.settled-cmts > summary{cursor:pointer;list-style:none;display:inline-flex;
  align-items:center;gap:.5rem;font:.8125rem/1.4 ui-sans-serif,system-ui,sans-serif;
  color:var(--muted)}
.settled-cmts > summary::-webkit-details-marker{display:none}
.settled-cmts > summary:hover{color:var(--ink)}
.settled-cmts .n{font-size:.75rem;color:var(--muted)}
/* The same triangle the settled-comments fold uses, and for the same reason:
   without
   a marker the summary reads as a caption and nobody presses it. Rendered
   once and looked at -- "Settled  1 conversation" sat there looking inert. */
.settled-cmts > summary .chev{display:inline-block;width:0;height:0;flex:0 0 auto;
  border-left:5px solid currentColor;border-top:4px solid transparent;
  border-bottom:4px solid transparent;transition:transform .12s}
.settled-cmts[open] > summary .chev{transform:rotate(90deg)}

.since{margin:.5rem 0 .75rem;padding:.45rem .7rem;border-left:2px solid var(--line);
  background:var(--bg);border-radius:.3rem}
.since b{display:block;font:600 .7rem/1.5 var(--mono);letter-spacing:.1em;
  text-transform:uppercase;color:var(--muted)}
.since ul{margin:.25rem 0 0;padding-left:1.1rem}
.since li{font-size:.875rem;line-height:1.5;color:var(--ink)}
.since li + li{margin-top:.3rem}

.sheet .ask{position:absolute;margin:0;z-index:1;
  transform:translateZ(0);isolation:isolate}

/* AND, below 56rem, the overlap does not happen in the first place.
   translateZ(0) above is a MITIGATION: it asks the compositor for a separate
   layer so the tap lands on the control instead of the iframe. It works, and
   it can only ever be confirmed on a real iPhone, and a later layout change
   can undo it without a single desktop browser noticing. ij8.82's acceptance
   criterion asks for better than that -- "the fix does not depend on a z-index
   that a later layout change can silently undo".

   This is the structural half. Lifting a control onto the frame exists to put
   it beside the part of the page it is about; below 56rem there is no beside,
   so the lift buys nothing and costs the whole class of risk. gallery.js
   therefore never lifts below this width, and this rule un-lifts anything that
   already was -- a window narrowed, or a phone rotated back to portrait after
   landscape put it over 896. An overlap that does not exist cannot lose a
   hit-test, in any engine, and that is checkable without an iPhone.

   56rem is 896px AT A 16px ROOT FONT SIZE, which is above every iPhone in
   either orientation (852 is the widest in landscape) and above an iPad in
   portrait (820); an iPad in landscape is 1180 and still lifts, with the
   mitigation above covering it.

   THAT EQUALITY IS NOT A CONSTANT, and reading it as one cost Jeremy two
   answerable questions on 22 Sep 2026. gallery.js used to compare
   `innerWidth>=896` against this `rem` breakpoint; at any root font size above
   16px the two disagree over a whole band of window widths (Chrome's "Font
   size: Large" is 20px, which puts this breakpoint at 1120px), and in that band
   the script lifts a control that this rule immediately un-positions -- leaving
   the block below the frame and a control-sized hole in the page where it
   should have been. The script now asks `matchMedia("(min-width:56rem)")`, and
   this query is its exact complement (55.99, not 56, so the boundary width
   belongs to exactly one of them). Change one and you must change the other;
   better, change neither without checking both. */
   `width` carries !important and the others do not, deliberately. gallery.js
   writes top, left and width as INLINE styles, and an inline style beats a
   stylesheet rule whatever its specificity -- so a plain `width:auto` here
   would lose and the block would keep its lifted pixel width. `top` and `left`
   need no such help: a static box ignores them. */
@media (max-width:55.99rem){
  .sheet .ask{position:static;margin:0 0 .8rem;width:auto !important}
}


/* MENUS: the title dropdown and the per-tile dots. Modelled on the claude.ai
   artifact bar Jeremy sent through — a compact bar whose title carries the
   actions, rather than controls parked at the bottom of the page.
   <details>, not script: everything else here works with scripts off. */
.menu{position:relative;display:inline-block}
/* The agent filter. It borrows .menu wholesale -- same <details>, same panel,
   same rows as the tile dots -- so it is one mechanism rather than a second
   one that merely looks similar. Only the summary is dressed, and it is
   dressed to match the search box it stands beside rather than to stand out
   from it: same height, same radius, same hairline, no fill.

   When a filter IS on, the summary carries the ink colour and the accent rule
   instead of shouting with a background. Something is being hidden from the
   wall and that must be visible at a glance -- but it is a state, not an
   alarm. */
.pick > summary{display:inline-flex;align-items:center;gap:.35rem;
  height:1.75rem;padding:0 .5rem;border:1px solid var(--line);
  border-radius:.45rem;cursor:pointer;list-style:none;
  font:.8125rem/1 ui-sans-serif,system-ui,-apple-system,sans-serif;
  color:var(--muted);max-width:10rem;overflow:hidden;white-space:nowrap}
.pick > summary::-webkit-details-marker{display:none}
.pick > summary:hover{color:var(--ink);border-color:var(--muted)}
.pick.on > summary{color:var(--ink);border-color:var(--accent)}
.pick .mpanel{right:auto;left:0;min-width:11rem}
.pick .mrow[aria-current]{color:var(--ink);font-weight:600}
@media (max-width:40rem){.pick{display:none}}
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
.selbtn{position:absolute;z-index:60;font:500 .8rem/1 ui-sans-serif,system-ui,sans-serif;
  padding:.45rem .7rem;border-radius:8px;border:1px solid var(--accent);
  background:var(--card);color:var(--ink);box-shadow:0 2px 8px rgba(0,0,0,.18);cursor:pointer}
/* The three-action bar that replaced the single .selbtn (page review step 1,
   homelab-ij8.108): same look, laid out as a row with gaps. */
.selbar{position:absolute;z-index:60;display:flex;gap:.4rem}
/* The author-origin `display:flex` above always beats the user-agent
   stylesheet's `[hidden]{display:none}` -- origin, not specificity, decides
   that fight, and author rules win it regardless of selector weight. Without
   this rule `bar.hidden=true` is a no-op in every real browser: the bar
   renders on every gallery page from the first paint and never goes away.
   The fake-DOM node harness (tests/js/comments.test.mjs) only ever reads the
   `hidden` property, never applies a stylesheet, so it cannot catch this --
   caught in review instead (page review step 1, homelab-ij8.108). */
.selbar[hidden]{display:none}
/* The outline over the frame while picking a spot, and the numbered badges at
   pinned elements' corners: drawn here, over the page, never in it. The
   outline lets the pointer through, so the frame keeps hearing it. */
.pinout{position:absolute;z-index:60;box-sizing:border-box;pointer-events:none;
  border:2px solid var(--accent);background:rgba(217,164,0,.12);border-radius:.25rem}
.pinout[hidden]{display:none}
.pinbadge{position:absolute;z-index:59;min-width:1.25rem;height:1.25rem;padding:0 .3rem;
  border:0;border-radius:.65rem;background:var(--accent);color:var(--on-accent);
  font:600 .6875rem/1.25rem var(--mono);text-align:center;cursor:pointer;
  box-shadow:0 1px 4px rgba(0,0,0,.3)}
.cpinno{display:inline-block;margin-right:.4rem;min-width:1.1rem;padding:0 .3rem;
  border-radius:.6rem;background:var(--accent);color:var(--on-accent);
  font:600 .6875rem/1.1rem var(--mono);text-align:center}
.cspot{margin-right:.4rem;font-style:normal;font-weight:600}
/* A HOVER CARD: the comment on marked text, beside it, drawn by the gallery
   (comments.js) from the panel's own thread. Tokens only, so it follows the
   theme; fixed, because a scroll closes it. */
.hovercard{position:fixed;z-index:70;max-width:22rem;min-width:14rem;
  background:var(--card);color:var(--ink);border:1px solid var(--line);
  border-radius:.6rem;box-shadow:var(--shadow);padding:.6rem .75rem .65rem;
  font-size:.875rem}
.hovercard .ckind{display:block;margin:0 0 .3rem;color:var(--muted)}
.hovercard .ckind-remove{color:var(--danger)}
.hovercard .ckind-looks_good{color:var(--ok)}
.hovercard .bubbles{max-height:14rem;overflow-y:auto}
.hovercard .creply[hidden]{display:none}
.hcact{display:flex;gap:.4rem;justify-content:flex-end;margin:.5rem 0 0}
.hcact button{padding:.3rem .65rem;cursor:pointer;border:1px solid var(--line);
  border-radius:.45rem;background:var(--card);color:var(--ink);font:500 .8rem/1 ui-sans-serif,system-ui,sans-serif}
.hcact button:hover{background:var(--chip-hover)}
.selbar button{font:500 .8rem/1 ui-sans-serif,system-ui,sans-serif;
  padding:.45rem .7rem;border-radius:8px;border:1px solid var(--accent);
  background:var(--card);color:var(--ink);box-shadow:0 2px 8px rgba(0,0,0,.18);cursor:pointer}
.cquote{margin:0 0 .4rem;padding:.3rem .6rem;border-left:3px solid var(--accent);opacity:.85;
  font-size:.85rem;white-space:pre-wrap}
.ckind{font:600 .75rem var(--mono);text-transform:uppercase}
/* --danger already IS the interface's one red (see its own comment, :root
   above) -- a "Remove this" mark is exactly that meaning, so it reuses the
   token rather than invent a second, uncalibrated red with no dark-mode
   counterpart. --ok already covers "fine as it is" the same way. */
.ckind-remove{color:var(--danger)}
.ckind-looks_good{color:var(--ok)}
.cunplaced{margin:0 0 .6rem;font-size:.8rem;opacity:.8}
.bar .profile{margin-left:auto}.bar .profile .mpanel{right:0;left:auto}
/* THE PAGE BAR ON A PHONE. The title menu is the one thing in the bar that may give
   way: it shrinks to an ellipsis rather than push the bar past the screen. When the
   bar was wider than a 390px phone, Safari zoomed the whole page out to fit it, and the
   framed page -- sized to the real screen -- filled only two thirds of what Jeremy saw
   (2 Nov 2026). Full screen is dropped there too: the phone is already all page. */
.bar.bleed > .menu:not(.profile){flex:1 1 auto;min-width:0}
.bar.bleed > .menu:not(.profile) > summary{max-width:100%}
@media (max-width:34rem){.bar.bleed{gap:.4rem}.bar.bleed .fsbtn{display:none}}
[data-unread] .t::before,[data-unread] .rt::before{content:"";display:inline-block;
  width:.55em;height:.55em;border-radius:50%;background:var(--accent);
  margin-right:.4em;vertical-align:.1em}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
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
   A tab of its own since 21 Sep, on Jeremy's word: it was a <details> fold at
   the foot of the wall, under everything else, which is where you put things
   you would rather were not read. The three states a page can be in -- on the
   wall, archived, deleted -- are three tabs in one row now.

   What survived the move is the SKIN. A page off the wall must not look like
   one on it, whichever tab reached it: the pictures recede, the type greys,
   and both come back on hover for anything he actually reaches for. */
.archived-body .shot,.archived-body .rshot{opacity:.55;filter:saturate(.65)}
.archived-body .t,.archived-body .rt{color:var(--muted)}
.archived-body .tiles > li > a,.archived-body .row{background:transparent}
.archived-body .tiles > li > a:hover .t,
.archived-body .row:hover .rt{color:var(--ink)}
.archived-body .tiles > li > a:hover .shot,
.archived-body .row:hover .rshot{opacity:1;filter:none}
.archived-body .daygroup:first-child{margin-top:0}

/* --- a newer version is ready ------------------------------------------------
   FIXED, not inserted into the flow: the page must not move under his cursor
   while he is reading it, and a button that slides as he reaches for it is the
   same fault as the refresh he rejected. Bottom centre, out of the way of the
   page bar and the comments drawer, and it only ever appears because something
   actually changed. */
.newer{position:fixed;left:50%;transform:translateX(-50%);bottom:1.1rem;z-index:40;
  display:flex;align-items:center;gap:.6rem;max-width:min(92vw,34rem);
  padding:.55rem .6rem .55rem .9rem;border:1px solid var(--line);
  border-radius:.7rem;background:var(--card);color:var(--ink);
  box-shadow:0 6px 24px rgba(0,0,0,.14);
  font:.8125rem/1.4 ui-sans-serif,system-ui,-apple-system,sans-serif}
.newer button{padding:.35rem .65rem;cursor:pointer;border-radius:.45rem;
  border:1px solid var(--accent);background:var(--accent);color:var(--on-accent);
  font:500 .8125rem/1.25 ui-sans-serif,system-ui,-apple-system,sans-serif}
.newer button.ghost{background:var(--chip);color:var(--ink);border-color:var(--line)}

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
.rows .setaside{color:var(--muted);font-size:.7rem;margin-left:.4rem}
.row-wrap .menu{position:absolute;top:50%;right:.35rem;transform:translateY(-50%);
  z-index:5}
.row-wrap .menu > summary{display:flex;align-items:center;justify-content:center;
  width:32px;height:32px;background:transparent;border:0;border-radius:8px;
  line-height:0}
.row-wrap .menu > summary:hover,.row-wrap .menu[open] > summary{
  background:var(--chip)}
.row-wrap .menu[open]{z-index:30}
.row-wrap.asking .row{box-shadow:inset 2px 0 0 var(--now-solid)}
/* SELECT MODE (folders design 3.3): a checkbox per page on the corner where the dots were,
   inside one form; the bar is stuck to the foot of the screen so it is under his thumb. */
.pick{position:absolute;z-index:6;margin:0;display:flex;align-items:center;
  justify-content:center;cursor:pointer}
.pick input{width:22px;height:22px;margin:0;accent-color:var(--accent)}
.tile-wrap > .pick{top:10px;left:10px;width:40px;height:40px;background:var(--chip);
  border-radius:8px}
.row-wrap > .pick{top:50%;left:0;width:44px;height:44px;transform:translateY(-50%)}
.row-wrap.sel .row{padding-left:2.9rem}
.selbar{position:sticky;bottom:0;z-index:50;display:flex;flex-wrap:wrap;gap:.5rem;
  align-items:center;padding:.6rem .75rem;margin-top:1rem;background:var(--card);
  border:1px solid var(--line);border-radius:12px;box-shadow:0 -2px 10px rgba(20,20,15,.12)}
.selbar .btn{min-height:40px}
.selnote{margin:.5rem 0;color:var(--muted);font-size:.875rem}
/* WHAT GOES FIRST WHEN THERE IS NO ROOM. A list is scanned for titles, so the
   title wins and the metadata yields -- in order: the agent, then the waiting
   badge (the amber edge on the row still says a question is there), then the
   time. Rendered at 390px, where the badge had pushed one row's title out
   COMPLETELY: a row with no title is not a row.
   `.rmeta` may also shrink now; it was flex:0 0 auto, which is what let it
   take the whole width and leave the title nothing. */
.rmeta{min-width:0}
/* WHICH SESSION, and the brief for it was "make it subtle in the design and
   look good" (Jeremy, 21 Sep). So: the monospace face, because it is an
   identifier and reads as one; a step smaller than the meta row it sits in;
   and --muted, the token the meta row already uses, NOT something fainter.
   Hierarchy here comes from size and face, never from dropping contrast: I
   made the pin invisible on this same wall by taking --muted to .5 opacity,
   which measures 1.76:1, and called it "subtle". Subtle is a smaller,
   different-looking thing you can still read. No border, no chip, no fill.

   NO SEPARATOR OF ITS OWN. The meta row already joins its parts with _SEP,
   and the ::before middot I first wrote here both duplicated that and was
   malformed -- "\00b7" with no terminating space, so CSS swallowed the next
   characters and every tile rendered a literal glyph-and-"b7". Invisible to
   the whole suite; obvious the moment the wall was rendered and looked at. */
.sess{font:.6875rem/1 var(--mono);color:var(--muted);letter-spacing:.02em}
/* First to go when the row narrows: the seat name answers "whose" and this
   only answers "which of theirs", which is the finer question and the one a
   phone has no room for. */
@media (max-width:52rem){.rmeta .sess{display:none}}
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
#    at least 200 and at most 100000 (clamped, not dropped, above that; below 200
#    the scripts-off floor stands) -- anything else is ignored and the last good height (or
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
#  - Asks this origin every twenty seconds whether the page in front of him has
#    been republished, and if it has, OFFERS the new one (homelab-ij8.74).
#
#    His own ruling, 19 Sep, choosing (b) over refreshing under him: tell him a
#    newer version exists and wait for him to say when. A page that changes
#    mid-sentence is worse than a stale one, and much worse with a question on
#    it. So nothing here ever reloads by itself; the bar is `position:fixed`,
#    so the page never moves under the cursor while he is reaching for a
#    button; and `told` remembers what has been offered, so dismissing is final
#    for that version while a newer one still speaks up.
#
#    It does not poll while the tab is hidden -- a wall of forgotten tabs
#    polling in the background is exactly the "hard enough to matter" the bead
#    rules out -- and the route it asks returns one integer from one indexed
#    row, no body and no join.
#  - Lifts a control ONLY where the stylesheet leaves it positioned, and asks
#    that question with `matchMedia("(min-width:56rem)")` -- the stylesheet's
#    own query, in the stylesheet's own unit.
#
#    22 Sep 2026, and it cost Jeremy two answerable questions. This script used
#    to compare `window.innerWidth>=896` against a stylesheet that un-lifts
#    below `56rem`, on the strength of "56rem = 896px" -- true only while the
#    root font size is 16px. Chrome's own "Font size: Large" is 20px, which puts
#    that breakpoint at 1120px, and for every window width in the band between
#    the two the script lifted a control that the stylesheet immediately forced
#    back to `position:static;width:auto`. The block fell out below the framed
#    page, and the height measured for it was still reported, so the page
#    reserved exactly that much room in the slot: a control-sized hole with the
#    control somewhere else. On the operator's board he saw a question, its
#    "asked at" line, and then blank white space where the buttons belong --
#    and a question whose buttons silently do not appear is worse than one that
#    says it cannot show them.
#
#    Two rules now, and they cannot drift apart: the query above, and a
#    stylesheet media query at `max-width:55.99rem` that is its exact
#    complement, so the boundary width belongs to one of them and not both.
#  - Lifts NOTHING while the frame is not showing the whole page, and clamps an
#    over-tall page instead of refusing its height outright.
#
#    22 Sep 2026, the second half of the same morning's fault and the one that
#    actually bit the operator's board. A lifted control is positioned at the
#    slot's PAGE coordinate, inside `.sheet`, which is `overflow:hidden`. That is
#    sound only while the frame is as tall as the page -- which is the normal
#    case, because this handler sizes it to the height the page reports. It used
#    to refuse any height over 20000px outright: the frame then kept its
#    scripts-off `78vh` floor and became a small scrolling window, every slot
#    coordinate below it pointed outside the sheet's box, and the controls were
#    clipped away -- while their measured heights were still reported, so the
#    page reserved a hole for each one. Jeremy saw a question with an empty
#    six-hundred-pixel void under it, and an INNER SCROLLBAR on the frame, which
#    is the tell: this handler sizes the frame so the outer page scrolls, so a
#    scrollbar inside it means the height was refused. The operator's board is
#    about 25,000px of aggregated decisions, and the ceiling was 20,000.
#
#    So: clamp rather than refuse (more of the page is visible either way), and
#    carry `fits` into the lift, because coordinates past the clamp point at
#    nothing. When it does not fit, the controls stay above the page where the
#    scripts-off layout keeps them -- readable, answerable, and no hole.
#  - And a net under that coupling: a block that reports `position:static` once
#    it is on the sheet was un-lifted by some rule anyway, so it goes back to
#    the parent it came from and NO size is reported for it. A control in the
#    wrong place is readable and answerable; an invisible one over a reserved
#    hole is the failure this whole arc exists to prevent.
#  - A frame whose pass is spent says so (`content.expired_page`), and this asks
#    the gallery's own origin for a fresh one and re-points the frame
#    (homelab-ij8.101). A pass is good once, and browsers re-request a frame's
#    last URL on reload (Firefox) and back/forward (Chrome), so this is the
#    ordinary path, not an error path. Capped at three in a row -- reset by the
#    first real state message -- so a pass that can never work (a deleted page,
#    a clock gone wrong) cannot turn into a loop against the server; the frame
#    then shows the expired page's own words, which name what to do. The new
#    src must be on the SAME content origin the frame already has, so nothing
#    this fetch returns can point the frame anywhere else.
# `frameScroll`: on an iPhone a swipe over the frame went to the frame's own
# scroller, which had nothing to scroll, so the gallery page did not move and
# Jeremy could scroll a page only from below it (2 Nov 2026, runcove-5t3oj.62).
# Once the frame holds the whole page it is told it has nothing to scroll
# (`scrolling="no"`), and the swipe scrolls the gallery. It keeps scrolling when
# it does not hold the whole page: before the first height arrives (the 78vh
# floor), past the cap, when the page is wider than its frame (the right of it
# would be unreachable; the shim reports `wide`), and in fullscreen (100vh).
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
var homes=new Map();var fits=true;
var sized=false,wide=false;
function frameScroll(){try{iframe.setAttribute("scrolling",sized&&fits&&!wide&&!document.fullscreenElement?"no":"auto")}catch(e){}}
document.addEventListener("fullscreenchange",frameScroll);
function liftsHere(){try{if(window.matchMedia)return window.matchMedia("(min-width:56rem)").matches}catch(e){}return window.innerWidth>=896}
function isStatic(el){try{return window.getComputedStyle(el).position==="static"}catch(e){return false}}
var renewals=0;
function renew(){
  if(++renewals>3)return;
  window.fetch(location.pathname+"/frame"+location.search,{credentials:"same-origin"})
    .then(function(r){return r.ok?r.json():null})
    .then(function(d){if(d&&typeof d.src==="string"&&new URL(d.src).origin===contentOrigin)iframe.src=d.src})
    .catch(function(){});
}
window.addEventListener("message",function(event){
  try{
    if(event.source!==iframe.contentWindow||event.origin!==contentOrigin)return;
    var data=event.data;
    if(data&&data.type==="artifact-expired"){renew();return}
    if(!data||typeof data!=="object"||data.type!=="artifact-state")return;
    renewals=0;
    if(isNum(data.height)&&data.height>0){
      fits=data.height<=100000;
      if(data.height>=200){iframe.style.height=(fits?data.height:100000)+"px";sized=true}
      wide=data.wide===true;frameScroll();
    }
    if(Array.isArray(data.slots)&&data.slots.length<=64){
      var seen=new Set();var sizes=[];
      // The stylesheet's breakpoint, in the stylesheet's unit -- never a pixel
      // number of our own. See the Python comment above this file's source.
      // `fits` too: a slot coordinate points nowhere while the frame is clipped.
      if(fits&&liftsHere())data.slots.forEach(function(slot){
        if(!slot||typeof slot.id!=="string"||seen.has(slot.id))return;
        seen.add(slot.id);
        var el=decisionEls.get(slot.id);
        if(!el||!isNum(slot.top)||!isNum(slot.left)||!isNum(slot.width))return;
        if(!homes.has(el))homes.set(el,el.parentNode);
        if(el.parentNode!==sheet)sheet.appendChild(el);
        el.style.top=slot.top+"px";
        el.style.left=slot.left+"px";
        el.style.width=slot.width+"px";
        // Un-lifted by some rule anyway: put it back, report no size.
        if(isStatic(el)){var hm=homes.get(el);
          if(hm&&hm!==sheet){hm.appendChild(el);
            el.style.top=el.style.left=el.style.width=""}
          return}
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
// A newer version, offered and never applied (homelab-ij8.74; the reasoning is
// in the note above this string). No timer anywhere: the server holds the
// read, so the answer arrives when something happens, and a hidden tab simply
// stops asking until it is looked at again.
// `window.fetch` guarded, not assumed: a browser without it simply never
// learns about a republish, which is where this started, rather than throwing
// on page load and taking the frame sizing down with it.
var sheetEl=document.getElementById("artifact-sheet");
if(sheetEl&&window.fetch){
  var told=+sheetEl.getAttribute("data-version")||0,bar=null,watching=false;
  var offer=function(){
    if(bar&&bar.parentNode)bar.parentNode.removeChild(bar);
    bar=document.createElement("div");bar.className="newer";
    var t=document.createElement("span");
    t.textContent="A newer version of this page has been published.";
    var go=document.createElement("button");go.type="button";
    go.textContent="Show it";
    go.addEventListener("click",function(){location.reload()});
    var no=document.createElement("button");no.type="button";
    no.className="ghost";no.textContent="Not now";
    no.addEventListener("click",function(){
      if(bar&&bar.parentNode)bar.parentNode.removeChild(bar);bar=null});
    bar.appendChild(t);bar.appendChild(go);bar.appendChild(no);
    document.body.appendChild(bar);
  };
  var watch=function(){
    if(watching||document.hidden)return;
    watching=true;
    window.fetch(location.pathname+"/current?since="+told+"&wait=25",
          {credentials:"same-origin"})
      .then(function(r){return r.ok?r.json():null})
      .then(function(d){
        watching=false;
        if(d&&typeof d.n==="number"&&d.n>told){told=d.n;offer()}
        watch();
      }).catch(function(){watching=false});
  };
  document.addEventListener("visibilitychange",watch);
  watch();
}
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
    "lock": '<path d="M4.5 7V5a3.5 3.5 0 0 1 7 0v2h.7c.4 0 .8.4.8.8v6.4c0 .4-.4.8-.8.8H3.8a.8.8 0 0 1-.8-.8V7.8c0-.4.4-.8.8-.8h.7Zm1.4 0h4.2V5a2.1 2.1 0 0 0-4.2 0v2Z"/>',
    "trash": '<path d="M6.4 1.6h3.2a.8.8 0 0 1 .8.8v.8h2.8a.65.65 0 0 1 0 1.3h-.5l-.7 8.2a1.6 1.6 0 0 1-1.6 1.5H5.6a1.6 1.6 0 0 1-1.6-1.5l-.7-8.2h-.5a.65.65 0 0 1 0-1.3h2.8v-.8a.8.8 0 0 1 .8-.8Zm.5 1.6h2.2v-.3H6.9v.3ZM4.6 4.5l.7 8.1a.3.3 0 0 0 .3.3h4.8a.3.3 0 0 0 .3-.3l.7-8.1H4.6Zm2 1.5h1.2v5.4H6.6V6Zm2.6 0h1.2v5.4H9.2V6Z"/>',
    "restore": '<path d="M8 2.4a5.6 5.6 0 1 0 5.4 7.1.7.7 0 0 0-1.34-.38A4.2 4.2 0 1 1 8 3.8c1.1 0 2.1.42 2.85 1.1l-1.6.02a.65.65 0 0 0 .01 1.3l3.2-.04a.65.65 0 0 0 .64-.66l-.04-3.2a.65.65 0 1 0-1.3.02l.01 1.35A5.58 5.58 0 0 0 8 2.4Z"/>',
    "copy": '<path d="M5.5 1.6h6.3a1.6 1.6 0 0 1 1.6 1.6v6.3h-1.3V3.2a.3.3 0 0 0-.3-.3H5.5V1.6ZM3.2 4.1h6.4a1.6 1.6 0 0 1 1.6 1.6v6.4a1.6 1.6 0 0 1-1.6 1.6H3.2a1.6 1.6 0 0 1-1.6-1.6V5.7a1.6 1.6 0 0 1 1.6-1.6Zm0 1.3a.3.3 0 0 0-.3.3v6.4c0 .17.13.3.3.3h6.4a.3.3 0 0 0 .3-.3V5.7a.3.3 0 0 0-.3-.3H3.2Z"/>',
    "pin": '<path d="M9.6 1.3 8.5 2.4l.4.4-3 2.2-1.6-.3-1.1 1.1 3 3L2 13l3.2-4.2 3 3 1.1-1.1-.3-1.6 2.2-3 .4.4 1.1-1.1-3.1-3.1Z"/>',
    "find": '<path d="M7.1 2.2a4.9 4.9 0 1 0 2.96 8.8l3 3a.7.7 0 0 0 1-1l-3-3A4.9 4.9 0 0 0 7.1 2.2Zm0 1.4a3.5 3.5 0 1 1 0 7 3.5 3.5 0 0 1 0-7Z"/>',
    "list": '<path d="M2 3.2h1.9v1.9H2V3.2Zm3.4 .35h8.6v1.2H5.4V3.55ZM2 7.05h1.9v1.9H2v-1.9Zm3.4 .35h8.6v1.2H5.4V7.4ZM2 10.9h1.9v1.9H2v-1.9Zm3.4 .35h8.6v1.2H5.4v-1.2Z"/>',
    "grid": '<path d="M2.2 2.2h5v5h-5v-5Zm6.6 0h5v5h-5v-5Zm-6.6 6.6h5v5h-5v-5Zm6.6 0h5v5h-5v-5Z"/>',
    # A speech bubble with a tail, at the same 16-square weight as the rest of
    # the set -- the reference uses one here too; its own glyph is a sprite we
    # cannot lift, so this is drawn to match our own icons rather than theirs.
    "comment": '<path d="M3 2.5h10a1.5 1.5 0 0 1 1.5 1.5v6A1.5 1.5 0 0 1 13 11.5H7.9l-3 2.6a.5.5 0 0 1-.83-.38V11.5H3A1.5 1.5 0 0 1 1.5 10V4A1.5 1.5 0 0 1 3 2.5Zm0 1.2a.3.3 0 0 0-.3.3v6c0 .17.13.3.3.3h1.87c.33 0 .6.27.6.6v1.3l2.1-1.77a.6.6 0 0 1 .39-.14H13a.3.3 0 0 0 .3-.3V4a.3.3 0 0 0-.3-.3H3Z"/>',
    "page": '<path d="M4 1.5h5l3 3v10a.5.5 0 0 1-.5.5h-7a.5.5 0 0 1-.5-.5v-13a.5.5 0 0 1 .5-.5Zm4.6 1.4v2.1h2.1L8.6 2.9ZM5.4 7.4h5.2v1H5.4v-1Zm0 2.3h5.2v1H5.4v-1Zm0 2.3h3.4v1H5.4v-1Z"/>',
    "link": '<path d="M6.9 9.1a2.9 2.9 0 0 0 4.3.3l1.7-1.7a2.9 2.9 0 0 0-4.1-4.1l-1 1 .9.9 1-1a1.6 1.6 0 0 1 2.3 2.3L10.3 8.5a1.6 1.6 0 0 1-2.4-.2l-1 .8Zm2.2-2.2a2.9 2.9 0 0 0-4.3-.3L3.1 8.3a2.9 2.9 0 0 0 4.1 4.1l1-1-.9-.9-1 1a1.6 1.6 0 0 1-2.3-2.3l1.7-1.7a1.6 1.6 0 0 1 2.4.2l1-.8Z"/>',
}


def _icon(name: str) -> str:
    return f'<svg class="mi" viewBox="0 0 16 16" aria-hidden="true">{_ICON[name]}</svg>'


# The unread marker (homelab-ij8.103): an ATTRIBUTE on the tile or row, drawn
# as a dot before the title by the stylesheet, and a word for a screen reader
# in the meta line. Not markup inside the title: other readers of this page
# (and its tests) take the title text as it is, and a marker spliced into it
# would change every page's name as far as they are concerned.
#
# No `title` on it (Jeremy, 14 Oct): the attribute sits on the whole tile, so the
# browser's tooltip covered it, and a click that opened the page left the tooltip
# standing over the page's frame until the pointer reached the gallery's own bar
# (the frame takes the mouse events that would have dismissed it). The dot says
# "changed" to the eye and _UNREAD_WORD says it to a screen reader.
_UNREAD_ATTR = ' data-unread="1"'
_UNREAD_WORD = '<span class="sr">Unread. </span>'

# A REPLY HE HAS NOT SEEN: the newest live agent comment under a thread in which
# he wrote an earlier message, made after he last opened the page. Without it a
# reply to him leaves a read page looking read. Yields who wrote it (the session
# label when there is one, so the tile can name the office, else the author).
# The rule itself lives in `comments.UNSEEN_REPLY` so his inbox asks the same
# question the wall does.
_WALL_REPLIED_BY = (
    "(select coalesce(nullif(c.session, ''), c.author) from comment c"  # noqa: S608
    f"  where c.artifact_id = a.id and {comments.UNSEEN_REPLY}"
    "  order by c.created_at desc, c.id desc limit 1)"
)
_WALL_UNREAD = (
    "(a.current_version > coalesce((select r.read_version"  # noqa: S608
    "   from page_read r where r.artifact_id = a.id), 0)"
    f"  or {_WALL_REPLIED_BY} is not null)"
)


def _reply_line(replied_by: str | None) -> str:
    """ "Reply from <office>" on a tile or row, in the style of `_part_line`."""
    if not replied_by:
        return ""
    who = comments.office_of(replied_by) or replied_by
    return f'<span class="part" title="Reply">Reply from {escape(who)}</span>'


def _part_line(part: str, flagged: bool) -> str:
    """Which part of the page changed, on its tile or row: "Running now:
    added". Jeremy's ruling 1a (6 Oct 2026) -- the wall marks a page updated and
    says WHICH PART, so one moved row does not read as the whole page
    redrawn. `flagged` is the publisher's `notify`: the same line, louder."""
    cls = "part flagged" if flagged else "part"
    return f'<span class="{cls}" title="Changed part">{escape(part)}</span>'


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


def _tile_menu(
    artifact_id: str,
    title: str,
    pinned: bool,
    origin: str,
    archived: bool = False,
    folder_id: str | None = None,
) -> str:
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
        # Folders (docs/agent/specs/2026-11-02-folders-design.md 3.3). "Move to folder" opens
        # a page of choices (`/a/{id}/folder`), never a list drawn into every tile of a wall.
        # Inside a folder the same menu can take the page straight out.
        _go(f"/a/{ident}/folder", "all", "Move to folder"),
        *(
            [
                _act(
                    f"/a/{ident}/folder",
                    "unarchive",
                    "Take out of folder",
                    {"folder": ""},
                )
            ]
            if folder_id
            else []
        ),
        # ARCHIVE BELONGS HERE. It was left off on the grounds that the tile
        # menu should be the short one -- which is true, and was the wrong
        # conclusion: archiving is a WALL action. Its whole purpose is to take
        # a page off the wall, so needing to open the page to do it is
        # backwards, and Jeremy said so. (Kept out of the short list for about
        # an hour, with a test asserting its absence. The test is now the
        # other way round.)
        _act(
            f"/a/{ident}/archive",
            "unarchive" if archived else "archive",
            "Unarchive" if archived else "Archive",
            {"archived": "0" if archived else "1"},
        ),
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


def _folder_menu(folder_id: str, title: str, archived: bool = False) -> str:
    """The dots on a folder's tile or row: rename it, archive it, or (in the Archived tab) bring
    it back. Same shape as a page's menu, and a SIBLING of the tile's link for the same reason.
    Both entries lead to a page of their own: a name has to be typed, and archiving asks first."""
    ident = escape(folder_id)
    rows = (
        [_act(f"/f/{ident}/archive", "unarchive", "Unarchive", {"archived": "0"})]
        if archived
        else [
            _go(f"/f/{ident}/rename", "rename", "Rename folder"),
            _go(f"/f/{ident}/archive", "archive", "Archive folder"),
        ]
    )
    return (
        '<details class="menu tmenu"><summary aria-label="Actions for folder '
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


# The connect page's own look: a narrow column of plain cards. Inline because
# the gallery CSP allows style 'unsafe-inline' and nothing else uses it.
_CONNECT_CSS = (
    "<style>.cx{max-width:40rem;margin:1.5rem auto;display:grid;gap:1.25rem}"
    ".cx section{border:1px solid var(--line,#ddd);border-radius:12px;padding:1.1rem 1.25rem}"
    ".cx h2{margin:0 0 .5rem;font-size:1.05rem}.cx p{margin:.4rem 0;line-height:1.5}"
    ".cx input{font:inherit;padding:.5rem .6rem;border:1px solid var(--line,#ccc);"
    "border-radius:8px;width:100%;box-sizing:border-box;margin:.35rem 0 .75rem}"
    ".cx .code{font:600 1.4rem/1.2 ui-monospace,monospace;letter-spacing:.12em;text-transform:uppercase}"
    ".cx pre{white-space:pre-wrap;background:var(--soft,#f5f5f4);padding:.75rem;border-radius:8px;"
    "font-size:.9rem;user-select:all}.cx .err{color:#b42318}.cx .ok{color:#067647}"
    ".cx dl{display:grid;grid-template-columns:auto 1fr;gap:.25rem .75rem;margin:.5rem 0}"
    ".cx dt{opacity:.7}.cx .row{display:flex;gap:.5rem;flex-wrap:wrap}"
    ".cx .boxes{display:flex;gap:.35rem;align-items:center;margin:.35rem 0 .9rem}"
    ".cx .boxes input{width:2.4rem;height:2.9rem;margin:0;padding:0;text-align:center;"
    "font:600 1.35rem ui-monospace,monospace;text-transform:uppercase}"
    ".cx .boxes .dash{padding:0 .2rem;opacity:.5}</style>"
)


# One box per character on the connect page, advancing as he types and
# accepting a paste of the whole code anywhere (Jeremy, 23 Sep 2026; he chose
# that a full code continues on its own -- approving still needs a press).
# PROGRESSIVE: the page is a plain one-field form, and this only swaps the
# field for boxes, so with scripts off it works exactly as before. The boxes
# write back into that same field, so the server sees one value either way.
_CONNECT_JS = """(function(){
"use strict";
var prompt=document.querySelector("pre.prompt");
if(prompt&&navigator.clipboard){var c=document.createElement("button");
  c.type="button";c.className="btn";c.textContent="Copy";
  c.addEventListener("click",function(){
    navigator.clipboard.writeText(prompt.textContent).then(function(){
      c.textContent="Copied";setTimeout(function(){c.textContent="Copy"},2000)},
      function(){c.textContent="Select the text and copy it"})});
  prompt.parentNode.insertBefore(c,prompt.nextSibling)}
var form=document.querySelector("form.code-form");if(!form)return;
var single=form.querySelector("input[name=code]");if(!single)return;
var wrap=document.createElement("div");wrap.className="boxes";var boxes=[];var sent=false;
function clean(t){return String(t||"").toUpperCase().replace(/[^0-9A-Z]/g,"")}
function sync(){var v=boxes.map(function(b){return b.value}).join("");
  single.value=v.length===8?v.slice(0,4)+"-"+v.slice(4):v;
  if(v.length===8&&!sent){sent=true;form.submit()}}
function fill(from,text){var t=clean(text);var i=from;
  for(;i<8&&t;i++){boxes[i].value=t.charAt(0);t=t.slice(1)}
  boxes[Math.min(i,7)].focus();sync()}
function make(i){var b=document.createElement("input");
  b.type="text";b.maxLength=1;b.autocomplete="off";b.className="box";
  b.setAttribute("autocapitalize","characters");b.setAttribute("inputmode","text");
  b.setAttribute("aria-label","Character "+(i+1)+" of 8");
  b.addEventListener("focus",function(){b.select&&b.select()});
  b.addEventListener("input",function(){var v=clean(b.value);
    if(v.length>1){b.value="";fill(i,v);return}
    b.value=v;if(v&&i<7)boxes[i+1].focus();sync()});
  b.addEventListener("keydown",function(e){
    if(e.key==="Backspace"&&!b.value&&i>0){e.preventDefault();boxes[i-1].value="";boxes[i-1].focus();sent=false}
    else if(e.key==="ArrowLeft"&&i>0)boxes[i-1].focus();
    else if(e.key==="ArrowRight"&&i<7)boxes[i+1].focus()});
  b.addEventListener("paste",function(e){var d=e.clipboardData||window.clipboardData;
    if(!d)return;e.preventDefault();fill(0,d.getData("text"))});
  return b}
for(var i=0;i<8;i++){var b=make(i);boxes.push(b);wrap.appendChild(b);
  if(i===3){var s=document.createElement("span");s.className="dash";s.textContent="-";wrap.appendChild(s)}}
var pre=clean(single.value);single.type="hidden";single.parentNode.insertBefore(wrap,single);
for(var j=0;j<pre.length&&j<8;j++)boxes[j].value=pre.charAt(j);
boxes[Math.min(pre.length,7)].focus();
})();
"""


def _machines_page(machines, confirm: str = "", note: str = "") -> str:
    """Every connected machine: its name (his to change), when it connected,
    when it was last used, which sessions have published with it, and how many
    pages it has -- and revoke, which asks once more before it acts
    (homelab-ij8.103)."""
    parts = [_CONNECT_CSS, '<div class="cx">']
    if note:
        parts.append(f'<section><p class="ok">{escape(note)}</p></section>')
    working = [m for m in machines if m.revoked_at is None]
    gone = [m for m in machines if m.revoked_at is not None]
    parts.append(
        "<section><h2>Connected machines</h2>"
        f"<p>{len(working)} with a working key. "
        '<a href="/connect">Connect another</a>.</p></section>'
    )
    for m in working:
        ident = escape(m.id)
        used = _ago(m.last_used_at) if m.last_used_at else ""
        facts = [
            (
                f"<dt>Key</dt><dd>renews itself weekly; this one issued "
                f"{escape(_ago(m.created_at) or 'just now')}</dd>"
                if m.rotating
                else f"<dt>Connected</dt><dd>{escape(_ago(m.created_at) or 'just now')}</dd>"
            ),
            f"<dt>Last used</dt><dd>{escape(used or 'not since it connected')}</dd>",
            f"<dt>Pages</dt><dd>{m.pages}</dd>",
            "<dt>Sessions</dt><dd>"
            + (escape(", ".join(m.sessions)) if m.sessions else "none labelled yet")
            + "</dd>",
        ]
        if m.label and m.label != m.agent:
            facts.insert(0, f"<dt>Files pages as</dt><dd>{escape(m.agent)}</dd>")
        if confirm == m.id:
            action = (
                f'<p class="err">Revoke {escape(m.name)}? Its key stops working at once, '
                "for every agent on that machine. Its pages stay. It can connect again "
                "later with a new code.</p>"
                f'<form method="post" action="/machines/{ident}/revoke" class="row">'
                '<button class="btn primary" type="submit">Revoke</button>'
                '<a class="btn" href="/machines">Cancel</a></form>'
            )
        else:
            action = (
                f'<form method="post" action="/machines/{ident}/rename" class="row">'
                f'<input name="label" value="{escape(m.name)}" aria-label="Name" autocomplete="off">'
                '<button class="btn" type="submit">Rename</button>'
                f'<a class="btn" href="/machines?revoke={ident}">Revoke</a></form>'
            )
        parts.append(
            f"<section><h2>{escape(m.name)}</h2><dl>{''.join(facts)}</dl>{action}</section>"
        )
    if gone:
        rows = "".join(
            f"<li>{escape(m.name)} &middot; revoked {escape(_ago(m.revoked_at) or '')}</li>"
            for m in gone
        )
        parts.append(
            f"<section><details><summary>Revoked ({len(gone)})</summary><ul>{rows}</ul>"
            "</details></section>"
        )
    parts.append("</div>")
    return "".join(parts)


def _people_section(artifact_id: str, people: list, mail_on: bool) -> str:
    """Sharing with one person by email (homelab-ij8.16, his (1a)): their own
    link, confirmed on each new device by a code sent to their inbox; he sees
    who has access and withdraws each person separately."""
    ident = escape(artifact_id)
    rows = "".join(
        f"<li>{escape(p.email)} &middot; shared {escape(_ago(p.created_at) or 'just now')}"
        + (
            f" &middot; last opened {escape(_ago(p.last_opened_at))}"
            if p.last_opened_at
            else " &middot; not opened yet"
        )
        + f'<form method="post" action="/a/{ident}/share/email/{escape(p.id)}/withdraw" '
        'style="display:inline"> <button class="btn" type="submit">Withdraw</button></form></li>'
        for p in people
    )
    listing = f"<ul>{rows}</ul>" if people else "<p>Nobody yet.</p>"
    if not mail_on:
        form = '<p class="err">Email isn\'t set up on this service yet, so nobody can be added.</p>'
    else:
        form = (
            f'<form method="post" action="/a/{ident}/share/email" class="row">'
            '<input name="email" type="email" placeholder="name@example.com" '
            'aria-label="Email address" required autocomplete="email">'
            '<button class="btn primary" type="submit">Send them a link</button></form>'
        )
    return (
        "<section><h2>Share with a person</h2>"
        "<p>They get their own link by email. The first time they open it on a "
        "device, they confirm with a code sent to the same address, so forwarding "
        "the email doesn't let anyone else in.</p>"
        f"{form}<h2>Who has access</h2>{listing}</section>"
    )


def _share_page(
    artifact_id: str,
    title: str,
    token: str | None,
    note: str = "",
    people: list | None = None,
    mail_on: bool = False,
) -> str:
    """Sharing one page with people outside his space (homelab-ij8.101). The
    link shows the latest version, just the page; he can stop it at any time.
    The link sits in a `pre.prompt` so /connect.js gives it the same Copy
    button as the starter prompt."""
    ident = escape(artifact_id)
    parts = [_CONNECT_CSS, '<div class="cx">']
    if note:
        parts.append(f'<section><p class="ok">{escape(note)}</p></section>')
    if token:
        parts.append(
            f"<section><h2>{escape(title)} is shared</h2>"
            "<p>Anyone with this link can read the page, always its latest version. "
            "They see just the page: not your questions, not your answers, not the "
            "gallery.</p>"
            f'<pre class="prompt">{escape(store.share_url(artifact_id, token))}</pre>'
            f'<form method="post" action="/a/{ident}/share/stop" class="row">'
            '<button class="btn" type="submit">Stop sharing</button>'
            f'<a class="btn" href="/a/{ident}">Back to the page</a></form>'
            "<p>Stopping kills this link at once. Sharing again makes a new one.</p>"
            "</section>"
        )
    else:
        parts.append(
            f"<section><h2>Share {escape(title)}</h2>"
            "<p>Create a link anyone can open without logging in. It shows the "
            "latest version of this page, and nothing else from your space. You can "
            "stop it at any time.</p>"
            f'<form method="post" action="/a/{ident}/share" class="row">'
            '<button class="btn primary" type="submit">Create link</button>'
            f'<a class="btn" href="/a/{ident}">Cancel</a></form></section>'
        )
    parts.append(_people_section(artifact_id, people or [], mail_on))
    parts.append("</div>")
    return "".join(parts)


# SELECT TEXT, THEN COMMENT ON IT (homelab-ij8.18). The page reports a
# selection (content._viewer_report); this puts a Comment button under it and,
# pressed, opens the comments drawer with the quote shown and the anchor ready
# to save with the comment. Its own file, not gallery.js, which is at its size
# budget. Only its own frame, on its own origin, is listened to, and nothing
# the page sends is ever put into the document as markup: the quote goes in via
# textContent, the anchor into a hidden field the service validates.
#
# MARKS ON THE PAGE (page review step 1, homelab-ij8.108). The page embeds its
# open marks (comments.marks_for_page: id, kind, quote, anchor -- never a note)
# as JSON; once the frame reports its state they are posted into it, once per
# load of the frame and only to its own origin, and the shim paints them. A new
# document in the frame is sent them on its `load` too, without waiting for a
# state report it may already have sent; repainting is idempotent. Its
# reply names the marks it could not place, and that becomes one line in the
# comments drawer -- counted against this page's OWN mark ids, each once, so a
# page cannot make the line say anything but a true count of Jeremy's marks.
_COMMENTS_JS = """(function(){
"use strict";
var f=document.getElementById("artifact-frame");if(!f)return;
var o;try{o=new URL(f.src).origin}catch(e){return}
var mk=document.getElementById("artifact-marks"),marks=[],known=Object.create(null),sent=false;
try{marks=JSON.parse(mk?mk.textContent:"[]");if(!Array.isArray(marks))marks=[]}catch(e){marks=[]}
marks.forEach(function(m){if(m&&typeof m.id==="string")known[m.id]=1});
function sendMarks(){if(sent||!marks.length)return;sent=true;
  try{f.contentWindow.postMessage({type:"artifact-marks",marks:marks},o)}catch(e){}}
function unplacedLine(ids,overflow){var p=document.getElementById("marks-unplaced");if(!p)return;
  var n=0,seen=Object.create(null);
  ids.slice(0,200).forEach(function(id){if(typeof id==="string"&&known[id]&&!seen[id]){seen[id]=1;n++}});
  // The shim itself caps how many unplaced ids it names and folds the rest
  // into `overflow` (homelab-ij8.111, follow-up Item 2), so the true count
  // this line says is the ids actually given PLUS that number -- never just
  // the ids, or a page with many unplaced marks would undercount its own
  // line. `overflow` is untrusted the same way `ids` already is: only a
  // finite non-negative integer is ever added, and even a genuine one from a
  // huge page is capped, so a forged message cannot make this line print an
  // arbitrary number.
  if(typeof overflow==="number"&&isFinite(overflow)&&overflow>0&&overflow===Math.floor(overflow))
    n+=Math.min(overflow,100000);
  p.textContent=n?n+(n===1?" mark":" marks")+" could not be placed on this version of the page.":"";p.hidden=!n}
if(f.addEventListener)f.addEventListener("load",function(){sent=false;sendMarks()});
window.addEventListener("message",function(e){try{
  if(e.source!==f.contentWindow||e.origin!==o)return;var d=e.data;
  if(!d||typeof d!=="object")return;
  if(d.type==="artifact-state")sendMarks();
  else if(d.type==="artifact-marks-placed"&&Array.isArray(d.unplaced)){unplacedLine(d.unplaced,d.overflow);badges(d.rects)}
}catch(x){}});
// THE DOCKED PANEL. Opened and closed by a class on <body>, never by the
// fragment: no navigation, no history entry, nothing laid over the page. The
// bar's button and the close control are intercepted; Escape closes; the
// state is this viewer's and this page's, in localStorage under a key per
// page id (open on one page is not open on the next), and storage that
// throws or is missing costs only the memory. `.cjs` hands the panel from :target to this.
// A link to #comments (the scope tabs, a scriptless redirect) opens it for
// this visit only -- it is not written to the memory -- and the fragment is
// replaced away so a reload obeys the memory rather than the old link.
var P=document.getElementById("comments"),B=document.body,bar=null,
  seg=String(location.pathname||"").split("/"),K="artifacts.comments.open:"+(seg[1]==="a"&&seg[2]?seg[2]:seg.join("/"));
function isOpen(){return !!(B&&B.classList.contains("cpanel-open"))}
function panel(on,keep){if(!P)return;B.classList.toggle("cpanel-open",on);
  var c=document.querySelector(".cbtn");if(c)c.setAttribute("aria-expanded",on?"true":"false");
  // The frame reflows to its new width and reports its height on `resize`;
  // a selection bar placed against the old layout would point at nothing.
  if(bar)bar.hidden=true;if(!busy())hide();
  if(!keep)try{window.localStorage.setItem(K,on?"1":"0")}catch(x){}}
if(P&&B&&B.classList){
  B.classList.add("cjs");
  var x0=P.querySelector(".cclose");
  if(x0&&x0.parentNode){var xb=document.createElement("button");xb.type="button";xb.className="cclose";
    xb.setAttribute("aria-label","Close comments");xb.textContent=x0.textContent;x0.parentNode.replaceChild(xb,x0)}
  var st=null;try{st=window.localStorage.getItem(K)}catch(x){}
  var linked=location.hash==="#comments";
  if(linked)try{window.history.replaceState(null,"",location.pathname+location.search)}catch(x){}
  panel(linked||st==="1",true);
  document.addEventListener("click",function(e){
    var t=e.target&&e.target.closest?e.target.closest(".cbtn,.cclose"):null;
    if(t){e.preventDefault();var shut=t.className==="cclose";panel(!shut&&!isOpen());
      // The close button is inside what just hid: keyboard focus would fall
      // to <body>, so it goes back to the control that opens the panel.
      if(shut){var c=document.querySelector(".cbtn");if(c&&c.focus)c.focus()}return}
    // A #comments link to THIS page (the scope tab already on, this page's
    // group in all-pages) would push a history entry for nothing: open instead.
    var a=e.target&&e.target.closest?e.target.closest('a[href$="#comments"]'):null;
    if(a&&a.pathname===location.pathname&&a.search===location.search){e.preventDefault();panel(true)}});
  // Capture, so this runs before gallery.js: an open dialog or menu is what
  // Escape closes first, and the panel only when nothing else is open.
  window.addEventListener("keydown",function(e){
    if(e.key==="Escape"&&pinOn){pinMode(false);if(e.stopPropagation)e.stopPropagation();return}
    if(e.key==="Escape"&&hc){hide();if(e.stopPropagation)e.stopPropagation();return}
    if(e.key!=="Escape"||!isOpen()||document.querySelector(".dlg:target,details.menu[open]"))return;
    var back=P.contains(document.activeElement);panel(false);
    if(back){var c=document.querySelector(".cbtn");if(c&&c.focus)c.focus()}},true)}
// HOVER CARDS. When the pointer rests 150 ms on marked text, the page says
// WHICH mark and WHERE -- `artifact-mark-hover` {id, rect}, nothing else -- and
// a card with that comment appears beside it. The card is drawn HERE, in
// Jeremy's origin, never in the agent's page, and it is a COPY of the panel's
// own thread (the server-escaped markup, found by `data-mark-id`): nothing the
// page sends is ever rendered. Only its own frame at its own origin is heard;
// an id that is not one of this page's marks is ignored; the rect must be four
// finite numbers and is clamped into the frame, and the card into the window.
// A flood cannot show a card that has not rested: a new id restarts the rest.
// It goes on `artifact-mark-leave` after a 250 ms grace (so the pointer can
// reach it), on leaving the card, on scroll, and when the panel moves the
// page. It reads Reply . Open; its reply box stays hidden until Reply. A reply
// being written (the box has the cursor, or text) is never thrown away by any
// of those -- on a phone, the keyboard coming up scrolls the window -- so the
// card then goes only on Escape (which is the card's alone), Open, a sent
// reply, or a press outside it. In full screen it lives inside the
// full-screen element, the one part of the page that is drawn.
var hc=null,hid=null,ht=0,hg=0,pend=null,pinOn=false;
function host(){return document.fullscreenElement||document.body}
function busy(){var i=hc&&hc.querySelector?hc.querySelector("input[name=body]"):null;
  return !!(i&&(document.activeElement===i||i.value))}
function thread(id){if(!P||!P.querySelectorAll)return null;var ls=P.querySelectorAll("li.thread");
  for(var i=0;i<ls.length;i++)if(ls[i].getAttribute("data-mark-id")===id)return ls[i];return null}
function fin(v){return typeof v==="number"&&isFinite(v)}
function hide(){clearTimeout(hg);hg=0;if(hc){drop(hc);hc=null;hid=null}}
function grace(){clearTimeout(hg);hg=setTimeout(function(){hg=0;if(!busy())hide()},250)}
function show(id,r){var li=thread(id);if(!li)return;hide();
  var c=document.createElement("div");c.className="hovercard";c.setAttribute("role","dialog");
  c.setAttribute("aria-label","Comment");c.setAttribute("data-mark-id",id);
  var k=li.querySelector(".ckind"),lab=document.createElement("span");
  lab.className=k?k.className:"ckind";lab.textContent=k?k.textContent:"Comment";c.appendChild(lab);
  var bu=li.querySelector(".bubbles");if(bu)c.appendChild(bu.cloneNode(true));
  var rf=li.querySelector("form.creply"),rc=rf?rf.cloneNode(true):null;if(rc){rc.hidden=true;c.appendChild(rc)}
  var row=document.createElement("div");row.className="hcact";c.appendChild(row);
  function btn(t,fn){var b=document.createElement("button");b.type="button";b.textContent=t;
    b.addEventListener("click",fn);row.appendChild(b)}
  if(rc)btn("Reply",function(){rc.hidden=false;var i=rc.querySelector("input[name=body]");if(i)i.focus()});
  btn("Open",function(){hide();panel(true);var t=thread(id);
    if(t&&t.scrollIntoView)t.scrollIntoView({block:"nearest"})});
  c.addEventListener("pointerenter",function(){clearTimeout(hg);hg=0});
  c.addEventListener("pointerleave",grace);
  host().appendChild(c);hc=c;hid=id;
  var fr=f.getBoundingClientRect(),FW=fin(fr.width)?fr.width:0,FH=fin(fr.height)?fr.height:0,
    W=window.innerWidth||0,H=window.innerHeight||0,cw=c.offsetWidth||0,ch=c.offsetHeight||0,
    l=Math.max(0,Math.min(r.left,FW)),t=Math.max(0,Math.min(r.top,FH)),
    h=Math.max(0,Math.min(r.height,200,FH-t)),x=fr.left+l,y=fr.top+t+h+6;
  if(y+ch>H-8)y=fr.top+t-ch-6;
  // Never past the frame's right edge: the docked panel is on the other side.
  c.style.left=Math.max(8,Math.min(x,Math.min(W,fr.left+FW)-cw-8))+"px";c.style.top=Math.max(8,Math.min(y,H-ch-8))+"px"}
window.addEventListener("message",function(e){try{
  if(e.source!==f.contentWindow||e.origin!==o)return;var d=e.data;
  if(!d||typeof d!=="object")return;
  if(d.type!=="artifact-mark-leave"&&d.type!=="artifact-mark-hover"||busy()||pinOn)return;
  if(d.type==="artifact-mark-leave"){clearTimeout(ht);ht=0;pend=null;if(hc)grace();return}
  if(typeof d.id!=="string"||!known[d.id])return;
  var q=d.rect;if(!q||typeof q!=="object"||!fin(q.top)||!fin(q.left)||!fin(q.width)||!fin(q.height))return;
  clearTimeout(hg);hg=0;q={top:q.top,left:q.left,width:q.width,height:q.height};
  if(hc&&hid===d.id)return;
  if(pend&&pend.id===d.id){pend.r=q;return}
  hide();pend={id:d.id,r:q};clearTimeout(ht);
  ht=setTimeout(function(){ht=0;var p=pend;pend=null;if(p)show(p.id,p.r)},150)
}catch(x){}});
window.addEventListener("scroll",function(){if(busy())return;clearTimeout(ht);ht=0;pend=null;hide()});
if(document.addEventListener){
  document.addEventListener("pointerdown",function(e){if(hc&&!(hc.contains&&hc.contains(e.target)))hide()});
  // The card and the selection bar move with full screen: outside the
  // full-screen element nothing is drawn. The bar's old place is wrong now.
  document.addEventListener("fullscreenchange",function(){
    if(hc){if(busy())host().appendChild(hc);else hide()}
    if(bar){bar.hidden=true;host().appendChild(bar)}})}
var form=document.querySelector("form.cnew");
// POSTING IN PLACE. A comment or reply is sent with fetch and the thread it
// comes back in is put where it belongs; the page, the open drawer and the
// scroll position are not touched. Delegated on the document because a reply
// form arrives with an in-place thread. ANY failure falls back to the plain
// form submit, so a comment is never lost, and the form is disabled while a
// post is in flight so a fast Enter plus a click cannot post twice.
// PICTURES ON A COMMENT. Up to four, pasted, dropped or chosen; a removable
// thumbnail for each; sent to POST /c/{id}/image one at a time when the comment
// is sent, and the comment then names them in repeated `image` fields. The
// thumbnails are canvases, not <img>: the gallery's img-src allows only itself
// and the content origin, so a local preview is drawn, never loaded. A picture
// over 4000 px a side, 16 megapixels or 5 MB is shrunk here when it can be; the
// server holds the same limits and says so if not. Everything the user or the
// server supplies goes in by textContent. With scripts off the form is the
// plain one: the control stays hidden (.cpics-on is set below).
var MAXN=4,MAXB=5*1024*1024,MAXF=25*1024*1024,MAXS=4000,MAXP=16000000,MAXD=300000000,held=typeof WeakMap==="function"?new WeakMap():null;
function pics(fm){var a=held&&held.get(fm);if(!a){a=[];if(held)held.set(fm,a)}return a}
function say(fm,t){var p=fm.querySelector?fm.querySelector(".cpics-say"):null;if(p)p.textContent=t||""}
function paint(fm){var s=fm.querySelector?fm.querySelector(".cpics"):null;if(!s)return;
  while(s.firstChild)s.removeChild(s.firstChild);
  pics(fm).forEach(function(p,i){
    var w=document.createElement("span");w.className="cpic";var t;
    if(p.bmp){t=document.createElement("canvas");t.width=56;t.height=56;
      var x=t.getContext?t.getContext("2d"):null;
      if(x){var k=Math.max(56/p.bmp.width,56/p.bmp.height);
        x.drawImage(p.bmp,(56-p.bmp.width*k)/2,(56-p.bmp.height*k)/2,p.bmp.width*k,p.bmp.height*k)}
      t.setAttribute("role","img");t.setAttribute("aria-label",p.file.name||"Picture")}
    else{t=document.createElement("span");t.className="cpicname";t.textContent=p.file.name||"Picture"}
    var b=document.createElement("button");b.type="button";b.className="cpicx";
    b.setAttribute("aria-label","Remove picture");b.textContent="\u00d7";
    b.addEventListener("click",function(){var l=pics(fm),j=l.indexOf(p);if(j>=0)l.splice(j,1);say(fm,"");paint(fm)});
    w.appendChild(t);w.appendChild(b);s.appendChild(w)})}
function add(fm,files){
  var l=pics(fm),over=false,odd=false,big=false;
  Array.prototype.forEach.call(files||[],function(f){
    if(!f||String(f.type||"").indexOf("image/")!==0){odd=true;return}
    if(l.length>=MAXN){over=true;return}
    if(f.size>MAXF){big=true;return}
    var p={file:f,bmp:null,id:null,ready:null};l.push(p);
    p.ready=decode(fm,p)});
  say(fm,big?"A picture over 25 MB is too large to attach (5 MB at most).":over?"Four pictures at most on one comment.":odd?"Only pictures can be attached.":"");paint(fm)}
// The size a picture declares, read from its first megabyte (PNG, JPEG, WebP); null when
// it cannot be told. A file is decoded only once this is known, and then at no more than
// the limits: a 30000 x 30000 PNG is never decoded at full size.
async function dims(f){
  var u=new Uint8Array(await f.slice(0,1048576).arrayBuffer()),v=new DataView(u.buffer),n=u.length;
  if(n>24&&u[0]===0x89&&u[1]===0x50)return[v.getUint32(16),v.getUint32(20)];
  if(n>3&&u[0]===0xFF&&u[1]===0xD8){var i=2;
    while(i+9<n){if(u[i]!==0xFF){i++;continue}
      var m=u[i+1];if(m===0xFF){i++;continue}
      if(m>=0xC0&&m<=0xCF&&m!==0xC4&&m!==0xC8&&m!==0xCC)return[v.getUint16(i+7),v.getUint16(i+5)];
      if(m===0xD8||m===0x01||(m>=0xD0&&m<=0xD7)){i+=2;continue}
      i+=2+v.getUint16(i+2)}
    return null}
  if(n>30&&u[0]===0x52&&u[8]===0x57){var k=String.fromCharCode(u[12],u[13],u[14],u[15]);
    if(k==="VP8X")return[1+(u[24]|u[25]<<8|u[26]<<16),1+(u[27]|u[28]<<8|u[29]<<16)];
    if(k==="VP8L")return[1+(((u[22]&63)<<8)|u[21]),1+(((u[24]&15)<<10)|(u[23]<<2)|((u[22]&192)>>6))];
    if(k==="VP8 ")return[v.getUint16(26,true)&16383,v.getUint16(28,true)&16383]}
  return null}
function decode(fm,p){
  if(typeof createImageBitmap!=="function")return Promise.resolve();
  return (async function(){
    var d=await dims(p.file);if(!d||!d[0]||!d[1])return;
    if(d[0]*d[1]>MAXD){var l=pics(fm),j=l.indexOf(p);if(j>=0)l.splice(j,1);
      say(fm,"That picture is too large to attach.");paint(fm);return}
    var s=Math.min(1,MAXS/Math.max(d[0],d[1]),Math.sqrt(MAXP/(d[0]*d[1])));
    p.scaled=s<1;
    p.bmp=await createImageBitmap(p.file,s<1?{resizeWidth:Math.max(1,Math.floor(d[0]*s)),resizeHeight:Math.max(1,Math.floor(d[1]*s)),resizeQuality:"high"}:undefined)
  })().catch(function(){}).then(function(){paint(fm)})}
function clearPics(fm){pics(fm).length=0;say(fm,"");paint(fm)}
function enc(c,type,q){return new Promise(function(ok){c.toBlob(ok,type,q)})}
async function shrink(p){
  var f=p.file;if(p.ready)await p.ready;
  var b=p.bmp;if(!b)return f;
  var s=Math.min(1,MAXS/Math.max(b.width,b.height),Math.sqrt(MAXP/(b.width*b.height)));
  if(s>=1&&f.size<=MAXB&&!p.scaled)return f;
  var png=f.type==="image/png";
  for(var k=0;k<5;k++){
    var c=document.createElement("canvas");c.width=Math.max(1,Math.floor(b.width*s));c.height=Math.max(1,Math.floor(b.height*s));
    var x=c.getContext("2d");if(!x)return f;
    if(!png||k>0){x.fillStyle="#fff";x.fillRect(0,0,c.width,c.height)}
    x.drawImage(b,0,0,c.width,c.height);
    var out=await enc(c,png&&k===0?"image/png":"image/jpeg",0.88);
    if(out&&out.size<=MAXB)return out;
    s*=0.75}
  return f}
async function refusal(r){var m="";
  try{var e=await r.json(),d=e&&e.detail;m=typeof d==="string"?d:(d&&typeof d.detail==="string"?d.detail:"")}catch(x){}
  return m||"The picture was not accepted."}
async function upload(fm,list){
  var ids=[];
  for(var i=0;i<list.length;i++){var p=list[i];
    if(!p.id){var d=new FormData();d.append("file",await shrink(p),"picture");
      var r=await fetch(fm.action+"/image",{method:"POST",body:d,headers:{"Accept":"application/json"},credentials:"same-origin"});
      if(r.status!==201)throw new Error(await refusal(r));
      var j=await r.json();if(!j||typeof j.image_id!=="string")throw new Error("The picture was not accepted.");
      p.id=j.image_id}
    ids.push(p.id)}
  return ids}
try{if(document.documentElement&&document.documentElement.classList&&typeof Promise==="function"&&typeof FormData==="function"&&typeof fetch==="function")
  document.documentElement.classList.add("cpics-on")}catch(x){}
function formOf(n){return n&&n.closest?n.closest("form.cnew,form.creply"):null}
if(document.addEventListener){
  document.addEventListener("click",function(e){
    var t=e.target;if(!t||!t.closest)return;
    var bt=t.closest(".cattachbtn");
    if(bt){var fm=formOf(bt),inp=fm&&fm.querySelector("input.cfile");if(inp&&inp.click)inp.click();return}
    var im=t.closest(".cimg");
    if(im&&im.classList){e.preventDefault();var on=!im.classList.contains("big");
      im.classList.toggle("big",on);var th=im.querySelector?im.querySelector(".cthumb"):null;if(th&&th.classList)th.classList.toggle("big",on)}});
  document.addEventListener("change",function(e){
    var t=e.target;if(!t||!t.closest||!t.closest("input.cfile"))return;
    var fm=formOf(t);if(fm)add(fm,t.files);t.value=""});
  document.addEventListener("paste",function(e){
    var fm=formOf(e.target),cd=e.clipboardData;if(!fm||!cd||!cd.files||!cd.files.length)return;
    e.preventDefault();add(fm,cd.files)});
  document.addEventListener("dragover",function(e){
    var fm=formOf(e.target);if(fm&&e.dataTransfer&&e.dataTransfer.types&&Array.prototype.indexOf.call(e.dataTransfer.types,"Files")>=0)e.preventDefault()});
  document.addEventListener("drop",function(e){
    var fm=formOf(e.target);if(!fm||!e.dataTransfer||!e.dataTransfer.files||!e.dataTransfer.files.length)return;
    e.preventDefault();add(fm,e.dataTransfer.files)})}
function controls(fm){return fm.querySelectorAll?fm.querySelectorAll("button,input[type=submit]"):[]}
function setBusy(fm,on){fm.dataset.busy=on?"1":"";
  Array.prototype.forEach.call(controls(fm),function(c){c.disabled=on})}
function drop(x){if(x&&x.parentNode&&x.parentNode.removeChild)x.parentNode.removeChild(x)}
function place(fm,html,root){
  var t=document.createElement("template");t.innerHTML=html;
  var li=t.content&&t.content.firstElementChild;if(!li)throw new Error("no thread");
  var old=fm.closest?fm.closest("li.thread"):null,hx=fm.closest?fm.closest(".hovercard"):null;
  if(hx){old=thread(hx.getAttribute("data-mark-id"));hide();if(!old)throw new Error("gone")}
  if(old&&old.parentNode){old.parentNode.replaceChild(li,old);return false}
  var list=document.querySelector("#comments > ul.threads");
  if(!list){list=document.createElement("ul");list.className="threads";
    form.parentNode.insertBefore(list,form)}
  drop(document.querySelector("#comments .cempty"));
  list.appendChild(li);return true}
function bump(){var c=document.querySelector(".cbtn .cn");
  if(c){var n=parseInt(c.textContent,10);c.textContent=String((isFinite(n)?n:0)+1)}}
function cleared(fm){
  var b=fm.querySelector("input[name=body]");if(b)b.value="";clearPics(fm);
  if(fm===form){["quote","anchor","kind"].forEach(function(n){drop(fm.querySelector("input[name="+n+"]"))});
    drop(fm.querySelector(".cquote"));if(b)b.placeholder="Comment on this page"}}
// A reply from a hover card that has since gone is sent through the panel's
// own copy of that thread, text and all: a detached form cannot submit, and a
// comment is never silently lost.
function fail(fm){setBusy(fm,false);
  var hx=fm.closest?fm.closest(".hovercard"):null;
  if(hx&&hx!==hc){var t=thread(hx.getAttribute("data-mark-id")),pf=t&&t.querySelector("form.creply");
    if(pf){var a=fm.querySelector("input[name=body]"),b=pf.querySelector("input[name=body]");
      if(a&&b)b.value=a.value;pf.submit();return}
    fm.hidden=true;document.body.appendChild(fm)}
  fm.submit()}
if(document.addEventListener)document.addEventListener("submit",function(e){
  var fm=e.target;
  if(!fm||!/(^|\\s)(cnew|creply)(\\s|$)/.test(fm.className||"")||typeof fetch!=="function"||typeof FormData!=="function")return;
  e.preventDefault();if(fm.dataset&&fm.dataset.busy)return;
  var mine=pics(fm).slice(),saved=false;setBusy(fm,true);
  function go(ids){
    var body=new FormData(fm);ids.forEach(function(i){body.append("image",i)});
    fetch(fm.action,{method:"POST",body:body,headers:{"Accept":"application/json"},credentials:"same-origin"})
    .then(function(r){
      if(r.status===201){saved=true;return r.json()}
      // With pictures, a refusal is told to the user: the plain submit below
      // would send the words without them.
      if(!ids.length)throw new Error("status "+r.status);
      return refusal(r).then(function(m){var x=new Error(m);x.say=m;throw x})})
    .then(function(d){
      if(d&&d.reload){location.reload();return}
      if(!d||typeof d.html!=="string"||!Array.isArray(d.marks))throw new Error("shape");
      var isRoot=place(fm,d.html,d.thread_root);
      if(isRoot)bump();
      // In the all-pages view a reply may belong to another page: the frame's
      // marks are this page's, so they are only replaced where this page is.
      if(form){marks=d.marks;known=Object.create(null);
        marks.forEach(function(m){if(m&&typeof m.id==="string")known[m.id]=1});
        sent=false}
      cleared(fm);setBusy(fm,false);sendMarks()})
    .catch(function(err){
      // Once the server has said 201 the comment IS saved: never post it again.
      if(saved){location.reload();return}
      if(ids.length){mine.forEach(function(p){p.id=null});
        say(fm,(err&&err.say)||"The comment was not sent. Your pictures are still here.");setBusy(fm,false);return}
      fail(fm)})}
  if(!mine.length){go([]);return}
  upload(fm,mine).then(go,function(err){say(fm,(err&&err.message)||"The picture was not sent.");setBusy(fm,false)})});
if(!form)return;
bar=document.createElement("div");bar.className="selbar";bar.hidden=true;
[["comment","Comment"],["remove","Remove this"],["looks_good","Looks good"]].forEach(function(k){
  var b=document.createElement("button");b.type="button";b.textContent=k[1];b.dataset.kind=k[0];
  bar.appendChild(b)});
host().appendChild(bar);
var last=null;
function num(v){return typeof v==="number"&&isFinite(v)}
function field(n,v){var x=form.querySelector("input[name="+n+"]");
  if(!x){x=document.createElement("input");x.type="hidden";x.name=n;form.appendChild(x)}x.value=v}
window.addEventListener("message",function(e){
  if(e.source!==f.contentWindow||e.origin!==o)return;var d=e.data;
  if(!d||typeof d!=="object"||d.type!=="artifact-selection"||pinOn)return;
  if(typeof d.quote!=="string"||!d.quote||!d.anchor||!d.rect||!num(d.rect.top)||!num(d.rect.left)||!num(d.rect.height)){
    bar.hidden=true;last=null;return}
  last=d;var r=f.getBoundingClientRect();
  // In full screen the frame scrolls itself, so the page's own offsets are no
  // use: the bar is fixed under the selection as the frame shows it (vt/vl).
  if(document.fullscreenElement&&num(d.rect.vt)&&num(d.rect.vl)){bar.style.position="fixed";
    bar.style.top=(r.top+Math.min(Math.max(0,d.rect.vt+d.rect.height),r.height)+6)+"px";bar.style.left=(r.left+Math.min(Math.max(0,d.rect.vl),r.width))+"px"}
  else{bar.style.position="";
    bar.style.top=(r.top+window.scrollY+d.rect.top+d.rect.height+6)+"px";
    bar.style.left=(r.left+window.scrollX+Math.max(0,d.rect.left))+"px"}
  bar.hidden=false});
bar.addEventListener("click",function(e){
  var b=e.target&&e.target.closest?e.target.closest("button"):null;
  if(!b||!last)return;
  field("quote",last.quote.slice(0,500));field("anchor",JSON.stringify(last.anchor));
  field("kind",b.dataset.kind);
  var q=form.querySelector(".cquote");
  if(!q){q=document.createElement("blockquote");q.className="cquote";form.insertBefore(q,form.firstChild)}
  q.textContent=last.quote.slice(0,500);panel(true);
  var i=form.querySelector("input[name=body]");
  if(b.dataset.kind==="comment"){
    if(i){i.placeholder="Comment on the selected text";i.focus()}
  }else{
    if(i){i.placeholder="Add a note (optional)";if(!i.value){form.requestSubmit()}}
  }});
// ELEMENT PINS (pinned comments, slice 2). The Pin button and the C key turn
// pin mode on in the frame; while it is on the frame reports the element under
// the pointer (`artifact-pin-hover`, an outline drawn HERE over the frame) and
// the one clicked (`artifact-pin`), which fills the composer exactly as a text
// selection does. Pins already made get a numbered badge at the element's
// corner, from the rectangles the frame reports. Nothing the page sends is
// markup: it arrives as text and numbers, is checked, and is drawn with
// textContent and style.
// byHand: pin mode was turned on here, by the Pin button or a key on the
// gallery. Only then does a pick move focus into the comment box: the page in
// the frame can send any message, so a pick it reports is shown, never typed into.
var pb=document.querySelector(".pinmodebtn"),po=null,pbs=[],prects=Object.create(null),byHand=false;
function pinMode(on,hand){on=!!on;if(on===pinOn)return;pinOn=on;byHand=on&&!!hand;
  try{f.contentWindow.postMessage({type:"artifact-pin-mode",on:on},o)}catch(e){}
  if(pb){pb.setAttribute("aria-pressed",on?"true":"false");pb.classList.toggle("on",on)}
  if(on){bar.hidden=true;last=null;hide()}else outline(null)}
function outline(r){
  if(!r){if(po)po.hidden=true;return}
  if(!po){po=document.createElement("div");po.className="pinout";po.hidden=true;host().appendChild(po)}
  var fr=f.getBoundingClientRect();
  if(document.fullscreenElement&&num(r.vt)&&num(r.vl)){po.style.position="fixed";
    po.style.top=(fr.top+r.vt)+"px";po.style.left=(fr.left+r.vl)+"px"}
  else{po.style.position="";po.style.top=(fr.top+window.scrollY+r.top)+"px";po.style.left=(fr.left+window.scrollX+r.left)+"px"}
  po.style.width=Math.max(0,r.width)+"px";po.style.height=Math.max(0,r.height)+"px";po.hidden=false}
function rectOk(r){return !!r&&typeof r==="object"&&num(r.top)&&num(r.left)&&num(r.width)&&num(r.height)}
function takePin(d){
  var a=d.anchor,hand=byHand;
  if(!a||typeof a!=="object"||a.kind!=="element"||typeof d.quote!=="string"||!rectOk(d.rect))return;
  var js;try{js=JSON.stringify(a)}catch(e){return}
  if(js.length>4000)return;
  var q=d.quote.slice(0,200);
  pinMode(false);bar.hidden=true;last=null;
  field("quote",q);field("anchor",js);field("kind","comment");
  var bq=form.querySelector(".cquote");
  if(!bq){bq=document.createElement("blockquote");bq.className="cquote";form.insertBefore(bq,form.firstChild)}
  bq.textContent=q||(typeof a.tag==="string"&&a.tag?a.tag:"element");
  panel(true);
  var i=form.querySelector("input[name=body]");
  if(i){i.placeholder="Comment on the pinned spot";if(hand)i.focus()}}
// Badges: the numbers are the element pins' order among this page's marks, the
// same number written into each thread so the two can be matched by eye.
function badges(rs){
  if(!bar)return;
  pbs.forEach(drop);pbs=[];prects=Object.create(null);
  if(!Array.isArray(rs)||document.fullscreenElement)return;
  var n=0,nums=Object.create(null),fr=f.getBoundingClientRect();
  marks.forEach(function(m){if(m&&typeof m.id==="string"&&m.anchor&&m.anchor.kind==="element")nums[m.id]=++n});
  rs.slice(0,200).forEach(function(r){
    if(!rectOk(r)||typeof r.id!=="string"||!nums[r.id])return;
    prects[r.id]=r;
    var b=document.createElement("button");b.type="button";b.className="pinbadge";
    b.textContent=String(nums[r.id]);b.setAttribute("aria-label","Pinned comment "+nums[r.id]);
    b.style.top=(fr.top+window.scrollY+r.top)+"px";b.style.left=(fr.left+window.scrollX+r.left)+"px";
    b.addEventListener("click",function(){panel(true);var t=thread(r.id);
      if(t&&t.scrollIntoView)t.scrollIntoView({block:"nearest"})});
    document.body.appendChild(b);pbs.push(b);
    var t=thread(r.id);
    if(t){var no=t.querySelector(".cpinno");
      if(!no){no=document.createElement("span");no.className="cpinno";t.insertBefore(no,t.firstChild)}
      no.textContent=String(nums[r.id])}})}
// A thread hovered or focused outlines its spot on the page.
function spot(e,on){try{var li=e.target&&e.target.closest?e.target.closest(".thread"):null,
  id=li?li.getAttribute("data-mark-id"):null;outline(on&&id&&prects[id]?prects[id]:null)}catch(x){}}
if(P&&P.addEventListener){
  P.addEventListener("mouseover",function(e){spot(e,true)});P.addEventListener("focusin",function(e){spot(e,true)});
  P.addEventListener("mouseout",function(e){spot(e,false)});P.addEventListener("focusout",function(e){spot(e,false)})}
if(pb){pb.hidden=false;pb.addEventListener("click",function(){pinMode(!pinOn,true)})}
// C, as in Figma and Vercel's comments: never while typing, never with a modifier.
document.addEventListener("keydown",function(e){try{
  if(!e||(e.key!=="c"&&e.key!=="C")||e.ctrlKey||e.metaKey||e.altKey)return;
  var t=e.target,nm=t&&t.nodeName?String(t.nodeName).toUpperCase():"";
  if(nm==="INPUT"||nm==="TEXTAREA"||nm==="SELECT"||(t&&t.isContentEditable))return;
  pinMode(!pinOn,true)}catch(x){}});
document.addEventListener("fullscreenchange",function(){drop(po);po=null;pbs.forEach(drop);pbs=[]});
window.addEventListener("message",function(e){try{
  if(e.source!==f.contentWindow||e.origin!==o)return;var d=e.data;
  if(!d||typeof d!=="object")return;
  if(d.type==="artifact-pin-toggle")pinMode(!pinOn);
  else if(d.type==="artifact-pin-exit")pinMode(false);
  else if(d.type==="artifact-pin-rects")badges(d.rects);
  else if(d.type==="artifact-pin-hover"){if(pinOn)outline(rectOk(d.rect)?d.rect:null)}
  else if(d.type==="artifact-pin"&&pinOn)takePin(d)
}catch(x){}});
})();
// Full screen: the sheet, not the page, so the bar's own chrome drops away and
// the framed page has the whole window. Shown only where the API exists.
(function(){
"use strict";
var b=document.querySelector(".fsbtn"),s=document.getElementById("artifact-sheet");
if(!b||!s||!document.fullscreenEnabled||!s.requestFullscreen)return;
b.hidden=false;
function paint(){var on=document.fullscreenElement===s;
  var t=b.getAttribute(on?"data-on":"data-off");
  b.setAttribute("aria-label",t);b.title=t;b.setAttribute("aria-pressed",on?"true":"false")}
b.addEventListener("click",function(){
  try{if(document.fullscreenElement)document.exitFullscreen();else s.requestFullscreen()}catch(e){}});
document.addEventListener("fullscreenchange",paint);paint();
})();
"""


def starter_prompt() -> str:
    """What he pastes into a new agent. It carries NO secret -- only where to
    read and what to tell him -- so it is safe in any chat and any transcript
    (homelab-ij8.102)."""
    return (
        "Connect this machine to my artifacts space, so you can publish pages "
        "for me and ask me questions on them. Read "
        f"{gallery_origin().rstrip('/')}/api/v1/agents.txt and follow it. When it "
        "gives you a code, tell me the code and wait while I approve it. Never "
        "show me or anyone the key it saves."
    )


def _connect_page(
    typed: str = "", pending=None, message: str = "", ok: str = "", name: str = ""
) -> str:
    """The page he approves a machine on. Step one asks for the code his agent
    is showing; step two shows that one request, lets him name the machine,
    and approves or denies it. He TYPES the code rather than picking from a
    list: anyone can create a request, so only the code in front of him is
    trustworthy."""
    parts = [_CONNECT_CSS, '<div class="cx">']
    if ok:
        parts.append(f'<section><p class="ok">{escape(ok)}</p></section>')
    if pending is None:
        parts.append(
            "<section><h2>Connect a machine</h2>"
            "<p>Type the code your agent is showing you.</p>"
            + (f'<p class="err">{escape(message)}</p>' if message else "")
            + '<form class="code-form" method="get" action="/connect">'
            f'<input class="code" name="code" value="{escape(typed)}" autocomplete="off"'
            ' autocapitalize="characters" placeholder="ABCD-EFGH" aria-label="Code" autofocus>'
            '<button class="btn primary" type="submit">Continue</button></form></section>'
            "<section><h2>Starting a new agent?</h2>"
            "<p>Paste this into it. It holds no key, so it is safe in any chat.</p>"
            f'<pre class="prompt">{escape(starter_prompt())}</pre></section>'
        )
    else:
        suggested = name or pending.suggested_name or ""
        parts.append(
            f"<section><h2>Approve this machine?</h2>"
            f'<p class="code">{escape(pending.code)}</p>'
            "<dl>"
            f"<dt>It calls itself</dt><dd>{escape(pending.suggested_name or 'nothing')}</dd>"
            f"<dt>Asked from</dt><dd>{escape(pending.from_address)}</dd>"
            f"<dt>Asked at</dt><dd>{escape(_when(str(pending.created_at)))}</dd>"
            "</dl>"
            "<p>Only approve if this is the code your own agent showed you. The machine "
            "gets its key directly; you will not see it.</p>"
            + (f'<p class="err">{escape(message)}</p>' if message else "")
            + '<form method="post" action="/connect/approve">'
            f'<input type="hidden" name="request_id" value="{escape(pending.id)}">'
            f'<input type="hidden" name="code" value="{escape(pending.code)}">'
            "<label>Name this machine"
            f'<input name="name" value="{escape(suggested)}" autocomplete="off"'
            ' placeholder="e.g. laptop" required></label>'
            '<div class="row"><button class="btn primary" type="submit">Approve</button>'
            f'<button class="btn" type="submit" formaction="/connect/deny">Deny</button>'
            '<a class="btn" href="/connect">Cancel</a></div></form></section>'
        )
    parts.append("</div>")
    return "".join(parts)


def _page_menu(
    artifact_id: str,
    title: str,
    agent: str,
    archived: bool,
    updated: str,
    agents_can_read: bool = True,
    shared: bool = False,
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
            # Who else can read it, said where he looks for what a page is
            # (homelab-ij8.105).
            + (
                "<span>All your agents can read it</span>"
                if agents_can_read
                else f"<span>Only you and {escape(agent)} can read it</span>"
            )
            + ("<span>Shared by link</span>" if shared else "")
            + "</div>"
        ),
        (f'<a class="mrow" href="#rename">{_icon("rename")}Rename</a>'),
    ]
    if archived:
        rows.append(
            f'<form method="post" action="/a/{ident}/archive">'
            '<input type="hidden" name="archived" value="0">'
            f'<button class="mrow" type="submit">{_icon("unarchive")}'
            "Unarchive</button></form>"
        )
    else:
        rows.append(
            f'<form method="post" action="/a/{ident}/archive">'
            '<input type="hidden" name="archived" value="1">'
            f'<button class="mrow" type="submit">{_icon("archive")}'
            "Archive</button></form>"
        )
    rows.append(
        f'<a class="mrow" href="/a/{ident}/move">{_icon("copy")}Move to another agent</a>'
    )
    rows.append(
        f'<a class="mrow" href="/a/{ident}/share">{_icon("open")}'
        + ("Shared by link" if shared else "Share a link")
        + "</a>"
    )
    rows.append(
        f'<form method="post" action="/a/{ident}/agents">'
        f'<input type="hidden" name="can_read" value="{"0" if agents_can_read else "1"}">'
        f'<button class="mrow" type="submit">{_icon("lock")}'
        + (
            f"Close to just you and {escape(agent)}"
            if agents_can_read
            else "Let all your agents read it"
        )
        + "</button></form>"
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


class _FolderItem(NamedTuple):
    """A folder as one item on the wall. Index 2 is the newest page's time, where a page row
    keeps its own, so the day headings of the list view group both the same way."""

    id: str
    title: str
    newest: datetime | None
    agents: list
    count: int
    waiting: int
    pinned: int
    unread: bool


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


def _search_box(query: str, view: str, base: str = "/") -> str:
    """A GET form whose result is the same gallery, filtered -- so a search is a
    URL he can keep, and it works with scripts off.

    The view travels with it in a hidden field. Without that, searching would
    silently throw him back to the grid, which reads as the search having done
    something odd rather than as a control losing state."""
    value = escape(query)
    clear = (
        f'<a class="clearq" href="{base}?view={view}" aria-label="Clear the search">'
        "&times;</a>"
        if query
        else ""
    )
    return (
        f'<form class="find" method="get" action="{base}" role="search">'
        f'<input type="hidden" name="view" value="{escape(view)}">'
        f"{_icon('find')}"
        f'<input type="search" name="q" value="{value}" placeholder="Search"'
        ' aria-label="Search pages">'
        f"{clear}</form>"
    )


def _view_toggle(view: str, base: str = "/") -> str:
    """The grid/list switch. The icon shows the view you would SWITCH TO, which
    is what the reference does and what every such control does -- an icon
    showing the state you are already in reads as a label and gets ignored.

    A link, not a button: it changes what you are looking at, not the world, so
    it should be shareable, bookmarkable and reachable with scripts off."""
    to_list = view != "list"
    target = "list" if to_list else "grid"
    label = "Show as a list" if to_list else "Show as a grid"
    return (
        f'<a class="vtoggle" href="{base}?view={target}" title="{label}"'
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
        # Questions set aside while the page sits in the bin, said quietly: they
        # are not waiting on him, and they come back if he restores the page.
        asking = (
            f'<span class="setaside">{row.open_questions} question'
            f"{'' if row.open_questions == 1 else 's'} set aside</span>"
            if getattr(row, "open_questions", 0)
            else ""
        )
        urgent = " soon" if remaining in ("less than a day left", "1 day left") else ""
        out.append(
            f'<li class="row-wrap"><span class="row">'
            f'<span class="rshot none">{_icon("trash")}</span>'
            f'<span class="rt">{escape(row.title)}</span>'
            f'<span class="rmeta">{asking}'
            f'<span class="who">{escape(row.agent)}</span>'
            f'<span class="left{urgent}">{escape(remaining)}</span>'
            f'<form method="post" action="/a/{ident}/restore">'
            f'<button class="restore" type="submit">{_icon("restore")}'
            "Put back</button></form>"
            '<details class="menu tmenu"><summary aria-label="Destroy this page">'
            '<span class="dots" aria-hidden="true"></span></summary>'
            '<div class="mpanel right">'
            f'<form class="mdanger destroyform" method="post" '
            f'action="/a/{ident}/destroy">'
            "<p>Destroy for good? Its versions and every answer on it go too. "
            "This cannot be undone.</p>"
            '<input name="confirm" placeholder="type destroy" '
            'aria-label="Type destroy to confirm" autocomplete="off">'
            '<button class="danger" type="submit">Destroy</button>'
            "</form></div></details>"
            "</span></span></li>"
        )
    out.append("</ul>")
    return "".join(out)


def _tabs(
    active: str,
    waiting: int,
    pinned: int,
    deleted: int = 0,
    archived: int = 0,
    base: str = "/",
) -> str:
    """All / Waiting on you / Pinned / Archived / Deleted.

    The reference's tabs are All / Yours / Shared with you, which do not map:
    every page here is his. These are the questions actually worth asking of
    this wall, and the second one is the reason the service exists.

    A tab appears only once it has something in it. A "Pinned" tab that is
    always empty teaches him to ignore the row it sits in, and that row is also
    where search lives.

    ARCHIVED IS A TAB, not a fold at the foot of the wall. It was a
    <details> under everything else until 21 Sep, when Jeremy asked for it up
    here with the others. The three states a page can be in -- on the wall,
    archived, deleted -- now read as three tabs in one row rather than as one
    row plus something you scroll to, and the count keeps doing the job the
    fold's count did: "out of the way" never becomes "lost"."""
    out = []
    for key, label, count in (
        ("all", "All", 0),
        ("waiting", "Waiting on you", waiting),
        ("pinned", "Pinned", pinned),
        ("archived", "Archived", archived),
        ("deleted", "Deleted", deleted),
    ):
        if key != "all" and not count:
            continue
        cls = "tab on" if key == active else "tab"
        suffix = f'<span class="tn">{count}</span>' if count else ""
        href = base if key == "all" else f"{base}?filter={key}"
        out.append(f'<a class="{cls}" href="{href}">{label}{suffix}</a>')
    return f'<nav class="tabs">{"".join(out)}</nav>'


def _agent_pick(
    agents: list[str], chosen: str, view: str, query: str, base: str = "/"
) -> str:
    """Filter the wall by who published. Jeremy, 21 Sep: "ideally we can filter
    on the gallery by agent as well as search", and then "make it subtle in the
    design and look good".

    SO: NOTHING NEW WHEN THERE IS NOTHING TO CHOOSE. One seat on the wall means
    no control at all -- a dropdown whose only entry is the answer you already
    have is furniture, and this wall is read by one person who will see it every
    day.

    A <details> menu, which is how every other menu here works and therefore
    needs no script: the tile dots, the page bar and the rename dialog are all
    the same mechanism. It is also the shape the reference uses for its "All
    types" filter, in the same slot beside the search.

    LINKS, not a form. Changing what you are looking at should be shareable and
    bookmarkable, and a GET form with a <select> needs either a submit button
    beside it -- two controls where one will do -- or a script, and this wall
    works with scripts off.

    Every link carries the CURRENT view and search, so filtering does not
    silently drop the other two things the reader has set.
    """
    if len(agents) < 2:
        return ""

    def link(value: str, label: str) -> str:
        bits = []
        if value:
            bits.append(f"agent={quote(value)}")
        if query:
            bits.append(f"q={quote(query)}")
        if view and view != "grid":
            bits.append(f"view={quote(view)}")
        href = f"{base}?" + "&amp;".join(bits) if bits else base
        mark = ' aria-current="true"' if value == chosen else ""
        return f'<a class="mrow" href="{href}"{mark}>{escape(label)}</a>'

    rows = link("", "Everyone") + "".join(link(a, a) for a in agents)
    summary = escape(chosen) if chosen else "Everyone"
    return (
        f'<details class="menu pick{" on" if chosen else ""}">'
        f'<summary title="Filter by who published">{summary}'
        '<span class="chev" aria-hidden="true"></span></summary>'
        f'<div class="mpanel">{rows}</div></details>'
    )


def _visible_count(active: str, rows: list, archived: list, deleted: list) -> int:
    """How many pages the tab you are on is showing -- the number beside the
    search box. It has to follow the tab, not the live wall: "9 pages" next to
    an Archived tab showing two is the count contradicting what is under it."""
    if active == "archived":
        return len(archived)
    if active == "deleted":
        return len(deleted)
    return len(rows)


def _pick(artifact_id: str, title: str) -> str:
    """The tick on a page's tile or row in select mode. The value is the page id, the label
    names the page for a screen reader; a label, so the whole corner is the target."""
    return (
        '<label class="pick"><input type="checkbox" name="page" '
        f'value="{escape(artifact_id)}" aria-label="Select {escape(title)}"></label>'
    )


def _select_toggle(request: Request, selecting: bool, active: str) -> str:
    """ "Select" (or "Done") beside the view toggle: a plain link to the same wall with
    `select=1` added or removed, so select mode is a URL and works with scripts off."""
    if active == "deleted":
        return ""
    params = [
        (k, v)
        for k, v in request.query_params.multi_items()
        if k not in ("select", "done", "n")
    ]
    if not selecting:
        params.append(("select", "1"))
    href = request.url.path + ("?" + urlencode(params) if params else "")
    return (
        f'<a class="vtoggle selecttoggle" href="{escape(href)}"'
        ' style="width:auto;padding:0 .6rem;font-size:.8125rem">'
        f"{'Done' if selecting else 'Select'}</a>"
    )


_SELECT_DONE = {
    "moved": "Moved {pages} into the folder.",
    "new": "Moved {pages} into a new folder.",
    "out": "Moved {pages} out of their folders.",
    "none": "Tick at least one page first.",
}


def _select_message(code: str, n: str) -> str:
    """The result line for `?done=<code>&n=<count>`. Only a known code shows anything, and the
    count is an integer clamped to 0..200, so a crafted link cannot put its own words on the wall."""
    template = _SELECT_DONE.get(code)
    if template is None:
        return ""
    count = int(n) if n.isascii() and n.isdigit() and len(n) <= 6 else 0
    count = min(count, 200)
    return template.format(pages=f"{count} page" if count == 1 else f"{count} pages")


def _select_form(main: str, back: str, note: str = "") -> str:
    """The wall's tiles inside ONE form, and the action bar under them. Both buttons post the
    ticked pages to /select: "Move to folder" opens a page of choices (nothing moves yet),
    "Take out of folder" moves at once. Folder tiles carry no tick."""
    shown = f'<p class="selnote" role="status">{escape(note)}</p>' if note else ""
    return (
        '<form method="post" action="/select" class="selectform">'
        f'<input type="hidden" name="back" value="{escape(back)}">'
        f'{shown}<p class="selnote">Tick pages, then choose what to do with them '
        "(at most 200 at once).</p>"
        f"{main}"
        '<div class="selbar">'
        '<button class="btn primary" type="submit" name="do" value="choose">'
        "Move to folder</button>"
        '<button class="btn" type="submit" name="do" value="out">Take out of folder</button>'
        f'<a class="btn" href="{escape(back)}">Done</a>'
        "</div></form>"
    )


def _controls_row(
    view: str,
    query: str,
    active: str,
    waiting: int,
    pinned: int,
    pages: int,
    deleted: int = 0,
    agents: list[str] | None = None,
    agent: str = "",
    archived: int = 0,
    base: str = "/",
    select_html: str = "",
) -> str:
    """The row under the title: WHAT to show on the left, how to find it and how
    to see it on the right."""
    pages_text = f"{pages} page" if pages == 1 else f"{pages} pages"
    return (
        '<div class="controls">'
        f"{_tabs(active, waiting, pinned, deleted, archived, base)}"
        '<div class="cright">'
        f"{_agent_pick(agents or [], agent, view, query, base)}"
        f"{_search_box(query, view, base)}"
        f'<span class="count">{pages_text}</span>'
        f"{_view_toggle(view, base)}"
        f"{select_html}"
        "</div></div>"
    )


def _session_tag(session: str | None) -> str:
    """Which session, as quietly as it can be said and still be there.

    Jeremy asked for this on every tile (21 Sep, choosing (b) over my
    recommendation of the page only) and then: "make it subtle in the design
    and look good." So it is the SHORT half of the label and nothing else --
    the agent name is already in the slot beside it, and repeating
    "artifacts-seat" next to "artifacts-seat" is noise rather than information.
    The full label is one hover away in the title attribute.

    A label is "<seat>/<short id>" by convention, so the part after the slash
    is the part that distinguishes two sessions of the same seat, which is the
    entire question this answers. With no slash, the whole thing, bounded --
    a caller is free to send anything and a wall must not be pushed about by it.

    Empty for a page with no session, which is every page published before
    21 Sep and any publish by a caller that declared nothing. No placeholder,
    no "unknown": a tile that says nothing is correct, and a column of
    "unknown" would be a column of noise on a wall that is mostly history.
    """
    if not session:
        return ""
    # Six characters. Long enough to tell two of a seat's sessions apart on a
    # wall that holds a dozen pages, short enough that the meta row stays on
    # ONE line in a 304px card -- at eight it wrapped, which made those tiles
    # taller than the rest and the grid ragged. Subtle has a width.
    short = session.rsplit("/", 1)[-1][:6] or session[:6]
    return f'<span class="sess" title="{escape(session)}">{escape(short)}</span>'


def _inbox_link(count: int | str) -> str:
    """The way into his inbox, always there, with how many items it holds."""
    some = " some" if str(count) != "0" else ""
    return f'<a class="inboxlink{some}" href="/inbox">Inbox ({escape(str(count))})</a>'


def _wall_meta(waiting: int, inbox_count: int | str = 0) -> str:
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
    out = _inbox_link(inbox_count)
    if waiting:
        noun = "question" if waiting == 1 else "questions"
        out += (
            f'<a class="waiting" href="/?filter=waiting">'
            f"{waiting} {noun} waiting on you</a>"
        )
    return out


_ATTACH = (
    # Hidden until /comments.js says it can do the work (a `cpics-on` class on <html>);
    # without scripts the form is the plain one. The note is the whole privacy warning.
    '<div class="cattach"><button type="button" class="cattachbtn">Attach pictures</button>'
    '<input type="file" class="cfile" accept="image/png,image/jpeg,image/webp" multiple hidden>'
    '<span class="cnote">Images are not checked for secrets.</span>'
    '<div class="cpics"></div><p class="cpics-say" role="alert"></p></div>'
)


def _pictures(row, tokens: dict[str, str] | None) -> str:
    """The pictures on a comment, as thumbnails that open larger in place. Each `src` is
    the page's content origin with a one-hour token minted here for this reader; the
    gallery origin never serves a byte of them. Without scripts the thumbnail is a plain
    link to the same picture."""
    shown = getattr(row, "images", None)
    if not shown or not tokens:
        return ""
    out = []
    for n, pic in enumerate(shown, start=1):
        if pic["id"] not in tokens:
            continue
        url = image_store.origin_url(row.artifact_id, pic["id"], tokens[pic["id"]])
        out.append(
            f'<a class="cimg" href="{escape(url)}" target="_blank" rel="noopener noreferrer">'
            f'<img class="cthumb" src="{escape(url)}" loading="lazy" decoding="async"'
            f' alt="Picture {n} on this comment" width="{int(pic["width"])}"'
            f' height="{int(pic["height"])}"></a>'
        )
    return f'<div class="cpicsrow">{"".join(out)}</div>'


def _bubble(row, tokens: dict[str, str] | None = None) -> str:
    """One message in a thread: who, via which session, when, and what. With `tokens` (one
    per picture id, minted together by the caller), the pictures it carries."""
    who = "Jeremy" if row.author_kind == "human" else escape(row.author) + " (agent)"
    # The office is the part of the caller's session label before the first
    # "/", and only what the caller said: a key several offices share cannot
    # tell them apart, so it reads "via session", never as a fact.
    label = (getattr(row, "session", None) or "").strip()
    via = (
        f'<span class="cvia" title="{escape(label)}">'
        f"via session {escape(label.split('/', 1)[0] or label)}</span>"
        if row.author_kind != "human" and label
        else ""
    )
    # CommentRow.created_at is an ISO STRING, not a datetime -- `_ago` takes
    # the latter, and handing it the former raises on .tzinfo. Caught by
    # rendering the page; the whole comment section would have 500'd on
    # first use.
    when = _ago(datetime.fromisoformat(row.created_at)) if row.created_at else ""
    # Agent-written text landing in Jeremy's own signed-in origin, so it is
    # escaped here like everything else that crosses that boundary.
    #
    # `remove`/`looks_good` carry no note of their own (comments.add()
    # allows an empty body for those two kinds) -- an empty `<p></p>` is
    # not a rendering of anything anyone said, so a bodiless mark gets no
    # paragraph at all; what it IS is already the tag on its quote, in
    # `quoted()` below (final review, Item 4a).
    note = f"<p>{escape(row.body)}</p>" if row.body.strip() else ""
    held = '<span class="htag">held</span>' if getattr(row, "held", False) else ""
    return (
        f'<li class="cmt {escape(row.author_kind)}">'
        f'<span class="cwho">{who}</span>'
        f"{via}"
        f'<span class="cwhen">{escape(when)}</span>'
        f"{held}"
        f"{note}{_pictures(row, tokens)}</li>"
    )


def _render_comments(
    rows: list,
    artifact_id: str,
    everywhere: list | None = None,
    only_thread: str | None = None,
    db: Database | None = None,
) -> str:
    """The conversation on a page: threads, replies, and a box to start one --
    and, with `everywhere`, every page's conversations in the same drawer.

    homelab-ij8.18. Jeremy wanted the conversation with the agent to happen on
    the page rather than in a terminal. The storage and the agent's side of it
    have existed since 19 Sep; this is the half he uses.

    ORDERED OLDEST FIRST, unlike the wall. A conversation is read forwards --
    newest-first is right for a list of pages, where you want what changed, and
    wrong for a thread, where you want what was said.

    A RESOLVED THREAD IS FOLDED, NOT HIDDEN. It is the record of something that
    was settled, and this service loses nothing: the count is on the summary so
    "out of the way" never becomes "gone", exactly as the archived fold works.

    Always renders the composer, even with nothing to show -- an empty section
    with a box is an invitation; an empty section with no box is a fault.

    `only_thread` (a root id) returns just that one thread's <li>, rendered by
    the very same helper the drawer uses, for the in-place post.
    """
    roots = [r for r in rows if r.thread_root is None]
    replies: dict[str, list] = {}
    for row in rows:
        if row.thread_root:
            replies.setdefault(row.thread_root, []).append(row)

    # Every picture's token in one go, on one connection: a panel can show up to 40.
    tokens: dict[str, str] = {}
    if db is not None:
        shown = list(rows) + [r for page in everywhere or [] for r in page.rows]
        tokens = image_store.mint_tokens(
            db, [p["id"] for r in shown for p in getattr(r, "images", None) or []]
        )

    def bubble(row) -> str:
        return _bubble(row, tokens)

    def element_spot(pin: dict) -> str:
        """What an element pin is on, in the page's terms and escaped: a cell by
        its row and column, an image or chart by its alt text or label, else
        the tag and id."""
        if pin.get("kind") != "element":
            return ""
        tag = pin.get("tag") or "element"
        cell = []
        if pin.get("row"):
            cell.append(f"Row {pin['row']}")
        if pin.get("column"):
            cell.append(f"{'column' if cell else 'Column'} {pin['column']}")
        if cell:
            return escape(", ".join(cell))
        if pin.get("label"):
            kind = {"img": "Image", "picture": "Image", "svg": "Chart"}.get(
                tag, "Chart" if tag == "canvas" else "Element"
            )
            return escape(f"{kind}: {pin['label']}")
        return escape(f"{tag}#{pin['id']}" if pin.get("id") else tag)

    def pin_note(root) -> str:
        """Where the pin is on the page as it is now: said plainly, never
        hidden, and never re-attached to words it was not about. `anchor_state`
        is the server's reading of the pin's evidence (quote, heading, id);
        `quote_state` is the older quote-only reading, used when the pin's
        state is not known."""
        version = int(root.version_n)
        state = getattr(root, "anchor_state", "")
        pin = root.anchor or {}
        parts = []
        if pin.get("heading"):
            parts.append(f"under {escape(pin['heading'])}")
        if pin.get("kind") in ("range", "quote", "element") and state != "outdated":
            parts.append(f"Pinned on version {version}")
        where = "".join(f'<span class="cwhere">{p}</span>' for p in parts)
        if state == "outdated" or (not state and root.quote_state == "changed"):
            what = (
                "Outdated: this spot is gone from the current version &mdash; "
                f"it was there in version {version}."
            )
        elif state == "edited":
            what = f"Text edited since version {version}."
            now = getattr(root, "anchor_current", "")
            if now:
                what += f" Now: &ldquo;{escape(now)}&rdquo;"
        else:
            what = ""
        moved = getattr(root, "anchor_note", "")
        extra = f" {escape(moved)}." if moved else ""
        if not (what or extra):
            return f'<span class="cmoved">{where}</span>' if where else ""
        cls = " coutdated" if state == "outdated" or not state else ""
        return f'<span class="cmoved{cls}">{where}{" " if where else ""}{what}{extra}</span>'

    def quoted(root) -> str:
        """What the thread is about, when it is about a particular passage.

        The drawer used to show only the reply, so an agent that anchored a
        comment to a sentence sent Jeremy a remark with no subject. And a page
        is republished constantly here -- this one has had forty-odd versions
        in two days -- so the quoted words can be gone while the comment sits
        there looking like it is about whatever now occupies that spot. When
        they are gone the thread says so and names the version it was made on,
        rather than silently re-attaching to text it was never about
        (homelab-ij8.18's re-anchoring rule).
        """
        pin = root.anchor or {}
        if not root.quote and pin.get("kind") != "element":
            return ""
        note = pin_note(root)
        label = {"remove": "Remove this", "looks_good": "Looks good"}.get(
            getattr(root, "kind", "comment"), ""
        )
        tag = (
            f'<span class="ckind ckind-{escape(root.kind)}">{label}</span> '
            if label
            else ""
        )
        spot = element_spot(pin)
        shown = escape(root.quote) if root.quote else spot
        # What the quote is OF, for a pin on something that is not a passage.
        of = (
            f'<span class="cspot">{spot}</span> '
            if spot and root.quote and pin.get("kind") == "element"
            else ""
        )
        return f'<blockquote class="cquote">{tag}{of}{shown}{note}</blockquote>'

    def thread(root) -> str:
        kids = "".join(bubble(r) for r in replies.get(root.id, []))
        resolved = root.resolved_at is not None
        action = (
            ""
            if resolved
            else (
                f'<form class="cresolve" method="post" action="/c/{escape(root.id)}/resolve">'
                '<button type="submit">Mark settled</button></form>'
            )
        )
        reply = (
            ""
            if resolved
            # The thread's OWN page, not the page being looked at. They are
            # the same in the ordinary case and they are NOT in the
            # all-pages view, where a reply typed under one page's heading
            # must not land on another page.
            else (
                f'<form class="creply" method="post" action="/c/{escape(root.artifact_id)}">'
                f'<input type="hidden" name="thread_root" value="{escape(root.id)}">'
                '<input name="body" placeholder="Reply" aria-label="Reply">'
                f'<button type="submit">Send</button>{_ATTACH}</form>'
            )
        )
        return (
            # `data-mark-id` is what a hover card is copied by: the page names
            # a mark, and comments.js clones THIS escaped markup for it.
            f'<li class="thread{" settled" if resolved else ""}"'
            f' data-mark-id="{escape(root.id)}" id="thread-{escape(root.id)}">'
            f"{quoted(root)}"
            f'<ul class="bubbles">{bubble(root)}{kids}</ul>{reply}{action}</li>'
        )

    def settled_fold(settled_roots: list) -> str:
        noun = "conversation" if len(settled_roots) == 1 else "conversations"
        return (
            f'<details class="settled-cmts"><summary>'
            '<span class="chev" aria-hidden="true"></span>Settled'
            f'<span class="n">{len(settled_roots)} {noun}</span></summary>'
            '<ul class="threads">'
            + "".join(thread(r) for r in settled_roots)
            + "</ul></details>"
        )

    if only_thread is not None:
        return "".join(thread(r) for r in roots if r.id == only_thread)

    open_roots = [r for r in roots if r.resolved_at is None]
    settled = [r for r in roots if r.resolved_at is not None]

    ident = escape(artifact_id)
    # THIS PAGE / ALL PAGES, the filter the reference's comments panel carries
    # ("This page / All pages", plus a Show resolved checkbox -- which this
    # drawer has always had as the Settled fold, so it is not repeated here).
    # Links, not a script and not a checkbox: the same two-state switch the
    # wall's tabs are, wearing the same .tab class so it reads as one idea, and
    # the `#comments` fragment on both keeps the drawer open across the round
    # trip.
    on_all = everywhere is not None
    scope = (
        f'<nav class="tabs cscope">'
        f'<a class="tab{"" if on_all else " on"}" href="/a/{ident}#comments">This page</a>'
        f'<a class="tab{" on" if on_all else ""}"'
        f' href="/a/{ident}?comments=all#comments">All pages</a>'
        f"</nav>"
    )
    # A PANEL OPENED FROM THE BAR, which is the shape the reference uses: its
    # page bar carries a ButtonGroup labelled "Comments" holding a Comment
    # toggle and a "More comment options" menu, and the conversation lives in
    # a panel rather than in the document flow. Read off the saved claude.ai
    # HTML Jeremy gave us, not guessed from the screenshots.
    #
    # DOCKED BESIDE THE PAGE, not laid over it (see the .cpanel CSS). With
    # scripts, comments.js opens and closes it by toggling a class, so nothing
    # navigates. Without them `:target` still does the whole job: the bar link
    # is href="#comments", the panel is #comments, and the close control is a
    # link to "#_" (a fragment nothing has, so the page does not scroll to its
    # top the way "#" would), which drops the target. comments.js swaps it for a
    # real button. No backdrop link: there is no backdrop.
    out = [
        (
            '<aside class="cpanel" id="comments" aria-label="Comments">'
            '<div class="chead"><h2>Comments</h2>'
            '<a class="cclose" href="#_" aria-label="Close comments">\u00d7</a></div>'
            # Filled by /comments.js from the page's own report of the marks
            # it could not place; empty and hidden until then.
            '<p class="cunplaced" id="marks-unplaced" hidden></p>'
            f"{scope}"
        )
    ]
    if on_all:
        # Grouped by page, most recently active first, each group naming the
        # page it belongs to and linking to it -- a remark with no page is as
        # unreadable as the anchored comment with no quote was.
        for page in everywhere:
            page_roots = [r for r in page.rows if r.thread_root is None]
            # REBUILT per page, never appended to. `replies` was already filled
            # from the page being looked at, and that page is in this list too,
            # so appending showed every one of its replies twice. Invisible to
            # the tests, obvious in the render.
            replies.clear()
            for child in page.rows:
                if child.thread_root:
                    replies.setdefault(child.thread_root, []).append(child)
            page_open = [r for r in page_roots if r.resolved_at is None]
            page_settled = [r for r in page_roots if r.resolved_at is not None]
            flag = '<span class="carch">archived</span>' if page.archived else ""
            out.append(
                f'<h3 class="cpage"><a href="/a/{escape(page.artifact_id)}#comments">'
                f"{escape(page.artifact_title)}</a>{flag}</h3>"
            )
            if page_open:
                out.append(
                    '<ul class="threads">'
                    + "".join(thread(r) for r in page_open)
                    + "</ul>"
                )
            if page_settled:
                out.append(settled_fold(page_settled))
        if not everywhere:
            out.append(
                '<p class="cempty">No conversations on any page yet. Open a page '
                "and say something; the agent that published it will read it.</p>"
            )
        out.append("</aside>")
        return "".join(out)
    gone = [r for r in open_roots if getattr(r, "anchor_state", "") == "outdated"]
    if gone:
        # Page level, always at the top, so a pin whose spot a republish
        # removed cannot be missed. Each thread is also in its place below.
        out.append(
            '<section class="coutdated-list" aria-label="Outdated pins">'
            f"<h3>Outdated pins ({len(gone)})</h3><ul>"
            + "".join(
                f'<li><a href="#thread-{escape(r.id)}">&ldquo;{escape(r.quote[:120])}'
                f"&rdquo;</a> <span>"
                + (
                    f"under {escape(r.anchor['heading'])}, "
                    if (r.anchor or {}).get("heading")
                    else ""
                )
                + f"version {int(r.version_n)}</span></li>"
                for r in gone
            )
            + "</ul></section>"
        )
    if open_roots:
        out.append(
            '<ul class="threads">' + "".join(thread(r) for r in open_roots) + "</ul>"
        )
    elif not settled:
        out.append(
            '<p class="cempty">Nothing yet. Say something and the agent that '
            "published this page will read it.</p>"
        )
    out.append(
        f'<form class="cnew" method="post" action="/c/{ident}">'
        '<input name="body" placeholder="Comment on this page" '
        'aria-label="Comment on this page">'
        f'<button type="submit">Send</button>{_ATTACH}</form>'
    )
    if settled:
        out.append(settled_fold(settled))
    out.append("</aside>")
    return "".join(out)


def _comments_button(artifact_id: str, open_count: int) -> str:
    """The bar control. The reference groups a Comment toggle and a menu under
    an aria-label of "Comments" in the page bar; this is the half that has
    something to open, and it carries the count because a control that does not
    say whether there is anything behind it gets pressed once and then never
    again."""
    count = f'<span class="cn">{open_count}</span>' if open_count else ""
    return (
        f'<a class="cbtn{" has" if open_count else ""}" href="#comments"'
        f' aria-controls="comments" aria-label="Comments">{_icon("comment")}{count}</a>'
        # Revealed by comments.js once it has a composer to fill; the C key does the same.
        '<button type="button" class="pinmodebtn" hidden aria-pressed="false"'
        ' aria-label="Pin a comment to a spot on the page (C)"'
        ' title="Pin a comment to a spot on the page (C)">Pin</button>'
    )


def _share_button(artifact_id: str) -> str:
    """The bar's direct Share: the same route the menu row opens."""
    return (
        f'<a class="sharebtn" aria-label="Share" href="/a/{escape(artifact_id)}/share">'
        f"{_icon('link')}<span>Share</span></a>"
    )


# Hidden until comments.js finds the Fullscreen API; with scripts off it stays so.
_FS_BUTTON = (
    '<button type="button" class="fsbtn" hidden aria-label="Full screen"'
    ' data-on="Exit full screen" data-off="Full screen">'
    '<svg class="mi" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 2h4v1.3H3.3V6H2V2Zm8 0h4v4h-1.3V3.3H10V2ZM2 10h1.3v2.7H6V14H2v-4Zm10.7 0H14v4h-4v-1.3h2.7V10Z"/></svg>'
    "</button>"
)


# His menu, top right on every gallery page (homelab-ij8.103, his ask: "a little
# profile area on the top right for my user"). A <details>, like the page menu,
# so it opens with scripts off. Named outright: Authelia admits exactly one
# subject here, and the app does not trust an identity header it cannot prove
# was set by the ingress (see the module docstring).
_PROFILE = (
    '<details class="menu profile"><summary aria-label="Your account">Jeremy'
    '<span class="chev" aria-hidden="true"></span></summary><div class="mpanel">'
    '<a class="mrow" href="/machines">Machines and keys</a>'
    '<a class="mrow" href="/connect">Connect a machine</a>'
    "</div></details>"
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
    full: bool = False,
    actions: str = "",
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
            f'<header class="bar bleed{" flush" if full else ""}">{back}{heading}'
            f"{meta_block}{actions}{_PROFILE}</header>"
            f'<div class="wrap{" full" if full else ""}">'
        )
    else:
        top = (
            f'<div class="wrap"><header class="bar">{back}{heading}'
            f"{meta_block}{_PROFILE}</header>"
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
        # <main>. The reference has one and this had none, which a structural
        # diff of the two documents found rather than any amount of looking at
        # them: without it there is no landmark to skip to, so a screen-reader
        # user arrives at the top of the chrome every time and walks the header
        # again on every page. The controls row stays OUTSIDE it -- tabs and
        # search are navigation, not the content they filter.
        f"<main>{body}</main></div>{scripts}</body></html>"
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
    """ "Edited 5h ago", the way the reference says it -- and the way a person
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
    if row.answer_source == "slack":
        return (
            '<p class="relayed-note">You answered this in Slack, verified by '
            "quarterdeck. If that is not what you meant, answer it again here.</p>"
        )
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


_moment = inbox.moment
_is_overdue = inbox.is_overdue


def _inbox_key(row: decisions.DecisionRow, now: datetime) -> tuple:
    """Inbox order for a question (see `inbox`); the page's open list uses it too."""
    return inbox.key(row, now)


def _question_meta(row: decisions.DecisionRow, now: datetime) -> list[str]:
    """The compact facts about a question, each already escaped: topic, urgency
    (with what it is blocking and by when), overdue, and who else needs the answer."""
    parts = []
    if row.topic:
        parts.append(
            f'<span class="qtopic">{escape(row.topic["title"] or row.topic["id"])}</span>'
        )
    when = f"{escape(_when(row.needed_by))} UTC" if row.needed_by else ""
    if row.urgency == "blocking":
        label = "blocking" + (f": {escape(row.blocked_on)}" if row.blocked_on else "")
        if when:
            label += f", needed by {when}"
    elif row.urgency == "by":
        label = f"needed by {when}" if when else "needed by a set time"
    else:
        label = "when you can" + (f", wanted by {when}" if when else "")
    cls = "urg-blocking" if row.urgency == "blocking" else "urg"
    parts.append(f'<span class="{cls}">{label}</span>')
    if _is_overdue(row, now):
        parts.append('<span class="over">overdue</span>')
    if row.for_offices:
        parts.append(
            "<span>also needed by "
            + ", ".join(escape(o) for o in row.for_offices)
            + "</span>"
        )
    return parts


def _since_block(row: decisions.DecisionRow, mine: list) -> str:
    """The outcomes on a decision, the one markup the page card and the inbox card
    share: shown above the buttons, so he sees what moved before he chooses."""
    if not mine:
        return ""
    items = "".join(f"<li>{escape(o.body)}</li>" for o in mine)
    label = "Since you were asked" if row.answer is None else "What it led to"
    return f'<div class="since"><b>{label}</b><ul>{items}</ul></div>'


def _slack_button(option: dict, copy, chosen: str | None = None) -> str:
    recommended = option["letter"] == copy.recommended
    is_chosen = option["letter"] == chosen
    classes = " ".join(
        c for c in ("rec" if recommended else "", "chosen" if is_chosen else "") if c
    )
    if is_chosen:
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


def _slack_link(copy) -> str:
    """The link to Slack. Only an http(s) link was ever stored (partner_in)."""
    if not copy.link:
        return "Asked in Slack"
    return f'<a href="{escape(copy.link)}" rel="noopener noreferrer">Open in Slack</a>'


def _slack_head(copy, label: str = "Question") -> str:
    return (
        f'<p class="ihead"><span>{label}</span>'
        '<span class="from-slack">from Slack</span>'
        f"<span>from {escape(copy.asking_office)}</span></p>"
    )


def _render_slack_question(copy, now: datetime) -> str:
    meta = " &middot; ".join(_question_meta(copy, now))
    ctx = f'<p class="qctx">{escape(copy.context)}</p>' if copy.context else ""
    buttons = "".join(_slack_button(o, copy) for o in copy.options)
    return (
        f'<li class="ask" id="i-{escape(copy.id)}">{_slack_head(copy)}'
        f'<p class="q">{escape(copy.title)}</p>{ctx}'
        f'<p class="qmeta">{meta}</p>'
        f'<form method="post" action="/pq/{escape(copy.qid)}/answer">'
        f'<input type="hidden" name="next" value="inbox">{buttons}'
        '<input name="note" placeholder="note (optional)"></form>'
        f'<p class="ilink">{_slack_link(copy)}</p></li>'
    )


def _render_slack_status(pending: list, settled: list) -> str:
    """Slack-home questions he is no longer being asked: ones he answered here
    that the home has not confirmed, and ones the home has settled lately."""
    out = []
    for c in pending:
        mine = next(
            (o["label"] for o in c.options if o["letter"] == c.pending_answer), ""
        )
        out.append(
            f'<li class="ask" id="i-{escape(c.id)}">{_slack_head(c, "Answered here")}'
            f'<p class="q">{escape(c.title)}</p>'
            f'<p class="pickup">You answered ({escape(c.pending_answer or "")}) '
            f"{escape(mine)} here. Answered here, waiting for quarterdeck.</p>"
            f'<p class="ilink">{_slack_link(c)}</p></li>'
        )
    for c in settled:
        mine = next((o["label"] for o in c.options if o["letter"] == c.answer), "")
        out.append(
            f'<li class="ask" id="i-{escape(c.id)}">{_slack_head(c, "Settled")}'
            f'<p class="q">{escape(c.title)}</p>'
            f'<p class="pickup">Settled in Slack: ({escape(c.answer or "")}) '
            f"{escape(mine)}.</p>"
            f'<p class="ilink">{_slack_link(c)}</p></li>'
        )
    return '<ul class="inbox">' + "".join(out) + "</ul>" if out else ""


def _render_inbox(
    rows: list[inbox.Item], now: datetime, outcomes: dict | None = None
) -> str:
    """His inbox: one list, already in inbox order, each item with what he needs
    to act. Every agent-supplied string is escaped here."""
    if not rows:
        return '<p class="empty">Nothing is waiting on you.</p>'
    out = []
    for item in rows:
        page = f"/a/{escape(item.artifact_id)}"
        title = escape(item.page_title)
        d = item.decision
        if item.kind == "slack_question" and item.copy is not None:
            out.append(_render_slack_question(item.copy, now))
        elif item.kind == "question" and d is not None:
            meta = " &middot; ".join(_question_meta(d, now))
            ctx = f'<p class="qctx">{escape(d.context)}</p>' if d.context else ""
            buttons = "".join(_button(o, d) for o in d.options)
            out.append(
                f'<li class="ask" id="i-{escape(d.id)}">'
                f'<p class="ihead"><span>Question</span>'
                f'<a href="{page}">{title}</a>'
                f"<span>from {escape(item.office)}</span></p>"
                f'<p class="q">{escape(d.question)}</p>{ctx}'
                f'<p class="qmeta">{meta}</p>'
                f"{_since_block(d, (outcomes or {}).get(d.id) or [])}"
                f'<form method="post" action="/d/{escape(d.id)}/answer">'
                f'<input type="hidden" name="next" value="inbox">{buttons}'
                '<input name="note" placeholder="note (optional)"></form>'
                f'<p class="ilink"><a href="{page}#q-{escape(d.id)}">Open the page</a></p>'
                "</li>"
            )
        elif item.kind == "review" and d is not None:
            out.append(
                f'<li class="ask" id="i-{escape(d.id)}">'
                f'<p class="ihead"><span>Review</span>'
                f'<a href="{page}">{title}</a>'
                f"<span>from {escape(item.office)}</span></p>"
                f'<p class="q"><span class="rtag">Review</span> {escape(d.question)}</p>'
                f'<p class="ilink"><a href="{page}">Open the review</a></p></li>'
            )
        else:
            excerpt = item.body.strip()
            if len(excerpt) > inbox.REPLY_EXCERPT:
                excerpt = excerpt[: inbox.REPLY_EXCERPT].rstrip() + "\u2026"
            out.append(
                f'<li class="ask" id="i-{escape(item.id)}">'
                f'<p class="ihead"><span>Reply from {escape(item.office)}</span>'
                f'<a href="{page}">{title}</a>'
                f"<span>{escape(_when(item.created_at.isoformat()))} UTC</span></p>"
                f'<p class="ibody">{escape(excerpt)}</p>'
                f'<p class="ilink"><a href="{page}#comments">Open the thread</a></p></li>'
            )
    return '<ul class="inbox">' + "".join(out) + "</ul>"


def _render_decisions(
    rows: list[decisions.DecisionRow],
    listening_since: str | None = None,
    outcomes: dict[str, list] | None = None,
    held_count: int = 0,
    rounds: dict[str, int] | None = None,
    shown_version: int | None = None,
    round_view: dict | None = None,
    presence: dict | None = None,
    note_threads: dict[str, list] | None = None,
    prev_read_at: datetime | str | None = None,
    routes: dict[str, listening.Route] | None = None,
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

    An answered QUESTION still renders every button: re-answering is allowed (see
    `decisions.answer`'s docstring) precisely so Jeremy can change his mind here, on
    the page, rather than needing a terminal. A REVIEW is the exception: its first
    verdict is final, so a settled review shows the verdict and no form.
    """
    if not rows:
        return "", ""

    open_rows = [r for r in rows if r.answer is None]
    settled_rows = [r for r in rows if r.answer is not None]
    now = datetime.now(UTC)
    on_page = {r.id for r in open_rows}

    def _note_thread(row: decisions.DecisionRow) -> tuple[str, bool]:
        """The short thread under his note, and whether it holds a message from
        an agent newer than his previous visit. Empty until someone has replied:
        the form that continues a thread needs a thread to continue."""
        thread = (note_threads or {}).get(row.id) or []
        if not thread:
            return "", False
        first = thread[0]
        seen = _moment(prev_read_at)
        fresh = any(
            m.author_kind == "agent"
            and (seen is None or (_moment(m.created_at) or seen) > seen)
            for m in thread
        )
        answered = _moment(row.answered_at)
        earlier = (
            answered is not None and (_moment(first.created_at) or answered) < answered
        )
        head = ("About an earlier answer" if earlier else "Replies under your note") + (
            '<span class="ntag">new reply</span>' if fresh else ""
        )
        root = first.thread_root or first.id
        form = (
            f'<form class="nreply" method="post" action="/c/{escape(row.artifact_id)}">'
            f'<input type="hidden" name="thread_root" value="{escape(root)}">'
            '<input name="body" placeholder="Reply" aria-label="Reply under your note">'
            '<button type="submit">Send</button></form>'
        )
        html = (
            f'<div class="nthread" id="note-{escape(row.id)}"><p class="nhead">{head}</p>'
            f'<ul class="bubbles">{"".join(_bubble(m) for m in thread)}</ul>'
            f"{form}</div>"
        )
        return html, fresh

    def _since(row: decisions.DecisionRow) -> str:
        """What has happened to this question since it was asked.

        THE OPEN CASE IS WHY THIS IS HERE. An outcome on a question he has not
        answered means something moved under it -- the operator corrected two
        promises on a page while its question was still open (21 Sep 2026) --
        and a question whose premise changed after he read it is one he can
        answer wrongly through no fault of his own. So it renders ABOVE the
        buttons on an open card: he must see it before he chooses, not after.
        """
        return _since_block(row, (outcomes or {}).get(row.id) or [])

    def _mode_switch(row: decisions.DecisionRow, held: int) -> str:
        """Hold or live, for an OPEN review only: his marks reach the agent as he
        makes them, or wait for his verdict. The mode is the server's, so every
        device shows the same one."""
        if row.review_mode == "held":
            state, button, target = (
                "Holding your marks until your verdict",
                "Send them as I go",
                "live",
            )
        else:
            state, button, target = (
                "Sending your marks as you make them",
                "Hold them until my verdict",
                "held",
            )
        count = ""
        approved = ""
        if held > 0:
            count = f' <span class="rheld">{held} mark{"" if held == 1 else "s"} held</span>'
            approved = (
                '<p class="rhint">Approved with held marks counts as '
                "Approved with notes.</p>"
            )
        return (
            f'<div class="rmode"><span>{state}</span>{count}'
            f'<form method="post" action="/d/{escape(row.id)}/mode">'
            f'<button type="submit" name="mode" value="{target}">{button}</button>'
            f"</form></div>{approved}"
        )

    def _presence_line(row: decisions.DecisionRow, tag: str = "p") -> str:
        """Who is on a review, or why it was handed back. Every value is agent
        text and is escaped. `tag` is "span" inside a settled entry's summary."""
        p = (presence or {}).get(row.id)
        if not p:
            return ""
        if p["live"]:
            secs = p["seen_seconds"]
            if secs is None:
                seen = ""
            elif secs < 60:
                seen = " \u00b7 last seen just now"
            else:
                seen = f" \u00b7 last seen {int(secs // 60)} min ago"
            text = f"Agent listening \u00b7 {escape(p['session'])}{seen}"
        else:
            text = f"Handed back by {escape(p['session'])}: {escape(p['reason'])}"
        return f'<{tag} class="rhint rpresence">{text}</{tag}>'

    def _round_block(row: decisions.DecisionRow) -> str:
        """Round 2 and later of an open review (page review step 4): the round,
        the Hide/Show changes link (a plain link, so it works with scripts
        off), why the changes could not be marked up if they could not, and his
        marks from earlier rounds with what became of each."""
        view = (round_view or {}).get(row.id)
        if not view:
            return _presence_line(row)
        page = escape(view["artifact_id"])
        if view["on"]:
            toggle = (
                f"<span>Showing what changed since v{int(view['base'])}</span>"
                f'<a href="/a/{page}?changes=0">Hide changes</a>'
            )
        else:
            toggle = f'<a href="/a/{page}?changes=1">Show changes</a>'
        out = (
            f'<div class="rround"><span class="rtag">Round {int(view["n"])}</span>'
            f"{toggle}</div>" + _presence_line(row)
        )
        if view["on"] and view["reason"]:
            out += (
                '<p class="rhint">Showing the page as it now is: '
                f"{escape(view['reason'])}</p>"
            )
        carried = view["carried"]
        if carried:
            items = []
            for mark, state in carried:
                cls = "still" if state == comments.STILL else ""
                what = (
                    f"\u201c{escape(' '.join(mark.quote.split())[:160])}\u201d"
                    if mark.quote
                    else "General feedback about the page"
                )
                items.append(
                    f'<li><span class="cstate {cls}">{escape(state)}</span>{what}</li>'
                )
            out += (
                '<div class="carried"><b>Your marks from earlier rounds</b>'
                f"<ul>{''.join(items)}</ul></div>"
            )
        return out

    def _context_block(row: decisions.DecisionRow) -> str:
        if getattr(row, "kind", "question") == "review":
            return ""
        meta = _question_meta(row, now)
        if row.urgency == "when_you_can" and not (
            row.topic or row.for_offices or row.needed_by
        ):
            meta = []
        ctx = f'<p class="qctx">{escape(row.context)}</p>' if row.context else ""
        line = f'<p class="qmeta">{"".join(meta)}</p>' if meta else ""
        return ctx + line

    def _route_line(row: decisions.DecisionRow) -> str:
        """Where his answer goes and whether anyone waits for it: one line under
        an OPEN question, worded in `listening.WORDING`. Nothing when the caller
        gave no route (a settled card, a review, a page that does not look)."""
        route = (routes or {}).get(row.id)
        if route is None or row.answer is not None:
            return ""
        return (
            f'<p class="qroute {escape(route.state)}"'
            f' title="{escape(listening.SEEN_TITLE)}">'
            f"{escape(listening.line(route, now))}</p>"
        )

    def _card(row: decisions.DecisionRow, anchorable: bool = True) -> str:
        """`anchorable` carries the id the page's script matches a slot against.
        Only an OPEN decision gets one: a settled decision lives inside a collapsed
        <details> in the timeline, and a page that reported a slot for it would
        otherwise yank that card out of its own disclosure and drop it on the page."""
        is_review = getattr(row, "kind", "question") == "review"
        tag = '<span class="rtag">Review</span> ' if is_review else ""
        live = row.review_mode != "held"
        hint = (
            '<p class="rhint">'
            + (
                "Each mark reaches the agent as you make it, and all of them go "
                "with your verdict. Your first verdict is final."
                if live
                else "Your marks on this page go with your verdict. "
                "Your first verdict is final."
            )
            + "</p>"
            if is_review and row.answer is None
            else ""
        )
        anchor = f' data-decision-id="{escape(row.id)}"' if anchorable else ""
        if anchorable and row.answer is None and not is_review:
            anchor += f' id="q-{escape(row.id)}"'
        if row.held_by and row.answer is None and not is_review:
            # Held: no form at all, one line saying what it waits on. It keeps
            # its data-decision-id, so a slot for it still gets this note.
            first = row.held_by[0]
            more = f" (and {len(row.held_by) - 1} more)" if len(row.held_by) > 1 else ""
            return (
                f'<div class="ask held"{anchor}><p class="q">{escape(row.question)}</p>'
                '<p class="hint">Waiting on an earlier question: '
                + (
                    f'<a href="#q-{escape(first)}">see it</a>'
                    if first in on_page
                    else "it is on another page"
                )
                + f"{more}. This one opens when it is answered.</p></div>"
            )
        if is_review and row.answer is None:
            hint = _round_block(row) + hint + _mode_switch(row, held_count)
        if is_review and row.answer is not None:
            # One verdict counts: a settled review has no form.
            label = next(
                (o["label"] for o in row.options if o["letter"] == row.answer),
                row.answer,
            )
            return (
                f'<div class="ask"{anchor}>'
                f'<p class="q">{tag}{escape(row.question)}</p>'
                f'<p class="verdict">Your verdict: <b>{escape(label)}</b>'
                f" &middot; {escape(_when(row.answered_at or ''))}</p></div>"
            )
        buttons = "".join(_button(option, row) for option in row.options)
        # The round this form was drawn for: a tab left open while a republish
        # opened the next round must not have its verdict land on that one.
        round_n = (rounds or {}).get(row.id) if is_review else None
        round_field = (
            f'<input type="hidden" name="round" value="{int(round_n)}">'
            if round_n is not None
            else ""
        )
        # And the version the page view showed: the next round's changes are
        # drawn from what he actually looked at when he sent it back.
        if is_review and shown_version is not None:
            round_field += (
                f'<input type="hidden" name="version" value="{int(shown_version)}">'
            )
        return (
            f'<div class="ask"{anchor}>'
            f'<p class="q">{tag}{escape(row.question)}</p>'
            f"{_context_block(row)}"
            f"{_route_line(row)}"
            f"{_since(row)}"
            f"{hint}"
            f'<form method="post" action="/d/{escape(row.id)}/answer">'
            f"{round_field}{buttons}"
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

    answerable = sorted(
        (
            r
            for r in open_rows
            if getattr(r, "kind", "question") == "question" and not r.held_by
        ),
        key=lambda r: _inbox_key(r, now),
    )
    if answerable:
        # Its own element, outside #open-asks: gallery.js hides that section once
        # every question has been lifted into the page, and this list must stay.
        items = "".join(
            f'<li><a href="#q-{escape(r.id)}">{escape(r.question)}</a>'
            f'<span class="lmeta">{" &middot; ".join(_question_meta(r, now))}</span></li>'
            for r in answerable
        )
        above.append(
            '<nav class="openlist" id="open-list" aria-label="Open questions">'
            f"<h2>Open questions ({len(answerable)})</h2><ul>{items}</ul></nav>"
        )

    if open_rows:
        # A review's first verdict is final, so the "pick a different one" hint
        # belongs only to ordinary questions; a review card carries its own.
        hint = (
            ""
            if all(
                getattr(r, "kind", "question") == "review" or r.held_by
                for r in open_rows
            )
            else (
                '<p class="hint">Pick a letter and it is saved straight away; add a note '
                "if the options do not fit. Your answer is the filled button &mdash; pick "
                "a different one to change it, as often as you like.</p>"
            )
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
        any_fresh = False
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
            if row.answer_source == "slack":
                relayed = (
                    '<span class="relayed" title="Answered in Slack, verified by '
                    'quarterdeck; not tapped on this page">via Slack</span>'
                )
            elif row.answer_source and row.answer_source != "page":
                where = escape(row.answer_source)
                who = escape(row.recorded_by or "an agent")
                relayed = (
                    f'<span class="relayed" title="Recorded by {who} from '
                    f'{where}; not tapped on this page">via {where}</span>'
                )
            thread_html, fresh = _note_thread(row)
            any_fresh = any_fresh or fresh
            entries.append(
                f'<details class="settled"'
                f"{' open' if fresh else ''}>"
                f'<summary><span class="when">{escape(_when(row.answered_at))}</span>'
                f'<span class="sq">{escape(row.question)}</span>'
                f'<span class="sa">({escape(row.answer)}) {escape(chosen)}</span>'
                f"{relayed}{unread}"
                # Changes requested, and the review is stalled until a session
                # revises: who is on it, or who handed it back and why, where
                # he sees it without opening the entry.
                + (_presence_line(row, "span") if row.answer == "c" else "")
                + "</summary>"
                f'<div class="settled-body">{_provenance(row)}{pickup}{note}'
                f"{thread_html}"
                # SAID WHERE THE HAND IS. The section already carries "nothing
                # here is locked", but that sits at the foot of the whole list,
                # after every other entry -- so the card you have actually
                # opened says nothing about being changeable, and the buttons
                # below read as a record rather than a control. Jeremy,
                # 22 Sep 2026: "at least the ability to change the answer if we
                # press a button." It has worked since 19 September and was
                # proved again on the live service that morning; what was
                # missing was anything on screen saying so at the moment of
                # looking.
                + (
                    ""
                    if row.closed_at is not None
                    else '<p class="hint">Your first verdict is final.</p>'
                    if getattr(row, "kind", "question") == "review"
                    else '<p class="hint">Tap a different letter to change this '
                    "&mdash; it replaces your answer, and the note with it.</p>"
                )
                + ("" if row.closed_at is not None else _card(row, anchorable=False))
                + "</div></details>"
            )
        # Collapsed, and counted in its own summary. Four settled questions took
        # the whole first screen of a page whose point was the one still open;
        # closed, the same four are a single line he can open when he wants them.
        n = len(entries)
        noun = "question" if n == 1 else "questions"
        below.append(
            f'<details class="asks settled-list"{" open" if any_fresh else ""}>'
            "<summary><h2>"
            f"{n} {noun} you have already settled</h2></summary>"
            + "".join(entries)
            + (
                ""
                if all(getattr(r, "kind", "question") == "review" for r in settled_rows)
                else '<p class="hint">Open one to see the options again, or to change '
                "your mind &mdash; nothing here is locked.</p>"
            )
            + "</details>"
        )

    return "".join(above), "".join(below)


def gallery_router(db: Database) -> APIRouter:
    router = APIRouter()
    forms_ui.register(
        router, db, same_origin=_same_origin_write, headers=gallery_headers
    )

    @router.get("/gallery.js")
    def gallery_script() -> Response:
        # Same header set as every other gallery response (gallery_headers() is cheap
        # and read at call time) -- one hardened profile for the whole origin, rather
        # than a script route that is the one response nobody checked.
        # no-cache (homelab-ij8.94): revalidate every time, so a fix to the
        # decision control is never stuck behind a heuristically cached copy.
        return Response(
            _GALLERY_JS,
            media_type="text/javascript; charset=utf-8",
            headers={**gallery_headers(), "cache-control": "no-cache"},
        )

    def _wall(
        conn, archived: bool, folder_id: str | None = None, grouped: bool = False
    ):
        """The pages of the live wall, or the archived ones.

        `folder_id` lists only that folder's pages. `grouped` leaves out the pages a folder
        tile stands for: a page in a folder that is not archived (live wall), or in an
        ARCHIVED folder (archived tab). A page archived on its own out of a live folder
        has no tile to hide behind, so it stays in the flat archived list.
        """
        if folder_id is not None:
            scope, extra = " and a.folder_id = %s", (folder_id,)
        elif grouped:
            scope = (
                " and (a.folder_id is null or "  # noqa: S608
                + ("not " if archived else "")
                + "exists (select 1 from folder f where f.id = a.folder_id"
                "  and f.archived_at is not null))"
            )
            extra = ()
        else:
            scope, extra = "", ()
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
            "select a.id, a.title, a.updated_at, a.agent,"  # noqa: S608
            "       (select count(*) from version v"
            "          where v.artifact_id = a.id) as versions,"
            "       (select count(*) from decision d"
            "          where d.artifact_id = a.id"
            "            and d.answer is null and d.kind <> 'form'"
            # A question SET ASIDE (its page archived or binned) is not waiting.
            "            and a.archived_at is null and a.deleted_at is null"
            "            and d.closed_at is null) as waiting,"
            # The CURRENT version's text: the only one whose `body` is always plain.
            "       (select v.body from version v"
            "          where v.artifact_id = a.id and v.n = a.current_version"
            "       ) as body,"
            # The boolean only. Selecting the bytes here would pull every
            # thumbnail on the wall into memory to render a list of links.
            "       exists (select 1 from version v"
            "          where v.artifact_id = a.id and v.n = a.current_version"
            "            and v.thumbnail is not null) as has_thumb,"
            "       a.pinned_at,"
            # WHICH SESSION last changed it, not which first published it: the
            # tile already says "Edited 5h ago" beside this, and the question a
            # reader is asking at that moment is who did THAT. Jeremy, 21 Sep,
            # choosing (b) over my recommendation: on every tile, "so you can
            # see it without opening anything".
            "       (select v.session from version v"
            "          where v.artifact_id = a.id"
            "            and v.n = a.current_version) as session,"
            # UNREAD: new, or changed since he last opened it (homelab-ij8.103).
            f"       {_WALL_UNREAD} as unread,"
            # WHICH PART CHANGED, when the live version is a section update
            # (Jeremy, 6 Oct, ruling 1a: "Running now: added"), and whether
            # its publisher asked for his eye. NULL/false for a whole-page
            # version, which says nothing about a part.
            "       (select v.section from version v"
            "          where v.artifact_id = a.id"
            "            and v.n = a.current_version) as part,"
            "       coalesce((select v.notify from version v"
            "          where v.artifact_id = a.id"
            "            and v.n = a.current_version), false) as flagged,"
            f"       {_WALL_REPLIED_BY} as replied_by"
            "  from artifact a"
            " where (a.archived_at is null) = %s and a.deleted_at is null"
            + scope
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
            + " order by a.pinned_at desc nulls last,"  # noqa: S608
            "          (select count(*) from decision d"
            "             where d.artifact_id = a.id"
            "               and d.answer is null and d.kind <> 'form'"
            "               and a.archived_at is null and a.deleted_at is null"
            "               and d.closed_at is null) > 0 desc,"
            # Then what he has not read yet, newest first; once he opens a page
            # it drops back into the order below (homelab-ij8.103, his ruling).
            f"          {_WALL_UNREAD} desc,"
            "          a.updated_at desc, a.id desc limit 200",
            (not archived, *extra),
        ).fetchall()

    def _folder_pages(conn):
        """Every page that is in a folder and not binned, one line each, with the folder's
        own fields: the facts a folder tile is made of. Not capped like `_wall`, so a folder
        of a thousand daily digests still counts them all."""
        return conn.execute(
            "select f.id, f.title, f.archived_at, a.agent, a.updated_at,"  # noqa: S608
            "       (a.archived_at is not null) as page_archived,"
            "       (a.pinned_at is not null) as pinned,"
            f"       {_WALL_UNREAD} as unread,"
            "       (select count(*) from decision d"
            "          where d.artifact_id = a.id"
            "            and d.answer is null and d.kind <> 'form'"
            "            and a.archived_at is null"
            "            and d.closed_at is null) as waiting"
            "  from artifact a join folder f on f.id = a.folder_id"
            " where a.deleted_at is null"
        ).fetchall()

    def _folder_items(pages, archived: bool) -> list:
        """The folder tiles for the live wall (`archived` False: folders not archived, their
        live pages) or for the Archived tab (archived folders, their archived pages). A
        folder with no such page is not drawn."""
        found: dict = {}
        for (
            fid,
            title,
            f_arch,
            agent,
            updated,
            p_arch,
            pinned,
            unread,
            waiting,
        ) in pages:
            if bool(f_arch) != archived or bool(p_arch) != archived:
                continue
            f = found.setdefault(fid, [title, 0, None, set(), 0, 0, False])
            f[1] += 1
            if f[2] is None or updated > f[2]:
                f[2] = updated
            f[3].add(agent)
            f[4] += waiting or 0
            f[5] += 1 if pinned else 0
            f[6] = f[6] or bool(unread)
        return [
            _FolderItem(fid, f[0], f[2], sorted(f[3]), f[1], f[4], f[5], f[6])
            for fid, f in found.items()
        ]

    def _folder_tile(item, archived: bool = False, plain: bool = False) -> str:
        """A folder as one tile: its title, how many pages, the newest page's age, who
        published them, and how many questions wait. The same card as a page's, with a menu
        of its own (rename, archive) and no pin."""
        ident = escape(item.id)
        flag = ""
        if item.waiting:
            flag = f'<span class="wait">{item.waiting} waiting</span>'
        when = _ago(item.newest)
        exact = stamp.format_for_display(item.newest) if item.newest else ""
        noun = "page" if item.count == 1 else "pages"
        bits = [f"{item.count} {noun}", ", ".join(escape(a) for a in item.agents)]
        if when:
            bits.append(f'<time title="{escape(exact)}">Edited {escape(when)}</time>')
        return (
            f'<li class="tile-wrap folder{" asking" if item.waiting else ""}'
            f'{" archived" if archived else ""}">'
            f'<a href="/f/{ident}"><span class="frame">'
            f'<span class="shot none">{_icon("all")}'
            f'<span class="d">{item.count} {noun}</span></span>{flag}</span>'
            f'<span class="body"><span class="t">{escape(item.title)}</span>'
            f'<span class="m">{_SEP.join(bits)}</span></span></a>'
            f"{'' if plain else _folder_menu(item.id, item.title, archived)}</li>"
        )

    def _folder_row(item, archived: bool = False, plain: bool = False) -> str:
        right = []
        if item.waiting:
            right.append(f'<span class="wait">{item.waiting} waiting</span>')
        noun = "page" if item.count == 1 else "pages"
        names = ", ".join(escape(a) for a in item.agents)
        right.append(f'<span class="who">{names}</span>')
        right.append(f"<span>{item.count} {noun}</span>")
        when = _ago(item.newest)
        if when:
            exact = stamp.format_for_display(item.newest) if item.newest else ""
            right.append(f'<time title="{escape(exact)}">Edited {escape(when)}</time>')
        return (
            f'<li class="row-wrap folder{" asking" if item.waiting else ""}">'
            f'<a class="row" href="/f/{escape(item.id)}">'
            f'<span class="rshot none">{_icon("all")}</span>'
            f'<span class="rt">{escape(item.title)}</span>'
            f'<span class="rmeta">{"".join(right)}</span></a>'
            f"{'' if plain else _folder_menu(item.id, item.title, archived)}</li>"
        )

    def _row_view(
        row, archived: bool = False, folder_id: str | None = None, select: bool = False
    ) -> str:
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
            session,
            unread,
            part,
            flagged,
            replied_by,
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
        if unread:
            right.insert(0, _UNREAD_WORD)
            if part:
                right.append(_part_line(part, flagged))
            if replied_by:
                right.append(_reply_line(replied_by))
        right.append(f'<span class="who">{escape(agent)}</span>')
        right.append(_session_tag(session))
        when = _ago(updated)
        if when:
            exact = stamp.format_for_display(updated) if updated else ""
            right.append(f'<time title="{escape(exact)}">Edited {escape(when)}</time>')

        # The same pin button the tile has -- the reference's row carries one
        # too (24x24, and always visible where the CARD's fades in on hover).
        # This row had only a "Pinned" label, so the list was the one view
        # where pinning meant opening a menu.
        return (
            f'<li class="row-wrap{" sel" if select else ""}{" asking" if waiting else ""}"'
            f"{_UNREAD_ATTR if unread else ''}>"
            f"{_pick(artifact_id, title) if select else ''}"
            f'<a class="row" href="/a/{ident}">'
            f'{badge}<span class="rt">{escape(title)}</span>'
            f'<span class="rmeta">{"".join(right)}</span></a>'
            + (
                ""
                if select
                else f"{_pin_button(artifact_id, bool(pinned_at))}"
                f"{_tile_menu(artifact_id, title, bool(pinned_at), gallery_origin(), archived, folder_id)}"
            )
            + "</li>"
        )

    @router.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return _wall_page(request)

    @router.get("/f/{folder_id}", response_class=HTMLResponse)
    def open_folder(folder_id: str, request: Request) -> HTMLResponse:
        # The opaque id, never the slug: a slug is only unique per owner.
        return _wall_page(request, folder_id)

    def _wall_page(request: Request, folder_id: str | None = None) -> HTMLResponse:
        query = (request.query_params.get("q") or "").strip()
        chosen_agent = (request.query_params.get("agent") or "").strip()
        chosen = request.query_params.get("filter")
        # THE WALL GROUPS, A SEARCH DOES NOT. A page in a folder is hidden behind its folder
        # tile only while he is looking at the plain wall (or the plain Archived tab). Any
        # question put to the wall -- a search, an agent, "Waiting on you", "Pinned" -- is
        # about pages, and a folder tile cannot answer it: those list the matching pages
        # flat, folders or not.
        flat = bool(query or chosen_agent or chosen in ("waiting", "pinned"))
        # SELECT MODE: `?select=1` renders the same wall with a checkbox per page inside one
        # form (below). A GET that only draws; the move itself is the POST at /select.
        selecting = request.query_params.get("select") == "1" and chosen != "deleted"
        folder_items: list = []
        in_folder = None
        with db.connect() as conn:
            if folder_id is not None:
                in_folder = conn.execute(
                    "select id, title, archived_at from folder where id = %s",
                    (folder_id,),
                ).fetchone()
                if in_folder is None:
                    raise HTTPException(404, "no such folder")
                shelved = in_folder[2] is not None
                rows = [] if shelved else _wall(conn, False, folder_id)
                archived_rows = _wall(conn, True, folder_id) if shelved else []
                pages = []
            else:
                rows = _wall(conn, archived=False, grouped=not flat)
                archived_rows = _wall(conn, archived=True, grouped=not flat)
                pages = [] if flat else _folder_pages(conn)
        # Before grouping: what is waiting on him and what is pinned count the pages in
        # folders too, so the header never says fewer than there are.
        in_live = [] if flat else _folder_items(pages, False)
        in_archived = [] if flat else _folder_items(pages, True)
        folder_waiting = sum(f.waiting for f in in_live)
        folder_pinned = sum(f.pinned for f in in_live)
        folder_agents = sorted({a for f in in_live for a in f.agents})
        folder_pages = sum(f.count for f in in_live)
        archived_folder_pages = sum(f.count for f in in_archived)
        folder_items = in_live

        # A tile per page rather than a line per page: a wall Jeremy recognises
        # the board he wants in. Jeremy chose a real rendered thumbnail (19 Sep)
        # and qualified it -- "start with (b) but then (a) if we can" -- so this
        # is (b): everything a tile can say without a browser. The thumbnail is
        # tracked separately and needs a decision about where the renderer runs,
        # because the gallery must never load an agent's HTML into this origin.
        def tile(row, archived: bool = False) -> str:
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
                session,
                unread,
                part,
                flagged,
                replied_by,
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
            if unread and flagged and not waiting:
                # The publisher asked for his eye (`notify`). On the picture,
                # where a question rides, because it is the same kind of fact:
                # something to look at without reading the tile.
                flag = '<span class="wait look">Look at this</span>'

            bits = [
                (
                    f'<li class="tile-wrap{" pinned" if pinned_at else ""}'
                    f'{" asking" if waiting else ""}"{_UNREAD_ATTR if unread else ""}>'
                ),
                _pick(artifact_id, title) if selecting else "",
                f'<a href="/a/{escape(artifact_id)}">',
                f'<span class="frame">{shot}{flag}</span>',
                '<span class="body">',
                f'<span class="t">{escape(title)}</span>',
            ]
            # Changed since he opened it, by a section update: name the part
            # (ruling 1a). Gone once he has read it -- then it is history.
            if unread and part:
                bits.append(_part_line(part, flagged))
            if unread and replied_by:
                bits.append(_reply_line(replied_by))
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
            tag = _session_tag(session)
            if tag:
                meta_bits.append(tag)
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
                f'<span class="m">{_UNREAD_WORD if unread else ""}{_SEP.join(meta_bits)}'
                + (
                    f'{_SEP}<time title="{escape(exact)}">Edited {escape(when)}</time>'
                    if when
                    else ""
                )
                + "</span>"
                "</span></a>"
                + (
                    ""
                    if selecting
                    else f"{pin_button}"
                    f"{_tile_menu(artifact_id, title, bool(pinned_at), gallery_origin(), archived, folder_id)}"
                )
                + "</li>"
            )
            return "".join(bits)

        # Which view. The query parameter wins so a link is shareable and
        # says what it shows; the cookie only supplies the default, so the view
        # he last chose is the one he gets next time without it having to be in
        # every URL.
        # Counted BEFORE filtering: the tabs have to say how many there ARE,
        # not how many survived the tab you are already on -- otherwise the
        # number on a tab changes when you press it, and stops meaning anything.
        waiting_total = sum(r[5] for r in rows) + folder_waiting
        pinned_total = sum(1 for r in rows if r[8]) + folder_pinned

        shown = (
            ("waiting", "pinned")
            if in_folder
            else (
                "waiting",
                "pinned",
                "deleted",
                "archived",
            )
        )
        active = chosen if chosen in shown else "all"
        deleted_rows = [] if in_folder else store.list_deleted(db)
        if active == "waiting":
            rows = [r for r in rows if r[5]]
        elif active == "pinned":
            rows = [r for r in rows if r[8]]
        if in_folder and active in ("waiting", "pinned"):
            # An archived folder lists its archived pages: the tab narrows those too.
            archived_rows = [
                r for r in archived_rows if r[5 if active == "waiting" else 8]
            ]

        # WHO published, as a filter. The list of choices is taken from the
        # LIVE wall before any filtering, so picking a seat never empties the
        # menu you picked it from -- a control that removes its own other
        # options is a control you cannot get out of without editing the URL.
        # Archived pages do not add names of their own: a seat whose only pages
        # are archived is not somebody you filter the wall by.
        agents = sorted({r[3] for r in rows} | set(folder_agents))
        if chosen_agent:
            # A link saved before an agent was renamed still finds its pages.
            chosen_agent = rename.resolve(db, chosen_agent)
            rows = [r for r in rows if r[3] == chosen_agent]
            archived_rows = [r for r in archived_rows if r[3] == chosen_agent]

        needle = query.lower()
        if needle:
            # Both walls are filtered. Searching only the live one would hide
            # the archived hit AND still count it in the fold's total, which
            # reads as the search having missed something.
            rows = [r for r in rows if _matches(r, needle)]
            archived_rows = [r for r in archived_rows if _matches(r, needle)]

        asked = request.query_params.get("view")
        view = (
            asked
            if asked in ("grid", "list")
            else (
                request.cookies.get(_VIEW_COOKIE)
                if request.cookies.get(_VIEW_COOKIE) in ("grid", "list")
                else "grid"
            )
        )

        def listing(these, archived: bool = False) -> str:
            """The rows, under a heading per day. The headings come from the
            data rather than from a fixed set of buckets, so a wall with a
            three-month gap in it has no empty months in the middle."""
            out, current = [], None
            for row in these:
                group = _date_group(row[2])
                if group != current:
                    if current is not None:
                        out.append("</ul>")
                    out.append(
                        f'<h2 class="daygroup">{escape(group)}</h2><ul class="rows">'
                    )
                    current = group
                out.append(
                    _folder_row(row, archived, selecting)
                    if isinstance(row, _FolderItem)
                    else _row_view(row, archived, folder_id, selecting)
                )
            if current is not None:
                out.append("</ul>")
            return "".join(out)

        def render(these, archived: bool = False) -> str:
            if view == "list":
                return listing(these, archived)
            tiles = "".join(
                _folder_tile(r, archived, selecting)
                if isinstance(r, _FolderItem)
                else tile(r, archived)
                for r in these
            )
            return f'<ul class="tiles">{tiles}</ul>'

        # The folder tiles take their place among the pages, in the same order the wall
        # keeps: pinned first, then whatever waits on him, then what he has not read,
        # then newest. A folder sorts by its newest page and "waits" when it holds a
        # question. Inside a folder there are no unread bumps: newest publish first,
        # with pinned and waiting above.
        def rank(r):
            if isinstance(r, _FolderItem):
                return (0, 0, bool(r.waiting), r.unread, r.newest, r.id)
            pinned = (1, r[8]) if r[8] else (0, 0)
            return (
                *pinned,
                bool(r[5]),
                False if in_folder else bool(r[10]),
                r[2],
                r[0],
            )

        base = f"/f/{folder_id}" if in_folder else "/"
        archived_shown = (
            sorted([*archived_rows, *in_archived], key=rank, reverse=True)
            if in_archived
            else archived_rows
        )
        archived_pages = len(archived_rows) + archived_folder_pages
        if folder_items or in_folder:
            rows = sorted([*rows, *folder_items], key=rank, reverse=True)

        # Archived pages are their own tab (Jeremy, 21 Sep: put it up with the
        # others). Still rendered in the archived skin -- dimmed thumbnails,
        # quieter titles -- so a page that is off the wall never looks like one
        # that is on it, whichever tab you reached it through.
        if active == "archived" or (in_folder and archived_shown):
            main = (
                f'<div class="archived-body">{render(archived_shown, archived=True)}</div>'
                if archived_shown
                else (
                    f'<p class="empty">Nothing archived matches <b>{escape(query)}</b>. '
                    f'<a href="/?filter=archived&view={view}">Show everything '
                    "archived</a>.</p>"
                    if query
                    else '<p class="empty">Nothing is archived.</p>'
                )
            )
        elif active == "deleted":
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
                else (
                    f' There <a href="{base}?filter=archived&q='
                    + quote(query)
                    + f'&view={view}">is a match in the archived pages</a>.'
                )
            )
            main = (
                f'<p class="empty">Nothing here matches '
                f"<b>{escape(query)}</b>.{found_archived} "
                f'<a href="{base}?view={view}">Show everything</a>.</p>'
            )
        elif archived_shown:
            main = (
                '<p class="empty">Nothing on the wall. '
                f'<a href="/?filter=archived&view={view}">'
                f"{archived_pages} archived</a>.</p>"
            )
        elif in_folder:
            main = '<p class="empty">Nothing in this folder.</p>'
        else:
            main = '<p class="empty">No pages published yet.</p>'
        body = main
        if selecting:
            keep = {
                k: v
                for k, v in request.query_params.items()
                if k in ("view", "q", "agent", "filter")
            }
            back = (f"/f/{folder_id}" if in_folder else "/") + (
                "?" + urlencode(keep) if keep else ""
            )
            body = _select_form(
                main,
                back,
                _select_message(
                    request.query_params.get("done", ""),
                    request.query_params.get("n", ""),
                ),
            )
        # "N pages" counts pages, not tiles: a folder tile stands for all of its pages.
        visible = _visible_count(active, rows, archived_rows, deleted_rows)
        if active == "archived" or (in_folder and archived_shown):
            visible = archived_pages
        elif active not in ("deleted",):
            visible += folder_pages - len(folder_items)
        response = HTMLResponse(
            _shell(
                in_folder[1] if in_folder else "Artifacts",
                body,
                meta_html=_wall_meta(waiting_total, inbox.label(*inbox.count(db))),
                controls=_controls_row(
                    view,
                    query,
                    active,
                    waiting_total,
                    pinned_total,
                    visible,
                    len(deleted_rows),
                    agents,
                    chosen_agent,
                    0 if in_folder else archived_pages,
                    base,
                    _select_toggle(request, selecting, active),
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

    @router.get("/inbox", response_class=HTMLResponse)
    def inbox_page() -> HTMLResponse:
        rows, capped = inbox.gather(db)
        if capped:
            meta = (
                f"{inbox.label(len(rows), True)} items waiting on you; "
                "showing the oldest of each kind"
            )
        else:
            noun = "item" if len(rows) == 1 else "items"
            meta = f"{len(rows)} {noun} waiting on you"
        outcomes = decisions.outcomes_for_decisions(
            db, [i.id for i in rows if i.kind == "question"]
        )
        slack_status = _render_slack_status(
            partner_in.open_copies(db, waiting_on_him=False),
            partner_in.settled_copies(db, datetime.now(UTC) - timedelta(days=7)),
        )
        return HTMLResponse(
            _shell(
                "Inbox",
                _render_inbox(rows, datetime.now(UTC), outcomes) + slack_status,
                meta=meta,
            ),
            headers={**gallery_headers(), "cache-control": "no-store"},
        )

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
        # Archiving is "put this away", so it lands back on the wall, not on
        # the page just put away (Jeremy, 28 Sep 2026: staying on the page read
        # as if nothing had happened). Un-archiving opens the page again.
        if archived == "1":
            return RedirectResponse("/", status_code=303)
        return RedirectResponse(f"/a/{artifact_id}", status_code=303)

    @router.post("/a/{artifact_id}/agents")
    def agents_page(artifact_id: str, request: Request, can_read: str = Form("1")):
        """Open a page to every agent in his space, or close it to him and its
        publisher (homelab-ij8.105, his ruling). Same guard as every other state
        change made with his session."""
        _same_origin_write(request)
        if not store.set_agents_can_read(db, artifact_id, can_read == "1", agent=None):
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

    # ---- Folder controls (docs/agent/specs/2026-11-02-folders-design.md 3.3). His, so unscoped,
    # and behind the same same-origin guard as pin and archive. Each has an agent twin on the
    # /api/v1 folder routes; none of them is reachable with an agent key.

    def _text_refused(exc: Exception) -> HTTPException:
        if isinstance(exc, store.SecretFound):
            return HTTPException(422, "that name looks like it contains a secret")
        return HTTPException(422, "that name is too long")

    @router.get("/a/{artifact_id}/folder", response_class=HTMLResponse)
    def folder_choices(artifact_id: str) -> HTMLResponse:
        """Where a page can go: his folders (any agent's page fits), the page's own agent's
        folders, or a new folder he names here. No script: every choice is a button."""
        found = store.folders_for_page(db, artifact_id)
        if found is None:
            raise HTTPException(404, "no such artifact")
        agent, current, choices = found
        with db.connect() as conn:
            row = conn.execute(
                "select title from artifact where id = %s", (artifact_id,)
            ).fetchone()
        ident = escape(artifact_id)
        buttons = "".join(
            f'<form method="post" action="/a/{ident}/folder" class="row">'
            f'<input type="hidden" name="folder" value="{escape(fid)}">'
            f'<button class="btn{" primary" if fid == current else ""}" type="submit">'
            f"{escape(title)}{' (here)' if fid == current else ''}</button>"
            f'<span class="who">{"yours" if mine else escape(agent) + "&#39;s"}</span></form>'
            for fid, title, mine in choices
        )
        out = (
            f'<form method="post" action="/a/{ident}/folder" class="row">'
            '<input type="hidden" name="folder" value="">'
            '<button class="btn" type="submit">Take out of folder</button></form>'
            if current
            else ""
        )
        body = (
            _CONNECT_CSS
            + '<div class="cx">'
            + f"<section><h2>Move {escape(row[0])} to a folder</h2>"
            + (buttons or "<p>There is no folder to move it to yet.</p>")
            + out
            + "</section>"
            + "<section><h2>New folder</h2>"
            f'<form method="post" action="/a/{ident}/folder" class="row">'
            '<input name="new_title" aria-label="Folder name" autocomplete="off"'
            ' placeholder="Folder name" required>'
            '<button class="btn primary" type="submit">Make it and move the page</button>'
            "</form></section>"
            f'<p><a class="btn" href="/a/{ident}">Cancel</a></p></div>'
        )
        return HTMLResponse(
            _shell("Move to folder", body),
            headers={**gallery_headers(), "cache-control": "no-store"},
        )

    @router.post("/a/{artifact_id}/folder")
    def folder_page(
        artifact_id: str,
        request: Request,
        folder: str = Form(""),
        new_title: str = Form(""),
    ):
        """File a page in a folder, make a new folder for it, or take it out (`folder` empty).
        Lands on the wall: the answer to "did that work?" is the page gone behind its folder."""
        _same_origin_write(request)
        name = new_title.strip()
        if new_title and not name:
            raise HTTPException(422, "a folder needs a name")
        try:
            done = store.file_page_for_reader(
                db, artifact_id, None if name else (folder or None), name or None
            )
        except (store.BodyTooLarge, store.InvalidText, store.SecretFound) as exc:
            raise _text_refused(exc) from None
        except store.NotFound:
            raise HTTPException(404, "no such folder for that page") from None
        except folders.FolderArchived:
            raise HTTPException(
                409, "that folder is archived: unarchive it first"
            ) from None
        if not done:
            raise HTTPException(404, "no such artifact")
        return RedirectResponse("/", status_code=303)

    # ---- Multi-select (docs/agent/specs/2026-11-02-folders-design.md 3.3, 8.3b). The wall's
    # `?select=1` draws a tick per page inside one form; this is where it lands. Agent twin:
    # POST /api/v1/artifacts/move. Same guard, same store rules, one transaction per batch.

    def _back_url(back: str) -> str:
        """Where to return to: only this wall (`/` or `/f/<id>`, with a plain query), never a
        caller-chosen address."""
        if re.fullmatch(
            r"/(f/[A-Za-z0-9_-]{1,64})?(\?[A-Za-z0-9_=&.%+-]{0,300})?", back
        ):
            return back
        return "/"

    def _select_back(back: str, done: str, n: int = 0) -> RedirectResponse:
        """Back to the wall in select mode with a result code (never free text), so he can
        keep going."""
        base, _, query = _back_url(back).partition("?")
        params = [
            p
            for p in query.split("&")
            if p and p.split("=")[0] not in ("select", "done", "n")
        ]
        params += ["select=1", f"done={done}", f"n={n}"]
        return RedirectResponse(f"{base}?{'&'.join(params)}", status_code=303)

    def _noun(n: int) -> str:
        return f"{n} page" if n == 1 else f"{n} pages"

    @router.post("/select")
    def select_pages(
        request: Request,
        page: Annotated[list[str] | None, Form()] = None,
        do: str = Form("choose"),
        folder: str = Form(""),
        new_title: str = Form(""),
        back: str = Form("/"),
    ):
        """What the ticked pages do. `do=choose` draws a page of choices and changes nothing;
        `do=out` takes them out of their folders; `do=move` files them in `folder` or in a
        new folder called `new_title`. All of the pages move or none."""
        _same_origin_write(request)
        pages = list(dict.fromkeys(p for p in (page or []) if p))
        if not pages:
            return _select_back(back, "none")
        if len(pages) > store.MAX_FILE_BATCH:
            raise HTTPException(
                422, f"at most {store.MAX_FILE_BATCH} pages can be moved at once"
            )
        if do == "choose":
            found = store.folders_for_pages(db, pages)
            if found is None:
                raise HTTPException(404, "one of those pages is gone")
            agents, choices = found
            return _select_choices(pages, agents, choices, back)
        if do not in ("out", "move"):
            raise HTTPException(422, "unknown action")
        # `out` never creates or chooses a folder, whatever else was posted with it.
        name = new_title.strip() if do == "move" else ""
        if do == "move" and new_title and not name:
            raise HTTPException(422, "a folder needs a name")
        if do == "move" and not name and not folder:
            raise HTTPException(422, "choose a folder")
        try:
            moved = store.file_pages_for_reader(
                db,
                pages,
                None if (do == "out" or name) else folder,
                name or None,
            )
        except store.BatchTooLarge:
            raise HTTPException(
                422, f"at most {store.MAX_FILE_BATCH} pages can be moved at once"
            ) from None
        except (store.BodyTooLarge, store.InvalidText, store.SecretFound) as exc:
            raise _text_refused(exc) from None
        except store.NotFound:
            raise HTTPException(
                404, "a page or folder is gone, or that folder is not for all of them"
            ) from None
        except folders.FolderArchived:
            raise HTTPException(
                409, "that folder is archived: unarchive it first"
            ) from None
        return _select_back(
            back, "out" if do == "out" else ("new" if name else "moved"), moved
        )

    def _select_choices(
        pages: list[str], agents: list[str], choices: list, back: str
    ) -> HTMLResponse:
        """Where a batch can go: his folders, and the one agent's when all the pages are that
        agent's (agents' folders stay one agent each), or a new folder named here."""

        def hidden() -> str:
            return (
                "".join(
                    f'<input type="hidden" name="page" value="{escape(p)}">'
                    for p in pages
                )
                + f'<input type="hidden" name="back" value="{escape(_back_url(back))}">'
            )

        buttons = "".join(
            '<form method="post" action="/select" class="row">'
            f'{hidden()}<input type="hidden" name="do" value="move">'
            f'<input type="hidden" name="folder" value="{escape(fid)}">'
            f'<button class="btn" type="submit">{escape(title)}</button>'
            f'<span class="who">{"yours" if mine else escape(agents[0]) + "&#39;s"}</span></form>'
            for fid, title, mine in choices
        )
        body = (
            _CONNECT_CSS
            + '<div class="cx">'
            + f"<section><h2>Move {_noun(len(pages))} to a folder</h2>"
            + (buttons or "<p>There is no folder to move them to yet.</p>")
            + (
                ""
                if len(agents) == 1
                else "<p>These pages are from several agents, so only your own folders "
                "are offered.</p>"
            )
            + "</section><section><h2>New folder</h2>"
            '<form method="post" action="/select" class="row">'
            f'{hidden()}<input type="hidden" name="do" value="move">'
            '<input name="new_title" aria-label="Folder name" autocomplete="off"'
            ' placeholder="Folder name" required>'
            '<button class="btn primary" type="submit">Make it and move them</button>'
            "</form></section>"
            f'<p><a class="btn" href="{escape(_back_url(back))}">Cancel</a></p></div>'
        )
        return HTMLResponse(
            _shell("Move to folder", body),
            headers={**gallery_headers(), "cache-control": "no-store"},
        )

    def _folder_or_404(folder_id: str):
        row = store.folder_by_id(db, folder_id)
        if row is None:
            raise HTTPException(404, "no such folder")
        return row

    @router.get("/f/{folder_id}/rename", response_class=HTMLResponse)
    def folder_rename_form(folder_id: str) -> HTMLResponse:
        _fid, _owner, _slug, title, _archived = _folder_or_404(folder_id)
        ident = escape(folder_id)
        body = (
            _CONNECT_CSS + '<div class="cx"><section><h2>Rename folder</h2>'
            f'<form method="post" action="/f/{ident}/rename" class="row">'
            f'<input name="title" value="{escape(title)}" aria-label="Folder name"'
            ' autofocus autocomplete="off" required>'
            '<button class="btn primary" type="submit">Save</button>'
            f'<a class="btn" href="/f/{ident}">Cancel</a></form>'
            "</section></div>"
        )
        return HTMLResponse(
            _shell("Rename folder", body),
            headers={**gallery_headers(), "cache-control": "no-store"},
        )

    @router.post("/f/{folder_id}/rename")
    def folder_rename(folder_id: str, request: Request, title: str = Form(...)):
        """Rename a folder's title. The slug, which agents type, is left alone."""
        _same_origin_write(request)
        _fid, owner, slug, _title, _archived = _folder_or_404(folder_id)
        cleaned = title.strip()
        if not cleaned:
            raise HTTPException(422, "a folder needs a name")
        try:
            store.rename_folder(db, owner, slug, None, cleaned)
        except (store.BodyTooLarge, store.InvalidText, store.SecretFound) as exc:
            raise _text_refused(exc) from None
        return RedirectResponse(f"/f/{folder_id}", status_code=303)

    @router.get("/f/{folder_id}/archive", response_class=HTMLResponse)
    def folder_archive_sheet(folder_id: str):
        """Archive folder asks first: the page count and the questions it will set aside, from
        the same preview the agent API uses. Nothing changes until the Archive button."""
        _fid, owner, slug, title, archived = _folder_or_404(folder_id)
        ident = escape(folder_id)
        if archived is not None:
            return RedirectResponse(f"/f/{folder_id}", status_code=303)
        preview = store.set_folder_archived(
            db, owner, slug, True, confirm=False, actor=folders.HIS
        )
        if preview is None:
            raise HTTPException(404, "no such folder")
        pages = preview["would_archive"]["pages"]
        asked = preview["would_archive"]["questions_set_aside"]
        text = f"{pages} {'page goes' if pages == 1 else 'pages go'} to Archived. "
        if asked:
            text += (
                f"{asked} {'question' if asked == 1 else 'questions'} waiting on you will "
                "be set aside, and the agents that asked are told; they come back when "
                "you unarchive."
            )
        else:
            text += "No questions are waiting on you. Unarchive brings the pages back."
        body = (
            _CONNECT_CSS + '<div class="cx"><section class="archive-sheet">'
            f"<h2>Archive &lsquo;{escape(title)}&rsquo;?</h2><p>{escape(text)}</p>"
            f'<form method="post" action="/f/{ident}/archive" class="row">'
            '<input type="hidden" name="archived" value="1">'
            '<input type="hidden" name="confirm" value="1">'
            '<button class="btn primary" type="submit">Archive</button>'
            f'<a class="btn" href="/f/{ident}">Cancel</a></form>'
            "</section></div>"
        )
        return HTMLResponse(
            _shell("Archive folder", body),
            headers={**gallery_headers(), "cache-control": "no-store"},
        )

    @router.post("/f/{folder_id}/archive")
    def folder_archive(
        folder_id: str,
        request: Request,
        archived: str = Form("1"),
        confirm: str = Form(""),
    ):
        """Archive a folder and its pages (questions set aside, agents told), or unarchive it.
        Archiving without `confirm` changes nothing and sends him to the sheet."""
        _same_origin_write(request)
        _fid, owner, slug, _title, _archived = _folder_or_404(folder_id)
        if archived == "1" and confirm != "1":
            return RedirectResponse(f"/f/{folder_id}/archive", status_code=303)
        store.set_folder_archived(
            db, owner, slug, archived == "1", confirm=True, actor=folders.HIS
        )
        return RedirectResponse("/", status_code=303)

    def conn_current(artifact_id: str):
        with db.connect() as conn:
            return conn.execute(
                "select current_version from artifact"
                " where id = %s and deleted_at is null",
                (artifact_id,),
            ).fetchone()

    @router.get("/a/{artifact_id}/current")
    def current_version(
        artifact_id: str,
        since: int = -1,
        wait: float = Query(0, ge=0, le=_MAX_WATCH_SECONDS),
    ) -> dict:
        """Which version of this page is live, HELD until it is not `since`.

        homelab-ij8.74, Jeremy's own ruling on 19 Sep: a page republished while
        he is reading it should TELL him and wait, not refresh under him. This
        is the fact his open tab watches; the offer, and the waiting for his
        word, are gallery.js's.

        A HELD REQUEST, not a poll, and the bead says so: "the event feed
        already carries a held request that answers the moment something
        changes, and this is a reader-side use of the same machinery agents
        use". A tab therefore learns within a second of a republish while
        costing one row read every half second it is actually open, and
        gallery.js keeps its own rule of having no timer in it at all.

        Returns as soon as the version differs, when the hold runs out (the
        caller simply asks again), or the instant the pod is asked to stop --
        a rollout must never wait out somebody's browser.

        Bounded like the agent-facing waiter: past `_MAX_WATCHERS` held reads
        this answers at once rather than queueing, because an answer now is
        always better than a thread later.
        """
        row = conn_current(artifact_id)
        if row is None:
            raise HTTPException(404, "no such artifact")
        if wait <= 0 or int(row[0]) != since or not _watchers.acquire(blocking=False):
            return {"n": int(row[0])}
        try:
            deadline = time.monotonic() + wait
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or events.wait_for_shutdown(min(0.5, remaining)):
                    break
                row = conn_current(artifact_id)
                if row is None or int(row[0]) != since:
                    break
        finally:
            _watchers.release()
        return {"n": int(row[0]) if row else since}

    def _open_round(artifact_id: str, changes: str):
        """The open review's round when it is round 2 or later, and the base
        version the frame's changes view is drawn against (None when he turned
        the changes off, or there is nothing to draw them against).

        (review, round, changes_from), or (None, None, None)."""
        with db.connect() as conn:
            found = conn.execute(
                "select id from decision where artifact_id = %s and kind = 'review'"
                " and answer is null and closed_at is null"
                " order by created_at desc limit 1",
                (artifact_id,),
            ).fetchone()
            rnd = decisions.current_round(conn, found[0]) if found else None
        if rnd is None or rnd.n < 2:
            return None, None, None
        base = rnd.base_version if changes != "0" else None
        return found[0], rnd, base

    def _bodies(artifact_id: str, ns: list[int]) -> dict[int, str]:
        with db.connect() as conn:
            live = {
                n
                for (n,) in conn.execute(
                    "select n from version where artifact_id = %s"  # noqa: S608 -- placeholders only
                    f" and n in {in_list(ns)} and status = 'live'",
                    (artifact_id, *ns),
                ).fetchall()
            }
            return store.version_bodies(conn, artifact_id, sorted(live))

    @router.get("/a/{artifact_id}", response_class=HTMLResponse)
    def detail(
        artifact_id: str,
        # "This page" or "All pages" in the comments drawer (homelab-ij8.18).
        # A query parameter rather than a separate route, because it is a
        # filter on a panel that is already open: the fragment #comments keeps
        # the drawer open across the round trip, and the result is a URL he can
        # keep, which a script-driven toggle would not be.
        comments_scope: str = Query("this", alias="comments", pattern="^(this|all)$"),
        # Round 2 and later open on what changed; "0" is his Hide changes.
        changes: str = Query("1", pattern="^(0|1)$"),
    ) -> HTMLResponse:
        # Look the artifact up rather than framing whatever the URL said: an
        # unchecked id renders a frame pointing at an invented subdomain, and
        # the title is wanted on the page anyway.
        with db.connect() as conn:
            row = conn.execute(
                "select title, read_token, agent, archived_at, updated_at,"
                " current_version, agents_can_read, deleted_at"
                " from artifact where id = %s",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "no such artifact")
        # He has opened it: the wall stops calling it unread, and its agent can
        # see that he has (homelab-ij8.103).
        # What he had seen before this visit: a reply under his note is "new"
        # against it, and mark_read is about to move it.
        with db.connect() as conn:
            prev = conn.execute(
                "select read_at from page_read where artifact_id = %s",
                (artifact_id,),
            ).fetchone()
        prev_read_at = prev[0] if prev else None
        store.mark_read(db, artifact_id)
        # Open or answered-but-not-closed decisions: a closed one is the agent
        # saying it no longer needs an answer, so it has nothing left to show
        # here -- unless he left a note and a thread grew under it, which is a
        # conversation and stays readable.
        every_row = decisions.list_for_artifact(
            db, artifact_id, include_answered=True, include_closed=True
        )
        # A form decision is not drawn until the gallery can submit it (forms, slice 2).
        every_row = [r for r in every_row if r.kind != "form"]
        # SET ASIDE (open, the page archived): no card while the page is away; he
        # cannot answer it, and it comes back on unarchive. Answered ones still show.
        decision_rows = [
            r for r in every_row if r.closed_at is None and not r.set_aside
        ]
        note_threads = comments.note_threads(db, artifact_id)
        shown_rows = decision_rows + [
            r
            for r in every_row
            if r.closed_at is not None and r.answer and r.id in note_threads
        ]
        # The drawer is about the page: a thread under a decision's note lives
        # under that decision, never in the drawer or its count.
        comment_rows = [
            c
            for c in comments.list_for_artifact(
                db, artifact_id, include_resolved=True, include_held=True
            )
            if (c.anchor or {}).get("kind") != "decision"
        ]
        # ROUND 2 AND LATER (page review step 4): the frame opens on what
        # changed since the version he sent back, and the card says the round,
        # offers Hide/Show changes and lists his earlier marks. The fallback
        # reason is read here, with the same cached call the frame makes, so
        # it is on the card with scripts off.
        review_id, rnd, changes_from = _open_round(artifact_id, changes)
        round_view = None
        asked = None
        if rnd is not None:
            live_n = int(row[5])
            wanted = [live_n] + ([changes_from] if changes_from is not None else [])
            bodies = _bodies(artifact_id, wanted)
            reason = None
            if changes_from is not None and changes_from in bodies:
                _doc, reason = pagediff.render_changes_safely(
                    bodies[changes_from], bodies.get(live_n, "")
                )
            else:
                changes_from = None
            asked = next(
                (r.created_at for r in decision_rows if r.id == review_id), None
            )
        # A review that closed between the two reads has no round view.
        if rnd is not None and asked is not None:
            round_view = {
                review_id: {
                    "artifact_id": artifact_id,
                    "n": rnd.n,
                    "on": changes_from is not None,
                    "base": changes_from,
                    "reason": reason,
                    "carried": comments.carried_marks(
                        comment_rows, rnd.n, asked, bodies.get(live_n, "")
                    ),
                }
            }
        presence = {
            r.id: found
            for r in decision_rows
            if getattr(r, "kind", "") == "review"
            and r.answer in (None, "c")
            and r.closed_at is None
            and (found := decisions.round_presence(db, row[2], r.id)) is not None
        }
        asks_above, settled_below = _render_decisions(
            shown_rows,
            note_threads=note_threads,
            prev_read_at=prev_read_at,
            round_view=round_view,
            presence=presence,
            listening_since=events.last_read(db, row[2]),
            routes=listening.for_decisions(
                db,
                row[2],
                [
                    r.id
                    for r in shown_rows
                    if getattr(r, "kind", "question") == "question" and r.answer is None
                ],
            ),
            outcomes=decisions.outcomes_for_artifact(db, artifact_id),
            held_count=(
                holds.held_count(db, artifact_id)
                if any(
                    getattr(r, "kind", "") == "review"
                    and r.answer is None
                    and r.closed_at is None
                    for r in decision_rows
                )
                else 0
            ),
            shown_version=row[5],
            rounds=decisions.current_round_numbers(
                db,
                [
                    r.id
                    for r in decision_rows
                    if getattr(r, "kind", "") == "review" and r.answer is None
                ],
            ),
        )
        # A fresh pass per view, good once and for minutes (homelab-ij8.101), minted only
        # on a page Jeremy's Authelia session can load. It travels in the iframe's src, and
        # the CONTENT app's Referrer-Policy: no-referrer keeps it out of a Referer; spent on
        # first load, it is worthless afterwards anyway. The response is no-store (below):
        # an HTML page carrying a single-use pass must never be replayed from a cache.
        src = escape(
            content_url(
                artifact_id,
                frame_pass.mint(db, artifact_id, changes_from=changes_from),
            )
        )
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
        open_threads = sum(
            1 for c in comment_rows if c.thread_root is None and c.resolved_at is None
        )
        # Only when he asks for it. `across_pages` reads the CURRENT BODY of
        # every page that has a quoting comment, to say whether each quote is
        # still there, and doing that on every page view to render a drawer
        # nobody opened would be a real cost for no answer.
        everywhere = (
            comments.across_pages(db, include_resolved=True)
            if comments_scope == "all"
            else None
        )
        menu = _page_menu(
            artifact_id,
            row[0],
            row[2],
            archived,
            _when(str(row[4])) if row[4] else "just now",
            agents_can_read=row[6],
            shared=store.share_token(db, artifact_id) is not None,
        )
        return HTMLResponse(
            _shell(
                row[0],
                f"{f'''<div class="col top">{asks_above}</div>''' if asks_above else ''}"
                f'<div class="sheet" id="artifact-sheet"'
                # WHICH VERSION IS IN FRONT OF HIM. gallery.js compares this
                # with what the server says is current and offers the new one
                # rather than swapping it (homelab-ij8.74, his own ruling).
                f' data-version="{int(row[5])}"><iframe id="artifact-frame"'
                f' src="{src}" sandbox="{sandbox}" title="{escape(row[0])}"></iframe></div>'
                f"{f'''<div class="col">{settled_below}</div>''' if settled_below else ''}"
                # No Submit bar on a page that is archived or binned: nothing there can
                # take an answer.
                f"{'' if row[3] is not None or row[7] is not None else forms_ui.block(db, artifact_id)}"
                # AFTER the page, with the settled decisions. A conversation
                # about a page is read once you have read the page; putting it
                # above would be the mistake Jeremy already named about the
                # questions -- "it feels odd to have the questions at the top".
                f"{_render_comments(comment_rows, artifact_id, everywhere, db=db)}"
                f"{_rename_dialog(artifact_id, row[0])}"
                f"{_link_dialog(artifact_id)}"
                # The open marks /comments.js posts into the frame to be
                # painted: quote and anchor only, never the note, escaped for
                # an inline script so a quote cannot end the block. A JSON DATA
                # block, never executed (so the CSP's script-src does not apply
                # to it), and outside the comments panel, which stays
                # script-free markup.
                '<script type="application/json" id="artifact-marks">'
                f"{_script_json(comments.marks_for_page(db, artifact_id))}</script>",
                # gallery.js reads the content origin straight off this iframe's own
                # `src` -- fixed ids, not a class selector, so a future second iframe
                # on this page could never be mistaken for this one.
                scripts=(
                    '<script src="/gallery.js" defer></script>'
                    '<script src="/comments.js" defer></script>'
                ),
                # The bar carries the title menu AND the comments control, which
                # is how the reference groups them: its page bar holds the title
                # with its chevron on the left and a ButtonGroup labelled
                # "Comments" among the controls on the right.
                title_html=menu,
                # Right side, in the reference's order: comments, share, full
                # screen (revealed by script), then the profile.
                actions=(
                    '<span class="acts">'
                    + _comments_button(artifact_id, open_threads)
                    + _share_button(artifact_id)
                    + _FS_BUTTON
                    + "</span>"
                ),
                bleed=True,
                full=True,
            ),
            headers={**gallery_headers(), "cache-control": "no-store"},
        )

    def _share_response(artifact_id: str, note: str = "") -> HTMLResponse:
        with db.connect() as conn:
            row = conn.execute(
                "select title from artifact where id = %s and deleted_at is null",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "no such artifact")
        return HTMLResponse(
            _shell(
                "Share a page",
                _share_page(
                    artifact_id,
                    row[0],
                    store.share_token(db, artifact_id),
                    note,
                    people=email_share.people(db, artifact_id),
                    mail_on=mail.enabled(),
                ),
                scripts='<script src="/connect.js" defer></script>',
            ),
            headers={**gallery_headers(), "cache-control": "no-store"},
        )

    @router.get("/a/{artifact_id}/share", response_class=HTMLResponse)
    def share_page(artifact_id: str) -> HTMLResponse:
        return _share_response(artifact_id)

    @router.post("/a/{artifact_id}/share", response_class=HTMLResponse)
    def share_start(artifact_id: str, request: Request) -> HTMLResponse:
        """His deliberate act, and only his: there is no agent route for it."""
        _same_origin_write(request)
        if store.start_sharing(db, artifact_id) is None:
            raise HTTPException(404, "no such artifact")
        return _share_response(
            artifact_id,
            note="Link created. Copy it and send it to whoever should read the page.",
        )

    @router.post("/a/{artifact_id}/share/email", response_class=HTMLResponse)
    def share_email(
        artifact_id: str, request: Request, email: str = Form("")
    ) -> HTMLResponse:
        """His act only: send one person their own link (homelab-ij8.16)."""
        _same_origin_write(request)
        address = email_share.clean_email(email)
        if address is None:
            return _share_response(
                artifact_id, note="That doesn't look like an email address."
            )
        with db.connect() as conn:
            row = conn.execute(
                "select title from artifact where id = %s and deleted_at is null",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "no such artifact")
        if not mail.enabled():
            return _share_response(artifact_id, note="Email isn't set up yet.")
        share, token = email_share.create(db, artifact_id, address)
        link = store.email_share_url(artifact_id, token)
        try:
            mail.send(
                address,
                f"Jeremy shared \u201c{row[0]}\u201d with you",
                f"Jeremy has shared a page with you: {row[0]}\n\n"
                f"Open it here: {link}\n\n"
                "The first time you open it on a device, you'll be asked for a code "
                "we email to this address. The link is yours: forwarding this email "
                "won't let anyone else open it.",
            )
        except mail.MailFailed:
            email_share.withdraw(db, artifact_id, share.id)
            return _share_response(
                artifact_id,
                note="The email could not be sent, so nothing was shared. Try again.",
            )
        return _share_response(
            artifact_id, note=f"Sent. {address} has their own link now."
        )

    @router.post(
        "/a/{artifact_id}/share/email/{share_id}/withdraw", response_class=HTMLResponse
    )
    def share_email_withdraw(
        artifact_id: str, share_id: str, request: Request
    ) -> HTMLResponse:
        _same_origin_write(request)
        done = email_share.withdraw(db, artifact_id, share_id)
        return _share_response(
            artifact_id,
            note=(
                "Withdrawn. Their link and every device they confirmed stop working now."
                if done
                else "They no longer had access."
            ),
        )

    @router.post("/a/{artifact_id}/share/stop", response_class=HTMLResponse)
    def share_stop(artifact_id: str, request: Request) -> HTMLResponse:
        _same_origin_write(request)
        stopped = store.stop_sharing(db, artifact_id)
        return _share_response(
            artifact_id,
            note="Stopped. The link no longer works."
            if stopped
            else "It was not shared.",
        )

    def _move_page(artifact_id: str, note: str = "") -> HTMLResponse:
        with db.connect() as conn:
            row = conn.execute(
                "select title, agent from artifact where id = %s and deleted_at is null",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "no such artifact")
        title, owner = row
        names = sorted(
            {m.agent for m in keys.machines(db) if m.revoked_at is None} - {owner}
        )
        ident = escape(artifact_id)
        options = "".join(
            f'<option value="{escape(n)}">{escape(n)}</option>' for n in names
        )
        body = (
            _CONNECT_CSS
            + '<div class="cx">'
            + (f'<section><p class="ok">{escape(note)}</p></section>' if note else "")
            + f"<section><h2>Move {escape(title)}</h2>"
            f"<p>It belongs to {escape(owner)}. Moving it keeps its address, its "
            "versions, its questions and its comments; the new agent is woken about "
            "it from now on and the old one stops being. Who wrote each version is "
            "kept as it was.</p>"
            + (
                f'<form method="post" action="/a/{ident}/move" class="row">'
                f'<select name="to" aria-label="Agent">{options}</select>'
                '<button class="btn primary" type="submit">Move it</button>'
                f'<a class="btn" href="/a/{ident}">Cancel</a></form>'
                if names
                else "<p>There is no other connected agent to move it to.</p>"
            )
            + "</section></div>"
        )
        return HTMLResponse(
            _shell("Move a page", body),
            headers={**gallery_headers(), "cache-control": "no-store"},
        )

    @router.get("/a/{artifact_id}/move", response_class=HTMLResponse)
    def move_page(artifact_id: str) -> HTMLResponse:
        return _move_page(artifact_id)

    @router.post("/a/{artifact_id}/move", response_class=HTMLResponse)
    def move_page_do(
        artifact_id: str, request: Request, to: str = Form("")
    ) -> HTMLResponse:
        """His wall, any page (homelab-ij8.87): same operation the agents have."""
        _same_origin_write(request)
        try:
            old = store.transfer(db, artifact_id, to.strip(), agent=None)
        except store.NoSuchAgent:
            return _move_page(artifact_id, note=f"{to} is not a connected agent.")
        if old is None:
            raise HTTPException(404, "no such artifact")
        return _move_page(
            artifact_id, note=f"Moved from {old} to {store.owner(db, artifact_id)}."
        )

    @router.get("/machines", response_class=HTMLResponse)
    def machines_page(revoke: str = "") -> HTMLResponse:
        return HTMLResponse(
            _shell(
                "Machines and keys", _machines_page(keys.machines(db), confirm=revoke)
            ),
            headers={**gallery_headers(), "cache-control": "no-store"},
        )

    @router.post("/machines/{key_id}/rename")
    def machine_rename(key_id: str, request: Request, label: str = Form("")):
        _same_origin_write(request)
        cleaned = " ".join(label.split())[:60]
        if not keys.set_label(db, key_id, cleaned):
            raise HTTPException(404, "no such machine")
        return RedirectResponse("/machines", status_code=303)

    @router.post("/machines/{key_id}/revoke", response_class=HTMLResponse)
    def machine_revoke(key_id: str, request: Request) -> HTMLResponse:
        """Immediate and permanent for that key; the machine can connect again
        with a new code. His pages and its pages are untouched."""
        _same_origin_write(request)
        done = keys.revoke(db, key_id)
        note = (
            "Revoked. That key no longer works."
            if done
            else "That key was already revoked."
        )
        return HTMLResponse(
            _shell("Machines and keys", _machines_page(keys.machines(db), note=note)),
            headers={**gallery_headers(), "cache-control": "no-store"},
        )

    @router.get("/comments.js")
    def comments_script() -> Response:
        return Response(
            _COMMENTS_JS,
            media_type="text/javascript; charset=utf-8",
            headers={**gallery_headers(), "cache-control": "no-cache"},
        )

    @router.get("/connect.js")
    def connect_script() -> Response:
        return Response(
            _CONNECT_JS,
            media_type="text/javascript; charset=utf-8",
            headers={**gallery_headers(), "cache-control": "no-cache"},
        )

    @router.get("/connect", response_class=HTMLResponse)
    def connect_page(code: str = "") -> HTMLResponse:
        """Step one (no code) and step two (a code he typed). homelab-ij8.102."""
        pending, message = None, ""
        if code.strip():
            pending = connect.find(db, code)
            if pending is None:
                message = (
                    "No machine is waiting with that code. Check it against your "
                    "agent's screen; codes expire after 15 minutes."
                )
        return HTMLResponse(
            _shell(
                "Connect a machine",
                _connect_page(code, pending, message),
                scripts='<script src="/connect.js" defer></script>'
                if pending is None
                else "",
            ),
            headers={**gallery_headers(), "cache-control": "no-store"},
        )

    @router.post("/connect/approve", response_class=HTMLResponse)
    def connect_approve(
        request_id: str = Form(...),
        code: str = Form(...),
        name: str = Form(""),
        _guard: None = Depends(_same_origin_write),
    ) -> HTMLResponse:
        pending = connect.find(db, code)
        if pending is None or pending.id != request_id:
            body = _connect_page(
                code, None, "That request has expired or was already decided."
            )
        else:
            agent = connect.clean_name(name)
            if agent is not None:
                # A renamed agent's old name means the agent it became, so
                # typing it can never quietly bring the old name back.
                agent = rename.resolve(db, agent)
            if agent is None:
                body = _connect_page(
                    code,
                    pending,
                    "Use 2 to 40 letters, digits or hyphens for the name.",
                    name=name,
                )
            elif keys.is_reserved_agent_name(agent):
                body = _connect_page(
                    code,
                    pending,
                    f"{agent} is {keys.RESERVED_REASON}. Pick another.",
                    name=name,
                )
            elif connect.name_in_use(db, agent):
                body = _connect_page(
                    code,
                    pending,
                    f"{agent} is already the name of a connected machine. Pick another.",
                    name=name,
                )
            elif connect.approve(db, pending.id, agent):
                body = _connect_page(
                    ok=f"Approved. {agent} will pick up its key in a few seconds; "
                    "your agent will say when it has."
                )
            else:
                body = _connect_page(
                    code, None, "That request has expired or was already decided."
                )
        return HTMLResponse(
            _shell("Connect a machine", body),
            headers={**gallery_headers(), "cache-control": "no-store"},
        )

    @router.post("/connect/deny", response_class=HTMLResponse)
    def connect_deny(
        request_id: str = Form(...),
        code: str = Form(...),
        _guard: None = Depends(_same_origin_write),
    ) -> HTMLResponse:
        pending = connect.find(db, code)
        if pending is not None and pending.id == request_id:
            connect.deny(db, pending.id)
        return HTMLResponse(
            _shell(
                "Connect a machine",
                _connect_page(ok="Denied. That machine gets nothing."),
            ),
            headers={**gallery_headers(), "cache-control": "no-store"},
        )

    @router.get("/a/{artifact_id}/frame")
    def fresh_frame(
        artifact_id: str, changes: str = Query("1", pattern="^(0|1)$")
    ) -> Response:
        """A new pass for the page in front of him, for gallery.js to re-point a
        frame whose pass was spent (homelab-ij8.101). Behind Authelia like every
        gallery route, and JSON a cross-origin page cannot read. A deleted or
        unknown page gets 404 and no pass."""
        with db.connect() as conn:
            row = conn.execute(
                "select 1 from artifact where id = %s and deleted_at is null",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "no such artifact")
        return Response(
            json.dumps(
                {
                    "src": content_url(
                        artifact_id,
                        frame_pass.mint(
                            db,
                            artifact_id,
                            changes_from=_open_round(artifact_id, changes)[2],
                        ),
                    )
                }
            ),
            media_type="application/json",
            headers={**gallery_headers(), "cache-control": "no-store"},
        )

    def _already_decided_page(exc: decisions.AlreadyDecided) -> HTMLResponse:
        row, named, current = exc.row, exc.round, exc.current
        if (
            row.closed_at is None
            and named is not None
            and named.verdict is None
            and named.closed_at is None
        ):
            # The round asked for is not one that has a verdict: a forged or
            # future number. Name the round the review is actually in.
            return HTMLResponse(
                _shell(
                    "Already decided",
                    "<p>That verdict was not for the round this review is in. "
                    f"It is now in round {named.n}. "
                    f'<a href="/a/{escape(row.artifact_id)}">Back to the page</a></p>',
                ),
                status_code=409,
                headers=gallery_headers(),
            )
        if named is not None and named.verdict is not None:
            # Rounds: name the round whose verdict counted, and, when a
            # republish has since opened a later one, say so.
            letter = next(
                (k for k, v in decisions.VERDICTS.items() if v == named.verdict),
                None,
            )
            label = next(
                (o["label"] for o in row.options if o["letter"] == letter),
                named.verdict,
            )
            later = (
                f" The page has since been revised and is now in round {current.n}."
                if current is not None and current.n != named.n
                else ""
            )
            return HTMLResponse(
                _shell(
                    "Already decided",
                    f"<p>This review already has a verdict for round {named.n}: "
                    f"<b>{escape(label)}</b>, given "
                    f"{escape(_when(named.closed_at or ''))}. That is the one "
                    f"that counts.{later} "
                    f'<a href="/a/{escape(row.artifact_id)}">Back to the page</a></p>',
                ),
                status_code=409,
                headers=gallery_headers(),
            )
        if row.answer is None:
            # Closed by the agent: there is no verdict to name.
            return HTMLResponse(
                _shell(
                    "Already decided",
                    "<p>The agent closed this review, so there is nothing left "
                    f'to switch. <a href="/a/{escape(row.artifact_id)}">'
                    "Back to the page</a></p>",
                ),
                status_code=409,
                headers=gallery_headers(),
            )
        label = next(
            (o["label"] for o in row.options if o["letter"] == row.answer),
            row.answer,
        )
        return HTMLResponse(
            _shell(
                "Already decided",
                f"<p>This review already has a verdict: <b>{escape(label)}</b>, "
                f"given {escape(_when(row.answered_at or ''))}. That is the "
                f'one that counts. <a href="/a/{escape(row.artifact_id)}">'
                "Back to the page</a></p>",
            ),
            status_code=409,
            headers=gallery_headers(),
        )

    @router.post("/d/{decision_id}/mode")
    def review_mode(
        decision_id: str,
        mode: str = Form(...),
        _guard: None = Depends(_same_origin_write),
    ):
        try:
            artifact_id, _released = holds.set_review_mode(db, decision_id, mode)
        except ValueError as exc:
            raise HTTPException(422, "mode is held or live") from exc
        except decisions.AlreadyDecided as exc:
            return _already_decided_page(exc)
        except store.NotFound as exc:
            raise HTTPException(404, "no such open review") from exc
        return RedirectResponse(f"/a/{artifact_id}", status_code=303)

    @router.post("/d/{decision_id}/answer")
    def answer_decision(
        decision_id: str,
        letter: str = Form(...),
        note: str = Form(""),
        round_field: str = Form("", alias="round"),
        version_field: str = Form("", alias="version"),
        # "inbox" when the form was drawn on his inbox: he goes back there. Any
        # other value is ignored, so this is never an open redirect.
        next_field: str = Form("", alias="next"),
        _guard: None = Depends(_same_origin_write),
    ):
        # The round the form was drawn for. A form with none was drawn before
        # rounds existed, when every review was in round 1; a question's form
        # has none either, and a question ignores it.
        try:
            asked_round = int(round_field) if round_field else 1
            shown_version = int(version_field) if version_field else None
        except ValueError as exc:
            raise HTTPException(422, "round and version are numbers") from exc
        # `by="jeremy"` is hardcoded, not read from any header -- see the module
        # docstring for why: Authelia admits exactly one subject to this host, and
        # nothing here proves the ingress strips an inbound copy of an identity
        # header before this app ever sees the request.
        try:
            row = decisions.answer(
                db,
                decision_id,
                letter=letter,
                note=note,
                by="jeremy",
                round=asked_round,
                version=shown_version,
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
        except decisions.Held as exc:
            raise HTTPException(
                409,
                "this question waits on an earlier one that is not answered yet "
                f"({', '.join(exc.by)}); answer that first, then reload the page",
            ) from exc
        except decisions.AlreadyDecided as exc:
            return _already_decided_page(exc)
        except store.NotFound as exc:
            raise HTTPException(404, "no such open decision") from exc
        if next_field == "inbox":
            return RedirectResponse("/inbox", status_code=303)
        return RedirectResponse(f"/a/{row.artifact_id}", status_code=303)

    @router.post("/pq/{question_id}/answer")
    def answer_slack_question(
        question_id: str,
        letter: str = Form(...),
        note: str = Form(""),
        _guard: None = Depends(_same_origin_write),
    ):
        """He answers a question whose home is the partner. The answer is
        recorded as PENDING and sent to the home; it settles when the home says
        so (partner_in.answer_here)."""
        try:
            partner_in.answer_here(db, question_id, letter=letter, note=note)
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
            raise HTTPException(422, "not one of this question's options") from exc
        except store.NotFound as exc:
            raise HTTPException(404, "no such open question") from exc
        return RedirectResponse("/inbox", status_code=303)

    # --- comments: the conversation happens on the page --------------------
    #
    # homelab-ij8.18. Jeremy, 19 Sep, after reading the first status page: he
    # wants the conversation with the agent to happen ON the page rather than
    # in a terminal. The storage and the agent-facing API have existed since
    # then; what did not exist is any way for HIM to write one, which made
    # "agents read the comments and reply" a loop with no beginning.
    #
    # THIS IS THE HALF THAT NEEDS NO REFERENCE DESIGN: a thread against the
    # page as a whole. Selecting text and anchoring a comment to it is the
    # other half, it is what he actually described, and the bead says to ask
    # him for the claude.ai screenshots before building that UI -- so it is
    # asked for rather than guessed at.
    #
    # Same shape as the answer form above and for the same reasons: a plain
    # POST with the same-origin guard, `author="jeremy"` hardcoded rather than
    # read from any header, and a 303 back to the page so a refresh does not
    # repost.
    @router.post("/c/{artifact_id}")
    def add_comment(
        request: Request,
        artifact_id: str,
        # Default "": a mark ("remove"/"looks_good") needs no note, and it is
        # `kind`, not the form itself, that decides whether an empty body is
        # allowed -- see the check below.
        body: str = Form(""),
        thread_root: str = Form(""),
        # Set by /comments.js when he commented on selected text (homelab-ij8.18).
        quote: str = Form(""),
        anchor: str = Form(""),
        kind: str = Form("comment"),
        # Pictures staged through POST /c/{id}/image, one repeated field each.
        image: Annotated[list[str] | None, Form()] = None,
        _guard: None = Depends(_same_origin_write),
    ):
        if kind == "comment" and not body.strip():
            raise HTTPException(422, "a comment needs some words")
        parsed_anchor = None
        if anchor:
            try:
                parsed_anchor = json.loads(anchor)
            except ValueError:
                raise HTTPException(422, "not a valid anchor") from None
        try:
            row = comments.add(
                db,
                artifact_id=artifact_id,
                author_kind="human",
                author="jeremy",
                body=body,
                thread_root=thread_root or None,
                quote=quote[:500],
                anchor=parsed_anchor,
                kind=kind,
                image_ids=image,
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
        except images.ImageRefused as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        except comments.InvalidAnchor:
            raise HTTPException(422, "not a valid anchor") from None
        except comments.InvalidKind:
            raise HTTPException(
                422,
                "kind must be comment, remove or looks_good, with a quoted "
                "range for the last two",
            ) from None
        except comments.NoSuchDecision:
            raise HTTPException(404, "no such decision") from None
        except store.NotFound as exc:
            raise HTTPException(404, "no such artifact") from exc
        note_decision = (
            row.anchor.get("decision_id")
            if isinstance(row.anchor, dict) and row.anchor.get("kind") == "decision"
            else None
        )
        if note_decision:
            # A reply under his note lives under that decision, not in the
            # drawer: back to the note (or, in place, reload to draw it).
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"reload": True}, status_code=201)
            return RedirectResponse(
                f"/a/{artifact_id}#note-{note_decision}", status_code=303
            )
        if "application/json" in request.headers.get("accept", ""):
            # The in-place post: the thread as the drawer renders it, and the
            # marks the frame should now paint.
            try:
                root = row.thread_root or row.id
                rows = comments.list_for_artifact(
                    db, artifact_id, include_resolved=True, include_held=True
                )
                payload = {
                    "html": _render_comments(
                        rows, artifact_id, only_thread=root, db=db
                    ),
                    "thread_root": root,
                    "marks": comments.marks_for_page(db, artifact_id),
                }
            except Exception:
                # The comment IS saved. A 500 here would make the script post it
                # again, so say so and let the page reload.
                logging.getLogger("artifacts.gallery").exception(
                    "in-place reply failed after the save: comment=%s", row.id
                )
                return JSONResponse({"reload": True}, status_code=201)
            return JSONResponse(payload, status_code=201)
        return RedirectResponse(f"/a/{artifact_id}#comments", status_code=303)

    @router.post("/c/{artifact_id}/image", status_code=201)
    async def add_comment_image(
        artifact_id: str,
        request: Request,
        _guard: None = Depends(_same_origin_write),
    ):
        """Stage one picture for a comment he is writing on this page: multipart, one
        file in the field `file`, the same limits and answers as the agent route
        (`api.upload_comment_image`). Returns `{image_id, ...}`; the comment form names
        the id in a repeated `image` field. Pictures are not checked for secrets."""
        if not await run_in_threadpool(store.exists, db, artifact_id):
            raise HTTPException(404, "no such artifact")
        try:
            return await image_store.stage_upload(request, db, artifact_id, "jeremy")
        except images.ImageRefused as exc:
            raise HTTPException(
                exc.status, {"field": exc.field, "detail": exc.detail}
            ) from exc

    @router.post("/c/{comment_id}/resolve")
    def resolve_comment(
        comment_id: str,
        _guard: None = Depends(_same_origin_write),
    ):
        try:
            # agent=None: the gallery owns nothing in the `agent` column's
            # sense, and Jeremy owns everything in the sense that matters. Same
            # call `add` already makes for writing.
            row = comments.resolve(db, comment_id, by="jeremy", agent=None)
        except store.NotFound as exc:
            raise HTTPException(404, "no such comment") from exc
        return RedirectResponse(f"/a/{row.artifact_id}#comments", status_code=303)

    return router
