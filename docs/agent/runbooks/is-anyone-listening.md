# Is anyone listening? Reading the line under a question

Design: `specs/2026-10-29-where-an-answer-goes.md`. Code: `src/artifacts/listening.py`;
all the wording is `listening.WORDING`.

An open question on a page carries one line saying where Jeremy's answer goes:

| `state` | Meaning |
|---|---|
| `waiting_now` | A session of the asking office holds a `GET /events?wait=N` that has not lapsed. |
| `sent_to_slack` | Nobody holds a wait; the question was announced to the partner and that copy was delivered. |
| `slack_delayed` | Announced, but the copy is undelivered or parked (`artifacts-admin partner-outbox`). |
| `not_sent` | Bridge off, or the question predates it. The answer is saved; the agent reads it when it next looks. |

Read the same from an agent: `artifacts-decide listening [--decision ID] [--office NAME] [--json]`
(exit 0 a session of my office waits, 3 none), or `GET /api/v1/listening[?decision=ID]`. Both
see this key's agent only; a decision that is not the caller's is a 404 identical to a missing one.

## The pickup line

An agent may declare when it acts on answers: `artifacts-decide pickup "within the hour"`
(`--clear` removes it; `PUT|DELETE /api/v1/listening/pickup`). One line of plain text, at most
120 characters; a line break is refused and control characters are removed. It is per agent
(key), declared and never verified, and replaces "acted on at its next check" as
"`<agent>` says: ..." in the `sent_to_slack` and `not_sent` states only. It also rides in the
`/listening` body. Jeremy has no control for it.

## How "waiting" is measured

`seat_read.held_until`: set by an `/events` read with `wait` > 0 to now + the hold + 30 s grace,
pulled back to now when the hold returns. A plain `wait=0` read never sets it. A waiter
"belongs" to a question when its session label is the question's office or starts with
`office/`; an unlabelled waiter hears every office, so it counts for all.

## Why it can be wrong

- **Says waiting, client is dead.** A client that dies mid-hold leaves the hold standing until it
  ends: up to about 80 s through Cloudflare (50 s cap + grace), 330 s inside the house (300 s +
  grace). Also, when the server already holds its maximum number of waiters
  (`events._waiters`), a request returns at once but was stamped as a hold; it ends the stamp
  when it returns, so the error is bounded by the request's own length.
- **Says nobody waits, office is polling.** An office that runs `artifacts-decide list` at each
  check holds nothing; it shows as "last seen" only if it also reads `/events`.
- **Says "delivered to X in Slack".** X is the artifacts agent name. Whether the partner maps it
  to an office, and whether that office is running, is outside this service
  (`partner-bridge.md`).
- Labels are declared by the caller. This is a display, never a permission.
