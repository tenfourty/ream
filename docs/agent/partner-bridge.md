# The partner bridge, outbound half

Source: `src/artifacts/partner.py`. Tracker: runcove-5t3oj.39.4 (first half). The
contract (v3) is agreed with the quarterdeck service. This file covers what artifacts
sends; the receiving endpoint and the answer coming back are a later slice.

A question has one home. When artifacts is the home, the partner holds a copy that
says `source: "artifacts:<decision id>"`, and artifacts tells it what happens to the
question. Reviews are never shared: only `kind = 'question'` decisions.

## Configuration

All four, or none. None means the bridge is off: no rows are written, no thread runs,
and the service behaves as it did. Some but not all stops the service at start-up with
a message that names the variables and never a value.

| Variable | Meaning |
|---|---|
| `ARTIFACTS_PARTNER_NAME` | the partner's name, for logs and the admin report |
| `ARTIFACTS_PARTNER_EVENTS_URL` | where events are POSTed; must be `https://` unless the opt-in below is set |
| `ARTIFACTS_PARTNER_SIGNING_KEY_FILE` | path to artifacts' Ed25519 private key, PEM PKCS8, unencrypted |
| `ARTIFACTS_PARTNER_KEY_ID` | the key's id, sent in every event so the receiver can rotate keys |

Optional: `ARTIFACTS_PARTNER_ALLOW_HTTP=1` allows a plain `http://` URL (for an in-cluster
service with no TLS). Without it an `http://` URL stops start-up. When it is in use the
service logs a warning once at start. Proxy variables in the environment are ignored, and
redirects are never followed (a 3xx counts as a failed attempt and is retried).

The key file is read once at start-up, never logged, never put in an error and never
stored. Mount it from the deployment's secret store; it is not in this repo. Generate a
pair with any tool that writes PKCS8 (for example `openssl genpkey -algorithm ed25519`)
and give the partner only the public half.

## Events

Every request body is one JSON object (compact, UTF-8):

```
{event_id, kind, source, question, version, key_id, at, sent_at, body}
```

`kind` is `asked`, `answered`, `withdrawn` or `replaced`. `edited` and `message_added`
are in the contract but not sent yet: artifacts has no path that edits a question, and
thread replies under a question are a later slice.

| kind | sent when | `body` |
|---|---|---|
| `asked` | an agent asks a question | `asking_office, title, options [{letter,text}], recommended, context, link (page#q-<id>), topic {id,title}, depends_on, replaces, urgency, blocked_on, needed_by, for` |
| `answered` | he answers on the page (a changed answer is a new event); an agent records an answer given elsewhere | `answer, note, answered_by {name, verified_by}, answered_at, answer_source`, plus `recorded_by` and `said_at` for a relayed answer |
| `withdrawn` | a question is closed with no answer, including when its page is archived or deleted | `reason` |
| `replaced` | a question is replaced by a new one (sent on the OLD question) | `replaced_by, replaced_by_source` |

`answered_by.verified_by` is `artifacts` for his own tap on the page and `agent` for a
relay: artifacts saw only a seat's word, and `recorded_by` names the seat.

A question closed after it was answered (the agent finishing its work) sends nothing.
A question asked before the bridge was switched on is never announced late: its later
events are not sent either, because the partner never saw it.

`version` counts per question, from 1, and is strictly increasing. The counter is the
`decision.partner_version` column, bumped by the same `UPDATE` that takes the row lock,
inside the transaction that writes the event, so two events for one question never
share a number and a rolled-back change gives its number back.

## The outbox

`partner_outbox`: one row per event, written in the same transaction as the change that
causes it (a rolled-back ask enqueues nothing). It holds `event_id, decision_id, version,
kind, body, created_at, attempts, next_attempt_at, claimed_until, delivered_at,
last_error, last_error_at`. The body stored is the event's own `body`; `key_id` and
`sent_at` are added, and the whole signed, on each attempt. Delivered rows are pruned
after 30 days. The table is in both schemas (SQLite step 4) and the Postgres to SQLite
copier carries it.

## Signing

Ed25519 over the exact bytes of the request body, base64, in the header named by
`partner.SIGNATURE_HEADER` (`X-Partner-Signature`). The receiver refuses a body whose
`sent_at` is more than five minutes old.

## Delivery

One daemon thread (`partner-sender`), started by `serve.app_from_env` only when the
bridge is configured, polling every 2 seconds. It never runs on a request's path.

- At least once. 2xx is delivered. Anything else is retried with exponential backoff
  from 5 seconds, capped at 15 minutes, EXCEPT the statuses that mean the receiver
  refused this event for good: 400, 404, 409, 410, 413, 415 and 422. Those PARK the row
  (`parked_at` set, the code in `last_error`): it is not retried, and the same question's
  later versions are held behind it (the receiver cannot apply `answered` without
  `asked`). Other questions keep flowing. 401 and 403 are NOT parked: the partner answers
  401 for a bad signature, an unknown `key_id` or a stale `sent_at`, so a key rotation or
  a clock fix lets the queue drain by itself. 5xx, 408, 425, 429, 3xx, other 4xx and
  network failures are retried too.
- Every attempt is re-signed with a fresh `sent_at`; `event_id` and `version` do not
  change.
- Order: version N+1 of a question is never claimed while version N is undelivered
  (or parked), whatever N's retry time. Different questions do not wait for each other.
- Claiming: one due row at a time is claimed with a lease (`claimed_until`, 60 seconds). On Postgres
  the claim query takes `for update of o skip locked`, so two processes get different
  rows; the lease then holds the row across the network call without an open
  transaction, and a crashed process's rows come back when it runs out. SQLite assumes
  one process and its `begin immediate` already serialises writers; the lease applies
  there too. Claiming one row per attempt means a lease is taken just before its own
  request, so a slow neighbour cannot let it lapse. An empty queue is detected by a
  read-only select first, so idle polling never takes SQLite's writer lock.
- Redirects are not followed. The timeout is 10 seconds.
- `last_error` holds `HTTP <status>` or a short class of failure ("timed out", "could
  not connect"). A response body is never read into it or logged.

## Looking at it

`artifacts-admin partner-outbox` (needs `DATABASE_URL`) prints the undelivered count, the
age of the oldest undelivered event, the parked rows (count, oldest, their codes), a
warning when waiting events last failed with 401/403, and the last error of an event
still waiting, with its age. A delivered event's old error is not shown.
`partner.status(db)` returns the same as a dict. No body, no key.

To release parked events once the cause is fixed (the receiver deployed, the event
understood): `artifacts-admin partner-outbox --retry <event_id>` for one, or
`--retry-parked` for all. Either resets attempts and the retry time.
