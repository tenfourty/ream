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
    insertBefore(c, ref) { const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); c.parent = this; return c; },
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

function build({ draft = null, fetchImpl, form = {}, draftVersion = null } = {}) {
  const bar = node("div"), dataEl = node("script"), frame = node("iframe");
  dataEl.textContent = JSON.stringify({ ...DATA, forms: [{ ...DATA.forms[0], ...form }] });
  frame.attrs.src = ORIGIN + "/p/tok/";
  const win = { posted: [], postMessage(m, o) { this.posted.push([m, o]); } };
  frame.contentWindow = win;
  const body = node("body"); body.classList = { add() {} };
  const store = {};
  if (draft) store["artifact-form:art1:rollout"] = JSON.stringify(draft);
  if (draftVersion !== null) store["artifact-form:art1:rollout:v"] = String(draftVersion);
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
  // A report from the page while the sheet is open must never change what Confirm posts.
  const g = build();
  g.send(snap({ region: "a", why: "one", pets: [] }));
  g.go().click();
  const open = () => all(g.body).filter((n) => n.className === "fscrim");
  const sheet = open()[0];
  const confirm = all(sheet).find((n) => n.className === "fgo");
  g.send(snap({ region: "a", why: "one", pets: [] }));
  ok(open().length === 1, "an identical report leaves the sheet open");
  g.send(snap({ region: "b", why: "two", pets: ["cat"] }));
  ok(open().length === 0, "a report that changes the values closes the open sheet");
  confirm.click();
  const posted = g.fetched.length ? JSON.parse(g.fetched[0][1].body).values : null;
  ok(posted === null || (posted.region === "a" && posted.why === "one" && posted.pets === undefined),
     "Confirm, even if it fires, posts exactly the values the sheet showed");
  g.go().click();
  const text = all(open()[0]).map((n) => n.textContent).join("|");
  ok(text.includes("two") && text.includes("Cat"), "reopening shows the new values");
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
{
  // A successful send says so: the sheet stays open with a Sent line and one Close button,
  // and the bar shows the sent time and offers "Send again".
  const g = build({ fetchImpl: async () => ({ ok: true, status: 200, json: async () => ({ ok: true, answered_at: "2026-11-02T22:25:54Z" }) }) });
  g.send(snap({ region: "a", why: "", pets: [] }));
  g.go().click();
  const sheet = all(g.body).find((n) => n.className === "fscrim");
  all(sheet).find((n) => n.className === "fgo").click();
  await new Promise((r) => setTimeout(r, 10));
  ok(all(g.body).filter((n) => n.className === "fscrim").length === 1, "the sheet stays open after a successful send");
  const okp = all(sheet).find((n) => n.className === "fok");
  ok(okp && okp.textContent.startsWith("Your answers were sent"), "the sheet says the answers were sent");
  const btns = all(sheet).filter((n) => n.tag === "button");
  ok(btns.length === 1 && btns[0].textContent === "Close", "only a Close button is left");
  ok(g.count().startsWith("\u2713 Sent ") && g.count().endsWith("1 of 3 answered"), "the bar shows it was sent");
  ok(g.go().hidden === true && g.bar.children[0].className === "fbar fsent", "the bar drops Submit once the page shows the summary");
  const sums = () => g.win.posted.map(([m]) => m).filter((m) => m.type === "artifact-form-summary");
  ok(sums().length === 1 && sums()[0].summary.rows.some((r) => r.label === "Region" && r.text === "eu")
     && sums()[0].summary.rows.some((r) => r.label === "Pets" && r.text === "(no answer)") && sums()[0].summary.when !== "",
     "the page is told to show a summary of what was sent, with the sent time");
  btns[0].click();
  ok(all(g.body).filter((n) => n.className === "fscrim").length === 0, "Close closes the sheet");
  ok(sums().length === 2 && sums()[1].summary !== null && sums()[1].focus === true, "Close puts focus on the summary's button");
  // Change answers brings the form back, and Send again is offered.
  g.send({ type: "artifact-form-edit", form: "rollout" });
  ok(sums().length === 3 && sums()[2].summary === null && sums()[2].focus === true, "Change answers asks the page for the form back, focused");
  ok(g.go().hidden === false && g.go().textContent === "Send again", "the bar offers Send again while the answers are being changed");
  g.send({ type: "artifact-form-edit", form: "rollout" });
  ok(sums().length === 3, "a second edit request changes nothing");
  g.send(snap({ region: "b", why: "", pets: [] }));
  ok(sums().length === 3, "reports while editing do not bring the summary back");
}
{
  // A form already sent opens as a summary, once the page has reported; an unsent one never gets one.
  const g = build({ form: { answered: { answered_at: "2026-11-02T22:25:54Z" }, carried: { region: "a", pets: ["dog"] } } });
  const sums = () => g.win.posted.map(([m]) => m).filter((m) => m.type === "artifact-form-summary");
  ok(sums().length === 0, "nothing is posted before the page reports");
  g.send(snap({ region: null, why: "", pets: [] }, true));
  ok(g.win.posted[0][0].type === "artifact-form-fill" && sums().length === 1, "the load report is answered with the fill, then the summary");
  const rows = sums()[0].summary.rows;
  ok(rows[0].text === "eu" && rows[2].text === "Dog" && rows[1].text === "(no answer)" && sums()[0].focus === false,
     "the summary has stored labels and the sent answers, and does not steal focus on load");
  g.send(snap({ region: "a", why: "", pets: ["dog"] }));
  ok(sums().length === 1, "an identical state is not posted again");
  g.send(snap({ region: "a", why: "", pets: ["dog"] }, true));
  ok(sums().length === 2, "a reloaded page gets its summary again");
  const plain = build();
  plain.send(snap({ region: "a", why: "", pets: [] }, true));
  ok(!plain.win.posted.some(([m]) => m.type === "artifact-form-summary"), "a form never sent gets no summary");
  ok(plain.go().hidden === false, "and keeps its Submit button");
}
{
  // Slice 3: carried answers fill the controls; a draft of another version never refills a
  // stale field; the stale panel and the summary sheet say what is on record.
  const STALE = { name: "why", value: "old", text: "old", version: 1, label: "Why not", reason: "changed" };
  const carried = build({ form: { carried: { region: "a", bogus: "x" } } });
  carried.send(snap({ region: null, why: "", pets: [] }, true));
  const fill = carried.win.posted[0][0];
  ok(fill.type === "artifact-form-fill" && JSON.stringify(fill.values) === JSON.stringify({ region: "a" }),
     "fills the controls from carried answers, stored names only");

  const over = build({ draft: { region: "b", why: "typed", pets: [] }, draftVersion: 2, form: { carried: { region: "a" } } });
  over.send(snap({ region: null, why: "", pets: [] }, true));
  ok(JSON.stringify(over.win.posted[0][0].values) === JSON.stringify({ region: "b", why: "typed" }),
     "a non-blank draft value overrides a carried one, a blank draft value does not");

  const old = build({ draft: { region: "b", why: "stale text" }, draftVersion: 1, form: { stale: [STALE] } });
  old.send(snap({ region: null, why: "", pets: [] }, true));
  ok(JSON.stringify(old.win.posted[0][0].values) === JSON.stringify({ region: "b" }),
     "a draft from another form version does not refill a stale field");
  const same = build({ draft: { why: "kept" }, draftVersion: 2, form: { stale: [STALE] } });
  same.send(snap({ region: null, why: "", pets: [] }, true));
  ok(same.win.posted[0][0].values.why === "kept", "a draft of the current version still fills the field");

  const panel = all(old.bar).find((n) => n.className === "fstale");
  const ptext = panel ? all(panel).map((n) => n.textContent).join("|") : "";
  ok(panel && ptext.includes("1 earlier answer not carried") && ptext.includes("Earlier answer, version 1: old")
     && ptext.includes("the question changed"), "the stale panel renders the old answer, its version and why");
  ok(!all(build().bar).some((n) => n.className === "fstale"), "no stale panel when nothing is stale");

  const sh = build({ form: {
    carried: { region: "a" },
    relayed: { why: { text: "from chat", source: "slack", by: "ops" } },
    stale: [STALE, { name: "gone", value: "x", text: "xx", version: 1, label: "Gone", reason: "removed" }] } });
  sh.send(snap({ region: "b", why: "", pets: [] }));
  sh.go().click();
  const st = all(all(sh.body).find((n) => n.className === "fscrim")).map((n) => n.textContent).join("|");
  ok(st.includes("On record from his last send: eu"), "the sheet notes an answer on record that differs from the one shown");
  ok(st.includes("On record, relayed from slack by ops: from chat"), "the sheet notes a relayed value");
  ok(st.includes("Earlier answer, version 1: old") && st.includes("(it read: Why not)"), "the sheet notes a stale answer and its old wording");
  ok(st.includes("Gone (no longer asked)") && st.includes("the question was removed"), "the sheet lists a removed field's answer");
}
console.log(`\n${passes} passed, ${fails} failed`);
