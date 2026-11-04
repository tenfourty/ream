# Delivery receipts (design, 4 Nov 2026)

Tracker item runcove-5t3oj.55. Shaped by two rounds of the offices' answers: the 29 Oct round
(`docs/agent/feedback/2026-10-29-form-controls-and-receipts.md`, Q6 and Q7; bound by forms spec
8.3) and this one (`docs/agent/feedback/2026-11-04-receipts.md`, eleven offices; bosun not yet
answered). Nothing here is built. Section 9 lists what must go to Jeremy first; where this
design picks a default, it says so.

## 1. Problem

Jeremy answers a question or leaves a comment, and then sees nothing. Today a decision has two
receipts (`acknowledged_at/by`: "<agent> picked this up <when>", and `decision_outcome`: a note on
what it caused) and a comment has none, only the thread's `resolved_at/by`. He cannot tell "nobody
is listening" from "somebody is on it" from "it is done", and an agent has no way to say "I am
working on this" or "I will not do this". The gallery's route line (.26, `listening.for_decision`)
already explains why a question is not picked up; it says nothing after pickup.

## 2. Goals, non-goals, anti-goals

Goals:
- Five states an agent can declare, each optional and unrendered when unset: **seen**,
  **working**, **done** (shown as "answered"), **blocked**, **won't do**. Done, blocked and
  won't-do take a note; done takes a link.
- The states apply to a decision (a question, a form, a review) and to a comment thread.
- When nothing is picked up, Jeremy sees why (existing route line) and the office is nudged once.
- Every state he sees has an `/api/v1` twin; scoped to the key's agent, labelled with the session
  that declared it.

Non-goals (v1): receipts on a comment's individual read state; inferring any state from a
republish or a read; a nudge for a lapsed "working".

Anti-goals:
- **A receipt that lies.** "Seen" means taken in, never a read (forms spec 8.3, and ten of
  eleven offices). A false "seen" tells him he was heard when he was not. The receipt is the
  agent's statement about itself, labelled as such (like the pickup line: display, never a
  permission); it is not verified.
- **Auto-answered.** Nothing sets "done" for an agent. jeremys-mac: automatic "would mark
  unanswered things answered: the failure this prevents". Reply and settle do not set it.
- **A receipt waking its own author.** See 4.4 (warpgate).
- **Noise.** cove and homelab called "working" noise. It is optional, expires, and an agent that
  never sets it shows nothing.

## 3. What each choice rests on

| Choice | Whose feedback |
|---|---|
| Seen stays deliberate (explicit call), never on `list` or `wait` | 10 of 11; 29 Oct round and forms 8.3 |
| Per thread, with an optional comment id | 10 of 11 per thread; retrobrew per comment, served by section 6 |
| Working set by hand with a note, expiring | all 11 by hand; 11 expiry; retrobrew "unless a bead link backs it" |
| Done is stated with a link; nothing sets it | all 11; jeremys-mac, ch, pmo |
| Blocked and won't-do carry a reason | gg-ops, quarterdeck (29 Oct); ch, homelab, warpgate |
| One verb | all 11 |
| Readable on list and events | 10 of 11; warpgate on list only, resolved in 4.4 |
| A single nudge to the office | 9 of 11; wolff only if blocking; homelab 4h |
| Lapsed vs fall back to seen vs renew | split, goes to Jeremy (9.1) |

## 4. Shape

### 4.1 Data

Both databases (`schema.sql` with `if not exists`; SQLite as the next numbered step in
`db.py` `_SQLITE_STEPS`; plain SQL, no driver-specific syntax).

Reuse first, no parallel column families:
- **Seen on a decision** stays `decision.acknowledged_at/by`: first wins, idempotent, set by
  `close` too, cleared when a form republish reopens it. The new verb writes it.
- **Done on a decision** is a `decision_outcome` row (append only, allowed while open) with a
  new nullable `link text` column. The receipt's note is the outcome body, so the card and inbox
  already show it.
- **Seen on a thread** is two columns on the thread's root comment, `acknowledged_at` and
  `acknowledged_by`, with the same meaning and first-wins rule as on a decision.

New, for what has no home: one table,

`receipt(id, subject_kind, subject_id, state, note, link, agent, session, comment_id, created_at,
expires_at)` with `subject_kind` in `decision | thread` and `state` in `working | blocked |
wont_do | done`; index `(subject_kind, subject_id, created_at)`. `comment_id` is null or a
comment inside that thread (retrobrew). `expires_at` is set for `working` only. Append only: the
**current state** is the latest row; seen is not a row, it lives in the columns above. (As built in slice 1: a `seen` row is also written, only when `acknowledge` newly sets `acknowledged_at`, to carry the session; `receipt` has a `voided_at` column rather than deleting on reopen.) A thread's
`done` is a `receipt` row (a thread has no outcome table); a decision's `done` writes both its
outcome row and a `receipt` row, so one query reads the current state.

`session` is the declaring session label (`ARTIFACTS_SESSION`), nullable, as on events.
`agent` comes from the key, never from a claimed name.

**Reset on a new human act.** A new human comment in a thread, or a form republish that reopens
a decision, makes every earlier receipt history, not current: the thread reads "new, not picked
up" again (the existing reopen rule, extended). Without this a "done" from last week would cover
this morning's comment. Any new human act voids, not only a reopen: his first answer to a plain
question also makes receipts set before it history (and clears the acknowledgement), as a
re-answer, a form submit or relay, a review sent back, or a form republish that reopens it do.
`receipt` has no foreign key (a subject may be a thread), so purging or destroying a page deletes
its decisions' receipts in the same transaction.

### 4.2 The agent side

One verb, `artifacts-decide receipt <id> seen|working|done|blocked|wont-do [--note TEXT]
[--link URL] [--for DURATION]` (`--comment ID` waits for slice 4; nothing validates a comment id before then, so the body does not take one). `<id>` is a decision id or a comment id (any
comment resolves to its thread root; a not-yours or unknown id answers identically, `404`).
- `seen` writes the acknowledge columns; `ack` stays as an alias for it.
- `working` takes a note ("which bead", the ask of cove and gg-ops); `--for` sets the lifetime
  (default 4 hours, at most 12). Calling `working` again **renews** it (quarterdeck).
- `done` takes `--link`; `blocked` and `wont-do` require `--note`.
- `done` on a decision closes nothing: closing stays its own act.
- `done` needs an answer: on a decision he has not answered yet it is refused (`409`, "he has
  not answered this yet"). `seen`, `working`, `blocked` and `wont-do` are allowed before one.

API: `POST /decisions/{id}/receipt` and `POST /comments/{id}/receipt`, body `{state, note?, link?,
comment_id?, for?}`, honouring `Idempotency-Key` as `acknowledge` does. Result: the current
receipt. Reads: the same fields appear on `GET /decisions`, `GET /decisions/uncollected`, `GET
/comments` and the `list`/`threads` CLI output as `receipt: {state, note, link, session, at,
lapsed}` (null when unset), and `GET /decisions/{id}/receipts` returns the history.

### 4.3 What Jeremy sees

- **Decision card, after he answers:** the existing line ("not picked up yet" with the why, or
  "<agent> picked this up <when>") becomes the strip of the current state: "working: <note>",
  "blocked: <note>", "won't do: <note>", "answered: <note> <link>", each with the agent and
  session and "self-declared" in the tooltip. A card with no receipt looks as it does today.
- **A comment thread:** the same strip under the last comment of the thread, once. With the
  optional comment id the strip names the comment.
- **Lapsed working:** computed, never stored: `expires_at` has passed with no renewal and no
  later state. How it shows is his choice (9.1).
- **Nothing picked up:** the route line from `listening.for_decision` stays the single source of
  the sentence (`WORDING`). After the nudge threshold (4.5) the strip adds "<agent> was told in
  Slack <when>".
- Every strip is derived from the agent-readable fields in 4.2; the gallery adds no state the
  API cannot read.

### 4.4 Events, and the warpgate case

Ten offices want their receipts on `events`, so a fresh session sees what an earlier one
declared; warpgate asks not to wake on its own actions. Both hold:
- A new event kind `receipt_set` (schema CHECK and `events.KINDS`, a migration on both drivers)
  records every state change, with `session`.
- It **never wakes a waiter**: `wait_for_agent` ignores it, so a `wait=N` poll does not return
  for it and an agent's own call cannot wake itself or its sibling sessions.
- A plain `GET /events` returns it only with `?include=receipts` (as built, no CLI flag: `artifacts-decide events
  --receipts`), so existing consumers see no change. Another session of the same key reads it
  that way to see what was declared. Receipts also stay on `list` regardless (4.2), which is all
  warpgate needs.

### 4.5 When nothing is picked up

The existing stuck/no_reader alert and `GET /decisions/uncollected` stay as they are. New: if an
answered decision, or a thread with a new human comment, has no receipt of any kind after the
**nudge threshold**, the service sends **one** nudge to the office through the existing partner
bridge (`nudge.py`, `partner_outbox`): "<page>: his answer from <time> is not picked up". It is
never repeated and never goes to Jeremy. Seen counts as a receipt (a nudge is for the case where
nothing happened). Default threshold 1 hour (harland, cove, quarterdeck, homelab for the strip;
most others gave no figure); homelab wants the Slack nudge at 4 hours, wolff only when the
question is blocking. Both are a per-agent setting on the same shelf as the pickup line
(`PUT /listening/nudge` `{after: "1h" | "4h" | "off"}`), default per 9.2. An ignored nudge
following the stalled-message route to pmo (pmo's ask) is left to quarterdeck and not built here.

### 4.6 Scope and labelling

Routes are scoped to the key's agent through `_scoped`, as `acknowledge` is; "not yours" answers
as "no such". The session label rides on every receipt and event. Receipts are declared, not
verified; scope is the key's agent only, so any session of the key can renew or replace another
session's `working` (the later row wins; the history shows who). The gallery has no receipt
controls: only agents state these, so no human twin is needed, and the twin rule is met the other
way round (everything he sees is readable).

## 5. What is reused

`acknowledged_at/by` and its first-wins, idempotent, reopen-clearing behaviour;
`decision_outcome` for "done with a note"; `listening.for_decision` and `WORDING` for the why-not
sentence; `events.append` and `KINDS`; `nudge.py` and the partner bridge; `Idempotency-Key`;
`_scoped`; the `coalesce` first-wins update that works on both drivers; the `in_list` helper for
multi-id reads on the list views.

## 6. Retrobrew's per-comment ask

The unit is the thread, because ten offices said one fix answers several comments. Retrobrew's
pages put one comment on one screen, so a thread there is one comment, and the thread receipt is
a per-comment receipt for it. Where a thread does hold several comments, `--comment ID` records
which comment the state is about and the strip says so, but the state itself stays one per
thread. Retrobrew's other ask, `wait` marking seen automatically, is not taken: ten offices say a
`wait` that returned reached the process, not a session that acted (it may be dying), and a false
"seen" is the anti-goal. Instead the verb is one short call, and the nudge catches a session that
never makes it.

## 7. Slices

1. **Decisions** (built; departures: no `events --receipts` flag, since there is no such subcommand, the `include=receipts` query parameter is the read; no `--comment`). Schema on both drivers (`receipt`, `decision_outcome.link`), the verb and
   routes for decisions, `receipt` on the decision reads, the `receipt_set` kind that never wakes,
   `ack` aliased, reset on reopen. Tests on SQLite and Postgres. (Agents can declare and read.)
2. **Decision strip in the gallery:** expiry computed, lapsed rendering, route line unchanged.
   Live proof on his phone.
3. **The nudge:** the per-agent setting, the one-shot send, the strip line.
4. **Comments:** `acknowledged_*` on thread roots, `subject_kind=thread`, the comment route, the
   `threads` / `GET /comments` fields, reset on a new human comment, the thread strip.
5. **Docs:** `agents.txt`, `--help`, news.py, the design section on listening, the
   is-anyone-listening runbook, the lab-artifacts and runcove artifacts skills. Docs land with
   each slice.

## 8. Risks

- **Reset on a new human act** is easy to miss on one path (a held or withdrawn comment, a form
  republish, a set-aside question coming back). Each needs a test.
- **A declare races a new answer**: `declare` locks the decision row, and the acknowledge
  write is conditional, so two simultaneous `seen` write one row.
- **`receipt_set` is a CHECK change** on `events.kind` on both drivers; the SQLite step rewrites
  the table. Order it first in its slice.
- **An agent that sets "working" and dies.** Expiry handles it; lapsed is computed, so there is
  no sweeper to fail. The default lifetime is a guess (4 hours): short enough that a stale one is
  not trusted, long enough that a long task does not need three renewals.
- **Self-declared means gameable.** A key can say "done" on a thing it did not do. The strip says
  "self-declared"; his answer to a question stays the only truth about the world.
- **`rename.py` and `transfer`** rewrite every column holding an agent name: `receipt.agent` and
  the new `acknowledged_by` on comments must join the list.
- **Bosun** has not answered; its digests are one-way pages, so it likely has little use, but
  check before slice 4.

## 9. Decisions for Jeremy

Only what he sees in the gallery. The rest this design decides itself.

9.1 **When an agent said "working" and then went quiet, what do you see?**
(a) "Lapsed: <agent> said working 5 hours ago", with its note, so you can see it was claimed and
dropped (jeremys-mac, pmo, retrobrew). (b) It falls back to "picked up", as if the working claim
never happened (ch, homelab). (c) It stays "working" until the agent changes it (nobody asked
for this).

9.2 **How soon is an agent told that you answered and nothing was picked up?**
(a) After 1 hour, once, in Slack to the office, never to you; an agent can change its own to 4
hours or off. (b) After 4 hours by default (homelab's figure). (c) Never by default: only agents
that ask are told.

9.3 **Do you want to see who said it?**
(a) The agent and the session on every state ("working, cove/lane-3"), since a key is shared by
several sessions. (b) Only the agent; the session is in the page's history.

9.4 **What shows on a question nobody has picked up?**
(a) Exactly what shows today: "not picked up yet" with the reason. (b) The same, and after the
nudge also "<agent> was told in Slack at <time>". (c) Either, and a count on the wall of answers
nobody picked up.

9.5 **"Answered" against "closed".**
(a) "Answered: <note>" with the link is shown on the card while the question stays open; closing
is a separate act. (b) "Answered" closes the question too, so it leaves your waiting list.
