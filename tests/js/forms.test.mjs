// Drive the REAL page-side form listener (content.py FORM_JS, inside the shim) against a
// minimal fake DOM. Run by tests/test_forms_js.py, which takes the script from the app.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");

let fails = 0, passes = 0;
const ok = (c, m) => { c ? passes++ : (fails++, console.log("FAIL: " + m)); if (c) console.log("ok   " + m); };

const GALLERY = "https://artifacts.tenfourty.site";

function ctl(type, name, value = "", extra = {}) {
  return { type, name, value, checked: false, parentNode: null, nodeType: 1,
           getAttribute: () => null, ...extra };
}

function build(controls, opts = {}) {
  const form = {
    nodeType: 1, parentNode: null,
    getAttribute: (k) => (k === "data-artifact-form" ? (opts.formName ?? "rollout") : null),
    querySelectorAll: () => controls,
  };
  controls.forEach((c) => (c.parentNode = form));
  const doc = {
    documentElement: { scrollHeight: 10 },
    body: { scrollHeight: 10, offsetHeight: 10, getBoundingClientRect: () => ({ bottom: 10 }) },
    querySelectorAll: (sel) => (sel === "[data-artifact-form]" ? [form] : []),
    querySelector: () => null,
  };
  const listeners = {};
  const posted = [];
  const scope = {
    document: doc,
    parent: { postMessage: (m, o) => posted.push([m, o]) },
    scrollX: 0, scrollY: 0,
    addEventListener: (t, fn) => (listeners[t] ||= []).push(fn),
    getComputedStyle: () => ({ marginBottom: "0px" }),
    Array, JSON, Math, Object, String, CSS: undefined,
  };
  scope.window = scope;
  const names = Object.keys(scope);
  new Function(...names, src)(...names.map((n) => scope[n]));
  const forms = () => posted.filter(([m]) => m.type === "artifact-form");
  return {
    form, posted, forms,
    fire: (t, e) => (listeners[t] || []).forEach((fn) => fn(e)),
  };
}

const radios = () => [ctl("radio", "region", "a"), ctl("radio", "region", "b")];
const sample = () => [
  ...radios(),
  ctl("checkbox", "pets", "cat"), ctl("checkbox", "pets", "dog"),
  ctl("checkbox", "ack", "on"),
  ctl("text", "why", "because"), ctl("select-one", "size", ""),
  ctl("number", "count", "3"),
];

// 1. on load, one full snapshot per form, to the gallery's origin only
{
  const c = sample();
  c[1].checked = true; c[3].checked = true;   // radio b, pets dog
  const g = build(c);
  g.fire("load", {});
  const [m, origin] = g.forms()[0];
  ok(g.forms().length === 1 && origin === GALLERY, "one snapshot at load, never to '*'");
  ok(m.form === "rollout" && m.load === true, "names the form and says it is the load report");
  ok(JSON.stringify(m.values) === JSON.stringify({
    region: "b", pets: ["dog"], ack: false, why: "because", size: "", count: "3" }),
    "radio = checked value, group = list, lone checkbox = bool, others = string: " + JSON.stringify(m.values));
}
// 2. a radio nobody chose is null; an empty group is []; a lone checkbox that is ticked is true
{
  const c = sample(); c[4].checked = true;
  const g = build(c);
  g.fire("load", {});
  const v = g.forms()[0][0].values;
  ok(v.region === null && JSON.stringify(v.pets) === "[]" && v.ack === true,
     "null radio, empty group, true lone checkbox");
}
// 3. input and change inside the form report the whole snapshot; outside it, nothing
{
  const c = sample();
  const g = build(c);
  c[0].checked = true;
  g.fire("input", { target: c[0] });
  ok(g.forms().length === 1 && g.forms()[0][0].values.region === "a" && !g.forms()[0][0].load,
     "an input event reports the full snapshot (not marked as load)");
  g.fire("change", { target: c[5] });
  ok(g.forms().length === 2, "a change event reports too");
  g.fire("input", { target: { nodeType: 1, parentNode: null, getAttribute: () => null } });
  g.fire("input", {});
  g.fire("input", null);
  ok(g.forms().length === 2, "an event outside a form, or without a target, reports nothing");
}
// 4. values and names are capped
{
  const big = "x".repeat(5000);
  const c = [ctl("text", "t", big)];
  for (let i = 0; i < 250; i++) c.push(ctl("text", "f" + i, "v"));
  c.push(ctl("text", "__proto__", "evil"));
  const g = build(c);
  g.fire("load", {});
  const v = g.forms()[0][0].values;
  ok(v.t.length === 2000, "a value is cut to 2000 characters");
  ok(Object.keys(v).length === 200, "at most 200 fields are reported");
  ok(!Object.prototype.hasOwnProperty.call(v, "__proto__") && ({}).evil === undefined,
     "a __proto__ name is never reported");
}
// 5. the fill leg: only the gallery, only a known form, no events, caps, then a report
{
  const fillMsg = (over = {}) => ({
    origin: GALLERY,
    data: { type: "artifact-form-fill", form: "rollout",
            values: { region: "b", pets: ["cat"], ack: true, why: "hi", size: "l", count: 7, nope: "x" },
            ...over },
  });
  const c = sample();
  const g = build(c);
  g.fire("message", fillMsg());
  ok(c[1].checked === true && c[0].checked === false, "fills the radio");
  ok(c[2].checked === true && c[3].checked === false, "fills the checkbox group from a list");
  ok(c[4].checked === true, "fills a lone checkbox from true");
  ok(c[5].value === "hi" && c[6].value === "l" && c[7].value === "7", "fills text, select and number");
  ok(g.forms().length === 1 && g.forms()[0][0].values.region === "b" && !g.forms()[0][0].load,
     "reports the snapshot after a fill, without firing an input event");

  const other = build(sample());
  other.fire("message", { origin: "https://evil.example", data: fillMsg().data });
  ok(other.form.querySelectorAll()[1].checked === false && other.forms().length === 0,
     "ignores a fill from another origin");

  const wrong = build(sample());
  wrong.fire("message", fillMsg({ form: "other" }));
  ok(wrong.forms().length === 0, "ignores a fill for a form the page does not have");

  const huge = build(sample());
  huge.fire("message", fillMsg({ values: { why: "y".repeat(5000) } }));
  ok(huge.form.querySelectorAll()[5].value.length === 2000, "cuts a filled value to 2000 characters");

  const many = {};
  for (let i = 0; i < 300; i++) many["k" + i] = "v";
  many.why = "late";
  const capped = build(sample());
  capped.fire("message", fillMsg({ values: many }));
  ok(capped.form.querySelectorAll()[5].value === "because", "writes only the first 200 names");

  const junk = build(sample());
  for (const data of [null, "str", { type: "artifact-form-fill" }, { type: "artifact-form-fill", form: "rollout", values: null }])
    junk.fire("message", { origin: GALLERY, data });
  ok(junk.forms().length === 0, "survives malformed messages");
}
console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
