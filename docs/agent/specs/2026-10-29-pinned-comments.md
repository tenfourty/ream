# Comments pinned to a spot, with images attached: design (29 Oct 2026)

Status: proposed. The reader-facing points are open as "Choices for Jeremy" at the end; nothing
here is ruled yet. Feedback it argues from: `docs/agent/feedback/2026-10-28-listening-and-pinned-comments.md`
(eleven offices; the table there is the source for every "the offices said" below).

## 1. Problem

A comment can point at a passage today, but not at a chart, an image or a table, and it cannot
carry a screenshot. When a republish changes the page, a quoted comment only learns that its words
are "changed", and an agent reading it from the CLI sees a quote with no sense of where on the page
it was. The offices asked for four things:

- **A spot an agent can find again.** Quote plus nearest heading first, an element id when the page
  has one. A CSS path alone breaks on generated pages (gg-ops, retrobrew, homelab).
- **Images an agent can open.** A local file path, PNG/JPEG/WebP, about 5 MB and 4000 px a side.
- **A spot that vanishes stays visible.** The comment stays where it was, marked "outdated", with its
  quote, and is also listed at page level. None want a comment moved silently.
- **Spot screenshots are optional.** Wanted only for layout and charts.

### What already exists (read from the code, so this spec does not rebuild it)

| Piece | Where | Today |
|---|---|---|
| Anchor kinds | `comments.validate_anchor` | `page`, `point` (x, y fractions; no UI makes one), `range`, `decision` |
| Selection pin | `content.py` shim, `_COMMENTS_JS` in `gallery.py` | Selecting text posts `artifact-selection` (quote up to 500 chars, `range` anchor: CSS `path`, `start`/`end` offsets, 32 chars of `prefix`/`suffix`); the gallery shows a Comment bar and posts the form to `POST /c/{artifact_id}` |
| Painting | shim `place`/`paint` | Stored path and offsets first, accepted only if the text there still matches the quote; else a whitespace/case-folded search of visible text with prefix/suffix scoring; ambiguous or absent means unplaced. The shim reports unplaced ids; the drawer prints one count line |
| Agent quote | `api._text_anchor`, `resolve_text_anchor` | `{"kind":"text","quote","section"}` is found on the current version (exactly once, else 422) and **stored as `{"kind":"page"}` plus the quote**, so the gallery cannot paint it |
| Staleness | `comments.quote_state`, `carried_state` | `quote_state` is `current` or `changed`, computed on read, not stored. `carried_state` already tells "text changed" (prefix, suffix or half the quote survives) from "addressed/removed" |
| CLI | `decide.py` | `comment PAGE TEXT [--quote T] [--section H]`, `comments`, `reply`; `_fmt_thread` prints `on: "quote"` |
| Page files | `file` table | Per-version bytes, served from the content origin. Not for comment attachments (see 6) |

Two facts shape the design. First, the gallery cannot see inside the frame; everything about the
page comes through `postMessage` from the shim, to the gallery origin only. Second, `author="jeremy"`
is hard-coded in `gallery.add_comment`; section 4 says what that means for many readers.

## 2. Principles

1. **One anchor model for every kind of pin**, because items after this one (in-page forms,
   change notes, editable diagrams) will also need "which part of the page". The anchor is data
   (JSON in the existing `comment.anchor` column); no new column.
2. **A pin carries evidence, not only coordinates**: the quote (or an element's text excerpt), the
   nearest heading, an element id when there is one. A CSS path is a fast path, verified before use,
   never trusted.
3. **Never move a pin onto text it was not about.** A pin is re-found only by evidence that is
   checked (id, quote), never by position.
4. **The page's DOM is not changed** (the shim's standing rule). The hover outline and the pin
   badges are drawn by the gallery over the frame from rectangles the shim reports.
5. **Every human action has an agent twin**, scoped to the calling key; "not yours" answers
   exactly like "no such page".

## 3. Anchor model

Anchors stay JSON on `comment.anchor`. Every key set is exact, and every failure is a bare
`InvalidAnchor` (no oracle, as `validate_anchor` says today).

| `kind` | Made by | Keys |
|---|---|---|
| `range` (exists) | reader, selecting text | `path`, `start`, `end`, optional `prefix`, `suffix`; **new optional** `heading`, `heading_id`, `el_id` |
| `element` (new, **built, slice 2**) | reader, picking a spot; agent, by id | at least one of `id`, `heading`, or a non-empty `quote` (the quote is the comment's own column); also optional `tag`, `path`, `ordinal`, `label`, `row`, `column`, `prefix`, `suffix` |
| `quote` (new) | agent, quoting text | `prefix`, `suffix` optional, `heading`, `heading_id` optional; no DOM path |
| `page`, `point`, `decision` | unchanged | unchanged |

Field meanings and caps (all strings are UTF-8 round-trip checked by `_reject_lone_surrogate`):

- `heading`: text of the nearest preceding `h1`-`h6` in document order, whitespace-collapsed,
  at most 200 characters. `heading_id`: that heading's `id`, or the `id` of the nearest enclosing
  `section`/`article`/`table`/`figure` that has one, at most 200 characters.
- `el_id` / `id`: the `id` of the pinned element (for `range`, of its nearest id-bearing ancestor),
  at most 200 characters, charset `[A-Za-z0-9_.:-]` (HTML ids may be wilder; an id outside the set
  is simply not recorded).
- `tag`: lower-case tag name. `ordinal`: n-th element with that tag under the heading, 1-based,
  for an `img`/`svg`/`canvas` with no id and no text. `path`: as for `range`, a fast path only.
- The `comment.quote` column carries the evidence text: for `range` the selection (as now, 500
  characters), for `element` the element's visible text (first 200 characters) or, with none,
  its `alt`/`aria-label`/`title`/caption. So the CLI, the nudge, `quote_state` and the
  review-carry code keep working unchanged on every pin.
- `remove` and `looks_good` stay `range`-only with a quote (unchanged rule).

**Agents.** `{"kind":"text","quote","section"}` is kept as the request shape and still resolves
exactly-once, but is now stored as a `quote` anchor with the heading the server found, so the
gallery can paint it. Existing agent comments stored as `page` plus quote stay as they are. New
request shape `{"kind":"element","id":"cost-table"}`: the id must occur exactly once on the
current version (`sections.locate` already finds an id and refuses an ambiguous one); the server
fills `tag`, `heading`, `heading_id` and the quote from the body. A missing or duplicated id is a
422 with the same `{"field":"anchor","detail"}` shape the text anchor uses.

### Re-anchoring after a republish

A pin keeps the `version_n` it was made on and is never rewritten. Where it sits now is read, not
stored (as `quote_state` is), by a new pure module `anchors.py` that takes the current body once
per request and returns one of:

- `current`: the evidence is found where the pin says it is.
- `edited`: the spot is still there but its words changed.
- `outdated`: the spot is gone.
- `""`: unknown (a page too large to search, or an element pin with only a tag and ordinal,
  which only the browser can count).

Order of evidence, first hit wins, each step checked:

1. **Element id.** `id` / `el_id` present exactly once on the page. If the pin has a quote and the
   element's text still holds it: `current`. If the text no longer holds it: `edited` (the id says
   it is the same section). Absent or duplicated: next step.
2. **Quote under its heading.** The heading is found by `heading_id`, then by `heading` text
   (`comments._section_slices`). The quote (case and spacing folded, as `resolve_text_anchor`)
   found under it: `current`. Found only elsewhere on the page: `current` with a note
   `moved under "<heading>"`, so a section rename or reorder does not outdate everything.
3. **Quote anywhere.** As step 2's second half, for pins with no heading.
4. **Edit in place.** Quote absent but `prefix`, `suffix` or half the quote survives: `edited`
   (the existing `carried_state` rule, reused as is).
5. **Approximate, in its own section** (added after slice 2; see "As built: approximate
   re-find"). Otherwise `outdated`.

`quote_state` stays on every comment unchanged (`current`/`changed`); `anchor_state` and
`anchor_note` are new fields beside it. The gallery paints from the shim's own placement (which
follows the same steps 2-4 in the browser); a pin the shim cannot place and the server calls
`current` is shown as placed-in-list, unpainted, with the existing "could not be placed" line.
The list state is the server's; the painted state is the browser's; each is labelled for what it
is. `_with_quote_state` becomes `_with_anchor_state`, still one read of the page for the whole
listing; a page over the existing search limits (`text.INPUT_LIMIT`, `SECTION_PAGE_MAX`) yields
`""` for all, like today's `cut` case.

## 4. What the reader does and sees

The frame stays as is; the comments panel gains a pin mode, a picker and an attach control.

- **Pin to a spot.** A "Pin" button in the page bar next to Comment turns pin mode on. In pin
  mode the shim stops page clicks (capture phase, `preventDefault`, only while the mode is on),
  and as the pointer moves reports the candidate element's rectangle; the gallery draws an outline
  over the frame. The candidate is the smallest block-level element under the pointer that is not
  a whole-page wrapper, preferring one with an `id`; the up arrow widens to the parent, down
  narrows back. A click reports `artifact-pin` (anchor, evidence text, rectangle); the gallery
  opens the panel with the spot quoted, exactly as for a text selection. Escape or the button again
  leaves the mode. Selecting text still works without the mode and is unchanged. (Changed 5 Nov 2026: a text selection made while the mode is on now leaves the mode and shows the selection bar; until then it was silently ignored, which left Jeremy unable to comment.)
- **Pins on the page.** Text pins keep the highlight. Element pins show a numbered badge at the
  element's corner, drawn by the gallery from rectangles the shim reports in
  `artifact-marks-placed` (extended with `rects`, capped like `unplaced`). Hover or focus on a
  thread outlines its spot.
- **In the thread.** Each pinned comment shows its evidence and where it was:
  `on "the quote" · under Costs · v3`. When `anchor_state` is `edited`, a chip "text edited since
  v3"; when `outdated`, a chip "outdated: this spot is gone from the current version" and the
  quote stays shown. An outdated comment is never hidden, never resolved by itself and never
  re-pinned: the pin is simply not drawn.
- **Page level.** The panel header gains "Outdated pins (n)" when n is above zero: a list of
  those threads (quote, heading, version made on, link to that version), always at the top so
  none is missed. The thread also stays in its normal place. The count line the shim drives
  ("n marks could not be placed") stays, for placement failures that are not republishes.
- **Attach images.** Beside the comment box: paste, drop or choose files, up to four per comment.
  A thumbnail row shows what is attached, each removable before sending. The browser shrinks a
  picture over the limits (4000 px a side, 5 MB) before sending when it can; the server is the
  authority and refuses what is still over. A one-line note under the control: "Images are not
  checked for secrets." In a thread an image is a thumbnail that opens larger in the panel.
- **Many readers on one page.** Pinning is available to anyone the page is shared with, because
  it is part of commenting, and a pin is data of one comment, not of a user: two readers pinning
  the same spot make two comments with two anchors. I could not find the "Many readers on one
  page" text itself in this repository or in runcove's docs by search, so this spec holds only to
  the brief: no assumption of a single reader. Concretely: staged images are owned by the
  *author string* of the writer (today the gallery hard-codes `jeremy`; when a reader identity
  arrives, the same value is used), quotas and rate limits are per author, and every image is
  served on the page's own access: whoever may read the page may see its comments' images, nobody
  else. The reader identity itself is not designed here.

## 5. Shim and gallery changes

- `content.py` selection leg: add `heading`, `heading_id`, `el_id` to the `range` anchor it posts
  (nearest preceding heading by walking previous siblings and parents; ids as in section 3).
- New shim messages, still posting only to the gallery origin, still behind the origin check:
  from the gallery `artifact-pin-mode` `{on}`; to the gallery `artifact-pin-hover` `{rect, tag}`
  and `artifact-pin` `{anchor, quote, rect}`. Evidence sent is capped (quote 200, heading 200,
  ids 200); nothing else from the page leaves it.
- `place()` accepts `quote` anchors (no `path`): the `typeof m.anchor.path==="string"` guard in
  `paint` is relaxed to "quote non-empty or element id present". Element pins are placed by
  `id`, then heading plus tag plus ordinal, then quote search; the DOM is not touched.
- `_COMMENTS_JS` (the gallery's own file): pin button, outline overlay, badges, attach control,
  outdated section. Everything the page sends goes in via `textContent`, as the file's rule says.
- Tests for the shim: `tests/js/marks.test.mjs` already runs the shim source against a fake DOM;
  new cases there. Unchanged rule: no browser test is run on this machine; CI runs them.

## 6. Images: upload, storage, serving

### 6.1 Limits (checked in this order, each before the next costs anything)

- At most **4 per comment**, **40 per page**, **5 MB each** (5 x 1024 x 1024 bytes), **4000 px a
  side**, **16 megapixels**. Reading the request body stops at 5 MB plus one byte: a larger upload
  is refused without being held in memory. Staged and unattached images count per author: 12 at a
  time, swept after one hour.
- Formats: PNG, JPEG, WebP only. The type is decided from the **bytes**, never from the client's
  `Content-Type` or file name (the name is not stored). GIF, SVG, HEIC, AVIF, TIFF, animated WebP
  and APNG are refused with a named reason ("HEIC is not accepted: send PNG, JPEG or WebP").
- Refusals are 413 (size) or 422 with `{"field":"image","detail"}` and never echo content.

### 6.2 Sniffing, dimensions and stripping, without a decoder

The server never decodes pixels, so a decompression bomb costs it nothing: a small parser in a new
`images.py` reads the magic bytes and the header (PNG `IHDR`, JPEG `SOF`, WebP `VP8`/`VP8L`/`VP8X`)
for width and height, refuses a file whose declared size breaks the caps, and **rewrites the file
keeping only the chunks that draw the picture**: PNG `IHDR PLTE IDAT IEND tRNS` plus `sRGB`/`gAMA`/
`cHRM`/`iCCP`; JPEG everything except `APP1` (EXIF, XMP), `APP13` and `COM`; WebP drops `EXIF`
and `XMP `. Camera EXIF (GPS, device) and text chunks never reach storage. A file the parser cannot
walk to its end is refused. No new dependency (no Pillow), and the stored bytes are the rewritten
ones, with `sha256` of those. The browser's own re-encode (the shrink in section 4) is a
convenience, not a defence.

### 6.3 Secret scanning

Pixels cannot be scanned by `scan.py` or `ggscan.py` (both read text), and OCR is out of scope. What
holds: the comment's own note is scanned as today, whatever images it carries; text-bearing
metadata is dropped in 6.2 rather than scanned; and the gallery says "Images are not checked for
secrets" at the point of attaching. This is a stated gap, not a closed one: a screenshot of a
terminal can show a token. It is raised in the choices for Jeremy only as the reminder wording.

### 6.4 Storage

New table `comment_image`, in `schema.sql` and `schema_sqlite.sql` with the same columns (SQLite
`blob`/text timestamps as the neighbouring tables use). Only `db.py` talks to a driver; the `file`
table already carries `bytes` through both, so no driver change is expected.

| Column | Meaning |
|---|---|
| `id` | 26-char base32 (`ids`), unguessable |
| `artifact_id` | `references artifact(id) on delete cascade` (deleting a page deletes its images) |
| `comment_id` | nullable until attached; `references comment(id) on delete cascade` |
| `position` | 1-4 within the comment; unique with `comment_id` |
| `staged_by` | author string of the uploader, while unattached |
| `content_type` | `image/png`, `image/jpeg`, `image/webp`; the sniffed one |
| `width`, `height`, `byte_size`, `sha256` | from the parser |
| `bytes` | the rewritten file (`storage external` on Postgres: already compressed) |
| `created_at` | for the sweep |

Not in the `file` table (that is per page version and served as page content) and not through
`packing.py` (that is zstd for page bodies; images are already compressed). `maintenance.py` gains
the hourly sweep of staged rows. A withdrawn or held comment (the GitGuardian withdrawal path, and
Jeremy's held review) hides its images with it: an image is served only if its comment would be.

### 6.5 Upload and attach

Two steps, the same for the gallery and for agents, so the comment body stays JSON and keeps its
idempotency:

1. `POST /api/v1/artifacts/{id}/comment-images` (multipart, one file, key-scoped) and the
   gallery twin `POST /c/{artifact_id}/image` (same-origin guard). Returns `{image_id, content_type,
   width, height, byte_size}`. The same bytes from the same author return the same id.
2. The comment body gains `images: [image_id, ...]` (API) / repeated `image` fields (gallery form).
   Attaching an id that is unknown, someone else's, already attached or staged on another page is a
   422 that does not say which of those it was.

Both are subject to `ratelimit.py` per author, and the page must be readable by the caller; "not
yours" is a 404 identical to "no such page".

### 6.6 Serving (content origin only for the browser)

- **Reader's browser.** Images are displayed from the content origin, never the gallery origin,
  which holds Jeremy's session. The gallery, when it renders the panel, mints a capability for each
  image (HMAC over image id and an expiry of one hour, key from the same secret store as
  `frame_pass`), and `<img src>` points at `<content-origin>/ci/<image_id>?t=<token>`. The content
  app answers only that path, only with a valid token, with: the stored sniffed `content-type`,
  `x-content-type-options: nosniff`, `content-security-policy: default-src 'none'; sandbox`,
  `cache-control: private, max-age=3600`, `content-disposition: inline`. The gallery CSP gains
  `img-src <content-origin>`; nothing else. A token for a page the reader lost access to simply
  expires; the reader never holds a token the gallery did not mint for them on a page they may read.
- **Agents.** `GET /api/v1/comments/{comment_id}/images/{image_id}` with the key, scoped like the
  comment listing (own page, or another agent's page that is open to agents; otherwise the same 404).
  The API origin sets no cookie and honours no ambient credential, so a stored image cannot ride a
  session; the response is `content-disposition: attachment`, the sniffed type, `nosniff`, and the
  same sandbox CSP.

## 7. API and CLI shapes

**API** (all under `/api/v1`, all key-scoped):

- `POST /artifacts/{id}/comments` body gains `images` and accepts the anchors in section 3; the
  reply/`thread_root` rules are unchanged (a reply inherits the root's anchor and quote, and may
  carry its own images).
- `GET /artifacts/{id}/comments` and `GET /comments` rows gain `anchor` (full), `anchor_state`,
  `anchor_note` and `images: [{id, content_type, width, height, byte_size, url}]`, where `url` is
  the API path in 6.6. A filter `anchor_state=outdated` lists only gone spots. The paging added on
  `GET /comments` (`limit`, `after`) is unchanged.
- `POST /artifacts/{id}/comment-images` and the download route, as above.

**CLI** (`artifacts-decide`, reinstall after the change):

```
artifacts-decide comment PAGE "text" [--quote T [--section H]] [--element ID]
                                      [--image PATH ...]          # up to 4
artifacts-decide reply   THREAD "text" [--image PATH ...]
artifacts-decide comments [--outdated] [--no-images]
```

`--element ID` and `--quote` are mutually exclusive. `--image` uploads each path (checked locally
for the same limits so the error is immediate), then attaches. `comments` fetches the images of
every thread it prints into `$XDG_CACHE_HOME/artifacts/images/<comment_id>/<n>.<ext>` (directory
0700, files 0600, name from the sha256 so a repeat run downloads nothing, at most 20 images a call),
and prints paths. `--no-images` skips the download and prints sizes only. No URL with a token is
ever printed.

```
01H...  comment thread on page 01G...  [NEEDS YOUR REPLY]
  on: "Cost per seat is flat"  (under "Costs" #cost-table, v3 -- OUTDATED, gone since v5)
  element: <table id="cost-table"> under "Costs"          # element pins, instead of "on:"
  image: /home/me/.cache/artifacts/images/01H.../1.png  (png 1200x800, 214 KB)
  HE SAID: this column is wrong
  reply: artifacts-decide reply 01H... "..."   · done: artifacts-decide settle 01H...
```

`needs_reply` and the wait/nudge flow are unchanged. A thread whose pin is outdated is still
listed with its normal state; `--outdated` only filters. `artifacts-decide wait` wakes on a
pinned comment as on any comment.

## 8. Slices (each shippable alone, in order)

1. **Evidence and outdated** (built, 29 Oct 2026). Section 3's `range` extension and `quote` kind, `anchors.py`,
   `anchor_state`/`anchor_note` in the API and CLI, the shim's heading and id context, the
   thread chip and the "Outdated pins" list. No new interaction; every existing comment gets a
   state. Agent text anchors become paintable.
2. **Element pins** (built, 29 Oct 2026; see "As built, slice 2" below). `element` kind, pin mode
   and outline and badges in the shim and gallery, `--element ID` and `{"kind":"element"}` for
   agents.
3. **Images** (built, 29 Oct 2026; see "As built, slice 3 (server)" and "(gallery)" below).
   `images.py`, `comment_image`, upload and download routes, content-origin serving, `--image` and
   the cached paths in `comments`; the gallery's attach control, thumbnails and CSP entry.
4. **Spot screenshot** (optional, only if Jeremy asks): for element pins, a server-side render of
   that version cropped to the element through `thumbs.py`, attached automatically. Not designed
   here beyond that; it is the one slice with real rendering cost and quality risk.

### As built, slice 2 (29 Oct 2026)

Follow-up (29 Oct 2026): an element pin whose evidence has fewer than `anchors.ELEMENT_QUOTE_MIN` (4)
visible characters skips the quote and label search, so "A" no longer reads current; it resolves by
id or is outdated.

Built as written in sections 3, 4, 5 and 7, with the prior-art additions Jeremy accepted
(`2026-10-29-pinned-comments-prior-art.md`) folded in:

- Focus: a pick moves the cursor into the comment box only when pin mode was turned on in the
  gallery (the Pin button or a key there). The page can send any message, so a pin mode it
  turns on (the C key while the frame has focus, or a forged toggle) shows the pick and leaves
  the cursor where it was. Found in a security review; tested in `test_element_pins.py`.
- An SVG's `<title>` is found with three linear finds, not a lazy regex that rescanned to the
  end from every unclosed opener.
- The bar's Pin button is `.pinmodebtn`; `.pinbtn` stays the wall's pin-to-wall form.
- An element with no words and no label is stored with an empty quote; only the gallery composer
  shows its tag. Captions (`figcaption`) are read by the browser only, so an agent's `--element`
  pin is labelled by alt text, aria-label or title.
- **An edited spot shows both quotes.** An `edited` row carries `anchor_current`: the element's
  text (or alt text) now for an element pin found by its id, or for a quote the words now between
  its stored prefix and suffix. The panel prints `Text edited since version N. Now: "..."` under
  the original quote; the CLI prints a `now:` line. Never taken from elsewhere on the page, so it
  cannot show words the pin was not about; "" when it cannot be told. A listing fills at most 50
  per call (`comments.EDITED_NOW_MAX`).
- **32-character prefix and suffix.** An element pin carries 32 characters of the page's words
  either side (the shim measures them; for an agent's id the server reads them off the body).
  The prefix and suffix of an *element* are the words around it, so they are not evidence that it
  was edited rather than removed: step 4 of the resolution order uses the element's own words only.
- **The version stamp.** The thread says `Pinned on version N` for every text and element pin
  (and `it was there in version N` once outdated); the CLI prints `vN`.
- **Row and column labels** for a table cell (`row`, `column`): the first cell of its row and the
  header row's cell at the same position; left out, never guessed, when either row has a spanning
  cell or the cell is itself the header.
- **Alt text and captions** (`label`) for an image, chart or canvas: `alt`, `aria-label`, `title`,
  an SVG's `<title>`, else its `<figure>`'s caption (the caption only from the browser; the
  server, reading by id, uses the first three and the SVG title). The label is the quote when the
  element has no words, and an image with no id is re-found by it.
- **The state on create.** The reply to `POST /artifacts/{id}/comments` carries `anchor_state`,
  `anchor_note` and `anchor_current`, read as a listing reads them.
- Still not built, as agreed: automatic spot capture.

### As built: approximate re-find of an edited quote

- When the quote is gone and no prefix, suffix or half of it survives, `anchors.PageIndex.fuzzy`
  looks for it **approximately inside the pin's own heading section** (found by `heading_id`, then
  heading text; the first three matches). A match is `edited`, never `current`, and its text is
  the pin's `anchor_current`, beside the original quote. No match: `outdated`. It never searches
  outside the section, so a pin is never re-attached elsewhere. Quote pins only, not element pins.
- Matcher: `difflib.SequenceMatcher.ratio() >= 0.8` on case-folded text, over windows of the
  section's words (as many words as the quote, one fewer, one more). 0.8 is about one word in
  five changed: looser starts matching a neighbouring sentence of the same shape, tighter misses a
  reworded clause. Standard library only.
- Minimum: a quote under 24 characters or 4 words is never matched approximately.
- Bounds: section HTML read 200,000 chars, its words compared 4,000, quote capped at 200,
  at most 1,500 windows scored per search. One `anchors.FuzzyBudget` per listing, shared by every
  page's index, allows 20 searches and 1.5 million work (each window charged its length plus the
  quote's, each scored window also length x quote length): about 0.15 s for a whole wall, however
  many pages it spans. Finished searches are cached across requests (a page version never
  changes) by hash of the section words and the quote, 4,000 entries, cleared when full; a search
  cut short by the budget is not cached and reads as no answer. `autojunk` is off and the cheap
  upper bounds (`real_quick_ratio`, `quick_ratio`) run first.
- Out of scope, server-side state only: the browser shim does not place a fuzzy-matched quote
  (the gallery still paints from its own exact placement and lists such a pin unpainted with the
  server's `edited` label), and the carry-forward state of a comment (`comments.carried_state`)
  still uses the exact evidence only.

Differences from the text above, and why:

- An id is at most **128** characters, not 200 (the limit `sections` addresses, fixed in slice 1);
  the shim's id pattern was brought into line.
- An `element` anchor names its element `id`, never `el_id` (which stays `range`'s); `heading_id`
  is the heading's own id, as in slice 1.
- Three shim messages beyond the spec's list, because the keys go to the frame when it has focus
  and the frame resizes: `artifact-pin-toggle` (C pressed in the frame), `artifact-pin-exit`
  (Escape in the mode) and `artifact-pin-rects` (the badges' rectangles again after a resize).
- The shim places by tag and ordinal only if the element still holds the quote it was pinned
  with (principle 3: never by position alone); an image is also re-found by its alt text.
- A pick of an element with no id, no heading, no words and no label is swallowed but not sent: the
  service would refuse an anchor nothing can find again.
- Element pins are placed, not painted, so a pin the shim cannot place and the server calls
  `current` is shown in the list only, as for text.
- Tests: `tests/test_element_pins.py` holds the Python tests for this slice (resolution, anchors,
  the agent route, the CLI and the panel) rather than extending `test_anchors.py` and
  `test_comments.py`; the shim and gallery cases are in `tests/js/marks.test.mjs` and
  `comments.test.mjs`.

### As built, slice 3 (server) (29 Oct 2026)

The gallery half is described under "As built, slice 3 (gallery)" below.

Built as written in sections 6, 7 and 9. How each limit is enforced:

| Limit | Where |
|---|---|
| Body read stops at 5 MB (plus 16 KB of multipart framing) | `api.upload_comment_image` streams the body and refuses at the cap, or on a larger `Content-Length` before reading; the file is then held to exactly 5 MB in `images.inspect` (413) |
| PNG, JPEG, WebP from the bytes; named refusals | `images._sniff`; GIF, SVG, HEIC, AVIF, TIFF, BMP named; animated WebP and APNG refused by their markers |
| 4000 px a side, 16 megapixels | read from `IHDR`, `SOF0/1/2`, `VP8`/`VP8L`/`VP8X` before the file is walked (`images._check_size`) |
| Metadata stripped, file walked to its end | `images._png`, `_jpeg`, `_webp` rewrite the file; CRCs checked for PNG; a file that does not reach IEND/EOI/its RIFF size is refused |
| 12 staged per author, swept at one hour | `image_store.stage` (counts only rows under an hour old) and `image_store.sweep` (hourly thread in `maintenance`, and nightly) |
| 40 per page, 4 per comment | `image_store.stage`; `images.check_ids` and `image_store.attach` |
| Not yours, unknown, used, other page, expired | one 422 from `image_store.attach`; one 404 (`no such image`) from the download route |

Differences from the text above, and why:

- **SQLite schema.** `schema_sqlite.sql` is step 1 and never edited, so the SQLite tables are step
  10 in `db.py` (`_SQLITE_STEPS`); the columns are the same as `schema.sql`.
- **The signing key** is stored in a new table `server_secret` (named secrets, made on first use),
  not read from the `frame_pass` secret store: `frame_pass` keeps hashed one-time passes in the
  database and has no key. A table needs no deployment change and is the same on every replica
  and after a restart. It is copied by `migrate.py` like any table.
- **JPEG metadata is an allowlist.** APP0 is kept only if it begins `JFIF\0`, APP2 only if it
  begins `ICC_PROFILE\0`, APP14 only if it begins `Adobe`; every other APPn, COM and the
  reserved markers 0x02-0xBF and 0xF0-0xFD are dropped, so a vendor's APP2 or a JPGn marker
  cannot carry text through. A WebP VP8X keeps its fixed 10 bytes and nothing after them.
  Data after EOI is dropped.
- **Hostile files cost bounded work.** A file with more than 10,000 PNG chunks, JPEG segments
  or WebP chunks is refused by name (`images.MAX_CHUNKS`); the JPEG scan is one compiled regex
  search, not a byte loop; the upload route runs `file_from_multipart` and `inspect` in
  `run_in_threadpool`, as `stage`, so a crafted 5 MB file cannot stall the event loop.
- **The 40-per-page cap is held in `attach`** under a per-page lock, counting the batch: 39
  attached plus 2 is refused whole. Staged bytes per key are already bounded by 12 staged
  pictures at 5 MB each.
- **The CLI trusts no id from the server**: `_fetch_images` skips (with a warning) any comment
  or picture id that is not 26 characters of `[a-z2-7]` before building a path or a URL.
- **A multipart envelope adds up to 16 KB** to the 5 MB before a body is refused unread; the file
  itself is held to exactly 5 MB.
- **`sha256` is on each image row** in the comment listing (the spec's list has no digest); the
  CLI names its cached file by it. The cached name is `<n>-<first 16 of sha256>.<ext>`, so a
  repeat run finds the file and downloads nothing.
- **The 40 a page counts attached pictures only.** Pictures waiting to be attached count against
  their uploader's 12, not the page's, so an agent cannot fill another office's page with uploads.
- **No separate rate limiter.** `ratelimit.py` counts failed keys only; the staged-per-author and
  per-page quotas bound uploads. A throughput limit per author would be a new mechanism.
- **The content origin does not check `held`.** The token is the gallery's decision (it mints one
  for Jeremy's own held comment, which he may see); an agent never gets one, and the agent route
  refuses held and withdrawn comments. A withdrawn comment, a deleted page and a picture not
  attached to a comment are 404 on the content origin.
- `images.py` imports nothing from the service, so `artifacts-decide` runs the same
  `images.inspect` before it uploads; storage and tokens are in `image_store.py`.
- Tests: `tests/test_comment_images.py` (formats, limits, access, the content origin, the CLI) and
  rows for the two new tables in `tests/test_migrate.py`.

## 9. Tests (written first; run by CI on a `ci/<name>` branch, never on baba)

- `tests/test_anchors.py`: each step of the resolution order, with the literal inputs the gallery and
  an agent really send (an id that vanished, a duplicated id, a heading renamed, a quote moved under
  another heading, a quote edited in place, a quote gone, an emphasis tag added, a page over the
  search limit). Both databases.
- `tests/test_comments.py`, `test_agent_comments.py`: `validate_anchor` for `range` with the new keys
  and for `element`/`quote` (exact key sets, caps, lone surrogate, bare `InvalidAnchor`); an agent
  `element` anchor on a real id; a `text` anchor now stored as `quote`; `remove`/`looks_good` still
  `range`-only; listings carry `anchor_state`.
- `tests/test_comment_images.py`: sniff by bytes with a lying `Content-Type` and name; every refused
  format with its named reason; 5 MB and 4000 px edges (at the limit passes, one over fails); body
  cut off at the cap without being read whole; metadata gone from the stored bytes for each format
  (a PNG with `tEXt`/`eXIf`, a JPEG with EXIF GPS, a WebP with `EXIF`); truncated files refused;
  staging, same-bytes-same-id, expiry sweep, attach of another author's or another page's id
  refused identically to an unknown id; quotas.
- Access: an image of another agent's closed page and an unknown image answer the same 404; a held
  or withdrawn comment's image is not served; deleting the page deletes the images; the content
  origin serves nothing without a valid, unexpired token and serves with `nosniff` and the sandbox
  CSP; the gallery origin serves no image bytes at all.
- `tests/js/marks.test.mjs`: `quote` anchors place without a path; element placement by id, then
  heading and ordinal; pin mode swallows clicks only while on; nothing but capped evidence is posted.
- CLI: `comment --element`, `--image` local limit errors, `comments` prints anchors, outdated
  chips and cached paths, `--no-images` prints none, a repeat run downloads nothing.

## 10. Documentation (part of done, in the same changes)

- `onboarding.py` (the agents text near "artifacts-decide comments"): the `--element`, `--image`,
  `--outdated` forms; what an anchor line and an image path mean; the 5 MB / 4000 px / three-format
  limits; that an outdated pin keeps its quote.
- `decide.py` `--help` for `comment`, `reply`, `comments`.
- `docs/comments-design.md`: the anchor kinds and the pin mode; `docs/agent/design.md`: images
  served from the content origin only.
- The `lab-artifacts` skill (runcove) and the office doc `artifacts-maintainer.md`: the new commands
  and the image path behaviour, as a runcove change.
- A reply in the feedback thread to the eleven offices when slice 1 ships (what an outdated pin
  looks like to them), and a line in `docs/agent/implementation-plan.md`'s ledger.

## 11. Out of scope

In-page forms, delivery receipts, change notes and editable diagrams (they reuse the anchor model
and the shim's pin messages, nothing more); OCR or secret scanning of pixels; image editing or
annotation; video; a reader identity beyond the author string comments already use.

## Choices for Jeremy

**1. How do you pin a comment to something that is not text, such as a chart, an image or a table?**
- (a) A "Pin" button in the page bar. Press it, move over the page to see the part that would be
  pinned outlined, click it; Escape cancels. Selecting text works as it does now. Recommended: the
  page's own buttons and links keep working the rest of the time.
- (b) Clicking anywhere on the page always pins. Simpler, but a page's own buttons and links stop
  working while you read.
- (c) Text only for now; no pinning to charts or images.

**2. A page is republished and the spot a comment was pinned to is gone. What do you see?**
- (a) The comment stays in its thread, labelled "outdated" with its quote still shown, and a list
  "Outdated pins" sits at the top of the comments panel as well. Nothing is hidden or moved.
- (b) Only the label in the thread, no separate list.
- (c) Only the separate list at the top, with the label left off the thread.

**3. A pinned spot is still on the page but its words were edited. What do you see?**
- (a) The pin stays on that spot with a small "text edited since v3" label; the comment is not
  called outdated.
- (b) Treated as outdated, the same as a spot that is gone.

**4. How do you attach pictures to a comment?**
- (a) Paste, drop or choose up to four per comment, with a thumbnail row you can remove from before
  sending; a picture over 5 MB or 4000 px is shrunk in your browser first, and only refused if it is
  still too big. A small note beside the control says images are not checked for secrets.
- (b) As (a), without the note about secrets.
- (c) Choose a file only; no paste or drop; over-size pictures are refused with a message.

**5. Should the page capture a picture of the pinned spot for you?**
- (a) Not now. Attaching your own picture comes first; an automatic "capture this spot" can follow
  later for element pins if you find yourself wanting it.
- (b) Build it with the first release of pinning, accepting that it will be slower to arrive and
  may not match what you saw on pages that change as they load.

### As built, slice 3 (gallery) (29 Oct 2026)

Built as written in sections 4 (Attach images), 6.5 and 6.6. What each piece is:

- **Twin route** `POST /c/{artifact_id}/image` (`gallery.add_comment_image`): behind the gallery's
  same-origin guard, one multipart file in `file`, staged for author `jeremy`. A page that does
  not exist or is deleted is `404 no such artifact`. Answers and limits are the agent route's;
  both call `image_store.stage_upload`, the one place that streams the body to the 5 MB + 16 KB
  cap (or refuses on `Content-Length` first) and runs the lift, inspect and stage in the thread
  pool.
- **Form.** `POST /c/{artifact_id}` takes repeated `image` fields into `comments.add(image_ids=)`.
  A refusal is one `422` with the same plain-string text the agent route gives, and the comment
  is not stored. The in-place (JSON) reply renders the thread with its thumbnails.
- **Panel.** `_pictures` draws, for each picture of a comment, a link and an `<img class="cthumb">`
  whose address is `image_store.origin_url(...)` with a token minted for this render (valid an
  hour; a page left open longer needs a reload). `_render_comments` mints all of a render's tokens
  in one `image_store.mint_tokens` call on one connection (`mint_token` stays for single ids). His own held comments show their pictures, as
  planned. Clicking a thumbnail toggles it larger in the panel (a `big` class); without scripts
  it is a link to the same picture on the content origin.
- **CSP.** The gallery's policy changes in one place: `img-src 'self' https://*.<content domain>`
  (the domain is `store.content_domain()`, as for `frame-src`). No `data:` and no `blob:`.
- **Attach control** (in `/comments.js`; markup from `gallery._ATTACH`, in the new-comment form and
  in every reply form): hidden until the script sets `cpics-on` on `<html>`. Paste into the form,
  drop on it, or the "Attach pictures" button; at most 4 per comment (a fifth, or a file that is
  not an image, is refused with a line under the control). Each shows as a removable thumbnail.
  On send the pictures are staged one at a time, then the comment is posted with the ids; a
  file over 25 MB is refused before it is decoded; for the rest the declared size is read from
  the header (PNG, JPEG, WebP; unreadable means no preview and no decode), a picture declaring over
  300 megapixels is refused, and anything over the limits is decoded already scaled
  (`createImageBitmap` with `resizeWidth`/`resizeHeight`), never at full size. Send waits for a
  decode still running. A picture over 4000 px a side, 16 megapixels or 5 MB is redrawn on a canvas first (PNG stays PNG
  if that fits, else JPEG at falling quality and size) and sent as it is when the browser cannot
  decode it, for the server to judge. A refusal at any step is written under the control (by
  `textContent`), the words stay in the box, and the comment is never re-sent as a plain form
  without its pictures. The note under the control is "Images are not checked for secrets."

Differences from the text above, and why:

- **Thumbnails in the attach row are canvases, not `<img>`.** A local preview as `<img>` would need
  `blob:` or `data:` in `img-src`; the spec allows nothing but the content origin. A decoded
  bitmap is drawn on a 56 px canvas instead; when the browser cannot decode the file the chip
  shows its name.
- **A comment needs words.** A picture alone is not a comment: the gallery form still refuses an
  empty body (kind `comment`), as it did before.
- **"Not yours" for the twin** is only "no such page": he owns every page, so nothing is hidden
  from him. An id staged by an agent is refused at attach with the same `422` as an unknown one.
- **No rate limiter** on the twin, for the reason given for the agent route: the 12 staged per
  author and 40 per page quotas bound it.

## As built: the comment redesign (5 Nov 2026)

See `docs/comments-design.md` ("As built: Open and Resolved tabs, and reopening")
and the entry in `docs/decisions.md`. In this spec's terms: the panel's resolved
fold became Open and Resolved tabs, and threads can be reopened (gallery form,
`POST /api/v1/comments/{id}/reopen`, `artifacts-decide reopen`). The pin mode,
Pin button and C key described above are replaced by other pieces of the same
redesign; their notes follow when they land.
