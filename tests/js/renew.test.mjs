// Drive the REAL gallery script's "this frame's pass is spent" recovery
// against a minimal fake DOM.
//
// homelab-ij8.101. A page's frame opens with a pass that works once, and
// browsers re-request a frame's last URL on reload (Firefox) and back/forward
// (Chrome). The frame then says `artifact-expired` and the gallery must fetch
// a fresh pass and re-point it. What matters: it only listens to its own
// frame, never points the frame at another origin, and cannot loop.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");

let fails = 0, passes = 0;
const ok = (c, m) => { c ? passes++ : (fails++, console.log("FAIL: " + m)); if (c) console.log("ok   " + m); };
const tick = () => new Promise((r) => setImmediate(r));
const ORIGIN = "https://abc.tenfourty.com";

function build() {
  const frameCalls = [];
  let reply = { src: ORIGIN + "/p/fresh/" };
  const listeners = {};
  const sheet = { getAttribute: () => null, style: {} };
  const iframe = { src: ORIGIN + "/p/first/", style: {},
                   contentWindow: { postMessage() {} }, getAttribute: () => null };
  const doc = {
    hidden: true,  // keeps the republish watch quiet; this file is about the frame
    body: { appendChild() {}, removeChild() {} },
    getElementById: (id) => (id === "artifact-frame" ? iframe : id === "artifact-sheet" ? sheet : null),
    querySelectorAll: () => [], querySelector: () => null, addEventListener() {},
    createElement: () => ({ style: {}, appendChild() {}, addEventListener() {} }),
  };
  const win = {
    innerWidth: 1280,
    addEventListener: (t, fn) => (listeners[t] ||= []).push(fn),
    fetch(url) {
      if (url.endsWith("/frame")) frameCalls.push(url);
      const r = reply;
      return Promise.resolve({ ok: r !== null, json: () => Promise.resolve(r) });
    },
  };
  new Function("document", "window", "URL", "Map", "Set", "isFinite", "navigator",
               "history", "location", src)(
    doc, win, URL, Map, Set, isFinite, {}, { replaceState() {} },
    { pathname: "/a/abc", search: "", reload() {} });
  const send = async (data, { source = iframe.contentWindow, origin = ORIGIN } = {}) => {
    (listeners.message || []).forEach((f) => f({ source, origin, data }));
    await tick(); await tick(); await tick();
  };
  return { iframe, frameCalls, send, setReply: (r) => { reply = r; } };
}

// 1. a spent pass is replaced by a fresh one
{
  const g = build();
  await g.send({ type: "artifact-expired" });
  ok(g.frameCalls.length === 1 && g.frameCalls[0] === "/a/abc/frame",
     "asks its own gallery route for a fresh pass, once");
  ok(g.iframe.src === ORIGIN + "/p/fresh/", "and re-points the frame at it");
}
// 2. only its own frame, on its own origin, may ask
{
  const g = build();
  await g.send({ type: "artifact-expired" }, { source: {} });
  await g.send({ type: "artifact-expired" }, { origin: "https://evil.example" });
  ok(g.frameCalls.length === 0, "another window or origin cannot make it fetch");
}
// 3. never points the frame somewhere else, whatever the route returns
{
  const g = build();
  g.setReply({ src: "https://evil.example/p/x/" });
  await g.send({ type: "artifact-expired" });
  ok(g.iframe.src === ORIGIN + "/p/first/", "a src on another origin is ignored");
  g.setReply({ src: 42 });
  await g.send({ type: "artifact-expired" });
  ok(g.iframe.src === ORIGIN + "/p/first/", "a src that is not a string is ignored");
}
// 4. it cannot loop against the server
{
  const g = build();
  g.setReply(null);  // e.g. the page was deleted: 404 every time
  for (let i = 0; i < 6; i++) await g.send({ type: "artifact-expired" });
  ok(g.frameCalls.length === 3, "gives up after three in a row");
}
// 5. a frame that loaded properly resets the count
{
  const g = build();
  for (let i = 0; i < 3; i++) await g.send({ type: "artifact-expired" });
  await g.send({ type: "artifact-state", height: 900, slots: [] });
  await g.send({ type: "artifact-expired" });
  ok(g.frameCalls.length === 4, "a real state message means the next expiry is a fresh start");
}

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
