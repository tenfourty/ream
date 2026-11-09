// Drive the REAL /live.js (the page's held read on its comments) against a
// minimal fake DOM: when it asks, when it does not, what it does with an answer.
// 9 Nov 2026: comments the agent added never reached an open page. The cases
// that matter are the renewal ones -- an answer, a quiet hold, an error, a
// hidden tab -- because a loop that quietly stops is the bug this replaces.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");

let fails = 0, passes = 0;
const ok = (c, m) => { c ? passes++ : (fails++, console.log("FAIL: " + m)); if (c) console.log("ok   " + m); };
const tick = () => new Promise((r) => setImmediate(r));

function el(tag, cls = "") {
  return { tag, className: cls, nodeType: 1, children: [], attrs: {}, parentNode: null, textContent: "", id: "",
    classList: { add() {}, remove() {} },
    setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    insertBefore(c, ref) { c.parentNode = this; const i = ref ? this.children.indexOf(ref) : -1;
      if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); },
    removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; },
    appendChild(c) { this.insertBefore(c, null); } };
}

function build({ hidden = false, search = "" } = {}) {
  const calls = [], timers = [], events = [], docOn = {};
  let resolveNext, rejectNext;
  const P = el("aside", "cpanel"); P.id = "comments";
  const head = el("div", "chead"), form = el("form", "cnew"); form.id = "cwhole";
  P.children.push(head, form); head.parentNode = form.parentNode = P;
  P.attrs["data-ctab"] = "open";
  const mk = el("script"); mk.id = "artifact-marks";
  const sheet = el("div"); sheet.attrs["data-csig"] = "s1";
  const doc = {
    get hidden() { return hidden; }, activeElement: null,
    getElementById: (id) => (id === "comments" ? P : id === "artifact-marks" ? mk : id === "artifact-sheet" ? sheet : null),
    querySelector: () => null, querySelectorAll: () => [],
    createElement: (t) => {
      if (t !== "template") return el(t);
      const tpl = el("template");
      Object.defineProperty(tpl, "innerHTML", { set(v) {
        const aside = el("aside"); const pane = el("div", "cpane"); pane.textContent = v; aside.children.push(pane);
        tpl.content = { firstElementChild: aside }; } });
      return tpl;
    },
    addEventListener: (t, fn) => (docOn[t] ||= []).push(fn),
    dispatchEvent: (e) => events.push(e),
  };
  const win = {
    fetch(url) { calls.push(url);
      return new Promise((resolve, reject) => {
        resolveNext = (p, okFlag = true) => resolve({ ok: okFlag, json: () => Promise.resolve(p) }); rejectNext = reject; }); },
    addEventListener() {},
  };
  const loc = { pathname: "/a/abc", search };
  new Function("document", "window", "location", "setTimeout", "clearTimeout", "CustomEvent", "encodeURIComponent", "Date", "Math",
    "Array", "Object", "JSON", src)(
    doc, win, loc, (fn, ms) => { const t = { fn, ms }; timers.push(t); return t; }, () => {},
    class { constructor(n, o) { this.type = n; this.detail = o && o.detail; } }, encodeURIComponent, Date, Math, Array, Object, JSON);
  return { calls, timers, events, P, mk, docOn,
    answer: async (p, okFlag = true) => { resolveNext(p, okFlag); await tick(); await tick(); },
    fail: async () => { rejectNext(new Error("net")); await tick(); await tick(); },
    reveal: async () => { hidden = false; (docOn.visibilitychange || []).forEach((f) => f()); await tick(); },
    hide: () => { hidden = true; } };
}

// 1. it asks, holding, about the signature the page was drawn from
{
  const g = build();
  ok(g.calls.length === 1 && g.calls[0] === "/a/abc/comments-live?since=s1&wait=25",
     "asks once on load, holding, about the signature it was drawn from");
}
// 2. a change: the panel is swapped, the marks go out as an event, the next ask uses the new signature
{
  const g = build();
  await g.answer({ sig: "s2", panel: "<aside>NEW</aside>", open: 2, marks: [{ id: "m1" }] });
  ok(g.P.children.some((c) => c.className === "cpane" && c.textContent.indexOf("NEW") >= 0), "the new lists are in the panel");
  ok(g.P.children.indexOf(g.P.children.find((c) => c.id === "cwhole")) === g.P.children.length - 1,
     "the new-comment form is kept, still last");
  ok(g.events.length === 1 && g.events[0].type === "ream-marks" && g.events[0].detail[0].id === "m1",
     "the marks are handed on for the frame");
  ok(g.mk.textContent === JSON.stringify([{ id: "m1" }]), "the marks block holds the new marks");
  g.timers[g.timers.length - 1].fn();
  ok(g.calls.length === 2 && g.calls[1] === "/a/abc/comments-live?since=s2&wait=25", "and it asks again about the new signature");
}
// 3. nothing new: renewed, after a pause only if it came straight back
{
  const g = build();
  await g.answer({ same: true, sig: "s1" });
  ok(g.events.length === 0, "an unchanged answer touches nothing");
  ok(g.timers.length === 1 && g.timers[0].ms === 5000, "a hold that came straight back is renewed after a pause, not at once");
}
// 4. an error is retried with a backoff, and the loop survives it
{
  const g = build();
  await g.fail();
  ok(g.timers.length === 1 && g.timers[0].ms === 5000, "a dropped read is retried after 5 s");
  g.timers[0].fn();
  ok(g.calls.length === 2, "and the second ask goes out");
  await g.answer(null, false);
  ok(g.timers.length === 2 && g.timers[1].ms === 10000, "a non-200 backs off further");
}
// 5. a hidden tab asks nothing and catches up the moment it is shown
{
  const g = build({ hidden: true });
  ok(g.calls.length === 0, "a tab nobody is looking at asks nothing");
  await g.reveal();
  g.timers[g.timers.length - 1].fn();
  ok(g.calls.length === 1, "and asks, with the signature it last drew, when shown (the server answers at once if it moved)");
}
// 6. the all-pages scope is kept
{
  const g = build({ search: "?comments=all" });
  ok(/&comments=all$/.test(g.calls[0]), "the scope he chose rides on the ask");
}

// 7. the card over a mark asks for the panel now (it settled or reopened a thread): answered at once, not held
{
  const g = build();
  const ev = { detail: { handled: false } };
  (g.docOn["ream-refresh"] || []).forEach((f) => f(ev));
  ok(ev.detail.handled === true, "the refresh request is marked handled, so the card does not reload the page");
  ok(g.calls.length === 2 && g.calls[1] === "/a/abc/comments-live?since=&wait=0", "it reads the panel now, without holding: " + g.calls[1]);
  await g.answer({ sig: "s9", panel: "<aside>SETTLED</aside>", open: 1, marks: [{ id: "m2" }] });
  ok(g.events.length === 1 && g.events[0].type === "ream-marks" && g.events[0].detail[0].id === "m2", "the panel is swapped and the marks go out as for any change");
}

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
