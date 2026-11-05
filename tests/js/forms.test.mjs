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
  ok(g.forms().length === 1 && origin === GALLERY, "one snapshot at load, never to a wildcard origin");
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
  const big = "x".repeat(15000);
  const c = [ctl("text", "t", big)];
  for (let i = 0; i < 250; i++) c.push(ctl("text", "f" + i, "v"));
  c.push(ctl("text", "__proto__", "evil"));
  const g = build(c);
  g.fire("load", {});
  const v = g.forms()[0][0].values;
  ok(v.t.length === 10000, "a value is cut to 10000 characters");
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
  huge.fire("message", fillMsg({ values: { why: "y".repeat(15000) } }));
  ok(huge.form.querySelectorAll()[5].value.length === 10000, "cuts a filled value to 10000 characters");

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
// 6. the summary leg: a sent form is swapped for a plain-text summary and a Change answers button
{
  const mk = (tag) => ({ tag, nodeType: 1, children: [], attrs: {}, style: {}, className: "", _t: "", listeners: {}, parentNode: null,
    set textContent(v) { this._t = String(v); }, get textContent() { return this._t; },
    set innerHTML(v) { throw new Error("innerHTML must never be used"); },
    setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; },
    insertBefore(c, ref) { const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); c.parentNode = this; return c; },
    get nextSibling() { const p = this.parentNode; return p ? p.children[p.children.indexOf(this) + 1] || null : null; },
    addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); },
    click() { (this.listeners.click || []).forEach((fn) => fn({})); },
    focus() { this.focused = true; } });
  const flat = (n, out = []) => { out.push(n); n.children.forEach((c) => flat(c, out)); return out; };
  const root = mk("body"), form = mk("form"), first = Object.assign(mk("input"), { type: "text", name: "why", value: "x" });
  form.setAttribute("data-artifact-form", "rollout");
  form.querySelectorAll = () => [first];
  form.style.display = "block";
  first.parentNode = form; root.appendChild(form);
  const doc = { documentElement: { scrollHeight: 10 }, body: { scrollHeight: 10, offsetHeight: 10, getBoundingClientRect: () => ({ bottom: 10 }) },
    createElement: mk,
    querySelectorAll: (sel) => sel === "[data-artifact-form]" ? [form]
      : sel === "[data-artifact-form-summary]" ? flat(root).filter((n) => n.attrs["data-artifact-form-summary"] !== undefined) : [],
    querySelector: () => null };
  const listeners = {}, posted = [];
  const scope = { document: doc, parent: { postMessage: (m, o) => posted.push([m, o]) }, scrollX: 0, scrollY: 0,
    addEventListener: (t, fn) => (listeners[t] ||= []).push(fn), getComputedStyle: () => ({ marginBottom: "0px" }),
    Array, JSON, Math, Object, String, CSS: undefined };
  scope.window = scope;
  const names = Object.keys(scope);
  new Function(...names, src)(...names.map((n) => scope[n]));
  const send = (data, origin = GALLERY) => listeners.message.forEach((fn) => fn({ origin, data }));
  const summary = { title: "Rollout", when: "5 Nov, 10:00", rows: [{ label: "Why", text: "<img src=x onerror=1>" }, { label: "Pets", text: "(no answer)" }] };

  send({ type: "artifact-form-summary", form: "rollout", summary }, "https://evil.example");
  ok(form.style.display === "block" && !flat(root).some((n) => n.tag === "section"), "a summary from another origin is ignored");
  send({ type: "artifact-form-summary", form: "other", summary });
  ok(form.style.display === "block", "a summary for a form the page lacks is ignored");
  send({ type: "artifact-form-summary", form: "rollout", summary: { title: 1 } });
  ok(form.style.display === "block", "a malformed summary is ignored");

  send({ type: "artifact-form-summary", form: "rollout", summary });
  const sec = flat(root).find((n) => n.tag === "section");
  ok(form.style.display === "none" && sec && form.parentNode.children[1] === sec, "the form is hidden and the summary sits right after it");
  const text = flat(sec).map((n) => n.textContent).join("|");
  ok(text.includes("Rollout") && text.includes("5 Nov, 10:00") && text.includes("Why") && text.includes("<img src=x onerror=1>") && text.includes("(no answer)"),
     "the summary shows the title, when, each label and answer as plain text");
  ok(sec.attrs.role === "group" && sec.attrs["aria-label"] === "Answers sent: Rollout", "the summary is a labelled group");
  const btn = flat(sec).find((n) => n.tag === "button");
  ok(btn && btn.textContent === "Change answers" && btn.type === "button", "a real button says Change answers");
  btn.click();
  const edit = posted.map(([m]) => m).filter((m) => m.type === "artifact-form-edit");
  ok(edit.length === 1 && edit[0].form === "rollout" && posted.at(-1)[1] === GALLERY, "the button asks the gallery, at its origin only");

  send({ type: "artifact-form-summary", form: "rollout", summary: { ...summary, rows: [{ label: "Why", text: "two" }] }, focus: false });
  ok(flat(root).filter((n) => n.tag === "section").length === 1, "a second summary replaces the first, never stacks");
  send({ type: "artifact-form-summary", form: "rollout", summary: null, focus: true });
  ok(form.style.display === "block" && !flat(root).some((n) => n.tag === "section"), "summary null removes it and restores the form's own display");
  ok(first.focused === true, "focus goes to the form's first control");
  ok(form.getAttribute("data-artifact-was") === null, "no bookkeeping attribute is left behind");
}
console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
