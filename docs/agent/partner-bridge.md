# The partner bridge

Source: `src/artifacts/partner.py` (outbound) and `src/artifacts/partner_in.py`
(inbound). Tracker: runcove-5t3oj.39.4. The contract (v3) is agreed with the
quarterdeck service. The first half of this file is what artifacts sends; the
second, "The inbound half", is what it receives and does with it.

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

`answered_by.verified_by` is `artifacts` for his own tap on the page, `quarterdeck` for a press in Slack it vouched for, and `agent` for a
relay: artifacts saw only a seat's word, and `recorded_by` names the seat.

`asking_office` is the **artifacts agent name** of the key that asked (`artifacts-maintainer`,
`homelab-maintainer`, `runcove-pmo`, …), not an office id: artifacts does not know the
partner's offices, and some of its agents are not offices at all. `for` is what the agent
passed to `--for`, unchecked. The partner maps both to its own offices (quarterdeck:
`partner.agents` in the hub's config, agreed 26 Oct 2026); a name it cannot map gets no
Slack copy.

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


# The inbound half

Source: `src/artifacts/partner_in.py`; tests `tests/test_partner_inbound.py`.

## Configuration

Both variables, or neither. Neither means the inbound side is off: there is no
endpoint, and the host router behaves as it did. Only one, or either without the
outbound bridge (the four `ARTIFACTS_PARTNER_*` variables above: one partner, and the
answers we send back go to the same place), stops the service at start-up with a message
that names variables and never a value.

| Variable | Meaning |
|---|---|
| `ARTIFACTS_PARTNER_INBOUND_KEYS_FILE` | path to a JSON object mapping `key_id` to the partner's Ed25519 PUBLIC key: PEM text (SubjectPublicKeyInfo) or the 32 raw bytes in base64. Several ids at once, for a rotation |
| `ARTIFACTS_PARTNER_INBOUND_HOST` | the service's name inside the cluster, the only `Host` the endpoint answers on |

The host may not be the gallery's, the API host or a page origin (start-up refuses). The
file is read once; nothing from it reaches a log or an error. It holds public keys only:
a leak of this side cannot forge an event.

## The endpoint and how it stays inside the cluster

`POST /partner/v1/events`. There is no ingress and no tunnel route for it. The service
already had one mechanism for keeping one face apart from the others, and this uses it:
the host router in `app.create_app` picks an app by the request's `Host` (gallery, API
host, page origin; anything else is 404). The endpoint is a fourth, tiny app that is
picked only when `Host` equals `ARTIFACTS_PARTNER_INBOUND_HOST`. So it does not exist on
the gallery, the API host or a page origin, and the gallery, API and content routes do not
exist on the in-cluster host. Two further guards, because a Host header is only a claim:
the route answers 404 when any proxy header is present (`X-Forwarded-*`, `Forwarded`,
`X-Real-IP`, `CF-Connecting-IP`, `CF-Ray`, `True-Client-IP`), which a call straight to the
service name never carries and an ingress or a tunnel adds; and the signature is checked
whatever the path. It is not behind the agent key: the signature is the credential.

## Verification, cheapest first

On the exact request bytes, never re-serialised JSON:

| Check | Status |
|---|---|
| body over 64 KiB (also by `Content-Length`, before reading) | 400 |
| not JSON (including nested too deeply for the parser), or not a JSON object | 400 |
| a field missing or mistyped (`event_id, kind, source, question, version, key_id, at, sent_at, body`; `question` must be the id inside `source`; `at` and `sent_at` need a zone) | 422 |
| `key_id` not in the key file | 401 |
| signature (`X-Partner-Signature`, base64 Ed25519) does not verify, or is missing | 401 |
| `sent_at` more than 5 minutes old, or more than 60 seconds ahead | 401 |
| the same `event_id`, or a version not newer than the last applied for `source` | 2xx, nothing changes |
| applied, or understood and deliberately not acted on | 2xx |
| `answered`, `edited`, `withdrawn` or `replaced` for a `qd:` question whose `asked` has not landed | 503 with `Retry-After: 30`: "not yet, retry"; nothing is stored |
| a failure on our side | 500 (the sender retries) |

413 is not used: the sender parks a 413 and a 400 alike, and 400 is what was agreed. A
response is `{"result": "applied|duplicate|stale|ignored"}` or `{"error": "<one fixed
word>"}`. Neither a response nor a log line ever carries a body, a signature or a key; a
log line names the event id only after the signature has verified.

## What is stored, and the dedupe

All in the same transaction as the change it describes:

- `partner_event_seen (event_id, source, version, kind, outcome, detail, seen_at)`: every
  event applied or deliberately ignored. Pruned after 30 days (by the sender's hourly
  prune). `outcome` says what was done (`applied`, `held`, `unchanged`, `refused`,
  `unknown`, `stale`, `ignored_kind`). `detail` holds, for a Slack press, who quarterdeck
  says pressed (name, Slack id): the audit, never shown, logged or sent on.
- `partner_version_seen (source, version)`: the last version applied per question, kept.
  The test is one atomic upsert (`... where version < excluded.version`), so two requests
  racing on one question cannot both pass.
- `partner_question`: the copies below. Settled copies are pruned after 30 days too.

## A question whose home is artifacts (`source: "artifacts:<decision id>"`)

Only `answered` means anything: the copy reports a press in Slack. The other kinds
describe what artifacts itself told the partner, and are recorded and ignored.

THE TRUST RULE (Jeremy, 9 Oct 2026): a Slack answer counts when the person holds approve
on the asking office in quarterdeck's configuration, and artifacts trusts quarterdeck's
verified identity. So `answered_by` must carry `verified_by: "quarterdeck"` (else 422),
with `name` and the Slack id (`slack_id`). The decision is then answered through the same
path as a page answer (`decisions.answer(..., source='slack')`): `answer_source` is
`slack`, `recorded_by` is `quarterdeck`, `answered_by` is `jeremy` (the page's one
identity; the real name and Slack id are in the audit record), the normal
`decision_answered` event is journalled and the page's agent is nudged, and the outbound
`answered` (a new version, `answered_by.verified_by` `quarterdeck`, `answer_source`
`slack`) is queued: quarterdeck uses it as the doorbell, so it is never suppressed. The
page shows "answered in Slack, verified by quarterdeck".

It is applied only if the decision exists, is a question, is not closed, was announced to
the partner (`partner_version > 0`) and the answer is not the one already recorded (a
changed answer or note is allowed, as on the page). Otherwise the status is still 2xx and
nothing changes: an unknown, foreign, closed or review id is indistinguishable from any
other (existence is not revealed, and the log names only the event id).

THE HELD CHOICE: a question held behind an unanswered prerequisite (slice 2) is not
settled by a press. The event answers 2xx (so the sender stops retrying a press that can
never apply), nothing is written to the decision, and the event record says `held`. The
version still counts as seen, so a stale retry cannot slip in later; a new press (a new,
higher version) after the prerequisite is answered applies normally. quarterdeck posts the
Slack copy of a chained question only after its prerequisite, so this is the backstop, not
the usual path. A letter that is not an option, or a note that fails the size or secret
checks, is treated the same way (`refused`).

Agents still cannot claim `slack`: `decisions.ANSWER_SOURCES`, what a relay may name, is
unchanged, and `slack` is a separate stored value that only `partner_in` writes.

## A question whose home is quarterdeck (`source: "qd:<id>"`)

`partner_question` holds the copy: source, the home's last applied version, asking office,
title, options, recommended, context (4,000 characters at most), the link to the home (kept
only if http or https), topic, `depends_on` (as given, not enforced here: quarterdeck
holds a chained question until it is ready), urgency, blocked-on, needed-by, `for`, state
(`open`, `answered`, `withdrawn`, `replaced`), the answer and who gave it, `replaced_by`,
and a pending local answer.

- `asked`: creates the copy, or updates its fields. Its `created_at` is the event's `at`.
- `answered`: state `answered`, with the answer, note and name; clears a pending answer.
- `withdrawn`, `replaced`: the state changes (`replaced_by` is kept).
- `edited`: replaces the context.
- `message_added`: ignored for now (2xx; the version counts as seen). Replies under a
  Slack question are a later slice.
- `answered`, `edited`, `withdrawn` or `replaced` for a copy we do not hold (its `asked`
  has not landed): 503 with `Retry-After: 30`, and NOTHING is recorded: not the event id,
  not the version. 503 is retryable (our own sender retries it too), so the event is
  delivered again after the `asked` has applied, and then applies. `message_added` for an
  unknown copy is still 2xx-ignored.

His inbox takes the open copies as a second source (`inbox.py`, kind `slack_question`):
the same order key as a page question, labelled "from Slack", with the context, the link
to Slack and a button per option. They count in the inbox total. The form posts to
`POST /pq/<id>/answer` behind the same same-origin guard as the page's answer form.

THE ANSWER HERE IS PENDING, NEVER FINAL. artifacts is not the home, so it settles
nothing: the form records `pending_answer` and queues an `answered` event whose `source`
is `qd:<id>` (the outbox row's `decision_id` holds the whole source). The copy leaves the
main list and shows "Answered here, waiting for quarterdeck" beneath it, with no buttons.
When the home's own `answered` arrives the copy is `answered`, the pending answer is
cleared, and the inbox shows "Settled in Slack" for a week. Answering again while pending
is allowed (a new event). `withdrawn` or `replaced` takes the copy out whatever was
pending.

VERSIONING OUR EVENTS FOR A QUESTION WE ARE NOT THE HOME OF: the home owns the versions of
its own question, so ours must not share their number space. artifacts keeps its own
counter per copy (`partner_question.out_version`, bumped by the same `UPDATE` that records
the pending answer: 1, 2, ...), and the outbox's `unique (decision_id, version)` is that
space. Those numbers are unrelated to the home's `version` for the same question, so
quarterdeck must dedupe the events it receives about its own questions in a space of their
own (keyed by sender and source), not against the last version it applied for its own
events. This needs quarterdeck's agreement; if it prefers another scheme (for example a
version above the home's last) only `out_version` changes.

## The API twin

`GET /api/v1/questions?scope=all` also lists the open copies, for a key holding
`decisions:read-all` only (the same check as for other seats' questions). Each is marked
`source: "qd:<id>"`, `home: "partner"`, `read_only: true`, ordered with the page questions
by the inbox's order, and carries `pending_answer` when he has answered it here. Nothing
in the API answers one: an agent has no more right to answer Slack's question than to
answer as him. The default scope never lists them.
