# The listener: how an agent hears that Jeremy answered

**Status:** revised after round two (19 Sep 2026). Every finding from both
rounds is triaged at the end, with the reason. Jeremy chose this shape — "(a)
and can't we have this on each machine that uses the service?" — so the
listener ships with the client, not as a setting on one box.

**The listener itself is not built, and six things must have written results
first.** They are listed under "Prerequisites" below. Four are experiments and
decisions rather than code; the acknowledgement is built (it needed a column, a
call and a place on the page before anything could be said about delivery), and
the arm-time copy is a rule the client must follow rather than a feature.

## The problem, stated exactly

The feed is not the problem. `GET /api/v1/events?since=<seq>&wait=<n>` with a
per-seat monotonic sequence works: measured against the live service at 5, 30,
70, 110 and 180 seconds, each answering within 0.11 s of the value asked for,
and three consecutive 280-second holds at 280.073, 280.101 and 280.071 — the
last under a load average of 16.65.

The problem is that **arming it is an agent's manual act**, and an agent that
forgets is silent in precisely the way a broken listener is silent. Three times
on 19 September Jeremy answered a question and the agent that asked found out
only because he said so in a terminal.

So: the harness listens, not the agent.

## The measurement that changed the design

Two seats logged `timed out after 300001 milliseconds with 0 bytes received`
within a minute of a deploy, and one probe answered 200 after **336 seconds**
for a 280-second hold.

That 336 is now explained, by the clock rather than by argument:

| | |
|---|---|
| deploy commit | 17:40:10Z |
| hold started | ~17:40:23Z |
| new pod started | **17:40:44Z** |
| hold answered 200 | 17:45:59Z — 336.098 s |

336 − 280 = 56 s, which is when the old pod's 30-second grace period ran out.
The held request was killed mid-flight and **the ingress retried it upstream to
the new pod, which served a fresh full 280-second hold**. Three repeats with no
deploy in the window came back at 280.07–280.10, so neither the wait loop nor
load is involved: the loop is bounded by an absolute `time.monotonic()`
deadline recomputed every iteration, and the container has no CPU limit to be
throttled against.

Fixed server-side (`events.begin_shutdown`, wired to SIGTERM at lifespan
startup): a held request now ends the instant the process is asked to stop,
with an ordinary empty 200 — which already means "nothing yet, ask again". The
ingress then sees a normal answer and has nothing to retry.

Two consequences for this design, both load-bearing:

- **A transport timeout and a clean empty 200 are different outcomes.** Only a
  complete HTTP response is a completed round. A transport error is a failed
  round: it counts toward the breaker, never advances the cursor, stays silent
  in `hook` and is loud in `watch`. With a departing server now answering 200,
  strictness costs nothing — and a transport timeout is the signature of both
  the failure that lost Jeremy's answers and of a machine that cannot reach the
  service's network.
- **Every deploy that evening silently broke every waiting agent**, and nothing
  anywhere said so. Measured once more at 20:05 CEST, on the live waiter: three
  of its ten rounds died at `300001 ms`, and its closing line still read
  "nothing from Jeremy in 2862s" — fifteen minutes of that window were never
  waited. That is the case for the visibility section below, made by the system
  rather than by argument.

A separate latent hazard found in review and accepted: `db.py` opens a new
connection per look, every 0.5 s, with no `connect_timeout`, and the deadline
is re-checked only *after* the look returns. One slow connect can park the loop
past its deadline. That did not cause tonight's 336 — but it could cause the
next one, so the look is bounded (`connect_timeout` in the DSN) and the
deadline is checked before each look as well as after. Until that lands, margin
is chosen deliberately: hold at most 240 s against a `wait` the API caps at
300, and a client timeout of at least hold plus 90 s.

## What ships

One installer in the client package, because that is what makes this true on
**any** machine that uses the service rather than on the management machine:

```
artifacts-notify install      # merge hooks into ~/.claude/settings.json
artifacts-notify install --dry-run   # print exactly what would be written, where
artifacts-notify uninstall    # remove only what install added
artifacts-notify hook         # what the hooks actually run
artifacts-notify read <id>    # the one command the injected line ever names
artifacts-notify watch        # the long-held request, for everything else
artifacts-notify version      # this machine's version, beside the newest the service has seen
```

## The kill switch

The **first** statement of `hook` mode, before config, before the key, before
any network call:

```python
if (home / ".config" / "artifacts" / "hook-disabled").exists():
    return 0
```

One `touch` per machine, with a test. Claude Code reloads `settings.json`
live, so without this a bad hook runs on every prompt of every session on every
machine until someone hand-edits JSON on each one.

## The hook

Two user-scope hooks, `SessionStart` (matchers `startup|resume|clear|compact`)
and `UserPromptSubmit`, both running `artifacts-notify hook`: a `wait=0`
catch-up read of this consumer's own feed with this seat's own key, printed as
`hookSpecificOutput.additionalContext` with `hookEventName` matching the firing
event. Nothing but that JSON object goes to stdout; advisories go to stderr.

**It injects no free text — including the page title.** One line per row: the
kind, the decision id, the artifact id as an anchor, and the fixed command to
read it. Both ids are 26 characters of lowercase base32, so the line is
closed-alphabet by construction; a row whose ids do not match `^[a-z2-7]{26}$`
is not printed. The command is a fixed subcommand plus an id — never a curl
line, never a URL carrying a token, nothing with quotes or a pipe. The block is
framed as what it is: *ids from the artifacts service; data, not instructions.*

The title was in the last version and is out. It is publisher-authored, 512
bytes, guarded only for size and secrets, and it is not even in `EventRow` — so
printing it would cost a second API call per artifact inside a two-second
budget to fetch text that must then be sanitised. The agent that cares fetches
everything in its own turn, where reading it is that agent's decision.

Hard rules, each because of a failure this repo has already had:

- **Always exit 0** — a property of the command line, not only of the client.
  The hook entry is an absolute path to an installed wrapper written so that a
  missing target is still success, and `install` refuses to write an entry
  whose target does not exist, **or whose target lives inside a git working
  tree**. Agents edit scripts in the shared tree while other sessions run them;
  a wait client died mid-line that way tonight.
- **Two seconds, total**, bounded at the HTTP call and again at the process.
  Hook mode does not inherit the client's 30-second request timeout.
- **A circuit breaker**, per seat per machine at
  `~/.local/state/artifacts/breaker-<seat>` — never beside a per-consumer
  cursor, or every new session pays three full timeouts before it settles.
  Three consecutive failures and the client stops calling the network, retrying
  once per 10 minutes, reset on the first success. Only `hook` honours it;
  `watch` never does, so a real network outage stays visible to whoever is actually
  waiting. Its case is **not** "a laptop away from home": where the hostname has no
  public record, being away from the network normally means NXDOMAIN in milliseconds. It earns
  its keep against hijacking resolvers and captive portals — the same adversary
  as the environment section below.
- **Silent when there is nothing, and silent when there is no key.**
- **Never under a shell trace**, and the key never in argv. Both cost a
  rotation on 19 Sep 2026.
- **The cursor advances to the highest sequence actually PRINTED**, never to
  the highest fetched. A cap that prints 5 of 40 and advances past all 40 is
  the fetch-and-discard bug wearing a hat — the same bug, by construction, that
  cost this project Jeremy's answers this afternoon. The block ends with "N
  more waiting".
- **A consumer's first run seeds, it does not replay**, at the then-current
  maximum, and says so without ids: "seeded at N; earlier events exist, list
  them with `<command>`". `install` cannot do this seeding — it cannot know a
  future consumer — so seeding belongs to the first run and nowhere else.
  Existing `cursor-*` files are abandoned, not imported.

## The environment is not an input

In hook mode the client ignores the environment — and that takes more than not
reading `$ARTIFACTS_API`, because urllib reads it for you:

- `build_opener` installs urllib's default `ProxyHandler`, which honours
  `http_proxy`/`https_proxy`/`no_proxy`. Hook mode passes `ProxyHandler({})`
  explicitly.
- `ssl.create_default_context()` honours `SSL_CERT_FILE`/`SSL_CERT_DIR`. With
  an attacker CA and a hijacking resolver, `Authorization: Bearer art_…` leaves
  the machine — the exact chain this section exists to close. Hook mode builds
  its context with a pinned `cafile`, or deletes `SSL_CERT_FILE`,
  `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE` and `*_proxy` at entry.
- `~` is `$HOME`. Home comes from `pwd.getpwuid(os.getuid()).pw_dir`.

Plus, from round one: base URL from `~/.config/artifacts/config`; key paths
resolve only inside `~/.config/artifacts/`, mode-checked; seat names match
`^[a-z0-9][a-z0-9-]{0,31}$`; `.artifacts-seat` honoured only from
`$CLAUDE_PROJECT_DIR`, never from "any parent".

## Who hears it: the ask, not the session

Round one moved the cursor from per-seat to per-session. Round two measured
what a session is on this machine and the answer breaks it: **a teammate shares
its parent's session identity** — one id, 19 days, 86 compactions — so
`cursor-<seat>-<session id>` is one cursor for every teammate, which is the
original bug intact. And `UserPromptSubmit` fires on a *user* prompt, which a
teammate's turn is not; if it does not fire for teammates, rows land in the
operator's context and the asking agent hears nothing — the operator relay
Jeremy ruled against, arrived at by accident.

So delivery stops being keyed on session identity at all:

- **Keyed on the ask.** Whoever poses a decision gets back a read handle for
  it. Delivery is "this decision has an answer", to every listener of that
  seat, idempotently — at least once, to all.
- **Consumed by an explicit ack**, by whoever acts on it. Exactly once, by ack.
- A cursor remains, per consumer, as an efficiency: it stops a listener
  re-reading the whole feed. It is no longer what decides who hears. Cursors
  accumulate, so they are reaped — and reaping is re-seeding, which is a silent
  skip, so the reaper is bounded by age and says what it dropped.
- **Which header to save.** `X-Artifacts-Cursor` is "from now", read after the
  hold, so saving it after a quiet hold can skip an event that landed between
  the empty look and the header. `X-Artifacts-Seen` is the one to save as the
  next `since` after any hold: every event matching the request's filters at or
  below it was returned or is at or below `since`. It is the newest event number
  read before the first look on a quiet or complete page, and the last row's
  number on a full (truncated) page.

**Which identity a hook can see for a teammate is unknown, and is one of the
five prerequisites.** If nothing per-consumer is visible to a hook, the hook
cannot be the delivery path for teammates and a teammate must pull in its own
turn — a different shape, which gets its own review.

## The ack, and what the alert actually watches

"Acted on" cannot mean "republished or closed". Most answers are acted on by
doing infrastructure work — a commit, a playbook, an SSH session — so an alert
keyed on republishing fires on correctly-handled decisions, gets muted, and
then misses the real one. That is the same failure one layer up.

- **`acknowledge` is its own call**: "seen by `<seat>` at `<time>`", shown in
  the gallery beside the decision. Closing implies it; acknowledging does not
  imply closing. The two states are genuinely different — *heard* is immediate,
  *done* can be hours — and the alert wants the first.
- **The alert keys on answered-and-not-acknowledged**, and reports which of two
  things is true: *nobody read this seat's feed since the answer* (no live
  reader) or *read, and still unacknowledged after N minutes* (live but stuck).
  That distinction is what tells a dead owner from a slow one, and it costs
  nothing extra because "last checked" already requires the read time.
- **When the owning agent is gone, the operator clears it** by acknowledging on
  its behalf — which needs a key it does not have today, so that is named as
  work, not assumed.
- Routed to `ops-timers-check` as well as the gallery. "Where Jeremy already
  looks" is what failed on 19 September.

## Failure has to be visible

A listener that silently stops is worse than no listener, because the silence
reads as "Jeremy has not answered".

- **"Last checked" is labelled honestly**: "this seat's key checked 3 min ago",
  not "healthy". Any listener of the seat polling turns it green, including one
  with no interest in the page.
- **The client version travels with every check** (`x-artifacts-client`),
  recorded with the seat's last read and shown beside it. It is caller-supplied
  and lands in the one page Jeremy reads, so it is validated
  (`^[0-9A-Za-z.+-]{1,32}$`) and rejected otherwise. A missing header renders
  as "client too old to say" — never as "never checked".
- The "last read" write is debounced server-side, or it is one database write
  per prompt per session.
- An exposed `MAX_WAITERS` count says **per pod**: it is a per-process
  semaphore, so the real ceiling is 16 × replicas.

## Distribution

What this ships is N copies on N machines whose only healthy output is silence,
so the channel is named rather than assumed: CI publishes a wheel to the forge
and a machine installs or updates with `uv tool install --from`. The installed
copy on the management machine is already stale — it has no `text.py` — which is the whole
argument in one fact.

## The key this installs

A credential that can publish, update and rename that agent's pages and pose
decisions to Jeremy in its name. A listener needs none of that. The fix is a
`scope` column on `agent_key`, checked in `caller()`, and read-only keys for
listeners. Until it exists the exposure is stated here rather than discovered
later, and the gallery surfaces last-used-at and last-seen client version per
key.

## Everything that is not Claude Code

`artifacts-notify watch` is the same held request as a plain command, and **the
plain-curl path stays documented** — requiring a Python package would undercut
the promise that any agent with a key is first class.

## The idle session

Still open, and it changes the shape. If `asyncRewake: true` wakes an idle
session, a `SessionStart` async hook holds the long poll and the harness does
everything. If it only injects on the next turn, an idle session hears nothing
and a per-seat watcher runs as a user service.

The watcher branch is deliberately **not** specified further until the
experiment picks it, because its missing half is the thing rejected elsewhere:
to wake an idle session it must post to that session's messaging socket, which
needs a seat-to-session map — the routing table that can be silently wrong. If
it ships: its own cursor namespace, `Restart=always`, failure visible in the
gallery, a map built from a breadcrumb each session writes for itself, a re-arm
rule and a cap on consecutive wakes.

## What this deliberately does not do

- **No operator-held all-seats key.** The answer to shared seats is more seats.
- **No push to the agent.** Nothing here needs an inbound address, which is
  what keeps it working for an agent that is not on this machine.
- **No new transport.** It is the feed that already exists, called by someone
  who cannot forget.

## Prerequisites — six

1. **Per-agent seats**, and the shared seat retired. With a shared seat this
   whole design delivers an answer to "some session on this machine".
2. **The sanitiser and the shutdown fix deployed and verified against the live
   `/events`**, and the long hold re-measured. Both were committed and not
   running at review time; `a92adb3` is the deploy that carries them.
3. **The idle-session experiment.**
4. **The consumer-identity experiment**: which hook events fire for a teammate
   and for a spawned agent, and what `session_id` each hook sees. A no-op hook
   appending `hook_event_name`, `session_id` and `cwd` from stdin to a file,
   then a teammate and a spawned agent started against it. The delivery path
   depends on the answer.
5. **The ack decided and built**: the `acknowledge` call, its display in the
   gallery, and the alert's two distinguishable states.
6. **The listener runs a COPY taken when it was armed, never the file in the
   shared working tree.** Measured 19 Sep 2026: a waiter was started at 20:21
   and the script it was reading was rewritten in the tree at 20:24, while that
   process was still running. bash reads a script incrementally, so the process
   became a mixture of both versions — its messages came from the new renderer
   and its control flow from the old loop, and the failure it then reported
   described a program that exists in no commit. Every session here shares one
   tree, so this is not an edge case; it is the ordinary condition.

   Note what this does NOT mean. Incident #2 in the service's own log is a copy
   that went stale and silently became a different program from the one being
   reasoned about, and its lesson was "make it a shim, never a copy". Both are
   true, and the resolution is that the copy is taken AT ARM TIME and belongs to
   that run: fresh by construction, immutable for the life of the process, and
   named so that its provenance — which commit, armed when — is readable. A
   long-lived process wants a frozen copy; a path that outlives many runs wants
   a shim. The mistake is a copy that is neither.

## Order of work, once those have results

1. The `/events` envelope carrying a cursor, keeping the bare array one release
   so the existing renderer survives the deploy.
2. `hook` and `watch`, with the cap/cursor interaction, the two-consumer race
   and the transport-error-is-not-silence rule under test — the failures the
   originally proposed tests could not have seen.
3. The installer, reviewed before it writes to anyone's settings: parse or
   refuse (never repair), append one tagged entry, temp file in the same
   directory, atomic rename; refuse a symlinked or group-readable
   `settings.json`; a second `install` is a byte-level no-op; `uninstall`
   removes only tagged entries. The merge target already holds another tool's
   `SessionStart` hook, and Claude Code reloads that file on change, so a
   half-written file is read by every running session.
4. **A staged rollout**: one seat, one machine, one week — ending early if a
   hook ever costs a session more than two seconds, or if anything but the
   injected block reaches a context.
5. "Last checked", the client version beside it, and the alert.
6. Scoped read-only keys.

Windows is refused rather than half-worked: the settings path and the mode-600
key check do not hold there.

---

# Round two — triage

The review is at
the house's brief of 19 Sep 2026 (not in this repo).
Its verdict — build the prerequisites only, and the list is wrong — is
accepted. Two findings are overtaken by measurements taken after it was
written; everything else is accepted and folded into the body above.

## Blockers

| # | Verdict |
|---|---------|
| B2-1 | **Accepted, and it is the finding of the night.** A teammate shares its parent's session identity, so my per-session cursor is a per-machine cursor and round one's blocker was never fixed. Delivery no longer keys on session identity at all: at-least-once to every listener of the seat, exactly-once by ack, with the consumer-identity experiment as a prerequisite. I reasoned about sessions from the outside; the review went and measured one. |
| B2-2 | **Accepted, with one deliberate difference.** The review makes `close` the ack; I am adding a separate `acknowledge` instead, because *heard* and *done* are different facts — most answers are acted on over hours, and an alert that waits for completion is an alert that fires on correct behaviour. Closing implies acknowledgement, so nothing is added to the path of an agent that just finishes its work. The two alert states and the "operator clears it for a dead owner" gap are taken exactly as written. |

## High

| # | Verdict |
|---|---------|
| H2-1 | **Accepted in full.** "Ignores the environment" did not survive urllib: the default `ProxyHandler` reads `*_proxy`, `create_default_context` honours `SSL_CERT_FILE`, and `~` is `$HOME`. Explicit empty proxy handler, pinned CA, home from the passwd entry. My round-one fix closed the door and left the window. |
| H2-2 | **Fix accepted, attribution corrected.** The unbounded look is real and the bound is in: `connect_timeout`, and the deadline checked before each look as well as after. But the 336 s is no longer unexplained — the pod started at 17:40:44Z, 56 s into a hold that began at 17:40:23Z, and the ingress retried the killed request into a fresh full 280 s. Three repeats with no deploy: 280.073, 280.101, 280.071, the last under load 16.65. The margin advice is taken anyway, because a bound that is only sometimes needed is still cheaper than the failure. |
| H2-3 | **Accepted.** A transport error is a failed round — breaker, no cursor advance, silent in `hook`, loud in `watch`. The shutdown fix is what makes strictness free. |
| H2-4 | **Accepted.** The title is out entirely: publisher-authored, unsanitised, and not even in `EventRow`, so it would have cost an extra call per row to fetch text I then had to clean. |
| H2-5 | **Accepted.** Ids validated against `^[a-z2-7]{26}$` before printing, a fixed subcommand plus an id, and the block framed as data rather than instructions. |
| H2-6 | **Accepted.** No kill switch existed anywhere in the design. It is now the first statement of hook mode, ahead of config, key and network. |
| H2-7 | **Accepted.** The channel is named — CI publishes a wheel to the forge, `uv tool install --from` — and a missing version header renders as "client too old to say" rather than as silence. |
| H2-8 | **Accepted.** `install` refuses a target inside a git working tree. Tonight's wait client died mid-line for exactly this reason. |

## Medium and low

| # | Verdict |
|---|---------|
| M2-1 | **Accepted.** `install` cannot seed a cursor for a consumer that does not exist yet. Seeding belongs to the first run, and the consequence is stated: an answer given while no listener of that seat exists is heard by nobody, and the alert is the only net. |
| M2-2 | **Accepted** — the seed line says what it skipped, without ids. |
| M2-3 | **Accepted** — the version header is validated and rejected, not merely capped. |
| M2-4 | **Accepted** — breaker state per seat per machine, and `watch` never honours it. |
| M2-5 | **Accepted, and it sharpens the rationale**: where the hostname has no public record, being away from the network is NXDOMAIN in milliseconds. The breaker is against hijacking resolvers and captive portals; written that way so nobody deletes it as useless. |
| M2-6 | **Accepted** — the staged rollout is a numbered step with the condition that ends it early. |
| M2-7 | **Accepted, and already done**: `artifacts-wait.sh` reads `ARTIFACTS_API` first as of `1713297c` tonight, which is why the premise reads stale. Restated as the Python client accepting the same fallback. |
| M2-8 | **Accepted** — dated, and superseded: `a92adb3` carries both the sanitiser and the shutdown fix. |
| L2-1 | **Accepted** — the waiter count says per pod. |
| L2-2 | **Accepted** — the API caps `wait` at 300, so the margin comes out of the hold; both numbers stated. |
| L2-3 | **Accepted** — only the JSON object on stdout, advisories to stderr. |
| L2-4 | **Accepted** — the 336 s is now in the body, with its explanation. |
| L2-5 | **Accepted** — cursors are reaped, and the reaper says what it dropped, because reaping is re-seeding. |

---

# Round one — triage

All 22 findings accepted, none dismissed. Retained in brief:

- **B1** cursor per consumer, not per machine — superseded by B2-1 above.
- **B2** no free text in any context: ids plus the read command.
- **B3** hook mode ignores the environment — extended by H2-1.
- **B4** the client version travels with every check.
- **H1** the cursor advances to what was printed, never to what was fetched.
- **H2** a first run seeds rather than replaying — relocated by M2-1.
- **H3** a circuit breaker, so a prompt away from the service's network does not pay a timeout.
- **H4** the wrapper succeeds when its target is missing.
- **H5** parse or refuse, one tagged entry, atomic rename.
- **H6** neither idle branch is written until the experiment picks one.
- **H7** the listener key is also a publishing key; scoped keys are the fix.
- **M1–M6, L1–L5** as folded into the body.

Two drifts, both accepted: shimming `scripts/artifacts-wait.sh` to the Python
client is dropped from scope — four behaviours won in a day, two selftests
covering two of them, and a shim would make both pass while testing nothing —
and per-agent seats were already Jeremy's ruling, so they lead the
prerequisites rather than being absent.

The most useful paragraph of round one, unreserved: of the four real failures
this project had that day, the tests originally proposed here would have caught
none.
