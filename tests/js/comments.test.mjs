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
function matches(n, sel) {
  const m = sel.match(/^(\w+)?(?:\.([\w-]+))?(?:\[name=(\w+)\])?$/);
  if (!m) return false;
  return (!m[1] || n.tag === m[1]) && (!m[2] || String(n.className || "").split(" ").includes(m[2])) && (!m[3] || n.name === m[3]);
}
function text(n) { return (n.textContent || "") + n.children.map(text).join(""); }
function el(tag) {
  return { tag, style: {}, hidden: false, children: [], attrs: {}, on: {}, value: "", textContent: "", dataset: {},
           classList: classes(),
           setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
           replaceChild(n, o) { const i = this.children.indexOf(o); if (i >= 0) this.children[i] = n; n.parentNode = this; o.parentNode = null; },
           contains(n) { while (n) { if (n === this) return true; n = n.parentNode; } return false; },
           appendChild(c) { this.children.push(c); c.parentNode = this; },
           insertBefore(c) { this.children.unshift(c); c.parentNode = this; },
           addEventListener(t, f) { this.on[t] = f; }, focus() { this.focused = true; },
           removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; },
           scrollIntoView() { this.scrolled = true; },
           cloneNode(deep) { const n = el(this.tag);
             for (const k of ["className", "textContent", "name", "type", "value"]) n[k] = this[k];
             n.attrs = { ...this.attrs }; if (deep) this.children.forEach((c) => n.appendChild(c.cloneNode(true))); return n; },
           // "tag", ".cls", "tag.cls" or "input[name=x]", over descendants.
           querySelectorAll(sel) { const out = [];
             (function walk(n) { n.children.forEach((c) => { if (matches(c, sel)) out.push(c); walk(c); }); })(this); return out; },
           querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
           closest(sel) {
             // A tag, or a class (".x"), or a comma list of either.
             const hit = (node) => sel.split(",").map((x) => x.trim()).some((x) =>
               x.startsWith(".") ? node.className === x.slice(1) : node.tag === x.replace(/^\W+/, ""));
             let node = this;
             while (node) { if (hit(node)) return node; node = node.parentNode; }
             return null;
           } };
}
// A fake clock, so the rest and the grace period are measured, not slept.
function clock() {
  let now = 0, id = 0; const due = new Map();
  return { setTimeout: (fn, ms) => { due.set(++id, [now + (ms || 0), fn]); return id; },
           clearTimeout: (k) => { due.delete(k); },
           advance(ms) { const end = now + ms;
             for (;;) { let next = null;
               for (const [k, [t]] of due) if (t <= end && (!next || t < due.get(next)[0])) next = k;
               if (!next) break; const [t, fn] = due.get(next); due.delete(next); now = t; fn(); }
             now = end; } };
}
function build({ noPinBtn = false, marks = "[]", fetchImpl = null, noForm = false, stored = undefined, storage = true, hash = "", noPanel = false, threads = [], page = "abc", seed = null } = {}) {
  const framePosts = [];
  const frameWin = { postMessage: (m, o) => framePosts.push([m, o]) };
  const frameOn = {};
  const iframe = { src: "https://abc.tenfourty.com/p/x/", contentWindow: frameWin,
                   addEventListener: (t, fn) => ((frameOn[t] ||= []).push(fn)),
                   getBoundingClientRect: () => ({ top: 100, left: 20, width: 800, height: 3000 }) };
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
  const tlist = el("ul"); tlist.className = "threads"; threads.forEach((t) => tlist.appendChild(t)); panel.appendChild(tlist);
  const tabBtns = ["open", "resolved"].map((n) => { const b = el("button"); b.className = "ctab"; b.attrs["data-ctab"] = n;
    b.attrs["aria-selected"] = n === "open" ? "true" : "false"; return b; });
  panel.setAttribute("data-ctab", "open");
  panel.querySelectorAll = (q) => (q === "li.thread" ? tlist.children.slice() : q === ".ctab" ? tabBtns : []);
  const cbtn = el("a"); cbtn.className = "cbtn";
  const byId = { "artifact-frame": iframe, "artifact-marks": marksEl, "marks-unplaced": unplacedEl, comments: noPanel ? null : panel };
  const pinbtn = el("button"); pinbtn.className = "pinmodebtn"; pinbtn.hidden = true;
  const pagebtn = el("a"); pagebtn.className = "pagecbtn";
  const docOn = {}, extra = { ".cbtn": cbtn, ".pagecbtn": pagebtn, ...(noPinBtn ? {} : { ".pinmodebtn": pinbtn }) };
  const doc = { getElementById: (id) => byId[id] || null,
                createElement: (t) => { const n = el(t); if (t === "template") Object.defineProperty(n, "innerHTML", { set(h) { n.content = { firstElementChild: { html: h } }; } }); return n; },
                body: docBody,
                addEventListener: (t, f) => ((docOn[t] ||= []).push(f)),
                querySelector: (q) => (q === "form.cnew" ? (noForm ? null : form) : extra[q] || null) };
  const listeners = {}, capture = {};
  const store = new Map(seed || (stored === undefined ? [] : [["artifacts.comments.open:" + page, stored]]));
  const localStorage = storage ? { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) }
    : { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); } };
  const history = { entries: 0, replaced: [], pushState() { this.entries++; }, replaceState(s, t, u) { this.replaced.push(u); } };
  const win = { scrollX: 0, scrollY: 50, innerWidth: 1000, innerHeight: 700, localStorage, history,
                addEventListener: (t, f, c) => (((c ? capture : listeners)[t] ||= []).push(f)) };
  const location = { hash, pathname: "/a/" + page, search: "" };
  const FD = function (f) { this.form = f; this.fields = []; };
  FD.prototype.append = function (k, v) { this.fields.push([k, v]); };
  const clk = clock();
  new Function("document", "window", "location", "URL", "fetch", "FormData", "setTimeout", "clearTimeout", src)(
    doc, win, location, URL, fetchImpl || undefined, FD, clk.setTimeout, clk.clearTimeout);
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
  // One event through capture, then bubble; stopPropagation ends its trip.
  const key = (k, more = {}) => { const ev = { key: k, stopped: false, stopPropagation() { ev.stopped = true; }, ...more };
    for (const fn of (capture.keydown || []).concat(listeners.keydown || [], docOn.keydown || [])) { if (ev.stopped) break; fn(ev); }
    return ev; };
  const press = (target) => (docOn.pointerdown || []).forEach((fn) => fn({ target }));
  const fsChange = () => (docOn.fullscreenchange || []).forEach((fn) => fn({}));
  const isOpen = () => docBody.classList.contains("cpanel-open");
  const card = () => docBody.children.filter((c) => c.className === "hovercard");
  const scroll = () => (listeners.scroll || []).concat(capture.scroll || []).forEach((fn) => fn({}));
  return { bar, buttons, form, body, location, send, click, framePosts, unplacedEl, reload, submit, extra, doc,
           panel, cbtn, docBody, store, history, tap, key, isOpen, clk, card, scroll, tlist, press, fsChange, pinbtn, pagebtn, win, docOn, tabBtns };
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
  g.extra["#comments .cpane-open > ul.threads"] = list;
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

// Pictures on a comment: chosen files are held (four at most, pictures only), each is
// staged with POST <action>/image when the comment is sent, and the comment names them in
// repeated `image` fields. A refusal is said on the form and the words are not sent
// without them; the plain submit is never the fallback for a comment that has pictures.
{ const pic = (n) => ({ type: "image/png", name: n + ".png", size: 10 });
  const rig = (fetchImpl) => {
    const calls = [];
    const g = build({ fetchImpl: (url, o) => { calls.push([url, o]); return fetchImpl(url, o, calls.length); } });
    const f = g.form; f.className = "cnew"; f.action = "/c/abc"; f.submitted = 0; f.submit = () => { f.submitted++; };
    const btn = { disabled: false }; f.querySelectorAll = () => [btn];
    const list = el("ul"); list.appended = []; list.appendChild = (c) => list.appended.push(c);
    g.extra["#comments .cpane-open > ul.threads"] = list;
    const sayEl = { textContent: "" }, base = f.querySelector;
    f.querySelector = (q) => (q === ".cpics-say" ? sayEl : base(q));
    const choose = (files) => { const t = { files, value: "x", closest: (sel) => (sel.includes("input.cfile") ? t : sel.includes("form.cnew") ? f : null) };
      (g.docOn.change || []).forEach((fn) => fn({ target: t })); return t; };
    return { g, f, btn, sayEl, calls, choose };
  };
  { const r = rig((url, o, n) => Promise.resolve(n === 5 ? OKRESP([]) : { status: 201, json: async () => ({ image_id: "img" + n }) }));
    r.choose([pic("a"), { type: "text/plain", name: "n.txt" }, pic("b"), pic("c"), pic("d"), pic("e")]);
    ok(r.sayEl.textContent === "Four pictures at most on one comment.", "a fifth picture is refused with a line saying so");
    r.choose([pic("f")]);
    r.g.body.value = "see";
    r.g.submit(r.f); await tick(); await tick(); await tick(); await tick(); await tick(); await tick(); await tick(); await tick();
    ok(r.calls.length === 5 && r.calls.slice(0, 4).every((c) => c[0] === "/c/abc/image" && c[1].method === "POST"), "each picture is staged at the form's action plus /image, one by one");
    const sent = r.calls[4][1].body.fields.filter((x) => x[0] === "image").map((x) => x[1]);
    ok(r.calls[4][0] === "/c/abc" && sent.join() === "img1,img2,img3,img4", "the comment names the staged ids in order in repeated image fields: " + sent);
    ok(r.f.submitted === 0 && r.btn.disabled === false, "no native submit, and the form is usable again"); }
  { const r = rig(() => Promise.resolve({ status: 422, json: async () => ({ detail: { field: "image", detail: "send PNG, JPEG or WebP" } }) }));
    r.choose([pic("a")]); r.g.submit(r.f); await tick(); await tick(); await tick(); await tick();
    ok(r.calls.length === 1 && r.sayEl.textContent === "send PNG, JPEG or WebP", "a refused picture is said on the form: " + r.sayEl.textContent);
    ok(r.f.submitted === 0 && r.btn.disabled === false, "and the words are not sent without it"); }
  { const r = rig((url) => Promise.resolve(url.endsWith("/image") ? { status: 201, json: async () => ({ image_id: "img" }) } : { status: 422, json: async () => ({ detail: "not one of yours" }) }));
    r.choose([pic("a")]); r.g.submit(r.f); await tick(); await tick(); await tick(); await tick(); await tick(); await tick();
    ok(r.sayEl.textContent === "not one of yours" && r.f.submitted === 0, "a refused comment with pictures is told, never re-sent as a plain form"); }
  // A file's declared size is read from its header before anything is decoded.
  const header = (w, h) => { const b = new Uint8Array(33); b.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52]);
    const v = new DataView(b.buffer); v.setUint32(16, w); v.setUint32(20, h); return b.buffer; };
  const sized = (n, w, h, size = 10) => ({ type: "image/png", name: n + ".png", size, slice: () => ({ arrayBuffer: async () => header(w, h) }) });
  { const seen = []; globalThis.createImageBitmap = (f, o) => { seen.push(o); return Promise.resolve({ width: 1, height: 1 }); };
    const r = rig(() => Promise.resolve({ status: 201, json: async () => ({ image_id: "i" }) }));
    r.choose([sized("huge", 1000, 1000, 26 * 1024 * 1024)]); await tick();
    ok(seen.length === 0 && /25 MB/.test(r.sayEl.textContent), "a file over 25 MB is refused before it is decoded, with a line saying so: " + r.sayEl.textContent);
    r.choose([sized("bomb", 30000, 30000)]); await tick(); await tick();
    ok(seen.length === 0 && /too large/.test(r.sayEl.textContent), "a picture declaring more than 300 megapixels is never decoded");
    r.choose([sized("big", 30000, 10000)]); await tick(); await tick();
    ok(seen.length === 1 && seen[0].resizeWidth === 4000 && seen[0].resizeHeight === 1333,
       "a big one is decoded already scaled to the limits: " + JSON.stringify(seen[0]));
    delete globalThis.createImageBitmap; }
  // Send pressed before the decode finishes: the upload waits for it.
  { let finish; globalThis.createImageBitmap = () => new Promise((res) => { finish = () => res({ width: 100, height: 100 }); });
    const r = rig((url) => Promise.resolve(url.endsWith("/image") ? { status: 201, json: async () => ({ image_id: "i" }) } : OKRESP([])));
    r.choose([sized("slow", 100, 100)]); await tick();
    r.g.submit(r.f); await tick(); await tick();
    ok(r.calls.length === 0, "nothing is uploaded while the picture is still being decoded");
    finish(); await tick(); await tick(); await tick(); await tick(); await tick();
    ok(r.calls.length === 2 && r.calls[0][0] === "/c/abc/image", "it goes once the decode is done");
    delete globalThis.createImageBitmap; }
}
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
  const list = el("ul"); g.extra["#comments .cpane-open > ul.threads"] = list;
  g.submit(f); await tick(); await tick(); await tick();
  ok(cn.textContent === "3", "a new root thread bumps the count on the bar"); }
// The all-pages view has no new-comment form, and replies still post in place.
{ const g = build({ fetchImpl: () => Promise.resolve(OKRESP([])), noForm: true });
  const f = el("form"); f.className = "creply"; f.action = "/c/other"; f.submitted = 0; f.submit = () => { f.submitted++; };
  f.querySelectorAll = () => []; f.querySelector = () => null;
  const li = el("li"); li.parentNode = { replaced: null, replaceChild(n, o) { this.replaced = n; } };
  f.closest = (sel) => (sel === "li.thread" ? li : null);
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
  ok(g.store.get("artifacts.comments.open:abc") === "1", "open is remembered");
  ev = g.tap(g.cbtn);
  ok(ev.prevented && !g.isOpen() && g.cbtn.attrs["aria-expanded"] === "false", "pressing it again closes it");
  ok(g.store.get("artifacts.comments.open:abc") === "0", "and closed is remembered");
  g.tap(g.cbtn); g.cbtn.focused = false; ev = g.tap(btn);
  ok(ev.prevented && !g.isOpen(), "the close button closes it");
  ok(g.cbtn.focused === true, "and hands focus back to the comments button, not to <body>");
  const other = el("p"); other.closest = () => null;
  ev = g.tap(other);
  ok(!ev.prevented && !g.isOpen(), "a click anywhere else is left alone"); }
{ const g = build();
  g.tap(g.cbtn); g.key("Enter");
  ok(g.isOpen(), "a key other than Escape leaves it open");
  g.key("Escape");
  ok(!g.isOpen() && g.store.get("artifacts.comments.open:abc") === "0", "Escape closes it, and that is remembered");
  g.extra[".dlg:target,details.menu[open]"] = el("div");
  g.tap(g.cbtn); g.key("Escape");
  ok(g.isOpen(), "Escape with a dialog or menu open closes that first, not the panel"); }
{ const g = build({ stored: "1" });
  ok(g.isOpen() && g.cbtn.attrs["aria-expanded"] === "true", "a viewer who left it open finds it open"); }
{ const g = build({ stored: "0" });
  ok(!g.isOpen(), "and one who closed it finds it closed"); }
// Remembered per page, not across pages (Jeremy, 19 Oct 2026): open on one
// page is not open on the next.
{ const g = build({ page: "xyz", seed: [["artifacts.comments.open:abc", "1"]] });
  ok(!g.isOpen(), "a page left open does not open the next page's panel");
  g.tap(g.cbtn);
  ok(g.store.get("artifacts.comments.open:xyz") === "1" && g.store.get("artifacts.comments.open:abc") === "1",
     "each page keeps its own memory");
  g.tap(g.cbtn);
  ok(g.store.get("artifacts.comments.open:xyz") === "0" && g.store.get("artifacts.comments.open:abc") === "1",
     "and closing one leaves the other's alone"); }
{ const g = build({ stored: "0", hash: "#comments" });
  ok(g.isOpen(), "a link to #comments opens it, whatever was remembered");
  ok(g.store.get("artifacts.comments.open:abc") === "0", "without overwriting what the viewer chose");
  ok(g.history.entries === 0 && g.history.replaced.length === 1 && g.history.replaced[0] === "/a/abc",
     "and the fragment is dropped in place, so a reload obeys the memory rather than the old link"); }
{ let threw = false, g;
  try { g = build({ storage: false }); g.tap(g.cbtn); } catch (e) { threw = true; }
  ok(!threw && g.isOpen(), "storage that throws costs only the memory: the panel still opens"); }
{ let threw = false;
  try { build({ noPanel: true }); } catch (e) { threw = true; }
  ok(!threw, "a page with no panel is left alone"); }
{ const g = build();
  const link = (pathname, search) => { const a = el("a"); a.pathname = pathname; a.search = search;
    a.closest = (sel) => (sel === 'a[href$="#comments"]' ? a : null); return a; };
  let ev = g.tap(link("/a/abc", ""));
  ok(ev.prevented && g.isOpen(), "a #comments link to this very page opens the panel without navigating");
  g.key("Escape");
  ev = g.tap(link("/a/abc", "?comments=all"));
  ok(!ev.prevented && !g.isOpen(), "one to another scope still navigates");
  ev = g.tap(link("/a/other", ""));
  ok(!ev.prevented, "and so does one to another page"); }
{ const g = build();
  g.send(SEL); g.tap(g.cbtn);
  ok(g.bar.hidden === true, "toggling the panel hides a selection bar whose position the reflow just made wrong"); }

// OPEN / RESOLVED tabs: a press picks the list by `data-ctab` on the panel and
// says so with aria-selected; the panel's open state and memory are untouched.
{ const g = build();
  const [open, done] = g.tabBtns;
  let ev = g.tap(done);
  ok(ev.prevented && g.panel.getAttribute("data-ctab") === "resolved", "the Resolved tab shows the resolved list");
  ok(done.attrs["aria-selected"] === "true" && open.attrs["aria-selected"] === "false", "and is the selected tab");
  ok(!g.isOpen() && g.store.size === 0, "without opening, closing or remembering the panel");
  g.tap(open);
  ok(g.panel.getAttribute("data-ctab") === "open" && open.attrs["aria-selected"] === "true", "the Open tab brings the open list back");
  const bad = el("button"); bad.className = "ctab"; bad.attrs["data-ctab"] = "evil";
  g.tap(bad);
  ok(g.panel.getAttribute("data-ctab") === "open", "a tab name that is not one of the two is ignored"); }
// A new comment lands in the open list, so the panel goes back to it.
{ const g = build({ fetchImpl: () => Promise.resolve(OKRESP([])) });
  const f = g.form; f.className = "cnew"; f.action = "/c/abc"; f.querySelectorAll = () => [];
  const list = el("ul"); g.extra["#comments .cpane-open > ul.threads"] = list;
  const cn = { textContent: "0" }, tn = { textContent: "2" };
  g.extra["#ctab-open .tn"] = tn; g.extra[".cbtn .cn"] = cn;
  g.tap(g.tabBtns[1]);
  g.submit(f); await tick(); await tick(); await tick();
  ok(g.panel.getAttribute("data-ctab") === "open", "posting a comment shows the Open tab");
  ok(tn.textContent === "3", "and the Open tab's count goes up"); }
// Pointing at a thread lights its spot whichever tab it is in.
{ const g = build({ marks: JSON.stringify([{ id: "e1", kind: "comment", quote: "", anchor: { kind: "element", id: "plans" } }]),
                    threads: [threadLi("e1")] });
  g.send({ type: "artifact-marks-placed", placed: ["e1"], unplaced: [], rects: [{ id: "e1", top: 100, left: 10, width: 300, height: 50 }] });
  g.tap(g.tabBtns[1]);
  g.panel.on.mouseover({ target: g.tlist.children[0] });
  const o = g.docBody.children.find((c) => c.className === "pinout");
  ok(o && o.hidden === false, "a thread's spot still lights up with the Resolved tab showing"); }

// HOVER CARDS. The page says only WHICH mark the pointer rests on and WHERE;
// the card is the panel's own (already escaped) thread markup, cloned, drawn
// by the gallery beside the text. Nothing the page sends is ever rendered.
function threadLi(id, { note = "Move this up", kind = null, held = false, reply = true } = {}) {
  const li = el("li"); li.className = "thread"; li.setAttribute("data-mark-id", id);
  if (kind) { const bq = el("blockquote"); bq.className = "cquote";
    const sp = el("span"); sp.className = "ckind ckind-" + kind; sp.textContent = kind === "remove" ? "Remove this" : "Looks good";
    bq.appendChild(sp); li.appendChild(bq); }
  const ul = el("ul"); ul.className = "bubbles";
  const cm = el("li"); cm.className = "cmt human";
  if (held) { const h = el("span"); h.className = "htag"; h.textContent = "held"; cm.appendChild(h); }
  const p = el("p"); p.textContent = note; cm.appendChild(p); ul.appendChild(cm); li.appendChild(ul);
  if (reply) { const f = el("form"); f.className = "creply"; f.action = "/c/abc";
    const i = el("input"); i.name = "body"; f.appendChild(i); li.appendChild(f); }
  return li;
}
const HMARKS = JSON.stringify([
  { id: "m1", kind: "remove", quote: "the plan", anchor: SEL.anchor },
  { id: "m2", kind: "comment", quote: "the rest", anchor: SEL.anchor },
  { id: "h1", kind: "", quote: "held one", anchor: SEL.anchor, held: true },
  { id: "m9", kind: "comment", quote: "no thread here", anchor: SEL.anchor }]);
const hthreads = () => [threadLi("m1", { kind: "remove", note: "" }), threadLi("m2", { note: "Say more" }),
                        threadLi("h1", { held: true, note: "Parked for later" })];
const HOV = (id, rect = { top: 30, left: 10, width: 60, height: 18 }) => ({ type: "artifact-mark-hover", id, rect });
const LEAVE = { type: "artifact-mark-leave" };
const btnOf = (c, label) => c.querySelectorAll("button").find((b) => b.textContent === label);
{ const g = build({ marks: HMARKS, threads: hthreads() });
  g.send(HOV("m2")); g.clk.advance(149);
  ok(g.card().length === 0, "no card before the pointer has rested 150 ms");
  g.clk.advance(1);
  const [c] = g.card();
  ok(c && g.card().length === 1, "a card after the rest");
  ok(c.getAttribute("data-mark-id") === "m2" && text(c).includes("Say more"), "showing that mark's note, from the panel's markup");
  ok(c.querySelector(".ckind") && c.querySelector(".ckind").textContent === "Comment", "labelled with its kind");
  ok(btnOf(c, "Reply") && btnOf(c, "Open"), "with Reply and Open");
  ok(c.style.left === "30px" && c.style.top === "154px", "beside the text: the frame's position plus the rect, just under it");
  ok(g.tlist.children[1].getAttribute("data-mark-id") === "m2" && text(g.tlist.children[1]).includes("Say more"),
     "the panel keeps its own thread: the card is a copy");
  ok(!JSON.stringify(g.framePosts).includes("Say more"), "and no note is ever posted to the frame");
  g.send(LEAVE); g.clk.advance(249);
  ok(g.card().length === 1, "leaving keeps it through the grace period");
  g.clk.advance(1);
  ok(g.card().length === 0, "and closes it after 250 ms"); }
{ const g = build({ marks: HMARKS, threads: hthreads() });
  g.send(HOV("m2")); g.clk.advance(150); g.send(LEAVE); g.clk.advance(100);
  const [c] = g.card(); c.on.pointerenter({}); g.clk.advance(1000);
  ok(g.card().length === 1, "moving into the card inside the grace period keeps it");
  c.on.pointerleave({}); g.clk.advance(250);
  ok(g.card().length === 0, "and leaving the card closes it after the grace period"); }
{ const g = build({ marks: HMARKS, threads: hthreads() });
  g.send(HOV("m2")); g.clk.advance(150);
  const [c] = g.card();
  btnOf(c, "Reply").on.click({});
  const mine = c.querySelector("input[name=body]"), panelBox = g.tlist.children[1].querySelector("input[name=body]");
  ok(mine && mine.focused === true && !panelBox.focused, "Reply puts the cursor in the card's own reply box");
  mine.focused = true; g.doc.activeElement = mine;
  g.send(LEAVE); g.clk.advance(1000);
  ok(g.card().length === 1, "a card being typed in does not close when the pointer wanders off");
  btnOf(c, "Open").on.click({});
  ok(g.isOpen() && g.card().length === 0, "Open opens the panel and puts the card away");
  ok(g.tlist.children[1].scrolled === true, "scrolled to that thread");
  ok(g.location.hash === "" && g.history.entries === 0, "without navigating"); }
{ const g = build({ marks: HMARKS, threads: hthreads() });
  g.tap(g.cbtn); g.send(HOV("m1")); g.clk.advance(150);
  ok(g.card().length === 1 && g.card()[0].querySelector(".ckind").textContent === "Remove this", "a remove mark says so");
  g.key("Escape");
  ok(g.card().length === 0 && g.isOpen(), "Escape closes the card first, and leaves the panel open");
  g.send(HOV("m1")); g.clk.advance(150); g.scroll();
  ok(g.card().length === 0, "scrolling closes it");
  g.send(HOV("m1")); g.clk.advance(150); g.tap(g.cbtn);
  ok(g.card().length === 0, "so does opening or closing the panel, which moves the page under it"); }
{ const g = build({ marks: HMARKS, threads: hthreads() });
  g.send(HOV("h1")); g.clk.advance(150);
  ok(g.card().length === 1 && g.card()[0].querySelector(".htag").textContent === "held", "a held mark's card says held");
  g.send(HOV("m2")); 
  ok(g.card().length === 0, "a different mark puts the old card away at once");
  g.clk.advance(150);
  ok(g.card().length === 1 && g.card()[0].getAttribute("data-mark-id") === "m2", "and shows its own after the rest"); }
{ const g = build({ marks: HMARKS, threads: hthreads() });
  g.send(HOV("m2"), { origin: "https://evil.example" }); g.send(HOV("m2"), { source: {} });
  g.send(HOV("invented")); g.send(HOV(7)); g.send({ type: "artifact-mark-hover", id: "m2" });
  for (const rect of [{ top: "x", left: 1, width: 1, height: 1 }, { top: NaN, left: 1, width: 1, height: 1 },
                      { top: 1, left: Infinity, width: 1, height: 1 }, { top: 1, left: 1 }, null, "1,2,3,4"]) g.send(HOV("m2", rect));
  g.clk.advance(1000);
  ok(g.card().length === 0, "another origin or window, an unknown id or a malformed rect shows nothing");
  let threw = false;
  try { g.send(HOV("m9")); g.clk.advance(1000); g.send(null); g.send("x"); g.send(LEAVE); g.clk.advance(1000); } catch (e) { threw = true; }
  ok(!threw && g.card().length === 0, "a known mark with no thread in the panel shows nothing, and nothing throws"); }
{ const inView = (c) => { const l = parseFloat(c.style.left), t = parseFloat(c.style.top); return l >= 8 && l <= 992 && t >= 8 && t <= 692; };
  const g = build({ marks: HMARKS, threads: hthreads() });
  g.send(HOV("m2", { top: 1e9, left: 1e9, width: 1e9, height: 1e9 })); g.clk.advance(150);
  ok(g.card().length === 1 && inView(g.card()[0]), "a huge rect is clamped: the card stays in the window");
  g.send(LEAVE); g.clk.advance(250);
  g.send(HOV("m2", { top: -1e6, left: -1e6, width: -5, height: -5 })); g.clk.advance(150);
  ok(g.card().length === 1 && inView(g.card()[0]), "so is a negative one"); }
{ const g = build({ marks: HMARKS, threads: hthreads() });
  for (let i = 0; i < 40; i++) { g.send(HOV(i % 2 ? "m1" : "m2")); g.clk.advance(100); }
  ok(g.card().length === 0, "a flood that never rests never shows a card");
  for (let i = 0; i < 1000; i++) g.send(HOV("m1"));
  g.clk.advance(150);
  ok(g.card().length === 1, "and a flood of one mark shows exactly one"); }
{ const g = build({ marks: HMARKS, threads: hthreads(), fetchImpl: () => Promise.resolve(OKRESP([])) });
  g.send(HOV("m2")); g.clk.advance(150);
  const [c] = g.card(), fm = c.querySelector("form.creply");
  fm.querySelectorAll = () => []; fm.submit = () => { fm.submitted = true; };
  c.querySelector("input[name=body]").value = "Sure";
  g.submit(fm); await tick(); await tick(); await tick();
  ok(g.tlist.children.length === 3 && g.tlist.children[1].html === "<li>t</li>", "a reply sent from the card replaces the panel's thread, not a new one");
  ok(g.card().length === 0 && !fm.submitted, "and the card is put away"); }

// Fix wave: the card reads Reply . Open, its reply box hidden until Reply; a
// reply being written survives hover, leave, scroll and the panel; one Escape
// is the card's alone; a failed send falls back through the panel; the card
// never covers the panel; a tap outside closes it; full screen shows it.
{ const g = build({ marks: HMARKS, threads: hthreads() });
  g.send(HOV("m2")); g.clk.advance(150);
  const [c] = g.card(), fm = c.querySelector("form.creply");
  ok(fm && fm.hidden === true, "the card's reply box is hidden until Reply is pressed");
  ok(c.querySelector(".hcact").children.map((b) => b.textContent).join(" · ") === "Reply · Open", "the card reads Reply · Open");
  btnOf(c, "Reply").on.click({});
  const box = c.querySelector("input[name=body]");
  ok(fm.hidden === false && box.focused === true, "Reply reveals the box and focuses it");
  g.doc.activeElement = box;
  g.send(HOV("m1")); g.clk.advance(1000);
  ok(g.card().length === 1 && g.card()[0] === c, "a reply being typed survives hovering another mark");
  g.send(LEAVE); g.clk.advance(1000); g.scroll();
  ok(g.card().length === 1, "and a leave, and a scroll (a phone's keyboard scrolls the window)");
  g.tap(g.cbtn);
  ok(g.card().length === 1 && g.isOpen(), "and the panel opening");
  g.doc.activeElement = null; box.value = "half a thought";
  g.send(HOV("m1")); g.clk.advance(1000); g.scroll();
  ok(g.card().length === 1, "text in the box keeps it even once the cursor has gone");
  g.press(box);
  ok(g.card().length === 1, "a press inside the card keeps it");
  const ev = g.key("Escape");
  ok(g.card().length === 0 && ev.stopped === true, "Escape closes it, and goes no further");
  ok(g.isOpen(), "so the same Escape does not also close the panel"); }
{ const g = build({ marks: HMARKS, threads: hthreads() });
  g.send(HOV("m2")); g.clk.advance(150);
  g.press(el("div"));
  ok(g.card().length === 0, "a press outside the card closes it (a phone's tap)");
  g.send(HOV("m2")); g.clk.advance(150);
  const [c] = g.card(), box = c.querySelector("input[name=body]");
  btnOf(c, "Reply").on.click({}); box.value = "typed"; g.press(el("div"));
  ok(g.card().length === 0, "even with a reply typed: a tap outside is a choice to leave"); }
{ let rejectIt; const g = build({ marks: HMARKS, threads: hthreads(), fetchImpl: () => new Promise((_, rej) => { rejectIt = rej; }) });
  g.send(HOV("m2")); g.clk.advance(150);
  const [c] = g.card(), fm = c.querySelector("form.creply");
  fm.submitted = 0; fm.submit = () => { fm.submitted++; };
  const pf = g.tlist.children[1].querySelector("form.creply"); pf.submitted = 0; pf.submit = () => { pf.submitted++; };
  c.querySelector("input[name=body]").value = "Keep this";
  g.submit(fm); g.key("Escape");
  ok(g.card().length === 0, "the card can go while its reply is in flight");
  rejectIt(new Error("net")); await tick(); await tick(); await tick();
  ok(pf.submitted === 1 && pf.querySelector("input[name=body]").value === "Keep this" && fm.submitted === 0,
     "a failed send from a card that is gone falls back through the panel's copy, text and all"); }
{ const g = build({ marks: HMARKS, threads: hthreads() });
  g.send(HOV("m2", { top: 30, left: 1e9, width: 10, height: 18 })); g.clk.advance(150);
  ok(parseFloat(g.card()[0].style.left) <= 812, "the card stops at the frame's right edge, clear of the docked panel"); }
{ const g = build({ marks: HMARKS, threads: hthreads() });
  const fs = el("div"); g.doc.fullscreenElement = fs; g.fsChange();
  g.send(HOV("m2")); g.clk.advance(150);
  ok(g.card().length === 0 && fs.children.filter((c) => c.className === "hovercard").length === 1,
     "in full screen the card goes inside the full-screen element, where it can be seen");
  g.fsChange();
  ok(fs.children.filter((c) => c.className === "hovercard").length === 0, "leaving or entering full screen puts it away");
  ok(g.bar.parentNode === fs, "the selection bar moves into the full-screen element too");
  g.send({ ...SEL, rect: { ...SEL.rect, top: 900, vt: 30, vl: 10 } });
  ok(g.bar.hidden === false && g.bar.style.position === "fixed" && g.bar.style.top === "154px" && g.bar.style.left === "30px",
     "and sits under the selection as the frame shows it, since the frame scrolls itself there");
  g.doc.fullscreenElement = null; g.fsChange();
  ok(g.bar.parentNode === g.docBody && g.bar.hidden === true, "back in the page when full screen ends");
  g.send(SEL);
  ok(g.bar.style.position === "" && g.bar.style.top === "204px", "placed in page coordinates again"); }

// ===== ELEMENT PINS (pinned comments, slice 2) ===============================
// The page bar's Pin button, the C key, an outline over the frame while the
// pointer moves, a pick that fills the composer, and numbered badges drawn from
// the rectangles the shim reports. Everything the page sends goes in through
// textContent and numbers; nothing from it is markup.
const PIN = { type: "artifact-pin", quote: "Plan Price Pro $12 a month",
              anchor: { kind: "element", id: "plans", tag: "table", heading: "Costs", path: "body>table:nth-of-type(1)" },
              rect: { top: 60, left: 12, width: 300, height: 80, vt: 60, vl: 12 } };
const modeMsgs = (g) => g.framePosts.filter(([m]) => m && m.type === "artifact-pin-mode");
const outlineOf = (g) => g.docBody.children.find((c) => c.className === "pinout");
const badgesOf = (g) => g.docBody.children.filter((c) => c.className === "pinbadge");

{ const g = build();
  ok(g.pinbtn.hidden === false, "the Pin button is revealed once the composer is there");
  g.pinbtn.on.click({});
  ok(modeMsgs(g).length === 1 && modeMsgs(g)[0][0].on === true && modeMsgs(g)[0][1] === "https://abc.tenfourty.com",
     "pressing it turns pin mode on in the frame, at the frame's origin");
  ok(g.pinbtn.attrs["aria-pressed"] === "true" && g.pinbtn.classList.contains("on"), "and shows it is on");
  g.pinbtn.on.click({});
  ok(modeMsgs(g)[1][0].on === false && g.pinbtn.attrs["aria-pressed"] === "false", "pressing it again leaves the mode");
  const n = build({ noForm: true });
  ok(n.pinbtn.hidden === true, "with no composer on the page there is nothing to pin to: the button stays hidden");
  ok(build({ noPinBtn: true }) !== null, "a page bar without the button does not break the script"); }

// COMMENT ON PAGE
{ const g = build();
  g.send(SEL); g.click("comment");
  ok(g.form.querySelector(".cquote") && g.form.querySelector("input[name=anchor]"), "a selection filled the composer");
  g.panel.setAttribute("data-ctab", "resolved");
  let focused = 0; g.body.focus = () => focused++;
  const ev = { prevented: false, preventDefault() { ev.prevented = true; } };
  g.pagebtn.on.click(ev);
  ok(ev.prevented, "the link's jump is replaced by the script");
  ok(g.isOpen() && g.panel.getAttribute("data-ctab") === "open", "the panel opens on the Open tab");
  ok(!g.form.querySelector(".cquote") && !g.form.querySelector("input[name=anchor]") && !g.form.querySelector("input[name=quote]"),
     "the composer is the whole page again: no quote, no anchor");
  ok(g.body.placeholder === "Comment on this page" && focused === 1, "with its own placeholder, and focus in the field");
  ok(g.bar.hidden === true, "and the selection bar goes");
  g.key("c", { target: { nodeName: "BODY" } });
  g.pagebtn.on.click(ev);
  ok(modeMsgs(g).at(-1)[0].on === false, "it also leaves pin mode"); }

// the C key
{ const g = build();
  g.key("c", { target: { nodeName: "BODY" } });
  ok(modeMsgs(g).length === 1 && modeMsgs(g)[0][0].on === true, "C turns pin mode on");
  g.key("C", { target: { nodeName: "BODY" } });
  ok(modeMsgs(g).length === 2 && modeMsgs(g)[1][0].on === false, "and off");
  for (const target of [{ nodeName: "INPUT" }, { nodeName: "TEXTAREA" }, { nodeName: "SELECT" }, { nodeName: "DIV", isContentEditable: true }])
    g.key("c", { target });
  g.key("c", { target: { nodeName: "BODY" }, ctrlKey: true });
  g.key("c", { target: { nodeName: "BODY" }, metaKey: true });
  ok(modeMsgs(g).length === 2, "never while typing in a field, nor with a modifier");
  g.key("c", { target: { nodeName: "BODY" } });
  const esc = g.key("Escape");
  ok(modeMsgs(g).length === 4 && modeMsgs(g)[3][0].on === false && esc.stopped, "Escape leaves the mode, and is not also the panel's");
  g.key("c", { target: { nodeName: "BODY" } });
  g.panel.classList.add("cpanel-open"); g.docBody.classList.add("cpanel-open");
  g.key("Escape");
  ok(g.isOpen(), "with the mode on, Escape leaves the mode first and the panel stays open");
  g.key("Escape");
  ok(!g.isOpen(), "and the next one closes the panel as before"); }

// the frame's own keys
{ const g = build();
  g.send({ type: "artifact-pin-toggle" });
  ok(modeMsgs(g).length === 1 && modeMsgs(g)[0][0].on === true, "C pressed inside the frame toggles the mode");
  g.send({ type: "artifact-pin-exit" });
  ok(modeMsgs(g)[1][0].on === false, "and Escape inside the frame leaves it");
  g.send({ type: "artifact-pin-toggle" }, { origin: "https://evil.example" });
  g.send({ type: "artifact-pin-toggle" }, { source: {} });
  ok(modeMsgs(g).length === 2, "only the frame, at its own origin, is heard"); }

// the outline
{ const g = build();
  g.send({ type: "artifact-pin-hover", rect: { top: 20, left: 8, width: 100, height: 30 }, tag: "p" });
  ok(!outlineOf(g) || outlineOf(g).hidden, "no outline when pin mode is off");
  g.pinbtn.on.click({});
  g.send({ type: "artifact-pin-hover", rect: { top: 20, left: 8, width: 100, height: 30 }, tag: "p" });
  const o = outlineOf(g);
  ok(o && o.hidden === false && o.style.top === "170px" && o.style.left === "28px" && o.style.width === "100px" && o.style.height === "30px",
     "an outline over the frame, in page coordinates: " + (o && JSON.stringify(o.style)));
  g.send({ type: "artifact-pin-hover", rect: null, tag: "" });
  ok(outlineOf(g).hidden === true, "no candidate hides it");
  g.send({ type: "artifact-pin-hover", rect: { top: "20", left: 8, width: 100, height: 30 }, tag: "p" });
  ok(outlineOf(g).hidden === true, "a rectangle that is not four numbers is ignored");
  g.send({ type: "artifact-pin-hover", rect: { top: 1, left: 1, width: 5, height: 5 }, tag: "p" });
  g.pinbtn.on.click({});
  ok(outlineOf(g).hidden === true, "leaving the mode hides it");
  g.pinbtn.on.click({});
  g.send({ type: "artifact-mark-hover", id: "m1", rect: { top: 30, left: 10, width: 60, height: 18 } });
  ok(g.card().length === 0, "no hover cards over marks while picking"); }

// a pick fills the composer
{ const g = build({ threads: [] });
  g.send(PIN);
  ok(!g.form.children.find((c) => c.name === "anchor"), "a pick that was not asked for is ignored");
  g.pinbtn.on.click({});
  g.send(PIN);
  const field = (n) => g.form.children.find((c) => c.name === n);
  ok(field("quote") && field("quote").value === "Plan Price Pro $12 a month", "the quote goes into the form");
  ok(field("anchor") && JSON.parse(field("anchor").value).id === "plans" && JSON.parse(field("anchor").value).kind === "element", "and the anchor");
  ok(field("kind") && field("kind").value === "comment", "as a plain comment");
  const q = g.form.children.find((c) => c.className === "cquote");
  ok(q && q.textContent === "Plan Price Pro $12 a month", "the spot is quoted above the box, as text");
  ok(g.isOpen() && g.body.focused === true && /pinned/i.test(g.body.placeholder), "the panel opens with the cursor in the box");
  ok(modeMsgs(g).slice(-1)[0][0].on === false, "and pin mode is left, so the page's own buttons work again");
  ok(g.bar.hidden === true, "the selection bar is not also offered"); }
{ const g = build();
  g.pinbtn.on.click({});
  g.send({ ...PIN, quote: "", anchor: { kind: "element", tag: "img", ordinal: 2, heading: "Costs" } });
  const q = g.form.children.find((c) => c.className === "cquote");
  ok(q && q.textContent === "img", "an element with no words is named by its tag");
  const h = build();
  for (const bad of [{ ...PIN, anchor: { kind: "range", path: "p" } }, { ...PIN, anchor: null }, { ...PIN, quote: 5 },
                     { ...PIN, rect: null }, { ...PIN, anchor: { kind: "element", pad: "x".repeat(5000) } }]) {
    h.pinbtn.on.click({}); h.send(bad);
    ok(!h.form.children.find((c) => c.name === "anchor"), "a malformed pick fills nothing: " + JSON.stringify(bad).slice(0, 50));
    if (modeMsgs(h).slice(-1)[0][0].on) h.pinbtn.on.click({});
  }
  const long = build(); long.pinbtn.on.click({});
  long.send({ ...PIN, quote: "w".repeat(900) });
  ok(long.form.children.find((c) => c.name === "quote").value.length === 200, "a long quote is cut at 200"); }

// Selecting text always offers the bar, even while picking (Jeremy, 5 Nov 2026:
// pin mode held the bar back with nothing on screen saying why, and he was stuck).
// The selection says what he wants: pin mode is left, then the bar shows.
{ const g = build();
  g.pinbtn.on.click({});
  g.send(SEL);
  ok(g.bar.hidden === false, "text selected in the frame during pin mode still offers the action bar");
  ok(modeMsgs(g).slice(-1)[0][0].on === false && g.pinbtn.attrs["aria-pressed"] === "false",
     "and pin mode is left, in the frame and on the button");
  const h = build();
  h.pinbtn.on.click({});
  h.send({ type: "artifact-selection", quote: "" });
  ok(modeMsgs(h).slice(-1)[0][0].on === true, "an empty selection (a click to pick) leaves pin mode on"); }

// badges
const EMARKS = JSON.stringify([
  { id: "t1", kind: "comment", quote: "some words", anchor: SEL.anchor },
  { id: "e1", kind: "comment", quote: "", anchor: { kind: "element", id: "plans" } },
  { id: "e2", kind: "comment", quote: "", anchor: { kind: "element", id: "chart" } }]);
const placed = (rects, extra = {}) => ({ type: "artifact-marks-placed", placed: ["t1", "e1", "e2"], unplaced: [], rects, ...extra });
{ const g = build({ marks: EMARKS, threads: [threadLi("e1"), threadLi("e2")] });
  g.send(placed([{ id: "e1", top: 100, left: 10, width: 300, height: 50 }, { id: "e2", top: 400, left: 20, width: 50, height: 50 }]));
  const bs = badgesOf(g);
  ok(bs.length === 2 && bs[0].textContent === "1" && bs[1].textContent === "2", "a numbered badge for each element pin, in order");
  ok(bs[0].style.top === "250px" && bs[0].style.left === "30px", "at the element's corner, in page coordinates: " + JSON.stringify(bs[0].style));
  const li = g.tlist.children[0];
  ok(li.querySelector(".cpinno") && li.querySelector(".cpinno").textContent === "1", "and the same number in its thread");
  g.send(placed([{ id: "e2", top: 410, left: 20, width: 50, height: 50 }]));
  ok(badgesOf(g).length === 1 && badgesOf(g)[0].textContent === "2", "the next report replaces them (numbers stay with their pins)");
  ok(g.tlist.children[1].querySelector(".cpinno").textContent === "2", "e2 keeps the number 2 in its thread");
  g.send(placed([{ id: "t1", top: 1, left: 1, width: 1, height: 1 }, { id: "zz", top: 1, left: 1, width: 1, height: 1 },
                 { id: "e1", top: "x", left: 1, width: 1, height: 1 }, null, { id: "e1", top: 1, left: 1, width: NaN, height: 1 }]));
  ok(badgesOf(g).length === 0, "rects for text marks, unknown ids or non-numbers draw nothing");
  g.send(placed(Array.from({ length: 500 }, () => ({ id: "e1", top: 1, left: 1, width: 1, height: 1 }))));
  ok(badgesOf(g).length <= 200, "capped");
  g.send({ type: "artifact-pin-rects", rects: [{ id: "e1", top: 130, left: 10, width: 300, height: 50 }] });
  ok(badgesOf(g).length === 1 && badgesOf(g)[0].style.top === "280px", "a resize report moves them");
  badgesOf(g)[0].on.click({});
  ok(g.isOpen() && g.tlist.children[0].scrolled === true, "pressing a badge opens the panel at its thread");
  g.send(placed([{ id: "e1", top: 100, left: 10, width: 300, height: 50 }], { placed: ["e1"] }), { origin: "https://evil.example" });
  g.send({ type: "artifact-marks-placed", placed: [], unplaced: [] });
  ok(badgesOf(g).length === 1 || badgesOf(g).length === 0, "a report with no rects is not an error"); }
{ const g = build({ marks: EMARKS, threads: [threadLi("e1")] });
  g.send(placed([{ id: "e1", top: 100, left: 10, width: 300, height: 50 }]));
  const t = g.tlist.children[0];
  g.panel.on.mouseover({ target: t });
  const o = outlineOf(g);
  ok(o && o.hidden === false && o.style.top === "250px" && o.style.width === "300px", "hovering a thread outlines its spot: " + (o && JSON.stringify(o.style)));
  g.panel.on.mouseout({ target: t });
  ok(outlineOf(g).hidden === true, "leaving it hides the outline");
  g.panel.on.focusin({ target: t });
  ok(outlineOf(g).hidden === false, "focus does the same");
}
{ const g = build({ marks: EMARKS, threads: [threadLi("e1")] });
  g.doc.fullscreenElement = g.docBody;
  g.send(placed([{ id: "e1", top: 100, left: 10, width: 300, height: 50 }]));
  ok(badgesOf(g).length === 0, "no badges in full screen, where page offsets would point at nothing"); }

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
