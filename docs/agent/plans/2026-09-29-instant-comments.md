# Instant comments — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Posting a comment on a page takes about a tenth of a second and leaves Jeremy where he was. Today it waits about 2 s on GitGuardian and then reloads the whole page.

**Architecture:**
- **Jeremy's comments** skip the GitGuardian relay. `store._check_secrets` gains a `second_opinion` switch.
- **Agent comments** save after the built-in scan. A FastAPI background task then runs the relay; if it finds a secret, the comment is withdrawn and the agent is told.
- **The gallery's comment forms** post with `fetch`, and the server answers them with the new thread's HTML and the page's marks. The script inserts that HTML in place and re-sends the marks to the frame. The plain form stays as the fallback.

**Tech Stack:** FastAPI (`BackgroundTasks`), psycopg 3 + Postgres, and plain JavaScript in `gallery.py`'s `_COMMENTS_JS`.

**Spec:** the decision recorded on homelab-ij8.112 (mirrored and confirmed by Jeremy on 29 Sep 2026, "Aa Ba Ca"). There is no design doc; this plan and that bead are the authority.

## Global Constraints

- **Jeremy's own gallery writes never go to GitGuardian.** That covers comments, marks and replies (`POST /c/{artifact_id}`), and answer and verdict notes, which already skip it because they are field `note`. The built-in scanner (`scan.scan`) still runs, synchronously, before anything is saved.
- **Agent comments and replies** (`POST /api/v1/artifacts/{id}/comments`) run the built-in scan synchronously, save, and return 201 at once. GitGuardian then scans the saved body in the background. For a reply, it scans the reply's body, not the root's quote.
- **When the background scan finds a secret, the comment is withdrawn:**
  - `comment.withdrawn_at` and `comment.withdrawn_kind` are set (the kind is e.g. `ggshield_slack_bot_token`, never the value);
  - it disappears from every gallery and agent read, and from the page's marks;
  - an event of the new kind `comment_withdrawn` goes to the owning agent, with the summary `Your comment was withdrawn: it looked like a <kind>. Nothing of it was kept visible.`, `ref_id` set to the comment id, and no note;
  - a WARNING line is logged: `comment withdrawn: agent=%s artifact=%s comment=%s reason=%s`;
  - the body is overwritten with `""` and the quote with `""`, so the secret does not sit in the database.
- **When the background scan cannot run** (the relay is unreachable or not configured), the comment stays up. A WARNING is logged, `comment kept on the built-in scan only: agent=%s artifact=%s comment=%s reason=ggshield_unavailable`, matching the publish fallback.
- **Pages are unchanged:** publish, update and preview still wait for GitGuardian.
- **The in-place post:**
  - It covers the new-comment form (`form.cnew`, including a mark from the selection bar) and every reply form.
  - The request carries `Accept: application/json` and the same-origin headers a browser form sends.
  - The server returns 201 with `{"html": <the rendered thread>, "thread_root": <root id>, "marks": <marks_for_page list>}`.
  - A root comment's thread is appended to the open threads. A reply's thread replaces that thread's element.
  - The input clears, the quote clears, the scroll position stays, and the marks are re-sent to the frame.
  - On any failure (network error, non-2xx), the script falls back to the plain form submit, so a comment is never lost.
  - Without `Accept: application/json`, the route behaves exactly as today, with a 303 back to the page.
- **Resolving a thread keeps its reload.** It was not asked for.
- **Commit rules:** conventional commits with no Co-Authored-By or "Generated with" lines (the repo guard refuses them), explicit pathspecs, and no amend, stash or reset --hard.

## Review Focus

1. **A withdrawn comment still shows somewhere:** the gallery panel, the across-pages view, `marks_for_page`, the agent lists, `review.render`, or the uncollected/thread views. *Test (Task 1):* withdraw, then assert absence from every one.
2. **A background scan racing a reply.** Jeremy replies to an agent comment that is withdrawn a second later. The thread must not come back as an orphan with a 500. *Test (Task 1):* withdraw a root that has replies; the lists and render still work, and the replies are hidden with their root.
3. **The fetch path drops the same-origin guard,** or returns agent-controlled HTML unescaped. *Test (Task 2):* a cross-site JSON post is refused exactly like a form post; a comment body containing `<script>` comes back escaped in `html`.
4. **A double submit** from a fast Enter plus a click posts twice. *Test (Task 2):* the script disables the form while a post is in flight. This is asserted on the shipped script source and in the node harness if one covers `_COMMENTS_JS`.
5. **A held mark (step 3) posted in place** appears with its `held` tag, and the frame gets `held: true` in the re-sent marks. *Test (Task 2).*

---

### Task 1: GitGuardian off Jeremy's comments; after the save for agents'

**Files:**
- Modify: `src/artifacts/store.py`. Change `_check_secrets(value, field, *, agent, artifact_id, second_opinion: bool = True)`. With `second_opinion=False` the `ggscan` call is skipped entirely: no call and no fallback log.
- Modify: `src/artifacts/comments.py`.
  - `add(..., second_opinion: bool | None = None)`. When it is None, it defaults to `False` for `author_kind == "human"` and for agents alike, because agents are scanned afterwards. Pass it to both `_check_secrets` calls.
  - Add `withdraw(db, comment_id, *, kind: str) -> bool`: set the withdrawn fields, blank the body and quote, and write the `comment_withdrawn` event plus the log, all in one transaction. It returns False if the comment was already withdrawn or is missing.
  - Every listing (`list_for_artifact`, `list_for_agent`, `across_pages`, `marks_for_page`) and the reply and resolve lookups exclude withdrawn comments, and replies whose ROOT is withdrawn.
- Create, in `src/artifacts/comments.py`: `second_scan(db, comment_id) -> None`, the background job. It reads the body, calls `ggscan.scan`, and on findings calls `withdraw(kind=f"ggshield_{finding.kind}")`, using the shape `ggscan` already names. On None it logs the unavailable line when `ggscan.configured()`. It never raises: a background task's exception is lost, so it logs at ERROR and returns.
- Modify: `src/artifacts/api.py`. `create_comment` takes `background: BackgroundTasks` and adds `comments.second_scan(db, row.id)` after a successful add.
- Modify: `src/artifacts/schema.sql`. Append:
  ```sql
  -- A comment GitGuardian flagged after it was saved (homelab-ij8.112): its
  -- text is blanked and it is hidden from every read; the kind stays.
  alter table comment add column if not exists withdrawn_at timestamptz;
  alter table comment add column if not exists withdrawn_kind text;
  ```
  Also widen the event kind check to include `comment_withdrawn`, the same way the file already re-creates `event_kind_check` (around line 367). Read the current list there and add to it.
- Modify: `src/artifacts/news.py`. Add a new first entry, `("2026-09-29d", "Your comments on a page are saved at once and checked by GitGuardian just after; if it finds a secret the comment is withdrawn and you get a comment_withdrawn event naming the kind, never the value.")`.
- Test: `tests/test_instant_comments.py`, which you create.

**Interfaces:**
- Produces:
  - `store._check_secrets(..., second_opinion=True)`;
  - `comments.withdraw(db, comment_id, *, kind) -> bool`;
  - `comments.second_scan(db, comment_id) -> None`;
  - `CommentRow` is unchanged. Withdrawn rows are never returned, so no field is needed.
- Consumes: `ggscan.scan(body) -> list[Finding] | None` and `ggscan.configured()`. Read `ggscan.py` for the `Finding` shape and how `_check_secrets` names a finding's kind today, then reuse that naming.

- [ ] **Step 1: Failing tests.** Monkeypatch `ggscan.scan` and `ggscan.configured` to count calls and to return findings or None.
  - `test_his_comment_never_waits_on_ggshield`: post through the gallery `POST /c/{id}` (TestClient, same-origin headers as in the existing gallery comment tests). `ggscan.scan` is called 0 times. A built-in-scan secret, e.g. an `AKIA…` shape from the existing scan tests, is still refused with the same response as today.
  - `test_an_agent_comment_is_saved_then_scanned`: an API post returns 201. The background task runs, which TestClient does before returning, and `ggscan.scan` is called exactly once with the body.
  - `test_a_finding_withdraws_it_and_tells_the_agent`: ggscan returns a finding.
    - The comment is absent from both agent lists, the gallery panel, `across_pages` and `marks_for_page`.
    - A `comment_withdrawn` event exists with the summary text from the Global Constraints.
    - The stored body and quote are `""`.
    - The log line is emitted (use `caplog`).
  - `test_an_unreachable_relay_keeps_it_and_logs`: ggscan returns None with `configured()` True. The comment stays and the unavailable line is logged.
  - `test_a_withdrawn_root_hides_its_replies_without_breaking` (Review Focus 2): Jeremy replies to an agent comment, then it is withdrawn. Every list and `review.render` still work, and neither the root nor the reply appears.
  - `test_publish_still_waits_for_ggshield`: `store.publish` still calls `ggscan.scan` synchronously.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** run the full suite (`ARTIFACTS_BROWSER=/usr/lib64/chromium-browser/headless_shell uv run pytest -q`), then `ruff format` and `ruff check`.
- [ ] **Step 5: Commit.** `git commit -m "perf(comments): his comments skip the GitGuardian wait, and agents' are checked just after saving"`.

### Task 2: post a comment without reloading the page

**Files:**
- Modify: `src/artifacts/gallery.py`, in two places:
  - **The route.** `POST /c/{artifact_id}` (around line 4230): when the request's `Accept` header contains `application/json`, return `JSONResponse({"html": ..., "thread_root": ..., "marks": comments.marks_for_page(db, artifact_id)}, status_code=201)`.
    - `html` is the thread rendered by the SAME helper the page uses for one thread. Find it and reuse it; do not write a second renderer.
    - Errors return JSON `{"detail": ...}` with the status codes the form path already uses.
    - Without that `Accept` header, the route behaves exactly as today.
  - **`_COMMENTS_JS`** (around line 1723), which intercepts `submit` on `form.cnew` and on every reply form:
    - `preventDefault`, disable the form's submit controls, then `fetch(form.action, {method:"POST", body:new FormData(form), headers:{"Accept":"application/json"}, credentials:"same-origin"})`.
    - On 201, insert or replace the thread per the Global Constraints, reset the form (value, quote, hidden anchor fields), then call the existing `sendMarks` with the new marks. Update the variable it reads first, and reset its `sent` guard if it has one.
    - On any failure, re-enable the form and call `form.submit()`, the native fallback.
    - Reply forms may be added after load, when an in-place thread arrives, so use event delegation on the document, not per-form listeners.
- Test: `tests/test_instant_comments.py`. If a node harness in `tests/js/` covers `_COMMENTS_JS`, extend it; otherwise assert on the served script source.

**Interfaces:**
- Consumes: Task 1's changes, and `comments.marks_for_page` (which returns `held` from step 3).

- [ ] **Step 1: Failing tests.**
  - A JSON post of a new root comment gets 201 with `html` containing the comment body escaped, a non-empty `thread_root`, and `marks` including a new range mark.
  - A JSON reply returns the whole thread's HTML with both messages.
  - The same post without `Accept: application/json` still gets 303.
  - A cross-site JSON post is refused exactly like a form post (Review Focus 3).
  - A body with `<script>alert(1)</script>` comes back escaped.
  - On a page under a held review (`decisions.ask_review(mode="held")`), the JSON marks carry `held: true` and the html carries the `held` tag (Review Focus 5).
  - The served `/comments.js` contains `application/json` and a disable-while-posting step (Review Focus 4), and it falls back with `form.submit()`.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Implement.** Keep the plain form working with JavaScript off.
- [ ] **Step 4:** run the full suite, then `ruff format` and `ruff check`.
- [ ] **Step 5: Commit.** `git commit -m "feat(gallery): a comment appears where he wrote it, without reloading the page"`.

The controller merges and deploys. Then the live proof:
- time a gallery comment post in the pod log: it should be under 300 ms;
- confirm no relay log line appears for it;
- confirm an agent API comment produces one relay line just after it;
- a fabricated Slack-shaped token in an agent comment, on a throwaway page, is withdrawn and yields `comment_withdrawn`;
- check the in-place post in a real headless browser against the served gallery if feasible; otherwise Jeremy tries it.

## Self-review

- **Coverage of ij8.112:**
  - his writes skip GitGuardian: Task 1;
  - agents are scanned after saving, with the withdrawal telling the agent the kind: Task 1;
  - no reload: Task 2;
  - pages unchanged: Task 1's publish test.
- **Placeholders:** the JavaScript is specified by behaviour and tests, not code, because it must fit the existing `_COMMENTS_JS` closure, whose variable names the implementer reads first.
- **Types:** `second_opinion`, `withdraw`, `second_scan` and the JSON keys `html`, `thread_root` and `marks` are consistent across both tasks.
