// Drive the REAL "no such question" note against a minimal fake DOM.
//
// homelab-ij8.50. The server tells the shim which slot ids name no decision
// on the page; the shim writes a sentence into each such slot, so a reader
// sees why there is no control instead of blank space that looks like a
// question which lost its options.
//
// Run by tests/test_shim_js.py with the served script for ids "gone1" and
// 'a"b', which it takes from the running app rather than a copy.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");

let fails = 0, passes = 0;
const ok = (c, m) => { c ? passes++ : (fails++, console.log("FAIL: " + m)); if (c) console.log("ok   " + m); };

function el(id, text = "") {
  return { style: {}, textContent: text, _id: id,
           getBoundingClientRect: () => ({ top: 0, left: 0, width: 0 }),
           getAttribute(k) { return k === "data-artifact-decision" ? this._id : null; } };
}

function build({ css = undefined, throwOnQuery = false, fallback = "" } = {}) {
  const gone = el("gone1", fallback);
  const quoted = el('a"b');
  const live = el("live1");
  const all = [gone, quoted, live];
  const listeners = {};
  const asked = [];
  const doc = {
    documentElement: { scrollHeight: 100 },
    body: { scrollHeight: 100, offsetHeight: 100, getBoundingClientRect: () => ({ bottom: 100 }) },
    querySelectorAll: () => all,
    // Answers ONLY an exactly-formed attribute selector, so a selector that is
    // wrongly quoted or wrongly escaped finds nothing here, as it would in a
    // browser, instead of passing because the stub ignored it.
    querySelector(sel) {
      asked.push(sel);
      if (throwOnQuery) throw new Error("boom");
      return all.find((e) => sel === `[data-artifact-decision="${css ? css.escape(e._id) : e._id}"]`) || null;
    },
  };
  const scope = {
    document: doc,
    parent: { postMessage: () => {} },
    scrollX: 0, scrollY: 0,
    addEventListener: (t, fn) => (listeners[t] ||= []).push(fn),
    getComputedStyle: () => ({ marginBottom: "0px" }),
    Array, JSON, Math, CSS: css,
  };
  scope.window = scope;
  const names = Object.keys(scope);
  new Function(...names, src)(...names.map((n) => scope[n]));
  return { gone, quoted, live, asked,
           fire: (t) => (listeners[t] || []).forEach((fn) => fn({})) };
}

// A stand-in for CSS.escape that is visibly different from the raw id, so a
// shim that forgot to call it cannot find the quoted slot.
const CSSStub = { escape: (s) => s.replace(/"/g, '\\"') };

// 1. nothing is written before the document has been parsed
{
  const g = build({ css: CSSStub });
  ok(g.gone.textContent === "", "writes nothing before DOMContentLoaded -- the slot has not been parsed yet");
  g.fire("DOMContentLoaded");
  ok(g.gone.textContent.includes("“gone1”"), "names the id it could not find, so the author can be told which");
  ok(g.gone.textContent.includes("Nothing has been lost"), "says nothing of the reader's is gone -- the reading Jeremy took");
  ok(g.gone.textContent.startsWith("There should be a question here"), "says what is missing, in words");
}
// 2. how it looks
{
  const g = build({ css: CSSStub });
  g.fire("DOMContentLoaded");
  const s = g.gone.style;
  ok(s.display === "block" && s.border === "1px dashed currentColor",
     "draws a dashed outline in the page's own text colour, so it reads on dark pages too");
  ok(s.opacity === ".75", "and recedes rather than shouting");
  ok(s.minHeight === undefined, "leaves min-height to the size leg");
}
// 3. only the slots the server named
{
  const g = build({ css: CSSStub });
  g.fire("DOMContentLoaded");
  ok(g.live.textContent === "" && Object.keys(g.live.style).length === 0,
     "never touches a slot the server did not name -- that one is a real question arriving");
}
// 4. ids that need escaping
{
  const g = build({ css: CSSStub });
  g.fire("DOMContentLoaded");
  ok(g.quoted.textContent.includes('“a"b”'), "finds a slot whose id needs CSS escaping");
  const raw = build({ css: undefined });
  raw.fire("DOMContentLoaded");
  ok(raw.gone.textContent.includes("gone1"), "still works where CSS.escape does not exist");
}
// 5. the page's own words win
{
  const g = build({ css: CSSStub, fallback: "  Waiting for the ops seat to ask this.  " });
  g.fire("DOMContentLoaded");
  ok(g.gone.textContent === "  Waiting for the ops seat to ask this.  ",
     "leaves a slot alone when the page already put its own words in it");
  ok(Object.keys(g.gone.style).length === 0, "and does not restyle it either");
}
// 6. it cannot throw into the page
{
  const g = build({ css: CSSStub, throwOnQuery: true });
  let threw = false;
  try { g.fire("DOMContentLoaded"); } catch (e) { threw = true; }
  ok(!threw, "a throwing DOM call stays inside the shim, out of the agent's console");
}

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
