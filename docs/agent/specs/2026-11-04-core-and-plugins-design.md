# A core that runs for anyone, and our specifics as plug-ins (design, 4 Nov 2026)

Tracker item runcove-5t3oj.68. Shaped by the owner's rulings of 4 Nov 2026 (section 2). Nothing
here is built. Section 10 lists what must go to Jeremy first; where this design picks a default,
it says so. The coupling this argues from was inventoried on 4 Nov 2026 (about 25 hard-coded
owner ids, five outbound integrations, house text in everything an agent is served); lines cited
below were re-checked against `main` at `8da2c2c`.

## 1. Problem

The service was built for one household and says so everywhere. `"jeremy"` is a literal in about
25 call sites (some inside SQL); the default origins are `tenfourty.site`; `/api/v1/agents.txt` and
`skill.md`, which every arriving agent reads, name him, his gallery, Cloudflare and a vault wrapper
"as on the management machine"; the CLI help uses his pronoun about 60 times; `'slack'` is a value in a schema
CHECK; Mailgun, GitGuardian, quarterdeck and a doorbell on one machine are imported by name from
the middle of the decision code; the two tracker tools ship as console scripts of the core. Anyone
else who installs it gets a service that is wrong in every sentence and cannot run without those
integrations' settings.

Every new feature adds to this (receipts, chasing dropped answers). Doing the cleanup first, as
its own work, stops the debt compounding.

## 2. Goals, non-goals, anti-goals

Rulings (Jeremy, 4 Nov 2026), as this design reads them:
- House-specific behaviour is out of the core, and the existing coupling is cleaned up as its own
  piece of work **before** more features.
- The core has extension points and **works with no plug-ins installed**. Our specifics become
  plug-ins.
- Our plug-ins live in a **separate repo**, installed into our deployed image at build time. The
  artifacts repo holds only the core.
- The service stays **single-reader**: one owner. The cleanup makes the owner a setting (id,
  display name, pronoun or neutral wording). Many readers is a separate, later design.
- Chasing dropped answers (a separate piece of work): nudge the agent once for an unpicked answer
  and once for a lapsed "working" receipt; after a day, emit a generic **dropped** event to a
  deployment-configured target. This design must leave room for that event kind (4.1).
- House names leave code, user-visible messages and text served to agents. Hosts, mail sender and
  owner name become settings.

Goals:
- A fresh install with no settings and no plug-ins starts, serves a gallery, takes pages and
  decisions, and says nothing about anyone's house (3.3).
- Our deployment behaves **exactly** as today after every slice: same rows, same API, same text
  where it is ours (provided by settings and plug-ins), same events.
- One place an action says "this happened"; everything downstream subscribes (4.1).

Non-goals (v1): plug-ins that add database tables or change core SQL; plug-in-supplied gallery
pages or JavaScript; more than one reader; a plug-in marketplace or a stable third-party
guarantee (the API is versioned from day one, section 4.6, but only we use it); a new SMTP or
other generic mail backend (a later, optional slice).

Anti-goals:
- **Frameworks for their own sake.** An extension point exists only where a house module has to
  plug in today. Six interfaces, no more (4.2).
- **A silent plug-in failure.** An installed plug-in that cannot load stops start-up; a plug-in
  that fails at run time is counted and logged, never swallowed (4.5).
- **A changed owner id reading old rows wrong.** See 3.2: the guard is the point of the first
  slice.
- **Behaviour change smuggled in as cleanup.** Each slice is checked by the same suite plus a
  golden diff of the served text for our settings (section 8).

## 3. Settings

### 3.1 One object, read at call time

New `src/artifacts/settings.py`: a frozen dataclass `Settings` and `settings.current()`.
Replaces the scattered `os.environ.get` calls (inventory: about 40 variables in 15 modules). The
env names stay (`ARTIFACTS_*`), so no manifest changes except the new ones.

**Call-time reads are kept.** Tests and pods change values without an import-time freeze
(`store.py:327`, `hosts.py:84`, `nudge.py:21` rely on it). So `current()` builds the object from
`os.environ` on every call (about 40 `get`s, microseconds) and caches nothing. A request handler
takes it once at the top and passes it down, so a render loop never re-reads. Tests use
`with settings.override(owner_name="Ann"):`, which layers over the environment for that block
only. A module that deliberately reads late (hosts, stamp) keeps doing so by calling
`current()` at the point of use; there is no module-level `SETTINGS = ...`, and a lint (a grep in
`scripts/guards`) refuses `os.environ` outside `settings.py`, `serve.py` startup switches and the
CLI client's own env.

**Validation.** `settings.validate_for_serve()` runs at start-up, collects every problem (a
malformed origin, an owner id with spaces, a half-configured sink) and fails once with the whole
list. The partner bridges' all-or-nothing `config_from_env` checks move behind it; each plug-in
validates its own settings through the same call (4.1, `validate`).

**Secrets stay files.** Settings hold paths and ids (`..._KEY_FILE`), never values; `Settings`
has no `__repr__` that could print one.

### 3.2 The owner

| Setting | Env | Meaning | Neutral default |
|---|---|---|---|
| `owner_id` | `ARTIFACTS_OWNER_ID` | The principal id stored in `by`, `author`, `answered_by`, `actor`, folder owner | `owner` |
| `owner_name` | `ARTIFACTS_OWNER_NAME` | How text says it | `the owner` |
| `owner_pronoun` | `ARTIFACTS_OWNER_PRONOUN` | `they` (default), `he`, `she` | `they` |

`owner_id` is a stored value, not only a label. `folders.HIS = "jeremy"` and `owner <> 'jeremy'`
in `store.py` SQL become a bound parameter; `keys.RESERVED_AGENT_NAMES` is `{owner_id}`; the 25
literals in `gallery`, `decisions`, `formanswer`, `formrecord`, `store` read `current().owner_id`
(SQL gets it as a parameter, never by formatting). Pronoun forms (he/him/his, she/her/hers,
they/them/their, with the verb agreement) come from one small `text.pronoun(setting)` helper
used where today's text says "he answered"; sentences are written so the neutral form reads
naturally (prefer "the owner's answer" to a pronoun).

**Guard against a wrong id.** Our rows say `jeremy`. A pod started without `OWNER_ID=jeremy`
would treat his every answer as a stranger's. At first start after slice 1 the service records
`owner_id` in a one-row `service_meta` table (both drivers); on every start a mismatch with a
non-empty database refuses to start ("this database was written for owner 'jeremy'; set
ARTIFACTS_OWNER_ID or run `artifacts-admin change-owner`"). `change-owner` rewrites the stored
columns (the same list `rename.py` already walks for agent names) in one transaction. A brand-new
database takes whatever it is given. Deploy order for slice 1 is therefore: manifest PR setting the
three values first (inert), core second.

### 3.3 Hosts, origins, and a fresh install

No house defaults. Today `store.py:321/329` default to `tenfourty.com` and
`https://artifacts.tenfourty.site`, `client.py:34` to `.../api/v1`, `mail.py:28-39` to a Mailgun
region, domain and sender, and `markdown.py:45`, `traps.py:39` and eight templates hard-code the
gallery host.

| Setting | Env | Unset means |
|---|---|---|
| `gallery_origin` | `ARTIFACTS_GALLERY_ORIGIN` | `http://localhost:8000` |
| `content_domain` | `ARTIFACTS_CONTENT_DOMAIN` | `localhost` (page content on a different port-qualified origin; see below) |
| `api_host` | `ARTIFACTS_API_HOST` | derived from `gallery_origin` |
| ~~`mail_from`~~ | ~~`ARTIFACTS_MAIL_FROM`~~ | removed in step 9b: the mail backend plug-in reads its own sender; with no backend, mail is off |
| `display_tz` | `ARTIFACTS_DISPLAY_TZ` | UTC |
| `trusted_ip_header` | `ARTIFACTS_TRUSTED_IP_HEADER` | none (use the socket address); today `ratelimit.py` trusts `X-Real-IP` |
| `session_env` | `ARTIFACTS_SESSION_ENV` | `CLAUDE_CODE_SESSION_ID`; today's `QD_OFFICE`, `ARTIFACTS_SEAT` come from the plug-in (4.2, item 6) |
| `proxy_notes` | `ARTIFACTS_PROXY_NOTES_FILE` | none; the Cloudflare advice ("error code 1010", 100 s cut) is a note text a deployment supplies |

A **fresh install with nothing set**: starts on one port, gallery at `http://localhost:8000`,
owner `owner`/"the owner"/"they", no mail (share-by-email is hidden, not broken), no second
scanner, no sinks, no tracker pages, `agents.txt` and `skill.md` rendered with those words, and
one start-up line that says which settings are defaulted, and a second warning when the server
binds to a non-loopback address with `trusted_ip_header` unset (rate limiting then keys on the
proxy's address; a warning, not a refusal, because a deployment with no proxy is legitimate). Production (anything not loopback)
that leaves `gallery_origin` unset **refuses to start** rather than serving links to localhost.
The CLI has no default endpoint: with `ARTIFACTS_API` unset it says how to set it (and
`artifacts-connect` stores it in a config file); our machines get the old default from the
plug-in's client hook (4.2, item 6), so nothing changes for us. Open point to check in slice 2: how
`hosts.py` serves content on a separate origin when both are `localhost`. **Checked (slice 2):
it does.** `hosts.normalise` drops the port, so `<id>.localhost:8000` routes to the content app,
and browsers resolve `*.localhost` to loopback without DNS. What was missing was the URL
builders, which hard-coded `https://` and no port: `store.content_base` now gives
`http://<id>.localhost:8000` when the gallery is http and the content domain is loopback, and
the content domain may carry a port (unset on a localhost gallery it is `localhost:<port>`).
The path-under-gallery fallback is not built; a browser that does not resolve `*.localhost`
(Safari is the one to test) would need it.

### 3.4 Text variables

Everything an agent or reader is served is a template filled from settings and plug-ins. Variables:
`owner_name`, `owner_they/them/their/theirs/are` (pronoun forms), `gallery_origin`, `api_host`,
`product` (default "artifacts"), `proxy_notes`. Syntax `{{name}}`; an unknown name fails a test
(so a typo never ships). Applies to `onboarding._TEXT` (`agents.txt`), `_SKILL` (`skill.md`),
`news.py`, `templates.USE_WHEN`, the eight `templates/*.html` footers and gallery links
(the house's old footer sentence becomes `{{footer}}`, default empty),
API error text (`api.py:1932,2695,2739`, `decisions.py:837`, `store.py:1808`: "the owner"), CLI
help, and `mail`/share wording. `agent.json` is already neutral and is the pattern. Shipped
comments ("every homelab page shares these", "homelab teal") are reworded.

## 4. Extension points

### 4.1 Discovery and the one emit point

**Discovery:** Python entry points, one group, `artifacts.plugins`. Each entry point names a
function `register(api: PluginAPI) -> None`. Core calls them once at start-up (server and CLI),
in name order. `ARTIFACTS_PLUGINS=none` loads none (tests), `ARTIFACTS_PLUGINS=a,b` loads only
those; unset loads every installed one. `api` offers six registrations (below); nothing else of the
core is a promise.

**One emit point.** Today an action fans out by hand: `events.append` (journal, in the
transaction), then after commit `nudge.emit`, and `partner.enqueue` (outbox, in the transaction)
from `decisions.py` (6 sites), `store.py` (3), `comments.py`, `formanswer.py`, `partner_in.py`.
Replace with `events.emit(conn, Event(...))`, called where `append` is called now:

1. **Journal** (core, always, in the transaction): the existing `events.append`, for kinds that
   are journal kinds. The journal and `/events` stay exactly as they are.
2. **Outbox** (core, in the transaction): one row per registered sink whose `kinds` match.
   Generalises `partner.enqueue`: `outbox(id, sink, event_json, created_at, attempts, next_at,
   delivered_at)`. The sender thread (today `partner.start`) belongs to the core and calls each
   sink's `deliver(event)`; at-least-once, backoff as today, and `event.id` is the idempotency key
   the sink passes on.
3. **After-commit fan-out** (core, best effort): sinks that declared `delivery="after_commit"`
   get `deliver(event)` called once, never raising into the caller (today's `nudge.emit`:
   fire-and-forget, `event_id` only after commit).

`Event` is a frozen dataclass: `kind`, `artifact_id`, `agent` (who it is for), `actor`, `ref_id`,
`summary`, `note`, `session`, `data` (a small JSON-able dict the producer may add), `id`.
Event kinds have two namespaces: **journal kinds** (today's `events.KINDS`, which agents poll)
and **bridge kinds** (today's partner kinds: asked, answered, withdrawn, replaced,
message_added, edited, set_aside, reopened). Bridge kinds are not in the journal and agents never
see them. The chasing work adds, as bridge kinds, `answer_unpicked`, `receipt_lapsed` (the two
single nudges, delivered to the agent's own sink) and **`answer_dropped`** (after a day, to the
deployment's target); they are listed in `events.BRIDGE_KINDS` and sink `kinds` may name any. A
sink routes by kind and, optionally, target: the core's generic webhook sink reads
`REAM_SINKS_FILE` (also `ARTIFACTS_SINKS_FILE`; a TOML file of `[[sink]] name, kinds, url,
signing_key_file, key_id`, and optionally `label`, `allow_http`, `allow_private`; built, `src/ream/webhook.py`), so
"`answer_dropped` goes to this URL" is configuration, not code. Chase bookkeeping (a
once-only marker per subject and stage) is the chasing design's table, not this one's.

### 4.2 The six interfaces

1. **Event sink.** `api.sink(name, kinds, deliver, delivery="outbox"|"after_commit",
   validate=None)` (built as `api.sink(name, kinds, deliver, delivery="outbox", *, label=None)`: no
   `validate`, and a kind outside `events.KINDS`/`events.BRIDGE_KINDS` is refused). `deliver(event)` returns normally on success, raises to retry (outbox) or is
   logged (after commit). Replaces `nudge.py`, the quarterdeck envelope in `partner.py`, and
   Telegram or any future target.
2. **Answer source.** `api.answer_source(name, label, *, relay=False, verified_label=None,
   route_label=None)`. Declares a stored `answer_source` value, how the gallery words it ("You
   answered this in {label}", "verified by {verified_label}"), and the listening/route wording
   (`listening.py` `SENT_TO_SLACK`, `SLACK_DELAYED`: states keep a neutral id, the label is the
   plug-in's). `relay=True` means an agent may record it (today's `ANSWER_SOURCES`:
   terminal, telegram, in-person, other are core built-ins; `slack` is not claimable by a relay
   and comes only from the plug-in's inbound route).
3. **Answer hook** (replaces `decisions.answer(in_transaction=...)` for plug-ins). `api.on_answer(
   name, fn)`: `fn(conn, decision, answer)` runs **inside** the answer's transaction, after the row
   is written and before the commit; raising aborts the whole answer. Only an answer-source
   plug-in that vouches for its own inbound answers may use it (today: `partner_in`, which marks
   the question answered as the owner via the partner and must not half-apply). Everything else is
   a sink. The hook is for answers the person gave through the plug-in's own inbound path
   (`decisions.answer`); an agent's relay (`record_elsewhere`) never runs it. `slack` and `page` are
   reserved names: no plug-in may register them as a source.
4. **Scanner.** `api.scanner(name, scan, on_error="skip"|"block")`: `scan(text) -> list[Finding]`,
   called alongside the core's pattern scan on publish and on comment save
   (`store.py:449`, `comments.py:1639`). `on_error` preserves today's `ggscan` behaviour exactly
   (to be read from `ggscan.py` in the slice, not assumed here). Core ships the pattern scan
   only.
5. **Mail backend.** `api.mail_backend(name, send, enabled)` with `send(to, subject, text)` (as
   built; not a message object); core has `mail.send(to, subject, text)` and
   `mail.enabled()` that ask the one registered backend (or none: off). Mailgun has moved out (9b);
   the sender and region/domain are settings the backend reads.
6. **CLI command and client hooks.** `api.cli(name, main, help)` registers `artifacts <name>`
   subcommands (and keeps the standalone console script working through a one-line entry in the
   plug-in's own `pyproject`). `api.client_defaults(api_url=..., session_envs=[...],
   news=[...])` supplies the CLI's default endpoint, the session-label env names (`QD_OFFICE`,
   `ARTIFACTS_SEAT`) and news lines. The tracker pages (`artifacts-beads-page`,
   `artifacts-bead-metrics`) are CLI commands.

   *Trust and cost of the CLI hooks (7b part 4).* A plug-in's `register` runs in every
   command-line process, on every start, with the caller's rights (it can read the key file):
   it must be cheap and side-effect free (no network, files or subprocesses), and the user
   installs only plug-ins they trust. `api_url` applies only when `REAM_API`/`ARTIFACTS_API` is
   unset and keys are not bound to an endpoint; it is held to the request path's rules (https,
   or http to loopback; valid port; no userinfo, query or fragment; trailing "/" dropped).
   A `session_envs` value must match `^[a-z0-9][a-z0-9._-]{0,63}$` (lower-cased) or the next
   source is tried. News ids are dates not later than today (UTC), text one printable line;
   the "seen" state is kept per source and a plug-in's entry never moves the core's marker. With
   `REAM_PLUGINS` unset a plug-in that fails to load or register is skipped with a warning on
   stderr; naming plug-ins makes a failure exit 2; the server's start-up still stops.

Plus **text** (as built, a variable or section value that is a function is cached for 60 s,
`textvars._TEXT_TTL`, so a slow or failing one is not called on every request):
`api.text_vars({...})` (extra template variables) and `api.text_section(slot,
fn)` for named slots in `agents.txt`/`skill.md` (`proxy`, `tools`, `integrations`), so the
tracker-page paragraph of `skill.md` ships with the plug-in. And **routes**:
`api.router(router, host)` mounts an inbound route on a dedicated host (the partner's inbound
webhook, today `ARTIFACTS_PARTNER_INBOUND_HOST`); core never mounts it on the gallery host.

### 4.3 What the core ships with no plug-in

Journal, outbox and sender thread, the generic **signed webhook sink** (built: an Ed25519-signed JSON
POST of the generic envelope `{key_id, sent_at, event}` to configured URLs, https and public
addresses only unless a sink opts out, no redirects, 64 KiB cap; see `docs/plugins.md`; stays core as ruled, the part of `partner.py`
that is not quarterdeck's contract), the pattern scanner, built-in answer sources
(`page`, `terminal`, `telegram`, `in-person`, `other`), no mail, no tracker pages. Everything a
plug-in does can be turned off by not installing it.

### 4.4 Transaction semantics

| Step | Where | If it fails |
|---|---|---|
| Journal row, outbox rows | the action's transaction | the action rolls back |
| `on_answer` hook | the answer's transaction, before commit | the answer rolls back; the caller sees a 5xx with the plug-in's name |
| Outbox `deliver` | sender thread, after commit | retried with backoff; after N attempts parked and counted, never dropped |
| After-commit sink | the request thread's executor, after commit | logged and counted; the action is already done |
| Scanner | before the write | per `on_error` |

An outbox row names its sink. If a sink is not registered at start (plug-in removed), its rows
stay pending, `artifacts-admin outbox` shows them by sink, and nothing is delivered or deleted;
re-installing drains them.

### 4.5 Failure isolation

A plug-in cannot take down a request or the sender thread: every call into one is wrapped; an
exception becomes a log line with the plug-in and interface and a counter (there is no `/metrics` route: the counts
are `plugin_api.failure_counts()` and `sinks.status()`, shared across workers through the
`plugin_failure` table), and the per-sink circuit breaker pauses a
sink that fails N times running (outbox rows wait). Exceptions: the answer hook and scanner
`on_error="block"` fail the action by design (4.4). A plug-in that raises while registering, or
declares an unsupported API version, **stops start-up** with its name: a missing scanner or
source silently absent is worse than a pod that does not start.

### 4.6 Versioning

`artifacts.plugin_api.API_VERSION = (1, 0)`. A plug-in's `register` is passed `api` whose
`api.version` it checks, and declares `REQUIRES_API = "1"` as module data; core compares major
and refuses a mismatch (start-up failure above). Minor additions only add registrations or
optional fields; removing or changing one is a major bump with one release of both. The `Event`
dataclass only gains fields (plug-ins must accept unknown ones). The plug-in repo's CI pins the
core it builds against (section 7), so a core change that breaks the API is a failing PR on the
core side first (7.4).

## 5. Fixed CHECK values and migration

The constraints that name house values: `decision.answer_source` in (`page`, `terminal`,
`telegram`, `in-person`, `other`, `slack`) (`schema.sql:260` and the widening at `:861-868`;
SQLite step 6 in `db.py`, `_widen_answer_sources`) and `event.kind` (`schema.sql:370`, SQLite
steps 11/16). Both are widened by migration every time a kind or source is added, which is what a
plug-in cannot do.

**Choice:** drop both CHECKs, validate in code. `answer_source` is checked against the core
built-ins plus the registered sources at write time (`decisions.STORED_SOURCES` becomes
`decisions.stored_sources()`); `event.kind` against `events.KINDS`. Existing rows are untouched
and stay readable: `'slack'` rows keep `'slack'`. A row whose source is not registered (the
plug-in is absent) still reads and is shown by its raw value, wording from a generic
"answered via <value>", never an error.

- **Postgres:** `alter table decision drop constraint if exists decision_answer_source_check`
  (the actual name read from `pg_constraint` in the slice, both the inline-created and re-added
  names handled) and the same for `event_kind_check`. Idempotent, in `schema.sql`'s style.
- **SQLite:** a new numbered step using the writable_schema approach of
  `_widen_answer_sources`, but removing the `check (...)` clause rather than replacing the list,
  with the same self-proving guard (the stored SQL must no longer contain the clause or the
  step raises and `user_version` stays). A table rebuild is the fallback if the text is not the
  expected shape. Order the event step with the receipts slice's `receipt_set` step so only one
  `event` rewrite happens.
- **Which values the plug-in uses:** `'slack'` stays the stored name for us, declared by the
  plug-in (a stored value is data, not code). The generic webhook sink's inbound counterpart, if
  ever built in core, would use `partner`.
- A test asserts a row written under the old CHECK and a row written after migration both read
  through the API unchanged, on both drivers.

Other fixed values with the same risk: `decision.kind` (`question`, `review`, `form`) stays a
CHECK (core concepts); `receipt.state` stays (core); `inbox` item kind `slack_question` is a
served value, not stored (section 6).

## 6. What moves, what stays, what is renamed

**Stays in core (generic):** `events`, outbox and sender (from `partner.py`'s generic half),
signed-webhook sink, pattern scanner (`scan.py`, `secret_shapes.txt` with comments reworded),
`ratelimit` (header becomes a setting), the answer-source registry, `listening`, `inbox`,
`onboarding`/`news` (as templates), `republish` (generic example reworded), gallery.

**Moves to the plug-in repo:**

| From | To plug-in as |
|---|---|
| `partner.py` quarterdeck envelope, `verified_by` mapping (`:292-293`), `for_offices` mapping | event sink `quarterdeck` |
| `partner_in.py` (inbound route, `verified_by == "quarterdeck"`, `slack_id`, `answer_here`) | answer source `slack` + `on_answer` hook + router |
| `nudge.py` | after-commit sink `doorbell` |
| `ggscan.py` | scanner `gitguardian` (moved and deleted from core, 9c) |
| `mail.py` Mailgun | mail backend `mailgun` (moved and deleted from core, 9b) |
| `beadpage.py`, `beadmetrics.py` and their two console scripts | CLI commands |
| `client.py:34` default endpoint, `QD_OFFICE`/`ARTIFACTS_SEAT`, `news.py` lines naming GitGuardian and beads, the skill's tracker section | `client_defaults`, `text_section` |

`partner.py`'s two halves are split in slice 5: the outbox and generic envelope to `outbox.py` and
`webhook.py` (core), the quarterdeck specifics stay behind until slice 9 moves them.

**Renames (API clients and stored data keep working):**

| Today | Neutral | Compatibility |
|---|---|---|
| decision field `for` / `for_offices` | `audience` | requests accept both for one release (new wins); reads emit both; stored column unchanged; error text "lists OTHER offices" -> "other teams" |
| CLI `--office`, `--all-offices`, `<office>/<ref>` | `--team`, `--all-teams`, `<team>/<ref>` | old flags remain aliases, hidden from help |
| inbox kind `slack_question` | `partner_question` | reads emit `kind: partner_question` and `legacy_kind: slack_question` for one release; kind filters accept both |
| listening states `SENT_TO_SLACK`, `SLACK_DELAYED` | `SENT_TO_PARTNER`, `PARTNER_DELAYED` | the old ids are emitted as `legacy_state`; wording from the answer source's label |
| `answer_source` value `slack` | unchanged (stored, declared by plug-in) | none needed |
| gallery text "Open in Slack", "waiting for quarterdeck" | label from the answer source / sink | per plug-in |
| `docs` wording "office" in core docs | "team" | prose only |

"One release" means: removed only after `news` has announced it for a release and `artifacts-admin
status` shows no client still sending the old form (the access log counts it).

## 7. Build and deploy

Today `.forgejo/workflows/ci.yml` runs the suite on Postgres and SQLite, builds
`git.tenfourty.site/runcove/artifacts:<sha12>@sha256:...`, logs the `PUBLISHED image=... digest=...`
line that homelab's pin lander binds to main's CI run, and `pin-pr.sh` opens the pin PR on the
homelab repo. After the split, the deployed image is core **plus** our plug-ins.

### 7.1 Options

**(A) The plug-in repo builds the deployed image (recommended).** The core repo's CI builds and
pushes a **plug-in-free core image** (`runcove/artifacts-core:<sha12>@sha256`), plus the wheel.
The plug-in repo has a Dockerfile `FROM` that core image (digest in a `core.lock` file in that
repo), installs the plug-ins, runs both suites, builds `runcove/artifacts:<sha12>@sha256` (the
name production already uses) and opens the homelab pin PR with a copy of `pin-pr.sh`.
- Core change to production: core main -> core CI green -> core image -> a bot PR on the plug-in
  repo moves `core.lock` (same technique as `pin-pr.sh`) -> plug-in CI runs the combined suite,
  builds the deployed image, opens the homelab pin PR -> homelab maintainer merges. One more hop
  than today; the lock-bump PR may auto-merge when the combined suite is green (decision 10.3).
- A plug-in change goes straight to its own main, builds with the locked core.
- Core repo "holds only the core": it knows nothing of the plug-ins except that a CI job
  (7.3) may clone them.
- Cost: homelab's pin lander binds a digest to a run of the **artifacts** repo's main CI; it
  must learn to bind to the plug-in repo's run (a homelab change, requested through its
  maintainer before the cut-over slice).

**(B) The core repo builds the deployed image**, cloning the plug-in repo at a ref in
`plugins.lock` in the core repo. Production path is unchanged (same CI, same pin PR, same lander).
But the core repo then holds a reference to ours and every plug-in change needs a commit to the
core repo to ship; "the repo holds only the core" is strained and ownership of "what runs" is
blurred.

**(C) A third image repo** (or the homelab repo) assembles core digest + plug-in ref into the
image. Cleanest ownership, two lock files, one more repo and CI to keep. Worth it only if a
second deployment appears.

### 7.2 Recommendation

(A). It keeps the core repo clean without making a third repo, and the one-hop cost is
automatable. The lander change is the only external dependency, and it is small.

### 7.3 Core CI: no plug-ins, and with ours

Two jobs, both on Postgres and SQLite as today:
- **core-bare:** `ARTIFACTS_PLUGINS=none` and nothing installed: the whole core suite. This is the
  contract that the core works for anyone; it is the required check.
- **core-with-house:** clones the plug-in repo's main, installs it into the environment, and
  runs the core suite **and** the plug-in suite against this core. Initially informational
  (it fails if the plug-in repo is broken for reasons unrelated to this change); promoted to
  required once it has been stable for two weeks. It is how a core change learns it broke us
  before the lock-bump PR does.
Plug-in repo CI mirrors it: its own suite against the core in `core.lock` (required), and against
core main (informational).

### 7.4 The plug-in API test

The core has `tests/test_plugin_api.py` with a throwaway in-tree plug-in per interface (a fake
sink, source, scanner, mail backend, CLI command), including the failure cases (raises on
register, wrong version, sink raises, hook aborts the answer). The plug-in repo's tests do not
re-test the core; they test the house behaviour (envelopes, wording, Mailgun requests).

## 8. Slicing

Each slice lands alone, with no behaviour change for us. Every slice that touches served text
ships a golden test: with our settings, `agents.txt`, `skill.md`, `agent.json`, the eight
template footers, `news` and the CLI `--help` render byte-identical to today's. Tests on both
drivers; CI only (not on the management machine).

1. **Owner settings** (first, as ruled; built on `ci/core-slice1-owner`: unset means the neutral
   `owner`/`the owner`/`they`, so a pod without the three variables refuses to start on our database). `settings.py` with the three owner fields,
   `service_meta`, the wrong-id guard, `change-owner`, `keys`/`folders`/SQL literals/25 call sites
   switched, `text.pronoun`. Manifest PR setting `OWNER_ID=jeremy`, name and `he` lands before
   the pin. The remaining `os.environ` reads are untouched. The client's `jeremy` fallback for an
   answer row with no `answered_by` (`decide._fmt`) is dropped: the server stamps every answer with
   the owner id, so a row without one prints `?`, as a form's does. The served texts that still
   say "Jeremy"/"he" (template footers, `news`, CLI `--help`) are pinned by digest in
   `tests/test_owner.py` until slice 3 re-takes them.
2. **All other settings** (built on `ci/core-slice2-settings`, 6 Nov 2026). `Settings` now holds
   every server variable (gallery origin, content domain, API host, display zone, trusted IP
   header, mail, nudge, ggshield, the partner groups, backup dir, tools dir, tracker command,
   browser path, and the CLI endpoint); `os.environ` is read only in `settings.py`, in
   `serve.py` (DATABASE_URL, ARTIFACTS_MAINTENANCE, ARTIFACTS_PACK_VERSIONS), in `admin.py`
   and in the command-line tools' own modules (`client`, `decide`, `republish`, `news`), held
   by `scripts/guards/no-environ-guard.sh` (selftest, `--tree`, lefthook, CI).
   `validate_for_serve()` collects every problem (owner, origin shape, content domain, header
   name, mail group, both partner groups) and runs before the key files are opened. Origins
   and the mail sender lose their defaults: mail is on only when key, API, domain and sender
   are all set; `gallery_origin` unset means `http://localhost:8000` and refuses to start when
   `ARTIFACTS_BIND_HOST` (the image sets `0.0.0.0`) is not loopback; the start-up log names the
   defaulted owner and origin settings. **Manifest values set first** (our pod already sets
   the rest): `ARTIFACTS_TRUSTED_IP_HEADER=X-Real-IP`, and the image sets
   `ARTIFACTS_BIND_HOST` itself. **Kept for our machines**: the CLI's default endpoint stays
   `https://artifacts.tenfourty.site/api/v1`, now as `settings.HOUSE_DEFAULT_API` behind
   `ARTIFACTS_API`; many agents run the tools with no variable set, and the plug-in client
   hook that replaces it does not exist before slice 7, which also removes the constant.
   **Not done here**: `session_env`/`QD_OFFICE`/`ARTIFACTS_SEAT` stay in `client.py` (slice 7);
   `proxy_notes` and the hard-coded gallery links in `markdown.py`, `traps.py` and
   `beadpage.py` wait for the text templates (slice 3); the Mailgun key, nudge token and
   ggshield token are still read as values (their Secrets are mounted as variables), the move
   to key files needs a manifest change; `display_tz` unset keeps meaning the host's zone,
   not UTC. The fresh-install content origin works (3.3). Docs: `AGENTS.md`;
   the runbooks need no change (a settings runbook is left to do).
3. **Text templates.** `{{var}}` mechanism, then `onboarding`, `news`, templates, API error text,
   CLI help, share mail move to variables. Golden test. No plug-in yet: the house sections are
   settings (`proxy_notes`, `footer`) for now.
   **Built (`ci/core-slice3-text`, 6 Nov 2026).** `src/artifacts/textvars.py` is the
   mechanism (`{{name}}`, capitals follow the name, unknown name raises; `tests/test_textvars.py`
   renders every served text under three owners). Moved: `agents.txt`, `skill.md`, `news`
   (rendered by `news.entries()`), `templates.USE_WHEN`, the eight templates and the shared CSS
   (footer, gallery link, eyebrow host, owner words), `markdown.py`/`beadpage.py` gallery links,
   `traps.py` gallery host. New settings: `ARTIFACTS_PRODUCT`, `ARTIFACTS_FOOTER`,
   `ARTIFACTS_HOST_LABEL`, `ARTIFACTS_PROXY_NOTES_FILE` (two paragraphs split by a `%%` line: the
   proxy note, then the clause about held requests). The command-line tools' own words use
   `render(client=True)`: owner unset there means `settings.HOUSE_DEFAULT_OWNER` and the gallery
   origin falls back to the API's origin (both go in slice 7). The skill-directory copies of the
   templates are rendered with house values by `scripts/sync-templates.py`.
   **Built, complete (6 Nov 2026).** The API error strings that name the owner (`api.py`,
   `decisions.py`, `store.py`, `review.py`, `receipts.py`, `keys.py`), the command-line tools'
   `--help` and messages (`client.py`, `decide.py`, `connect_cli.py`) and the share-mail wording
   (the mail's subject and body in `gallery.py`, the door page in `content.py`) all use
   variables. `--help` is filled by `client._HelpFormatter` BEFORE argparse wraps, so the wrap does
   not depend on the name's length; runtime messages go through `textvars.words()`, which takes
   literals only, never a user's text. `tests/test_owner.py` digests (both `--help`s included)
   are unchanged: with our settings every word is byte-identical. The host stamp was already the
   `host_label` setting (only a comment in `thumbs.py` named it).
   **Manifest values to set before the pin** (all optional for a house that wants the neutral
   wording; ours sets them to keep today's text): `ARTIFACTS_FOOTER`, `ARTIFACTS_HOST_LABEL=<the host's label>`,
   `ARTIFACTS_PROXY_NOTES_FILE` (a ConfigMap file: the proxy note, a `%%` line, the held-request
   clause). **Carried forward, not slice 3**: the nudge alert names (`nudge.py`), the gallery's
   profile chip and thread-author label (`gallery.py`) and route docstrings in OpenAPI still say
   "Jeremy"; they are gallery wording, taken with slice 6.
4. **One emit point.** `events.emit`; core outbox generalised from `partner.enqueue`; the
   existing nudge and partner code become two in-core sinks behind it; fan-out call sites
   (13) collapse to `emit`. Outbox rows from before the slice keep delivering (the migration
   renames the table or reads both). No plug-in loader yet. Order first, because slices 5 to 8
   depend on it.
   **Part 1 built (`ci/slice4-emit-part1`, 6 Nov 2026).** `events.Event`, `events.emit` (returns an
   `Emission`; the caller runs `.fire()` after commit for the after-commit sinks), `events.BRIDGE_KINDS`,
   `sinks.py` (registry, `outbox` table with a sender thread always started by `serve`). Migration
   approach: a NEW `outbox` table (schema.sql, SQLite step 20) and `partner_outbox` untouched; the
   partner sink is a sink with a `stage` hook that calls `partner.enqueue` in the transaction, because
   its per-question version counter and ordering live in that table and its own sender still drains it
   (so old rows keep delivering by construction; merging the two tables is not needed for the
   plug-in design and is deferred). Outbox columns are the spec's plus `claimed_until`, `parked_at`,
   `last_error`, `last_error_at`. Converted call sites: `formanswer.py` and the set-aside loop in
   `store.py`. Not yet: the other fan-out sites (part 2), the per-sink circuit breaker and
   the per-sink circuit breaker (4.4), and `change-owner` over `outbox.event_json`.
   **Part 2 built (`ci/slice4-emit-part2`).** All remaining fan-out sites emit through `events.emit`
   (decisions, store, comments); `partner_in.answer_here` stays on `partner.enqueue_foreign` (no
   artifact or agent to put on an `Event`). `deliver` has a protocol bound: it must return within
   `sinks.DELIVER_TIMEOUT_SECONDS` (30 s), enforced by running it in a daemon thread; a timeout is a
   failure with backoff. `artifacts-admin outbox` (counts by sink) is in; the circuit breaker (4.4)
   is in; the generic webhook sink is built (step 10, `ci/webhook-sink`).
5. **Drop the CHECKs, source registry** (section 5), receipts' `receipt_set` step folded into
   the same `event` rewrite if receipts slice 1 has not shipped; otherwise a second rewrite.
   **Part 1 built (`ci/slice5-checks-part1`).** Both CHECKs dropped (Postgres by a `pg_constraint`
   lookup in `schema.sql`; SQLite step 21, `_drop_house_checks`, self-proving, one rewrite of
   `event` since `receipt_set` shipped as step 16). Validation moved to code: `events.append`
   against `events.KINDS`, `decisions.require_stored_source` against `STORED_SOURCES`. No table
   rebuild fallback (an unknown stored shape fails start-up). **Part 2 built
   (`ci/slice5-sources-part2`).** `decisions.register_source(name)` (idempotent; refuses a core
   name or one outside `[a-z][a-z0-9-]{0,31}`), `decisions.stored_sources()` (core's
   `CORE_SOURCES`, then the registered ones; `slack` is still registered in core until slice 6)
   and `decisions.is_known_source()` replace `STORED_SOURCES`. A row whose source is not
   registered reads through the API unchanged, and the gallery shows it as "Answered via
   <value>, not on the page" with a "via <value>" badge, claiming no relayer.
6. **Renames with aliases** (section 6), `listening`/`inbox` neutral ids, gallery wording from the
   answer-source label (still declared in core for `slack` at this point).
   **Built (`ci/slice6-renames`).** `audience` joins `for` on `POST /decisions` (either; `audience`
   wins; an error names the field the caller sent; "lists other teams"), and every decision read
   carries both (`decisions.public`, the partner-home view). `artifacts-decide wait`/`listening`
   take `--team` and `--all-teams`; `--office`/`--all-offices` stay, hidden from `--help`
   (`argparse.SUPPRESS`, same destinations); `ask` takes `--audience` beside `--for` and sends both
   keys; `--json` carries `team` beside `office`. Session labels read `<team>/<ref>` (same format).
   Inbox kind is `partner_question`, with `Item.legacy_kind = slack_question`, `Item.view()` emitting
   both and `inbox.kind_matches` accepting either (no API route serves the kind today; the inbox
   is gallery-only). Listening states are `sent_to_partner`/`partner_delayed` with `legacy_state`
   on the two renamed ones. `register_source(name, label=...)` and `decisions.source_label()`;
   `sinks.Sink(label=...)` and `sinks.label_of()` (`partner` is "quarterdeck"). "Open in Slack",
   "Asked in/Settled in/from Slack", "via Slack" and "waiting for quarterdeck" and the listening
   sentences take those labels, so our text is byte-identical. `news` announces the renames (the
   clock for "one release" starts there). Pins re-taken on purpose in `tests/test_owner.py`:
   agents.txt, skill.md and the `artifacts-decide` help (the `artifacts-publish` help moved by one
   word, `<team>/<session id>`). Left alone: the partner wire body (`for`, a protocol with
   quarterdeck, moves with its plug-in), the `QD_OFFICE` variable, stored columns.
7. **Plug-in loader and API v1**, the six interfaces, failure isolation, versioning, the test
   plug-in, core-bare CI job. Still nothing moved.
   **7a built (`ci/slice7a-loader`).** `plugin_api.py`: `API_VERSION = (1, 0)`, `PluginAPI`
   (`version`, `sink(name, kinds, deliver, delivery, *, label)`, `answer_source(name, label, *,
   relay, verified_label, route_label)`), and `load()`, called by `serve` after the migration and
   owner check: entry-point group `artifacts.plugins`, name order, `ARTIFACTS_PLUGINS` (a
   `Settings` field: unset all, `none`, or a list whose unknown name stops start-up). A `register`
   that raises, a `REQUIRES_API` major mismatch or an unknown listed name stops start-up as
   `PluginError` naming the plug-in. A plug-in's `deliver` is wrapped: a raise is logged with
   plug-in and interface and counted (`plugin_api.failure_counts()`, also `plugin_failures` in
   `sinks.status()`), then raised on so the outbox backs off and after-commit stays best effort.
   `decisions.register_source` takes `relay`, `verified_label`, `route_label`
   (`relay_sources()`, `verified_label_of`, `route_label_of`; the relay check and the 422 text read
   `relay_sources()`, equal to `ANSWER_SOURCES` with no plug-in). Not in 7a: `validate` on a sink,
   the per-sink circuit breaker, the other registrations (7b), the CLI side of loading. Nothing
   moved; core still registers `slack` and the `nudge`/`partner` sinks itself.
   **7b part 1 built (`ci/slice7b-part1`).** `api.on_answer(name, fn)`: `fn(conn, decision, answer)`
   (`answer` a `decisions.Answer`) runs inside `decisions.answer`'s transaction after the row is
   written and before the commit, only for answers stored with a source the same plug-in registered
   (`answer_source` first, else start-up stops); a raise aborts the answer as
   `decisions.AnswerHookFailed` naming the plug-in, and is counted as `<plug-in>:on_answer`.
   `decisions.answer` accepts any registered source, not only `page`/`slack`. Circuit breaker in
   `sinks.py`, per sink (shared across processes since part 5): 5 failures running (a raise or a timeout), from at least two different rows, open it for 60 s;
   outbox rows of an open sink wait uncounted, after-commit deliveries are skipped; after the
   cooldown one trial goes through (success closes, failure re-opens). `sinks.status()` shows
   `open_breakers`. Left for 7b: cli/client, text and router hooks, CLI loading of
   plug-ins, cross-process failure counts and breaker state, the core-bare CI job.
   **7b part 2 built (`ci/slice7b-part2`).** `api.scanner(name, scan, on_error="skip")`
   (`scanners.py`): runs where the core takes its second opinion (`store._check_secrets`, after a
   clean pattern scan and a clean `ggscan`, bodies and the fields whose caller asks for it; and
   `comments.second_scan`), so a plug-in scanner replaces `ggscan` in place when 9c moves it. Any
   finding refuses as a pattern finding does. A raise or a non-list answer is counted as
   `<plug-in>:scanner`; `skip` judges the text on the rest, `block` fails the publish
   (`ScanFailed`) or leaves the comment unscanned for the sweeper to retry. `api.mail_backend(name, send,
   enabled)` (`mail.py`): one at most (a second stops start-up); `send(to, subject, text)` keeps
   today's three arguments rather than a message object; `mail.enabled()`/`mail.send()` ask it,
   and with none registered mail is off (the built-in Mailgun code went out in 9b); a raise becomes
   `MailFailed` (so callers are unchanged) and counts as `<plug-in>:mail`.
   Review fixes: a `block` scanner fault on a comment's second scan leaves the comment intact and
   unscanned and hidden reversibly (`withdrawn_kind = 'scan_blocked'`, nothing blanked; the sweeper
   retries and a clean scan lifts it, a finding withdraws it for good; only a publish fails
   closed); a clean answer from a plug-in scanner counts as scanned only when no relay is
   configured (a configured relay that is down stays unscanned); a finding's shape must be a non-empty
   `str` of at most 64 characters, else the answer is a scanner error; each scanner call is
   bounded (`SCAN_TIMEOUT_SECONDS`; a timeout is a scanner error); a backend whose `enabled()`
   is false or raises refuses to send (`MailFailed`). A held comment is hidden with no event, and
   replies to it fail as for a withdrawn comment until it reappears; a hold is lifted by a clean scan,
   or when no plug-in scanner is registered and no relay is configured (shown, unscanned, as on
   main); a hide never touches a comment already stamped scanned, and a lift stamps in the same
   transaction. A scanner raising `SystemExit` or any `BaseException` is a scanner error. A hung
   scanner costs one daemon thread per call (the timeout's caller bounds the rate; threads are not
   reclaimed). **Scope:** plug-in scanners run in
   ggscan's slot (bodies, previews, `gitguardian=True` fields, agent comments' second scan),
   not on titles, decision fields or human comments; widening that waits for 9c, when ggscan
   moves into a plug-in.
   **7b part 3 built (`ci/slice7b-part3`).** `api.text_vars({name: str | fn})`: each name must
   start with the plug-in's own prefix (its name lower-cased, anything but letters and digits
   made `_`, then `_`; `beads-page` -> `beads_page_`), so it cannot take a core name or another
   plug-in's, and a core variable name is refused (open in the spec; simplest reading: a
   namespace, not a reserved list). A value may be a function, read each time a text is served.
   Core texts name none of them: they are for a plug-in's own words. `api.text_section(slot,
   fn)`: slots `proxy` and `tools` (agents.txt) and `integrations` (the end of agents.txt and of
   skill.md), the spec's three; `fn(doc)` gets `agents.txt` or `skill.md` and returns the
   paragraph or `""`; sections follow in registration order, one per slot per plug-in. The
   templates carry `{{section_<slot>}}`, empty with no plug-in, so every served-text digest is
   unchanged. A value or section that raises, is not a string or exceeds 20,000 characters
   renders as nothing (served words are not a security check, so failing quiet is the safe
   side), is counted as `<plug-in>:text` and logged by exception type only. `api.router(router,
   host)`: `router` is a FastAPI `APIRouter`, mounted in its own app (no docs, no schema, a 422
   that never echoes input) and served only when the normalised Host equals `host`; the host may
   not be the gallery, API, partner-inbound or content domain (or under it), nor another
   plug-in's, and `create_app` checks plug-in hosts last so one can never shadow a core host.
   The route has no gallery or key authentication (it checks its own caller, as `partner_in`
   does). A fault in it answers a bare 500, is counted as `<plug-in>:router` and logged by type
   only. Any registration error stops start-up naming the plug-in.
   **7b part 4 built (`ci/slice7b-part4`).** `api.cli(name, main, help)` and
   `api.client_defaults(api_url=None, session_envs=(), news=())`, with the CLI side of loading
   (`plugin_cli.py`, standard library only, so a client machine without the server stack can run
   it). Every command-line entry (`ream`, `artifacts-publish`, `-decide`, `-connect`) runs the
   same selection as the server (`REAM_PLUGINS`/`ARTIFACTS_PLUGINS`: unset all, `none`, a list
   whose unknown name stops) and hands each plug-in a `CliPluginAPI`: `cli` and
   `client_defaults` work, the server registrations are accepted and ignored, so one `register`
   serves both processes. A registration fault stops the tool (exit 2) naming the plug-in and the
   exception's type, never its text. `cli`: a name matching `[a-z][a-z0-9-]{0,31}`, not a verb
   the core owns (`publish`, the decide verbs, `admin`, `republish`, `connect`, `help`; also
   `beads-page` and `bead-metrics` until 9a removed them), not taken by another plug-in; `ream <name>` calls `main()` as a
   console script is called (argv[0] `ream <name>`), a raise prints the type only (exit 1), and
   `ream --help` gains a "From plug-ins" section only when one is installed (with none the text is
   unchanged, the HELP_DIGEST tests as they were). `client_defaults`: `api_url` (plain http(s),
   no userinfo/query; one plug-in at most) is the endpoint when `REAM_API` is unset, ahead of
   the house constant; `session_envs` (upper-case names, none containing KEY, TOKEN, SECRET,
   PASSWORD or CREDENTIAL, at most 8) are asked for the office part of the default session label
   before `QD_OFFICE` and the seat; `news` is `(YYYY-MM-DD[a-z], text)`, at most 20, merged
   newest first into the tools' once-per-machine news. A refused call changes nothing.
   **Choices:** the house fallbacks (`HOUSE_DEFAULT_API`, `QD_OFFICE`, the seat, the house owner
   words) stay in the core as the lowest default, because with no plug-in the tools must behave
   as today and the house plug-in does not exist before step 9; they go when it does. The
   `session_env` setting (the variable holding the session id) is not made configurable here:
   `CLAUDE_CODE_SESSION_ID` stays. The tracker-page verbs (`beads-page`, `bead-metrics`) stayed
   core verbs until 9a, which removed them (see step 9).
   **7b part 5 built (`ci/slice7b-part5`); slice 7b is complete.** Breaker state and plug-in
   failure counts now live in the database: tables `sink_breaker` (sink, failures, open_until,
   failed_keys, opened_at) and `plugin_failure` (plugin, interface, failures), SQLite step 22,
   `schema.sql` for Postgres; no stored name changed. `sinks.bind(db)` points the state at a
   database (`serve.py` binds, `sinks.stop()` unbinds; `sinks.shared()` reads it); the
   breaker rules are the same code for both copies (`_fail_state`), run read-modify-write in
   one transaction (`for update` on Postgres, `begin immediate` on SQLite), so the cooldown
   trial is taken by exactly one process. **Choices:** (a) with no database bound (the command
   line tools, unit tests) or one that cannot be reached, the old in-memory copy answers, so
   a breaker can never stop a delivery by failing itself; (b) failure counts are
   counted in memory at once and stored by ONE long-lived daemon writer (started on the first
   count with a database bound, stopped by `sinks.stop()`), which coalesces the queued
   increments into one upsert per key and one connection per flush, off the caller's thread
   because the caller may be inside its own transaction (the answer hook is) and on SQLite a
   second writer would wait for that commit; a route faulting on every request therefore costs
   a dict update, not a thread and a Postgres connection each; if the database is down the
   increments stay queued (bounded to 1000 keys), the writer retries every two seconds and
   logs once per failure streak; `failure_counts()` waits (bounded) for the writer to go idle
   and reports the stored total plus this process's still-unstored increments, so another
   process's share is kept and nothing is counted twice; `breaker_peek` and `open_breakers`
   read on a plain connection, not a write transaction (on SQLite that is `begin immediate`);
   (c) `late_failures` (after-commit raises) stays per process: it is a
   process counter by its own wording and has no breaker meaning. CI job `core-bare`
   (`.forgejo/workflows/ci.yml`): asserts no `ream.plugins`/`artifacts.plugins` entry point is
   installed, sets `REAM_PLUGINS=none`, and runs the whole suite on Postgres and on SQLite
   (the browser tests stay in `test-sqlite`). `pin` does not list it in `needs` yet: the pin
   lander names its required checks in the homelab repo, so making it required (7.3) is that
   change, not this one. `core-with-house` was left out in step 8 (below).
8. **The plug-in repo and its pipeline** (homelab office creates the repo; lander change). It
   builds an image identical to today's from an empty plug-in set, opens a pin PR, and one real
   pin PR is merged as proof before anything moves. Core starts publishing the `-core` image and
   the lock-bump PRs; `core-with-house` job added informational.
   **Core half, part 1 built (`ci/core-image`, 9 Nov 2026).** On a push to `main` the `image` job
   has a second step, `push core image` (`continue-on-error`, so it never fails the job or the pin):
   it tags the image the step before built (by digest) as `git.tenfourty.site/runcove/artifacts-core:<sha12>`,
   pushes it with the same login, and logs `PUBLISHED core image=... digest=...` (job output
   `core_ref`; it is tagged from the build step's exact digest, and when that step reused an existing image, or the core tag exists, it pushes nothing and logs `REUSED core`, never `PUBLISHED`). The core image is today's
   image: core still carries our in-tree plug-ins until step 9, so "plug-in-free" means free of
   anything installed from outside. Today's image, `pin` and deploy are untouched; a `ci/**`
   branch still builds and pushes nothing. **Not built here:** `core-with-house` (core's
   own job; the plug-in repo now exists and its own `test` job installs the core named in
   `core.lock` and tests the plug-ins against it, which is the drift check the job was for), and
   the lock-bump bot, which part 2 below built.
   **Core half, part 2 built (`ci/core-lock`, 9 Nov 2026): the lock-bump bot.** `core-lock-pr.sh` and a
   `core-lock` job (main only, after `image`, only when `core_ref` is non-empty) open or update the one PR
   on `runcove/ream-homelab` (branch `core-lock/next`, title `core: lock <sha12>`) that moves `core.lock`,
   then ask the forge to merge it when checks succeed (`merge_when_checks_succeed`); it stands down when
   the lock already names this commit or a newer one, and needs the secret `CORE_LOCK_TOKEN`. It only
   touches a PR whose head is `core-lock/next` on ream-homelab itself and whose head sha is the commit it
   just pushed. **Cut over (9 Nov 2026):** `core-lock` is the only path on main, core's `pin` job is
   `if: false` (homelab's lander no longer accepts this repo as a source; ream-homelab's own pin job
   is on). Runbook: `docs/agent/runbooks/image-from-ream-homelab.md`. **Proven end to end (11 Nov 2026):** the job runs only
   on `main`, and its lock PRs (`core: lock <sha12>`, branch `core-lock/next`) have merged on
   `runcove/ream-homelab` (#25 to #28); the production pod runs the image tagged with the plug-in
   repo's merge commit of #28, which locks core `9cc0b61fa9e9`, then `main`.
9. **Move, one at a time, each its own slice:** (9a) `beadpage`/`beadmetrics` and their console
   scripts, (9b) Mailgun, (9c) `ggscan`, (9d) doorbell, (9e) quarterdeck sink and inbound
   (the largest; includes the `slack` answer source and `on_answer` hook). Each: copy into the
   plug-in repo, ship, confirm the deployed behaviour, then delete from core in the next pin.
   Removing a console script from core means agents reinstall the tool
   (`uv tool install ... --force`); announced in `news` first.
   **9a, first half built (`ci/9a-beadpage` on ream-homelab, 10 Nov 2026; CI green, not on its main).**
   Plug-in `tracker-pages` (`ream_homelab.tracker_pages:register`) holds copies of `beadpage` and
   `beadmetrics` with their tests, and registers `ream tracker-page` and `ream tracker-metrics`
   through `api.cli` plus console scripts `ream-tracker-page` and `ream-tracker-metrics`. **Names:**
   new, because core owns `beads-page`/`bead-metrics` (the plug-in API refuses them) and two
   packages cannot ship `artifacts-beads-page` into one tool environment. The copy reads `REAM_BD`
   (or `ARTIFACTS_BD`) itself at use rather than core's settings. Core needed no change. ream-homelab's
   test job now installs the core named in `core.lock` before testing. **Not tested:** the plug-in
   inside the production image, the command through the real `ream` dispatcher with the package
   installed, a real `bd`.
   **9a, second half, core side built (`ci/step-9a-second-half`, 10 Nov 2026; ea45bee; not on main).**
   Planned as: announce in `news` first; delete `beadpage.py`, `beadmetrics.py`, their tests, the two
   `artifacts-*` console scripts, the `beads-page`/`bead-metrics` entries in `cli.py`'s verb table and
   help, and `bd_command`; in the same step the plug-in takes the old names (`beads-page`,
   `bead-metrics`, `artifacts-beads-page`, `artifacts-bead-metrics`) and drops the `tracker-*` ones;
   agents reinstall (`uv tool install ... --force`, with the plug-in) and the `render` lines in
   `pages.toml` keep working. Do it only after the deployed behaviour is confirmed. **Done in core:**
   news entry `2026-11-10b` (generic: names no house plug-in), in the same commit; deleted the two
   modules, their two test files, the two console scripts in `pyproject.toml`, the two entries in
   `cli.py`'s `_OTHER` and the two help lines, and the `bd_command` setting (`REAM_BD` and
   `ARTIFACTS_BD` are read by the plug-in alone now; a server that still sets them ignores them).
   `plugin_cli.reserved_verbs()` is built from `_OTHER`, so it stopped reserving `beads-page` and
   `bead-metrics` by itself, which is what lets the plug-in take them back; a test pins both that and
   what core alone does (below). **What the old names do now:** with no plug-in, `ream beads-page`
   and `ream bead-metrics` are no verb (exit 2, "no such verb" and the help), and
   `artifacts-beads-page` and `artifacts-bead-metrics` are not installed. Core carries no hint
   message for them, because a hint would have to name the house plug-in. **Not done, in
   ream-homelab (the other half of "the same step"):** the plug-in registers `beads-page` and
   `bead-metrics`, ships the console scripts `artifacts-beads-page` and `artifacts-bead-metrics`
   in place of `ream-tracker-page` and `ream-tracker-metrics`, and drops the `tracker-*` names. Until
   that lands and an agent's tool is reinstalled with the plug-in, an agent whose tool updates to
   this core has no tracker-page command at all, and a `render` line using one fails. **Not tested:**
   the plug-in under the real dispatcher with the old names, a real `bd`, and a `render` line run by
   `artifacts-republish` after the move.
   **9d, core stand-down (11 Nov 2026).** The doorbell moves into the house plug-in as an
   after-commit sink named `doorbell`. Core's built-in `nudge` sink (`sinks._builtin_sinks`,
   `deliver_nudge`) now checks at delivery whether a sink of that name is registered and, if so,
   rings nothing and logs once at INFO (no value named), so the two never both ring. Core's doorbell
   code stays until the plug-in half is proven in production; deleting it is a later step. Tests in
   `tests/test_emit.py` (rings without the plug-in sink, silent with it). **Not run here:** the full
   suite (CI).
   **9c, built in two halves (10 to 11 Nov 2026).** *The hook (on main):* the plug-in registers a
   scanner `gitguardian` (ream-homelab: `timeout=20`, an 18 s total deadline, text that cannot be
   encoded answered with a finding) and core's relay scan stood down while one was registered;
   `api.scanner(..., timeout=)` (API 1.1, capped at `scanners.MAX_SCAN_TIMEOUT_SECONDS`, 20) let the
   plug-in keep the 20 s the relay always had. *The deletion (`ci/step-9c-core`, 11 Nov 2026):*
   proven first in production (a fresh process in the pod's own image and environment loads
   plug-ins `gitguardian`, `mailgun`, `tracker-pages`; the scanner `gitguardian` is registered with
   `on_error=skip`, bound 20 s; core's relay scan reported stood down and answered without asking),
   then `ggscan.py`, `tests/test_ggscan.py`, the `ggshield_url` and `ggshield_token` settings and the
   stand-down branches are gone. Core now has one path: `store._check_secrets` and
   `comments.second_scan` run the registered scanners and nothing else, and name no relay. **Rules
   kept (security review):** `scanners.SECOND_OPINION = "gitguardian"`; while a scanner of that name is
   registered only its clean answer stamps an agent comment scanned, so a `gitguardian` that raised
   or timed out leaves the comment unscanned for the sweeper (a `block` one holds it) and another
   scanner's clean answer never stands in; with none of that name, any plug-in scanner's real clean
   answer is the second opinion, and with no scanner at all the comment is shown unscanned and a hold
   is lifted. A finding from any scanner refuses a publish or withdraws a comment; all registered
   scanners are asked (the old relay finding short-circuited the others). **Behaviour changes:**
   the `published on the built-in scan only ... reason=ggshield_unavailable` and
   `comment kept on the built-in scan only` log lines are gone (the plug-in logs its own fault, core
   logs `scanner gitguardian failed (skip)` and counts `<plug-in>:scanner`). **Settings:**
   `REAM_GGSHIELD_URL` and `REAM_GGSHIELD_TOKEN` (and the `ARTIFACTS_` forms) are not core settings
   now; core reads neither and never inspects unknown variables, so a deployment that still sets them
   starts as before and the plug-in reads them itself; the one difference is that the two forms set
   to different values no longer stop core (the plug-in raises, which is no opinion). A test pins
   this. Tests: `tests/test_gitguardian_scanner.py` (new: a finding refuses a publish with the
   scanner asked once; a raise or a timeout leaves a comment unscanned; the other cases above);
   `test_instant_comments.py`, `test_unencodable_text.py`, `test_sections.py` and
   `test_plugin_scanner_mail.py` use a fake `gitguardian` scanner. **Not done:** widening scanners to
   titles, decision fields and human comments (they still run in the second opinion's slot only);
   `docs/agent/plans/2026-09-29-*` and `specs/2026-10-29-pinned-comments.md` still name `ggscan` as it
   was when written. **Not run here:** the full suite (CI on `ci/step-9c-core`).
   **9b, the Mailgun deletion (11 Nov 2026).** The house plug-in's mail backend (ream-homelab #12,
   hardened in ad3e9a0: no redirects, https to its configured host only) already carried all
   production mail; `mail._mailgun_send`, `_mailgun_enabled`, the `mailgun_key`, `mailgun_api`,
   `mailgun_domain` and `mail_from` settings with their half-configured start-up refusal, and the
   Mailgun request test are deleted. Core now has one path: the registered backend, or none, which
   is what a fresh install had: `mail.enabled()` is false, `mail.send()` raises `MailFailed("mail is
   not configured")`, and the share page says email is not set up. `mail.has_backend()` is new.
   **Settings:** `REAM_MAILGUN_KEY`, `_API`, `_DOMAIN` and `REAM_MAIL_FROM` (and the `ARTIFACTS_`
   forms) are not core settings now; core ignores them and the plug-in reads them itself. **Start-up:**
   if any is still set (presence only, never a value) and no mail backend is registered after the
   plug-ins load, one warning says mail is OFF (`serve._warn_mail_off`); it warns and does not refuse.
   **Behaviour change:** a half-set group no longer stops a core start (the plug-in checks its own).
   Tests: `test_plugin_scanner_mail.py` (backend path, no-backend path, the warning),
   `test_email_share.py` (a registered backend stands in for the sender), `test_settings.py`.
   **9e, not a stand-down: the plug-in API grows first (11 Nov 2026).** The quarterdeck bridge
   (`partner.py`, `partner_in.py`, and what `inbox`, `gallery`, `api`, `listening`, `app`, `serve`,
   `admin` and `decisions` read from them) cannot move the way 9d's doorbell did, because the plug-in
   API could not express it: a `stage(conn, event)` that writes in the answering transaction (the
   per-question `partner_version` and the outbox row are written there), tables of the plug-in's
   own, a sender thread to start and stop, and the inbound copies the inbox, gallery and listening
   read. The spec's done-when stands: quarterdeck leaves core entirely (not a generic bridge kept
   in core). Sliced: **9e-1, built (`ci/plugin-api-1-2`): plug-in API 1.2, additive**, in core only:
   `api.sink(name, kinds, stage=fn)` (exactly one of `deliver` and `stage`, outbox delivery; run in
   a savepoint inside the action's transaction where the built-in `partner` stage sink runs; a raise
   undoes only its own writes, is logged and counted as `<plug-in>:stage`, and the answer is kept,
   the event not staged for that sink); `api.schema([...])` (a plug-in's own tables, only
   `create table|index if not exists`, names prefixed with the plug-in's name, created after the
   core's migration under its lock on both databases; `Database.ensure_plugin_tables`);
   `api.on_start(fn(db))` and `api.on_stop(fn())` (called from `serve.py` after `sinks.start` and
   from the lifespan shutdown before `sinks.stop`, each isolated). `API_VERSION` is `(1, 2)`; the
   CLI's no-op API accepts the new calls. Tests: `tests/test_plugin_api_1_2.py` (a fake plug-in
   using all three, the failure and refusal cases). Nothing moved; `partner.py` is untouched.
   **Review fixes (11 Nov 2026):** a stage that catches its own database error is still rolled back
   to its savepoint on Postgres (a `select 1` after it raises inside the savepoint), and one that
   ends the transaction itself (`rollback`, SQLite's `or rollback`) fails the whole action with
   `db.TransactionLost` instead of letting it go on in autocommit, nothing kept; `commit` is
   the one form core cannot contain (everything the action wrote before the stage is already
   committed, only the rest fails) and is forbidden; a schema statement may hold no `$`,
   backslash, backtick or `[`; `apply_schema` reads the
   catalog under the lock and refuses a name the core uses (schema.sql, or anything in the catalog no
   plug-in declares) and an index or `references` on a table the plug-in does not create itself, so a
   plug-in named `agent` or `partner` cannot reach `agent_key` or `partner_outbox`; at load, of two
   plug-ins that both declare a schema one's prefix may not cover the other's (`acme`, `acme-chat`;
   plug-ins with no schema load as before); the Postgres catalog check reads `pg_class`, so
   sequences and materialized views count as names; a table statement ends at its
   column list (no `as table`, `as values`, `inherits`); a database refusal names the plug-in; each
   stop hook gets 2 seconds, like the core's sender.
   **9e-2, built (`ci/plugin-read-side`, 11 Nov 2026): plug-in API 1.3, additive**, in core only:
   `api.read_side(open_copies=, settled_copies=, answer=, enabled=,
   outbox_status=)`, keyword-only, all optional (at least one), once per plug-in. New
   `src/ream/readside.py` is the one place the readers go through: `inbox.py`, `gallery.py`
   (open and settled copies, `answer_here`), `api.py` (the `scope=all` list) and `listening.py`
   (`bridge_on()`) now call it instead of `partner_in` / `partner` directly. Core's partner code
   is the built-in contributor and answers first; a plug-in's copies are merged behind it (a
   `source` seen already is dropped, ordered by creation, cut to `limit`), the badge counts the
   same de-duplicated open copies (no count argument), `enabled`
   is core's or any plug-in's, and `answer` goes to each plug-in in turn for a question core
   does not hold (`store.NotFound` passes it on). A contributor that raises or returns the wrong
   shape is logged by type, counted as `<plug-in>:read_side`, and ignored. The outbox report is in
   `sinks.status()["read_side_outbox"]` and printed by `artifacts-admin outbox`. `API_VERSION` is
   `(1, 3)`; the CLI's no-op API accepts the call. Tests: `tests/test_plugin_read_side.py`; two
   existing files only had their version pin loosened. Core's own partner behaviour is untouched.
   **Left for 9e-3:** `listening`'s per-question "delivered" lookup still reads core's
   `partner_outbox` (a plug-in's outbox would need a contributor argument for it);
   `artifacts-admin` does not load plug-ins, so a plug-in's outbox line appears only where they
   are loaded (or 9e-3 gives the admin shell a way to). **Review fixes (11 Nov 2026):** the note's size and
   secret checks run once in `readside.answer_here` before core's or any plug-in's call; a
   plug-in `answer` that fails other than by the store's refusals answers 503 (`AnswerFailed`).
   **Review round 2 (11 Nov 2026):** a `NotFound` from one plug-in's `answer` now tries the
   next; the note gate sits after core's NotFound and before the plug-in loop; a copy is
   shape-checked (`created_at` made aware, field types) so one cannot break a sort or render;
   `count_waiting` was dropped from the interface. Next, 9e-3, the quarterdeck plug-in's
   outbound half (stage sink, own `quarterdeck_*` tables, sender) with core's `partner` sink
   standing down while a sink named `quarterdeck` is registered; 9e-4, the inbound half (`slack`
   answer source, `on_answer`, router) and the deletion from core. **Open for 9e-3:** the existing
   `partner_outbox`, `partner_question` and the version tables are core's and hold live rows; a
   plug-in's names must carry its prefix, so the move needs a copy or a read-through of those rows.
   **Not run here (9e-2):** the full suite and Postgres (CI on `ci/plugin-read-side`); the new
   file, `test_plugin_api.py` and `test_plugin_api_1_2.py` passed serially on SQLite.
   **9e-3 design (11 Nov 2026): move on hand-over, counter stays in core.** The open question
   above is settled this way. Core keeps owning the per-question version counter
   (`decision.partner_version`) until 9e-4: the plug-in's stage takes its version from
   `partner.next_version` in the answering transaction, so the hub sees one unbroken run of
   versions across the switch and back again, and the listening state's "announced" needs no
   change. Core's undelivered outbox rows move to the plug-in's own `quarterdeck_*` outbox with
   `partner.hand_over`, called by the plug-in's sender on every pass: it returns and deletes
   them in the caller's transaction, so a row is in exactly one outbox (a rollback keeps it in
   core's); it moves whole questions only, skips a question with a row under a live lease (a
   sender elsewhere is attempting it), and returns `held`, every question core still has rows
   for, whose rows the plug-in must not send that pass, so no later version overtakes an earlier
   one. Repeating it each pass, not once, also carries the rows staged by core before the
   plug-in loaded, by a process still on the old image, and the partner-home answers core's
   inbound half still writes until 9e-4. `partner.release_bridge` is the undo (rows back into
   core's outbox and the bridge back to core, one transaction; review round below). *Why the others lost:* (a) a one-time copy at first start
   strands any row written after it (an old process, core's inbound half) and, since a copy
   leaves the original, can send a row twice if core's sender still runs anywhere; (b) a plug-in
   that reads and writes core's tables until 9e-4 moves nothing (the outbox, claim and sender
   are still core's schema, and 9e-4 would need the copy anyway). Copying the counters instead
   of borrowing them would race an old process still bumping core's; core stops bumping at the
   switch, so 9e-4 copies a frozen column. Both databases: plain selects and deletes, Postgres
   adding `for update skip locked`, SQLite serialised by its writer lock.
   *The hub's wire invariants, which the plug-in's sender must keep (read from quarterdeck's
   partner intake, 11 Nov 2026), and where core stands on each:* (1) the `X-Partner-Key-Id`
   header is present and equals the body's `key_id`, and the signature is over the exact bytes
   sent: core does this (`partner._attempt` signs the bytes `envelope` built and posts those
   same bytes); the plug-in copies it and never re-serialises a signed body. (2) An event whose
   `sent_at` is more than 5 minutes old on arrival is refused: core re-stamps and re-signs every
   attempt (`envelope(row, key_id, now)`); the plug-in copies it and never replays stored bytes.
   (3) The hub remembers an `event_id` for a day and drops a repeat; a retry keeps its id and an
   id is never reused for another event: core's ids are random (`ids.new_id()`, no counter or
   other state to carry), `hand_over` returns each row's `event_id` and the plug-in must insert
   it unchanged and mint its own new ones the same way. (4) The hub keeps the latest version per
   (source, question) and drops an older one, so the two senders must never overlap on a
   question: within one outbox core sends only the oldest undelivered version of a question and
   a parked row holds the later ones (`partner._claim`), which the plug-in's claim must copy; at
   the switch a row is in one outbox only (`hand_over` deletes what it returns, skips a question
   under a live lease, and `held` names every question core still has rows for, which the
   plug-in sends nothing of that pass); core's own sender stands down once a plug-in holds the bridge, and the versions come from one counter (`next_version`). (5) A 503 carries
   `Retry-After` and the sender honours it: **core does not** (`http_post` returns only the
   status; a 503 is retried on core's own backoff, 5 s doubling to 15 min, which may come
   sooner than the hub asked); the plug-in must read `Retry-After` and wait at least that long
   before the row's next attempt. Core's sender is left as it is: it stands down wherever the
   plug-in runs, and it goes in 9e-4.
   **9e-3, core half built (`ci/partner-standdown`, 11 Nov 2026).** Core's built-in `partner`
   stage sink and its sender (`partner.sender_pass`, the loop's one pass) stand down once a plug-in
   holds the bridge, and log once each at INFO (no value), like the 9d doorbell; pruning goes on
   (the inbound tables are still core's). New in `partner.py`: `next_version` (split out of
   `enqueue`, which calls it; it does not read core's partner settings), `hand_over` with
   `Handover(rows, held)`, `take_back`, and the switch below. Plug-in API 1.4:
   `api.read_side(delivered=fn(db, decision_ids))`, merged into `listening.for_decisions`
   beside core's own `partner_outbox` lookup (closing the "left for 9e-3" item above);
   `readside.delivered`. Core-only installs behave exactly as before.
   **Review round (11 Nov 2026).** (1) *The switch is a database fact:* the `service_meta` key
   `partner_bridge_owner`, set by `partner.claim_bridge(conn, owner)` in the same transaction as
   the plug-in's first `hand_over`, read by core's stage in the staging transaction and by its
   sender once a pass, so a process that did not load the plug-in (another `REAM_PLUGINS`) stands
   down too; the plug-in's stage stages only when `bridge_owner(conn)` is its name, so exactly one
   writer per event, before and after the claim. *A process from before 9e-3 cannot read the key*
   and would keep sending from core's outbox: the switch is deployed stop-then-start, never a
   rolling update beside the old image (no code can fix the old image). (2) The name-only
   registry check (`a sink named quarterdeck`) is gone: any sink of that name (a
   `REAM_SINKS_FILE` entry, say) silenced core with nothing taking over. *9d's doorbell check has
   the same weakness* (a sink named `doorbell` of any kind silences core's nudge); not fixed here.
   (3) The stage of the plug-in that holds the bridge failing now fails the action
   (`plugin_api.BridgeStageFailed`, naming the plug-in), as core's unguarded stage did; before,
   `guarded_stage` swallowed it and the answer was kept with no outbox row anywhere. Other
   plug-ins' stage failures stay isolated. (4) *Rollback:* `partner.release_bridge(conn, owner,
   rows)` gives the plug-in's undelivered rows back to core's outbox and clears the key in one
   transaction; the plug-in exposes it as a command the operator runs **before** removing the
   plug-in (core cannot call it once the plug-in is gone). (5) `hand_over` on Postgres checks
   the lease under the row lock (`_lock_rows`: `claimed_until is null or <= now` in the locking
   select), and gives back untouched any question whose undelivered rows were not all locked, so
   "whole questions only" holds against a concurrent sender. Tests:
   `tests/test_partner_standdown.py` (new; the claim, a sink merely named `quarterdeck`, a process
   without the plug-in, the holder's failing stage, release, a question past `limit` held, a
   question not wholly locked given back); the version pins of `test_plugin_api.py` and
   `test_plugin_read_side.py` loosened to `>= (1, 3)`. **What the plug-in half (ream-homelab)
   must do:** a stage sink on `events.BRIDGE_KINDS` that stages only while `bridge_owner(conn)` is
   its name, with `next_version`, into its `quarterdeck_outbox` (core's columns); a sender (on
   start/stop) that on its first pass calls `claim_bridge` and `hand_over` in one transaction,
   then each pass `hand_over`s, claims as core's `_claim` does skipping `held` **and with
   `partner.core_outbox_clear("o.decision_id")` in the claim statement itself** (second round,
   D), passes its own entry-point name as `owner`, and keeps the wire invariants above
   (Retry-After included); `read_side(enabled=, outbox_status=,
   delivered=)`; a command that runs `release_bridge` before a rollback. `artifacts-admin
   partner-outbox` still reports and releases only core's outbox. **Not run here:** the full
   suite and Postgres (CI on `ci/partner-standdown`); the new file and `test_partner_outbox.py`,
   `test_plugin_read_side.py`, `test_plugin_api.py`, `test_plugin_api_1_2.py`, `test_emit.py`
   passed serially on SQLite.
   **Second review round (11 Nov 2026).** (A) *Round one's fix lost events:* core's stage stood
   down wherever the key was set, so an event raised in a process where the owner's stage is not
   loaded was written nowhere: `artifacts-admin purge-deleted` (`store.purge_expired` emits
   `withdrawn` in a CLI process, whose plug-in API registers no sink) and any server with another
   `REAM_PLUGINS`. Now core's stage stands down only when the key is set **and** the owner's stage
   for that kind is registered in this process (`sinks.bridge_stage_of`, by the plug-in name
   `api.sink` now records on each `Sink`, never by a sink's name); elsewhere it writes core's
   outbox, which core's sender leaves alone while the key is set and the owner's `hand_over`
   empties. Still one writer per event: the plug-in's stage writes only while it owns the bridge
   and is loaded. The key stays the sender's switch. (B) *An owner removed without
   `release_bridge`* no longer loses anything (A queues in core's outbox), and is visible: a
   WARNING once per process (at server start, `partner.warn_if_owner_absent`, and when a stage or
   sender pass meets it), `partner.status` gains `bridge_owner` and `bridge_owner_loaded`,
   `artifacts-admin partner-outbox` names the holder, and `artifacts-admin partner-bridge
   [--release]` shows it and clears the key. `--release` is refused while the owner is installed
   in the shell's environment and selected by its `REAM_PLUGINS` (`plugin_cli.selected_names`,
   the set a server started there would load; the admin command loads no plug-in, so "loaded in
   this process" would always be false there): the plug-in's own release also moves its rows.
   (C) *A pass that began before the claim could send a later version:* the owner is now read in
   `_claim`'s own statement, per row (`not exists` on `service_meta`), so a claim landing between
   two rows stops the pass. (D) *`held` is a snapshot:* on Postgres an uncommitted staging
   transaction's row can appear in core's outbox after the hand-over, so the plug-in could send
   v(n+1) before core's v(n) reaches it. The contract now makes the plug-in's claim statement
   re-check core's outbox (`partner.core_outbox_clear(column)`, a checked column name in a fixed
   fragment, tested). *A lock serialising `stage_partner` with `claim_bridge`: no.* With (A),
   core's stage legitimately writes core's outbox at any time after the claim, so a lock at the
   claim would not remove the need for the per-claim re-check, and it would add a cross-process
   lock (Postgres advisory, nothing on SQLite) to every bridge event's transaction. (E) *Two
   claims at once:* `claim_bridge` inserts with `on conflict do nothing` and reads the owner
   again, so the second gets `BridgeHeld`, never the primary key's raw error. Also: the owner
   must be the plug-in's own entry-point name (documented on `claim_bridge`; not checked, since a
   plug-in's sender may claim from a process whose registry core cannot vouch for, and a wrong
   name only leaves core staging). Tests: `tests/test_partner_standdown.py` (round one's
   "stands down without the plug-in" test, which asserted the loss, replaced; a CLI process, another
   plug-in's stage, a kind the owner does not stage, a claim mid-pass, the fragment, two claims,
   the warning, status, the admin command); the same files passed serially on SQLite.
10. **Docs and decisions.** `docs/decisions.md` entry for the core-and-plug-ins split, the design
    doc's extension section, a plug-in author guide, the office doc's deployment paragraph.
    (Docs also land with each slice above; this is the consolidation.)
    **Part 1 built (`ci/plugin-docs`, 8 Nov 2026).** Covered: the plug-in author guide
    (`docs/plugins.md`, every interface of steps 1 to 7 with an example and the failure rules) and
    the `docs/decisions.md` entry for the split. Not yet: the design doc's extension section
    (section 4 above still reads as written before building; where it differs, the ledger above
    and the guide are right: the mail backend's `send(to, subject, text)`, no `validate` on a
    sink, failure counts in `failure_counts()`
    and `sinks.status()` rather than `/metrics`), the office doc's deployment paragraph, and the
    parts that describe steps 8 and 9, which are not built.
    **Part 2 built (`ci/plugin-guards`, 8 Nov 2026).** Two gaps closed: `api.sink` refuses a kind
    that is in neither `events.KINDS` nor `events.BRIDGE_KINDS` (plug-ins cannot declare kinds)
    with a `PluginError` naming the plug-in, sink and kind, so start-up stops; and
    `decisions.AnswerHookFailed` is answered **503** (the spec said "a 5xx"; 503 because the
    fault is ours and retrying later may succeed, as with a failed scanner) as JSON naming the
    plug-in, by an app-level handler on the gallery and API apps, and as a readable "Answer not
    saved" page (503) on the gallery answer form.
    **Part 3 built (`ci/webhook-sink`, 8 Nov 2026).** The generic signed webhook sink
    (`src/ream/webhook.py`) and `REAM_SINKS_FILE` (`ARTIFACTS_SINKS_FILE` read too): outbox sinks from
    a TOML file, Ed25519-signed envelope, https and public addresses only unless a sink opts out
    (`allow_http`, `allow_private`), no redirects, 64 KiB cap. Section 4 above now reads as built for
    the mail `send(to, subject, text)` signature, no `validate` on a sink, no `/metrics`, and the
    60 s text cache.

The chasing work (nudge once, `answer_dropped` after a day) follows slice 4 at the earliest, and
only needs sinks to exist; it does not wait for the plug-in repo.

## 9. Risks

- **Owner-id mismatch** reads every old answer as a stranger's: the `service_meta` guard and
  manifest-first deploy order (3.2).
- **Text drift.** Moving thousands of words to templates can change one: the golden tests.
- **The lander.** Option A depends on a homelab change to bind digests to another repo's CI run;
  without it the pin PRs cannot land. Raise with the homelab maintainer in slice 8's planning,
  not at cut-over.
- **API drift between repos.** `core-with-house` informational is only a warning: a core change
  can merge and break us until the lock-bump PR fails. The required lock-bump check is the
  backstop; core never reaches production without it.
- **SQLite CHECK removal** edits `sqlite_master` text; the guard proves its work (same pattern as
  step 6), and a table-rebuild fallback exists.
- **Outbox migration** while rows are pending: slice 4 reads the old table until empty.
- **Fresh-install content origin** (3.3): checked in slice 2, works on `*.localhost`; Safari untested.
- **One plug-in repo for all house code** couples our own release cadence: acceptable at one
  deployment; split later if it hurts.

## 10. Decisions for Jeremy

10.1 **Where do our plug-ins live, and what is it called?** (a) A new repo under the same forge
organisation as artifacts, named `artifacts-homelab` (homelab office creates it; the artifacts
office gets write access). (b) The same, named `artifacts-plugins`. (c) Inside the homelab
repo, in a directory, with no new repo.

10.2 **Which repo builds the image that runs in production?** (a) The plug-in repo: core publishes
a plug-in-free image, the plug-in repo adds our plug-ins and opens the pin PR to homelab; one extra
hop for a core change, and the homelab maintainer's pin process learns to follow the new repo.
(b) The artifacts repo as today, cloning the plug-in repo at a pinned version; production path
unchanged, but every plug-in change needs a commit in the artifacts repo. (c) A third repo whose
only job is to assemble the image.

10.3 **How does a core change reach production?** (a) Core change merges, a bot opens a bump PR on
the plug-in repo, and it merges by itself when the combined tests pass; then the homelab pin PR as
today. (b) The same but a person (the artifacts office) merges the bump PR. (c) No automation:
bump by hand when wanted.

10.4 **How do other people's installs get their first settings?** (a) Neutral defaults for
everything (owner "the owner", pronoun "they", no mail, no scanners), a production start that
refuses to run with no gallery address set, and a warning line at start-up. (b) A required setup
command that writes the settings before the first start. (c) Defaults as in (a) but the service
runs on localhost with no refusal.

10.5 **How do you want the renamed words handled for the clients that already use them?** (a) Old
and new both accepted and shown for one release (the field `for`, the inbox kind `slack_question`,
`--office`), announced in the news line, then the old ones removed. (b) Old ones kept forever.
(c) Switch at once, no aliases, and agents are told in the news line.

10.6 **Settled, not asked again:** new features wait for this cleanup (Jeremy, 4 Nov 2026,
decision d_01M6K1QHEVKDT977KK25W648FC: in-flight work lands first, then this cleanup goes ahead
of every new feature). Receipts' remaining slices and the chasing work are built against the new
emit point once slice 4 lands.
