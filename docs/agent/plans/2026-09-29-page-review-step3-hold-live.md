# Page review, step 3: hold or live — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On a page under review, Jeremy's marks are held back from the agent until his verdict, or sent as he makes them. A switch on the review card sets which, remembers his choice on every device, and starts at *held*.

**Architecture:**
- The mode lives in two places:
  - a per-review `decision.review_mode`, set when the review is asked for and flipped by the switch;
  - one server-side preference row, `human_pref('review_mode')`, which is the default for the next review.
- A mark Jeremy writes while its page has an open review in *held* mode is stored with `comment.held = true`. It gets no event and no nudge. Every agent-facing read leaves it out.
- A new `holds.py` owns the preference, the page's current mode and `release()`.
- Release happens:
  - on a verdict, tapped or relayed;
  - on flipping to *live*;
  - when the review is closed or shelved.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3 and Postgres, with the stdlib-only CLI in `decide.py`. Tests run under pytest against a real Postgres.

**Spec:** `docs/agent/specs/2026-09-28-page-review-design.md`, in particular:
- "Decisions already made" 3;
- "What Jeremy sees", on the review bar and held marks;
- "Data": `comment.held` and `human_pref`;
- "Build order" 3.

## Global Constraints

- **Mode values:** exactly `held` and `live`. The preference key is `review_mode`. With no row, the default is `held` ("the first ever is *held*").
- **The mode is server-side, never a cookie or localStorage**, so every device agrees.
- **What is held:** only a mark written by Jeremy (`author_kind='human'`) while its page has an OPEN review (`kind='review'`, `answer is null`, `closed_at is null`) whose `review_mode` is `held`. A reply he writes in such a thread is held too. Agent writes are never held.
- **A held mark is invisible to the agent:**
  - `GET /api/v1/artifacts/{id}/comments` and `GET /api/v1/comments` leave it out;
  - no `comment_added` event is written and no nudge is sent;
  - `wait --review` never counts it for exit 7;
  - an agent reply to it, or an agent resolve of it, answers exactly like a missing thread: `NoSuchThread` or `NotFound`, the same 404 as today.
- **A held mark is visible to Jeremy:** in the gallery's comment panel, tagged `held`, and painted on the page with a dotted underline.
- **Release sets `held = false` on every held comment of the page:**
  - **On a verdict** (`decisions.answer` or `decisions.record_elsewhere` on a review), the release writes NO `comment_added` events, because the verdict's message already carries every mark.
  - **On a flip to live, or when the review is closed or shelved**, the release writes one `comment_added` event per released ROOT mark, with the same summary, note and session fields `comments.add` writes today. It sends no nudge.
- **Approved with held marks:** a review answered `a` (Approved) while any OPEN held human ROOT mark exists on the page is stored as `b` (Approved with notes). This applies to the tapped and the relayed path alike, and the check runs inside the same transaction as the verdict.
- **Dismissed** releases the held marks like any other verdict, so the agent sees them as context.
- **The switch** is a gallery-origin control behind `_same_origin_write`. Its route is `POST /d/{decision_id}/mode`, form field `mode`. It 303s back to `/a/{artifact_id}`.
- **The agent's equivalent** of the switch is `artifacts-decide ask-review <page> --live|--hold`. The agent chooses for its own review when asking; after that, only Jeremy flips it. This is recorded as a ruling: the switch is how *he* reviews, and an agent turning his hold off would be acting as him.
- **Copy, verbatim:**
  - switch, held state: `Holding your marks until your verdict` with button `Send them as I go`;
  - switch, live state: `Sending your marks as you make them` with button `Hold them until my verdict`;
  - held count: `N mark held` / `N marks held`, shown only when N > 0;
  - tag in the comment panel: `held`;
  - card hint with held marks: `Approved with held marks counts as Approved with notes.`
- **Commit rules:** conventional commits, no Co-Authored-By or "Generated with" lines (the repo guard refuses them), explicit pathspecs, no `--amend`, no `stash`, no `reset --hard`.

## Review Focus

1. **A held mark leaks through a read nobody listed.** Every agent-facing path must leave it out: the per-page list, the all-pages list, the events feed, the review waiter, and the uncollected/digest count if it counts comments. *Test (Task 1):* one test walks every agent-facing read after a held mark is written and asserts the mark's id appears in none.
2. **Held marks are stranded forever.** The review is closed by the agent, or the page is archived or deleted with a held review open, and nothing releases the marks. *Test (Task 2):* close the review, and separately archive the page; both release with events.
3. **Two devices race a flip and a verdict.** Approved is tapped while another tab flips to live. The verdict must be exactly one of Approved (if the marks were already live) or Approved with notes (if they were still held), never Approved with marks silently dropped. *Test (Task 2):* the verdict's held-check and the release run in one transaction, and the flip's release in another; interleave them with the monkeypatch pattern used in `test_reviews.py::test_two_simultaneous_verdicts_keep_only_the_first`.
4. **The switch is flipped on a settled review, or on a question.** A flip on an answered or closed review, or on a `kind='question'` decision, is refused with 409 (settled) or 404 (not a review). It never touches the preference. *Test (Task 3).*
5. **The agent learns a mark exists by guessing its id.** A reply or resolve by the agent on a held thread gives the same 404 body as a random id. *Test (Task 1).*

---

### Task 1: held marks — the column, the preference, and invisibility to the agent

**Files:**
- Modify: `src/artifacts/schema.sql`, appending at the end:
  ```sql
  -- HOLD OR LIVE (page review step 3, homelab-ij8.108). A mark Jeremy makes
  -- while its page is under review in 'held' mode is invisible to the agent
  -- until his verdict or until he switches to 'live'.
  alter table comment add column if not exists held boolean not null default false;
  alter table decision add column if not exists review_mode text
      check (review_mode in ('held', 'live'));
  create table if not exists human_pref (
      key        text primary key,
      value      text not null,
      updated_at timestamptz not null default now()
  );
  ```
- Create: `src/artifacts/holds.py`
- Modify: `src/artifacts/comments.py`:
  - add `comment.held` to `_COLUMNS` and `held: bool = False` to `CommentRow`;
  - update `_row_to_comment`;
  - make `add` hold the mark, and skip the event and nudge when held;
  - make the replies and `resolve` treat a held thread as missing when `agent` is given;
  - listing: `list_for_artifact(..., include_held: bool = False)`; `list_for_agent` always excludes held; `across_pages` includes held; `marks_for_page` includes held and returns a `held` key.
- Modify: `src/artifacts/decisions.py`: `ask_review(..., mode: str | None = None)` stores `review_mode = mode or holds.get_mode(conn)`. Add `review_mode` to `_COLUMNS` and `DecisionRow` (`review_mode: str | None = None`) without disturbing the existing bare indexes. `_SESSION_IDX` and `_KIND_IDX` must still point at the same columns, so append the new column at the END.
- Modify: `src/artifacts/gallery.py`: the two `comments.list_for_artifact` callers (about line 3780) pass `include_held=True`. `review.py` keeps the default. Every held mark is released before a verdict renders, and leaving it out is the safe default.
- Test: create `tests/test_holds.py`.

**Interfaces:**
- Produces, in `holds.py`:
  - `MODES = ("held", "live")`
  - `get_mode(conn) -> str`: the preference, `"held"` when no row exists.
  - `set_mode(conn, mode: str) -> None`: an upsert into `human_pref`. Raises `ValueError` for a mode not in `MODES`.
  - `page_mode(conn, artifact_id: str) -> str | None`: `"held"` if ANY open review on the page has `review_mode = 'held'`, `"live"` if open reviews exist and none is held, `None` if there is no open review.
  - `release(conn, artifact_id: str, *, emit: bool) -> list[str]`: sets `held = false` on every held comment of the page and returns the released ids. With `emit=True`, it appends one `comment_added` event per released ROOT comment, built exactly as `comments.add` builds its event: owning agent, actor = the author, the kind-label summary and the page session. Move that summary-building into a helper in `comments.py`, `_event_summary(kind, quote, body) -> str`, and use it from both places rather than duplicating it. Runs inside the caller's transaction.
- Consumes: `events.append`, and `store._owned` as `comments.add` already uses it.

- [ ] **Step 1: Failing tests** in `tests/test_holds.py`, reusing `_page`/`_human`-style helpers from `tests/test_reviews.py` (copy them; do not import across test files):
  - `test_the_first_ever_mode_is_held`: `get_mode` gives `"held"` with no row, and `set_mode(conn, "live")` then gives `"live"`. `set_mode(conn, "loud")` raises `ValueError`.
  - `test_a_review_takes_his_remembered_mode_unless_the_agent_chose`: with no preference, `ask_review` stores `"held"`. After `set_mode("live")`, the next stores `"live"`. `ask_review(mode="held")` stores `"held"` whatever the preference is.
  - `test_his_mark_on_a_held_review_is_held_and_silent`: open a held review, and have `_human` add a `remove` mark. Then `row.held is True`, no `comment_added` event exists for it (query the event table by `ref_id`), and `nudge.emit` was not called (monkeypatch it to record calls).
  - `test_his_mark_without_a_review_or_on_a_live_review_is_not_held`: both cases give `held is False` and an event exists.
  - `test_an_agent_write_is_never_held`: an agent reply on a held-review page to a non-held thread gives `held False`.
  - `test_a_held_mark_is_in_no_agent_read` (Review Focus 1): after a held mark,
    - its id is absent from the API `GET /api/v1/artifacts/{id}/comments` and from `GET /api/v1/comments` (use the TestClient and minted-key pattern from `tests/test_answer_loop.py`);
    - it is absent from `GET /api/v1/events?since=0`;
    - it is absent from `decisions.uncollected`/digest if any comment count is involved (grep `comment` in `decisions.py` and `store.py` for any count, and assert on each one you find, naming it in the test's docstring);
    - it IS present in `comments.list_for_artifact(..., include_held=True)`, in `across_pages`, and in `marks_for_page` with `"held": True`.
  - `test_the_agent_cannot_reach_a_held_thread_by_its_id` (Review Focus 5): through the API, an agent reply with `thread_root=<held id>` and an agent resolve of `<held id>` return the same status and body as with a random 26-character id.
- [ ] **Step 2: Run them and watch them fail** with `uv run pytest -q tests/test_holds.py`.
- [ ] **Step 3: Implement** the schema, `holds.py` and the `comments.py`/`decisions.py`/`gallery.py` edits above. In `comments.add`, compute `held = author_kind == "human" and holds.page_mode(conn, artifact_id) == "held"` inside the same connection, before the insert. When held, write no event, leave `event_seq` None, and send no nudge.
- [ ] **Step 4: Run** `uv run pytest -q tests/test_holds.py tests/test_reviews.py tests/test_comments*.py tests/test_answer_loop.py`, then the full suite with `ARTIFACTS_BROWSER=/usr/lib64/chromium-browser/headless_shell uv run pytest -q`. Finish with `uv run ruff format src tests` and `uv run ruff check src tests`.
- [ ] **Step 5: Commit:** `git commit -m "feat(review): his marks on a held review stay out of the agent's sight"` with explicit paths.

### Task 2: release — on a verdict, on a flip, and when the review ends

**Files:**
- Modify: `src/artifacts/holds.py`: add `set_review_mode(db, decision_id: str, mode: str) -> tuple[str, int]`.
- Modify: `src/artifacts/decisions.py`:
  - in `answer` and `record_elsewhere` for a review, inside the existing transaction and BEFORE the conditional update, turn `a` into `b` when held open human roots exist. After a successful update, call `holds.release(conn, artifact_id, emit=False)`;
  - in `close`, call `holds.release(conn, artifact_id, emit=True)` when the closed decision is a review.
- Modify: `src/artifacts/store.py`: `_shelve_open_questions` calls `holds.release(conn, artifact_id, emit=True)` when any shelved row is a review. The import must not create a cycle; import inside the function if needed.
- Test: `tests/test_holds.py`.

**Interfaces:**
- Produces `holds.set_review_mode(db, decision_id, mode) -> tuple[str, int]`, which returns `(artifact_id, released_count)`. In ONE transaction it:
  - selects the decision `for update`;
  - raises `store.NotFound` if the decision is missing or is not `kind='review'`;
  - raises `decisions.AlreadyDecided(row)` if `answer is not null or closed_at is not null`, importing inside the function to avoid a cycle;
  - sets `decision.review_mode = mode` and calls `set_mode(conn, mode)`;
  - if `mode == "live"`, calls `release(conn, artifact_id, emit=True)`;
  - raises `ValueError` for a bad mode.
- Consumes: Task 1's `holds.release`, `holds.set_mode` and `holds.page_mode`.

- [ ] **Step 1: Failing tests:**
  - `test_a_verdict_releases_his_held_marks_without_extra_events`: on a held review with two held marks, answer `c`. Both are now `held False`, there are zero `comment_added` events for them, and `review.render` lists both.
  - `test_approved_with_held_marks_counts_as_approved_with_notes`: answer `a` with one held mark, and the stored answer is `b`. Do the same through `record_elsewhere`. With no held marks, `a` stays `a`. A RESOLVED held mark does not count.
  - `test_dismissed_releases_them_as_context`: answer `d` and the marks are released. `review.render` still lists them.
  - `test_flipping_to_live_releases_with_events_and_remembers`: `set_review_mode(db, rid, "live")` returns `(page_id, 2)`. One `comment_added` event exists per released root and none for a released reply. `get_mode` is `"live"`, and a mark written afterwards is not held.
  - `test_flipping_back_to_held_holds_new_marks_only`: flip to live, write a mark (not held), flip to held, and write another (held). The first stays unheld.
  - `test_a_settled_or_non_review_decision_cannot_be_flipped` (Review Focus 4): flipping an answered review raises `AlreadyDecided`, and a closed one does too. A question raises `NotFound`. In all three, the preference is unchanged.
  - `test_closing_or_archiving_releases_held_marks` (Review Focus 2): `decisions.close` on a held review releases with events. Separately, `store.set_archived` (find the real name near line 1305) on a page with a held review releases with events.
  - `test_a_flip_racing_approved_never_drops_a_mark` (Review Focus 3): monkeypatch so that `answer(a)` runs its held-check, then `set_review_mode(live)` commits from a second thread, then the answer proceeds. It is whichever the database serialises, but the invariant under test is: the stored answer is `b` if any mark was held when the verdict row was written, and in every interleaving, every mark is released and appears in `review.render`. Lock the decision row `for update` at the start of the answer transaction for reviews, so the two serialise on the row.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Implement.** `answer` and `record_elsewhere` must take `select … for update` on the decision row for a review BEFORE the held-check, so a flip and a verdict cannot interleave.
- [ ] **Step 4: Run** the Task 1 test set plus `tests/test_record_answer.py` and `tests/test_decisions*.py`, then the full suite, format and lint.
- [ ] **Step 5: Commit:** `git commit -m "feat(review): his held marks go to the agent with his verdict, or at once when he switches to live"`.

### Task 3: the switch on the review card, and held marks drawn for him

**Files:**
- Modify: `src/artifacts/gallery.py`:
  - add the route `POST /d/{decision_id}/mode`;
  - in `_card`, for an OPEN review, add the switch, the held count and the approved hint (Global Constraints copy);
  - in the comment panel, add the `held` tag on held rows;
  - in the CSS, style the tag. Match the existing `.rtag` style in a muted colour, following the page's tokens.
- Modify: `src/artifacts/content.py`, the page shim. A mark whose `held` is true is painted in a separate highlight, `ar-held`, with `text-decoration: underline dotted` and no background, instead of the normal `ar-mark` fill. Find the existing `CSS.highlights` code and the `::highlight(...)` rules and follow them.
- Test: `tests/test_holds.py`, plus the shim's existing string or real-browser tests where the shim's CSS is asserted (`grep -n "highlight" tests/*.py`).

**Interfaces:**
- Consumes: `holds.set_review_mode`, and `comments.list_for_artifact(include_held=True)` rows with `.held`. From `marks_for_page`, dicts with a `"held"` key.
- The route:
  ```python
  @router.post("/d/{decision_id}/mode")
  def review_mode(decision_id: str, mode: str = Form(...), _guard: None = Depends(_same_origin_write)):
      # 422 for a mode not in holds.MODES; 404 for NotFound; 409 page for AlreadyDecided,
      # reusing the "Already decided" HTML the answer route renders (factor it into a helper
      # `_already_decided_page(row)` and call it from both routes); on success 303 to /a/{artifact_id}.
  ```
- The held count comes from one query:
  - `select count(*) from comment where artifact_id = %s and held and thread_root is null and resolved_at is null`;
  - exposed as `holds.held_count(conn_or_db, artifact_id) -> int`;
  - computed only when the page has an open review.

- [ ] **Step 1: Failing tests** (TestClient with the gallery app, as in `tests/test_reviews.py`):
  - The open held-review card contains `Holding your marks until your verdict` and a form posting `mode=live` with the button `Send them as I go`. With two held marks, it shows `2 marks held` and `Approved with held marks counts as Approved with notes.` With one, it shows `1 mark held`. With none, no count and no approved hint.
  - After posting `mode=live` (with the same-origin headers the existing answer tests use), the response is 303, the card says `Sending your marks as you make them` with `Hold them until my verdict`, and the marks are released.
  - `POST /d/{id}/mode` with `mode=loud` gives 422. On a question it gives 404. On an answered review it gives 409, and the body names the verdict. A cross-origin post is refused exactly as the answer route refuses it.
  - The comment panel shows the `held` tag on a held row and not on a released one.
  - The page's embedded marks JSON carries `"held": true` for a held mark. The shim source contains `ar-held` and `underline dotted`. If a real-browser shim test exists, extend it: a held mark is registered under `ar-held` and not `ar-mark`.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Implement.** Escape every value interpolated into HTML with `escape()`, as the surrounding code does.
- [ ] **Step 4: Run** the full suite, format and lint.
- [ ] **Step 5: Commit:** `git commit -m "feat(gallery): a hold-or-live switch on the review card, and his held marks drawn dotted"`.

### Task 4: the agent side — choose the mode when asking, and learn it

**Files:**
- Modify: `src/artifacts/api.py`:
  - `ReviewBody` gains `mode: Literal["held", "live"] | None = None`, and the POST passes it to `decisions.ask_review`. The body stays optional; a bodyless POST still gives 201.
  - `GET /api/v1/reviews/{id}` adds `"mode": row.review_mode`.
- Modify: `src/artifacts/decide.py`: `ask-review` gains a mutually exclusive `--live` / `--hold`, sending `{"mode": "live"}` / `{"mode": "held"}`, or `{}` when neither is given. The output line becomes `<id>  review requested on <page> (marks: held|live)`, read from the response's `review_mode`. The following `  wait:` line is unchanged.
- Modify: `src/artifacts/onboarding.py`: the skill's loop line for `ask-review` becomes `artifacts-decide ask-review <page-id> [--live|--hold]` with the same comment. In the skill's prose after the loop block, add: "By default his marks are held until his verdict (his choice, remembered); --live asks for them as he makes them, and he can switch either way on the page."
- Modify: `src/artifacts/news.py`. New first entry: `("2026-09-29c", "On a page under review Jeremy's marks are now held until his verdict unless he switches to live; artifacts-decide ask-review --live asks for them as he goes. A held mark is invisible to you until then.")`
- Test: `tests/test_decide_review.py`, `tests/test_reviews.py`, `tests/test_tools.py`.

**Interfaces:**
- Consumes: `decisions.ask_review(..., mode=...)`, and `DecisionRow.review_mode`.

- [ ] **Step 1: Failing tests:**
  - The API POST with `{"mode":"live"}` stores `live`. `{}` and no body store his preference. `{"mode":"loud"}` gives 422. `GET /reviews/{id}` has `mode`.
  - The live CLI (the `live` and `key_file` fixtures, imported as `from test_client import key_file, live`):
    - `ask-review <page> --live` prints `(marks: live)`, and `--hold` prints `(marks: held)`. Both flags together exit 2.
    - On a held review, a mark by Jeremy (`comments.add(..., author_kind="human")`) does NOT end `wait --review --timeout 3s` with 7; it exits 3.
    - After `holds.set_review_mode(db, rid, "live")`, the same wait exits 7 and prints the mark.
  - The skill contains `ask-review <page-id> [--live|--hold]` and the new sentence.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite, format and lint.
- [ ] **Step 5: Commit:** `git commit -m "feat(tools): an agent chooses hold or live when asking for a review, and is told which"`.

Merging, pushing, deploying and the live proof are done by the controller after the final review. The live proof uses a throwaway page:
1. `ask-review` (held by default, fresh preference permitting). The card shows the switch.
2. A mark written through the gallery comment route in the pod (`POST /c/{page}`, same-origin headers) is invisible to `artifacts-decide comments` and does not wake `wait --review`.
3. A relayed verdict `c` releases it, and the message lists it.
4. A second throwaway: `ask-review --live`, then a mark wakes `wait --review` with exit 7.

Each step is recorded by the controller.

---

## Self-review

- **Spec coverage, step 3:**
  - `comment.held`, and the `human_pref` row with first-ever *held*: Task 1.
  - Release on a verdict or on a flip; approved-with-held counts as notes; dismissed gives context: Task 2.
  - The switch and held count on the review bar, and dotted held marks: Task 3.
  - `ask-review --live|--hold`, and the default of his remembered choice: Task 4.
  - Rounds, diffs, claim/lease and presence ("agent listening") are steps 4 and 5.
- **Placeholders:** the gallery and shim edits are specified by exact copy, routes and assertions rather than code, because they must fit existing rendering helpers whose exact shapes the implementer reads first. Everything else is given exactly.
- **Types:** `holds.MODES`, `get_mode`, `set_mode`, `page_mode`, `release(conn, artifact_id, *, emit)`, `set_review_mode(db, decision_id, mode) -> (artifact_id, n)`, `held_count`, `CommentRow.held`, `DecisionRow.review_mode` and `ask_review(mode=)` are used consistently in Tasks 1–4.
- **Review Focus:** 1 and 5 are pinned in Task 1, 2 and 3 in Task 2, and 4 in Task 3.
