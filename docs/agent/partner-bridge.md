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
| `ARTIFACTS_PARTNER_EVENTS_URL` | where events are POSTed (`http://` or `https://`; inside the cluster) |
| `ARTIFACTS_PARTNER_SIGNING_KEY_FILE` | path to artifacts' Ed25519 private key, PEM PKCS8, unencrypted |
| `ARTIFACTS_PARTNER_KEY_ID` | the key's id, sent in every event so the receiver can rotate keys |

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

- At least once. 2xx is delivered; anything else, or a transport failure, is retried
  with exponential backoff from 5 seconds, capped at 15 minutes. The receiver answers a
  duplicate with success.
- Every attempt is re-signed with a fresh `sent_at`; `event_id` and `version` do not
  change.
- Order: version N+1 of a question is never claimed while version N is undelivered,
  whatever N's retry time. Different questions do not wait for each other.
- Claiming: a due row is claimed with a lease (`claimed_until`, 60 seconds). On Postgres
  the claim query takes `for update of o skip locked`, so two processes get different
  rows; the lease then holds the row across the network call without an open
  transaction, and a crashed process's rows come back when it runs out. SQLite assumes
  one process and its `begin immediate` already serialises writers; the lease applies
  there too.
- Redirects are not followed. The timeout is 10 seconds.
- `last_error` holds `HTTP <status>` or a short class of failure ("timed out", "could
  not connect"). A response body is never read into it or logged.

## Looking at it

`artifacts-admin partner-outbox` (needs `DATABASE_URL`) prints the undelivered count, the
age of the oldest undelivered event and the last error. `partner.status(db)` returns
the same as a dict. No body, no key.
