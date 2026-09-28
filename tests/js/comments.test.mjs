// Drive the REAL /comments.js against a minimal fake DOM (homelab-ij8.18,
// extended for page review step 1 / homelab-ij8.108): a selection reported by
// the page puts a three-action bar (Comment / Remove this / Looks good) under
// it. Comment fills the composer and opens the drawer, as before; Remove
// this/Looks good set kind and submit the mark on one tap when there is no
// note to add, and leave an in-progress note alone.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");
let fails = 0, passes = 0;
const ok = (c, m) => { c ? passes++ : (fails++, console.log("FAIL: " + m)); if (c) console.log("ok   " + m); };

function el(tag) {
  return { tag, style: {}, hidden: false, children: [], attrs: {}, on: {}, value: "", textContent: "", dataset: {},
           appendChild(c) { this.children.push(c); c.parentNode = this; },
           insertBefore(c) { this.children.unshift(c); c.parentNode = this; },
           addEventListener(t, f) { this.on[t] = f; }, focus() { this.focused = true; },
           closest(sel) {
             const tag = sel.replace(/^\W+/, "");
             let node = this;
             while (node) { if (node.tag === tag) return node; node = node.parentNode; }
             return null;
           } };
}
function build() {
  const frameWin = {};
  const iframe = { src: "https://abc.tenfourty.com/p/x/", contentWindow: frameWin,
                   getBoundingClientRect: () => ({ top: 100, left: 20 }) };
  const body = el("input"); body.name = "body";
  const form = el("form");
  form.submits = 0;
  form.requestSubmit = () => { form.submits++; };
  form.querySelector = (q) => {
    if (q === "input[name=body]") return body;
    if (q === ".cquote") return form.children.find((c) => c.className === "cquote") || null;
    const m = q.match(/input\[name=(\w+)\]/);
    return m ? form.children.find((c) => c.name === m[1]) || null : null;
  };
  const docBody = el("body");
  const doc = { getElementById: (id) => (id === "artifact-frame" ? iframe : null),
                querySelector: (q) => (q === "form.cnew" ? form : null),
                createElement: el, body: docBody };
  const listeners = {};
  const win = { scrollX: 0, scrollY: 50, addEventListener: (t, f) => (listeners[t] = f) };
  const location = { hash: "" };
  new Function("document", "window", "location", "URL", src)(doc, win, location, URL);
  const bar = docBody.children[0];
  const buttons = {};
  bar.children.forEach((b) => (buttons[b.dataset.kind] = b));
  const send = (data, { source = frameWin, origin = "https://abc.tenfourty.com" } = {}) =>
    listeners.message({ source, origin, data });
  const click = (kind) => bar.on.click({ target: buttons[kind] });
  return { bar, buttons, form, body, location, send, click };
}
const SEL = { type: "artifact-selection", quote: "the plan",
              anchor: { kind: "range", path: "body>p:nth-of-type(1)", start: 4, end: 12 },
              rect: { top: 30, left: 10, width: 60, height: 18 } };

{ const g = build();
  ok(g.bar && g.bar.hidden === true, "a hidden action bar exists");
  ok(["comment", "remove", "looks_good"].every((k) => g.buttons[k]), "with all three actions");
  g.send(SEL);
  ok(g.bar.hidden === false, "a selection shows it");
  ok(g.bar.style.top === "204px" && g.bar.style.left === "30px", "under the selection, in page coordinates");
  g.click("comment");
  const quote = g.form.children.find((c) => c.name === "quote");
  const anchor = g.form.children.find((c) => c.name === "anchor");
  const kind = g.form.children.find((c) => c.name === "kind");
  ok(quote && quote.value === "the plan", "the quote goes into the form");
  ok(anchor && JSON.parse(anchor.value).kind === "range", "and the anchor");
  ok(kind && kind.value === "comment", "and the kind");
  ok(g.form.children.find((c) => c.className === "cquote").textContent === "the plan", "the quote is shown, as text");
  ok(g.location.hash === "comments" && g.body.focused, "the drawer opens with the cursor in the box");
  ok(g.body.placeholder === "Comment on the selected text", "asking for a comment");
  ok(g.bar.hidden === true, "and the bar goes");
  ok(g.form.submits === 0, "a comment is never auto-submitted"); }
{ const g = build();
  g.send(SEL, { origin: "https://evil.example" });
  g.send(SEL, { source: {} });
  ok(g.bar.hidden === true, "another origin or window cannot show it"); }
{ const g = build();
  g.send(SEL); g.send({ type: "artifact-selection", quote: "" });
  ok(g.bar.hidden === true, "clearing the selection hides it");
  g.send({ ...SEL, rect: { top: "x", left: 1, height: 1 } });
  ok(g.bar.hidden === true, "a malformed rect is ignored"); }
{ const g = build();
  g.send(SEL);
  g.click("remove");
  const kind = g.form.children.find((c) => c.name === "kind");
  ok(kind && kind.value === "remove", "remove sets kind");
  ok(g.body.placeholder === "Add a note (optional)", "and offers an optional note");
  ok(g.form.submits === 1, "an empty note submits the mark on one tap"); }
{ const g = build();
  g.send(SEL);
  g.body.value = "already typing a note";
  g.click("looks_good");
  const kind = g.form.children.find((c) => c.name === "kind");
  ok(kind && kind.value === "looks_good", "looks_good sets kind");
  ok(g.form.submits === 0, "a note already in progress is never submitted for them"); }
{ const g = build();
  g.send(SEL);
  g.bar.on.click({ target: g.bar });
  ok(g.form.children.find((c) => c.name === "kind") === undefined, "a click that hits no button does nothing"); }

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
