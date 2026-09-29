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

function el(tag) {
  return { tag, style: {}, hidden: false, children: [], attrs: {}, on: {}, value: "", textContent: "", dataset: {},
           appendChild(c) { this.children.push(c); c.parentNode = this; },
           insertBefore(c) { this.children.unshift(c); c.parentNode = this; },
           addEventListener(t, f) { this.on[t] = f; }, focus() { this.focused = true; },
           closest(sel) {
             const tag = sel.replace(/^\W+/, "");
             let node = this;
             while (node) { if (node.tag === tag) return node; node = node.parentNode; }
             return null;
           } };
}
function build({ marks = "[]", fetchImpl = null, noForm = false } = {}) {
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
  const byId = { "artifact-frame": iframe, "artifact-marks": marksEl, "marks-unplaced": unplacedEl };
  const docOn = {}, extra = {};
  const doc = { getElementById: (id) => byId[id] || null,
                createElement: (t) => { const n = el(t); if (t === "template") Object.defineProperty(n, "innerHTML", { set(h) { n.content = { firstElementChild: { html: h } }; } }); return n; },
                body: docBody,
                addEventListener: (t, f) => ((docOn[t] ||= []).push(f)),
                querySelector: (q) => (q === "form.cnew" ? (noForm ? null : form) : extra[q] || null) };
  const listeners = {};
  const win = { scrollX: 0, scrollY: 50, addEventListener: (t, f) => ((listeners[t] ||= []).push(f)) };
  const location = { hash: "" };
  const FD = function (f) { this.form = f; };
  new Function("document", "window", "location", "URL", "fetch", "FormData", src)(doc, win, location, URL, fetchImpl || undefined, FD);
  const bar = docBody.children[0];
  const buttons = {};
  if (bar) bar.children.forEach((b) => (buttons[b.dataset.kind] = b));
  const send = (data, { source = frameWin, origin = "https://abc.tenfourty.com" } = {}) =>
    (listeners.message || []).forEach((fn) => fn({ source, origin, data }));
  const click = (kind) => bar.on.click({ target: buttons[kind] });
  const reload = () => (frameOn.load || []).forEach((fn) => fn({}));
  const submit = (fm, e = {}) => { const ev = { target: fm, prevented: false, preventDefault() { ev.prevented = true; }, ...e };
    (docOn.submit || []).forEach((fn) => fn(ev)); return ev; };
  return { bar, buttons, form, body, location, send, click, framePosts, unplacedEl, reload, submit, extra, doc };
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
  ok(g.location.hash === "comments" && g.body.focused, "the drawer opens with the cursor in the box");
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
  g.extra["#comments .cdrawer-card > ul.threads"] = list;
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
// The count on the bar goes up when a new root comes back, not for a reply.
{ const g = build({ fetchImpl: () => Promise.resolve(OKRESP([])) });
  const f = g.form; f.className = "cnew"; f.action = "/c/abc";
  f.querySelectorAll = () => [];
  const cn = { textContent: "2" }; g.extra[".cbtn .cn"] = cn;
  const list = el("ul"); g.extra["#comments .cdrawer-card > ul.threads"] = list;
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

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
