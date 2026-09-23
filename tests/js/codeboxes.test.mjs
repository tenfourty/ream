// Drive the REAL connect-page script (one box per character) against a
// minimal fake DOM. homelab-ij8.102 follow-up, Jeremy 23 Sep 2026: auto-advance,
// accept a paste, and a full code continues on its own (his choice (a)).
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");
let fails = 0, passes = 0;
const ok = (c, m) => { c ? passes++ : (fails++, console.log("FAIL: " + m)); if (c) console.log("ok   " + m); };

function el(tag) {
  return { tag, value: "", type: "text", children: [], attrs: {}, on: {}, className: "",
           setAttribute(k, v) { this.attrs[k] = v; }, addEventListener(t, f) { this.on[t] = f; },
           appendChild(c) { this.children.push(c); }, focus() { doc.activeElement = this; }, select() {} };
}
let doc;
function build(prefill = "") {
  const single = el("input"); single.value = prefill;
  const parent = { inserted: null, insertBefore(n) { this.inserted = n; } };
  single.parentNode = parent;
  const form = { submits: 0, submit() { this.submits++; }, querySelector: () => single };
  doc = { activeElement: null, querySelector: (q) => (q === "form.code-form" ? form : null),
          createElement: el };
  new Function("document", "window", "navigator", src)(doc, {}, {});
  const boxes = parent.inserted.children.filter((c) => c.tag === "input");
  const type = (i, ch) => { boxes[i].value = ch; boxes[i].on.input(); };
  const key = (i, k) => { let prevented = false; boxes[i].on.keydown({ key: k, preventDefault() { prevented = true; } }); return prevented; };
  const paste = (i, text) => boxes[i].on.paste({ clipboardData: { getData: () => text }, preventDefault() {} });
  return { single, form, boxes, type, key, paste, wrap: parent.inserted };
}

{ const g = build();
  ok(g.boxes.length === 8, "eight boxes");
  ok(g.single.type === "hidden", "the original field stays, hidden, carrying the value");
  ok(g.wrap.children.some((c) => c.className === "dash"), "a gap where the hyphen goes");
  ok(doc.activeElement === g.boxes[0], "the first box has the cursor"); }
{ const g = build();
  g.type(0, "a");
  ok(g.boxes[0].value === "A", "typing is upper-cased");
  ok(doc.activeElement === g.boxes[1], "and moves to the next box");
  g.type(1, "-");
  ok(g.boxes[1].value === "" && doc.activeElement === g.boxes[1], "a stray symbol is dropped and does not advance"); }
{ const g = build();
  g.type(0, "A"); g.type(1, "B");
  g.boxes[2].focus();
  ok(g.key(2, "Backspace") && g.boxes[1].value === "" && doc.activeElement === g.boxes[1],
     "backspace in an empty box clears and goes back one"); }
{ const g = build();
  g.paste(3, "dpkg-uwqm");
  ok(g.boxes.map((b) => b.value).join("") === "DPKGUWQM", "a paste fills all eight, from any box");
  ok(g.single.value === "DPKG-UWQM", "the field the server reads gets the hyphenated code");
  ok(g.form.submits === 1, "a full code continues on its own");
  g.type(7, "M");
  ok(g.form.submits === 1, "and only once"); }
{ const g = build();
  g.paste(0, "dp kg");
  ok(g.form.submits === 0 && g.single.value === "DPKG", "a partial paste fills what it has and waits"); }
{ const g = build("DPKG-UWQM");
  ok(g.form.submits === 0, "a code already on the page (after 'no machine waiting') does not resubmit itself");
  ok(g.boxes[7].value === "M", "and is shown in the boxes"); }

// the copy button on the starter prompt
{ const written = [];
  const promptEl = el("pre"); promptEl.textContent = "Connect this machine...";
  const holder = { children: [], insertBefore(n) { this.children.push(n); } };
  promptEl.parentNode = holder;
  const d = { querySelector: (q) => (q === "pre.prompt" ? promptEl : null), createElement: el };
  const nav = { clipboard: { writeText: (t) => { written.push(t); return Promise.resolve(); } } };
  new Function("document", "window", "navigator", "setTimeout", src)(d, {}, nav, () => {});
  const button = holder.children[0];
  ok(button && button.textContent === "Copy", "a Copy button sits under the prompt");
  button.on.click();
  await new Promise((r) => setImmediate(r));
  ok(written[0] === "Connect this machine...", "it copies the prompt's exact text");
  ok(button.textContent === "Copied", "and says so"); }
{ const promptEl = el("pre");
  const holder = { children: [], insertBefore(n) { this.children.push(n); } };
  promptEl.parentNode = holder;
  const d = { querySelector: (q) => (q === "pre.prompt" ? promptEl : null), createElement: el };
  new Function("document", "window", "navigator", src)(d, {}, {});
  ok(holder.children.length === 0, "no clipboard (an old or insecure browser): no button that cannot work"); }

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
