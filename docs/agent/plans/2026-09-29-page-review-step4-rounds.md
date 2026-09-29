# Page review, step 4: rounds and tracked changes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Jeremy requests changes and the agent republishes the page, round 2 opens by itself. The page opens showing what changed since the version he sent back (additions in green, removals struck through). His earlier marks are listed as still on page, text changed, or addressed. The agent's verdict message says which round it is and tags every mark the same way.

**Architecture:**
- A `review_round` table records each round of a review: its version, its base version, and its verdict.
- The review keeps ONE decision row and one id. When a round opens, the row's verdict fields go back to empty, and the old verdict stays in the round's row. Step 2's atomic "first verdict counts" guard (`answer is null`) therefore holds per round, and an agent waiting on the review id simply keeps waiting.
- A new pure-Python `pagediff.py` produces the new document with `<ins>`/`<del>` marked up.
- The page's own origin serves this "changes" view through a frame pass that names its base version.

**Tech Stack:** Python 3.12 (`difflib`, `html.parser`), FastAPI, Postgres, the frame-pass content origin (`content.py`, `frame_pass.py`), and the gallery (`gallery.py`).

**Spec:** `docs/agent/specs/2026-09-28-page-review-design.md`. Relevant parts: Decisions 4 and 5; "What Jeremy sees" (round 2 and later); "Data" (`review_round`, `comment.review_round`); "Diffing"; "Tests" 4–8; "Build order" 4.

## Global Constraints

- **When a round opens.** A new version of a page opens round n+1 only when all of these hold:
  - the page has a review whose latest round's verdict is `changes_requested`;
  - that review is not closed;
  - the new version is a real new version. An identical republish mints no version, so it opens nothing.

  The new round's `version_n` is the new version. Its `base_version` is the version the `changes_requested` was given on, which is the previous round's `version_n`. Further republishes while round n+1 is open move only its `version_n`; its base never moves (spec test 7).
- **Other verdicts end the review.** Approved, approved with notes and dismissed end the review. A later republish opens nothing.
- **What opening a round does to the decision row.** In one transaction: close nothing, and set `answer`, `answer_note`, `answered_at`, `answered_by`, `answer_source`, `recorded_by`, `said_at` and `acknowledged_at` to null on the decision row. The earlier verdict stays in `review_round` (`n`, `verdict`, `closed_at`) and in the event journal. The hold or live mode carries over.
- **Round 1** is created by `ask_review`, with `version_n` set to the current version and `base_version` null.
- **A verdict** closes the current round: it sets `closed_at` and `verdict` on the round, inside the verdict transaction, both for a tap and a relay. A second verdict on the same round still gets 409. The 409 page and body name the round: "This review already has a verdict for round N: …".
- **Marks in a round.** A mark Jeremy makes while a round is open gets `comment.review_round = n`.
- **Carried-mark state, for rounds ≥ 2.** Each open or resolved human root mark from an EARLIER round is one of:
  - `addressed/removed`, if it is resolved or its quote is no longer on the page (`quote_state` "changed" with nothing found, or the quote is gone);
  - `text changed`, if its quote was found but differs;
  - `still on page`, otherwise.

  These are inferred, never flags (spec, "Addressed is inferred"). Use `comments.quote_state` against the round's current version body.
- **The message** (`review.render`):
  - The first line gains `, round N` after the version: `Artifacts review: CHANGES REQUESTED — "<title>" (<page-id>, v<version>, round <N>)`. Round 1 keeps today's exact line, with no round text, so step-2 callers are unchanged.
  - From round 2 on, each carried item is tagged `[still on page]`, `[text changed]` or `[addressed/removed]` after its `(mark <id>)`.
  - The JSON gains `round`, `version`, `base_version` and, per item, `state`. The state is `"new"` for marks made in this round.
  - `GET /api/v1/reviews/{id}` adds `round`.
- **The diff** (`pagediff.py`, pure, no I/O):
  - `render_changes(old_html: str, new_html: str) -> tuple[str, str | None]` returns `(document, fallback_reason)`.
  - Tokens are whole tags, whitespace runs and words. `<script>…</script>`, `<style>…</style>` and `<!-- … -->` are opaque single tokens. A `>` inside a quoted attribute value does not end a tag.
  - Diffing uses `difflib.SequenceMatcher(None, a, b, autojunk=False)`.
  - The output is the NEW document. Inserted text runs are wrapped in `<ins class="ar-diff-ins">`. Deleted text runs are inserted at their position, wrapped in `<del class="ar-diff-del">`. Deleted TAGS are dropped, so the result stays well-formed. A deleted element with no text (for example `<img>` or `<tr>…</tr>` with only tags) leaves `<span class="ar-diff-chip">removed</span>`, so no removal is silent.
  - An author's own `<ins>`/`<del>` tags pass through unchanged, as ordinary tags.
  - **Fallbacks:** if either document has more than 60 000 tokens, or more than 70% of the new document's bytes are inside `<script>`, return a "changed blocks" document instead: the new document with a short banner, `<div class="ar-diff-banner">Showing the page as it now is: <reason></div>`, inserted after `<body>`, where `<reason>` is `too large to mark up changes` or `this page draws itself with script`. The second tuple value carries the same reason.
  - The styles live in the shim's injected stylesheet: `ins.ar-diff-ins{background:rgba(46,157,87,.22);text-decoration:none}`, `del.ar-diff-del{background:rgba(192,57,43,.18);text-decoration:line-through}`, and a small grey chip.
- **Serving the changes view:**
  - The `frame_pass` table gains a nullable `changes_from` column (the base version number).
  - `frame_pass.mint(db, artifact_id, version=None, changes_from=None)`.
  - `Spent` gains `changes_from`.
  - The content route (`/p/{token}/`), when `changes_from` is set, serves `pagediff.render_changes(body_of(changes_from), body_of(live))` with the same CSP, the same stamp and the same shim.
  - The view is never stored as a version.
- **The gallery page view,** when the page's open review is in round ≥ 2:
  - The frame starts in the changes view, with base = the round's `base_version`.
  - The review card shows `Round N`.
  - A `Hide changes` / `Show changes` toggle sits on the review card. It is a link that reloads the page with `?changes=0` or `?changes=1`, so it works with scripts off.
  - A carry-over strip on the card lists earlier-round marks with their state labels: `still on page`, `text changed`, `addressed/removed`.
- **Commit rules:** conventional commits, with no Co-Authored-By or "Generated with" lines. Stage explicit paths. No amend, stash or reset --hard. Run the full suite with `set -o pipefail` and `ARTIFACTS_BROWSER=/usr/lib64/chromium-browser/headless_shell uv run pytest -q -n auto --maxprocesses 8`.

## Review Focus

1. **The round-opening republish races a verdict,** for example Jeremy taps a verdict on round 1 at the moment the agent republishes. The outcome must be one of two things: the verdict lands on round 1 (and, if it is `changes_requested`, round 2 then opens on that republish), or the republish lands first and opens nothing because round 1 has no verdict yet. It must never be a verdict written onto an unopened round 2, or a lost verdict. *Test (Task 1):* the Event-gate interleaving pattern from `tests/test_holds.py`, with a `for update` on the decision row in both paths.
2. **A stale second device taps a verdict after round 2 opened.** Opening round 2 cleared the answer, so a stale tab's verdict would land on round 2 even though he meant round 1. The verdict form must carry the round number it was rendered for. `answer` must refuse, with 409 naming the current round, when that round is not the current one. The API relay (`record`) accepts an optional `round`. *Test (Task 1).*
3. **The diff breaks the page.** Examples: unbalanced tags, a `>` in an attribute, a script split across tokens, repeated tokens on a long page (autojunk), or whitespace-only changes producing noise. *Tests (Task 2),* per spec tests 5 and 6: whitespace-only changes produce no `ins`/`del`, and a 20 000-word page with one changed word marks exactly that word.
4. **Emphasis added inside a quoted sentence makes a carried mark read "text changed" or "addressed"** when the words are all still there. It must read `still on page` (spec test 4). `quote_state` compares text with tags stripped, and whitespace tightened. *Test (Task 3).*
5. **Held marks and rounds.** A mark held in round 1 is released by that round's verdict (step 3), so it is carried into round 2 like any other. A mark made while round 2 is held stays held until round 2's verdict. *Test (Task 1 or 3).*

---

### Task 1: review rounds — open, close, refuse the stale

**Files:**
- `src/artifacts/schema.sql`: append the `review_round` table, with `review_id` referencing `decision(id) on delete cascade`, `n int`, `version_n int`, `base_version int null`, `opened_at`, `closed_at`, `verdict text null`, and `primary key (review_id, n)`. Also append `alter table comment add column if not exists review_round int`. Leave out `claimed_by`, `claim_expires_at` and `released_reason`; they are step 5.
- `src/artifacts/decisions.py`:
  - `ask_review` inserts round 1.
  - `answer` and `record_elsewhere`, for a review, close the current round inside the verdict transaction.
  - `answer` gains an optional `round: int | None` that must equal the current round, or it raises `AlreadyDecided(row)` so the caller renders 409 with the current round.
  - Add `current_round(conn, review_id) -> RoundRow`.
- `src/artifacts/store.py`: after a real new version is committed, in the same transaction as the version insert, open or advance the round per the Global Constraints. Take the decision row `for update` first; lock order is decision, then round.
- `src/artifacts/comments.py`: `add` stamps `review_round` with the open round's n.
- `src/artifacts/gallery.py`: the verdict form carries a hidden `round`, and the 409 page names the round.
- `src/artifacts/api.py`: `RecordBody.round: int | None`, and `GET /reviews/{id}` adds `round`.
- Tests: `tests/test_rounds.py`.

- [ ] Failing tests:
  - round 1 on ask;
  - changes requested, then republish, opens round 2 with the right base and cleared answer fields;
  - approved, then republish, opens nothing;
  - an identical republish opens nothing;
  - three republishes in round 2 keep its base (spec test 7);
  - the verdict closes the round;
  - a second verdict gets 409;
  - a stale round's form gets 409 naming the current round (Review Focus 2);
  - the race (Review Focus 1);
  - a mark made in round 2 gets `review_round=2`;
  - held mode carries over.
- [ ] Implement, run the suite, commit `feat(review): a republish after changes requested opens the next round, and a stale verdict is refused`.

### Task 2: `pagediff.py` — the page with its changes marked

**Files:**
- Create `src/artifacts/pagediff.py`, with `render_changes` as specified.
- Create `tests/test_pagediff.py`.

- [ ] Failing tests:
  - a word changed produces one `del` and one `ins`;
  - whitespace only produces no marks;
  - a `>` inside `title="a>b"` stays one tag;
  - script, style and comments are opaque and never diffed inside;
  - an author's own `<ins>`/`<del>` passes through;
  - a removed `<img>` leaves a chip;
  - a removed table row with text shows the text struck through;
  - the output is well-formed, checked by parsing it with `html.parser` with no unclosed-tag drift beyond the original;
  - a 20 000-word page with one change marks exactly one word (autojunk off);
  - more than 60 000 tokens falls back with its reason;
  - a mostly-script page falls back with its reason;
  - the fallback banner is inserted after `<body>`.
- [ ] Implement, commit `feat(review): mark up what changed between two versions of a page`.

### Task 3: the changes view, the carry-over, and the round-aware message

**Files:**
- `src/artifacts/frame_pass.py` and `schema.sql`, for `changes_from`.
- `src/artifacts/content.py`: serve the changes view, and add the diff styles to the shim's stylesheet.
- `src/artifacts/gallery.py`:
  - the page view mints a changes pass in round ≥ 2 unless `?changes=0`;
  - the card shows `Round N`, the toggle and the carry-over strip.
- `src/artifacts/review.py`: the round in the first line, the item tags, and the JSON fields.
- `src/artifacts/comments.py`: a helper `carried_state(row, current_body) -> str`.
- Tests:
  - the content route serves ins/del for a changes pass, and never for a normal pass;
  - a changes pass for another page is refused;
  - the gallery in round 2 frames the changes view by default, and `?changes=0` does not;
  - the strip labels;
  - `review.render` in round 2 carries tags, the first line has `round 2`, and round 1's first line is unchanged byte for byte;
  - emphasis added inside a quote stays `still on page` (Review Focus 4);
  - a real-browser check in `tests/test_panel_browser.py` style: round 2 shows green insertions in the frame.
- [ ] Implement, commit `feat(review): round 2 opens on what changed, with his earlier marks accounted for`.

### Task 4: tell the agents

**Files:**
- `src/artifacts/news.py`: add a new first entry, `("2026-09-29e", "When Jeremy requests changes and you republish the page, round 2 of the same review opens by itself: keep waiting on the same review id. The verdict message now says the round and tags each earlier mark [still on page], [text changed] or [addressed/removed].")`.
- `src/artifacts/onboarding.py`: add one sentence after the review lines in the skill: "After changes requested, republish the page and run the same wait --review again: the next round opens by itself."
- Test: `tests/test_tools.py` asserts that sentence.
- [ ] Commit `docs(skill): agents learn that a republish opens the next round`.

The controller merges, pushes and deploys, then proves it live on a throwaway page:
1. `ask-review`, then a relayed `changes_requested`.
2. Republish with one changed sentence.
3. `GET /reviews` shows round 2, and `wait --review` waits again.
4. A relayed verdict gives a message with `round 2` and tags.
5. The page view in the pod frames a changes pass whose body contains `ar-diff-ins`.

## Self-review

- **Spec coverage, step 4:**
  - `review_round`, auto-opening rounds, and `comment.review_round`: Task 1.
  - `pagediff`: Task 2.
  - The changes view, the hide toggle, the carry-over strip and the round message: Task 3.
  - Stale second device: Task 1.
- **Left for step 5:** the claim/lease (exit 8), presence, and `review-release`. Also left: the "changed blocks" rendering as a real list of blocks. The fallback here is the page as it now is, with the reason stated, which the spec allows as the fallback wording.
- **Placeholders:** none. Every string, exit condition and name is given.
- **Types:** `render_changes -> (str, str|None)`, `mint(..., changes_from=)`, `Spent.changes_from`, `current_round`, `carried_state`, `RecordBody.round` and `answer(round=)` are used consistently across the tasks.
