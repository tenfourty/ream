# Writing a plug-in for Ream (formerly artifacts)

A plug-in adds what is particular to one deployment (a chat bridge, a mail sender, an extra secret
scanner, a house command) without touching the core. The core runs with none installed; a plug-in
is installed beside it and found at start-up.

Status, 10 Nov 2026: the loader and the whole interface are built and on `main` (steps 1 to 7 of
`docs/agent/specs/2026-11-04-core-and-plugins-design.md`), and the house's plug-in repo
(`ream-homelab`, step 8) exists with its first plug-in. **Step 9 (moving the house code out of the
core) is under way, one slice at a time:** the tracker-page commands (`beads-page`, `bead-metrics`)
have left the core for the house's `tracker-pages` plug-in (9a); the house sinks (`nudge`, `partner`)
and the `slack` answer source are still inside the core; the GitGuardian second opinion
has left it (9c: the core's own relay scan is deleted, the house's `gitguardian` scanner is the
second opinion, see Scanner below), and so has Mailgun (9b: the core's own sender is deleted, the
house's mail backend sends all mail, see Mail backend below). Write
against this guide for new code; do not expect the other house modules to have moved yet. The API is
versioned (below), but only we use it: there is no promise to third parties yet.

## What a plug-in is

An installed Python package that declares one entry point naming a `register` function:

```toml
# the plug-in's pyproject.toml
[project.entry-points."ream.plugins"]
acme = "acme_ream:register"
```

The group is `ream.plugins`. The old group `artifacts.plugins` is read as well, for good; where one
name is in both, the `ream.plugins` entry wins. The entry-point **name** (`acme`) is the plug-in's
name: it is what `REAM_PLUGINS` selects, what error messages and failure counts carry, and the
prefix of its text variables.

```python
# acme_ream/__init__.py
REQUIRES_API = "1"          # the major version of the plug-in API this code is written for


def register(api):          # api is a PluginAPI (server) or CliPluginAPI (command line)
    api.sink("acme-chat", ["decision_answered"], deliver=_deliver, delivery="outbox")
```

`register` is called once per process, in name order. It only **declares**: no network, no files,
no subprocesses (it also runs in every command-line tool, below). A plug-in may use any of the
registrations below, and one `register` serves both the server and the command line: the command
line accepts and ignores the server registrations.

Ream's settings stay Ream's. A plug-in reads its own configuration from its own files or
environment variables, at the moment it is used, never at import.

## Which plug-ins load: `REAM_PLUGINS`

| Value | Meaning |
|---|---|
| unset (or empty) | every installed plug-in |
| `none` | no plug-in at all (the core suite and the `core-bare` CI job run this way) |
| `a,b` | only those, in name order. A name that is not installed stops the process |

`ARTIFACTS_PLUGINS` is read as well; set to a different value it refuses to start, as every
setting with two names does.

What a plug-in that cannot load or register does depends on the process:

| Process | `REAM_PLUGINS` unset | `REAM_PLUGINS` names plug-ins |
|---|---|---|
| The server | stops start-up, naming the plug-in | stops start-up, naming the plug-in |
| A command-line tool (`ream`, `artifacts-publish`, `-decide`, `-connect`) | **skips** that plug-in with a warning on stderr (`ream: skipped: ...`), undoing its partial registrations, so one bad install does not stop every tool | exits 2, naming the plug-in, with a hint to set `REAM_PLUGINS=none` |

The server refuses because a scanner, answer source or sink that is silently absent is worse than
a pod that does not start. The tools skip because they run on every agent's machine. In both, the
error names the plug-in and, for an exception, only its **type**, never its text or any value.

## API versioning

`ream.plugin_api.API_VERSION` is `(1, 3)`; `api.version` is the same tuple (1.1 added the
optional `timeout` of `api.scanner`; 1.2 added `api.sink(..., stage=)`, `api.schema`,
`api.on_start` and `api.on_stop`; 1.3 added `api.read_side`, all optional: a 1.0 or 1.1 plug-in runs unchanged). A plug-in declares
`REQUIRES_API = "1"` as module data of the module that holds `register`; the core compares the
major number and refuses a mismatch (the server stops; a tool skips or exits as above). A plug-in
that declares nothing is not checked. Minor releases only add registrations or optional
arguments; removing or changing one is a major bump, with one release of both. The `Event`
dataclass only gains fields, so a plug-in must ignore fields it does not know.

## The registrations

| Call | For | Server | Command line |
|---|---|---|---|
| `api.sink(...)` | act on events | yes | ignored |
| `api.answer_source(...)` | a new stored source of answers | yes | ignored |
| `api.on_answer(...)` | run in an answer's transaction | yes | ignored |
| `api.scanner(...)` | a second opinion on text | yes | ignored |
| `api.mail_backend(...)` | send mail | yes | ignored |
| `api.schema(...)` | the plug-in's own tables (1.2) | yes | ignored |
| `api.on_start(...)`, `api.on_stop(...)` | start and stop a thread of the plug-in's own (1.2) | yes | ignored |
| `api.read_side(...)` | questions whose home is a chat partner: copies, count, bridge state, outbox report (1.3) | yes | ignored |
| `api.cli(...)` | add `ream <name>` | checked | yes |
| `api.client_defaults(...)` | the tools' endpoint, session label, news | checked | yes |
| `api.text_vars(...)` | extra `{{variables}}` | yes | ignored |
| `api.text_section(...)` | words in `agents.txt` and `skill.md` | yes | ignored |
| `api.router(...)` | an inbound route on its own host | yes | ignored |

Any registration the core refuses (a bad name, a clash, a wrong type) raises from `register`,
which stops start-up (server) or skips the plug-in (tools, unset), as above.

### Sink: act on events

```python
def register(api):
    api.sink("acme-chat", ["decision_answered", "comment_added"], deliver=post, label="Acme chat")


def post(event):                       # event: ream.events.Event
    body = event.as_json()             # kind, artifact_id, agent, actor, ref_id, summary,
    send_to_chat(body, key=event.id)   # note, session, data, id; raise to say "not delivered"
```

`api.sink(name, kinds, deliver, delivery="outbox", *, label=None)`.

- `kinds`: any **journal kind** (`ream.events.KINDS`: `decision_answered`, `comment_added`,
  `thread_resolved`, `comment_withdrawn`, `decision_set_aside`, `decision_reopened`,
  `receipt_set`, `thread_reopened`, `comment_deleted`, `comment_restored`, `agent_moved`, `agent_keys_retired`, `agent_key_revoked`, and `decision_shelved`, kept for history) or **bridge kind**
  (`ream.events.BRIDGE_KINDS`: `asked`, `answered`, `withdrawn`, `replaced`, `message_added`,
  `edited`, `set_aside`, `reopened`). Bridge kinds never reach the journal or `/events`. A kind
  that is in neither list is refused at registration: start-up stops with a `PluginError`
  naming your plug-in and the kind (plug-ins cannot declare kinds of their own).
- `Event` is frozen: `kind`, `artifact_id`, `agent` (who it is for), `actor`, `ref_id`,
  `summary`, `note`, `session`, `data` (a small dict the producer may add), `id`. `event.id` is
  the idempotency key: pass it on so a retry does not act twice.
- `label` is the human word for the sink in the gallery ("waiting for ..."); without it the name
  shows.
- A sink name is registered once; a second one stops start-up.

**`delivery="outbox"`** (default): the core writes a row in the action's own transaction, and a
sender thread calls `deliver(event)` after the commit. Delivery is at least once. `deliver`
returns on success and **raises to be retried**, with backoff from 5 seconds up to 15 minutes; after
20 failed attempts the row is **parked** (kept, counted, never dropped) and waits for a release.
`deliver` must return within 30 seconds (`sinks.DELIVER_TIMEOUT_SECONDS`); a call that does not is
abandoned as a failure (`TimeoutError`) and retried, though its thread cannot be killed, so a sink
that truly hangs costs a thread per attempt. A late success of an abandoned call is a duplicate,
which at-least-once delivery already allows. If the plug-in is not installed at start, its rows
stay pending; `artifacts-admin outbox` shows counts by sink, and reinstalling drains them.

**`delivery="after_commit"`**: `deliver(event)` is called once on the request thread's side,
after the commit, best effort. A raise is logged (type only) and counted; it never reaches the
caller and the action is already done. Use it for fire-and-forget nudges.
**The doorbell (step 9d):** core's built-in `nudge` sink rings the doorbell itself. A plug-in that
registers an after-commit sink named `doorbell` takes that over: checked at each delivery, core's
sink stands down (it logs once at INFO and rings nothing) while a sink of that name is registered,
so the two never both ring. Without one, core rings as before.

**The circuit breaker** is per sink and automatic. Five failures running (a raise or a timeout),
from at least two different events (so one poison event cannot open the breaker of a healthy
sink), open it for 60 seconds. While open, outbox rows wait untouched (no attempt counted,
nothing parked) and after-commit deliveries are skipped. After the cooldown exactly one delivery
goes through as a trial: success closes the breaker, failure re-opens it. The breaker state and
the plug-in failure counts are shared by every process on the database (tables `sink_breaker` and
`plugin_failure`), so several workers see one breaker per sink. With no database bound (the
tools, unit tests) or one that cannot be reached, a per-process copy answers, so a breaker can
never stop a delivery by failing itself. `sinks.status()` reports `open_breakers`.

### Staging in the action's transaction, tables, start and stop (API 1.2)

For a plug-in that keeps its own queue (the quarterdeck bridge needs a per-question version
number written in the same transaction as the answer), three registrations work together:

```python
def register(api):
    api.schema(["create table if not exists acme_queue (id text primary key, n integer not null,"
                " body jsonb not null, created_at timestamptz not null)"])
    api.sink("acme", ["asked", "answered"], stage=enqueue, label="Acme")   # no deliver
    api.on_start(lambda db: start_sender(db))   # a thread of the plug-in's own
    api.on_stop(stop_sender)
```

- **`stage(conn, event)`** (`api.sink(name, kinds, stage=fn, label=...)`, outbox delivery only;
  exactly one of `deliver` and `stage`) is called in the action's own transaction, in the same
  place and order as the core's own staging sinks, so it can write `conn` (the plug-in's tables).
  The core writes no outbox row and calls no `deliver`: the plug-in's own sender delivers.
  **If it raises**, only its own writes are undone (it runs in a savepoint), the failure is
  logged by type and counted as `<plug-in>:stage`, and the action goes on: the answer, the
  journal row and the other sinks are kept. The event is then **not staged for that sink** and
  nothing retries it, so a plug-in that cannot afford a gap must catch what it can handle and
  keep its own reconciliation. (Unlike `on_answer`, which rolls the answer back.)
  **A database error it catches itself still counts on Postgres**: Postgres aborts the whole
  transaction at the first error, so the core rolls back to the stage's savepoint (its writes,
  even those before the error, are undone) and counts the failure, exactly as if it had raised.
  SQLite aborts only the failed statement, so there the stage's other writes stand. To skip a
  duplicate on both, write `insert ... on conflict do nothing` rather than catching the error.
  **A stage must never end the transaction**: no `commit`, `rollback`, `savepoint` or `release`
  statement, and on SQLite no `or rollback` / `on conflict rollback` (it ends the whole
  transaction, not the statement). `rollback` is contained: the core sees the transaction gone,
  fails the **whole** action with `ream.db.TransactionLost` (counted as `<plug-in>:stage`), nothing
  of it is kept and the caller sees a clean error. **`commit` is the one form the core cannot
  contain, and it is forbidden**: it has already committed everything the action wrote before the
  stage ran, so only the rest of the action fails (the caller sees the same error, but the earlier
  writes, the answer included, are kept). Never run `commit` in a stage.
- **`api.schema(statements)`**: the plug-in's tables, created at start-up after the core's
  migration, on Postgres and SQLite, once and idempotently, under the same lock as the core's
  migration. Only `create table if not exists` and `create [unique] index if not exists ... on`
  are accepted, every name starts with the plug-in's name in lower case with other characters as
  `_` (`acme-chat` -> `acme_chat_`), a table statement ends at the `)` that closes its column list
  (no `as select`, `as table`, `as values`, `inherits` or anything else after it), and a
  statement may hold no `;`, `%`, comment or the word `rollback`. The prefix is not the whole
  rule: at start-up, under the lock, the core reads the database's catalog and refuses a table or
  index name the core uses (its schema, or anything in the database no loaded plug-in declares),
  and an index `on`, or a `references`, naming any table the plug-in's **own** statements do not
  create; so a plug-in named `agent` or `partner` cannot touch `agent_key` or `partner_outbox`.
  Two plug-ins that both declare a schema and whose prefixes overlap (`acme` and `acme-chat`:
  `acme_` covers `acme_chat_`) stop start-up; plug-ins that declare no schema are not checked, so a
  1.0 or 1.1 plug-in loads unchanged. The catalog check covers every relation in the table name
  space (views, materialized views, sequences) as well as tables, and indexes by their own names.
  A statement may not hold `$`, a backslash, a backtick or `[` anywhere, a default string included
  (the check cannot follow `$$` / `$tag$` quoting, `E'\''` escapes or bracket and backtick names);
  the refusal never echoes the statement. A breach stops start-up naming the plug-in (never the text),
  as does a statement the database refuses (the error's type only; nothing of the plug-ins'
  schema is kept). Write the neutral subset:
  `text`, `integer`, `bigint`, `boolean` (defaults `true`/`false`), `timestamptz`, `jsonb`, constant
  defaults, primary key, unique, not null. There is no migration after first creation: a change is
  a new table or index, never an edit. A plug-in table is outside the core's schema and backups
  logic: a plug-in owns its rows and their pruning.
- **`api.on_start(fn)`**: `fn(db)` runs once after the tables exist and the core's outbox sender
  is up, in plug-in name order; it must return promptly (start a thread, do not run the loop).
  **`api.on_stop(fn)`**: `fn()` runs at shutdown, last started first, each isolated, best effort
  (a killed process runs none). It must return quickly (signal the thread, join it briefly): each
  stop hook runs in a thread of its own and is given 2 seconds, as the core's own sender is (hooks
  run one after another, each with its own 2 seconds, so n slow hooks can take 2n seconds); one
  still running after that is left behind, logged and counted as a `stop` failure, and the next
  hook and the core's `sinks.stop` go on. A raise in either is logged by type, counted
  (`<plug-in>:start`, `<plug-in>:stop`) and neither stops the server nor the other plug-ins.
  A sender that a start hook did not manage to start leaves its queue to wait; with the
  plug-in absent the rows stay, as the outbox's do.

### The read side: copies, count, bridge state, outbox report (API 1.3)

A bridge to a chat partner keeps a copy of each question whose home is the partner, which
the gallery's inbox, the question list (`GET /api/v1/questions?scope=all`) and the listening
state show. Core's own partner code is the built-in contributor and always answers first;
`api.read_side(...)` adds a plug-in's, merged in behind it. Every argument is optional (give at
least one), once per plug-in, keyword only:

| Argument | Called as | Returns |
|---|---|---|
| `open_copies` | `fn(db, limit, waiting_on_him=True)` | list of `ream.partner_in.Copy`; `waiting_on_him=False` asks only for those he has already answered here (pending) |
| `settled_copies` | `fn(db, since, limit)` | copies answered since `since` |
| `count_waiting` | `fn(db)` | a non-negative int: copies waiting on him |
| `answer` | `fn(db, qid, letter=, note=)` | the `Copy` after he answers it in the gallery; `store.NotFound` for one that is not yours |
| `enabled` | `fn()` | whether your bridge runs here (feeds `bridge.enabled` in the listening state) |
| `outbox_status` | `fn(db)` | a small dict of counts and codes; shown in `sinks.status()["read_side_outbox"]` and by `artifacts-admin outbox` where plug-ins are loaded (the admin shell does not load them today) |

```python
from ream import partner_in, store

def register(api):
    def open_copies(db, limit, waiting_on_him=True):
        return my_rows(db, limit, pending=not waiting_on_him)   # -> [partner_in.Copy(...)]

    def answer(db, qid, *, letter, note=""):
        row = my_row(db, qid)
        if row is None:
            raise store.NotFound()            # not mine: core tries the next, then 404
        return save_pending(db, row, letter, note)

    api.read_side(open_copies=open_copies, count_waiting=lambda db: count(db),
                  answer=answer, enabled=lambda: True)
```

Rules. A copy's `source` must be unique across core and every plug-in: one core (or an earlier
plug-in) already lists is dropped, and the merged list is cut to `limit` in creation order (the
inbox's own ordering still ranks them). A plug-in's copies are read-only to agents, as core's
are, and appear only for a key with the read-all-decisions grant and `scope=all`, exactly as
before: the agent routes' "not yours is no such" scoping is unchanged. `answer` may raise the
store's own refusals (`BodyTooLarge`, `InvalidText`, `SecretFound`, `ScanFailed`) and
`decisions.InvalidAnswer`, which the gallery answers as it does for core's copies; any other
raise is counted and that plug-in skipped, so the answer ends as a 404 if nobody takes it. The
copies are ranked and rendered by core, so a plug-in cannot supply markup.

### Webhook sinks without a plug-in

A deployment that only needs "this kind of event goes to that URL" writes no code: it points
`REAM_SINKS_FILE` (also read as `ARTIFACTS_SINKS_FILE`; both set and different refuses to start)
at a TOML file, and each `[[sink]]` becomes an outbox sink of the core, with the retry, parking
and circuit breaker above. A bad file stops start-up naming the sink and the field.

```toml
[[sink]]
name = "chase"                  # lower-case letters, digits, hyphens; not a built-in sink or a plug-in's (start-up refuses it)
kinds = ["set_aside"]      # the same kinds as api.sink
url = "https://hooks.example/in"
signing_key_file = "chase.pem"  # Ed25519, PEM PKCS8, unencrypted; relative to this file
key_id = "k1"                   # 1 to 64 of letters, digits . _ : - ; the receiver picks its public key from this
label = "Chase"                 # optional, as for api.sink
allow_http = false              # optional: allow plain http://
allow_private = false           # optional: allow private, loopback and link-local addresses
```

The request is a POST of `{"key_id", "sent_at", "event": <Event.as_json()>}` as compact JSON, with
`X-Ream-Signature` (base64 Ed25519 over the exact body bytes), `X-Ream-Key-Id` and
`X-Ream-Event-Id` (the idempotency key). Every attempt is re-signed with a fresh `sent_at`; a
receiver should refuse a body older than a few minutes and answer a repeated event id with success.
A 2xx is delivery; anything else is a failure and retried.

What it will and will not talk to (`src/ream/webhook.py`): https only unless `allow_http`; no
credentials in the URL; the socket is opened only to a public address (private, loopback,
link-local such as cloud metadata, CGNAT, multicast and unspecified ranges are refused unless
`allow_private`), checked on the address actually connected to, so DNS rebinding cannot get past
it. An IPv6 address is allowed only inside global unicast (`2000::/3`) and outside the
embedding ranges (NAT64 `64:ff9b::/96`, 6to4, Teredo and the like), so anything unlisted is
refused. Limit: a network-specific NAT64 prefix inside `2000::/3` cannot be recognised by this
filter, so a deployment that runs one must block it at the network; redirects are never followed and a 3xx is a failure; no proxy from the environment; 10 seconds
per socket operation, and 25 seconds for the whole attempt (connects use what is left, and the socket
is cut at the deadline, so a server that drips bytes cannot hold it open); name resolution is the
one step with no timeout, so a stalled resolver can still run to the sender's 30-second cap, which
abandons the call (its thread ends when the resolver returns); the response body is never read; a body over 64 KiB is a failure, never a
truncated send. Errors stored with a failed row (and logged) are a fixed phrase: `WebhookError: HTTP 503`,
`timed out`, `TLS failed`, `refused address (not a public one)` and the like, never the URL, host
or key; any other exception is stored as its type name only.

Things to know when running several:

- Give each sink its own key. The signature covers the body, not where it is sent, so a receiver
  that trusts one key for two destinations can be sent the other's events.
- Pending rows belong to the sink's name. Remove or rename a sink and its waiting rows stay; add
  that name back, even with another url or key, and they are sent there. Clear them first if that
  is not wanted.
- One sender thread delivers for every sink, one row at a time. A webhook that stalls (up to 30
  seconds an attempt) delays the others until its breaker opens.

### Answer source and answer hook

```python
def register(api):
    api.answer_source("acme-chat", "Acme chat", relay=False,
                      verified_label="Acme chat sign-in", route_label="Acme chat")
    api.on_answer("acme-record", record)


def record(conn, decision, answer):    # answer: ream.decisions.Answer
    conn.execute("insert into ...", (...))   # same transaction as the answer; a raise aborts it
```

`api.answer_source(name, label, *, relay=False, verified_label=None, route_label=None)` declares
a value that may be stored as a decision's `answer_source`, and how the gallery words it.

- `name`: lower-case letters, digits and hyphens, starting with a letter, at most 32 characters.
  `page`, `terminal`, `telegram`, `in-person` and `other` (the core's) are refused, as is
  `slack` (reserved). Registering the same name again by the same call is harmless.
- `label`, `verified_label`, `route_label`: 1 to 40 characters each. A stored row whose source
  is not registered (the plug-in is absent) still reads, and the gallery shows "Answered via
  <value>, not on the page".
- `relay=True` lets an agent record this source (`POST /decisions/{id}/record`). Leave it `False`
  for a source only your inbound path writes.

`api.on_answer(name, fn)` runs `fn(conn, decision, answer)` **inside** the transaction of
`decisions.answer`, after the answer row is written and before the commit, for answers whose
stored source is one **this plug-in** registered (so `answer_source` must be called first, or
registration fails). `answer` has `letter`, `note`, `by`, `source`, `recorded_by`. A raise aborts
the whole answer as `decisions.AnswerHookFailed` (naming plug-in and hook, no value) and is
counted as `<plug-in>:on_answer`; the caller's request fails with a 503 whose JSON `detail`
names the plug-in (no error text, no value). On the gallery's answer form a person sees a
plain "Answer not saved" page, also 503, with the same wording. It is for answers a person gave
through the plug-in's own inbound path, never for an agent's relay (`record_elsewhere` does not
run it). One hook per name per plug-in. Use it only when the answer must not half-apply;
everything else is a sink.

### Scanner

```python
from ream.scan import Finding


def register(api):
    api.scanner("acme-secrets", scan, on_error="skip")


def scan(text):                        # -> list[Finding]
    return [Finding(shape="acme-token", line=n)
            for n, line in enumerate(text.splitlines(), 1) if "acme_live_" in line]
```

`api.scanner(name, scan, on_error="skip", timeout=None)`. `scan(text)` returns a list (or tuple) of
`ream.scan.Finding(shape, line)`; `shape` is a non-empty `str` of at most 64 characters naming
the kind of secret (never the secret). Any finding refuses the text exactly as a pattern finding
does. At most 20 findings per call are kept.

Scanners are a **second opinion**: they run after the core's own pattern scan came back clean,
where the core takes its second opinion today (page bodies and previews, the fields whose
caller asks for it, and an agent comment's second scan). They do not run on titles, decision
fields or the owner's own comments. Scanner names are unique.

`on_error` says what a scanner that raises, returns something other than a list of `Finding`
(including a bad shape), or takes longer than its time bound means. Only the exception's type is
logged, never its message or the text. A `SystemExit` or other `BaseException` counts as an error
too.

**The time bound is per scanner.** A call is abandoned after 10 seconds
(`scanners.SCAN_TIMEOUT_SECONDS`) unless the scanner declared its own with `timeout=` (API 1.1): a
positive, finite number of seconds, else `ValueError` when it registers. The core caps it at 20
seconds (`scanners.MAX_SCAN_TIMEOUT_SECONDS`), the time the former core relay scan always had, so
`timeout=60` is 20 and `timeout=3` is 3. A scanner that passes nothing keeps 10. Older
registrations work unchanged.

- `"skip"`: no opinion. It is logged and counted (`<plug-in>:scanner`) and the text is judged on
  the other scanners. A page publishes.
- `"block"`: the action fails closed. A publish fails with `scanners.ScannerBlocked`. An agent
  comment is **held**: hidden reversibly (`withdrawn_kind = 'scan_blocked'`, nothing blanked, no
  event), replies to it fail as for a withdrawn comment, and the sweeper retries; a clean scan
  lifts the hold, a finding withdraws it for good. A hold is also lifted when no plug-in scanner
  is registered and no relay is configured. A comment already stamped scanned is never hidden.

A scanner that really hangs costs one daemon thread per call, never reclaimed.

**A scanner named `gitguardian` is the second opinion on an agent comment.** The core has no relay
scan of its own (it moved into the house plug-in as this scanner, 9c). Every registered scanner is
asked on a publish, a preview and a `gitguardian=True` field, and a finding refuses. In an agent
comment's second scan, while a scanner of that exact name is registered, only **its** clean answer
stamps the comment scanned; if it gives no answer (`skip`, a raise or a timeout) the comment stays
unscanned, shown, and the sweeper asks again (under `block` it is held), and another scanner's clean
answer never stands in. With no scanner of that name, any scanner's real clean answer counts. Only
the name counts, not which plug-in registered it. A plug-in that registers `gitguardian` should pass
`timeout=20`, or a relay answer slower than 10 seconds would count as no answer. Core reads no
`GGSHIELD_*` setting; a plug-in reads its own.
If `REAM_GGSHIELD_URL`/`TOKEN` (or the `ARTIFACTS_` forms) are still set but no `gitguardian` scanner is
registered after the plug-ins load, start-up logs one warning naming the variables (never a value) that the second scan is OFF; it warns and does not refuse, so a core-bare install still starts.

### Mail backend

```python
def register(api):
    api.mail_backend("acme-mail", send, enabled)


def enabled():                        # is mail on?
    return bool(read_my_config().get("key"))


def send(to, subject, text):          # raise to fail
    post_to_my_provider(to, subject, text)
```

`api.mail_backend(name, send, enabled)`. **One backend at most**: a second stops start-up. With
none registered mail is off: `enabled()` is false, `send` raises `MailFailed`, and the share page
says email is not set up. The core has no Mailgun code and reads no `MAILGUN_*` or `MAIL_FROM`
setting; a backend reads its own. If `REAM_MAILGUN_KEY`, `_API`, `_DOMAIN` or `REAM_MAIL_FROM` (or the
`ARTIFACTS_` forms) are still set but no backend is registered after the plug-ins load, start-up logs
one warning naming the variables (never a value) that mail is OFF; it warns and does not refuse. `send(to, subject, text)` takes three strings (not a
message object). `enabled()` is asked often (the gallery asks on every render): keep it cheap.
If `enabled()` raises it counts as off (logged once per process); if `send` raises, callers see
`ream.mail.MailFailed("the mail backend failed")` and nothing of your message; both are counted
as `<plug-in>:mail`. `name` is only for logs.

### CLI command and client defaults

```python
def register(api):
    api.cli("acme-report", main, "Print the Acme weekly report")
    api.client_defaults(
        api_url="https://ream.example.org/api/v1",
        session_envs=["ACME_SEAT"],
        news=[("2026-11-08", "ream acme-report prints the weekly report.")],
    )


def main():                           # called as a console script: reads sys.argv, returns the status
    ...
    return 0
```

`api.cli(name, main, help)` adds `ream <name>`. `name` matches `[a-z][a-z0-9-]{0,31}`, is not a
verb the core owns (`publish`, the decide verbs, `admin`, `republish`, `connect`, `mcp-headers`,
`help`) and is not taken by another plug-in. `main()` is called with no
arguments as a console script is: `sys.argv[0]` reads `ream <name>` and the return value is the
exit status. If it raises, the tool prints the exception's type only and exits 1. `help` is one
printable line of at most 200 characters; `ream --help` gains a "From plug-ins" section only when
one is installed.

`api.client_defaults(api_url=None, session_envs=(), news=())` supplies the tools' defaults:

- `api_url`: the endpoint when `REAM_API` (or `ARTIFACTS_API`) is unset and the key is not bound
  to an endpoint. https, or http to a loopback host; valid port; no userinfo, query or fragment;
  a trailing `/` is dropped. One plug-in at most may supply it.
- `session_envs`: names of environment variables holding the office part of the default session
  label, asked before the core's own. Upper-case names, at most 8, none containing KEY, TOKEN,
  SECRET, PASSWORD, PASSWD or CREDENTIAL. A value is used only if it is a plain label
  (`^[a-z0-9][a-z0-9._-]{0,63}$` after lower-casing); otherwise the next source is tried.
- `news`: `(YYYY-MM-DD[a-z], text)` pairs, at most 20, each text one printable line of at most
  2000 characters. The date must be real and not later than today (UTC), so a future id cannot hide
  the core's later entries. They are merged newest first into the once-per-machine news; a
  plug-in's entry never moves the core's "seen" marker.

A refused call changes nothing. **Trust:** `register` runs in every command-line process, on every
start, with the caller's rights (it can read the key file). Keep it cheap and free of side
effects, and install only plug-ins you trust.

### Text: variables and sections

```python
def register(api):                    # the plug-in is named "acme-bridge"
    api.text_vars({"acme_bridge_channel": "#hq-ream",
                   "acme_bridge_status": lambda: current_status()})
    api.text_section("integrations", section)


def section(doc):                     # doc is "agents.txt" or "skill.md"
    return "Answers can also arrive from Acme chat." if doc == "agents.txt" else ""
```

`api.text_vars({name: str | fn})`: each name is lower-snake and must start with the plug-in's own
prefix (its name lower-cased, runs of anything but letters and digits made `_`, then `_`;
`beads-page` gives `beads_page_`), so it can never take a core name or another plug-in's. A
function takes no arguments. Core texts name none of these variables: they are for the plug-in's
own section text.

`api.text_section(slot, fn)` adds words at a named place. Slots: `proxy` and `tools` (in
`agents.txt` only) and `integrations` (the end of both `agents.txt` and `skill.md`). `fn(doc)`
returns the paragraph, or `""` for none. Several plug-ins' sections follow in registration order,
each as its own paragraph. One section per slot per plug-in.

Served words are not a security check, so they fail quiet: a function that raises, a value that
is not a string or one over 20,000 characters renders as nothing, is counted as
`<plug-in>:text` and logged by type only. Results are cached for 60 seconds per variable or
section, a fault included, so a slow function costs at most one call a minute; these documents
are served without authentication.

### Router: an inbound route on its own host

```python
from fastapi import APIRouter

hooks = APIRouter()


@hooks.post("/acme/answer")
async def answer(request):            # check the caller yourself
    ...


def register(api):
    api.router(hooks, "hooks.example.org")
```

`api.router(router, host)`: `router` is a FastAPI `APIRouter` (anything else raises
`TypeError`). It is mounted in its own app (no docs, no schema, a 422 that reports where a field
went wrong and never what was sent) and served **only** when the request's Host equals `host`.
`host` is a plain dotted name (a non-ASCII one is IDNA-encoded first). It may not be the
gallery, the API host, the partner inbound host or the content domain (or under it), nor
another plug-in's; any of these stops start-up.

The core applies **no authentication and no body-size cap**: the route must check its own caller
and bound what it reads. Every response carries `x-content-type-options: nosniff`. WebSockets
are refused. Cookies are host-only in the core; a plug-in must not set `Domain=` cookies on the
parent domain. A fault inside the route answers a bare 500 (`plug-in error`), is counted as
`<plug-in>:router` and logged by exception type only; a client that went away is not counted.

## Failure isolation: the rules in one place

- Every call into a plug-in is wrapped. A raise becomes a log line carrying the plug-in and the
  interface (exception type only, never its message or any content) and a count. Read the
  counts with `ream.plugin_api.failure_counts()` (keys `<plug-in>:<interface>`, with interfaces
  `sink`, `stage`, `start`, `stop`, `read_side`, `on_answer`, `scanner`, `mail`, `text`, `router`); `sinks.status()` carries them as
  `plugin_failures`. With the database bound they are totals over every process.
- A sink or route fault never takes down a request or the sender thread. Outbox failures retry,
  after-commit failures are logged, a repeatedly failing sink is paused by its breaker.
- By design, two things **do** fail the action: an `on_answer` hook that raises (the answer rolls
  back) and a `block` scanner that fails (the publish fails or the comment is held).
- A plug-in that raises while registering, requires another API major, or is named in
  `REAM_PLUGINS` but not installed stops the server's start-up, naming it.
- A `stage` failure never loses the action (savepoint, logged, counted; the event is not staged
  for that sink), unless the stage ends the transaction itself, which fails the action whole
  rather than half (`commit` in a stage cannot be contained and is forbidden: what the action
  wrote before it stays); a start or stop hook failure never stops the server, and a stop hook gets
  2 seconds.
- A `read_side` call that raises, or returns a copy that is not a `partner_in.Copy`, a count that
  is not a non-negative int or a status that is not a dict, is logged by type, counted
  (`<plug-in>:read_side`) and treated as nothing contributed: the page still renders.
- A plug-in may add its own tables (`api.schema`, 1.2) but may not change core tables or SQL, supply gallery pages or JavaScript, or
  serve the reader. The service stays single-reader.

## Testing a plug-in

The core's own plug-in tests (`tests/test_plugin_api.py` and its neighbours) use a throwaway
in-tree plug-in per interface, including the failure cases. A plug-in's tests should cover its
own behaviour (envelopes, wording, provider requests) and not re-test the core. On the management machine the test
suite is never run; push a `ci/<name>` branch and read CI.
