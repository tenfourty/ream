import html as _html
import json
from collections.abc import Sequence
from datetime import datetime

from fastapi import FastAPI, Form, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from artifacts import (
    decisions,
    email_share,
    frame_pass,
    hosts,
    mail,
    pagediff,
    stamp,
    store,
    templates,
)
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

# THE CHANGES VIEW'S STYLES (page review step 4). Beside the mark styles
# (`ar-mark-style`, added by the viewer script once marks arrive), but a plain
# <style> of its own, because the insertions and deletions must show on a page
# with no marks and with scripts off. Only the changes view carries it.
DIFF_STYLE = (
    '<style id="ar-diff-style">'
    "ins.ar-diff-ins{background:rgba(46,157,87,.22);text-decoration:none}"
    "del.ar-diff-del{background:rgba(192,57,43,.18);text-decoration:line-through;"
    "user-select:none;-webkit-user-select:none}"
    "span.ar-diff-chip{user-select:none;-webkit-user-select:none;display:inline-block;margin:0 .2em;padding:0 .4em;"
    "border-radius:.6em;background:rgba(127,127,127,.18);color:#666;"
    "font:600 .7em/1.6 system-ui,sans-serif;text-decoration:none;"
    "vertical-align:middle}"
    "</style>"
)


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
      brings back the second scrollbar this is here to remove. A page wider than its
      frame adds its horizontal scrollbar's thickness for the same reason (10 Oct 2026:
      a 1060px image in a 1085px frame left an inner scrollbar the frame's full height).
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
        "function h(){var b=document.body,m=0,x=0,d=document.documentElement;"
        "if(!b)return d.scrollHeight;try{"
        "m=parseFloat(getComputedStyle(b).marginBottom)||0}catch(e){}"
        # A page wider than its frame has a horizontal scrollbar, which takes its
        # thickness off the frame's viewport; without adding it back the frame is
        # that much short and grows a SECOND vertical scrollbar (10 Oct 2026).
        "try{if(d.scrollWidth>d.clientWidth)"
        "x=Math.max(0,(window.innerHeight||0)-d.clientHeight)}catch(e){}"
        "return Math.ceil(Math.max(b.scrollHeight,b.offsetHeight,"
        "b.getBoundingClientRect().bottom+scrollY)+m+x)}"
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
        # MARKS ON THE PAGE (page review step 1, homelab-ij8.108). The gallery
        # posts the open marks -- id, kind, quote and anchor, NEVER the note
        # (comments.marks_for_page) -- and each is painted where its text is
        # through the CSS Custom Highlight API. THE AGENT'S DOM IS NOT CHANGED:
        # nothing is wrapped, moved, split or inserted around its content. The
        # single addition is one <style id="ar-mark-style"> holding the
        # ::highlight() rules -- a stylesheet, not content -- added once. No
        # Highlight API means paint nothing and report nothing.
        #
        # Placement never guesses. The stored anchor is tried first (element
        # path + offsets into that element's text, the same offsets the
        # selection leg computes, script text included, so both walks agree),
        # and accepted only if the text there still matches the quote. Failing
        # that, the quote is searched for in the page's VISIBLE text -- a
        # separate walk that skips <script>/<style>/<noscript>/<template>, so a
        # copy of the words inside a script neither makes a real quote
        # ambiguous nor gets painted -- with whitespace and case ignored,
        # because a selection's own toString() is rendered text and need not
        # match textContent byte for byte. More than one best match, even after
        # the prefix/suffix context, is unplaced. So is anything past 50 hits.
        "var mq=null,SK={SCRIPT:1,STYLE:1,NOSCRIPT:1,TEMPLATE:1};"
        # THE CHANGES VIEW (page review step 4) adds old words in
        # <del class="ar-diff-del"> and "removed" chips that are not on the
        # live page. Every walk below -- placing, searching, and the selection
        # leg's quote, offsets and path -- skips their text, and treats the
        # inserted words (<ins class="ar-diff-ins">) as the ordinary text they
        # are, so a mark made in either view is the same mark in both.
        "function DK(e){if(!e||e.nodeType!==1)return 0;var c=e.className;"
        'if(typeof c!=="string")return 0;var nm=String(e.nodeName).toUpperCase();'
        'return nm==="DEL"&&c==="ar-diff-del"?2:nm==="SPAN"&&c==="ar-diff-chip"?2:'
        'nm==="INS"&&c==="ar-diff-ins"?1:0}'
        "function DX(t){for(var e=t&&t.parentNode;e&&e.nodeType===1;e=e.parentNode)"
        "if(DK(e)===2)return 1;return 0}"
        'var LT="";'
        "function lo(c){var x=c.toLowerCase();return x.length===1?x:c}"
        'function tight(v){var r="",i,c;for(i=0;i<v.length;i++){c=v.charAt(i);'
        "if(!/\\s/.test(c))r+=lo(c)}return r}"
        'function isn(v){return typeof v==="number"&&v>=0&&v<=1e8&&v%1===0}'
        # `o+l>a` (strict) for the start: an offset exactly at the end of one
        # text node starts at 0 in the NEXT one, not at the tail of this one.
        # `LT` is the text the range covers, deleted text left out.
        "function rangeAt(el,a,z){var w=document.createTreeWalker(el,4),t,o=0,"
        'r=document.createRange(),s=0;LT="";while((t=w.nextNode())){if(DX(t))continue;'
        "var v=t.nodeValue,l=v.length;"
        "if(!s&&o+l>a){r.setStart(t,a-o);s=1;LT=v.slice(a-o,Math.min(l,z-o))}"
        "else if(s)LT+=v.slice(0,Math.min(l,z-o));"
        "if(s&&o+l>=z){r.setEnd(t,z-o);return r}o+=l}return null}"
        # The visible text, tightened, with where each kept character came
        # from. Built at most once per message, and only if a search is needed.
        'function index(){var b=document.body,x={s:"",n:[],k:[]};if(!b)return x;'
        "var w=document.createTreeWalker(b,4),t,v,i,c,p,s=[];"
        "while((t=w.nextNode())){p=t.parentNode;"
        "if(p&&SK[String(p.nodeName).toUpperCase()])continue;if(DX(t))continue;v=t.nodeValue;"
        "for(i=0;i<v.length;i++){c=v.charAt(i);if(/\\s/.test(c))continue;"
        's.push(lo(c));x.n.push(t);x.k.push(i)}}x.s=s.join("");return x}'
        "function search(m,ix){var q=tight(m.quote);if(!q)return null;"
        "if(!ix.v)ix.v=index();var x=ix.v,all=x.s,hits=[],i=all.indexOf(q);"
        "while(i>=0&&hits.length<51){hits.push(i);i=all.indexOf(q,i+1)}"
        "if(!hits.length||hits.length>50)return null;"
        'var a=m.anchor,pre=typeof a.prefix==="string"?tight(a.prefix.slice(-64)):"",'
        'suf=typeof a.suffix==="string"?tight(a.suffix.slice(0,64)):"",best=-1,bs=-1,tie=false;'
        "hits.forEach(function(h){var sc=0;"
        "if(pre&&all.slice(Math.max(0,h-pre.length),h)===pre)sc+=2;"
        "if(suf&&all.slice(h+q.length,h+q.length+suf.length)===suf)sc+=2;"
        "if(sc>bs){bs=sc;best=h;tie=false}else if(sc===bs)tie=true});"
        "if(tie)return null;"
        "var e=best+q.length-1,r=document.createRange();"
        "r.setStart(x.n[best],x.k[best]);r.setEnd(x.n[e],x.k[e]+1);return r}"
        # A quote at the 500-character cap the selection leg applies is only
        # the START of what was selected, so the stored range may be longer.
        # Only the part the quote verifies is painted: the range's end is
        # moved back to the character that completes the quote (counted the
        # way `tight` counts, one kept character per non-whitespace one), so
        # a tail past the cap -- which nothing checked, and which may have
        # been edited since -- is never painted. Painting the whole stored
        # range would be a partial guess.
        "function cut(el,r,n){var w=document.createTreeWalker(el,4),t,v,i,go=0,c=0;"
        "while((t=w.nextNode())){if(t===r.startContainer)go=1;if(!go||DX(t))continue;v=t.nodeValue;"
        "for(i=t===r.startContainer?r.startOffset:0;i<v.length;i++){"
        "if(/\\s/.test(v.charAt(i)))continue;if(++c===n){r.setEnd(t,i+1);return r}}}"
        "return null}"
        "function place(m,ix){var a=m.anchor,el=null;"
        "if(a.path){try{el=document.querySelector(a.path)}catch(x){el=null}}"
        "if(el&&isn(a.start)&&isn(a.end)&&a.end>a.start){var r=null;"
        "try{r=rangeAt(el,a.start,a.end)}catch(x){r=null}"
        "if(r){var got=tight(LT),want=tight(m.quote);"
        "if(want&&got===want)return r;"
        "if(want&&m.quote.length>=500&&got.indexOf(want)===0){"
        "try{r=cut(el,r,want.length)}catch(x){r=null}if(r)return r}}}"
        "return search(m,ix)}"
        # TEST-ONLY HOOK (page review step 1, Task 6). A page served for real is
        # never file://, so production never takes this branch -- it exists only
        # so a browser test can drive real placement (a real Range, a real
        # TreeWalker, real text search) without going through `postMessage`,
        # which a file:// page cannot send to satisfy the origin check just
        # above. It mirrors the same field checks `paint()` applies before ever
        # calling `place()`, so the hook cannot place a mark production's own
        # message handler would have refused. `typeof location` first: the
        # node fake-DOM harness in tests/js/marks.test.mjs runs this same
        # source with no `location` global at all, and a bare reference to an
        # undeclared name throws, not just reads as undefined.
        'if(typeof location!=="undefined"&&location.protocol==="file:"){'
        "window.__arPlace=function(m){"
        'if(!m||typeof m!=="object"||typeof m.quote!=="string"||!m.quote||'
        '!m.anchor||typeof m.anchor!=="object"||typeof m.anchor.path!=="string")'
        "return null;"
        "try{return place(m,{v:null})}catch(x){return null}}}"
        "function paint(list){var G={comment:new Highlight(),remove:new Highlight(),"
        "looks_good:new Highlight()},held=new Highlight(),placed=[],unplaced=[],ix={v:null},hr=[];"
        # Only the first 200 are ever placed or painted -- a real cost, worth
        # capping. Past that, a mark with a usable id is still reported
        # unplaced rather than vanishing: the gallery's list of open marks and
        # what the page actually painted must never quietly disagree (final
        # review, Item 3). A shape too broken to report at all (no string id)
        # is dropped exactly as it would be within the cap.
        "list.forEach(function(m,mi){"
        'if(!m||typeof m!=="object"||typeof m.id!=="string")return;'
        "if(mi>=200){unplaced.push(m.id);return}"
        "var r=null;"
        'if(typeof m.quote==="string"&&m.quote&&m.anchor&&typeof m.anchor==="object"'
        '&&typeof m.anchor.path==="string"){try{r=place(m,ix)}catch(x){r=null}}'
        # hasOwnProperty, not `G[m.kind]||`: a kind of "constructor" would
        # otherwise pick Object and throw on .add().
        # A held mark arrives with kind "" (the page never learns the kind)
        # and is drawn dotted, in a highlight of its own.
        "var g=m.held===true?held:Object.prototype.hasOwnProperty.call(G,m.kind)?G[m.kind]:G.comment;"
        "if(r){g.add(r);placed.push(m.id);hr.push({id:m.id,r:r})}else unplaced.push(m.id)});"
        "MR=hr;"
        'CSS.highlights.set("ar-comment",G.comment);CSS.highlights.set("ar-remove",G.remove);'
        'CSS.highlights.set("ar-good",G.looks_good);CSS.highlights.set("ar-held",held);'
        'if(!document.getElementById("ar-mark-style")){var st=document.createElement("style");'
        'st.id="ar-mark-style";st.textContent="::highlight(ar-comment){background:rgba(217,164,0,.28)}"'
        '+"::highlight(ar-remove){background:rgba(192,57,43,.22);text-decoration:line-through}"'
        '+"::highlight(ar-good){background:rgba(46,157,87,.22)}"'
        '+"::highlight(ar-held){text-decoration:underline dotted}";'
        "(document.head||document.documentElement).appendChild(st)}"
        # THE REPLY ITSELF IS CAPPED (final review, Item 3's own follow-up:
        # homelab-ij8.111, Item 2). `unplaced` can hold one id for every mark
        # in `list` -- unbounded by this function -- and Item 3 deliberately
        # keeps every one of them rather than dropping any. Sending all of
        # them back is a different cost from painting them: at most 200 ids
        # are named, and however many more there are is said as a count,
        # `overflow`, so the gallery's line can still say a true total without
        # this message growing without bound. `overflow` is present only when
        # it is greater than zero -- a page where nothing was cut says nothing
        # extra, exactly as before this existed.
        "var un=unplaced.length>200?unplaced.slice(0,200):unplaced,"
        "ov=unplaced.length>200?unplaced.length-200:0;"
        'var pm={type:"artifact-marks-placed",placed:placed,unplaced:un};'
        "if(ov>0)pm.overflow=ov;"
        "parent.postMessage(pm,"
        f"{origin})}}"
        # The whole handler body is in a try: the top-level try/catch wraps
        # only the REGISTRATION, and nothing may throw into the page's console.
        # Marks that arrive before the page has loaded wait for it, or text
        # further down would be reported unplaced; the latest set wins.
        'addEventListener("message",function(e){try{'
        f"if(e.origin!=={origin})return;"
        "var d=e.data;"
        'if(!d||d.type!=="artifact-marks"||!Array.isArray(d.marks))return;'
        "if(!(window.CSS&&CSS.highlights&&window.Highlight))return;"
        'if(document.readyState==="complete"){mq=null;paint(d.marks);return}'
        'if(!mq)addEventListener("load",function(){try{var l=mq;mq=null;if(l)paint(l)}catch(x){}});'
        "mq=d.marks}catch(x){}});"
        # HOVERING A MARK (page view, Task 3). The gallery draws a card with the
        # comment beside marked text the pointer rests on; the card is the
        # gallery's, never this page's, so the page must never hold a note. It
        # says only WHICH mark (an id the gallery gave it) and WHERE (the client
        # rect of the line under the pointer): `artifact-mark-hover` on
        # entering, a bare `artifact-mark-leave` on leaving. No text, ever.
        # Hit-testing uses the very ranges painted above (`MR`), via
        # getClientRects -- nothing in the agent's DOM is touched or listened
        # to beyond window-level pointer events. At most one check per
        # animation frame, however fast the pointer moves; a touch tap is
        # checked at once. A hover is re-sent on the next move once the line
        # has moved (the gallery scrolled), so the card can come back. The
        # frame's OWN scroll (full screen, where it scrolls itself) moves the
        # text from under the pointer with no move at all: that is a leave,
        # and it clears what was last said, so the next move re-hovers.
        'var MR=[],hv=null,hk="",raf=0,px=-1,py=-1;'
        "function hit(){for(var i=0;i<MR.length;i++){var rs=MR[i].r.getClientRects();"
        "for(var j=0;j<rs.length;j++){var q=rs[j];"
        "if(px>=q.left&&px<=q.left+q.width&&py>=q.top&&py<=q.top+q.height)"
        "return{id:MR[i].id,rect:{top:q.top,left:q.left,width:q.width,height:q.height}}}}"
        "return null}"
        'function out(){if(hv){hv=null;hk="";'
        f'parent.postMessage({{type:"artifact-mark-leave"}},{origin})}}}}'
        "function look(){raf=0;try{var h=hit();if(!h){out();return}"
        'var k=h.id+"|"+h.rect.top+"|"+h.rect.left;if(k===hk)return;hv=h.id;hk=k;'
        'parent.postMessage({type:"artifact-mark-hover",id:h.id,rect:h.rect},'
        f"{origin})}}catch(x){{}}}}"
        'function at(e){if(!e||typeof e.clientX!=="number"||typeof e.clientY!=="number")return 0;'
        "px=e.clientX;py=e.clientY;return 1}"
        'addEventListener("pointermove",function(e){try{if(!at(e)||raf)return;raf=1;'
        'if(typeof requestAnimationFrame==="function")requestAnimationFrame(look);else look()}catch(x){}});'
        'addEventListener("pointerdown",function(e){try{if(at(e)&&e.pointerType==="touch")look()}catch(x){}});'
        'addEventListener("pointerout",function(e){try{if(e&&!e.relatedTarget)out()}catch(x){}});'
        'addEventListener("scroll",function(){try{out();hk=""}catch(x){}});'
        # SELECTING TEXT TO COMMENT ON IT (homelab-ij8.18). The gallery cannot
        # read inside this cross-origin page, by design, so the page reports a
        # selection: the quoted text (capped), an anchor the service already
        # validates -- an element path plus character offsets within that
        # element's text -- and where it sits, so the gallery can put a Comment
        # button under it. Nothing else from the page is ever sent, and only to
        # the gallery's origin. An empty selection says so, so the button goes.
        "var st=0;function sel(){var s=getSelection(),"
        't=s&&s.rangeCount?String(s).trim():"";'
        'if(!t){parent.postMessage({type:"artifact-selection",quote:""},'
        f"{origin});return}}"
        "var r=s.getRangeAt(0),n=r.commonAncestorContainer;"
        "if(n.nodeType!==1)n=n.parentElement;while(n&&DK(n))n=n.parentElement;if(!n)return;"
        "var p=[],e=n;while(e&&e!==document.body&&e.nodeType===1){"
        "var i=1,x=e;while((x=x.previousElementSibling))if(x.tagName===e.tagName&&!DK(x))i++;"
        'p.unshift(e.tagName.toLowerCase()+":nth-of-type("+i+")");e=e.parentElement}'
        'var path=("body"+(p.length?">"+p.join(">"):"")).slice(0,512);'
        # Offsets into `n`'s text as the LIVE page has it: counted by one walk
        # that leaves deleted text out (on any other page, the same numbers
        # a Range's own toString would give). Where deleted text was inside
        # the selection, the quote is read from the live text too.
        'var LV="",LD=0,w=document.createTreeWalker(n,4),q;'
        "while((q=w.nextNode())){if(DX(q))LD=1;else LV+=q.nodeValue}"
        "function cnt(c,o){var k=0,res=-1;(function g(x){"
        "if(x.nodeType===3){if(x===c)res=k+(DX(x)?0:o);else if(!DX(x))k+=x.nodeValue.length;return}"
        "var ch=x.childNodes||[];for(var j=0;j<ch.length;j++){if(x===c&&j===o){res=k;return}"
        "g(ch[j]);if(res>=0)return}if(x===c)res=k})(n);return res}"
        "var a=cnt(r.startContainer,r.startOffset),z=cnt(r.endContainer,r.endOffset);"
        "if(a<0||z<=a)return;"
        "if(LD){t=LV.slice(a,z).trim();"
        'if(!t){parent.postMessage({type:"artifact-selection",quote:""},'
        f"{origin});return}}}}"
        # Text either side of the quote, so validate_anchor's text-quote-selector
        # can tell two identical quotes on the same page apart (homelab-ij8.108,
        # Task 2). `a`/`z` are already offsets into `n`'s own text, so
        # `n.textContent` -- not the range's stringified text -- is the right
        # string to slice, and `Math.max(0,...)` covers a selection near the
        # start of `n` without a negative `slice` start wrapping from the end.
        "var all=LV,prefix=all.slice(Math.max(0,a-32),a),"
        "suffix=all.slice(z,z+32);"
        # `slice` cuts UTF-16 code units, not characters: an astral character
        # (an emoji, say) straddling either boundary leaves a lone surrogate
        # half in `prefix` or `suffix` -- valid in a JS string, but not valid
        # UTF-8, and the server's `json` column raises on it (final review,
        # Item 1). Drop the half that has nothing to pair with rather than
        # send it; the 31-or-so characters either side of it are still
        # plenty of context.
        "if(prefix.charCodeAt(0)>=0xdc00&&prefix.charCodeAt(0)<=0xdfff)"
        "prefix=prefix.slice(1);"
        "if(suffix.charCodeAt(suffix.length-1)>=0xd800&&"
        "suffix.charCodeAt(suffix.length-1)<=0xdbff)suffix=suffix.slice(0,-1);"
        "var b=r.getBoundingClientRect();"
        'parent.postMessage({type:"artifact-selection",quote:t.slice(0,500),'
        'anchor:{kind:"range",path:path,start:a,end:z,prefix:prefix,suffix:suffix},'
        # vt/vl: where the frame SHOWS it, for full screen, where the frame
        # scrolls itself and top/left (page offsets) cannot place the bar.
        "rect:{top:b.top+scrollY,left:b.left+scrollX,width:b.width,height:b.height,"
        "vt:b.top,vl:b.left}},"
        f"{origin})}}"
        # Guarded: registering this listener must never be what stops the rest
        # of the shim, whatever document it finds itself in.
        'if(document.addEventListener)document.addEventListener("selectionchange",function(){'
        "clearTimeout(st);st=setTimeout(function(){try{sel()}catch(e){}},350)});"
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
        # THE CHANGES VIEW (page review step 4): the live page marked up against
        # the version he sent back. Diffed on the stored bodies (the stamp is
        # applied after), with the same call and so the same cache as the
        # gallery's card, which shows any fallback reason. Never stored.
        diff_style = ""
        if spent.changes_from is not None and n is None:
            base = _load(artifact_id, spent.changes_from)
            if base is not None:
                body, _reason = pagediff.render_changes_safely(base[0], body)
                diff_style = DIFF_STYLE
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
            shim = SHIM + _viewer_report(missing) + diff_style
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

    # SHARED WITH ONE PERSON BY EMAIL (homelab-ij8.16, Jeremy's (1a)). Their
    # personal link opens the page once this device has been confirmed with a
    # one-time code sent to their inbox; until then it shows the confirm step.
    # Every failure -- wrong link, withdrawn, page deleted -- is the same 404.
    _GONE = "This link isn't valid, or it has been withdrawn."

    def _gone() -> Response:
        return PlainTextResponse(
            _GONE,
            status_code=404,
            headers={**base_headers(), "cache-control": "no-store"},
        )

    def _small_page(title: str, inner: str, status: int = 200) -> HTMLResponse:
        body = (
            "<!doctype html><html lang=en><meta charset=utf-8>"
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{_html.escape(title)}</title>"
            "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:3rem auto;"
            "padding:0 1rem;color:#1f1f1f}input,button{font:inherit;padding:.55rem .7rem;"
            "border-radius:8px;border:1px solid #bbb}button{cursor:pointer;background:#1f1f1f;"
            "color:#fff;border-color:#1f1f1f}.err{color:#b42318}.muted{opacity:.7}</style>"
            f"{inner}</html>"
        )
        return HTMLResponse(
            body,
            status_code=status,
            headers={
                "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
                "referrer-policy": "no-referrer",
                "x-content-type-options": "nosniff",
                "cache-control": "no-store",
                "x-robots-tag": "noindex, nofollow",
            },
        )

    def _title(artifact_id: str) -> str:
        with db.connect() as conn:
            row = conn.execute(
                "select title from artifact where id = %s", (artifact_id,)
            ).fetchone()
        return row[0] if row else "a page"

    def _confirm_page(
        token: str, share, note: str = "", sent: bool = False
    ) -> HTMLResponse:
        title = _title(share.artifact_id)
        where = _html.escape(email_share.mask(share.email))
        err = f'<p class="err">{_html.escape(note)}</p>' if note else ""
        if sent:
            inner = (
                f"<h1>Enter your code</h1><p>We sent a six-digit code to {where}. "
                f"It works for {email_share.CODE_MINUTES} minutes.</p>{err}"
                f'<form method="post" action="/e/{_html.escape(token)}/verify">'
                '<input name="code" inputmode="numeric" autocomplete="one-time-code" '
                'maxlength="6" required autofocus> <button type="submit">Open the page</button>'
                "</form>"
                f'<form method="post" action="/e/{_html.escape(token)}/code">'
                '<p class="muted">No code? <button type="submit">Send another</button></p></form>'
            )
        else:
            inner = (
                f"<h1>{_html.escape(title)}</h1><p>Jeremy shared this page with {where}. "
                "To open it on this device, confirm it's you: we'll email a code to that "
                f"address.</p>{err}"
                f'<form method="post" action="/e/{_html.escape(token)}/code">'
                '<button type="submit">Email me a code</button></form>'
            )
        return _small_page(title, inner)

    def _cookie_name(share) -> str:
        return f"artifacts_share_{share.id}"

    @app.get("/e/{token}/")
    def emailed(request: Request, token: str) -> Response:
        artifact_id = _artifact_id(request)
        share = email_share.find(db, artifact_id, token) if artifact_id else None
        if share is None:
            return _gone()
        if not email_share.device_ok(
            db, share, request.cookies.get(_cookie_name(share))
        ):
            return _confirm_page(token, share)
        found = store.shared_page_body(db, artifact_id)
        if found is None:
            return _gone()
        body, published_at = found
        html = inject(stamp.apply(body, published_at), INERT_SHIM)
        return HTMLResponse(
            html, headers={**headers_for(False), "x-robots-tag": "noindex, nofollow"}
        )

    @app.post("/e/{token}/code")
    def emailed_code(request: Request, token: str) -> Response:
        artifact_id = _artifact_id(request)
        share = email_share.find(db, artifact_id, token) if artifact_id else None
        if share is None:
            return _gone()
        try:
            code = email_share.new_code(db, share)
        except email_share.Refused as refused:
            return _confirm_page(token, share, note=str(refused))
        try:
            mail.send(
                share.email,
                f"Your code for \u201c{_title(share.artifact_id)}\u201d",
                f"Your code is {code}. It works for {email_share.CODE_MINUTES} minutes.\n\n"
                "If you did not ask for it, you can ignore this email.",
            )
        except mail.MailFailed:
            return _confirm_page(
                token,
                share,
                note="The code could not be sent just now. Try again shortly.",
            )
        return _confirm_page(token, share, sent=True)

    @app.post("/e/{token}/verify")
    def emailed_verify(request: Request, token: str, code: str = Form("")) -> Response:
        artifact_id = _artifact_id(request)
        share = email_share.find(db, artifact_id, token) if artifact_id else None
        if share is None:
            return _gone()
        cookie = email_share.check_code(db, share, code)
        if cookie is None:
            return _confirm_page(
                token,
                share,
                sent=True,
                note="That code didn't work. Check it, or send another.",
            )
        response = RedirectResponse(f"/e/{token}/", status_code=303)
        response.set_cookie(
            _cookie_name(share),
            cookie,
            max_age=email_share.DEVICE_DAYS * 24 * 3600,
            path=f"/e/{token}/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    # The only way in to a page body (homelab-ij8.101). Which version the pass
    # reads is part of the pass, so there is no version path for a caller to
    # choose: the gallery decided that when it minted it.
    @app.get("/p/{token}/")
    def framed(request: Request, token: str) -> Response:
        return _serve(request, token)

    return app
