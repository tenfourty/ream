// Drive the REAL page-side shim's marks leg against a small fake DOM (page
// review step 1, Task 5, homelab-ij8.108). The gallery posts the open marks
// (quote + anchor, never the note); the shim paints each one where its text is
// through the CSS Custom Highlight API, reports which it could not place, and
// changes nothing in the agent's DOM beyond one <style> for ::highlight().
//
// The fake DOM models what the placement code actually depends on: text nodes
// in document order (TreeWalker SHOW_TEXT, including <script>/<style> text, as
// a real walker does), a Range whose offsets are validated and whose
// toString() concatenates the text between its boundaries, and
// querySelector over a fixed path table. Run by tests/test_shim_marks.py,
// which takes the script from the running app.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");

let fails = 0, passes = 0;
const ok = (c, m) => { c ? passes++ : (fails++, console.log("FAIL: " + m)); if (c) console.log("ok   " + m); };
const GALLERY = "https://artifacts.tenfourty.site";

// ---- a minimal DOM ------------------------------------------------------
// Document order, for compareDocumentPosition: set by build() to the tree it makes.
let ROOT = null, FOCUSED = null;
function order() { const out = []; (function w(n) { out.push(n); (n.childNodes || []).forEach(w); })(ROOT); return out; }
function cmp(a, b) {
  if (a === b) return 0;
  const has = (x, y) => { for (let n = y; n; n = n.parentNode) if (n === x) return true; return false; };
  if (has(a, b)) return 20;
  if (has(b, a)) return 10;
  const o = order();
  return o.indexOf(a) < o.indexOf(b) ? 4 : 2;
}
function T(v) { return { nodeType: 3, nodeValue: v, parentNode: null, get parentElement() { return this.parentNode; },
                         compareDocumentPosition(o) { return cmp(this, o); } }; }
function E(tag, ...kids) {
  const e = { nodeType: 1, nodeName: tag.toUpperCase(), tagName: tag.toUpperCase(), childNodes: [], parentNode: null,
              style: {}, id: "", scrollHeight: 0, offsetHeight: 0, attrs: {}, rect: null,
              getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
              get children() { return this.childNodes.filter((c) => c.nodeType === 1); },
              contains(n) { for (; n; n = n.parentNode) if (n === this) return true; return false; },
              compareDocumentPosition(o) { return cmp(this, o); },
              // Tag selectors only, which is all the shim asks of an element.
              closest(sel) { for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (n.nodeName === sel.toUpperCase()) return n; return null; },
              querySelectorAll(sel) { const hit = []; (function w(n) { n.childNodes.forEach((c) => { if (c.nodeType === 1) { if (c.nodeName === sel.toUpperCase()) hit.push(c); w(c); } }); })(this); return hit; },
              querySelector(sel) { const hit = []; (function w(n) { n.childNodes.forEach((c) => { if (c.nodeType === 1) { if (c.nodeName === sel.toUpperCase()) hit.push(c); w(c); } }); })(this); return hit[0] || null; },
              get parentElement() { return this.parentNode; },
              // Only what the selection leg's path-building actually walks: the
              // nearest earlier ELEMENT sibling, or null when there is none.
              get previousElementSibling() {
                if (!this.parentNode) return null;
                const kids = this.parentNode.childNodes, i = kids.indexOf(this);
                for (let k = i - 1; k >= 0; k--) if (kids[k].nodeType === 1) return kids[k];
                return null;
              },
              // Only for the shim's height report, which the `load` in case 11 also fires.
              getBoundingClientRect() { return this.rect || { top: 0, left: 0, width: 0, height: 0, bottom: 0 }; },
              appendChild(c) { this.childNodes.push(c); c.parentNode = this; return c; },
              // What the comment button's host needs: listeners, attributes, focus and a closed shadow root.
              ls: {},
              addEventListener(t, fn) { (this.ls[t] ||= []).push(fn); },
              setAttribute(k, v) { this.attrs[k] = String(v); },
              focus() { FOCUSED = this; },
              matches(sel) { return sel === ":focus-visible" && !!this.fv; },
              attachShadow(o) { this.shadow = { mode: o.mode, childNodes: [], appendChild(c) { this.childNodes.push(c); c.parentNode = this; return c; } }; return this.shadow; } };
  Object.defineProperty(e, "textContent", {
    get() { return texts(e).map((t) => t.nodeValue).join(""); },
    set(v) { e.childNodes = [T(v)]; e.childNodes[0].parentNode = e; } });
  kids.forEach((k) => e.appendChild(typeof k === "string" ? T(k) : k));
  return e;
}
function texts(root) {
  const out = [];
  (function walk(n) { if (n.nodeType === 3) out.push(n); else n.childNodes.forEach(walk); })(root);
  return out;
}

function build(body, paths, { highlights = true, readyState = "complete", immediateTimers = false, rects = new Map(), raf = true, hit = () => null } = {}) {
  const head = E("head");
  const html = E("html", head, body);
  ROOT = html;
  const listeners = {};
  const docListeners = {};
  const posted = [];
  const all = () => texts(html);
  class Range {
    setStart(n, o) { if (n.nodeType !== 3 || o < 0 || o > n.nodeValue.length) throw new Error("IndexSizeError"); this.sc = n; this.so = o; }
    setEnd(n, o) { if (n.nodeType !== 3 || o < 0 || o > n.nodeValue.length) throw new Error("IndexSizeError"); this.ec = n; this.eo = o; }
    // Only the case the selection leg's own `n` argument ever is here: an
    // element with at least one text descendant (real `selectNodeContents`
    // works from ANY node; a fuller model is not needed to drive `sel()`).
    selectNodeContents(n) {
      if (n.nodeType === 3) { this.setStart(n, 0); this.setEnd(n, n.nodeValue.length); return; }
      const ts = texts(n);
      if (ts.length) { this.setStart(ts[0], 0); this.setEnd(ts[ts.length - 1], ts[ts.length - 1].nodeValue.length); }
    }
    get startContainer() { return this.sc; }
    get startOffset() { return this.so; }
    get endContainer() { return this.ec; }
    get endOffset() { return this.eo; }
    // Only the same-text-node case the selection tests need: a real
    // multi-node commonAncestorContainer is not modelled.
    get commonAncestorContainer() { return this.sc === this.ec ? this.sc : this.sc.parentNode; }
    getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; }
    // Hover hit-testing: the client rects a test pinned to the range's first
    // text node (none by default, so nothing is hoverable unless a test says).
    getClientRects() { return rects.get(this.sc) || []; }
    toString() {
      const ts = all(), i = ts.indexOf(this.sc), j = ts.indexOf(this.ec);
      if (i < 0 || j < 0 || j < i) return "";
      if (i === j) return this.sc.nodeValue.slice(this.so, this.eo);
      return this.sc.nodeValue.slice(this.so) + ts.slice(i + 1, j).map((t) => t.nodeValue).join("") + this.ec.nodeValue.slice(0, this.eo);
    }
  }
  const doc = {
    readyState, head, body, documentElement: html,
    createRange: () => new Range(),
    createTreeWalker(root, what) {
      if (what !== 4) throw new Error("only SHOW_TEXT is modelled");
      const ts = texts(root); let k = 0;
      return { nextNode: () => (k < ts.length ? ts[k++] : null) };
    },
    createElement: (tag) => E(tag),
    getElementById: (id) => order().find((c) => c.nodeType === 1 && c.id === id) || null,
    elementFromPoint: (x, y) => hit(x, y),
    querySelector(sel) {
      if (sel.includes("!!")) throw new SyntaxError("bad selector");
      return paths[sel] || null;
    },
    // Only the selectors the shim asks: headings, a tag, an id, the labelled.
    querySelectorAll(sel) {
      const els = order().filter((c) => c.nodeType === 1);
      if (sel === "h1,h2,h3,h4,h5,h6") return els.filter((c) => /^H[1-6]$/.test(c.nodeName));
      const m = sel.match(/^\[id="([^"]*)"\]$/);
      if (m) return els.filter((c) => c.id === m[1]);
      if (sel === "[alt],[aria-label],[title]") return els.filter((c) => ["alt", "aria-label", "title"].some((k) => k in c.attrs));
      return els.filter((c) => c.nodeName === sel.toUpperCase());
    },
    addEventListener: (t, fn) => (docListeners[t] ||= []).push(fn),
  };
  class Highlight { constructor() { this.ranges = []; } add(r) { this.ranges.push(r); } }
  const registry = new Map();
  const frames = [];
  let currentSelection = null;
  const scope = {
    document: doc,
    parent: { postMessage: (m, o) => posted.push([m, o]) },
    scrollX: 0, scrollY: 0,
    addEventListener: (t, fn) => (listeners[t] ||= []).push(fn),
    getComputedStyle: (e) => ({ marginBottom: "0px",
      display: e && e.display ? e.display : e && ["SPAN", "EM", "A", "B", "STRONG"].includes(e.nodeName) ? "inline" : "block" }),
    getSelection: () => currentSelection,
    // Real time by default, unaffected by `immediateTimers` -- only the
    // selection leg's 350ms debounce ever needs it collapsed for a test.
    setTimeout: immediateTimers ? (fn) => { fn(); return 0; } : setTimeout,
    clearTimeout: immediateTimers ? () => {} : clearTimeout,
    Array, JSON, Math, Object, String,
    // One frame per flush: pointer moves between flushes are a single check.
    ...(raf ? { requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; } } : {}),
    CSS: highlights ? { highlights: registry, escape: (s) => s } : undefined,
    Highlight: highlights ? Highlight : undefined,
  };
  scope.window = scope;
  const names = Object.keys(scope);
  new Function(...names, src)(...names.map((n) => scope[n]));
  const send = (marks, origin = GALLERY) =>
    (listeners.message || []).forEach((fn) => fn({ origin, data: { type: "artifact-marks", marks } }));
  const reports = () => posted.filter(([m]) => m && m.type === "artifact-marks-placed");
  const raw = (data) => (listeners.message || []).forEach((fn) => fn({ origin: GALLERY, data }));
  return { head, body, doc, registry, posted, focused: () => FOCUSED, reports, send, raw, Range,
           fire: (t, ev = {}) => (listeners[t] || []).forEach((fn) => fn(ev)),
           flush: () => { const fs = frames.splice(0); fs.forEach((fn) => fn(0)); return fs.length; },
           fireDoc: (t) => (docListeners[t] || []).forEach((fn) => fn({})),
           setSelection: (s) => { currentSelection = s; } };
}

const QUOTE = "Rotate the keys every 90 days.";
const mark = (over = {}) => ({ id: "m1", kind: "remove", quote: QUOTE,
  anchor: { kind: "range", path: "body>p:nth-of-type(1)", start: 0, end: QUOTE.length, ...(over.anchor || {}) },
  ...Object.fromEntries(Object.entries(over).filter(([k]) => k !== "anchor")) });

// 1. placed where the stored anchor says, painted, reported to the gallery only
{
  const p = E("p", QUOTE);
  const g = build(E("body", p), { "body>p:nth-of-type(1)": p });
  g.send([mark()]);
  const [[msg, origin]] = g.reports();
  ok(origin === GALLERY, "reports only to the gallery's origin");
  ok(JSON.stringify(msg.placed) === '["m1"]' && msg.unplaced.length === 0, "the mark is placed");
  const hl = g.registry.get("ar-remove");
  ok(hl && hl.ranges.length === 1 && String(hl.ranges[0]) === QUOTE, "painted on exactly the quoted text, in the remove highlight");
  ok(g.registry.get("ar-comment").ranges.length === 0 && g.registry.get("ar-good").ranges.length === 0, "and in no other");
  const st = g.doc.getElementById("ar-mark-style");
  ok(st && st.nodeName === "STYLE" && st.textContent.includes("::highlight(ar-remove)"), "one stylesheet carries the ::highlight rules");
  ok(p.childNodes.length === 1 && p.childNodes[0].nodeValue === QUOTE, "the page's own text is not wrapped, split or moved");
  g.send([mark()]);
  ok(g.head.childNodes.filter((c) => c.id === "ar-mark-style").length === 1, "a second message adds no second stylesheet");
}
// 2. a range across elements, and the boundary between two text nodes
{
  const b = E("b", "keys");
  const p = E("p", "Rotate the ", b, " every 90 days.");
  const g = build(E("body", p), { "body>p:nth-of-type(1)": p });
  g.send([mark({ id: "a", kind: "comment" }),
          mark({ id: "k", kind: "looks_good", quote: "keys every", anchor: { start: 11, end: 21 } })]);
  const [[msg]] = g.reports();
  ok(msg.placed.join() === "a,k", "both marks are placed across element boundaries");
  const k = g.registry.get("ar-good").ranges[0];
  ok(String(k) === "keys every", "the second is exactly its quote");
  ok(k.startContainer === b.childNodes[0],
     "an offset exactly at the end of one text node starts in the NEXT node, not at the tail of the previous one");
  ok(String(g.registry.get("ar-comment").ranges[0]) === QUOTE, "a comment mark goes in the comment highlight");
}
// 3. stale offsets fall back to finding the text
{
  const p1 = E("p", "A new first paragraph.");
  const p2 = E("p", QUOTE);
  const g = build(E("body", p1, p2), { "body>p:nth-of-type(1)": p1 });
  g.send([mark()]);
  const [[msg]] = g.reports();
  ok(msg.placed.join() === "m1", "moved text is found by searching for the quote");
  ok(g.registry.get("ar-remove").ranges[0].startContainer === p2.childNodes[0], "and painted where it now is");
}
// 4. never guess: ambiguous means unplaced
{
  const p1 = E("p", QUOTE), p2 = E("p", QUOTE);
  const g = build(E("body", p1, p2), {});
  g.send([mark({ anchor: { path: "body>div" } })]);
  const [[msg]] = g.reports();
  ok(msg.unplaced.join() === "m1" && msg.placed.length === 0, "a quote found twice with no context to choose is unplaced");
  ok(g.registry.get("ar-remove").ranges.length === 0, "and painted nowhere");
}
// 5. context either side chooses between two identical quotes
{
  const p1 = E("p", "Old policy: " + QUOTE + " Ignore."), p2 = E("p", "New policy: " + QUOTE + " Do it.");
  const g = build(E("body", p1, p2), {});
  g.send([mark({ anchor: { path: "body>div", prefix: "New policy: ", suffix: " Do it." } })]);
  const [[msg]] = g.reports();
  ok(msg.placed.join() === "m1", "prefix and suffix pick one of two identical quotes");
  ok(g.registry.get("ar-remove").ranges[0].startContainer === p2.childNodes[0], "the right one");
}
// 6. script and style text is not page text
{
  const s = E("script", `var q="${QUOTE}"`), st = E("style", `/* ${QUOTE} */`);
  const p = E("p", QUOTE);
  const g = build(E("body", s, st, p), {});
  g.send([mark({ anchor: { path: "nope" } })]);
  const [[msg]] = g.reports();
  ok(msg.placed.join() === "m1", "a copy inside <script>/<style> does not make the visible quote ambiguous");
  ok(g.registry.get("ar-remove").ranges[0].startContainer === p.childNodes[0], "and the mark is never painted into script text");
}
// 7. whitespace and case as a selection reports them still match
{
  const p = E("p", "Rotate the keys", E("br"), "\n   every 90 days.");
  const g = build(E("body", p), {});
  g.send([mark({ quote: "ROTATE the keys\nevery 90 days.", anchor: { path: "nope" } })]);
  const [[msg]] = g.reports();
  ok(msg.placed.join() === "m1", "whitespace and case differences do not stop the search");
  ok(String(g.registry.get("ar-remove").ranges[0]) === "Rotate the keys\n   every 90 days.", "the painted range covers the page's own text");
}
// 8. a quote that is gone is unplaced
{
  const p = E("p", "Something else entirely.");
  const g = build(E("body", p), { "body>p:nth-of-type(1)": p });
  g.send([mark()]);
  const [[msg]] = g.reports();
  ok(msg.unplaced.join() === "m1", "text that is no longer on the page is unplaced");
}
// 9. only the gallery may send marks
{
  const p = E("p", QUOTE);
  const g = build(E("body", p), { "body>p:nth-of-type(1)": p });
  g.send([mark()], "https://evil.example");
  ok(g.posted.length === 0 && g.registry.size === 0, "marks from another origin are ignored");
}
// 10. no Highlight API: paint nothing, say nothing
{
  const p = E("p", QUOTE);
  const g = build(E("body", p), { "body>p:nth-of-type(1)": p }, { highlights: false });
  let threw = false;
  try { g.send([mark()]); } catch (e) { threw = true; }
  ok(!threw && g.posted.length === 0, "without CSS.highlights nothing is painted or reported");
  ok(!g.doc.getElementById("ar-mark-style"), "and no stylesheet is added");
}
// 11. the page is still loading: wait for it
{
  const p = E("p", QUOTE);
  const g = build(E("body", p), { "body>p:nth-of-type(1)": p }, { readyState: "loading" });
  g.send([mark({ id: "old" })]);
  g.send([mark()]);
  ok(g.reports().length === 0, "marks that arrive before the page has loaded wait");
  g.doc.readyState = "complete";
  g.fire("load");
  ok(g.reports().length === 1 && g.reports()[0][0].placed.join() === "m1", "and the latest set is painted once, on load");
}
// 11b. a long selection (past the 500-character quote cap) whose tail changed:
// only the verified quote is painted, never the unchecked tail
{
  const head = "Keys rotate on a schedule. ".repeat(19).slice(0, 500);
  const quote = head;                                   // the selection leg's slice(0,500)
  const oldTail = " and the old tail that was selected".padEnd(300, ".");
  const newTail = " BUT THIS TAIL WAS EDITED AFTERWARDS".padEnd(300, "!");
  const b = E("b", newTail.slice(0, 20));
  const p = E("p", head, b, newTail.slice(20));        // the tail also crosses an element
  const g = build(E("body", p), { "body>p:nth-of-type(1)": p });
  g.send([mark({ quote, anchor: { start: 0, end: head.length + oldTail.length } })]);
  const [[msg]] = g.reports();
  ok(msg.placed.join() === "m1", "a long selection is still placed on its stored position");
  const r = g.registry.get("ar-remove").ranges[0];
  ok(String(r) === head, "painting stops at the end of the verified quote");
  ok(!String(r).includes("EDITED"), "and the edited tail nobody checked is never painted");
}
// 12. hostile payloads never throw into the page, and the list is capped
{
  const p = E("p", QUOTE);
  const paths = { "body>p:nth-of-type(1)": p };
  const hostile = [null, 7, "x", {}, { id: 5 }, { id: "q", quote: 5, anchor: {} },
    mark({ id: "c", kind: "constructor" }), mark({ id: "t", kind: "toString" }),
    mark({ id: "s", anchor: { path: "!!bad" } }), mark({ id: "n", anchor: { start: "x", end: {} } }),
    mark({ id: "o", anchor: { start: 5, end: 99999 } }), { ...mark({ id: "z" }), anchor: null },
    mark({ id: "w", quote: "   " }), mark({ id: "pp", anchor: { prefix: 5, suffix: null, path: "nope" } })];
  const g = build(E("body", p), paths);
  let threw = false;
  try { g.send(hostile); } catch (e) { threw = true; }
  ok(!threw, "a hostile mark list does not throw");
  const [[msg]] = g.reports();
  ok(msg.placed.includes("c") && msg.placed.includes("t"), "an unknown kind (even an Object.prototype name) paints as a comment");
  ok(msg.placed.includes("s") && msg.placed.includes("n") && msg.placed.includes("o") && msg.placed.includes("pp"),
     "a broken anchor falls back to finding the text");
  ok(msg.unplaced.includes("q") && msg.unplaced.includes("z") && msg.unplaced.includes("w"), "a mark with no usable quote is unplaced");
  ok([...msg.placed, ...msg.unplaced].every((id) => typeof id === "string"), "only string ids are ever reported");
  for (const d of [null, "s", 1, { type: "artifact-marks" }, { type: "artifact-marks", marks: "no" }]) {
    let t = false;
    const h = build(E("body", E("p", QUOTE)), {});
    try { h.raw(d); } catch (e) { t = true; }
    ok(!t && h.posted.length === 0, "survives, and says nothing to, a malformed message: " + JSON.stringify(d));
  }
  const big = Array.from({ length: 500 }, (_, i) => mark({ id: "b" + i }));
  const h = build(E("body", E("p", QUOTE)), {});
  h.send(big);
  const [[m2]] = h.reports();
  ok(m2.placed.length === 200, "at most 200 marks are painted");
  // homelab-ij8.111, follow-up Item 2: the unplaced REPLY is itself capped at
  // 200 ids, with the rest folded into an `overflow` count rather than sent
  // as ids -- every mark is still accounted for, just not all of them by id.
  ok(m2.unplaced.length === 200, "the unplaced ids in the reply are capped at 200");
  ok(m2.overflow === 100, "the other 100 unplaced marks are counted, not listed");
  ok(m2.placed.length + m2.unplaced.length + m2.overflow === 500,
     "every mark is still accounted for, across ids and the overflow count");
}
// 15. the unplaced cap carries no `overflow` key at all when nothing was cut
// from the list -- only sent when there is something it would otherwise hide
{
  const list400 = Array.from({ length: 400 }, (_, i) => mark({ id: "c" + i }));
  const h = build(E("body", E("p", QUOTE)), {});
  h.send(list400);
  const [[msg]] = h.reports();
  ok(msg.placed.length === 200 && msg.unplaced.length === 200,
     "200 placed, 200 unplaced -- exactly at the cap, nothing left over");
  ok(!("overflow" in msg), "no overflow key when the unplaced list was not actually cut");
}
// 13. ids beyond the 200 cap are reported unplaced, never vanish -- but a
// hostile shape past the cap is still dropped, exactly as one within it is
{
  const p = E("p", QUOTE);
  const paths = { "body>p:nth-of-type(1)": p };
  const list = Array.from({ length: 205 }, (_, i) => mark({ id: "b" + i }));
  list.push({ id: 5 }); // past the cap AND not a usable shape: dropped, not reported
  const g = build(E("body", p), paths);
  g.send(list);
  const [[msg]] = g.reports();
  ok(msg.placed.length === 200, "only the first 200 are painted");
  ok(msg.unplaced.length === 5, "the 5 valid ids past the cap are reported unplaced");
  ok(msg.unplaced.includes("b204") && !msg.unplaced.includes(5),
     "by id, and the hostile shape past the cap never appears anywhere");
  ok(msg.placed.length + msg.unplaced.length === 205, "every valid mark is accounted for");
}
// 14. selecting text where an astral emoji straddles the prefix/suffix
// boundary never sends a lone surrogate (final review, Item 1): `slice` cuts
// UTF-16 code units, not characters, and a lone surrogate reaching the
// server as JSON is what turned a mark into a 500 there.
{
  function hasLoneSurrogate(s) {
    for (let i = 0; i < s.length; i++) {
      const c = s.charCodeAt(i);
      if (c >= 0xd800 && c <= 0xdbff) {
        if (i + 1 >= s.length || s.charCodeAt(i + 1) < 0xdc00 || s.charCodeAt(i + 1) > 0xdfff) return true;
      } else if (c >= 0xdc00 && c <= 0xdfff) {
        if (i === 0 || s.charCodeAt(i - 1) < 0xd800 || s.charCodeAt(i - 1) > 0xdbff) return true;
      }
    }
    return false;
  }
  const EMOJI = "🟢"; // a green circle: one astral character, two UTF-16 units
  const before = EMOJI + "a".repeat(31); // the low surrogate lands exactly where the prefix starts
  const quote = "MIDDLE";
  const after = "b".repeat(31) + EMOJI + "b".repeat(5); // the high surrogate lands exactly where the suffix ends
  const text = before + quote + after;
  const p = E("p", text);
  const g = build(E("body", p), {}, { immediateTimers: true });
  const start = before.length, end = start + quote.length;
  g.setSelection({
    rangeCount: 1,
    toString: () => quote,
    getRangeAt: () => {
      const r = new g.Range();
      r.setStart(p.childNodes[0], start);
      r.setEnd(p.childNodes[0], end);
      return r;
    },
  });
  g.fireDoc("selectionchange");
  const [[msg]] = g.posted.filter(([m]) => m && m.type === "artifact-selection");
  ok(!hasLoneSurrogate(msg.anchor.prefix), "the reported prefix carries no lone surrogate at its boundary");
  ok(!hasLoneSurrogate(msg.anchor.suffix), "the reported suffix carries no lone surrogate at its boundary");
  ok(msg.anchor.prefix === "a".repeat(31), "a leading half emoji is dropped whole from the prefix, never split");
  ok(msg.anchor.suffix === "b".repeat(31), "a trailing half emoji is dropped whole from the suffix, never split");
}

// 13. a held mark goes in its own dotted highlight, never in a kind's group
{
  const p = E("p", QUOTE);
  const g = build(E("body", p), { "body>p:nth-of-type(1)": p });
  g.send([mark({ id: "h1", kind: "", held: true })]);
  const [[msg]] = g.reports();
  ok(JSON.stringify(msg.placed) === '["h1"]', "a held mark is placed");
  const h = g.registry.get("ar-held");
  ok(h && h.ranges.length === 1 && String(h.ranges[0]) === QUOTE, "painted in ar-held");
  ok(["ar-comment", "ar-remove", "ar-good"].every((n) => g.registry.get(n).ranges.length === 0), "and in no kind's group");
  const css = g.doc.getElementById("ar-mark-style").textContent;
  ok(css.includes("::highlight(ar-held){text-decoration:underline dotted}") && !/ar-held\)\{[^}]*background/.test(css), "dotted underline, no background");
}

// 14. HOVER. Resting the pointer on painted text tells the gallery WHICH mark
// and WHERE, nothing else: the card is drawn by the gallery from what it
// already holds, so no text -- not the quote, and never a note -- crosses.
{
  const QUOTE2 = "Held words stay dotted here.";
  const p = E("p", QUOTE), q = E("p", QUOTE2);
  const rects = new Map([[p.childNodes[0], [{ top: 10, left: 5, width: 200, height: 18, right: 205, bottom: 28 }]],
                         [q.childNodes[0], [{ top: 40, left: 5, width: 150, height: 18, right: 155, bottom: 58 }]]]);
  const g = build(E("body", p, q), { "body>p:nth-of-type(1)": p, "body>p:nth-of-type(2)": q }, { rects });
  const hovers = () => g.posted.filter(([m]) => m && /^artifact-mark-/.test(m.type));
  g.fire("pointermove", { clientX: 50, clientY: 20, pointerType: "mouse" }); g.flush();
  ok(hovers().length === 0, "before any marks are painted, a move posts nothing");
  g.send([mark(), mark({ id: "h1", kind: "", held: true, quote: QUOTE2,
    anchor: { path: "body>p:nth-of-type(2)", start: 0, end: QUOTE2.length } })]);
  g.fire("pointermove", { clientX: 50, clientY: 20, pointerType: "mouse" });
  g.fire("pointermove", { clientX: 60, clientY: 21, pointerType: "mouse" });
  g.fire("pointermove", { clientX: 70, clientY: 22, pointerType: "mouse" });
  ok(g.flush() === 1, "a burst of moves is one check per animation frame");
  let hs = hovers();
  ok(hs.length === 1, "entering marked text posts once");
  const [msg, origin] = hs[0];
  ok(origin === GALLERY, "to the gallery's origin only");
  ok(JSON.stringify(Object.keys(msg).sort()) === '["id","rect","type"]', "carrying only type, id and rect");
  ok(msg.type === "artifact-mark-hover" && msg.id === "m1", "naming the mark under the pointer");
  ok(JSON.stringify(Object.keys(msg.rect).sort()) === '["height","left","top","width"]'
     && msg.rect.top === 10 && msg.rect.left === 5 && msg.rect.width === 200 && msg.rect.height === 18,
     "and the rect of the line it sits on, four numbers");
  ok(!JSON.stringify(g.posted.filter(([m]) => m.type !== "artifact-marks-placed")).includes("Rotate"),
     "no text of the mark is ever posted");
  g.fire("pointermove", { clientX: 80, clientY: 20, pointerType: "mouse" }); g.flush();
  ok(hovers().length === 1, "moving within the same mark says nothing more");
  g.fire("pointermove", { clientX: 80, clientY: 45, pointerType: "mouse" }); g.flush();
  hs = hovers();
  ok(hs.length === 2 && hs[1][0].id === "h1" && hs[1][0].rect.top === 40, "a held mark is hoverable too");
  g.fire("pointermove", { clientX: 400, clientY: 300, pointerType: "mouse" }); g.flush();
  hs = hovers();
  ok(hs.length === 3 && JSON.stringify(hs[2][0]) === '{"type":"artifact-mark-leave"}' && hs[2][1] === GALLERY,
     "leaving marked text posts a bare leave");
  g.fire("pointermove", { clientX: 500, clientY: 300, pointerType: "mouse" }); g.flush();
  ok(hovers().length === 3, "and nothing more while off the marks");
  g.fire("pointerdown", { clientX: 50, clientY: 20, pointerType: "touch" });
  hs = hovers();
  ok(hs.length === 4 && hs[3][0].id === "m1", "a tap on marked text is a hover, at once");
  g.fire("pointerout", { relatedTarget: null });
  hs = hovers();
  ok(hs.length === 5 && hs[4][0].type === "artifact-mark-leave", "the pointer leaving the page is a leave");
  g.fire("pointerdown", { clientX: 50, clientY: 20, pointerType: "mouse" });
  ok(hovers().length === 5, "a mouse press is not a hover of its own");
  let threw = false;
  try { g.fire("pointermove", {}); g.flush(); g.fire("pointerdown", {}); g.fire("pointerout", {}); } catch (e) { threw = true; }
  ok(!threw, "a malformed pointer event never throws into the page");
}
// 15. without requestAnimationFrame the check still runs, once per move
{
  const p = E("p", QUOTE);
  const rects = new Map([[p.childNodes[0], [{ top: 10, left: 5, width: 200, height: 18, right: 205, bottom: 28 }]]]);
  const g = build(E("body", p), { "body>p:nth-of-type(1)": p }, { rects, raf: false });
  g.send([mark()]);
  g.fire("pointermove", { clientX: 50, clientY: 20 });
  g.fire("pointermove", { clientX: 51, clientY: 20 });
  ok(g.posted.filter(([m]) => m.type === "artifact-mark-hover").length === 1, "no animation frames: still one hover");
}

// 16. the frame's OWN scroll (full screen, where it scrolls inside itself)
// moves the text from under a still pointer: a leave, and the next move over
// the same mark is a hover again.
{
  const p = E("p", QUOTE);
  const rects = new Map([[p.childNodes[0], [{ top: 10, left: 5, width: 200, height: 18 }]]]);
  const g = build(E("body", p), { "body>p:nth-of-type(1)": p }, { rects });
  g.send([mark()]);
  const hovers = () => g.posted.filter(([m]) => m && /^artifact-mark-/.test(m.type)).map(([m]) => m.type);
  g.fire("pointermove", { clientX: 50, clientY: 20 }); g.flush();
  g.fire("scroll", {});
  ok(hovers().join() === "artifact-mark-hover,artifact-mark-leave", "the frame scrolling posts a leave");
  g.fire("pointermove", { clientX: 50, clientY: 20 }); g.flush();
  ok(hovers().join() === "artifact-mark-hover,artifact-mark-leave,artifact-mark-hover", "and the same mark hovers again after it");
  g.fire("scroll", {}); g.fire("scroll", {});
  ok(hovers().length === 4, "one leave, however many scroll events");
}

// 17. THE CHANGES VIEW (page review step 4). Round 2 opens on the page marked
// up against the version he sent back: old words in <del class="ar-diff-del">,
// removed-element chips, new words in <ins class="ar-diff-ins">. A selection
// across a deletion must store the LIVE page's words and offsets -- deleted
// text and chips are not page text -- and a mark made on the plain page must
// paint in the changes view with that text skipped.
{
  const del = E("del", "90"); del.className = "ar-diff-del";
  const ins = E("ins", "30"); ins.className = "ar-diff-ins";
  const chip = E("span", "removed"); chip.className = "ar-diff-chip";
  const p = E("p", "Rotate the keys every ", del, ins, chip, " days.");
  const g = build(E("body", p), { "body>p:nth-of-type(1)": p }, { immediateTimers: true });
  const LIVE = "keys every 30 days";
  g.setSelection({
    rangeCount: 1,
    toString: () => "keys every 9030removed days",
    getRangeAt: () => {
      const r = new g.Range();
      r.setStart(p.childNodes[0], 11);
      r.setEnd(p.childNodes[4], 5);
      return r;
    },
  });
  g.fireDoc("selectionchange");
  const [[msg]] = g.posted.filter(([m]) => m && m.type === "artifact-selection");
  ok(msg.quote === LIVE, "a selection across a deletion quotes only the live words: " + JSON.stringify(msg.quote));
  ok(msg.anchor.path === "body>p:nth-of-type(1)", "the path is the live page's element");
  ok(msg.anchor.start === 11 && msg.anchor.end === 11 + LIVE.length,
     "the offsets count the live text only: " + msg.anchor.start + "-" + msg.anchor.end);
  ok(msg.anchor.prefix === "Rotate the " && msg.anchor.suffix === ".", "and so does the context either side");

  // A selection inside the inserted words is anchored on their paragraph.
  g.posted.length = 0;
  g.setSelection({ rangeCount: 1, toString: () => "30",
    getRangeAt: () => { const r = new g.Range(); r.setStart(ins.childNodes[0], 0); r.setEnd(ins.childNodes[0], 2); return r; } });
  g.fireDoc("selectionchange");
  const [[m2]] = g.posted.filter(([m]) => m && m.type === "artifact-selection");
  ok(m2.quote === "30" && m2.anchor.path === "body>p:nth-of-type(1)" && m2.anchor.start === 22 && m2.anchor.end === 24,
     "inserted words are ordinary text of the live paragraph: " + JSON.stringify(m2.anchor));

  // Painting: the plain page's anchor lands on the same words here...
  g.send([mark({ id: "d1", quote: LIVE, anchor: { start: 11, end: 11 + LIVE.length } }),
          mark({ id: "d2", quote: "every 30 days.", anchor: { path: "body>div" } })]);
  const [[rep]] = g.reports();
  ok(rep.placed.join() === "d1,d2", "a plain-page mark paints in the changes view, by anchor and by search: " + JSON.stringify(rep));
  const r1 = g.registry.get("ar-remove").ranges[0];
  ok(r1.startContainer === p.childNodes[0] && r1.startOffset === 11 && r1.ec === p.childNodes[4] && r1.eo === 5,
     "spanning the deletion without counting it");
}

// ===== ELEMENT PINS (pinned comments, slice 2) ===============================
// A pin on a chart, an image, a table or a paragraph. Placed by id, then by
// heading + tag + ordinal, then by its quote, then by an image's label; never
// painted into the page (the gallery draws the badge from the rect reported).
const rectOf = (top, left, width, height) => ({ top, left, width, height, bottom: top + height, right: left + width });
const el = (tag, id, attrs, ...kids) => { const e = E(tag, ...kids); if (id) e.id = id; Object.assign(e.attrs, attrs || {}); return e; };
const ev = (extra = {}) => { const e = { prevented: 0, stopped: 0, preventDefault() { e.prevented++; }, stopPropagation() { e.stopped++; },
  stopImmediatePropagation() { e.stopped++; }, ...extra }; return e; };
const elMark = (over = {}) => ({ id: "e1", kind: "comment", quote: "", anchor: { kind: "element", ...(over.anchor || {}) },
  ...Object.fromEntries(Object.entries(over).filter(([k]) => k !== "anchor")) });

// 18. placed by id: reported with its page rectangle, nothing painted, nothing added to the page
{
  const table = el("table", "plans", {}, E("tr", E("td", "Pro")));
  table.rect = rectOf(100, 10, 300, 50);
  const g = build(E("body", E("h2", "Costs"), table), {});
  g.send([elMark({ anchor: { id: "plans", tag: "table" } })]);
  const [[rep]] = g.reports();
  ok(rep.placed.join() === "e1" && rep.unplaced.length === 0, "an element found by its id is placed: " + JSON.stringify(rep));
  ok(JSON.stringify(rep.rects) === JSON.stringify([{ id: "e1", top: 100, left: 10, width: 300, height: 50 }]),
     "and its rectangle is reported, in page coordinates: " + JSON.stringify(rep.rects));
  ok(!g.registry.has("ar-comment") || g.registry.get("ar-comment").ranges.length === 0, "an element pin paints no text highlight");
  ok(g.head.childNodes.filter((c) => c.nodeName === "STYLE").length === 1 && table.childNodes.length === 1, "and the page's own DOM is untouched");
  g.send([mark()]);
  ok(!("rects" in g.reports()[1][0]), "a report with no element pins carries no rects key");
}

// 19. an id used twice is not trusted; it falls through to heading, tag and ordinal
{
  const a = el("img", "dup", {}), b = el("img", "dup", {});
  b.rect = rectOf(300, 0, 50, 50);
  const g = build(E("body", E("h2", "Costs"), a, b), {});
  g.send([elMark({ anchor: { id: "dup", tag: "img", heading: "Costs", ordinal: 2 } })]);
  const [[rep]] = g.reports();
  ok(rep.placed.join() === "e1" && rep.rects[0].top === 300, "a repeated id falls through to the ordinal under the heading: " + JSON.stringify(rep));
  g.send([elMark({ anchor: { id: "dup", tag: "img" } })]);
  ok(g.reports()[1][0].unplaced.join() === "e1", "and with nothing else to go by the pin is unplaced, never guessed");
}

// 20. heading + tag + ordinal
{
  const i1 = el("img"), i2 = el("img"), i3 = el("img");
  i2.rect = rectOf(200, 0, 40, 40); i3.rect = rectOf(900, 0, 40, 40);
  const hosting = E("h2", "Hosting"); hosting.id = "host";
  const g = build(E("body", E("h2", "Costs"), i1, i2, hosting, i3), {});
  const at = (anchor) => { g.send([elMark({ anchor })]); const r = g.reports(); return r[r.length - 1][0]; };
  let rep = at({ tag: "img", ordinal: 2, heading: "Costs" });
  ok(rep.placed.length === 1 && rep.rects[0].top === 200, "the second image under Costs");
  rep = at({ tag: "img", ordinal: 1, heading: "Hosting", heading_id: "host" });
  ok(rep.placed.length === 1 && rep.rects[0].top === 900, "the first image under Hosting, the heading found by its id");
  rep = at({ tag: "img", ordinal: 3, heading: "Costs" });
  ok(rep.unplaced.join() === "e1", "a third image under Costs is not there: unplaced, not the next section's");
  rep = at({ tag: "img", ordinal: 1, heading: "Renamed" });
  ok(rep.unplaced.join() === "e1", "a heading that was renamed is not guessed at");
}

// 21. by its quote when it has no id and the structure moved
{
  const p = E("p", "Cost per seat is flat across plans.");
  const g = build(E("body", E("h2", "Else"), p), {});
  g.send([elMark({ quote: "Cost per seat is flat across plans.", anchor: { tag: "p", heading: "Costs" } })]);
  const [[rep]] = g.reports();
  ok(rep.placed.join() === "e1", "an element with no id is found by its text: " + JSON.stringify(rep));
  g.send([elMark({ quote: "Words nobody wrote", anchor: { tag: "p" } })]);
  ok(g.reports()[1][0].unplaced.join() === "e1", "text that is gone is unplaced");
}

// 22. an image with no id is found by its alt text
{
  const img = el("img", "", { alt: "Revenue by quarter" });
  img.rect = rectOf(40, 5, 400, 300);
  const g = build(E("body", img, el("img", "", { alt: "Something else" })), {});
  g.send([elMark({ quote: "Revenue by quarter", anchor: { tag: "img" } })]);
  const [[rep]] = g.reports();
  ok(rep.placed.join() === "e1" && rep.rects[0].top === 40, "found by its alt text: " + JSON.stringify(rep));
  g.send([elMark({ quote: "Revenue", anchor: { tag: "img" } }),
          elMark({ id: "e2", quote: "Nothing like it", anchor: { tag: "img" } })]);
  ok(g.reports()[1][0].placed.join() === "e1" && g.reports()[1][0].unplaced.join() === "e2",
     "a label that is not on the page is unplaced");
  const two = build(E("body", el("img", "", { alt: "Chart" }), el("img", "", { alt: "Chart" })), {});
  two.send([elMark({ quote: "Chart", anchor: { tag: "img" } })]);
  ok(two.reports()[0][0].unplaced.join() === "e1", "two images with the same alt text are ambiguous: unplaced");
}

// 23. hostile element marks never throw
{
  const g = build(E("body", E("p", "x")), {});
  let threw = false;
  try {
    g.send([elMark({ anchor: { id: "a\"]x", tag: 5, ordinal: "2", heading: {} } }), { id: "z", anchor: { kind: "element" } },
            elMark({ id: "q", anchor: { id: "x".repeat(500) } }), { id: 7, anchor: { kind: "element", id: "p" } }]);
  } catch (e) { threw = true; }
  ok(!threw, "malformed element marks do not throw");
  ok(g.reports()[0][0].placed.length === 0, "and place nothing");
}

// 24. COMMENT MODE: lit by the gallery (and only by it), a box is drawn round a chart,
// table, cell or picture that is pointed at, and a click on it reports the pick. Unlit,
// nothing is drawn and no click is touched. One host element outside the page's content.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const hostOf = (g) => g.doc.documentElement.childNodes.find((n) => n.nodeName === "AR-COMMENT-HOST");
const shown = (g) => !!hostOf(g) && !/display:none/.test(hostOf(g).style.cssText);
const hover = (g, x = 30, y = 25, pointerType = "mouse") => { g.fire("pointermove", { clientX: x, clientY: y, pointerType }); g.flush(); };
const pins = (g) => g.posted.filter(([m]) => m && m.type === "artifact-pin");
const lit = (g, on = true) => g.raw({ type: "artifact-comment-mode", on });
const clickAt = (g, x, y, extra = {}) => { const c = ev({ target: g.doc.elementFromPoint(x, y), ...extra }); g.fire("click", c); return c; };
const pickWith = (g, x, y) => { lit(g); hover(g, x, y); clickAt(g, x, y); return pins(g).at(-1); };
const sized = (g) => { g.doc.documentElement.clientWidth = 800; g.doc.documentElement.clientHeight = 600; return g; };
{
  const chart = el("svg", "revenue", { "aria-label": "Revenue by quarter" });
  chart.rect = rectOf(20, 10, 300, 150);
  const p = el("p", "intro", {}, "Hello there");
  p.rect = rectOf(200, 8, 300, 30);
  let target = p;
  const body = E("body", E("h2", "Costs"), chart, p);
  const g = sized(build(body, {}, { hit: () => target }));
  target = chart; hover(g, 30, 30);
  ok(!hostOf(g), "Comment off: pointing at a chart draws nothing and adds nothing to the page");
  const c0 = clickAt(g, 30, 30);
  ok(c0.prevented === 0 && c0.stopped === 0 && pins(g).length === 0, "and a click is the page's own: nothing swallowed, nothing posted");
  lit(g);
  target = p; hover(g);
  ok(!hostOf(g) && pins(g).length === 0, "lit: pointing at a paragraph draws nothing; text is commented on by selecting it");
  const cp = clickAt(g, 30, 205);
  ok(cp.prevented === 0 && pins(g).length === 0, "and clicking a paragraph is the page's own");
  target = chart; hover(g, 30, 30);
  ok(shown(g) && hostOf(g).parentNode === g.doc.documentElement, "lit: pointing at a chart draws the box, on the document element");
  ok(!body.children.includes(hostOf(g)) && hostOf(g).childNodes.length === 0, "not in the page's body, and no light-tree text for any walk to find");
  ok(hostOf(g).shadow.mode === "closed", "its shadow root is closed, so the page cannot reach into it");
  const css = hostOf(g).style.cssText;
  ok(/position:fixed/.test(css) && /top:20px/.test(css) && /left:10px/.test(css) && /width:300px/.test(css) && /height:150px/.test(css) && /pointer-events:none/.test(css), "round the chart, in viewport terms, letting the pointer through: " + css);
  ok(hostOf(g).shadow.childNodes[0].nodeName === "STYLE" && /all:initial/.test(hostOf(g).shadow.childNodes[0].textContent), "with styles of its own inside the shadow root, cut off from the page's CSS");
  chart.rect = rectOf(60, 10, 300, 150);
  g.fire("scroll", {});
  ok(/top:60px/.test(hostOf(g).style.cssText), "it follows the chart when the frame scrolls");
  const c = clickAt(g, 30, 30);
  ok(c.prevented === 1 && c.stopped === 1, "a click on the chart is taken, so the page does not also act on it");
  ok(pins(g).length === 1 && pins(g)[0][1] === GALLERY, "and reports a pin, to the gallery only");
  const m = pins(g)[0][0];
  ok(m.anchor.kind === "element" && m.anchor.id === "revenue" && m.anchor.tag === "svg" && m.anchor.label === "Revenue by quarter" && m.quote === "Revenue by quarter", "the anchor a pick always produced: " + JSON.stringify(m.anchor));
  ok(!shown(g), "the box goes once it is used");
  target = chart; hover(g, 30, 70);
  ok(shown(g), "pointing at it again draws it again");
  target = p; hover(g, 30, 205);
  ok(!shown(g), "moving off to text takes the box away");
  target = chart; hover(g, 30, 70);
  lit(g, false);
  ok(!shown(g), "turning Comment off takes it away");
  const c2 = clickAt(g, 30, 70);
  ok(c2.prevented === 0 && pins(g).length === 1, "and the page's clicks are its own again");
  lit(g, "yes"); hover(g, 30, 70);
  ok(!shown(g), "only a true on lights the mode");
}
// links, controls and selections are never captured, even over a chart or a table
{
  const a = el("a", "", { href: "https://example.com/x" }, "details");
  const t = el("table", "plans", {}, E("tr", E("td", a)));
  t.rect = rectOf(100, 10, 300, 50);
  const g = sized(build(E("body", E("p", "x"), t), {}, { hit: () => a }));
  lit(g);
  const c = clickAt(g, 30, 105);
  ok(c.prevented === 0 && c.stopped === 0 && pins(g).length === 0, "a link inside a table keeps working: the click is not captured");
  const bare = el("a", "", {}, "anchor only"), t2 = el("table", "t2", {}, E("tr", E("td", bare)));
  t2.rect = rectOf(100, 10, 300, 50);
  const g2 = sized(build(E("body", t2), {}, { hit: () => bare }));
  lit(g2);
  ok(clickAt(g2, 30, 105) && pins(g2).length === 1, "an anchor with no href is not a link: it is part of the table");
  const btn = el("button", "", {}, "go"), t3 = el("table", "t3", {}, E("tr", E("td", btn)));
  t3.rect = rectOf(100, 10, 300, 50);
  const g3 = sized(build(E("body", t3), {}, { hit: () => btn }));
  lit(g3);
  const cb = clickAt(g3, 30, 105);
  ok(cb.prevented === 0 && pins(g3).length === 0, "a button inside a table is the page's own too");
  const t4 = el("table", "t4", {}, E("tr", E("td", "Pro")));
  t4.rect = rectOf(100, 10, 300, 50);
  const g4 = sized(build(E("body", t4), {}, { hit: () => t4 }));
  lit(g4);
  g4.setSelection({ toString: () => "some words", rangeCount: 1 });
  const cs = clickAt(g4, 30, 105);
  ok(cs.prevented === 0 && pins(g4).length === 0, "words being selected are left alone: no pin while a selection stands");
  g4.setSelection(null);
  clickAt(g4, 30, 105);
  ok(pins(g4).length === 1, "and with nothing selected the same click starts a comment");
}
{
  const t = el("table", "plans", {}, E("tr", E("td", "Pro")));
  t.rect = rectOf(100, 10, 300, 50);
  const g = sized(build(E("body", E("p", "x"), t), {}, { hit: () => t }));
  g.raw({ type: "artifact-pin-mode", on: true });
  const c = clickAt(g, 30, 105);
  ok(c.prevented === 0 && pins(g).length === 0, "the old pin-mode message still does nothing");
  for (const k of ["c", "C", "Escape", "ArrowUp", "ArrowDown"]) g.fire("keydown", ev({ key: k, target: E("p") }));
  ok(g.posted.length === 0, "unlit, no single-letter key, Escape or arrow says anything to the gallery");
  ok(!/artifact-pin-(mode|toggle|exit|hover)|crosshair/.test(src), "the old pin-mode messages and the cursor style are not in the shim");
}

// 29. the keyboard, while lit: a focused chart, table or picture is boxed; Enter starts a
// comment on it; Escape turns Comment off and says so
{
  const fig = el("figure", "fig", { tabindex: "0" }, E("figcaption", "Latency"));
  fig.rect = rectOf(10, 10, 300, 120);
  const link = el("a", "", {}, "a link"); link.rect = rectOf(300, 10, 40, 10);
  const g = sized(build(E("body", fig, link), {}, { hit: () => null }));
  fig.fv = true; g.fire("focusin", { target: fig });
  ok(!shown(g), "unlit: keyboard focus on a figure draws nothing");
  lit(g);
  fig.fv = false; g.fire("focusin", { target: fig });
  ok(!shown(g), "focus that came from a mouse click (not focus-visible) shows nothing");
  fig.fv = true; g.fire("focusin", { target: fig });
  ok(shown(g), "lit: keyboard focus on a figure draws its box");
  const tab = ev({ key: "Tab", target: fig }); g.fire("keydown", tab);
  ok(tab.prevented === 0, "Tab is the page's own");
  const o = ev({ key: "Enter", target: link }); g.fire("keydown", o);
  ok(o.prevented === 0 && pins(g).length === 0, "Enter on anything else is the page's own");
  const en = ev({ key: "Enter", target: fig }); g.fire("keydown", en);
  ok(en.prevented === 1 && pins(g).length === 1 && pins(g)[0][0].anchor.id === "fig", "Enter on the focused figure reports the same pin");
  ok(!shown(g), "and the box goes");
  g.fire("focusin", { target: fig });
  g.fire("focusin", { target: Object.assign(E("p"), { fv: true }) });
  ok(!shown(g), "focus moving to text takes the box away");
  g.fire("focusin", { target: fig });
  const before = g.posted.length;
  g.fire("keydown", ev({ key: "Escape", target: fig }));
  const off = g.posted.slice(before).filter(([m]) => m.type === "artifact-comment-off");
  ok(off.length === 1 && off[0][1] === GALLERY && Object.keys(off[0][0]).length === 1 && !shown(g), "Escape tells the gallery Comment is off, with only its type, and puts the box away");
  ok(g.posted.filter(([m]) => m.type === "artifact-comment-key").length === 0, "no chord was needed");
}
// 25. what a pin carries: capped evidence, in the charset the service accepts
{
  const long = "word ".repeat(80);
  const t = el("table", "intro", {}, long);
  t.rect = rectOf(20, 8, 300, 30);
  const h = E("h2", "Costs"); h.id = "costs";
  const g = sized(build(E("body", E("p", "Before the spot, a long run of words that go on."), h, t, E("p", "After the spot comes this sentence, also long.")), {}, { hit: () => t }));
  const msg = pickWith(g, 30, 25)[0];
  ok(JSON.stringify(Object.keys(msg).sort()) === '["anchor","quote","rect","type"]', "a pin message is the anchor, the quote and the rectangle");
  ok(msg.quote.length <= 200 && msg.quote.startsWith("word word"), "the quote is the element's text, capped at 200: " + msg.quote.length);
  const a = msg.anchor;
  ok(a.kind === "element" && a.id === "intro" && a.tag === "table", "kind, id and tag: " + JSON.stringify(a));
  ok(a.heading === "Costs" && a.heading_id === "costs", "the heading it sits under, and that heading's id");
  ok(!("el_id" in a) && !("path" in a && a.path.length > 512), "no key the service would refuse");
  ok(typeof a.prefix === "string" && a.prefix.length <= 32 && a.prefix.endsWith("Costs"), "32 characters of the words before: " + JSON.stringify(a.prefix));
  ok(typeof a.suffix === "string" && a.suffix.length <= 32 && a.suffix.startsWith("After the spot"), "and after: " + JSON.stringify(a.suffix));
  ok(a.path === "body>table:nth-of-type(1)", "a fast path, only: " + a.path);
  ok(!("ordinal" in a), "no ordinal for an element with an id and text");
  ok(JSON.stringify(msg).length < 2000, "and the whole message is small");
}

// 26. an image: its alt text, else its figure's caption, and an ordinal when it has no id
{
  const fig = E("figure"), cap = E("figcaption", "Revenue, 2026");
  const img = el("img", "", { alt: "Revenue by quarter" }), bare = el("img");
  fig.appendChild(bare); fig.appendChild(cap);
  const both = E("body", E("h2", "Costs"), img, fig);
  let pick = img;
  const g = sized(build(both, {}, { hit: () => pick }));
  img.rect = rectOf(10, 0, 100, 100); bare.rect = rectOf(200, 0, 100, 100);
  let m1 = pickWith(g, 5, 5)[0];
  ok(m1.quote === "Revenue by quarter" && m1.anchor.label === "Revenue by quarter" && m1.anchor.tag === "img", "an image is quoted by its alt text, which is also its label: " + JSON.stringify(m1.anchor));
  ok(m1.anchor.ordinal === 1 && m1.anchor.heading === "Costs", "with no id, its place among the images under its heading");
  pick = bare;
  m1 = pickWith(g, 5, 205)[0];
  ok(m1.quote === "Revenue, 2026" && m1.anchor.label === "Revenue, 2026" && m1.anchor.ordinal === 2, "an image with no alt text takes its figure's caption: " + JSON.stringify(m1.anchor));
}

// 27. a table cell: its row and its column, left out when they cannot be told
{
  const th1 = E("th", "Plan"), th2 = E("th", "Price");
  const c1 = E("td", "Pro"), c2 = E("td", "$12 a month");
  const table = E("table", E("thead", E("tr", th1, th2)), E("tbody", E("tr", c1, c2)));
  let pick = c2;
  const g = sized(build(E("body", E("h2", "Costs"), table), {}, { hit: () => pick }));
  c2.rect = rectOf(60, 100, 80, 20); c1.rect = rectOf(60, 10, 80, 20);
  let [m] = [pickWith(g, 110, 65)[0]];
  ok(m.anchor.tag === "td" && m.anchor.row === "Pro" && m.anchor.column === "Price" && m.quote === "$12 a month", "a cell names its row and column: " + JSON.stringify(m.anchor));
  pick = c1; m = pickWith(g, 10, 65)[0];
  ok(!("row" in m.anchor) && m.anchor.column === "Plan", "the first cell is its own row header: no row label");
  th2.attrs.colspan = "2"; pick = c2; m = pickWith(g, 110, 65)[0];
  ok(!("column" in m.anchor) && m.anchor.row === "Pro", "a colspan in the header makes the column ambiguous: left out, never mislabelled");
}

// 28. an element with nothing to go by gets no box and no pin
{
  const lone = el("img");
  lone.rect = rectOf(0, 0, 10, 10);
  const plain = el("div"); plain.rect = rectOf(20, 0, 10, 10);
  let pick = lone;
  const g = sized(build(E("body", lone, plain), {}, { hit: () => pick }));
  lit(g); hover(g, 5, 5); clickAt(g, 5, 5);
  ok(!shown(g) && pins(g).length === 0, "no id, no heading, no text, no label: no box, so nothing that could not be found again");
  pick = plain; hover(g, 5, 25); clickAt(g, 5, 25);
  ok(!shown(g) && pins(g).length === 0, "and a plain block that is no chart, table or picture gets none either");
}

// the chord is still forwarded, and nothing else is
{
  const g = sized(build(E("body", E("p", "x")), {}));
  const ck = () => g.posted.filter(([m]) => m && m.type === "artifact-comment-key");
  let k = ev({ key: "M", target: E("p"), ctrlKey: true, shiftKey: true }); g.fire("keydown", k);
  ok(ck().length === 1 && ck()[0][1] === GALLERY && Object.keys(ck()[0][0]).length === 1, "Ctrl+Shift+M tells the gallery, with only its type");
  g.fire("keydown", ev({ key: "m", target: E("p"), metaKey: true, shiftKey: true }));
  ok(ck().length === 2, "Cmd+Shift+M the same");
  g.fire("keydown", ev({ key: "m", target: E("p") }));
  g.fire("keydown", ev({ key: "M", target: E("p"), shiftKey: true }));
  g.fire("keydown", ev({ key: "m", target: E("p"), ctrlKey: true }));
  g.fire("keydown", ev({ key: "M", target: E("p"), ctrlKey: true, shiftKey: true, altKey: true }));
  ok(ck().length === 2, "no single letter, and no other modifier mix, does it");
}

// 30. touch: while lit, a tap on a table is a comment on it; wrappers are no candidate; an id is preferred
{
  const pic = el("img", "", { alt: "Map" }), para = el("p", "", {}, "text"), box = el("div", "card", {}, pic, para), wrapper = el("div", "app", {}, box);
  const body = E("body", wrapper);
  body.scrollHeight = 1000; body.scrollWidth = 800;
  wrapper.rect = rectOf(0, 0, 800, 1000); box.rect = rectOf(100, 100, 300, 40); para.rect = rectOf(110, 110, 280, 20); pic.rect = rectOf(100, 100, 30, 30);
  let pick = wrapper;
  const g = sized(build(body, {}, { hit: () => pick }));
  lit(g);
  hover(g, 5, 5);
  ok(!shown(g), "over a wrapper that is the whole page: no box");
  pick = para; hover(g, 120, 115);
  ok(shown(g) && /top:100px/.test(hostOf(g).style.cssText) && /left:100px/.test(hostOf(g).style.cssText), "the nearest ancestor with an id is preferred when it is not much larger, and holds a picture: " + hostOf(g).style.cssText);
  for (let i = 0; i < 20; i++) g.fire("pointermove", { clientX: 120 + i, clientY: 115, pointerType: "mouse" });
  ok(g.flush() === 1, "a burst of moves is one check per animation frame");
}
{
  const link = el("a", "", { href: "/x" }, "Open"), cell = E("td", link), table = el("table", "t", {}, E("tr", cell));
  table.rect = rectOf(50, 10, 300, 60); cell.rect = rectOf(50, 10, 100, 30);
  let pick = link;
  const g = sized(build(E("body", E("p", "x"), table), {}, { hit: () => pick }));
  lit(g);
  const d = ev({ clientX: 20, clientY: 60, pointerType: "touch" });
  g.fire("pointerdown", d);
  ok(shown(g) && d.prevented === 0 && d.stopped === 0, "a touch on a table draws its box, and the touch itself is untouched");
  const tap = ev({ target: link }); g.fire("click", tap);
  ok(tap.prevented === 0 && pins(g).length === 0, "a tap on a link in it still opens the link");
  pick = cell;
  const tap2 = ev({ target: cell }); g.fire("click", tap2);
  ok(tap2.prevented === 1 && pins(g).length === 1 && pins(g)[0][0].anchor.tag === "td", "a tap on the cell starts a comment on that cell");
  pick = null; g.fire("pointerdown", { clientX: 600, clientY: 500, pointerType: "touch" });
  ok(!shown(g), "a touch elsewhere puts the box away");
}

// 30b. the box is never in what the page reports: not in a quote, a path or a selection
{
  const t = el("table", "plans", {}, "Plan Pro costs twelve dollars");
  t.rect = rectOf(100, 10, 300, 50);
  const p = E("p", "Rotate the keys every 90 days.");
  const g = sized(build(E("body", p, t), {}, { immediateTimers: true, hit: () => t }));
  lit(g); hover(g, 30, 105);
  ok(shown(g), "(the box is up)");
  g.setSelection({ rangeCount: 1, toString: () => "Rotate the keys", getRangeAt: () => { const r = new g.Range(); r.setStart(p.childNodes[0], 0); r.setEnd(p.childNodes[0], 15); return r; } });
  g.fireDoc("selectionchange");
  const [[sm]] = g.posted.filter(([m]) => m && m.type === "artifact-selection");
  ok(sm.quote === "Rotate the keys" && !/Comment/.test(JSON.stringify(sm)), "a selection report never carries the button's word");
  ok(sm.anchor.path === "body>p:nth-of-type(1)", "nor does it move the path");
  g.setSelection(null); const m = pickWith(g, 30, 105)[0];
  ok(!/Comment/.test(JSON.stringify(m)) && m.quote === "Plan Pro costs twelve dollars", "a pin's quote and evidence never carry it either");
  ok(g.send && (g.send([mark({ quote: "Comment", anchor: { path: "nope" } })]), g.reports().at(-1)[0].unplaced.join() === "m1"), "and text searches cannot find 'Comment' on the page");
  ok(g.doc.documentElement.childNodes.filter((n) => n.nodeName === "AR-COMMENT-HOST").length === 1, "there is exactly one host, however often it is shown");
}

// 31. the rectangles are sent again when the frame is resized
{
  const table = el("table", "plans", {}, E("tr", E("td", "Pro")));
  table.rect = rectOf(100, 10, 300, 50);
  const g = build(E("body", table), {}, { immediateTimers: true });
  g.send([elMark({ anchor: { id: "plans" } })]);
  g.fire("resize", {});
  const rs = g.posted.filter(([m]) => m && m.type === "artifact-pin-rects");
  ok(rs.length === 1 && rs[0][1] === GALLERY && rs[0][0].rects[0].id === "e1", "a resize posts the pins' rectangles again, to the gallery only");
  table.rect = rectOf(130, 10, 200, 50);
  g.fire("resize", {});
  ok(g.posted.filter(([m]) => m && m.type === "artifact-pin-rects")[1][0].rects[0].top === 130, "as they are now");
}

// 33. SHOW A MARK. The gallery names a mark by id (a comment pressed in its panel); the
// frame answers where it is, to the gallery only, and flashes the painted words.
{
  const p = E("p", QUOTE), q = E("p", "Another paragraph further down the page.");
  const rects = new Map([[p.childNodes[0], [{ top: 10, left: 5, width: 200, height: 18 }]]]);
  p.rect = { top: 10, left: 5, width: 200, height: 18 };
  const g = build(E("body", p, q), { "body>p:nth-of-type(1)": p, "body>p:nth-of-type(2)": q }, { rects });
  g.send([mark()]);
  const at = () => g.posted.filter(([m]) => m && m.type === "artifact-mark-at");
  g.raw({ type: "artifact-mark-show", id: "m1" });
  ok(at().length === 1 && at()[0][1] === GALLERY && at()[0][0].id === "m1"
     && at()[0][0].rect.top === 10 && at()[0][0].rect.height === 18,
     "naming a painted mark answers with its rect, to the gallery only");
  ok(g.registry.get("ar-flash") && g.registry.get("ar-flash").ranges.length === 1, "and flashes the words");
  g.raw({ type: "artifact-mark-show", id: "nope" });
  g.raw({ type: "artifact-mark-show" });
  ok(at().length === 1, "an unknown or missing id answers nothing");
  let threw = false;
  try { g.raw(null); g.raw({ type: "artifact-mark-show", id: 5 }); } catch (e) { threw = true; }
  ok(!threw, "a malformed request never throws into the page");
}

// 32. the shim never reads the page's HTML or writes into it for a pin
ok(!/innerHTML|outerHTML|insertAdjacentHTML/.test(src), "no HTML is read or written by the shim");

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
