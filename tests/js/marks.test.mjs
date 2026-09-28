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
function T(v) { return { nodeType: 3, nodeValue: v, parentNode: null }; }
function E(tag, ...kids) {
  const e = { nodeType: 1, nodeName: tag.toUpperCase(), tagName: tag.toUpperCase(), childNodes: [], parentNode: null,
              style: {}, id: "", scrollHeight: 0, offsetHeight: 0,
              // Only for the shim's height report, which the `load` in case 11 also fires.
              getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, bottom: 0 }),
              appendChild(c) { this.childNodes.push(c); c.parentNode = this; return c; } };
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

function build(body, paths, { highlights = true, readyState = "complete" } = {}) {
  const head = E("head");
  const html = E("html", head, body);
  const listeners = {};
  const posted = [];
  const all = () => texts(html);
  class Range {
    setStart(n, o) { if (n.nodeType !== 3 || o < 0 || o > n.nodeValue.length) throw new Error("IndexSizeError"); this.sc = n; this.so = o; }
    setEnd(n, o) { if (n.nodeType !== 3 || o < 0 || o > n.nodeValue.length) throw new Error("IndexSizeError"); this.ec = n; this.eo = o; }
    get startContainer() { return this.sc; }
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
    getElementById: (id) => head.childNodes.find((c) => c.id === id) || null,
    querySelector(sel) {
      if (sel.includes("!!")) throw new SyntaxError("bad selector");
      return paths[sel] || null;
    },
    querySelectorAll: () => [],
  };
  class Highlight { constructor() { this.ranges = []; } add(r) { this.ranges.push(r); } }
  const registry = new Map();
  const scope = {
    document: doc,
    parent: { postMessage: (m, o) => posted.push([m, o]) },
    scrollX: 0, scrollY: 0,
    addEventListener: (t, fn) => (listeners[t] ||= []).push(fn),
    getComputedStyle: () => ({ marginBottom: "0px" }),
    setTimeout, clearTimeout,
    Array, JSON, Math, Object, String,
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
  return { head, body, doc, registry, posted, reports, send, raw, fire: (t) => (listeners[t] || []).forEach((fn) => fn({})) };
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
  ok(m2.placed.length + m2.unplaced.length === 200, "at most 200 marks are considered");
}

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
