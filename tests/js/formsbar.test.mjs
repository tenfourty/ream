// Drive the gallery's /forms.js (forms_ui.FORMS_JS) against a minimal fake DOM: the origin and
// source checks, the draft replay, the Submit bar's count and disabled state, and the summary
// sheet built with textContent only. Run by tests/test_forms_js.py.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");
let fails = 0, passes = 0;
const ok = (c, m) => { c ? passes++ : (fails++, console.log("FAIL: " + m)); if (c) console.log("ok   " + m); };

const ORIGIN = "https://page.tenfourty.com";
function node(tag) {
  const n = { tag, children: [], className: "", _text: "", listeners: {}, hidden: false, disabled: false,
    style: {}, attrs: {},
    set textContent(v) { this._text = String(v); }, get textContent() { return this._text; },
    set innerHTML(v) { throw new Error("innerHTML must never be used"); },
    appendChild(c) { this.children.push(c); c.parent = this; return c; },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this); },
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k] ?? null; },
    addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); },
    click() { (this.listeners.click || []).forEach((fn) => fn({ target: this })); },
    focus() {}, classList: { add() {} },
  };
  return n;
}
const all = (n, out = []) => { out.push(n); n.children.forEach((c) => all(c, out)); return out; };

const DATA = { artifact_id: "art1", forms: [{
  form: "rollout", title: "Rollout", version: 2, complete: null, answered: null,
  fields: [
    { name: "region", type: "radio", label: "Region", required: true,
      options: [{ value: "a", label: "eu" }, { value: "b", label: "us" }] },
    { name: "why", type: "text", label: "Why", required: false, options: [] },
    { name: "pets", type: "checkbox", label: "Pets", required: false,
      options: [{ value: "cat", label: "Cat" }, { value: "dog", label: "Dog" }] },
  ] }] };

function build({ draft = null, fetchImpl } = {}) {
  const bar = node("div"), dataEl = node("script"), frame = node("iframe");
  dataEl.textContent = JSON.stringify(DATA);
  frame.attrs.src = ORIGIN + "/p/tok/";
  const win = { posted: [], postMessage(m, o) { this.posted.push([m, o]); } };
  frame.contentWindow = win;
  const body = node("body"); body.classList = { add() {} };
  const store = {};
  if (draft) store["artifact-form:art1:rollout"] = JSON.stringify(draft);
  const listeners = {};
  const document = {
    body, getElementById: (id) => ({ "artifact-frame": frame, "artifact-forms-bar": bar, "artifact-forms": dataEl })[id] ?? null,
    createElement: node, addEventListener() {}, removeEventListener() {},
  };
  const fetched = [];
  const scope = {
    document, location: { href: "https://artifacts.tenfourty.site/a/art1" }, URL, JSON, Object, Array, Date, Promise,
    sessionStorage: { getItem: (k) => store[k] ?? null, setItem: (k, v) => (store[k] = v) },
    addEventListener: (t, fn) => (listeners[t] ||= []).push(fn),
    fetch: (url, init) => { fetched.push([url, init]); return fetchImpl ? fetchImpl(url, init) : Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) }); },
    encodeURIComponent,
  };
  const names = Object.keys(scope);
  new Function(...names, src)(...names.map((n) => scope[n]));
  const row = bar.children[0];
  return {
    bar, win, store, fetched, body,
    count: () => all(bar).find((n) => n.className === "fcount").textContent,
    go: () => all(bar).find((n) => n.className === "fgo"),
    send: (m, over = {}) => (listeners.message || []).forEach((fn) =>
      fn({ source: frame.contentWindow, origin: ORIGIN, data: m, ...over })),
  };
}
const snap = (values, load = false) => ({ type: "artifact-form", form: "rollout", values, ...(load ? { load: true } : {}) });

{
  const g = build();
  ok(g.count() === "0 of 3 answered" && g.go().disabled === true, "starts with nothing answered and Submit disabled");
  g.send(snap({ region: "a", why: "", pets: [] }));
  ok(g.count() === "1 of 3 answered" && g.go().disabled === false, "a required field answered enables Submit");
  g.send(snap({ region: null, why: "x", pets: ["cat"] }));
  ok(g.go().disabled === true, "a blank required radio disables Submit");
}
{
  const g = build();
  g.send(snap({ region: "a" }), { origin: "https://evil.example" });
  g.send(snap({ region: "a" }), { source: { postMessage() {} } });
  g.send(snap({ region: "a" }, false), { data: { type: "other" } });
  ok(g.count() === "0 of 3 answered", "ignores a message from another origin, another source or another type");
  g.send(snap({ region: "a", nope: "x", why: {} }));
  ok(g.count() === "1 of 3 answered", "keeps only stored field names (and sane value types)");
}
{
  const g = build({ draft: { region: "b", why: "kept", bogus: "x" } });
  g.send(snap({ region: null, why: "", pets: [] }, true));
  const [m, o] = g.win.posted[0];
  ok(o === ORIGIN && m.type === "artifact-form-fill" && m.form === "rollout", "replays a stored draft after the load report, to the frame's origin");
  ok(JSON.stringify(m.values) === JSON.stringify({ region: "b", why: "kept" }), "the replayed draft holds only stored field names");
  ok(g.count() === "0 of 3 answered", "the load report does not overwrite the draft");
  g.send(snap({ region: "b", why: "kept", pets: [] }));
  ok(g.count() === "2 of 3 answered", "the report after the fill is adopted");
  ok(JSON.parse(g.store["artifact-form:art1:rollout"]).region === "b", "the draft is kept in the tab's sessionStorage");
}
{
  let resolve;
  const g = build({ fetchImpl: () => new Promise((r) => (resolve = r)) });
  g.send(snap({ region: "a", why: "<img src=x onerror=1>", pets: ["dog"] }));
  g.go().click();
  const sheet = all(g.body).filter((n) => n.className === "fscrim");
  ok(sheet.length === 1, "Submit opens the summary sheet");
  const text = all(sheet[0]).map((n) => n.textContent).join("|");
  ok(text.includes("Region") && text.includes("eu") && text.includes("<img src=x onerror=1>") && text.includes("Dog"),
     "the sheet shows stored labels and the values being sent, verbatim as text");
  ok(g.fetched.length === 0, "nothing is sent before Confirm");
  all(sheet[0]).find((n) => n.className === "fgo").click();
  ok(g.fetched.length === 1 && g.fetched[0][0] === "/a/art1/forms/rollout", "Confirm posts to the gallery's own route");
  const sent = JSON.parse(g.fetched[0][1].body);
  ok(sent.version === 2 && sent.values.region === "a" && sent.values.pets[0] === "dog", "posts the stored version and the values");
  ok(g.fetched[0][1].headers["Content-Type"] === "application/json", "as JSON");
}
{
  const g = build({ fetchImpl: async () => ({ ok: false, status: 422, json: async () => ({ detail: "this field is required", field: "region" }) }) });
  g.send(snap({ region: "a", why: "", pets: [] }));
  g.go().click();
  const sheet = all(g.body).find((n) => n.className === "fscrim");
  all(sheet).find((n) => n.className === "fgo").click();
  await new Promise((r) => setTimeout(r, 10));
  const err = all(sheet).find((n) => n.className === "ferr");
  ok(err.textContent === "Region: this field is required" && err.hidden === false, "a 422 shows the stored label and the server's detail");
}
console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
