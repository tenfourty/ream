// Drive the REAL /comments.js against a minimal fake DOM (homelab-ij8.18):
// a selection reported by the page puts a Comment button under it; pressing it
// fills the composer's hidden quote and anchor and opens the drawer.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");
let fails = 0, passes = 0;
const ok = (c, m) => { c ? passes++ : (fails++, console.log("FAIL: " + m)); if (c) console.log("ok   " + m); };

function el(tag) {
  return { tag, style: {}, hidden: false, children: [], attrs: {}, on: {}, value: "", textContent: "",
           appendChild(c) { this.children.push(c); c.parentNode = this; },
           insertBefore(c) { this.children.unshift(c); c.parentNode = this; },
           addEventListener(t, f) { this.on[t] = f; }, focus() { this.focused = true; } };
}
function build() {
  const frameWin = {};
  const iframe = { src: "https://abc.tenfourty.com/p/x/", contentWindow: frameWin,
                   getBoundingClientRect: () => ({ top: 100, left: 20 }) };
  const body = el("input"); body.name = "body";
  const form = el("form");
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
  const btn = docBody.children[0];
  const send = (data, { source = frameWin, origin = "https://abc.tenfourty.com" } = {}) =>
    listeners.message({ source, origin, data });
  return { btn, form, body, location, send };
}
const SEL = { type: "artifact-selection", quote: "the plan",
              anchor: { kind: "range", path: "body>p:nth-of-type(1)", start: 4, end: 12 },
              rect: { top: 30, left: 10, width: 60, height: 18 } };

{ const g = build();
  ok(g.btn && g.btn.hidden === true, "a hidden Comment button exists");
  g.send(SEL);
  ok(g.btn.hidden === false, "a selection shows it");
  ok(g.btn.style.top === "204px" && g.btn.style.left === "30px", "under the selection, in page coordinates");
  g.btn.on.click();
  const quote = g.form.children.find((c) => c.name === "quote");
  const anchor = g.form.children.find((c) => c.name === "anchor");
  ok(quote && quote.value === "the plan", "the quote goes into the form");
  ok(anchor && JSON.parse(anchor.value).kind === "range", "and the anchor");
  ok(g.form.children.find((c) => c.className === "cquote").textContent === "the plan", "the quote is shown, as text");
  ok(g.location.hash === "comments" && g.body.focused, "the drawer opens with the cursor in the box");
  ok(g.btn.hidden === true, "and the button goes"); }
{ const g = build();
  g.send(SEL, { origin: "https://evil.example" });
  g.send(SEL, { source: {} });
  ok(g.btn.hidden === true, "another origin or window cannot show it"); }
{ const g = build();
  g.send(SEL); g.send({ type: "artifact-selection", quote: "" });
  ok(g.btn.hidden === true, "clearing the selection hides it");
  g.send({ ...SEL, rect: { top: "x", left: 1, height: 1 } });
  ok(g.btn.hidden === true, "a malformed rect is ignored"); }

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
