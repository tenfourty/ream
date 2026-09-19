// Drive the REAL page-side shim against a minimal fake DOM.
//
// The companion to tests/js/viewer.test.mjs, for the other end of the same
// conversation. The shim runs in the agent's own page, inside the frame, and
// never under pytest -- so without this, every test of it can only assert that
// certain bytes were served, which cannot tell an origin check that runs from
// one that is merely written down.
//
// Run by tests/test_shim_js.py, which takes the script from the running app.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");

let fails = 0, passes = 0;
const ok = (c, m) => { c ? passes++ : (fails++, console.log("FAIL: " + m)); if (c) console.log("ok   " + m); };

const GALLERY = "https://artifacts.tenfourty.site";

function el(attrs = {}, rect = { top: 40, left: 10, width: 300 }) {
  return { style: {}, _attrs: attrs, getBoundingClientRect: () => rect,
           getAttribute(k) { return this._attrs[k] ?? null; } };
}

function build() {
  const slot = el({ "data-artifact-decision": "dec1" });
  const listeners = {};
  const posted = [];
  const doc = {
    documentElement: { scrollHeight: 1234 },
    querySelectorAll: () => [slot],
    querySelector: (sel) => sel.includes('"dec1"') ? slot : null,
  };
  const scope = {
    document: doc,
    parent: { postMessage: (m, o) => posted.push([m, o]) },
    scrollX: 0, scrollY: 0,
    addEventListener: (t, fn) => (listeners[t] ||= []).push(fn),
    Array, JSON, CSS: undefined,
  };
  scope.window = scope;
  const names = Object.keys(scope);
  new Function(...names, src)(...names.map((n) => scope[n]));
  return {
    slot, posted,
    fire: (t, e) => (listeners[t] || []).forEach((fn) => fn(e)),
    has: (t) => (listeners[t] || []).length > 0,
  };
}

const sized = (over = {}) => ({
  origin: GALLERY,
  data: { type: "artifact-slot-sizes", sizes: [{ id: "dec1", height: 96 }] },
  ...over,
});

// 1. what it reports on load
{
  const g = build();
  ok(g.has("load") && g.has("resize"), "reports on load and on resize");
  g.fire("load", {});
  ok(g.posted.length === 1, "posts exactly one message per trigger");
  const [msg, origin] = g.posted[0];
  ok(origin === GALLERY, "posts only to the gallery's origin, never '*'");
  ok(msg.type === "artifact-state", "labels the message");
  ok(msg.height === 1234, "reports the document's own height");
  ok(JSON.stringify(msg.slots) === '[{"id":"dec1","top":40,"left":10,"width":300}]',
     "reports the marked slot's page-coordinate rectangle, and nothing of its contents");
}
// 2. the return leg reserves the room
{
  const g = build();
  g.fire("message", sized());
  ok(g.slot.style.minHeight === "96px", "reserves the room the gallery measured");
}
// 3. and only from the gallery
{
  const g = build();
  g.fire("message", sized({ origin: "https://evil.example" }));
  ok(g.slot.style.minHeight === undefined, "ignores a size message from another origin");
}
// 4. bounds and shapes
{
  for (const [h, why] of [[-1, "negative"], [20001, "absurd"], ["96", "a string"], [NaN, "NaN"], [Infinity, "Infinity"]]) {
    const g = build();
    const m = sized(); m.data.sizes = [{ id: "dec1", height: h }];
    g.fire("message", m);
    ok(g.slot.style.minHeight === undefined, `refuses a height that is ${why}`);
  }
  const g = build();
  const m = sized(); m.data.sizes = [{ id: "nope", height: 96 }];
  g.fire("message", m);
  ok(g.slot.style.minHeight === undefined, "ignores a size for a slot the page never marked");
}
// 5. hostile payloads must not throw
{
  for (const d of [null, "string", 42, [], { type: "other" },
                   { type: "artifact-slot-sizes" },
                   { type: "artifact-slot-sizes", sizes: "nope" },
                   { type: "artifact-slot-sizes", sizes: [null, 7, {}] },
                   { type: "artifact-slot-sizes", sizes: new Array(200).fill({ id: "dec1", height: 96 }) }]) {
    const g = build();
    let threw = false;
    try { g.fire("message", sized({ data: d })); } catch (e) { threw = true; }
    ok(!threw, "survives a hostile size payload: " + JSON.stringify(d).slice(0, 40));
  }
}
// 6. it touches nothing but that one property
{
  const g = build();
  g.fire("message", sized());
  ok(Object.keys(g.slot.style).join() === "minHeight", "sets min-height and no other style");
}

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
