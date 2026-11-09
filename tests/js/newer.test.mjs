// Drive the REAL gallery script's "a newer version is ready" watch against a
// minimal fake DOM, and watch what it does.
//
// homelab-ij8.74. Jeremy chose (b) -- tell me and wait -- over refreshing
// under him. The things that matter here are all things it must NOT do:
// reload by itself, ask again about a version he has dismissed, or keep
// asking while nobody is looking. None of those can be read off the source
// text, which is why this file exists.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");

let fails = 0, passes = 0;
const ok = (c, m) => { c ? passes++ : (fails++, console.log("FAIL: " + m)); if (c) console.log("ok   " + m); };
const tick = () => new Promise((r) => setImmediate(r));

function build({ version = "3", hidden = false } = {}) {
  const calls = [];
  const timers = [];
  let resolveNext, rejectNext;
  const made = [];
  const body = { children: [], appendChild(c) { c.parentNode = body; body.children.push(c); },
                 removeChild(c) { body.children = body.children.filter((x) => x !== c); c.parentNode = null; } };
  const docListeners = {};
  const sheet = { getAttribute: (k) => (k === "data-version" ? version : null), style: {} };
  const iframe = { src: "https://abc.tenfourty.com/", style: {},
                   contentWindow: { postMessage() {} },
                   getAttribute: () => null };
  const doc = {
    get hidden() { return hidden; },
    body,
    getElementById: (id) => (id === "artifact-frame" ? iframe : id === "artifact-sheet" ? sheet : null),
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener: (t, fn) => (docListeners[t] ||= []).push(fn),
    createElement: (tag) => ({ tag, style: {}, textContent: "", type: "", className: "",
                               children: [], parentNode: null,
                               appendChild(c) { this.children.push(c); c.parentNode = this; },
                               addEventListener(t, fn) { (this._on ||= {})[t] = fn; } }),
  };
  const reloads = { n: 0 };
  const location = { pathname: "/a/abc", search: "", reload() { reloads.n++; } };
  const win = {
    innerWidth: 1280,
    addEventListener() {},
    fetch(url) {
      calls.push(url);
      return new Promise((resolve, reject) => { resolveNext = (payload, okFlag = true) =>
        resolve({ ok: okFlag, json: () => Promise.resolve(payload) }); rejectNext = reject; });
    },
  };
  new Function("document", "window", "URL", "Map", "Set", "isFinite", "navigator",
               "history", "location", "setTimeout", src)(
    doc, win, URL, Map, Set, isFinite, {},
    { replaceState() {}, pushState() {} }, location, (fn, ms) => timers.push({ fn, ms }));
  return {
    calls, reloads, body, timers,
    fail: async () => { rejectNext(new Error("net")); await tick(); await tick(); },
    answer: async (payload, okFlag = true) => { resolveNext(payload, okFlag); await tick(); await tick(); },
    show: () => body.children.find((c) => c.className === "newer"),
    press: async (label) => {
      const bar = body.children.find((c) => c.className === "newer");
      const b = bar && bar.children.find((c) => c.textContent === label);
      b._on.click();
      await tick();
    },
    reveal: async () => { hidden = false; (docListeners["visibilitychange"] || []).forEach((f) => f()); await tick(); },
    hide: () => { hidden = true; },
  };
}

// 1. it asks, holding, about the version in front of him
{
  const g = build({ version: "3" });
  ok(g.calls.length === 1, "asks once on load");
  ok(g.calls[0] === "/a/abc/current?since=3&wait=25",
     "about the version it is showing, and asks the server to HOLD -- no timer in this file at all");
}
// 2. nothing changed: it asks again, and says nothing to him
{
  const g = build({ version: "3" });
  await g.answer({ n: 3 });
  ok(!g.show(), "an unchanged answer shows him nothing");
  ok(g.calls.length === 2, "and it goes straight back to waiting");
}
// 3. a republish: it OFFERS, and does not reload
{
  const g = build({ version: "3" });
  await g.answer({ n: 4 });
  const bar = g.show();
  ok(!!bar, "a newer version puts a bar on the page");
  ok(bar.children[0].textContent.indexOf("newer version") >= 0, "in words");
  ok(g.reloads.n === 0,
     "and NOTHING reloads -- he chose (b) over the page changing under him");
}
// 4. his word is what applies it
{
  const g = build({ version: "3" });
  await g.answer({ n: 4 });
  await g.press("Show it");
  ok(g.reloads.n === 1, "Show it is what reloads, and only when he presses it");
}
// 5. dismissing is final for that version, and only that version
{
  const g = build({ version: "3" });
  await g.answer({ n: 4 });
  await g.press("Not now");
  ok(!g.show(), "Not now takes the bar away");
  await g.answer({ n: 4 });
  ok(!g.show(), "and the same version never asks twice");
  await g.answer({ n: 5 });
  ok(!!g.show(), "but a newer one still speaks up");
  ok(g.reloads.n === 0, "still nothing reloaded on its own");
}
// 6. a hidden tab stops asking, and resumes when looked at
{
  const g = build({ version: "3", hidden: true });
  ok(g.calls.length === 0, "a tab nobody is looking at asks nothing");
  await g.reveal();
  ok(g.calls.length === 1, "and picks it up again when he looks, on an event rather than a timer");
}
// 7. a failed read is not an offer, and not a loop
{
  const g = build({ version: "3" });
  await g.answer(null, false);
  ok(!g.show(), "a read that failed says nothing to him");
  ok(g.calls.length === 1, "and does not ask again at once (a 5xx used to spin this)");
  ok(g.timers.length === 1 && g.timers[0].ms >= 1000, "it asks again after a pause");
  g.timers[0].fn();
  ok(g.calls.length === 2, "and does ask again: the watch is renewed, not dead");
}
// 8. a dropped connection (a rollout ends the held read) does not end the watch for good
{
  const g = build({ version: "3" });
  await g.fail();
  ok(g.calls.length === 1 && g.timers.length === 1, "a rejected read waits and retries");
  g.timers[0].fn();
  ok(g.calls.length === 2, "the second ask goes out");
  await g.answer({ n: 4 });
  ok(!!g.show(), "and a republish after the blip is still offered");
}

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
