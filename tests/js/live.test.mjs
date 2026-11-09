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

function build({ hidden = false, search = "", version = "3", comments = true } = {}) {
  const calls = [], vcalls = [], timers = [], events = [], docOn = {}, body = { children: [],
    appendChild(c) { c.parentNode = body; body.children.push(c); },
    removeChild(c) { body.children = body.children.filter((x) => x !== c); c.parentNode = null; } };
  let resolveNext, rejectNext, vResolve, vReject;
  const reloads = { n: 0 };
  const P = el("aside", "cpanel"); P.id = "comments";
  const head = el("div", "chead"), form = el("form", "cnew"); form.id = "cwhole";
  P.children.push(head, form); head.parentNode = form.parentNode = P;
  P.attrs["data-ctab"] = "open";
  const mk = el("script"); mk.id = "artifact-marks";
  const sheet = el("div"); sheet.attrs["data-csig"] = "s1"; sheet.attrs["data-version"] = version;
  const doc = {
    get hidden() { return hidden; }, activeElement: null,
    getElementById: (id) => (id === "comments" ? (comments ? P : null) : id === "artifact-marks" ? mk : id === "artifact-sheet" ? sheet : null),
    body, querySelector: () => null, querySelectorAll: () => [],
    createElement: (t) => {
      if (t !== "template") { const e = el(t); e.addEventListener = (n, fn) => { (e._on ||= {})[n] = fn; }; return e; }
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
    fetch(url) {
      if (url.indexOf("/current") >= 0) { vcalls.push(url);
        return new Promise((resolve, reject) => { vResolve = (p, okFlag = true) => resolve({ ok: okFlag, json: () => Promise.resolve(p) }); vReject = reject; }); }
      calls.push(url);
      return new Promise((resolve, reject) => {
        resolveNext = (p, okFlag = true) => resolve({ ok: okFlag, json: () => Promise.resolve(p) }); rejectNext = reject; }); },
    addEventListener() {},
  };
  const loc = { pathname: "/a/abc", search, reload() { reloads.n++; } };
  new Function("document", "window", "location", "setTimeout", "clearTimeout", "CustomEvent", "encodeURIComponent", "Date", "Math",
    "Array", "Object", "JSON", src)(
    doc, win, loc, (fn, ms) => { const t = { fn, ms }; timers.push(t); return t; }, () => {},
    class { constructor(n, o) { this.type = n; this.detail = o && o.detail; } }, encodeURIComponent, Date, Math, Array, Object, JSON);
  const ctimers = () => timers.filter((t) => t.fn.name !== "vwatch");
  return { calls, vcalls, reloads, body, get timers() { return ctimers(); }, events, P, mk, docOn,
    vanswer: async (p, okFlag = true) => { vResolve(p, okFlag); await tick(); await tick(); },
    vfail: async () => { vReject(new Error("net")); await tick(); await tick(); },
    vtimers: () => timers.filter((t) => t.fn.name === "vwatch"),
    vshow: () => body.children.find((c) => c.className === "newer"),
    vpress: async (label) => { const b = body.children.find((c) => c.className === "newer").children.find((c) => c.textContent === label); b._on.click(); await tick(); },
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

// 8. THE VERSION WATCH (runtime bug .99): moved here from gallery.js. A newer version is offered, never applied.
{
  const g = build({ version: "3" });
  ok(g.vcalls.length === 1 && g.vcalls[0] === "/a/abc/current?since=3&wait=25", "the version watch asks, holding, about the version in front of him");
  await g.vanswer({ n: 4 });
  const bar = g.vshow();
  ok(!!bar && bar.children[0].textContent.indexOf("newer version") >= 0, "a 200 with a newer version puts the offer on the page");
  ok(g.reloads.n === 0, "and nothing reloads by itself");
  await g.vpress("Show it");
  ok(g.reloads.n === 1, "Show it is what reloads");
}
{
  const g = build({ version: "3" });
  await g.vanswer({ n: 4 });
  await g.vpress("Not now");
  ok(!g.vshow(), "Not now takes the bar away");
  g.vtimers()[0].fn();
  await g.vanswer({ n: 4 });
  ok(!g.vshow(), "and the same version never asks twice");
  g.vtimers()[g.vtimers().length - 1].fn();
  await g.vanswer({ n: 5 });
  ok(!!g.vshow(), "but a newer one still speaks up");
}
// 9. a 503 backs off and does not ask again at once; the schedule doubles to a cap of 60 s
{
  const g = build({ version: "3" });
  await g.vanswer(null, false);
  ok(g.vcalls.length === 1, "a non-200 does not re-request immediately");
  ok(g.vtimers().length === 1 && g.vtimers()[0].ms === 5000, "it retries after 5 s");
  ok(!g.vshow(), "and says nothing to him");
  g.vtimers()[0].fn();
  ok(g.vcalls.length === 2, "the retry goes out");
  await g.vanswer(null, false);
  ok(g.vtimers()[1].ms === 10000, "a second failure waits 10 s");
  for (let i = 0; i < 6; i++) { g.vtimers()[g.vtimers().length - 1].fn(); await g.vanswer(null, false); }
  ok(g.vtimers()[g.vtimers().length - 1].ms === 60000, "capped at 60 s");
}
// 10. a network rejection backs off and retries; the watch never dies
{
  const g = build({ version: "3" });
  await g.vfail();
  ok(g.vcalls.length === 1 && g.vtimers().length === 1 && g.vtimers()[0].ms === 5000, "a rejected read backs off 5 s, not at once");
  g.vtimers()[0].fn();
  ok(g.vcalls.length === 2, "and retries");
  await g.vanswer({ n: 4 });
  ok(!!g.vshow(), "and the recovered watch still offers a new version");
}
// 11. a hidden tab pauses, and resumes when shown
{
  const g = build({ version: "3", hidden: true });
  ok(g.vcalls.length === 0, "a hidden tab asks nothing");
  await g.reveal();
  g.vtimers()[g.vtimers().length - 1].fn();
  ok(g.vcalls.length === 1, "and asks when shown");
}
// 12. it runs where the comments panel is off
{
  const g = build({ version: "3", comments: false });
  ok(g.vcalls.length === 1 && g.calls.length === 0, "the version watch runs without a comments panel, and the comments loop does not");
}

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
