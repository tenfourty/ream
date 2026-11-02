# Archiving sets a page's questions aside (design, 2 Nov 2026)

Tracker item runcove-5t3oj.64; folders (runcove-5t3oj.61) build on it. Rule set by Jeremy on
2 Nov 2026 (decision `d_01M6DYNEAS82RMW23WWPYES3E6`, (a)), replacing his 21 Sep 2026 rule that
archiving or binning a page closes its open questions. Shaped by the offices' answers
(`docs/agent/feedback/2026-11-02-archive-sets-aside.md`: homelab, harland, runcove-pmo,
quarterdeck, jeremys-mac and warpgate answered; gg-ops, retrobrew, ch, wolff and cove are n/a).

## 1. The rule

- Archiving or binning a page **sets aside** every question on it that is still open (unanswered,
  not withdrawn), of every kind: question, review and form.
- While the page is archived or binned, **nothing that shows Jeremy what waits on him shows them**:
  not his wall, not the waiting count, not the page itself, not any office's list of what waits
  on him (`across_seats`, `open_counts`, `/questions`, the inbox, read-all). He cannot answer one,
  in the gallery or through the partner hub.
- **Unarchiving or restoring** the page brings them back open and unanswered, with the same id and
  the original asked-at, plus a new `reopened_at`. The wall orders by asked-at, so a parked question
  does not jump the queue (quarterdeck).
- The asking agent is **told both times**, by a durable event (jeremys-mac) that names the question,
  the page, what happened (archived or binned), who did it and when, and the asking session (pmo,
  jeremys-mac).
- **Binning is the same as archiving** (every office that answered). The final purge, 30 days after
  binning, closes them for good, and the partner hub is told `withdrawn`.
- Not retroactive: questions closed under the 21 Sep rule stay closed. They cannot be told apart
  from an agent's own `close` except through old `decision_shelved` events, and their context has
  moved on.

## 2. "Set aside" is derived, not stored

Every "open" check today is `closed_at is null and answer is null`. Rather than add a status column
that has to be kept in step with the page, **a question is set aside exactly when it is open and its
page is archived or binned**:

```sql
d.answer is null and d.closed_at is null
  and (a.archived_at is not null or a.deleted_at is not null)
```

One source of truth: the page's own state. Nothing can drift, unarchive needs no reopening sweep
over decision rows (beyond stamping `reopened_at`), and a question asked on a page that is already
archived is set aside from birth (the `ask` response warns: "this page is archived; the question is
set aside until it is unarchived"). This also closes the 21 Sep incident's class for good: a
question off the wall is never counted as waiting.

The cost is that every "waiting on him" predicate joins `artifact` and adds
`a.archived_at is null and a.deleted_at is null`. The full list (from the code map):
`decisions.open_counts`, `decisions.across_seats`, `decisions.list_for_agent`, the gallery wall's
`waiting` subquery and sort, `inbox._decision_items`, the gallery detail page's cards (no cards while
archived), and `partner_in._apply_artifacts` (an incoming `answered` for a set-aside question is
refused like one for a closed question). `store.open_questions_off_the_wall` and the "N tidied away"
line go: by construction it would now always count set-aside questions, which is the state the rule
wants hidden.

Data change: one column, `decision.reopened_at` (null until a reopen). Postgres:
`alter table decision add column if not exists reopened_at` in `schema.sql`; SQLite: step 12.

## 3. Events

Two new event kinds, written in the same transaction as the archive/bin or unarchive/restore:

| Kind | When | Summary |
|---|---|---|
| `decision_set_aside` | page archived or binned, once per open question | `archived: <question>` / `binned: <question>` |
| `decision_reopened` | page unarchived or restored, once per question set aside | `unarchived: <question>` / `restored: <question>` |

Both carry the asker's `session` and the actor (the agent, or `jeremy`). `decision_shelved` stays
in the CHECK for history and is no longer written. Unarchiving a page that is still binned (or the
reverse) reopens nothing: a question reopens only when its page is neither.

Review holds: as today, setting a review aside releases its marks (`holds.release`); a reopened
review starts with no held marks.

## 4. The partner hub (quarterdeck)

Two new partner kinds, **`set_aside`** and **`reopened`**, each with the body `{"reason": str}`
like `withdrawn`. The hub takes the Slack copy's buttons away on `set_aside` and puts them back on
`reopened`; quarterdeck lands its side first (it ignores unknown kinds, so the order is safe either
way, but until then a set-aside question keeps its Slack buttons and an answer through them is
refused here). Purge sends `withdrawn`.

## 5. The agent's side: `artifacts-decide`

- **`wait` wakes on both new kinds.** New exit code **9: set aside**, distinct from 3 (timeout) and
  from the review codes 4-8.
  - A waiter with `--page`: when nothing it waits on there is still open (every open question on the
    page is set aside), it prints the set-aside lines and exits 9.
  - An office-wide waiter (no `--page`, as pmo's `--timeout 0` listener): prints one `SET ASIDE`
    line per question and keeps waiting, because other pages' answers still have to reach it. It
    prints `REOPENED` lines the same way.
  - A waiter that exited 9 does not come back by itself: the office re-arms it when it reads the
    `decision_reopened` event (every office that answered).
  - `--json` gives each line as `{"event": "set_aside"|"reopened", "decision": …, "page": …, "why":
    "archived"|"binned"|…, "by": …, "at": …, "session": …}`.
- **`list`** shows set-aside questions in their own section after the open ones, `SET ASIDE (page
  archived 2026-11-02 13:40 by jeremy)`, with the asking session. They are never counted as
  waiting. `--open-only` and counts leave them out.
- **`answers`** marks each one `SET ASIDE since …, page archived` instead of `unanswered`.
- API: decision rows gain `set_aside` (bool), `set_aside_since` (the page's `archived_at` or
  `deleted_at`) and `reopened_at`. `GET /decisions?scope=mine` and `/questions` leave set-aside rows
  out unless `include_set_aside=true`, which the CLI passes for `list`. `GET
  /artifacts/{id}/decisions` returns them with the flag.
- Withdrawing stays `artifacts-decide close` (quarterdeck, pmo): archive means park.

## 6. Docs

The `_shelve_open_questions` docstring and note become the new rule's (the function becomes
`_set_aside_open_questions` and closes nothing); the comments in `gallery.py`, `api.py`,
`decisions.py`, `decide.py`, `events.py` and `schema.sql` that state the 21 Sep rule; onboarding
(`agents.txt`: archive and bin park questions, exit 9, the two events); the CLI help; the
lab-artifacts and runcove artifacts skills; `docs/services/artifacts/` in the homelab repo.

## 7. Tests

Archive then unarchive (same id, asked-at kept, `reopened_at` set, events both ways, partner
`set_aside` then `reopened`); bin then restore; archived and binned at once reopen only when both
are lifted; ask on an archived page is born set aside; every waiting count and list leaves them out;
`list --include-set-aside` / CLI sections; gallery wall and detail hide them; partner `answered` for a
set-aside question refused; `wait --page` exits 9, office-wide prints and keeps waiting; purge
withdraws; SQLite step 12 on both backends.
