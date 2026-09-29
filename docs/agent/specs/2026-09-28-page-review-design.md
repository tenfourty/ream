# Page review — design (28 Sep 2026)

Tracker: homelab-ij8.108. Research: `docs/agent/research/2026-09-28-plannotator-study.md`
(plannotator is MIT OR Apache-2.0; Jeremy ruled **ideas only, no copied code**).

## The outcome

Jeremy can review any page on his wall the way plannotator lets him review a plan, from any
device, over several rounds. The agent that published the page gets one clear, numbered
message and an exit code it can act on without reading prose.

## Decisions already made (mirrored and confirmed, 28 Sep)

1. **Marks on every page, invited or not.** The existing quoted comment, plus *remove this*,
   *looks good* and *a comment on the whole page*.
2. **Verdicts only where the agent asked for a review:** approved, approved with notes,
   changes requested, dismissed.
3. **Hold or live.** On a reviewed page, a switch sets whether marks reach the agent as made or
   are held until the verdict. It remembers his last choice across all his devices, and the
   first ever is *held*.
4. **Rounds open by themselves.** When the agent republishes a page he sent back, round 2
   opens.
5. **Tracked changes in place.** A revised page shows additions in green and removals struck
   through. Earlier notes are marked *changed* or *addressed*.
6. **One numbered message plus data** for the agent, and a distinct exit code per verdict.
7. **Build on what exists.** A review request is a kind of decision, and a mark is a kind of
   comment. The plan-mode hook comes later, as a thin source.

## What Jeremy sees

- **Select text on any page** and the existing comment popover offers three actions:
  *Comment*, *Remove this* and *Looks good*. A whole-page comment lives in the page's comment
  panel, where page-level comments already sit.
- **A page under review** has a review bar at the foot of the page, with:
  - the round;
  - the hold/live switch;
  - a count of held marks;
  - "agent listening / last seen 3 min ago";
  - four verdict buttons.

  *Approved with notes* and *Changes requested* send the held marks with the verdict.
  *Approved* with marks still held is recorded as *approved with notes*, so a note is never
  lost behind a plain approval. *Dismissed* sends only the dismissal; held marks are kept and
  shown to the agent as context.
- **Held marks** are visible to him (dotted outline) and invisible to the agent until the
  verdict. Switching to *live* releases them at once.
- **Round 2 and later** open the page in its *changes* view:
  - The page as it now is, with `ins` / `del` from the version he sent back, not from the
    previous version.
  - A toggle, *hide changes*.
  - A strip listing the earlier marks: *still on page*, *text changed* or *addressed/removed*.
  - A page that draws itself with script, or is too large to diff, says so in words and falls
    back to a list of changed blocks.
- **A verdict from a stale second device** (the round is already closed) is refused, and he is
  told which verdict counted and when.

## What the agent does

```bash
artifacts-decide ask-review <page-id> [--live|--hold]     # default: his remembered choice
artifacts-decide wait --review <review-id> [--json] [--result-file F] [--require-approval] [--hook stop|permission]
artifacts-decide review-release <review-id> --reason "…"  # hand it back, visibly
artifacts-decide reply <mark-id> "…"                      # unchanged; now per mark
```

**Exit codes** for `wait --review`. Codes 1, 2 and 3 keep today's meaning, so existing callers
are unaffected.

| code | meaning |
|---|---|
| 0 | approved |
| 4 | approved with notes |
| 5 | changes requested |
| 6 | dismissed |
| 7 | live marks arrived, no verdict yet |
| 8 | another session on this seat has claimed the round |
| 3 | timeout · 1 refused · 2 usage |

`--require-approval` collapses the verdicts: 0 and 4 become 0, and 5, 6, 7 and 8 become 1. Errors
stay 1 or 2 as today, but print `REFUSED`, so a typo never reads as a rejection.

**The message** is built by a new `review.py`, which is also exposed as
`GET /api/v1/reviews/{id}`:

- **First line:** `Artifacts review: CHANGES REQUESTED — "<title>" (<page-id>, v7, round 2)`.
  The message never starts with `/`.
- **Changes requested** adds a firm block: "You MUST revise the page to address ALL N items
  below, then republish it with `artifacts-publish <file> --update <id>`. Do not republish it
  unchanged." Plannotator found that agents ignore softer wording.
- **Approved with notes:** "These notes are non-blocking guidance. Do not revise solely
  because of them."
- **Dismissed:** "Closed without a verdict. This is not approval."
- **The numbered items, in page order:**
  - `N. Feedback on: "<quote>"` then `> note`
  - `N. Remove this: "<quote>"`
  - `N. Looks good: "<quote>"`
  - `N. General feedback about the page`
- **Round 2 and later** tag each item `[still on page]`, `[text changed]` or
  `[addressed/removed]`, and every item ends `(mark <id>)` so the agent can reply to it.
- Replies nest under their mark, without numbers. The header's count always equals the
  numbered items, and orphan replies are still emitted.
- Every human string loses bidi and zero-width characters (U+202A–202E, U+2066–2069,
  U+200B–200F, U+2060, U+FEFF), and quotes are collapsed to 300 characters.
- **The JSON twin** is
  `{verdict, round, version, base_version, items:[{n,id,kind,quote,note,state,replies}]}`.

**Hooks:**

- `--hook stop` prints `{"decision":"block","reason":<message>}` on 5, and nothing otherwise.
- `--hook permission` emits Claude Code's `PermissionRequest` shape. On allow it **must echo
  `tool_input` as `updatedInput`**, because Claude Code 2.1.199 and later silently drops an
  allow without it.

This is the seam the later plan-mode hook uses.

## Data

- **`decision.kind`:** `'question' | 'review'`, default `question`. A review row has fixed
  options (`approved`, `approved_with_notes`, `changes_requested`, `dismissed`) and reuses
  ack, outcome, record, shelve and the uncollected alert unchanged.
- **New `review_round` table:**

  | column | holds |
  |---|---|
  | `review_id` | the review's decision row |
  | `n` | the round number |
  | `version_n` | the version under review |
  | `base_version` | the version the previous round's *changes requested* was given on |
  | `opened_at` / `closed_at` | when the round opened and closed |
  | `verdict` | the verdict, when given |
  | `claimed_by` / `claim_expires_at` | the per-round lease (see Delivery) |
  | `released_reason` | why a seat handed it back |

- **`comment.kind`:** `'comment' | 'remove' | 'looks_good'`, default `comment`. A page comment
  is the existing `anchor.kind = 'page'`.
- **`comment.held`:** a boolean. A held mark is excluded from the agent's reads and events
  until release.
- **`comment.review_round`:** nullable; set on marks made while a round is open.
- **The anchor** gains optional `prefix` and `suffix`: 32 characters of text either side of
  the quote. The existing path plus offsets stay the fast path.
- **`quote_state`** gains `moved`: the quote was found, but not at the stored path. Marks are
  never moved on the server; they are re-evaluated.
- **The hold/live preference** is one row in a small `human_pref` table: key `review_mode`,
  value `held|live`, updated whenever he flips the switch. It is server-side, so every device
  agrees.

## Anchoring and painting (in the page shim, `content.py`)

The gallery posts `artifact-marks` (a list of `{id, quote, anchor}`) into the frame, and the
shim answers `{placed, unplaced}`. For each mark:

1. Resolve the path and offsets.
2. Verify the text against the quote, **with whitespace removed** (our `_tight` rule, which
   plannotator shares).
3. On failure, search the document for the quote and pick the occurrence whose prefix and
   suffix match best.
4. If several match equally, or none does, the mark is **unplaced**. It is never guessed.

Painting uses the **CSS Custom Highlight API** (`CSS.highlights`), which colours a range without
changing the agent's DOM and works under our CSP. Without it, the shim paints nothing and reports
nothing -- ruled by the controller on final review, since every current browser has the API and a
silent no-op is safer than a half-built fallback. A rects fallback (absolutely positioned boxes,
for a browser with no Highlight API) is deferred, not built. The shim stays small, hand-inlined,
and accepts messages only from the gallery origin, as today.

## Diffing (new `pagediff.py`, pure Python)

- **Tokenising:** htmldiff-style, into whole tags, whitespace runs and words. `script`,
  `style` and comments are opaque, and a `>` inside a quoted attribute does not end a tag.
- **Diffing:** `difflib.SequenceMatcher(autojunk=False)`. autojunk must be off, or long pages
  diff wrongly.
- **Output:** the **new** document. Removed tags are dropped to keep it well-formed, removed and
  added text runs are wrapped in `<del class="ar-diff-del">` / `<ins class="ar-diff-ins">`, and a
  removed element with no text (an image or a table row) leaves a small *removed* chip, so no
  removal is silent.
- **Serving:** a derived view on the content origin, `/v/{n}/changes?base={m}`, with the same CSP
  and shim. It is never stored as a version. A token cap and a mostly-script check fall back to
  "changed blocks" with the reason said.

## Delivery (lessons from plannotator's artifact-server)

- **One session acts on a verdict.**
  - `wait --review` *claims* the round with a five-minute lease, renewed while the process
    lives.
  - A second waiter on the same seat sees "claimed by <session>" and exits with that
    information (exit 8), rather than both revising.
  - An expired lease is requeued.
- **"Addressed" is inferred:** every carried mark is resolved, or its quote is gone. It is never
  a flag.
- **`Idempotency-Key`** on reply and verdict-ack, so a retried call never posts twice.
- **Presence:** the round shows the claiming session's last read, from `events.note_read`, which
  already exists.
- **Release:** a seat that cannot act runs `review-release`, and Jeremy sees why, instead of
  a silent timeout.
- **Closing a tab never means anything.** Dismiss is a button.

## Security

- **Marks stay in the gallery.** They are drawn from the gallery, and the shim receives quotes,
  never note text, so an agent page cannot read his notes.
- **Verdicts:**
  - verdict buttons are gallery-origin controls behind the same-origin write guard, as answers
    are;
  - an agent can **ask** for a review, **read** the verdict, and **record** a relayed one with
    its source, but never give one;
  - a stale verdict gets 409.
- **Scanning:** marks and notes go through the size cap and both secret scanners, exactly as
  comments do today. The anchor's `prefix`/`suffix` do not: each is capped at 64 characters
  (`ANCHOR_CONTEXT_MAX`) on the assumption that it is taken verbatim from the page's own
  already-published body, which was scanned when that version was published -- re-scanning a
  slice of text the service already served and already scanned would add a scan to every mark for
  no new risk caught. The server never checks that assumption: nothing compares `prefix`/`suffix`
  against the page they claim to quote, so a keyed API client -- never the gallery form, which only
  ever sends what the shim actually read off the page -- could store up to 64 characters of its
  own choosing, unscanned, in either field.

## Build order

1. **Marks everywhere:** comment kinds, prefix/suffix anchors, and Highlight-API painting.
2. **Review requests and verdicts:** the `decision.kind`, the review bar, `review.py` (the
   message and JSON), `wait --review` with codes, `--json`, `--result-file` and
   `--require-approval`.
3. **Hold/live:** `comment.held`, the preference row, and release on verdict or flip.
4. **Rounds and tracked changes:** `review_round`, `pagediff.py`, the changes view, and
   carry-over states.
5. **Delivery hardening:** claim and lease, idempotency keys, presence, and release.
6. **Later:** the plan-mode hook (`--hook permission`), the terminal tool and the herdr plugin
   (homelab-ij8.109).

Each step ships on its own and is proved live before the next.

## Tests (beyond each step's own)

These come from the study, and each is a test we would otherwise have to rediscover:

1. A repeated quote is placed correctly by its context; without context it is reported
   unplaced.
2. A quote spanning two blocks verifies.
3. A stored path now resolving onto different text is not painted there.
4. Emphasis added inside a quoted sentence stays *still on page*.
5. htmldiff handles a `>` inside an attribute, opaque script and style, whitespace-only changes,
   and an author's own `ins` tags.
6. A long page with repeated tokens diffs correctly (autojunk).
7. Three republishes in one round keep the round's base, and an identical republish mints
   nothing.
8. A second-device verdict after close gets 409 and the counted verdict.
9. Two waiters on one seat: exactly one claims the round, and a lease that expires requeues it.
10. U+202E, zero-width characters or a leading `/` in a note are sanitised, the message starts
    `Artifacts review:`, and the counts match.

## Not doing

- Copying plannotator code.
- Dismissing a review because a tab disconnected.
- Diffing against the previous version by default.
- Moving marks.
- CSS-selector element anchors.
- A second front-end stack in the gallery.
