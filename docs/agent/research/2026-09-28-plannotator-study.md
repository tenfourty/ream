# Plannotator study for page review (28 Sep 2026)

> **Licence note.** plannotator (`backnotprop/plannotator`) and `plannotator/artifact-server` are
> dual-licensed MIT OR Apache-2.0. This report contains **no copied code**. It has only
> descriptions, our own pseudocode and file/line references. The exceptions are a few one-line
> string literals, quoted where the exact wording is the finding. Studied at plannotator `8ce0d72`
> (28 Sep) and artifact-server `5af28ac` (24 Sep). Paths are relative to each repo root.

The product decisions (marks on every page, verdicts on requested reviews, hold/live, automatic
round 2, tracked changes, one numbered message plus a distinct exit code) are taken as given. This
report is about how to build them on `comments.py`, `decisions.py`, `content.py`, `events.py`,
`decide.py` and `store.py`.

---

## A. Anchoring a mark to text

**(i) How they do it.**
- *Markdown surface:* an annotation keeps `originalText` (the selected string) and, optionally,
  web-highlighter `startMeta`/`endMeta`, each holding a parent tag, the parent's index and a
  text offset (`packages/ui/types.ts:43-47, 74-106`). Restore is a ladder
  (`packages/ui/hooks/useAnnotationHighlighter.ts:1134-1172`):
  1. Paint from the stored positions.
  2. Read back the painted text and compare it with `originalText`, ignoring all whitespace
     (`:200-213`).
  3. On a mismatch, remove the wrong highlight and search for the text across the document
     (`:775-900`).
  4. If that also fails, report the mark as unanchored (`:645-653`).

  The text search normalises whitespace through an offset map, and treats a block-element
  boundary as a space, because that is what `Selection.toString()` puts there.
- *Raw-HTML surface* (the closest analogue to our pages): a sandboxed iframe with an injected
  "bridge" script (`packages/ui/components/html-viewer/bridge-script.ts`).
  - **Element anchors** use a CSS selector proved unique with a real `querySelectorAll`, built
    from a ladder: `#id`, then an identity attribute, then non-generated classes, then a
    `tag:nth-of-type` path, abandoned past 40 levels (`:2605-2790`).
  - Each anchor carries a **fingerprint** (tag plus a 180-character text snapshot). A *weak*
    selector must still match the snapshot on restore, or it is rejected. An element with no
    text may anchor only by `#id` or `data-*`.
  - Restore order (`:1695-1805`): text inside the resolved element, then text anywhere, then the
    bare element. Text that moved is followed, not pinned to a stale container.
  - Persisted anchors are size-capped and parsed fail-closed, truncated surrogate-safe
    (`packages/core/html-anchor.ts:72-170`).
  - Marks that cannot be placed are reported as an "unanchored" set that excludes whole-page
    comments (`unanchored.ts:9-47`).
- *Re-anchoring on live pages:* a mutation observer re-runs failed searches, gated by a DOM
  generation counter, a 300 ms–5 s backoff and two searches per frame (`:1820-1870`).
- Marks paint as rects in a **shadow-rooted overlay**, so the author's DOM is never wrapped
  (`:19, 1358-1440`). Messages are accepted only from listed origins with a token (`:255-268`).

**(ii) Edge cases worth inheriting.**
- A position that resolves onto the wrong text is worse than no mark. Verify, then fall back.
- Compare with whitespace **removed**, not collapsed: a cross-block quote carries `\n\n` in the
  selection but nothing in the painted text (`:205-212`). Our `comments._tight` reached the same
  rule independently.
- Skip viewer chrome text by node identity when searching (`:1657-1668`).
- Identical siblings defeat structural signatures, so the fingerprint is text, and an element
  with no text fails closed.
- **The weakness to fix:** `findTextRange` takes the *first* occurrence (`:1660-1692`), so a
  repeated quote such as "TBD" binds to the wrong one. Their PR-review path fixes this with
  surrounding context (see D).

**(iii) What does not fit us.** Their host owns the iframe (`srcdoc`). Our page runs on a separate
registrable domain under a strict CSP, the gallery treats everything from the frame as hostile,
and our shim is hand-inlined JavaScript in `content.py`, not a 5,000-line bundle. We want the
ideas at a fraction of the code.

**(iv) Recommendation.**
- Keep `comments.validate_anchor`'s `range` shape (path + start/end) as the fast path. Our shim
  already builds an nth-of-type path plus character offsets (`content.py:155-183`).
- Add optional **`prefix`/`suffix`** fields (32 characters each of text around the quote) to the
  anchor. This is the text-quote-selector idea, and it disambiguates repeated quotes.
- Restore order, run in the shim:
  1. Resolve the path and offsets.
  2. Verify the painted text against `quote` with whitespace removed.
  3. Otherwise, search the whole document for the quote and pick the occurrence whose
     prefix/suffix match best.
  4. Otherwise, the mark is unplaced.
- The gallery sends the shim an `artifact-marks` message (a list of `{id, quote, anchor}`). The
  shim answers with `{placed: [...], unplaced: [...]}` and rects.
- Paint with the **CSS Custom Highlight API** (`CSS.highlights` + `::highlight()`). It paints a
  Range without mutating the agent's DOM and works under our CSP. Fall back to an overlay of
  rects if the API is unsupported.
- Server side, `quote_state` stays the source of truth for "current / changed", so round 2 works
  without a browser. Add `"moved"` when the quote is found but not at the stored path.
- Skip CSS-selector element anchors for now. Our marks are text-based, and a "point" anchor
  already covers images.

---

## B. The feedback export

**(i) Structure** (`packages/ui/utils/parser.ts:1434-1660`):
- `# {title}`, optional `## Reference Images` (`:1462-1471`), then
  `I've reviewed this {subject} and have N pieces of feedback:`. An empty export is
  `No changes detected.` (`:1443`).
- One `## N.` entry per annotation in document order, prefixed `[In diff content] ` for diff-view
  marks (`:1573-1579`):
  - **comment:** `Feedback on: "<quote>"`, then the note as `> ` (`:1264-1272`);
  - **deletion:** `Remove this`, the quote fenced, then `> I don't want this in the {subject}.`;
  - **global:** `General feedback about the {subject}`, then `> ` note;
  - **quick label:** `[Label] Feedback on: "…"` plus the label's tip.
- Element context for pinpoints, `**Attached images:**` paths, and `**Replies:**` nested as
  `- **Reply (author):** …`, unnumbered, indent capped at 8. Orphans and cycles render as normal
  entries, so the count always matches (`:1508-1560`).
- A closing `---` and a `## Label Summary`. Page-controlled strings have backticks defused.

**Message templates** (`packages/shared/prompts.ts:43-72`):
- Denied: `YOUR PLAN WAS NOT APPROVED.`, then "You MUST revise … to address ALL of the feedback
  below before calling {{toolName}} again", then rules including "Do not resubmit the same plan
  unchanged".
- Approved with notes: the notes are "non-blocking guidance, not a request for another
  revision". The agent is told "Do not revise or reopen … solely because of these notes."
- **Why soft wording failed:** their docs say Claude "was tested to respond better to strong,
  directive language … If you soften the tone too much, you may notice the agent ignoring
  feedback or resubmitting unchanged plans"
  (`apps/marketing/src/content/docs/guides/custom-feedback.md:243`). The approve-with-notes text
  errs the other way on purpose, to stop needless revision loops.

**JSON shapes** (`docs/guides/annotate-gates-and-json-responses.md:31-67`;
`apps/hook/server/strict-annotate-result.ts:63-84`): one line on stdout.
- `{"decision":"approved"}`, or with `"feedback"` when approved with notes.
- `{"decision":"annotated","feedback":"<the markdown>"}`.
- `{"decision":"dismissed"}`.

**(ii) Good.** One message for the whole review. The count in the header matches what follows.
The quote comes first so the agent can grep for it. Replies read in order under their parent.
The deletion wording is explicit. Two tones: firm for changes, and "do not reopen" for notes.

**(iii) Not a fit.** The feedback has no per-mark ids, because the agent cannot reply to them. It
says nothing about a mark's state across rounds. It never sanitises bidi or zero-width
characters. And "annotated" merges "changes requested" with "notes only", where we have four
verdicts.

**(iv) Recommendation.** Add a `review.py` renderer that makes one message from the round's
marks, plus a JSON twin returned by a new `GET /api/v1/reviews/{id}`.
- **First line** always begins `Artifacts review:` (never `/`, following artifact-server) and
  carries the verdict, the title, the page id, the version and the round.
- **Changes requested:** a firm block, "You MUST revise the page to address ALL N items below,
  then republish with `artifacts-publish <file> --update <id>`. Do not republish it unchanged."
- **Approved with notes:** the non-blocking wording.
- **Dismissed:** "closed without a verdict; this is not approval."
- **Numbered items, in page order:**
  - `N. Feedback on: "<quote ≤300 chars, collapsed>"`, then `> note`.
  - `N. Remove this: "<quote>"`.
  - `N. Looks good: "<quote>"`, mapped from plannotator's thumbs-up label (`quickLabels.ts:35-45`).
  - `N. General feedback about the page`.
  - Round 2 and later add a state tag: `[still on page]`, `[text changed]` or
    `[addressed/removed]`.
  - Each item ends with `(mark <id>)` so `artifacts-decide reply` can answer it.
- Replies nest under their parent, with no numbers.
- Strip U+202A–202E, U+2066–2069, U+200B–200F, U+2060 and U+FEFF from every human string
  before rendering (artifact-server `docs/agent-bridge-protocol.md:186-195`). `scan._INVISIBLE`
  covers only part of this set.
- **JSON:** `{verdict, round, version, base_version, items:[{n, id, kind, quote, note, state,
  replies:[…]}]}`.

---

## C. The waiting and gate contract

**(i) How they do it** (`annotate-gates-and-json-responses.md`):
- `--gate` adds Approve; `--json` gives the one-line decision; `--hook` emits
  `{"decision":"block","reason":…}` on Send Annotations and nothing on Approve or Close, so notes
  on an approval have no channel (`:94-107`).
- `--require-approval`: 0 only on approved; annotated and dismissed print JSON, then exit 1; a
  misconfigured or failed start exits 2, so a typo never reads as a rejection (`:164-172`).
- `--result-file`: the same bytes go to a `0600` temp file, published by an atomic no-clobber
  hard link, after stdout (`:111-134`; `strict-annotate-result.ts:134-157`).
- **Claude Code `PermissionRequest`** (`apps/hook/server/index.ts:2648-2690`): an allow **must
  echo `tool_input` as `updatedInput`**, or Claude Code ≥ 2.1.199 silently drops it and shows
  its own dialog. Deny carries the firm message. Hook timeout: 345,600 s, 4 days
  (`apps/hook/hooks/hooks.json`).
- **Abandonment:** 30 s after the last tab disconnects, the gate resolves as dismissed. A stale
  tab's later Approve gets an error: "the decision the caller received is the one that counts"
  (`:136-142`; `packages/server/annotate.ts:670-690`).

**(ii) Good.** Exit codes follow the grep convention and keep errors apart from rejections.
Stdout is written before the file. Late actions from other tabs are refused. The hook output
format is documented, including the undocumented Claude Code trap.

**(iii) Not a fit.** Their gate is one short-lived local process per review. Ours is a
persistent, multi-device page, where closing a tab means nothing. **Never infer dismiss from
disconnection.** Dismiss is only an explicit button.

**(iv) Recommendation.** Extend `artifacts-decide`:
- `ask-review <id> [--hold|--live]` creates a review request. It is a `decisions` row of kind
  `review`, so ack, outcome, record and shelve all come for free.
- `wait --review <id>` exit codes:

  | Code | Meaning |
  |---|---|
  | 0 | approved |
  | 4 | approved with notes |
  | 5 | changes requested |
  | 6 | dismissed |
  | 7 | live marks arrived with no verdict yet |
  | 3 | timeout (unchanged) |
  | 1 | refused (unchanged) |
  | 2 | usage (unchanged) |

  Codes 1–3 keep their meaning, so existing callers are unaffected.
- Flags:
  - `--json`: the B JSON on one line.
  - `--result-file` with the atomic-link publish.
  - `--require-approval`: approved and approved-with-notes become 0, and everything else,
    except errors, becomes 1.
  - `--hook stop`: block with the message on 5; empty otherwise.
  - `--hook permission`: echoes `updatedInput`. Document the trap.
- The gallery refuses a verdict on a round that has already closed with **409**, and the
  response shows the verdict that did count. This covers the multi-device version of their
  stale-tab rule.

---

## D. Version diffing and carrying marks across revisions

**(i) How they do it.**
- **History:** `~/.plannotator/history/{project}/{slug}/NNN.md`. A save identical to the latest
  version is skipped (`packages/shared/storage.ts:195-251`).
- **Base:** automatically the immediately previous version (`packages/server/index.ts:188-200`).
  Any earlier version can be picked with `baseVersion` (`:405-415`) from the Version Browser.
- **Markdown diff** (`packages/ui/utils/planDiffEngine.ts`): a jsdiff line diff with
  remove+add grouped as "modified", then a second-pass **word diff** on modified paragraphs,
  headings and list items (like `git diff --word-diff`). Inline code, links, fences and emphasis
  pairs are swapped for sentinels first, so markers never land inside an href or split a fence
  (`:84-250`). Views: block-level (default), word-level, raw (`AGENTS.md:929-945`).
- **HTML diff** (`packages/shared/html-diff.ts`, classic "htmldiff"): tokenise into whole tags,
  whitespace runs and words, with `<script>`/`<style>`/comments opaque and a quoted `>` not
  ending a tag. Diff the tokens by value, then emit the **new** document: removed tags dropped
  to keep the DOM balanced, removed and added text runs wrapped in classed `<del>`/`<ins>`,
  whitespace-only runs unwrapped, author `<ins>`/`<del>` unstyled. It renders as the real page
  (`srcdoc.ts:52-56`), recomputed on reload (`packages/server/annotate.ts:750-780`).
- **Marks across versions:** plan annotations are **not** carried. Drafts are keyed by a content
  hash (`index.ts:157`). Diff-view marks are block-level, tagged `diffContext`. artifact-server
  keeps threads **per version** (`src/storage/postgres-migrations.ts:427`).
- **The only carry-over is PR-mode code review** (`AGENTS.md:457`;
  `packages/review-editor/utils/codeAnnotationAnchor.ts`). A comment records its lines, two lines
  of context either side, and the snapshot id. On a new snapshot it is re-stamped if text and
  context still match, else marked `outdated`: **never dropped, never moved**.

**(ii) Good.** Two-pass diffing gives block structure first and word detail second. Atomic
tokens for things that must not be split. Removed tags dropped, removed text kept. Tagged
wrappers. The outdated-never-moved rule, which matches our own ij8.18 rule in `quote_state`.

**(iii) Not a fit.** Diffing against n−1 is wrong for us: an agent may republish three times
inside one round. Their HTML diff is blind to changes a page makes at runtime from its own
JavaScript. Dropping removed tags means a removed image or table row disappears silently.

**(iv) Recommendation.**
- A new `pagediff.py` (pure Python) tokenises like htmldiff and diffs with
  `difflib.SequenceMatcher(autojunk=False)`. **autojunk must be off**, or common tokens in
  pages of 200+ tokens get ignored. Output wrappers use our own class, `ar-diff-ins` and
  `ar-diff-del`.
- Cap the input tokens, and fall back to "changed blocks" listing when:
  - the cap is exceeded, or
  - the body is mostly `<script>`, the client-rendered case: then say "this page draws itself;
    changes cannot be shown in place".
- For removed non-text elements (`img`, `tr`, `li` with no text), emit a small placeholder
  "removed" chip, so a removal is never silent.
- Serve the result as a derived view on the content origin, `/v/{n}/changes?base={m}`, under the
  same CSP and shim. It is never stored as a version.
- **Base version** = `review_round.base_version`, the version the "changes requested" verdict
  was given on, not n−1. Also offer "compare with any earlier version".
- **Carry-over at round open:**
  - Every open mark is re-evaluated with `quote_state` plus prefix/suffix.
  - `current` marks stay pinned.
  - `changed` marks are listed as "text changed/addressed" with their original `version_n`.
  - Marks whose quote falls inside a `<del>` run show "removed".
  - Nothing is moved, following their PR rule and ours.

---

## E. artifact-server's agent bridge compared with our `/api/v1` events

| Their idea (`docs/agent-bridge-protocol.md`) | Ours | Verdict |
|---|---|---|
| Register upsert on `connectionKey`, scoped to the principal, which keeps the queue across restarts (`:35-63`) | Key per seat; `ARTIFACTS_SESSION` label per publish; no registration | **Adapt:** a key is shared by sessions, so the session label is our connectionKey. |
| Claim long-poll `?wait=25`, **one active claim per agent, 5-min lease**, expired lease requeued inside the claim transaction (`:74-86`; `src/storage/postgres-artifact-repository.ts:3069-3140`) | `/events?since&wait` journal; every waiter sees every row | **Take for verdicts.** Two sessions on one seat can both wake on the same "changes requested" and both revise. A per-round claim with a lease fixes that. |
| The poll **is** the heartbeat; "connected" if seen in the last 90 s (`:82`; `publishing-limits.ts:41`) | `events.note_read` stamps `seat_read` at the start of a read (`events.py:206-270`) | Equivalent. Show it on the review page ("agent listening / last seen"). The window must exceed our hold time plus slack. |
| `delivered` only after the host accepted it; `failed` returns the work to the human (`:88-101, 226-233`) | `acknowledge` (idempotent, first ack counts) plus the `uncollected` alert | **Adapt:** add `review release <id> --reason` so a seat that cannot act hands it back visibly instead of timing out. |
| `addressed` is **inferred** when every thread is resolved; there is no report (`:101`; lazy transition `:4448-4560`) | `needs_reply` is derived from the thread state | **Take:** round state "addressed" = every carried mark is resolved or its quote is gone. Never a separate flag. |
| A queued dispatch fails `agent_unavailable` after 15 min of heartbeat silence | The `uncollected` alert goes to ops | Keep ours, and also show it to Jeremy on the page ("not picked up yet"), following their "don't dress a weaker tier as stronger" rule (`:199-207`). |
| Another principal's agent answers 404 with no disclosure (`:29`) | Identical NotFound everywhere already | Same. |
| At-least-once delivery; idempotent replies with `Idempotency-Key` (`:113, 232`) | The waiter re-looks at start, so nothing is lost; there are no idempotency keys | **Take:** `Idempotency-Key` on `reply` and verdict ack, so a retried call does not post twice. |
| One bundle = one message; never starts with `/`; ids on every item; quotes collapsed to 300; bidi and zero-width stripped (`:155-195`) | The waiter prints rows as they are | **Take** into the B renderer. |
| Hold delivery while the host compacts, inside the lease | n/a (the waiter is a subprocess) | Note in the skill: run `wait` as a background task; a lease covers a compaction mid-hand-off. |

The real weaknesses these fix are:
- two sessions acting on one verdict (claim and lease);
- a seat that cannot act has no visible way to hand the work back (release, or fail);
- the rendered message can carry invisible Unicode (sanitise);
- Jeremy cannot see whether anyone is listening (presence on the page).

---

## Take / Adapt / Skip

| Piece | Take | Adapt | Skip |
|---|---|---|---|
| A. Anchoring | Verify, then text fallback, then unplaced; whitespace-free compare; unplaced report | Prefix/suffix context in our `range` anchor; the Highlight API instead of an overlay | CSS-selector element anchors; mutation-driven re-search |
| B. Export | Numbered entries; `Feedback on:` / `Remove this` / general; nested replies; firm vs non-blocking tones | Four verdicts; mark ids; round state tags; Unicode sanitising | Label summary; images section (for now) |
| C. Gate | Exit codes that keep errors apart; `--json`; atomic `--result-file`; `updatedInput` echo | Per-verdict codes 0/4/5/6/7; 409 on a stale verdict | Dismiss-on-disconnect grace |
| D. Diff | Tag-aware htmldiff; atomic tokens; outdated-never-moved | Base = the round's base version; Python difflib with autojunk off; removed-element chips | n−1 default; carrying marks by content hash |
| E. Bridge | Claim + lease per round; inferred "addressed"; Idempotency-Key; one-message rules | Session label as connectionKey; release instead of failed | Registration ceremony; activity beacon |

## Top 10 edge cases for our tests

1. A quote that appears twice on the page: prefix/suffix picks the right one, and without
   context the mark reports unplaced rather than guessing.
2. A quote spanning two block elements (`\n\n` in the selection) verifies as current.
3. The stored path now resolves onto different text: the mark is not painted there, and the
   text fallback relocates it or reports it unplaced.
4. Emphasis added inside a quoted sentence (`<em>`) stays `current`, not `changed`.
5. htmldiff: a `>` inside a quoted attribute; `<script>`/`<style>` opaque; whitespace-only
   changes produce no wrappers; author `<ins>` left unstyled.
6. htmldiff on a page longer than 200 tokens with repeated tokens: wrong without
   `autojunk=False`.
7. The agent republishes three times in round 2: the diff base stays the round's base version,
   and an identical republish mints nothing.
8. A verdict submitted from a second device after the round closed gets 409 and the
   counted verdict.
9. Two waiters on one seat and one "changes requested": exactly one claims it, and the other
   sees it claimed. An expired lease requeues it.
10. A note containing U+202E, zero-width characters or a leading `/`: the rendered message is
    sanitised, starts with `Artifacts review:`, and the header count equals the numbered items,
    with replies unnumbered and orphan replies still emitted.
