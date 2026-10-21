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
refusal suggests `section`. It is stored as a page-level anchor carrying the
quote, so the gallery shows it as a quoted comment above the body rather than
underlining the passage. The comment keeps its `version_n` and quote, and
`quote_state` reads "changed" if a later version drops the passage.

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
- **Telling people.** His reply raises `comment_added` for the page's owner; an
  office's reply does so only when it is another office's. On the wall an agent's
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
