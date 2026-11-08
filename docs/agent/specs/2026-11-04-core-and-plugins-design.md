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
"as on baba"; the CLI help uses his pronoun about 60 times; `'slack'` is a value in a schema
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
| `mail_from` | `ARTIFACTS_MAIL_FROM` | mail off |
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
("Published from BABA through the homelab artifacts service" becomes `{{footer}}`, default empty),
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
`ARTIFACTS_SINKS_FILE` (a TOML file of `[[sink]] name, kinds, url, signing_key_file, key_id`), so
"`answer_dropped` goes to this URL" is configuration, not code. Chase bookkeeping (a
once-only marker per subject and stage) is the chasing design's table, not this one's.

### 4.2 The six interfaces

1. **Event sink.** `api.sink(name, kinds, deliver, delivery="outbox"|"after_commit",
   validate=None)`. `deliver(event)` returns normally on success, raises to retry (outbox) or is
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
   a sink.
4. **Scanner.** `api.scanner(name, scan, on_error="skip"|"block")`: `scan(text) -> list[Finding]`,
   called alongside the core's pattern scan on publish and on comment save
   (`store.py:449`, `comments.py:1639`). `on_error` preserves today's `ggscan` behaviour exactly
   (to be read from `ggscan.py` in the slice, not assumed here). Core ships the pattern scan
   only.
5. **Mail backend.** `api.mail_backend(name, send, enabled)`; core has `mail.send(message)` and
   `mail.enabled()` that ask the one registered backend (or none: off). Mailgun moves out; the
   sender and region/domain are settings the backend reads.
6. **CLI command and client hooks.** `api.cli(name, main, help)` registers `artifacts <name>`
   subcommands (and keeps the standalone console script working through a one-line entry in the
   plug-in's own `pyproject`). `api.client_defaults(api_url=..., session_envs=[...],
   news=[...])` supplies the CLI's default endpoint, the session-label env names (`QD_OFFICE`,
   `ARTIFACTS_SEAT`) and news lines. The tracker pages (`artifacts-beads-page`,
   `artifacts-bead-metrics`) are CLI commands.

Plus **text**: `api.text_vars({...})` (extra template variables) and `api.text_section(slot,
fn)` for named slots in `agents.txt`/`skill.md` (`proxy`, `tools`, `integrations`), so the
tracker-page paragraph of `skill.md` ships with the plug-in. And **routes**:
`api.router(router, host)` mounts an inbound route on a dedicated host (the partner's inbound
webhook, today `ARTIFACTS_PARTNER_INBOUND_HOST`); core never mounts it on the gallery host.

### 4.3 What the core ships with no plug-in

Journal, outbox and sender thread, the generic **signed webhook sink** (an Ed25519-signed JSON
POST of the generic envelope to configured URLs; stays core as ruled, the part of `partner.py`
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
exception becomes a log line with the plug-in and interface and a counter on `/metrics` (or the
admin health command if there is no metrics route yet), and the per-sink circuit breaker pauses a
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
| `ggscan.py` | scanner `gitguardian` |
| `mail.py` Mailgun | mail backend `mailgun` |
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
drivers; CI only (not on baba).

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
   are unchanged: with our settings every word is byte-identical. The "BABA" stamp was already the
   `host_label` setting (only a comment in `thumbs.py` named it).
   **Manifest values to set before the pin** (all optional for a house that wants the neutral
   wording; ours sets them to keep today's text): `ARTIFACTS_FOOTER`, `ARTIFACTS_HOST_LABEL=BABA`,
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
   the per-sink circuit breaker (4.4), the generic webhook sink, and `change-owner` over `outbox.event_json`.
   **Part 2 built (`ci/slice4-emit-part2`).** All remaining fan-out sites emit through `events.emit`
   (decisions, store, comments); `partner_in.answer_here` stays on `partner.enqueue_foreign` (no
   artifact or agent to put on an `Event`). `deliver` has a protocol bound: it must return within
   `sinks.DELIVER_TIMEOUT_SECONDS` (30 s), enforced by running it in a daemon thread; a timeout is a
   failure with backoff. `artifacts-admin outbox` (counts by sink) is in; the circuit breaker (4.4)
   and the generic webhook sink are still to do.
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
   `sinks.py`, per sink, per process: 5 failures running (a raise or a timeout) open it for 60 s;
   outbox rows of an open sink wait uncounted, after-commit deliveries are skipped; after the
   cooldown one trial goes through (success closes, failure re-opens). `sinks.status()` shows
   `open_breakers`. Left for 7b: scanner, mail, cli/client, text and router hooks, CLI loading of
   plug-ins, cross-process failure counts and breaker state, the core-bare CI job.
8. **The plug-in repo and its pipeline** (homelab office creates the repo; lander change). It
   builds an image identical to today's from an empty plug-in set, opens a pin PR, and one real
   pin PR is merged as proof before anything moves. Core starts publishing the `-core` image and
   the lock-bump PRs; `core-with-house` job added informational.
9. **Move, one at a time, each its own slice:** (9a) `beadpage`/`beadmetrics` and their console
   scripts, (9b) Mailgun, (9c) `ggscan`, (9d) doorbell, (9e) quarterdeck sink and inbound
   (the largest; includes the `slack` answer source and `on_answer` hook). Each: copy into the
   plug-in repo, ship, confirm the deployed behaviour, then delete from core in the next pin.
   Removing a console script from core means agents reinstall the tool
   (`uv tool install ... --force`); announced in `news` first.
10. **Docs and decisions.** `docs/decisions.md` entry for the core-and-plug-ins split, the design
    doc's extension section, a plug-in author guide, the office doc's deployment paragraph.
    (Docs also land with each slice above; this is the consolidation.)

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
