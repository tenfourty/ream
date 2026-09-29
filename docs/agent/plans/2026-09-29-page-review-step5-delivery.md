# Page review, step 5: one session acts on a verdict — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Exactly one agent session acts on each round of a review, and Jeremy can see that session on the page:
- A second session waiting on the same review is told who has it, and does not revise too.
- A session that cannot act hands the review back, with a reason he can read.
- A retried reply or acknowledgement never posts twice.

**Architecture:**
- Each `review_round` carries a lease: `claimed_by` (session label), `claim_expires_at` and `released_reason`.
- `wait --review` claims the current round when it starts and renews the claim while it runs. The CLI and the API speak the claim through two routes, claim/renew and release.
- The review card shows presence from the claim plus `events` `seat_read`, which already exists.
- An `Idempotency-Key` header on the reply and acknowledge routes is remembered for 24 h, keyed by agent, route and key, and returns the first response again.

**Tech Stack:** FastAPI, Postgres, the stdlib CLI in `decide.py`, and the gallery in `gallery.py`.

**Spec:** `docs/agent/specs/2026-09-28-page-review-design.md`, "Delivery", the exit-code table (code 8), and "Build order" 5.

## Global Constraints

- **The lease.** `review_round` gains `claimed_by text null`, `claim_expires_at timestamptz null` and `released_reason text null`, appended in `schema.sql`. The lease lasts 5 minutes.
- **Claiming.** `POST /api/v1/reviews/{id}/claim` takes a JSON body `{"session": "<label>"}`, where `session` is required and non-empty. It runs atomically under the decision row's `for update` lock:
  - If the current round is unclaimed, its lease has expired, or it is claimed by the same session, the claim succeeds. It sets `claimed_by` and `claim_expires_at = now() + 5 min`, clears `released_reason`, and answers 200 `{"claimed_by", "claim_expires_at", "round"}`.
  - Otherwise it answers 409 `{"claimed_by": "<other>", "claim_expires_at", "round"}`.
  - A review that is not yours gets the same 404 as today.
  - The claim is per round: a new round starts unclaimed.
- **Releasing.** `POST /api/v1/reviews/{id}/release` takes `{"session": "<label>", "reason": "<text>"}`. The reason is required, at most 500 bytes, and scanned like an outcome body. It clears `claimed_by` and `claim_expires_at`, and sets `released_reason = reason`. If another session holds the lease, it answers 409 in the claim shape. It writes no event to the agent, because the agent released it itself.
- **The CLI.**
  - `artifacts-decide wait --review <id>` needs a session label. It takes `--session` or `ARTIFACTS_SESSION`, the same source every command already uses. With no label it runs as today, without claiming, and prints one stderr line: `no session label: not claiming the round (set ARTIFACTS_SESSION)`.
  - It claims on start, and renews when less than 2 minutes of the lease remain (checked each loop).
  - If another session holds it, it prints `CLAIMED by <session> until <HH:MM UTC>: another session is acting on this round` and exits **8**.
  - `--require-approval` maps 8 to 1.
  - New command: `artifacts-decide review-release <id> --reason "…"`. It prints `released <id>: <reason>` and exits 0. On a 409 it prints the claim line and exits 8.
- **Presence on the review card**, for an open round:
  - Claimed with a live lease: `Agent listening · <session> · last seen <N> min ago`, where last seen is the newest `seat_read.last_read_at` for that agent and session, or "just now" if under a minute.
  - Released: `Handed back by <session>: <reason>`.
  - Otherwise nothing.
- **Idempotency.**
  - `POST /api/v1/artifacts/{id}/comments` (replies and new comments) and `POST /api/v1/decisions/{id}/acknowledge` accept an optional `Idempotency-Key` header of up to 128 printable ASCII characters.
  - The first request with a given (agent, route, key) stores its status code and JSON body for 24 hours, in a new `idempotency` table. A repeat returns exactly that response and writes nothing.
  - A key reused with a DIFFERENT body gets 422 `idempotency key reused with a different request`. Store a sha256 of the body.
  - The CLI's `reply` and `ack` send a fresh uuid4 key per invocation, and reuse it on their own internal retries.
  - Expired rows are pruned when new ones are written.
- **No regressions.** Everything from steps 2 to 4 stays as it is: verdicts, rounds, hold/live and marks.
- **Commit rules:** conventional commits, with no Co-Authored-By or "Generated with" lines. Stage explicit paths. No amend, stash or reset --hard. Run the full suite with `set -o pipefail` and `ARTIFACTS_BROWSER=/usr/lib64/chromium-browser/headless_shell uv run pytest -q -n auto --maxprocesses 8`. Timing bounds in tests must leave headroom for a loaded CI box: at least 5x the expected time.

## Review Focus

1. **Two waiters race to claim.** Exactly one wins, and the other gets 409 and exits 8. *Test (Task 1):* two threads claim at once through the API, using the Event-gate pattern from `tests/test_holds.py`.
2. **A waiter dies.** Its lease expires, and a new waiter from another session can claim after 5 minutes, but not before. *Test (Task 1):* monkeypatch the lease length to a few seconds.
3. **The same session starts a second wait**, for example after a restart. It re-claims its own lease and does not exit 8. *Test (Task 1).*
4. **A retried reply posts twice.** *Test (Task 2):* the same key and body give one comment and identical responses. The same key with a different body gets 422. Keys are scoped per agent, so another agent's identical key is independent.
5. **Presence leaks something about another agent's session,** or shows a stale "listening" after the lease expired. *Test (Task 3).*

---

### Task 1: claim, renew and release a round

**Files:** `schema.sql`, `decisions.py` (with `claim_round` and `release_round`), `api.py` (the two routes), `decide.py` (claim, renew and exit 8 in `_wait_review`, the `review-release` command, and the `--require-approval` mapping), `tests/test_claims.py`, and `tests/test_decide_review.py` for the CLI.

- [ ] **Failing tests:** claim, a second session getting 409, the same session re-claiming, lease expiry (Review Focus 1–3), release with a reason, release by a non-holder getting 409, a new round starting unclaimed, `wait --review` exiting 8 with the exact line, renewal while running (a short monkeypatched lease), `review-release` output, and the `--require-approval` mapping.
- [ ] Implement, run the full suite, then commit `feat(review): one session claims a round, and another is told who has it`.

### Task 2: retries never post twice

**Files:** `schema.sql` (the `idempotency` table), `api.py` (header handling on the two routes, shared through a small helper), `decide.py` (keys on `reply` and `ack`), `tests/test_idempotency.py`.

- [ ] **Failing tests** for Review Focus 4, plus pruning and the 128-character limit. A bad key gets 422.
- [ ] Commit `feat(api): a retried reply or acknowledgement never posts twice`.

### Task 3: presence and hand-back on the card, and tell the agents

**Files:**
- `gallery.py`: the review card's presence line.
- `news.py`: a new first entry, `("2026-09-29f", "wait --review now claims the round for your session (set ARTIFACTS_SESSION): a second session waiting on the same review exits 8 and names who has it. artifacts-decide review-release <id> --reason \"…\" hands it back, and Jeremy sees why. reply and ack send an Idempotency-Key, so a retry never posts twice.")`.
- `onboarding.py`: one sentence after the review lines in the skill: "Set ARTIFACTS_SESSION so wait --review can claim the round; if it exits 8 another session is acting on it, and if you cannot act, run artifacts-decide review-release <id> --reason \"…\"."
- Tests: presence text for a live lease, an expired lease and a release (Review Focus 5), and the skill sentence.
- [ ] Commit `feat(review): he sees which session is on his review, or why it was handed back`.

The controller merges and deploys, then proves it live on a throwaway page:
- two `wait --review` processes under different session labels: one waits, the other exits 8 naming the first;
- `review-release`, after which the page view in the pod shows the hand-back line;
- a reply sent twice with the same key gives one comment.

## Self-review

- **Coverage of spec "Delivery":** the claim with exit 8 and requeue on expiry (Task 1), idempotency (Task 2), presence and release (Tasks 1 and 3). "Addressed is inferred" was already done in step 4. "Closing a tab never means anything" holds, because nothing listens for a tab closing.
- **Left for step 6:** the plan-mode hook, the terminal tool and the herdr plugin (ij8.109).
- **Placeholders:** none.
