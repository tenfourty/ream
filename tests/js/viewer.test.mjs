// Drive the REAL gallery script against a minimal fake DOM and assert what it
// DOES, not what its source text contains.
//
// Why this file exists: the slice that added the script shipped 18 tests, and
// every one of them was a string assertion on the served bytes -- "the file
// contains `event.origin`" proves nothing about whether the check runs, or
// which way round it is. The repo has no JS runtime in its dev dependencies,
// which is how that gap opened. It needs no dependency: node plus ~40 lines of
// fake DOM is enough to execute the thing and watch what it touches.
//
// Run by tests/test_viewer_js.py, which skips when node is absent, and takes
// the script from the running app rather than a copy -- a copy would drift.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");

let fails = 0, passes = 0;
// Swapped per-case by the clipboard tests below. node's own global `navigator`
// has no `clipboard`, so passing it explicitly is what lets both arms be
// driven -- present and absent -- rather than only the one this runtime has.
let navigatorStub = {};
const ok = (c, m) => { c ? passes++ : (fails++, console.log("FAIL: " + m)); if (c) console.log("ok   " + m); };

function el(attrs = {}, offsetHeight = 88) {
  return { style: {}, parentNode: null, _attrs: attrs, offsetHeight,
           getAttribute(k) { return this._attrs[k] ?? null; } };
}

// `innerWidth` defaults to a desktop width because every case written before
// 21 Sep assumed one implicitly. The script now refuses to lift a control onto
// the frame below 896, so a stub with no innerWidth would silently turn every
// placement case into a narrow-screen case and pass for the wrong reason.
// `media` and `position` are how the two halves of the 22 Sep 2026 fault are
// driven: the stylesheet's breakpoint is in `rem`, so at any root font size
// above 16px it sits at a different pixel width than `innerWidth` -- a fake DOM
// can only show that by letting the media query and the pixel width disagree.
// `position` is what `getComputedStyle` reports for a block once it is on the
// sheet, which is how the script notices a rule un-lifted it anyway.
function build(innerWidth = 1280, { media = null, position = "absolute" } = {}) {
  const iframe = el(); iframe.src = "https://abc.tenfourty.com/";
  const posted = [];
  iframe.contentWindow = { NAME: "frame", postMessage: (m, o) => posted.push([m, o]) };
  const sheet = el(); sheet.appendChild = function (c) { c.parentNode = sheet; };
  const d1 = el({ "data-decision-id": "dec1" });
  // A real original parent, so "put it back where it came from" is observable
  // rather than silently doing nothing against a null.
  const home = el(); home.appendChild = function (c) { c.parentNode = home; };
  d1.parentNode = home;
  const d2 = el({ "data-decision-id": "dec2" });
  const listeners = [];
  const asks = el();
  asks._empty = false;
  asks.querySelector = function () { return this._empty ? null : d1; };
  // --- the menus: two tiles, each with a dots <details> and a link input ----
  const docListeners = {};
  const menus = [0, 1].map(() => {
    const copy = { textContent: "Copy" };
    const menu = { open: false, _inside: [], _copy: null,
      contains(t) { return t === menu || menu._inside.includes(t); },
      querySelector(sel) { return sel === ".mcopy" ? menu._copy : null; } };
    const input = { value: "https://artifacts.tenfourty.site/a/abc",
      _selected: 0, _focused: 0, _on: {},
      addEventListener(t, fn) { this._on[t] = fn; },
      select() { this._selected++; }, focus() { this._focused++; } };
    input.parentNode = { appendChild(b) { menu._copy = b; input._button = b; } };
    menu._inside.push(input);
    menu._copyTemplate = copy;
    return { menu, input };
  });
  // The open rename dialog, or nothing. `el` is null when it is closed, which
  // is what `.dlg:target` returns in a real document.
  const dlg = { el: null, replaced: 0, pushed: 0 };
  const doc = {
    getElementById: (id) => id === "artifact-frame" ? iframe
      : id === "artifact-sheet" ? sheet : id === "open-asks" ? asks : null,
    // Selector-aware, because the script now asks for three different things
    // and a querySelectorAll that ignores its argument would let a wrong
    // selector pass this file untouched -- the exact class of bug it exists
    // to catch.
    querySelectorAll(sel) {
      if (sel === "details.menu[open]") return menus.map((m) => m.menu).filter((m) => m.open);
      if (sel === ".mlink input") return menus.map((m) => m.input);
      return [d1, d2];
    },
    addEventListener(t, fn) { (docListeners[t] ||= []).push(fn); },
    // The rename dialog is a :target, so closing it is a navigation. Only the
    // one selector the script asks for; anything else returns null, so a wrong
    // selector shows up here rather than silently matching.
    querySelector(sel) { return sel === ".dlg:target" ? dlg.el : null; },
    createElement: () => ({ addEventListener(t, fn) { this._on = fn; },
                            textContent: "", type: "", className: "" }),
  };
  const queried = [];
  const win = { innerWidth, addEventListener: (t, fn) => t === "message" && listeners.push(fn),
                getComputedStyle: () => ({ position }) };
  // `media === null` means "this browser has no matchMedia", which is the
  // fallback path; otherwise it is the answer the stylesheet's own query gives.
  if (media !== null) {
    win.matchMedia = (q) => { queried.push(q); return { matches: media }; };
  }
  const history = {
    replaceState() { dlg.replaced++; },
    pushState() { dlg.pushed++; },
  };
  const location = { pathname: "/a/abc", search: "" };
  new Function(
    "document", "window", "URL", "Map", "Set", "isFinite", "navigator",
    "history", "location", src,
  )(doc, win, URL, Map, Set, isFinite, navigatorStub, history, location);
  const send = (e) => listeners.forEach((fn) => fn(e));
  const fire = (t, e) => (docListeners[t] || []).forEach((fn) => fn(e));
  return { iframe, sheet, d1, d2, send, posted, asks, menus, fire, dlg, queried, home };
}

const good = (over = {}) => ({
  source: undefined, origin: "https://abc.tenfourty.com",
  data: { type: "artifact-state", height: 1200, slots: [{ id: "dec1", top: 40, left: 10, width: 300 }] },
  ...over,
});

// 1. happy path
{
  const g = build();
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.iframe.style.height === "1200px", "sizes the frame to the reported height");
  ok(g.d1.parentNode === g.sheet, "moves the marked decision onto the sheet");
  ok(g.d1.style.top === "40px" && g.d1.style.left === "10px" && g.d1.style.width === "300px", "positions it where the page said");
  ok(g.d2.parentNode === null, "leaves an unmarked decision where it was");
}
// 2. wrong source window
{
  const g = build();
  g.send(good({ source: { NAME: "evil" } }));
  ok(g.iframe.style.height === undefined && g.d1.parentNode === g.home, "ignores a message from another window");
}
// 3. wrong origin
{
  const g = build();
  g.send(good({ source: g.iframe.contentWindow, origin: "https://evil.example" }));
  ok(g.iframe.style.height === undefined && g.d1.parentNode === g.home, "ignores a message from another origin");
}
// 4. height clamp
{
  for (const [h, why] of [[10, "absurdly short"], ["900", "a string"], [NaN, "NaN"], [Infinity, "Infinity"]]) {
    const g = build();
    const m = good({ source: g.iframe.contentWindow });
    m.data.height = h;
    g.send(m);
    ok(g.iframe.style.height === undefined, `refuses a height that is ${why}`);
  }
  const g = build(); const m = good({ source: g.iframe.contentWindow });
  m.data.height = 20000; g.send(m);
  ok(g.iframe.style.height === "20000px", "sizes the frame to a tall page");
}
// 4a. A PAGE TALLER THAN THE CLAMP (22 Sep 2026, the operator's board).
// A lifted control is positioned at the slot's PAGE coordinate inside a sheet
// that is overflow:hidden. Refusing an over-tall page's height left the frame
// at its 78vh floor -- a small scrolling window -- so every slot coordinate
// below it pointed outside the box and the control was clipped away, while its
// measured height was still reported and the page reserved a hole for it.
// Jeremy saw a question with an empty void under it and an inner scrollbar on
// the frame, which is the tell: this handler sizes the frame so the OUTER page
// scrolls, so a scrollbar inside it means the height was refused.
{
  const g = build();
  const m = good({ source: g.iframe.contentWindow });
  m.data.height = 250000;
  g.send(m);
  ok(g.iframe.style.height === "100000px", "clamps an over-tall page instead of refusing its height");
  ok(g.d1.parentNode === g.home, "lifts nothing while the frame cannot show the whole page");
  ok(g.posted.length === 1 && JSON.stringify(g.posted[0][0].sizes) === "[]",
     "and reserves no room for a control it did not place");
}
{
  // Just inside the clamp, everything works as before.
  const g = build();
  const m = good({ source: g.iframe.contentWindow });
  m.data.height = 100000;
  g.send(m);
  ok(g.iframe.style.height === "100000px", "accepts the top of the allowed range");
  ok(g.d1.parentNode === g.sheet, "and still lifts there");
}
// 5. unknown / duplicate / malformed slots
{
  const g = build();
  const m = good({ source: g.iframe.contentWindow });
  m.data.slots = [{ id: "nope", top: 1, left: 1, width: 1 },
                  { id: "dec1", top: 40, left: 10, width: 300 },
                  { id: "dec1", top: 999, left: 999, width: 999 },
                  { id: "dec2", top: "x", left: 1, width: 1 }];
  g.send(m);
  ok(g.d1.style.top === "40px", "a repeated slot does not move the control again");
  ok(g.d2.parentNode === null, "a slot with a non-numeric position is ignored");
}
// 6. hostile shapes must not throw
{
  for (const d of [null, "string", 42, [], { type: "other" }, { type: "artifact-state", slots: "nope" },
                   { type: "artifact-state", slots: new Array(65).fill({ id: "dec1", top: 1, left: 1, width: 1 }) }]) {
    const g = build();
    let threw = false;
    try { g.send(good({ source: g.iframe.contentWindow, data: d })); } catch (e) { threw = true; }
    ok(!threw, "survives a hostile payload: " + JSON.stringify(d).slice(0, 40));
  }
}
// 7. the return leg: the page is told how tall the placed control turned out
{
  const g = build();
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.posted.length === 1, "tells the page the size of the control it placed");
  ok(g.posted[0][1] === "https://abc.tenfourty.com", "addresses the size message to the content origin, never '*'");
  ok(g.posted[0][0].type === "artifact-slot-sizes", "labels the size message");
  ok(JSON.stringify(g.posted[0][0].sizes) === '[{"id":"dec1","height":88}]', "reports the measured height for that slot");
  // The page reserves the room, its height changes, and it reports again. The
  // control has not changed size, so nothing new is sent -- otherwise this and
  // the page's ResizeObserver would drive each other forever.
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.posted.length === 1, "does not repeat a size that has not changed");
  g.d1.offsetHeight = 120;
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.posted.length === 2 && g.posted[1][0].sizes[0].height === 120, "reports a size that genuinely changed");
}
// 8. an empty set is still sent, so the page can collapse a slot nothing fills
{
  const g = build();
  const m = good({ source: g.iframe.contentWindow });
  m.data.slots = [{ id: "nope", top: 1, left: 1, width: 1 }];
  g.send(m);
  ok(g.posted.length === 1 && g.posted[0][0].sizes.length === 0,
     "sends an empty set when nothing was placed, so a stale slot collapses");
  g.send(m);
  ok(g.posted.length === 1, "but does not repeat that empty set");
}
// 8b. a slot that stops being filled is reported as gone
{
  const g = build();
  const full = good({ source: g.iframe.contentWindow });
  full.data.slots = [{ id: "dec1", top: 40, left: 10, width: 300 },
                     { id: "dec2", top: 200, left: 10, width: 300 }];
  g.send(full);
  ok(g.posted[0][0].sizes.length === 2, "reports both filled slots");
  const one = good({ source: g.iframe.contentWindow });
  one.data.slots = [{ id: "dec1", top: 40, left: 10, width: 300 }];
  g.send(one);
  ok(g.posted.length === 2 && g.posted[1][0].sizes.length === 1,
     "reports the smaller set when one slot is no longer filled");
}
// 8c. the stranded heading
{
  const g = build();
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.asks.style.display === "", "keeps the open section while a question is still in it");
  g.asks._empty = true;
  g.send(good({ source: g.iframe.contentWindow, data: { type: "artifact-state", height: 1201, slots: [] } }));
  ok(g.asks.style.display === "none", "hides the section once every question has been placed");
}
// 9. an unmeasurable control is not reported
{
  const g = build();
  g.d1.offsetHeight = undefined;
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.d1.parentNode === g.sheet, "still places a control it cannot measure");
  ok(g.posted.length === 1 && g.posted[0][0].sizes.length === 0,
     "but reports no height for it, so the page reserves nothing");
}

// --- the tile menus -------------------------------------------------------
// Everything below is an upgrade to markup that already works with scripts
// off, so what is asserted is that the upgrade behaves -- not that it exists.

// 10. a click outside closes an open menu, and one inside does not
{
  const g = build();
  const [a, b] = g.menus;
  a.menu.open = true;
  g.fire("click", { target: a.input });
  ok(a.menu.open === true, "a click inside a menu leaves it open");
  g.fire("click", { target: b.menu });
  ok(a.menu.open === false, "a click outside closes it");
}
// 11. opening another tile's menu closes the first, without a rule for it
{
  const g = build();
  const [a, b] = g.menus;
  a.menu.open = true; b.menu.open = true;
  g.fire("click", { target: b.menu });
  ok(a.menu.open === false && b.menu.open === true,
     "clicking one tile's dots is a click away from every other tile's");
}
// 12. Escape closes every open menu
{
  const g = build();
  g.menus.forEach((m) => (m.menu.open = true));
  g.fire("keydown", { key: "a" });
  ok(g.menus.every((m) => m.menu.open), "an ordinary key closes nothing");
  g.fire("keydown", { key: "Escape" });
  ok(g.menus.every((m) => !m.menu.open), "Escape closes them all");
}
// 13. the link selects itself on focus, so one gesture copies it by hand
{
  const g = build();
  g.menus[0].input._on.focus();
  ok(g.menus[0].input._selected === 1, "focusing the link selects the whole address");
}
// 14. NO copy button where there is no clipboard
{
  navigatorStub = {};
  const g = build();
  ok(g.menus[0].input._button === undefined,
     "no button on an origin with no clipboard -- one that silently fails is worse than none");
}
// 15. the copy button copies, and says so
{
  let copied = null;
  navigatorStub = { clipboard: { writeText: (v) => { copied = v; return Promise.resolve(); } } };
  const g = build();
  const button = g.menus[0].input._button;
  ok(button !== undefined, "a button where the clipboard exists");
  button._on();
  await Promise.resolve();
  ok(copied === g.menus[0].input.value, "copies the page's address, not something else");
  await Promise.resolve();
  ok(button.textContent === "Copied", "and says so");
  // The label resets when the menu is closed, not on a timer: this script
  // must never contain one (tests/test_gallery.py asserts that), because a
  // timer here is indistinguishable from polling.
  g.menus[0].menu.open = true;
  g.fire("click", { target: g.menus[1].menu });
  ok(button.textContent === "Copy", "the label resets when the menu closes");
}
// 16. a refused clipboard falls back rather than lying
{
  navigatorStub = { clipboard: { writeText: () => Promise.reject(new Error("denied")) } };
  const g = build();
  const input = g.menus[0].input;
  input._button._on();
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  ok(input._selected === 1 && input._focused === 1,
     "selects the text for the reader instead of claiming it copied");
  ok(input._button.textContent === "Copy", "and never says Copied");
}
navigatorStub = {};

// 17. Escape closes the rename dialog WITHOUT leaving a history entry
{
  const g = build();
  g.dlg.el = { id: "rename" };
  g.fire("keydown", { key: "Escape" });
  ok(g.dlg.el.id === "", "Escape closes the dialog");
  ok(g.dlg.replaced === 1 && g.dlg.pushed === 0,
     "by REPLACING the history entry, so Back does not reopen it");
}
// 18. and does nothing at all when no dialog is open
{
  const g = build();
  g.fire("keydown", { key: "Escape" });
  ok(g.dlg.replaced === 0, "no navigation when there is nothing to close");
}
// 19. the dialog needs no script to exist -- this is an upgrade, not a
//     requirement. Asserted in tests/test_gallery.py against the served HTML,
//     because it is a fact about the markup rather than about this file.

// 20. NARROW SCREEN: the control is never lifted onto the frame at all.
//
//     ij8.82, Jeremy on his iPhone 20 Sep: "no feedback, nothing changed it was
//     like my tap wasn't accepted", with the buttons plainly visible. WebKit
//     composites an iframe separately and an overlay above it can lose HIT-
//     TESTING while still painting correctly, so the tap went to the sandboxed
//     page underneath, which does nothing. The first fix asked the compositor
//     for a separate layer (transform:translateZ(0)); it works, and it can only
//     ever be confirmed on a real iPhone, and a later layout change can undo it
//     with every desktop browser still passing.
//
//     This is the structural half, and the reason it is worth having: an
//     overlap that never happens cannot lose a hit-test in ANY engine, which
//     makes it checkable here, in node, with no phone involved.
{
  for (const w of [390, 852, 895]) {
    // `media` is what the stylesheet's own (min-width:56rem) query answers at
    // this width with a 16px root font; the script asks that, not the number.
    const g = build(w, { media: false });
    g.send(good({ source: g.iframe.contentWindow }));
    ok(g.d1.parentNode === g.home, `at ${w}px the control is not moved onto the frame`);
    ok(g.d1.style.top === undefined, `at ${w}px it is not positioned over the frame`);
  }
  const g = build(896);
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.d1.parentNode === g.sheet, "at 896px -- the boundary -- it lifts as before");
}
// 21. and the page is told to reserve nothing, or it holds a gap for a control
//     that is staying exactly where the server rendered it.
{
  const g = build(390);
  g.send(good({ source: g.iframe.contentWindow }));
  const sizes = g.posted.filter(([m]) => m.type === "artifact-slot-sizes");
  ok(sizes.length === 1, "the page is still told, rather than left guessing");
  ok(JSON.stringify(sizes[0][0].sizes) === "[]", "and told to reserve nothing");
  ok(g.iframe.style.height === "1200px", "the frame is still sized -- only the lift is skipped");
}
// 22. the heading above the frame must NOT be hidden when nothing was lifted.
//     Hiding it is right when every question has moved onto the page; here
//     none has, so hiding it would strand the only copy of the control.
{
  const g = build(390);
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.asks.style.display === "", "\"Waiting on you\" stays visible on a phone");
}


// 12. THE BREAKPOINT IS ASKED IN THE STYLESHEET'S UNIT (22 Sep 2026).
// The stylesheet un-lifts below 56rem; the script used to compare 896 PIXELS.
// At a 20px root font size 56rem is 1120px, so a 1000px-wide window lifted a
// control that the stylesheet immediately forced back to position:static --
// the block fell out below the framed page and the size reported for it left a
// control-sized hole where the control should have been. Jeremy read that hole
// as a question with no buttons, twice, on the operator's board.
{
  const g = build(1000, { media: false });
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.d1.parentNode === g.home,
     "does not lift when the stylesheet's own query says narrow, whatever innerWidth says");
  ok(g.posted.length === 1 && JSON.stringify(g.posted[0][0].sizes) === "[]",
     "and reserves no room in the page for a control it did not place");
  ok(g.queried.some((q) => q.indexOf("56rem") !== -1),
     "asks the breakpoint in rem, the unit the stylesheet is written in");
}
{
  // The other direction: a narrow pixel width but a query that says wide (a
  // small root font size). The pixel number must not veto the stylesheet.
  const g = build(400, { media: true });
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.d1.parentNode === g.sheet, "lifts when the stylesheet's query says wide, at any pixel width");
}
{
  // No matchMedia at all: the pixel fallback still works, unchanged.
  const g = build(1280, { media: null });
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.d1.parentNode === g.sheet, "falls back to innerWidth where matchMedia is absent");
  const narrow = build(500, { media: null });
  narrow.send(good({ source: narrow.iframe.contentWindow }));
  ok(narrow.d1.parentNode === narrow.home, "and still refuses below 896 on that fallback");
}
// 13. THE NET UNDER IT: if some rule un-lifts the block anyway, put it back and
// reserve nothing. A visible control in the wrong place beats an invisible one,
// and a reserved hole with nothing in it is worse than both.
{
  const g = build(1280, { media: true, position: "static" });
  g.send(good({ source: g.iframe.contentWindow }));
  ok(g.d1.parentNode === g.home, "returns a block the stylesheet un-positioned to where it came from");
  ok(g.d1.style.top === "" && g.d1.style.left === "" && g.d1.style.width === "",
     "and drops the inline position it had been given");
  ok(g.posted.length === 1 && JSON.stringify(g.posted[0][0].sizes) === "[]",
     "and tells the page to reserve nothing for it");
}

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
