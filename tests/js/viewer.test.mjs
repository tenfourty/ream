// Drive the REAL gallery script against a minimal fake DOM and assert what it
// DOES, not what its source text contains.
//
// Why this file exists: the slice that added the script shipped 18 tests, and
// every one of them was a string assertion on the served bytes -- "the file
// contains `event.origin`" proves nothing about whether the check runs, or
// which way round it is. The repo has no JS runtime in its dev dependencies,
// which is how that gap opened. It needs no dependency: node plus ~40 lines of
// fake DOM is enough to execute the thing and watch what it touches.
//
// Run by tests/test_viewer_js.py, which skips when node is absent, and takes
// the script from the running app rather than a copy -- a copy would drift.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");

let fails = 0, passes = 0;
const ok = (c, m) => { c ? passes++ : (fails++, console.log("FAIL: " + m)); if (c) console.log("ok   " + m); };

function el(attrs = {}, offsetHeight = 88) {
  return { style: {}, parentNode: null, _attrs: attrs, offsetHeight,
           getAttribute(k) { return this._attrs[k] ?? null; } };
}

function build() {
  const iframe = el(); iframe.src = "https://abc.tenfourty.com/";
  const posted = [];
  iframe.contentWindow = { NAME: "frame", postMessage: (m, o) => posted.push([m, o]) };
  const sheet = el(); sheet.appendChild = function (c) { c.parentNode = sheet; };
  const d1 = el({ "data-decision-id": "dec1" });
  const d2 = el({ "data-decision-id": "dec2" });
  const listeners = [];
  const doc = {
    getElementById: (id) => id === "artifact-frame" ? iframe : id === "artifact-sheet" ? sheet : null,
    querySelectorAll: () => [d1, d2],
  };
  const win = { addEventListener: (t, fn) => t === "message" && listeners.push(fn) };
  const ctx = { document: doc, window: win, URL, Map, Set, isFinite, console };
  new Function("document", "window", "URL", "Map", "Set", "isFinite", src)
    (doc, win, URL, Map, Set, isFinite);
  const send = (e) => listeners.forEach((fn) => fn(e));
  return { iframe, sheet, d1, d2, send, posted };
}

const good = (over = {}) => ({
  source: undefined, origin: "https://abc.tenfourty.com",
  data: { type: "artifact-state", height: 1200, slots: [{ id: "dec1", top: 40, left: 10, width: 300 }] },
  ...over,
});

// 1. happy path
{
  const g = build();
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.iframe.style.height === "1200px", "sizes the frame to the reported height");
  ok(g.d1.parentNode === g.sheet, "moves the marked decision onto the sheet");
  ok(g.d1.style.top === "40px" && g.d1.style.left === "10px" && g.d1.style.width === "300px", "positions it where the page said");
  ok(g.d2.parentNode === null, "leaves an unmarked decision where it was");
}
// 2. wrong source window
{
  const g = build();
  g.send(good({ source: { NAME: "evil" } }));
  ok(g.iframe.style.height === undefined && g.d1.parentNode === null, "ignores a message from another window");
}
// 3. wrong origin
{
  const g = build();
  g.send(good({ source: g.iframe.contentWindow, origin: "https://evil.example" }));
  ok(g.iframe.style.height === undefined && g.d1.parentNode === null, "ignores a message from another origin");
}
// 4. height clamp
{
  for (const [h, why] of [[1e9, "absurdly tall"], [10, "absurdly short"], ["900", "a string"], [NaN, "NaN"], [Infinity, "Infinity"]]) {
    const g = build();
    const m = good({ source: g.iframe.contentWindow });
    m.data.height = h;
    g.send(m);
    ok(g.iframe.style.height === undefined, `refuses a height that is ${why}`);
  }
  const g = build(); const m = good({ source: g.iframe.contentWindow });
  m.data.height = 20000; g.send(m);
  ok(g.iframe.style.height === "20000px", "accepts the top of the allowed range");
}
// 5. unknown / duplicate / malformed slots
{
  const g = build();
  const m = good({ source: g.iframe.contentWindow });
  m.data.slots = [{ id: "nope", top: 1, left: 1, width: 1 },
                  { id: "dec1", top: 40, left: 10, width: 300 },
                  { id: "dec1", top: 999, left: 999, width: 999 },
                  { id: "dec2", top: "x", left: 1, width: 1 }];
  g.send(m);
  ok(g.d1.style.top === "40px", "a repeated slot does not move the control again");
  ok(g.d2.parentNode === null, "a slot with a non-numeric position is ignored");
}
// 6. hostile shapes must not throw
{
  for (const d of [null, "string", 42, [], { type: "other" }, { type: "artifact-state", slots: "nope" },
                   { type: "artifact-state", slots: new Array(65).fill({ id: "dec1", top: 1, left: 1, width: 1 }) }]) {
    const g = build();
    let threw = false;
    try { g.send(good({ source: g.iframe.contentWindow, data: d })); } catch (e) { threw = true; }
    ok(!threw, "survives a hostile payload: " + JSON.stringify(d).slice(0, 40));
  }
}
// 7. the return leg: the page is told how tall the placed control turned out
{
  const g = build();
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.posted.length === 1, "tells the page the size of the control it placed");
  ok(g.posted[0][1] === "https://abc.tenfourty.com", "addresses the size message to the content origin, never '*'");
  ok(g.posted[0][0].type === "artifact-slot-sizes", "labels the size message");
  ok(JSON.stringify(g.posted[0][0].sizes) === '[{"id":"dec1","height":88}]', "reports the measured height for that slot");
  // The page reserves the room, its height changes, and it reports again. The
  // control has not changed size, so nothing new is sent -- otherwise this and
  // the page's ResizeObserver would drive each other forever.
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.posted.length === 1, "does not repeat a size that has not changed");
  g.d1.offsetHeight = 120;
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.posted.length === 2 && g.posted[1][0].sizes[0].height === 120, "reports a size that genuinely changed");
}
// 8. nothing is sent when nothing was placed
{
  const g = build();
  const m = good({ source: g.iframe.contentWindow });
  m.data.slots = [{ id: "nope", top: 1, left: 1, width: 1 }];
  g.send(m);
  ok(g.posted.length === 0, "says nothing when no control was placed");
}
// 9. an unmeasurable control is not reported
{
  const g = build();
  g.d1.offsetHeight = undefined;
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.d1.parentNode === g.sheet, "still places a control it cannot measure");
  ok(g.posted.length === 0, "but reports no height for it");
}

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
