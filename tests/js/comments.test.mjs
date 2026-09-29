// Drive the REAL /comments.js against a minimal fake DOM (homelab-ij8.18,
// extended for page review step 1 / homelab-ij8.108): a selection reported by
// the page puts a three-action bar (Comment / Remove this / Looks good) under
// it. Comment fills the composer and opens the drawer, as before; Remove
// this/Looks good set kind and submit the mark on one tap when there is no
// note to add, and leave an in-progress note alone.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");
let fails = 0, passes = 0;
const ok = (c, m) => { c ? passes++ : (fails++, console.log("FAIL: " + m)); if (c) console.log("ok   " + m); };

function classes() {
  const set = new Set();
  return { set, add: (c) => set.add(c), remove: (c) => set.delete(c), contains: (c) => set.has(c),
           toggle(c, on) { if (on === undefined) on = !set.has(c); on ? set.add(c) : set.delete(c); return on; } };
}
function el(tag) {
  return { tag, style: {}, hidden: false, children: [], attrs: {}, on: {}, value: "", textContent: "", dataset: {},
           classList: classes(),
           setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
           replaceChild(n, o) { const i = this.children.indexOf(o); if (i >= 0) this.children[i] = n; n.parentNode = this; o.parentNode = null; },
           contains(n) { while (n) { if (n === this) return true; n = n.parentNode; } return false; },
           appendChild(c) { this.children.push(c); c.parentNode = this; },
           insertBefore(c) { this.children.unshift(c); c.parentNode = this; },
           addEventListener(t, f) { this.on[t] = f; }, focus() { this.focused = true; },
           closest(sel) {
             // A tag, or a class (".x"), or a comma list of either.
             const hit = (node) => sel.split(",").map((x) => x.trim()).some((x) =>
               x.startsWith(".") ? node.className === x.slice(1) : node.tag === x.replace(/^\W+/, ""));
             let node = this;
             while (node) { if (hit(node)) return node; node = node.parentNode; }
             return null;
           } };
}
function build({ marks = "[]", fetchImpl = null, noForm = false, stored = undefined, storage = true, hash = "", noPanel = false } = {}) {
  const framePosts = [];
  const frameWin = { postMessage: (m, o) => framePosts.push([m, o]) };
  const frameOn = {};
  const iframe = { src: "https://abc.tenfourty.com/p/x/", contentWindow: frameWin,
                   addEventListener: (t, fn) => ((frameOn[t] ||= []).push(fn)),
                   getBoundingClientRect: () => ({ top: 100, left: 20 }) };
  // The page embeds its open marks as JSON (null: the page has no such block),
  // and renders an empty, hidden line the shim's report fills.
  const marksEl = marks === null ? null : { textContent: marks };
  const unplacedEl = el("p"); unplacedEl.hidden = true;
  const body = el("input"); body.name = "body";
  const form = el("form");
  form.submits = 0;
  form.requestSubmit = () => { form.submits++; };
  form.querySelector = (q) => {
    if (q === "input[name=body]") return body;
    if (q === ".cquote") return form.children.find((c) => c.className === "cquote") || null;
    const m = q.match(/input\[name=(\w+)\]/);
    return m ? form.children.find((c) => c.name === m[1]) || null : null;
  };
  const docBody = el("body");
  // The panel, its close link (a link to "#" as the server renders it) and
  // the bar's comments button.
  const panel = el("aside"); panel.id = "comments";
  const close = el("a"); close.className = "cclose"; close.textContent = "\u00d7";
  panel.appendChild(close);
  panel.querySelector = (q) => (q === ".cclose" ? panel.children.find((c) => c.className === "cclose") || null : null);
  const cbtn = el("a"); cbtn.className = "cbtn";
  const byId = { "artifact-frame": iframe, "artifact-marks": marksEl, "marks-unplaced": unplacedEl, comments: noPanel ? null : panel };
  const docOn = {}, extra = { ".cbtn": cbtn };
  const doc = { getElementById: (id) => byId[id] || null,
                createElement: (t) => { const n = el(t); if (t === "template") Object.defineProperty(n, "innerHTML", { set(h) { n.content = { firstElementChild: { html: h } }; } }); return n; },
                body: docBody,
                addEventListener: (t, f) => ((docOn[t] ||= []).push(f)),
                querySelector: (q) => (q === "form.cnew" ? (noForm ? null : form) : extra[q] || null) };
  const listeners = {}, capture = {};
  const store = new Map(stored === undefined ? [] : [["artifacts.comments.open", stored]]);
  const localStorage = storage ? { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) }
    : { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); } };
  const history = { entries: 0, replaced: [], pushState() { this.entries++; }, replaceState(s, t, u) { this.replaced.push(u); } };
  const win = { scrollX: 0, scrollY: 50, localStorage, history,
                addEventListener: (t, f, c) => (((c ? capture : listeners)[t] ||= []).push(f)) };
  const location = { hash, pathname: "/a/abc", search: "" };
  const FD = function (f) { this.form = f; };
  new Function("document", "window", "location", "URL", "fetch", "FormData", src)(doc, win, location, URL, fetchImpl || undefined, FD);
  const bar = docBody.children.find((c) => c.className === "selbar");
  const buttons = {};
  if (bar) bar.children.forEach((b) => (buttons[b.dataset.kind] = b));
  const send = (data, { source = frameWin, origin = "https://abc.tenfourty.com" } = {}) =>
    (listeners.message || []).forEach((fn) => fn({ source, origin, data }));
  const click = (kind) => bar.on.click({ target: buttons[kind] });
  const reload = () => (frameOn.load || []).forEach((fn) => fn({}));
  const submit = (fm, e = {}) => { const ev = { target: fm, prevented: false, preventDefault() { ev.prevented = true; }, ...e };
    (docOn.submit || []).forEach((fn) => fn(ev)); return ev; };
  const tap = (target) => { const ev = { target, prevented: false, preventDefault() { ev.prevented = true; } };
    (docOn.click || []).forEach((fn) => fn(ev)); return ev; };
  const key = (k) => (capture.keydown || []).concat(listeners.keydown || [], docOn.keydown || []).forEach((fn) => fn({ key: k }));
  const isOpen = () => docBody.classList.contains("cpanel-open");
  return { bar, buttons, form, body, location, send, click, framePosts, unplacedEl, reload, submit, extra, doc,
           panel, cbtn, docBody, store, history, tap, key, isOpen };
}
const SEL = { type: "artifact-selection", quote: "the plan",
              anchor: { kind: "range", path: "body>p:nth-of-type(1)", start: 4, end: 12 },
              rect: { top: 30, left: 10, width: 60, height: 18 } };

{ const g = build();
  ok(g.bar && g.bar.hidden === true, "a hidden action bar exists");
  ok(["comment", "remove", "looks_good"].every((k) => g.buttons[k]), "with all three actions");
  g.send(SEL);
  ok(g.bar.hidden === false, "a selection shows it");
  ok(g.bar.style.top === "204px" && g.bar.style.left === "30px", "under the selection, in page coordinates");
  g.click("comment");
  const quote = g.form.children.find((c) => c.name === "quote");
  const anchor = g.form.children.find((c) => c.name === "anchor");
  const kind = g.form.children.find((c) => c.name === "kind");
  ok(quote && quote.value === "the plan", "the quote goes into the form");
  ok(anchor && JSON.parse(anchor.value).kind === "range", "and the anchor");
  ok(kind && kind.value === "comment", "and the kind");
  ok(g.form.children.find((c) => c.className === "cquote").textContent === "the plan", "the quote is shown, as text");
  ok(g.isOpen() && g.location.hash === "" && g.body.focused, "the panel opens, by class, with the cursor in the box");
  ok(g.history.entries === 0 && g.history.replaced.length === 0, "and without touching the history");
  ok(g.body.placeholder === "Comment on the selected text", "asking for a comment");
  ok(g.bar.hidden === true, "and the bar goes");
  ok(g.form.submits === 0, "a comment is never auto-submitted"); }
{ const g = build();
  g.send(SEL, { origin: "https://evil.example" });
  g.send(SEL, { source: {} });
  ok(g.bar.hidden === true, "another origin or window cannot show it"); }
{ const g = build();
  g.send(SEL); g.send({ type: "artifact-selection", quote: "" });
  ok(g.bar.hidden === true, "clearing the selection hides it");
  g.send({ ...SEL, rect: { top: "x", left: 1, height: 1 } });
  ok(g.bar.hidden === true, "a malformed rect is ignored"); }
{ const g = build();
  g.send(SEL);
  g.click("remove");
  const kind = g.form.children.find((c) => c.name === "kind");
  ok(kind && kind.value === "remove", "remove sets kind");
  ok(g.body.placeholder === "Add a note (optional)", "and offers an optional note");
  ok(g.form.submits === 1, "an empty note submits the mark on one tap"); }
{ const g = build();
  g.send(SEL);
  g.body.value = "already typing a note";
  g.click("looks_good");
  const kind = g.form.children.find((c) => c.name === "kind");
  ok(kind && kind.value === "looks_good", "looks_good sets kind");
  ok(g.form.submits === 0, "a note already in progress is never submitted for them"); }
{ const g = build();
  g.send(SEL);
  g.bar.on.click({ target: g.bar });
  ok(g.form.children.find((c) => c.name === "kind") === undefined, "a click that hits no button does nothing"); }

// Marks on the page (page review step 1, Task 5): the embedded marks go into
// the frame once the page says it is there, once per load, only to the frame's
// own origin; its report of what it could not place becomes one line.
const MARKS = JSON.stringify([
  { id: "m1", kind: "remove", quote: "the plan", anchor: SEL.anchor },
  { id: "m2", kind: "comment", quote: "the rest", anchor: SEL.anchor }]);
const STATE = { type: "artifact-state", height: 400, slots: [] };
{ const g = build({ marks: MARKS });
  ok(g.framePosts.length === 0, "no marks are posted before the page reports it is there");
  g.send(STATE);
  ok(g.framePosts.length === 1, "the page's first state report sends the marks");
  const [msg, origin] = g.framePosts[0];
  ok(origin === "https://abc.tenfourty.com", "only to the frame's own origin, never '*'");
  ok(msg.type === "artifact-marks" && msg.marks.map((m) => m.id).join() === "m1,m2", "carrying the embedded marks");
  g.send(STATE); g.send(STATE);
  ok(g.framePosts.length === 1, "and only once per load, however often the page resizes");
  g.reload();
  ok(g.framePosts.length === 2, "a new load of the frame gets them again, at once, without waiting for a state report");
  g.send(STATE);
  ok(g.framePosts.length === 2, "and not a second time when that document then reports its state"); }
{ const g = build({ marks: MARKS });
  g.send(STATE, { origin: "https://evil.example" });
  g.send(STATE, { source: {} });
  ok(g.framePosts.length === 0, "another origin or window cannot ask for the marks"); }
{ for (const [marks, why] of [["[]", "no marks"], [null, "no marks block"], ["{not json", "a broken block"], ['{"a":1}', "a block that is not a list"]]) {
    const g = build({ marks });
    let threw = false;
    try { g.send(STATE); } catch (e) { threw = true; }
    ok(!threw && g.framePosts.length === 0, `nothing is posted for ${why}`);
  } }
{ const g = build({ marks: MARKS });
  g.send(STATE);
  g.send({ type: "artifact-marks-placed", placed: ["m2"], unplaced: ["m1"] });
  ok(g.unplacedEl.hidden === false && g.unplacedEl.textContent === "1 mark could not be placed on this version of the page.",
     "one unplaced mark is said, in the singular");
  g.send({ type: "artifact-marks-placed", placed: [], unplaced: ["m1", "m2", "m1", "invented", 7, null] });
  ok(g.unplacedEl.textContent === "2 marks could not be placed on this version of the page.",
     "the count is of this page's own marks, each once -- the page cannot inflate it");
  g.send({ type: "artifact-marks-placed", placed: ["m1", "m2"], unplaced: [] });
  ok(g.unplacedEl.hidden === true, "the line goes when every mark is placed");
  g.send({ type: "artifact-marks-placed", unplaced: ["m1"] }, { origin: "https://evil.example" });
  ok(g.unplacedEl.hidden === true, "another origin cannot write the line");
  let threw = false;
  try { g.send({ type: "artifact-marks-placed", unplaced: "m1" }); g.send({ type: "artifact-marks-placed" }); }
  catch (e) { threw = true; }
  ok(!threw && g.unplacedEl.hidden === true, "a malformed report is ignored"); }
// homelab-ij8.111, follow-up Item 2: the shim caps the unplaced ids it sends
// and reports the rest as a count, `overflow`. The line's own count must add
// that in, and must not trust it blindly -- a forged or nonsense value is
// never rendered as-is.
{ const g = build({ marks: MARKS });
  g.send(STATE);
  g.send({ type: "artifact-marks-placed", placed: [], unplaced: ["m1"], overflow: 3 });
  ok(g.unplacedEl.textContent === "4 marks could not be placed on this version of the page.",
     "the overflow count is added to the ids actually named");
  g.send({ type: "artifact-marks-placed", placed: ["m1", "m2"], unplaced: [], overflow: 0 });
  ok(g.unplacedEl.hidden === true, "an overflow of zero says nothing on its own");
  g.send({ type: "artifact-marks-placed", placed: [], unplaced: ["m1"], overflow: -5 });
  ok(g.unplacedEl.textContent === "1 mark could not be placed on this version of the page.",
     "a negative overflow is nonsense and ignored, not subtracted");
  g.send({ type: "artifact-marks-placed", placed: [], unplaced: ["m1"], overflow: 1.5 });
  ok(g.unplacedEl.textContent === "1 mark could not be placed on this version of the page.",
     "a non-integer overflow is ignored the same way");
  g.send({ type: "artifact-marks-placed", placed: [], unplaced: ["m1"], overflow: 1e15 });
  ok(g.unplacedEl.textContent === "100001 marks could not be placed on this version of the page.",
     "an absurd overflow from a forged message is capped sanely, not rendered verbatim"); }
{ const g = build({ marks: MARKS });
  g.send(SEL);
  ok(g.bar.hidden === false && g.framePosts.length === 0, "a selection still shows the bar and sends no marks"); }

// Posting in place: the submit is intercepted, sent with fetch as JSON, the
// thread is put in, the form clears, the marks are re-sent; any failure falls
// back to the plain submit.
const tick = () => new Promise((r) => setTimeout(r, 0));
const OKRESP = (marks) => ({ status: 201, json: async () => ({ html: "<li>t</li>", thread_root: "r1", marks }) });
{ let calls = [];
  const g = build({ marks: MARKS, fetchImpl: (url, o) => { calls.push([url, o]); return Promise.resolve(OKRESP([{ id: "n1", kind: "comment", quote: "q", anchor: SEL.anchor }])); } });
  const f = g.form; f.className = "cnew"; f.action = "/c/abc"; f.submitted = 0; f.submit = () => { f.submitted++; };
  const btn = { disabled: false }; f.querySelectorAll = () => [btn];
  const list = el("ul"); list.appended = []; list.appendChild = (c) => list.appended.push(c);
  g.extra["#comments > ul.threads"] = list;
  g.body.value = "hello";
  g.send({ type: "artifact-state" });
  const before = g.framePosts.length;
  const ev = g.submit(f);
  ok(ev.prevented === true, "the submit is intercepted");
  ok(btn.disabled === true, "the form is disabled while the post is in flight");
  const ev2 = g.submit(f);
  ok(calls.length === 1 && ev2.prevented === true, "a second submit while in flight posts nothing more");
  ok(calls[0][0] === "/c/abc" && calls[0][1].method === "POST" && calls[0][1].headers.Accept === "application/json"
     && calls[0][1].credentials === "same-origin", "it posts JSON-accepting, same-origin, to the form's action");
  await tick(); await tick(); await tick();
  ok(list.appended.length === 1 && list.appended[0].html === "<li>t</li>", "a new root thread is appended to the open threads");
  ok(g.body.value === "" && btn.disabled === false, "the input clears and the form is usable again");
  ok(f.submitted === 0, "no native submit on success");
  const last = g.framePosts[g.framePosts.length - 1][0];
  ok(g.framePosts.length === before + 1 && last.marks.length === 1 && last.marks[0].id === "n1",
     "the frame is sent the marks from the response"); }
{ for (const bad of [() => Promise.reject(new Error("net")), () => Promise.resolve({ status: 500, json: async () => ({}) })]) {
    const g = build({ fetchImpl: bad });
    const f = g.form; f.className = "creply"; f.action = "/c/abc"; f.submitted = 0; f.submit = () => { f.submitted++; };
    const btn = { disabled: false }; f.querySelectorAll = () => [btn];
    g.submit(f); await tick(); await tick(); await tick();
    ok(f.submitted === 1 && btn.disabled === false, "a failed post falls back to the plain form submit"); } }
{ const g = build({ fetchImpl: () => { throw new Error("must not"); } });
  const other = el("form"); other.className = "cresolve";
  const ev = g.submit(other);
  ok(ev.prevented === false, "resolving a thread keeps its reload"); }

// After a 201 the comment is saved: nothing that fails later may post it again.
{ const g = build({ fetchImpl: () => Promise.resolve(OKRESP([])) });
  const f = g.form; f.className = "cnew"; f.action = "/c/abc"; f.submitted = 0; f.submit = () => { f.submitted++; };
  const btn = { disabled: false }; f.querySelectorAll = () => [btn];
  g.doc.createElement = () => { throw new Error("boom"); };
  let reloads = 0; g.location.reload = () => { reloads++; };
  g.submit(f); await tick(); await tick(); await tick();
  ok(f.submitted === 0, "a failure after the 201 never submits the form again");
  ok(reloads === 1, "it reloads to show what was saved instead"); }
// A 201 that says reload (the server saved it but could not build the reply).
{ const g = build({ fetchImpl: () => Promise.resolve({ status: 201, json: async () => ({ reload: true }) }) });
  const f = g.form; f.className = "cnew"; f.action = "/c/abc"; f.submitted = 0; f.submit = () => { f.submitted++; };
  f.querySelectorAll = () => [];
  let reloads = 0; g.location.reload = () => { reloads++; };
  g.submit(f); await tick(); await tick(); await tick();
  ok(reloads === 1, "a 201 with reload reloads the page");
  ok(f.submitted === 0, "and never submits the form again"); }
// The count on the bar goes up when a new root comes back, not for a reply.
{ const g = build({ fetchImpl: () => Promise.resolve(OKRESP([])) });
  const f = g.form; f.className = "cnew"; f.action = "/c/abc";
  f.querySelectorAll = () => [];
  const cn = { textContent: "2" }; g.extra[".cbtn .cn"] = cn;
  const list = el("ul"); g.extra["#comments > ul.threads"] = list;
  g.submit(f); await tick(); await tick(); await tick();
  ok(cn.textContent === "3", "a new root thread bumps the count on the bar"); }
// The all-pages view has no new-comment form, and replies still post in place.
{ const g = build({ fetchImpl: () => Promise.resolve(OKRESP([])), noForm: true });
  const f = el("form"); f.className = "creply"; f.action = "/c/other"; f.submitted = 0; f.submit = () => { f.submitted++; };
  f.querySelectorAll = () => []; f.querySelector = () => null;
  const li = el("li"); li.parentNode = { replaced: null, replaceChild(n, o) { this.replaced = n; } };
  f.closest = () => li;
  const ev = g.submit(f); await tick(); await tick(); await tick();
  ok(ev.prevented === true && li.parentNode.replaced && li.parentNode.replaced.html === "<li>t</li>" && f.submitted === 0,
     "a reply in the all-pages view is posted in place"); }

// THE DOCKED PANEL. The bar's button and the close control toggle a class on
// <body>; nothing navigates, so no fragment, no history entry, no reflow of a
// tall page under a blurred backdrop. Escape closes it; the state is kept per
// viewer in localStorage and a broken or refused storage costs only the memory.
{ const g = build();
  ok(!g.isOpen(), "the panel starts closed for a viewer with no memory of it");
  ok(g.docBody.classList.contains("cjs"), "the script marks the page as scripted, so :target stops driving the panel");
  const btn = g.panel.children.find((c) => c.className === "cclose");
  ok(btn && btn.tag === "button" && btn.type === "button", "the close link becomes a real button");
  ok(btn.attrs["aria-label"] === "Close comments" && btn.textContent === "\u00d7", "keeping its name and its mark");
  let ev = g.tap(g.cbtn);
  ok(ev.prevented && g.isOpen(), "the comments button opens the panel without following its link");
  ok(g.location.hash === "" && g.history.entries === 0 && g.history.replaced.length === 0, "no fragment and no history entry");
  ok(g.cbtn.attrs["aria-expanded"] === "true", "the button says it is expanded");
  ok(g.store.get("artifacts.comments.open") === "1", "open is remembered");
  ev = g.tap(g.cbtn);
  ok(ev.prevented && !g.isOpen() && g.cbtn.attrs["aria-expanded"] === "false", "pressing it again closes it");
  ok(g.store.get("artifacts.comments.open") === "0", "and closed is remembered");
  g.tap(g.cbtn); ev = g.tap(btn);
  ok(ev.prevented && !g.isOpen(), "the close button closes it");
  const other = el("p"); other.closest = () => null;
  ev = g.tap(other);
  ok(!ev.prevented && !g.isOpen(), "a click anywhere else is left alone"); }
{ const g = build();
  g.tap(g.cbtn); g.key("Enter");
  ok(g.isOpen(), "a key other than Escape leaves it open");
  g.key("Escape");
  ok(!g.isOpen() && g.store.get("artifacts.comments.open") === "0", "Escape closes it, and that is remembered");
  g.extra[".dlg:target,details.menu[open]"] = el("div");
  g.tap(g.cbtn); g.key("Escape");
  ok(g.isOpen(), "Escape with a dialog or menu open closes that first, not the panel"); }
{ const g = build({ stored: "1" });
  ok(g.isOpen() && g.cbtn.attrs["aria-expanded"] === "true", "a viewer who left it open finds it open"); }
{ const g = build({ stored: "0" });
  ok(!g.isOpen(), "and one who closed it finds it closed"); }
{ const g = build({ stored: "0", hash: "#comments" });
  ok(g.isOpen(), "a link to #comments opens it, whatever was remembered");
  ok(g.history.entries === 0 && g.history.replaced.length === 1 && g.history.replaced[0] === "/a/abc",
     "and the fragment is dropped in place, so a reload obeys the memory rather than the old link"); }
{ let threw = false, g;
  try { g = build({ storage: false }); g.tap(g.cbtn); } catch (e) { threw = true; }
  ok(!threw && g.isOpen(), "storage that throws costs only the memory: the panel still opens"); }
{ let threw = false;
  try { build({ noPanel: true }); } catch (e) { threw = true; }
  ok(!threw, "a page with no panel is left alone"); }
{ const g = build();
  g.send(SEL); g.tap(g.cbtn);
  ok(g.bar.hidden === true, "toggling the panel hides a selection bar whose position the reflow just made wrong"); }

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
