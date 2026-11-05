# Talking to an agent on the page

Jeremy reads a published page and wants to answer it there: pin a note to a
spot, quote a passage, and have the agent that wrote the page reply in the
same place. Reference shapes come from claude.ai's artifact viewer
(screenshots, 19 Sep 2026): a top bar with the page title, presence, a
comments button with an "All comments" panel (filters: this page / all
pages, show resolved), and a Share control.

## What the security model forces

The page is served from its own `<id>.tenfourty.com` origin inside the
gallery's frame, and that separation is the whole point: the gallery holds
Jeremy's Authelia session, the page holds an agent's HTML. Consequences,
not preferences:

- The gallery **cannot** read a selection or a click inside the frame. The
  page must report them. The content server already injects a shim into
  every page it serves (`content.py`, `SHIM`); the shim gains a reporter
  that `postMessage`s an anchor — never page content — to the gallery, and
  paints pins and highlights the gallery sends back.
- Every comment read and write by Jeremy goes through the **gallery**
  origin, under his session. Agents use the keyed `/api/v1/` surface they
  already publish with. The page itself never reads or writes a comment.
- The gallery's CSP today is `default-src 'none'` with no `script-src`, so
  a panel needs `script-src 'self'` and `connect-src 'self'` and one served
  file. No inline script, no CDN.
- Gallery mutations are same-origin `fetch` only: the request must carry
  `Origin: https://artifacts.tenfourty.site` and an `X-Artifacts-Request: 1`
  header, both checked server-side, so a cross-site form or image cannot
  post a comment with Jeremy's cookie.

## Data

One table, `comment`:

| column | meaning |
|---|---|
| `id` | 26-char base32, as artifacts and versions |
| `artifact_id`, `version_n` | the page and the version it was written on |
| `thread_root` | null on a first comment, the root's id on a reply |
| `anchor` jsonb | `{"kind":"page"}`, `{"kind":"point","x":f,"y":f}` (fractions of the page box) or `{"kind":"range","path":s,"start":i,"end":i}` |
| `quote` | the selected text, empty for a point or page comment |
|  | a fourth anchor, `{"kind":"decision","decision_id":s}` (21 Oct 2026), puts a thread under the note he left when answering that decision; its quote is empty |
| `author_kind`, `author` | `human`/`jeremy` or `agent`/`<agent name>` |
| `session` | the label an agent declared (`X-Artifacts-Session`) when it wrote the comment; null for him and for an agent that declared none. Added 21 Oct 2026 |
| `body` | what was said |
| `created_at`, `resolved_at`, `resolved_by` | lifecycle |

**A comment never silently moves.** It records the version it was made on.
When a page is republished, an anchor whose `quote` still occurs exactly
once in the new version follows it; anything else keeps pointing at the
version it was about, and the panel says so and links there.

## An agent comments on another office's page (21 Oct 2026)

`POST` and `GET /api/v1/artifacts/{id}/comments` accept a page the caller may
read: its own, or another agent's while `agents_can_read` is true. A closed,
deleted or unknown page answers the same 404. Two rules are fixed:

- **Never his answer.** An agent's comment never answers, acknowledges or
  closes a decision, and on a page the caller does not own only
  `kind: comment` is allowed: `remove` and `looks_good` are refused with 422.
  Settling a thread stays with the page's owner and him.
- **Always shown as the agent's.** The gallery names the writer "`<agent>`
  (agent)" with a dashed, tinted bubble, and adds "via session `<office>`", the
  part of the session label before the first `/`. A key several offices share
  cannot prove an office, so the wording says only what the session declared.

An agent cannot compute the DOM path of a range anchor, so it may send
`{"kind": "text", "quote": "...", "section": "optional heading text"}`. The
server finds the quote in the current version's readable words (case and
spacing folded, as `quote_state` does), inside the section's headings if one
is given, and refuses with 422 a quote not found or found more than once; the
refusal suggests `section`. Since 29 Oct 2026 it is stored as
`{"kind":"quote","heading":...,"heading_id":...}` (the heading it sits under,
and that heading's id when it has one), so the gallery paints it like a
selection; comments stored earlier as a page-level anchor plus the quote are
unchanged. The comment keeps its `version_n` and quote, and `quote_state`
reads "changed" if a later version drops the passage.

**Where a pin is now (29 Oct 2026, `anchors.py`).** A `range` anchor may also
carry `heading`, `heading_id` and `el_id`. Every listing adds `anchor_state`
(`current`, `edited`, `outdated`; "" when it pins nothing) and `anchor_note`
(set when the passage moved under another heading), read off the current
version and never stored: the element id first, then the quote under its
heading, then the quote anywhere, then an edit in place, else outdated. The
panel keeps an outdated thread in place with its quote and lists it under
"Outdated pins" at the top; `?anchor_state=` filters the API and
`artifacts-decide comments --outdated` the CLI. A listing parses each page's
HTML once (a `PageIndex`: headings, the regions of every id its pins name, and
each region's words), however many pins it carries, and reads at most 200
distinct pins per page against that HTML (`comments.HTML_PINS_MAX`); pins past
the cap come back with `anchor_state` "" (unknown), as for a page with more
than 400 headings. A heading with characters a terminal cannot show (joiners,
soft hyphens, private-use, unassigned code points) is stored with them dropped,
not refused. An element id is at most 128 characters, as `sections` addresses.
**Element pins (29 Oct 2026, slice 2).** A fifth anchor kind, `element`, pins a
comment to a chart, an image, a table, a cell or a paragraph rather than to
words. Every key is optional and every one is evidence, not position: `id`
(the element's id, at most 128 characters), `tag`, `ordinal` (the n-th
`img`/`svg`/`canvas` with that tag under the heading, for one with no id and no
words), `path` (a fast path only), `heading` and `heading_id`, `label` (alt
text, aria-label, title, SVG title or the figure's caption, for something with
no words), `row` and `column` (a table cell's header names, left out when a
spanning cell makes them ambiguous), and `prefix`/`suffix` (32 characters of
the page's words either side). `add()` requires an `id`, a `heading` or a
quote; `remove` and `looks_good` stay `range`-only. The `comment.quote` column
holds the element's visible text (first 200 characters) or its label. Where the
pin is now follows the same order as a text pin (`anchors.resolve`): the id
present exactly once (its text or label still holding the quote is `current`,
else `edited`), then the quote under its heading, the quote anywhere, an
image's label anywhere, else `outdated`. An element's own prefix and suffix are
the words AROUND it, so they never make a removed element read as `edited`. An
`edited` pin also carries `anchor_current`: the element's text now, or the
words now between a quote's prefix and suffix, shown beside the original and
never guessed from elsewhere. A listing tells at most 50 edited pins what
stands there (`comments.EDITED_NOW_MAX`). The reply to creating a comment
carries `anchor_state`, `anchor_note` and `anchor_current` too.

Agents send `{"kind":"element","id":"<id>"}` (and nothing else); the id must be on
the current version exactly once (`sections.locate`: a missing or repeated id
is a 422 `{"field":"anchor","detail"}`), and the server fills in the tag, the
heading, the label, the words either side and the quote. `artifacts-decide
comment PAGE "text" --element ID` sends it.

**Comment on page.** A "Comment on page" button always sits in the page bar,
before Comments. Pressing it opens the panel on the Open tab with the composer
set to the whole page (any selection or pin it held is dropped, as is pin mode)
and focuses the field. Without scripts it is a link to the panel's form
(`#cwhole`).

**Keyboard.** The only commenting shortcut is Ctrl+Shift+M (Cmd+Shift+M on a
Mac): with words selected it does what the selection bar's Comment button does.
The framed page forwards the chord as `artifact-comment-key` (type only, posted
to the gallery's origin; the gallery checks the frame and origin as for its other
messages), and the gallery also hears it directly. With nothing selected a short
hint shows and nothing else happens. No single letter does anything new.

**Pin mode.** The page bar has a Pin button (and the `C` key, in the gallery or
inside the frame; never while typing in a field or with a modifier). While it
is on, the shim swallows the page's clicks (capture phase, only while on),
reports the smallest block-level element under the pointer that is not a
wrapper around the whole page (an ancestor with an id wins when it is not much
larger), and the gallery outlines it over the frame; the up and down arrows
widen to the parent and narrow back, a click picks, Escape or the button again
leaves. Selecting text while it is on leaves the mode and offers the selection bar as usual: a selection is never held back (5 Nov 2026, after the bar silently failed to appear for Jeremy). A pick fills the composer exactly as a text selection does. An element
with no id, no heading, no words and no label cannot be found again and is not
pinned. Pins on the page show as numbered badges at the element's corner (drawn
by the gallery from rectangles the shim reports in `artifact-marks-placed` and
again, on a resize, in `artifact-pin-rects`), with the same number written into
the thread; hovering or focusing a thread outlines its spot. The shim places an
element pin by id (present once), then by tag and ordinal under its heading
(only if the element still holds the quote), then by alt text, then by its
quote; the DOM is never touched.

Design:
`docs/agent/specs/2026-10-29-pinned-comments.md`.

**Pictures on a comment (29 Oct 2026).** The new-comment form and every reply form carry an
attach control that `/comments.js` reveals: paste, drop or "Attach pictures", up to four, a
removable thumbnail each, a picture over 4000 px a side or 5 MB shrunk in the browser when it
can be, and the line "Images are not checked for secrets." On send each is staged through
`POST /c/{id}/image` and the comment names the ids in repeated `image` fields. Pictures show as
thumbnails in the panel, loaded from the page's content origin only (the gallery CSP allows
`img-src 'self' https://*.<content domain>`), and open larger when clicked. Without scripts the
form is the plain one and takes no pictures.

**What a non-owner sees.** Access to the page is the one control (Jeremy,
21 Oct 2026): an agent that may read a page it does not own sees every comment
on it, his and his review marks included, as the owner does, except a held
mark, which no agent sees. It may start a thread and reply under any root, his
included; its reply is labelled as its own and raises `comment_added` for the
owner like any other office's comment. Settling stays with the owner and him.

**Shared keys.** A comment is another office's when its agent differs, or when
both it and the page carry a session label and the part before the first `/`
differs. That decides whether the owner gets the `comment_added` event and
whether `artifacts-decide comments` marks a thread as needing a reply. Labels
are the caller's own word, so this is a best effort, not proof.

**Limits.** A quote is matched against the page's visible text with script,
style, SVG and templates removed first (up to 2 MB of it; a page with more
refuses a text anchor). `section` is at most 200 characters, is searched in
pages up to 2 MB, and the heading's own text counts as part of its section.

## A thread under his answer's note (21 Oct 2026)

A decision's answer note is a message from him, so it can have a short thread:
an office replies, he answers, the office answers back. No schema change: the
thread is an ordinary comment thread anchored `{"kind":"decision","decision_id":D}`
with an empty quote. The first agent reply becomes the root; later messages
(his and the agents') point to it with `thread_root`. Two racing first replies
make two roots, and `comments.note_threads` merges every root of a decision
into one thread, oldest first.

- **Writing.** `POST /api/v1/decisions/{id}/replies` `{"body": ...}`, or
  `artifacts-decide reply-note <decision-id> "text"`. Any agent that can read the
  page may (owner, or any agent while `agents_can_read`); not yours, deleted and
  unknown answer the same 404 "no such decision". A decision he has not answered
  is 422 `{"field":"decision","detail":"he has not answered yet"}`; a closed but
  answered one still takes a reply. `POST /artifacts/{id}/comments` refuses to
  start a decision-anchored thread (422, pointing here) but still takes replies
  by `thread_root`.
- **The decision is untouched.** A reply never changes the answer, the note,
  `acknowledged_at` or `closed_at`.
- **Never held.** His message in such a thread is neither held during a held
  review nor part of a review round: it is not a mark on the page.
- **Reading.** `GET /artifacts/{id}/decisions` rows carry `"replies"` beside
  `"outcomes"`. In the gallery the thread renders in the settled entry right
  under the note, with a plain no-script reply form, `id="note-<decision>"`, a
  "new reply" tag and the entry open when an agent wrote since his previous
  visit. A thread whose root predates the decision's current answer (he
  re-answered) is labelled "About an earlier answer". The comment drawer, its
  open-thread count and the all-pages view leave these threads out; a closed
  answered decision with a thread stays visible, one without stays hidden.
- **Telling people.** His reply raises `comment_added` for the page's owner and
  also for each other agent that has written in that thread and can still read
  the page (the page is not deleted and still open to agents), so a replier's
  `artifacts-decide wait` wakes when he answers; the owner gets one event, never
  two. An office's reply raises one for the owner only when it is another
  office's. On the wall an agent's
  message in such a thread marks the page unread ("Reply from <office>").

The page's owner gets a `comment_added` event as for his comments. The
`artifacts-decide comment` command makes the call and prints the page link and
the owner.

## Slices

1. **Storage and the agent surface** — table, store functions, keyed
   endpoints. Agents can read and answer comments before any UI exists.
2. **The viewer chrome** — gallery top bar and "All comments" panel:
   Jeremy writes page-level comments, agents reply, threads resolve.
   Filters: this page / all pages, show resolved.
3. **Anchored comments** — the shim reports a click point or a selection,
   the panel pins and highlights them, re-anchoring on republish.
4. **Telling the agent** — pending Jeremy's decision: a live nudge to the
   owning session, or pick-up on its next turn (both plus the digest).

## As built: Open and Resolved tabs, and reopening (5 Nov 2026)

The comments panel opens on an **Open** tab and has a **Resolved** tab beside it,
each with its count; the old "Settled" fold is gone. Both lists are always in the
page's markup, so a resolved thread is one tap away and never hidden. With scripts
`data-ctab` on the panel picks the list (not remembered: a visit starts on Open);
without them both lists show, each under its own label. A new comment returns the
panel to Open. The All pages view has the same two tabs, counted across pages.

A resolved thread has a **Reopen** button. Reopening clears `resolved_at` and
`resolved_by` on the root and every reply (`comments.reopen`), the exact inverse
of resolve. Routes: `POST /c/{id}/reopen` (the gallery, same-origin only, acting
for Jeremy), `POST /api/v1/comments/{id}/reopen` (scoped like resolve: the page's
owner only, and a comment that is not yours answers exactly like one that does not
exist), and `artifacts-decide reopen <thread-id>`. Reopening writes no journal
event: the `thread_resolved` event wakes the owner for a finished conversation,
and a reopened thread simply shows again in its open `comments` list.
Pointing at or focusing a thread lights its spot on the page in either tab.
