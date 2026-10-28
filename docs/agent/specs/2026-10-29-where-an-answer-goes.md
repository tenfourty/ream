# Where an answer goes: showing whether the agent is listening (design, 29 Oct 2026)

Tracker item runcove-5t3oj.26. Draft for review; built except where a section says otherwise (the pickup line is built).

## 1. Problem

The item began as "show whether the publishing agent is waiting for an answer". The offices'
answers (`docs/agent/feedback/2026-10-28-listening-and-pinned-comments.md`, all eleven) change
what it is:

- **All eleven:** a bare "not listening" reads to Jeremy as "your answer is lost". The empty state
  must say **where the answer goes and when it is acted on** (runcove-pmo, cove, jeremys-mac,
  gg-ops, homelab, harland, warpgate, wolff, quarterdeck, retrobrew, ch). "A session is waiting now"
  is the best case, not the whole feature.
- **Eight of eleven hold no wait at all** (they use quarterdeck asks or `list` at a routine check;
  gg-ops every 15 min to 4 h, cove every 20-60 min). Only runcove-pmo, cove and quarterdeck hold
  a long wait, and it dies with the session. "Waiting now" is false most of the time, so the
  delivery route carries the message.
- **Ten of eleven want a read-only API twin**, mostly so a fresh session can see whether another
  session of its office already waits before arming a second one (runcove-pmo, wolff, cove, ch,
  jeremys-mac, harland, gg-ops, homelab, quarterdeck; warpgate "probably not"; retrobrew n/a).
  Cove asked for it scoped to its own office. AGENTS.md says the same: a control with no
  `/api/v1` twin is half a slice, and agent routes are scoped to the calling key's agent.

What the service can and cannot know today:

| Fact | Known? | From |
|---|---|---|
| A session is holding `GET /events?wait=N` right now | Not yet. | `events.note_read` stamps `seat_read.last_read_at` at the START of every `/events` read, **including `wait=0` polls**, and keeps no end or duration. |
| When a session of this agent last asked for events | Yes | `seat_read(agent, session)` |
| The question was announced to the partner, and the outbox delivered it | Yes | `decision.partner_version > 0`, `partner_outbox.delivered_at`, `partner.enabled()` |
| Which Slack office the partner maps this agent to | **No** | Mapping lives in quarterdeck's `partner.agents`; a name it cannot map gets no Slack copy (`docs/agent/partner-bridge.md`). |
| Whether that office is running, or when it acts | **No** | Outside this service. |
| Which sessions hold a wait through `list` polling (no hold) | **No** | `list` is a plain read and records nothing. |

The page must not claim more than that table, and must say what it does not know.

## 2. What the reader sees

Rendered by the gallery (never by the agent's page) as one line under the question on each
**open question** card, class `qroute`, beside `qmeta`. Reviews keep their own round-presence
line (`decisions.round_presence`). Settled cards keep the existing "picked this up" text.

`<agent>` is the artifacts agent name that asked; `<ref>` the session label.

| State | When it holds | Line |
|---|---|---|
| **Waiting now** | A session whose label belongs to the asking office holds an `/events` wait that has not lapsed (section 3). | "A session is waiting for your answer now (`<ref>`). It acts as soon as you answer." |
| **Sent to Slack, nobody waiting** | `partner.enabled()`, the question was announced, and its `asked` event is delivered. | "No session is waiting. Your answer is delivered to `<agent>` in Slack, and acted on at its next check. Last seen `<n> min ago`." |
| **Slack delivery delayed** | As above but the `asked` event is undelivered or parked. | "No session is waiting, and the copy of this question for `<agent>` in Slack has not been delivered yet. Your answer is saved; the agent reads it when it next looks." |
| **Not sent anywhere** | Bridge off, or the question predates the bridge (`partner_version = 0`). | "No session is waiting and this question is not in Slack. Your answer is saved; the agent reads it when it next looks. Last seen `<n> min ago`." (or "has never looked") |

Honesty rules, each a test:

1. "Delivered to `<agent>` in Slack" names the artifacts agent, not an office: this service does
   not know the office. It says "sent", never "received by", and never "will be acted on at HH:MM".
2. "Acted on at its next check" is the only timing claim. If the agent has declared how it picks
   answers up (section 6, `pickup`) the line uses its words instead ("`<agent>` says: within the
   hour").
3. "Last seen" is the newest `seat_read` among the asking office's sessions, or "has never
   looked". It reflects `/events` reads only; an office that polls `list` shows older than it is.
   The line says "last seen asking for events" in the title attribute.
4. Waiting now needs a live hold, not a recent poll: a `wait=0` read never makes the line say
   "waiting".
5. An answered card shows nothing new.

The gallery list (`/`) and inbox rows get no new text in this slice; see Choices.

## 3. How liveness is measured

Reuse `seat_read`; add one column, `held_until timestamptz null`.

- `events.note_read(db, agent, session, hold_seconds=0)` keeps its start-of-read stamp. With
  `hold_seconds > 0` it also sets `held_until = now + hold_seconds + GRACE`, `GRACE = 30 s`
  (covers the gap between two holds of one waiter: client re-request, a 20 s back-off after a
  dropped connection is deliberately NOT covered). With `hold_seconds = 0` it leaves
  `held_until` as it is.
- `api.list_events` passes the `wait` it will really use (after the `cf-ray` cap to
  `PUBLIC_MAX_WAIT`). The existing comment on the call (stamp BEFORE the hold, so a long listener
  does not look absent) still governs.
- When the hold returns (answer, or timeout), a best-effort `events.note_hold_end` sets
  `held_until = now` for that `(agent, session)` unless a newer stamp exists
  (`where held_until <= <this request's own value>`), so a waiter that heard its answer and
  exited stops reading "waiting" at once. It never raises, like `note_read`.
- **Waiting now** for a question = some `seat_read` row for the question's agent with
  `held_until > now` and a session that belongs to the asking office. "Belongs" is
  `decide._belongs`: the label equals the office or starts with `office + "/"`, where the office
  is the part of the question's own `session` before `/`. A question with no session matches the
  unlabelled row (`''`), as the existing `coalesce(decision.session, '')` join does. A waiter
  hears its whole office, so ANY session of the office waiting counts, not just the one that asked.

Known limits, stated in the code comment and the runbook:

- A client that dies mid-hold leaves `held_until` up to its hold length plus grace in the
  future (at most about 80 s through Cloudflare, 330 s inside the house). The line errs toward
  "waiting" for that long. Accepted: a wait is re-armed within seconds in the normal case.
- Labels are self-declared (`api.session`); this is a display, never a permission. Scoping uses
  the key's agent only.
- Two labels of different offices on one shared key are told apart only by their label prefix.

## 4. The API twin

`GET /api/v1/listening`, read-only, scoped to the calling key's agent.

Query: `decision=<id>` (optional), `session=<label>` (optional filter, like `/events`).

```json
{
  "agent": "cove-maintainer",
  "now": "2026-10-29T10:02:11Z",
  "ttl_grace_seconds": 30,
  "waiting": [
    {"session": "cove/ab12", "office": "cove", "held_until": "2026-10-29T10:03:01Z",
     "last_read_at": "2026-10-29T10:02:11Z"}
  ],
  "recent": [
    {"session": "cove/9f3c", "office": "cove", "last_read_at": "2026-10-29T08:40:00Z"}
  ],
  "bridge": {"enabled": true},
  "pickup": "within the hour",
  "decision": {
    "id": "d-...", "state": "waiting_now",
    "waiting_session": "cove/ab12",
    "route": {"kind": "partner", "agent": "cove-maintainer", "announced": true, "delivered": true},
    "last_seen": "2026-10-29T10:02:11Z"
  }
}
```

- `waiting`: this agent's sessions with `held_until > now`. `recent`: the others read in the
  last 24 h, newest first, at most 20. Nothing from any other agent, ever.
- `decision` appears only with `?decision=`; `state` is `waiting_now`, `sent_to_slack`,
  `slack_delayed` or `not_sent`, exactly the four rows of section 2, from one function the
  gallery and the route both call (`listening.for_decision`), so the page and the API cannot
  disagree.
- A decision that is not the caller's answers **identically** to one that does not exist: 404,
  the same body as `GET /decisions/{id}`.
- `route.kind` is `"partner"` or `"none"`. The service says nothing more about the office.
- It records nothing and does not touch `seat_read` itself (a status check must not make the
  checker look like a listener). Rate-limited like other reads.
- Documented in `/api/v1` index/OpenAPI summary with the section 2 honesty rules.

## 5. CLI

`artifacts-decide listening [--decision ID] [--office NAME] [--json]`

- Prints: who waits now (office-filtered by `--office`, `ARTIFACTS_SESSION`'s office, or all of
  this key's), last seen, and with `--decision` the route line exactly as the page words it.
- Exit 0: a session of my office is waiting now. Exit 3: none (same convention as `wait` 3, "not
  yet, not a failure"). Other failures as every command: 1 (see `client` exit codes).
- Fresh-session recipe, for onboarding: `artifacts-decide listening || artifacts-decide wait ...`
  (arm a wait only if nobody of my office holds one). `wait` itself prints one line on stderr
  when it starts and finds another session of its office already waiting, and does **not**
  refuse (two waiters are harmless; a refusal would strand an office whose waiter just died
  inside the grace window).
- `wait` sends nothing new: the heartbeat is the held `/events` request it already makes.
  Clients that write their own listener (onboarding "Writing your own listener") get the
  heartbeat for free by holding `wait=50`.
- Update: `src/artifacts/decide.py` (subcommand, `--help` epilog on `wait` and `listening`),
  `src/artifacts/client.py` if the HTTP helper is shared, `src/artifacts/onboarding.py`
  (the paragraphs near "start `artifacts-decide wait` in the background": add the `listening`
  check, the three states, and "most offices are not holding a wait: that is fine, the page says
  where the answer goes"). After the change: `uv tool install <checkout> --force`.

## 6. Schema

Both schemas, per the db.py rule (SQL identical on Postgres and SQLite; only `db.py` talks to a
driver):

| Change | Postgres `schema.sql` | SQLite `schema_sqlite.sql` |
|---|---|---|
| `seat_read.held_until timestamptz null` | `alter table seat_read add column if not exists held_until timestamptz;` | the `create table seat_read` gets `held_until timestamptz`; a step for existing files adds the column if missing |
| `agent_pickup(agent text primary key, pickup text not null, updated_at timestamptz not null)` (optional, below) | `create table if not exists` | `create table` |

- Reads compare timestamps in Python, never `max()` in SQL (SQLite hands an aggregate back as
  text; see `round_presence`'s `"[TIMESTAMPTZ]"` cast).
- `rename.py` (`_merge_reads`) must carry `held_until` as the later of the two rows;
  `migrate.py` copies the new column and its verifier covers it (`seat_read` has an
  empty-session special case already, `_EMPTY_SESSION`).
- **`agent_pickup`** (BUILT, 29 Oct 2026, Jeremy's choice: "an agent may set one line, shown as
  '<agent> says: within the hour', in place of 'at its next check'") holds an agent's own answer
  to "when do you act on an answer?". Set by `PUT /api/v1/listening/pickup`
  `{"pickup": "within the hour"}`, removed by `DELETE` on the same path; the CLI is
  `artifacts-decide pickup "within the hour"` / `pickup --clear` (not the `listening --set-pickup`
  flag drafted above). Rules, in `listening.clean_pickup`: at most 120 characters; one line (a
  line break of any kind is refused with 422, not folded); control and format characters (escape
  codes, bidi overrides, zero-width marks) removed, spaces collapsed; empty is refused. Per agent
  (key), not per office, because the service has no office entity. Declared, never verified,
  always shown as "`<agent>` says: ..." and only in the "sent to Slack, nobody waiting" and "not
  sent anywhere" states (waiting-now and delayed already say something stronger or different).
  The `/listening` body carries `pickup` (and each `decision` carries it). `rename.py` keeps the
  newer line of the two agents. Agent-only: Jeremy has no control for it.

## 7. Tests

All run on CI only (AGENTS.md: never on baba), both backends.

- `tests/test_events.py`: `note_read` with `hold_seconds` sets `held_until` to now + hold + 30 s;
  with 0 it leaves an earlier `held_until` unchanged; `note_hold_end` clears it and does not
  clear a newer hold; neither raises on a failing connection.
- `tests/test_api_events.py`: a held `GET /events?wait=1` shows in `/listening` as waiting
  during the hold (a thread, as the existing wait tests do) and not after; a `wait=0` poll never
  does; the `cf-ray` cap is what is stamped.
- `tests/test_listening.py` (new):
  - the four states of `for_decision`, including the unlabelled question and a question whose
    session is `office/ref` with another `office/other` session holding the wait;
  - a session of a different office on the same key does not count;
  - bridge off, `partner_version = 0`, outbox delivered, outbox undelivered and parked;
  - another agent's decision and a missing id give byte-identical 404s; `waiting` and `recent`
    never contain another agent's sessions; `recent` capped at 20 and 24 h;
  - `/listening` leaves `seat_read` untouched.
- `tests/test_gallery.py`: each of the four lines renders on an open question; none on an
  answered card or a review; HTML-escaping of a hostile session label and pickup; the line
  matches the API's `state` for the same decision.
- `tests/test_decide_cli.py`: `listening` exit codes 0 and 3 with `--office`; `--json` shape;
  `wait` prints the "already waiting" line and still waits; `--help` mentions the exit codes.
- `tests/test_onboarding.py`: the onboarding text names `listening` and the pre-arm check.
- `tests/test_rename.py`, `tests/test_migrate.py`: `held_until` survives a rename merge and a
  Postgres to SQLite copy and verify.
- Pickup (if kept): set, clear, length limit, sanitising, scoped to the key's agent.

## 8. Docs are part of done

- `docs/agent/design.md`: the "who is listening" paragraph (heartbeat now has a hold, the
  four states, the table of what is not known).
- `docs/agent/partner-bridge.md`: that the page claims "sent to `<agent>` in Slack" and nothing
  about the office; ask quarterdeck to offer an ack of which office a question mapped to (a
  follow-up that would let the line name the office; not required here).
- New runbook `docs/agent/runbooks/is-anyone-listening.md`: reading the states, the limits in
  section 3, "why does it say waiting for a dead client".
- `src/artifacts/onboarding.py` and the `--help` texts (section 5); the OpenAPI summary.
- `docs/agent/implementation-plan.md` ledger line; the feedback doc gets a pointer to this spec.
- Homelab side, for the maintainer: runcove `docs/offices/artifacts-maintainer.md` and each
  office doc that runs a wait gets one line: "run `artifacts-decide listening` before arming
  a wait" (offices owned by their own sessions; this lane does not edit them).
- Reinstall the CLI where agents run it (`uv tool install <checkout> --force`).

## 9. Out of scope

Pinned comments (a separate item). Naming the Slack office on the page (needs the partner's
ack, section 8). Detecting `list`-only pollers as listeners (no signal to measure). Any
notification when the state changes. A hard guarantee that "waiting now" survives a client
crash.

## Choices for Jeremy

1. **What the card says when nobody is waiting but the question is in Slack.**
   a. (recommended) "No session is waiting. Your answer is delivered to cove in Slack, and acted
      on at its next check. Last seen 12 min ago."
   b. "Nobody is waiting live; this question is also in Slack for cove. Your answer is saved
      either way."
   c. A short label only: "Slack" (a chip), with the detail on hover.

2. **Where it appears.**
   a. (recommended) One line under each open question on its page, and nothing new in the
      gallery list.
   b. The same line, plus a small "waiting now" mark on that question's row in the gallery list
      and inbox.
   c. The line on the page, plus a mark in the list only when nothing is waiting AND the
      question is not in Slack (the case that could lose an answer).

3. **Whether an agent can say when it acts on answers.**
   a. (recommended) Yes: an agent may set one line ("within the hour") that replaces "at its next
      check" on its cards, shown as "cove says: ...".
   b. No: every card says "at its next check", and nothing is declared.

4. **How much of the session shows.**
   a. (recommended) Show how long ago the agent last asked for events ("last seen 12 min ago")
      and, when waiting, the session label ("cove/ab12").
   b. Show only "waiting now" or "not waiting"; no times, no labels.
