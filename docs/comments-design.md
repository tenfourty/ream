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
| `author_kind`, `author` | `human`/`jeremy` or `agent`/`<agent name>` |
| `body` | what was said |
| `created_at`, `resolved_at`, `resolved_by` | lifecycle |

**A comment never silently moves.** It records the version it was made on.
When a page is republished, an anchor whose `quote` still occurs exactly
once in the new version follows it; anything else keeps pointing at the
version it was about, and the panel says so and links there.

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
