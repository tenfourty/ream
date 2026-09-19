# The listener: how an agent hears that Jeremy answered

**Status:** revised after round one of adversarial review (19 Sep 2026). Every
finding is triaged at the end of this document, with the reason. Jeremy chose
this shape — "(a) and can't we have this on each machine that uses the
service?" — so the listener ships with the client, not as a setting on one box.

**Nothing is built yet.** Three things must be true before any listener code is
written, and they are the review's, not mine:

1. **Every agent has its own seat and the shared seat is retired.** Until then
   delivery is to "some session on this machine" and the agent that asked is
   not the one that hears.
2. **The sanitiser is deployed and verified against the live `/events`** —
   committed is not running — and free text stays out of context regardless.
3. **The idle experiment has a result**, and the client carries a version the
   service can see.

## The problem, stated exactly

The feed is not the problem. `GET /api/v1/events?since=<seq>&wait=<n>` with a
per-seat monotonic sequence is the durable half and it works: measured against
the live service at 5 s, 30 s, 70 s, 110 s and 180 s, each answering within
0.11 s of the value asked for.

The problem is that **arming it is an agent's manual act**, and an agent that
forgets is silent in precisely the way a broken listener is silent. Three times
on 19 September Jeremy answered a question and the agent that asked found out
only because he said so in a terminal.

So: the harness listens, not the agent.

## What tonight's measurement changed

While this design was in review, two seats logged the same failure against the
live service — `timed out after 300001 milliseconds with 0 bytes received` —
within a minute of a deploy, while the same service answered every shorter hold
exactly on time. The cause was not the hold: **a held request did not survive
the pod rolling.** The old process was asked to stop, a request sleeping out a
280-second hold did not finish, and the platform killed it 30 seconds later.
The client saw a request that never answered, and the client of the day called
that silence.

Fixed server-side (`events.begin_shutdown`, wired to SIGTERM in
`artifacts.serve`): a held request now ends the instant the process is asked to
stop, with an ordinary empty 200 — which already means "nothing yet, ask
again", exactly what a client should do when the server it is talking to is
going away.

Two consequences for this design, both load-bearing:

- **A client must treat a timeout as "retry now", never as a failed round.**
  Deploys are routine; a listener that treats one as an error will report a
  broken path every time the service is updated.
- **Every deploy tonight silently broke every waiting agent**, and nothing
  anywhere said so. That is the case for the visibility section below, made by
  the system rather than by argument.

## What ships

One installer in the client package, because that is what makes this true on
**any** machine that uses the service rather than on baba:

```
artifacts-notify install      # merge hooks into ~/.claude/settings.json
artifacts-notify install --dry-run   # print exactly what would be written, where
artifacts-notify uninstall    # remove only what install added
artifacts-notify hook         # what the hooks actually run
artifacts-notify watch        # the long-held request, for everything else
artifacts-notify version      # what this machine is running
```

## The hook

Two user-scope Claude Code hooks, `SessionStart` and `UserPromptSubmit`, both
running `artifacts-notify hook`: a `wait=0` catch-up read of this seat's own
feed with this seat's own key, printed as
`hookSpecificOutput.additionalContext` with `hookEventName` matching the firing
event. `SessionStart` matchers are `startup|resume|clear|compact` — compaction
is where this project has lost state before.

**It injects no free text.** One line per row: the kind, the page title, the
decision id, and the exact command to read it. The agent that cares fetches
Jeremy's note in its own turn, where reading it is that agent's decision.

That is the whole of the injection defence, and it is deletion rather than
filtering. The alternative — printing the note — puts up to 1 KB of arbitrary
human text into every session on the machine on every prompt, and the trust
boundary is not even "Jeremy": `comments.add` takes an author kind, so another
agent's comment on my page would reach my sessions. Sanitising is still
required at the storage boundary (`answer_note` and `comment.body` are raw
today; only `events.summary` is cleaned), but it is the second line, not the
first.

Hard rules, each because of a failure this repo has already had:

- **Always exit 0** — and that is a property of the command line, not only of
  the client. The hook entry is an absolute path to an installed wrapper
  written so that a missing target is still success, and `install` refuses to
  write an entry whose target does not exist. A settings file synced to a
  machine where `install` never ran must not return 127 on every prompt.
- **Two seconds, total**, bounded at the HTTP call and again at the process.
- **A circuit breaker.** The service answers on the LAN only and "every
  machine" includes laptops. Three consecutive failures and the client stops
  calling the network, retrying once per 10 minutes, reset on the first
  success; and the call is skipped outright if a successful check happened in
  the last few seconds. Without it, every prompt in every session away from
  home pays the full timeout, for ever, silently.
- **Silent when there is nothing, and silent when there is no key.**
- **Never under a shell trace**, and the key never in argv. Both cost a
  rotation on 19 Sep 2026.
- **The cursor advances to the highest sequence actually PRINTED**, never to
  the highest fetched. A cap that prints 5 of 40 and advances past all 40 is
  the fetch-and-discard bug wearing a hat — the same bug, by construction, that
  cost this project Jeremy's answers this afternoon. The block ends with "N
  more waiting".
- **A seat's first run seeds, it does not replay.** No cursor means `since=0`,
  which is up to 100 rows of history injected into a prompt at install time.
  `install` seeds each seat's cursor to the current maximum sequence; a first
  hook run with no cursor does the same and prints nothing. Existing
  `cursor-*` files are **abandoned, not imported** — including the operator
  seat's under `ARTIFACTS_WAIT_STATE`. Importing a cursor whose provenance
  cannot be established is how a seat starts life having "already read" an
  answer nobody saw.

## The environment is not an input

In hook mode the client **ignores the environment entirely**. This is the
sharpest finding of the review and it is right: a hook inherits the session's
environment, so a `.envrc` in a cloned repository could repoint it at another
host, which would then receive the seat key as a bearer token on every prompt
and get to inject text into that session's context.

- Base URL from `~/.config/artifacts/config`, never `$ARTIFACTS_API`.
- Key paths resolve only inside `~/.config/artifacts/`, mode-checked.
- Seat names match `^[a-z0-9][a-z0-9-]{0,31}$` — no separators, no traversal —
  and must select a key file that already exists.
- `.artifacts-seat` is honoured only from `$CLAUDE_PROJECT_DIR`, never from
  "any parent", which includes any writable directory above a checkout.

Before any of this, the two clients' variable names are unified: the Python
client reads `$ARTIFACTS_API` and the shell client reads `$ARTIFACTS_API_BASE`,
which is a latent bug today independent of this design.

## Which seat am I?

Per-agent seats are a **prerequisite**, not a later tidy-up. Today every page
is owned by one shared seat, and with a shared seat this whole design delivers
an answer to "some session on this machine". Jeremy already ruled on this: the
agent that published the artifact is the one that needs the nudge.

Given per-agent seats, resolution is: `$CLAUDE_PROJECT_DIR/.artifacts-seat` if
present, else the single-seat default `~/.config/artifacts/key`, with the
validation above.

## The cursor

**Per session, not per seat** — `cursor-<seat>-<session id>`, from the session
id the hook is given on stdin.

Per-seat was my first answer and it is wrong for the same reason per-machine
was: several sessions share a seat, rows are delivered once, and the first
session to poll takes them. The session that asked the question then reads past
them and hears nothing — and because the cursor moved, an alert keyed on the
cursor reports healthy over a lost answer. Duplicate delivery to a sibling
session is harmless; loss is not.

**One implementation, eventually — but not by shimming a script whose guards
are one day old.** `scripts/artifacts-wait.sh` carries four behaviours won
today: a trace refusal placed before its own `set +x`, a cursor written only
after rows are printed, "every request failed is an error, not silence", and
replay-beats-loss. Two selftests cover two of them, and a shim would make both
pass while testing nothing. The Python client carries all four with its own
tests, and the shell tests are repointed, **before** any shim lands. (The shim
already in place at `~/.local/state/artifacts/wait-for-answer.sh` is a
different thing: it replaced a stale *copy* that had none of those guards.)

## The idle session

Still the open question, and it changes the shape:

- **If `asyncRewake: true` wakes an idle session**, a `SessionStart` async hook
  holds the long poll and the harness does everything.
- **If it only injects on the next turn**, an idle session hears nothing, and a
  per-seat watcher runs as a user service.

The watcher branch is deliberately **not** specified further until the
experiment picks it, because its missing half is the thing that was rejected
elsewhere: to wake an idle session it must post to that session's messaging
socket, which needs a seat→session map — the routing table that can be silently
wrong. If it ships: its own cursor namespace (a watcher sharing a session's
cursor drives it *backwards*, re-injecting the same answers every prompt until
it catches up), `Restart=always`, failure visible in the gallery, and a map
built from a breadcrumb each session writes for itself. A re-arm rule and a cap
on consecutive wakes are part of that branch, since a wake starts a turn and a
turn fires the hooks.

Settled by experiment, not by reading. "Wake Claude on failure" is not the same
claim as "wake an idle session".

## Everything that is not Claude Code

`artifacts-notify watch` is the same held request as a plain command. **And the
plain-curl path stays documented** — `curl -sS -H @- "$BASE/events?since=N&wait=280"` —
because requiring a Python package would undercut the promise that any agent
with a key is first class.

## Failure has to be visible

A listener that silently stops is worse than no listener, because the silence
reads as "Jeremy has not answered".

- **The alert is end-to-end: answered, and not acted on after N minutes** — the
  page not republished, the decision not closed. It must not be keyed on the
  cursor, because the cursor is exactly what a delivery race corrupts: the read
  that loses the answer is the read that clears the alarm.
- It is routed to `ops-timers-check` as well as the gallery. "Where Jeremy
  already looks" is what failed on 19 September.
- **"Last checked" is labelled honestly**: "this seat's key checked 3 min ago",
  not "healthy". Any session of the seat polling turns it green, including one
  with no interest in the page and one whose owning agent is dead.
- **The client version travels with every check** (`x-artifacts-client`),
  recorded with the seat's last read and shown beside it. What this ships is N
  frozen copies on N machines whose only healthy output is silence; the
  installed copy on baba already differs from the repo today. A seat running an
  old client must be as visible as a seat that stopped checking.
- The "last read" write is debounced server-side, or it is one database write
  per prompt per session.

## The key this installs

What goes on every machine is a credential that can publish, update and rename
that agent's pages and pose decisions to Jeremy in its name. A listener needs
none of that.

The fix is a `scope` column on `agent_key`, checked in `caller()`, and
read-only keys for listeners. Until that exists the exposure is stated here
rather than discovered later, and the gallery surfaces last-used-at and
last-seen client version per key, so a key live on a machine nobody remembers
is visible.

## What this deliberately does not do

- **No operator-held all-seats key.** The answer to shared seats is more seats.
- **No push to the agent.** Nothing here needs an inbound address, which is
  what keeps it working for an agent that is not on this machine.
- **No new transport.** It is the feed that already exists, called by someone
  who cannot forget.

## Order of work

1. **Per-agent seats**, and the shared seat retired. Prerequisite (B1).
2. **`homelab-ij8.55` deployed and verified against the live `/events`**, and
   `answer_note`/`comment.body` sanitised at the storage boundary. Committed is
   not running.
3. The `/events` envelope carrying a cursor (`M3`), keeping the bare array one
   release so the existing renderer survives the deploy.
4. The `hook` and `watch` commands, with the cap/cursor interaction and the
   two-consumer race under test — the two failures the originally proposed
   tests could not have seen.
5. The idle experiment's result, then the branch it chooses.
6. The installer, reviewed before it writes to anyone's settings: parse or
   refuse (never repair), append one tagged entry, temp file in the same
   directory, atomic rename; refuse a symlinked or group-readable
   `settings.json`; a second `install` is a byte-level no-op; `uninstall`
   removes only tagged entries. The merge target already holds another tool's
   `SessionStart` hook, and Claude Code reloads that file on change, so a
   half-written file is read by every running session.
7. "Last checked", the client version beside it, and the end-to-end alert.
8. Scoped read-only keys.

Windows is refused rather than half-worked: the settings path and the
mode-600 key check do not hold there.

---

# Round one review — triage

Every finding, with what was done. The review is at
`~/.local/state/homelab-ops/briefs/2026-09-19-listener-design-review.md`.

## Blockers — all four accepted

| # | Finding | Verdict |
|---|---------|---------|
| B1 | One cursor, many sessions: the asker loses the answer and the cursor-keyed alert says all clear | **Accepted in full.** Per-agent seats promoted to prerequisite; cursor is per session; the alert is now end-to-end (answered and not acted on), which no cursor race can fool. My per-key cursor fixed the machine-wide bug and left the session-sharing one, which is the common case today. |
| B2 | The hook prints Jeremy's free text; the prerequisite does not cover it, and is not deployed | **Accepted in full.** No free text in context at all: ids plus the exact read command. Deletion beats filtering. The review is also right on fact — the note is on the decision, not in the event summary, so my example was a fiction about a field that is not there. |
| B3 | The hook inherits the session environment; `.envrc` can repoint it and harvest the seat key | **Accepted in full.** Hook mode ignores the environment; fixed config path; key paths confined to `~/.config/artifacts/`; strict seat charset; `.artifacts-seat` only from `$CLAUDE_PROJECT_DIR`. The "or any parent" walk was mine and it was a path-traversal read waiting to happen. |
| B4 | Ships as a frozen copy per machine with no version signal | **Accepted.** `x-artifacts-client` on every check, recorded with the last read, shown in the gallery. The review's sharpest point is that the design cited incident #2 as its reason for one implementation and then proposed N frozen copies. |

## High — all seven accepted

| # | Finding | Verdict |
|---|---------|---------|
| H1 | A capped block plus one cursor is fetch-and-discard again | **Accepted.** Cursor advances to the highest sequence *printed*; the block says how many are waiting; tested with a large fixture. |
| H2 | First run replays a seat's history into a prompt | **Accepted.** `install` and a first run seed to the current maximum. Existing `cursor-*` files, including the operator seat's, are abandoned rather than imported, and the doc says so. |
| H3 | Two seconds on every prompt, for ever, off the LAN | **Accepted.** Circuit breaker plus a short success cache. |
| H4 | "Always exit 0" is a property of the client, but the harness runs a command line | **Accepted.** Absolute-path wrapper that succeeds when its target is missing; `install` refuses a non-existent target. |
| H5 | The merge target already has another tool's hook | **Accepted.** Parse-or-refuse, tagged entries, atomic rename, symlink and mode refusals, idempotent to the byte. |
| H6 | The watcher branch is half-specified and its missing half is what was rejected elsewhere | **Accepted.** Nothing written for either branch until the experiment picks one; if the watcher ships it gets its own cursor namespace and a self-written breadcrumb map. |
| H7 | The listener key is also a publishing key | **Accepted.** Scoped read-only keys are the fix and are in the order of work; the exposure is stated until they exist. |

## Medium and low

| # | Verdict |
|---|---------|
| M1 | **Accepted** — "last checked" labelled as what it measures; the alert moved off the cursor. |
| M2 | **Accepted** — routed to `ops-timers-check` as well as the gallery. |
| M3 | **Accepted** — the cursor envelope lands with the hook, bare array kept one release. |
| M4 | **Accepted, and the shim is dropped from scope.** Four paid-for behaviours, two selftests, and a shim would make both pass while testing nothing. Replacing the shell client is also drift: Jeremy did not ask for it. |
| M5 | **Accepted** — the plain-curl path stays documented. |
| M6 | **Accepted** — part of the idle branch, with a bound on consecutive wakes. |
| L1 | **Accepted** — `hookEventName` must match the firing event. |
| L2 | **Accepted** — matchers stated: `startup\|resume\|clear\|compact`. |
| L3 | **Accepted** — Windows refused rather than half-worked. |
| L4 | **Accepted** — the last-read write is debounced. |
| L5 | **Accepted** — `MAX_WAITERS` exhaustion is counted and exposed before a watcher per seat makes it bite. |

## Drift

Both drifts accepted. The shim was not asked for and is dropped. Per-agent
seats were already ruled on by Jeremy and are now first in the order of work
rather than absent.

## What the tests could not have seen

Accepted without reservation, and it is the most useful paragraph in the
review: of the four real failures this project had today, the proposed tests
would have caught **none**. Added: the cap/cursor interaction, the two-consumer
race, and the drill — including a hostile-note positive control and a
one-machine staged rollout — as the only things that reach the rest.
