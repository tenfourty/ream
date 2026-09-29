# The page view like claude.ai — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- The agent's page fills the window edge to edge, under one slim bar.
- Comments open instantly in a panel docked beside the page. There is no dimming, no blur and no navigation.
- Hovering over marked text shows that comment right there.

**Architecture:** Most of the claude.ai shape already exists. `detail()` emits a sticky 48px bleed bar with a home icon, a title menu, a comments button and the profile. What makes the page feel awkward is three things:
- `.wrap{max-width:62rem;padding:3rem …}` plus the bordered, rounded `.sheet` card;
- the `.cdrawer`, a `:target` overlay whose `.dlg-back` carries `backdrop-filter:blur(2px)` over a very tall page, and is opened by navigating to `#comments`;
- no hover.

This plan changes those three things and keeps the scripts-off behaviour working.

**Tech Stack:**
- server-rendered HTML and CSS in `gallery.py` (`_CSS`, `_shell`, `detail`, `_render_comments`);
- inline scripts `_GALLERY_JS`, which has a size budget of < 8192 bytes, and `_COMMENTS_JS`;
- the page shim in `content.py`, which runs on the agent page's own origin inside a sandboxed iframe.

**Spec:** homelab-ij8.113, Jeremy's confirmed decision "Aa Ba Ca Da" of 29 Sep 2026, together with his claude.ai screenshot: one slim white bar with a home icon, the page title with a dropdown, then on the right comments (with a dropdown), share, and a full-screen toggle. The page sits below the bar, edge to edge, with no frame.

## Global Constraints

- **Full width.** On the page view only, the agent's page takes the full window width. It has no border, radius or shadow, and no outer padding: the `.sheet` is flush under the bar. Wall and list views keep their current layout.
- **Questions and review cards** stay at the top of the page, in a centred column of at most 52rem. The same goes for the settled history below the page. The existing lift-onto-sheet at ≥ 56rem (`gallery.js`, decision blocks positioned from slot rects) keeps working against the full-width sheet.
- **The bar** is the existing sticky bleed bar. In the claude.ai order, left to right: home icon · title menu (the existing `_page_menu`) … right side: comments button · **share** (a direct button that opens the existing share route; it no longer lives only in the menu) · **full-screen toggle** · profile. The full-screen toggle calls the Fullscreen API on the sheet. Its label is `Full screen` / `Exit full screen`. It is hidden when scripts are off or the API is missing.
- **The comments panel:**
  - It is docked on the right, `min(24rem, 100%)` wide. When it is open, the page area narrows by that width: the `.sheet` and the iframe reflow, and nothing covers them.
  - There is no backdrop, no dimming and no blur anywhere on this page. The `.dlg-back` blur must not apply to the comments panel.
  - The comments button, the selection bar's buttons and a hover card's "Open" all open or close the panel by toggling a class. They do not navigate: no `location.hash` change and no history entry.
  - Escape closes it.
  - Its open or closed state is remembered per viewer in `localStorage`, key `artifacts.comments.open`, wrapped in try/catch.
  - Below 34rem the panel is a bottom sheet, 70vh tall, sliding up over the page. This is the one place where it may cover the page, and it still has no blur.
- **Scripts off:** the comments button stays an `href="#comments"` link. The `:target` rule shows the panel docked (not as an overlay), and it has no blur either.
- **Hover cards:**
  - When the pointer rests on painted marked text for 150 ms, a small card appears beside it. The card shows the mark's kind label, the note, the replies, and buttons `Reply` and `Open`. `Reply` focuses that thread's reply box inside the card. `Open` opens the panel scrolled to the thread.
  - On touch, a tap on marked text does the same.
  - The card is drawn by the gallery, never inside the agent's page. The shim sends the gallery only `{type:"artifact-mark-hover", id, rect}` and `{type:"artifact-mark-leave"}`. It never sends text.
  - The gallery accepts the messages only from `iframe.contentWindow` at the content origin. It checks that `id` is one of this page's mark ids, clamps the rect, and renders the card from the thread markup it already holds for the panel.
  - The card closes on leave (with a 250 ms grace period, so the pointer can move into it), on Escape, or on scroll.
- **Held marks** (dotted underline) show their card too, tagged `held`.
- **The size budget holds.** `gallery.js` stays under 8192 bytes. New behaviour goes into `comments.js`.
- **Commit rules:** conventional commits and no Co-Authored-By or "Generated with" lines. Stage explicit paths. No amend, stash or reset --hard.

## Review Focus

1. **The lifted decision blocks land in the wrong place** once the sheet is full width, or when the panel narrows it. *Test (Task 1):* the viewer JS harness at a full-width sheet, and again with the panel open. Block positions follow the sheet's new width.
2. **The panel covers the page, or leaves the iframe's height stale** when it opens. *Test (Task 2):* in a real headless browser against a served gallery (the reviewer's `browser_check.py` in the scratchpad is the model), the iframe's width shrinks by the panel width, no element overlaps it, and `location.href` is unchanged.
3. **A crafted hover message from the agent's page** (wrong origin, an unknown id, a huge or negative rect, a flood of messages) moves or shows a card it shouldn't. *Test (Task 3):* harness cases for each.
4. **The hover card leaks a note into the frame,** or text passes through `postMessage` to the frame. *Test (Task 3):* the shim's outgoing messages carry only `type`, `id` and `rect`. The gallery never posts note text to the frame.
5. **Scripts off loses comments.** *Test (Task 2):* the page HTML without scripts still has a working `#comments` panel. A test asserts on the served CSS: the `:target` rule exists and has no `backdrop-filter`.

---

### Task 1: the page fills the window, and the bar gets share and full screen

**Files:**
- Modify: `src/artifacts/gallery.py`:
  - `_shell` gains a `full=True` option, used only by `detail()`. It drops the `.wrap` max-width and padding around the sheet, and puts the asks and settled blocks inside a `.col` with `max-width:52rem;margin:0 auto;padding:0 1rem`.
  - `.sheet` on the page view has no border, radius or shadow, and a full-width iframe.
  - The bar gets a Share button (`href` to the existing share route or dialog) and a `button.fsbtn` hidden by default. Script reveals it when `document.fullscreenEnabled` is true.
- Modify: `_GALLERY_JS`, within budget. Wire the full-screen toggle, or put it in `comments.js` if the budget is tight. Make sure decision-block lifting reads the sheet's current width.
- Tests: update the structural tests the map listed (`test_archive_rename.py:265-288`, `test_gallery.py` sheet/56rem, `test_gallery_tiles.py`, `tests/js/viewer.test.mjs`). Add tests for the full-width CSS on the page view only, and for the bar order (home, title, …, comments, share, full screen, profile).

- [ ] Step 1: failing tests. Step 2: watch them fail. Step 3: implement. Step 4: `uv run pytest -q -n auto --maxprocesses 8`, then ruff. Step 5: commit `feat(gallery): the page fills the window under one slim bar with share and full screen`.

### Task 2: comments in a docked panel, opened without navigating

**Files:**
- Modify: `src/artifacts/gallery.py`, in `_CSS`. Add a `.cpanel` docked layout:
  - `body.cpanel-open .pagearea{margin-right:min(24rem,100%)}`, or an equivalent grid;
  - the panel `position:fixed; top:48px; right:0; bottom:0`;
  - no `.dlg-back` on this panel;
  - `:target` shows it docked as the scripts-off path;
  - the bottom sheet below 34rem.
- Modify: `_render_comments`, which keeps `id="comments"` and drops the backdrop link. Close is a button when scripts run, and a link to `#` otherwise.
- Modify: `_COMMENTS_JS`:
  - intercept clicks on `.cbtn` and the close control, and toggle `body.cpanel-open`, with no hash change;
  - replace the selection bar's `location.hash="comments"` with the open-panel call;
  - add Escape to close, and `localStorage` memory;
  - update anything that selects `#comments .cdrawer-card`, keeping in-place posting working;
  - after open or close, ask the frame to re-report its height, or re-send marks if the reflow needs it.
- Tests: update `test_page_comments.py`, `test_instant_post.py`, `tests/js/comments.test.mjs` and `test_shim_marks.py` where they rely on `.cdrawer`, `.dlg-back` or the hash. Add harness cases for the toggle, Escape and memory. Add a real-browser check (a test using `ARTIFACTS_BROWSER`, following the existing browser tests) for Review Focus 2.

- [ ] Steps 1–5 as above. Commit `feat(gallery): comments open beside the page at once, no blur and no navigation`.

### Task 3: hover over marked text to see its comment

**Files:**
- Modify: `src/artifacts/content.py`, the shim. On `pointermove` (throttled to one check per animation frame) and on `pointerdown` for touch, find which painted mark range contains the point. Use the ranges it already registered with `CSS.highlights`, via `range.getClientRects()`. Post `{type:"artifact-mark-hover", id, rect}` to the parent on entering and `{type:"artifact-mark-leave"}` on leaving. It never posts text.
- Modify: `_COMMENTS_JS`. Validate the message (source, origin, known id, clamped rect). After the 150 ms rest, clone that thread's markup from the panel into a `.hovercard` positioned beside the rect, offset by the iframe's position. Wire `Reply` and `Open`, close on leave (with the grace period), Escape or scroll, and throttle.
- Modify: `_CSS` for `.hovercard`: card styling from existing tokens, max-width 22rem, dark-mode safe.
- Tests: the shim node harness (`tests/js/marks.test.mjs`) for the messages it posts: only type, id and rect, and no text. The comments harness for validation and the timing. Server tests that the panel markup carries a per-thread `data-mark-id` for cloning.

- [ ] Steps 1–5 as above. Commit `feat(gallery): hover over marked text to see its comment`.

The controller merges, pushes, deploys and proves it:
- a real headless browser against the live gallery is not possible, because it sits behind Authelia;
- so the proof is the reviewer's local served-gallery browser check, and Jeremy tries it.

## Self-review

- **Coverage of ij8.113:**
  - the docked panel with no blur, no navigation, and a bottom sheet on phone: Task 2;
  - hover cards drawn by the gallery: Task 3;
  - the slim bar with share and full screen: Task 1;
  - full width with no frame: Task 1;
  - asks at the top: Task 1's `.col`.
- **Placeholders:** the layout is specified by behaviour, classes and tests, because it must fit the existing `_shell`/`detail` structure the map describes.
- **Names:** `.pagearea`, `.col`, `.cpanel-open`, `.hovercard`, `artifact-mark-hover`, `artifact-mark-leave` and `artifacts.comments.open` are used consistently.
