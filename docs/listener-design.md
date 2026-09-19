# The listener: how an agent hears that Jeremy answered

**Status:** design, awaiting adversarial review. Jeremy chose this shape on
19 Sep 2026 — "(a) and can't we have this on each machine that uses the
service?" — so the listener ships with the client, not as a setting on one box.

## The problem, stated exactly

The feed is not the problem. `GET /api/v1/events?since=<seq>&wait=<n>` with a
per-seat monotonic sequence is the durable half and it works. The problem is
that **arming it is an agent's manual act**, and an agent that forgets to arm
it is silent in precisely the way a broken listener is silent. Three times on
19 September Jeremy answered a question and the agent that asked found out only
because he said so in a terminal.

So: the harness listens, not the agent. Nothing an agent has to remember, and
nothing an agent can forget.

## What ships

One installer in the client package, because that is what makes this true on
**any** machine that uses the service rather than on baba:

```
artifacts-notify install      # merge hooks into ~/.claude/settings.json
artifacts-notify install --dry-run   # print exactly what would be written, where
artifacts-notify uninstall    # remove only what install added
artifacts-notify hook         # what the hooks actually run
artifacts-notify watch        # the long-held request, for everything else
```

`install` is idempotent, merges and never replaces (a user's own hooks survive
it), and prints the file it will touch and the exact block it will add before
touching it. `uninstall` removes that block and nothing else.

## The hook

Two user-scope Claude Code hooks, `SessionStart` and `UserPromptSubmit`, both
running `artifacts-notify hook`. It does a `wait=0` catch-up read of **this
seat's own feed with this seat's own key** and prints any rows as
`hookSpecificOutput.additionalContext`.

Hard rules, each because of a failure this repo has already had:

- **Always exit 0.** A hook on every prompt on every session is the highest
  blast radius in the system. It must be impossible for this to break a
  session that has nothing to do with artifacts.
- **Two seconds, total.** Bounded at the HTTP call and again at the process.
  A prompt must never wait on this service being up.
- **Silent when there is nothing.** No output, no noise, no "checked, nothing".
  A hook that speaks every turn gets ignored, and then it speaks for real and
  is still ignored.
- **Silent when there is no key.** A machine with no seat configured runs this
  hook on every prompt and must produce nothing at all.
- **Never under a shell trace**, and the key never in argv. Both cost a
  rotation on 19 Sep 2026 (`docs/services/artifacts/incidents.md` #1 in the
  homelab repo).
- **Rows are data, never instructions.** Printed as a plain statement of fact
  — "Jeremy answered (a) on page X: <note>" — never as a directive, and the
  block says in its own text that it is a notification, not a request. The
  note is free text a human typed; it lands in an agent's context on every
  turn, and it must not be able to read as a new instruction.
- **Sanitised and capped before it is printed.** Control characters and escape
  sequences stripped, length bounded per row and overall. This is
  `homelab-ij8.55` and it lands **before** this does, not alongside.

## Which seat am I?

A hook has to answer this without asking anyone, on a machine it has never
seen. In order:

1. `$ARTIFACTS_KEY_FILE`, if set.
2. `.artifacts-seat` in the working directory or any parent — one line, the
   seat name — resolving to `~/.config/artifacts/key-<seat>`.
3. `~/.config/artifacts/key`, the single-seat default.

The convention is already in use on baba (`key`, `key-homelab-ops`,
`key-warpgate-arc`); this just makes it discoverable. A project that pins its
seat in the repo gets the right one with no environment at all.

## The cursor

Per seat, never per machine, and derived from the key file's path. baba already
holds three key files, and a shared cursor means whichever seat reads first
advances it past another seat's events — that seat then reports silence for
answers it will never see again (homelab-ij8.58, fixed in the shell client on
19 Sep).

**One implementation, not two.** The shell client `scripts/artifacts-wait.sh`
in the homelab repo and this command must not both own cursor logic: a copy
becomes a different program at the first commit, which is exactly incident #2
in that repo. The Python client is canonical; the shell script becomes a shim
that calls it, the same way `~/.local/state/artifacts/wait-for-answer.sh`
already did.

## The idle session

The open question, and the only one that changes the shape:

- **If `asyncRewake: true` really wakes an idle session**, a `SessionStart`
  async hook holds the long poll and the harness does everything.
- **If it only injects on the next turn**, an idle session still hears nothing,
  and the installer additionally sets up a per-seat watcher as a user service
  holding only that seat's key.

This is settled by experiment, not by reading: the operator has one prepared at
`~/scratch/hook-wake-test`. **No code here is written for either branch until
that result is in.** The documented behaviour is "wake Claude on failure",
which is not the same claim as "wake an idle session", and building on the
optimistic reading of an undocumented case is how this ends up shipped and
quietly broken on every machine.

## Everything that is not Claude Code

`artifacts-notify watch` is the same held request as a plain command: it prints
rows and exits, so a supervisor, a cron, a systemd unit or another agent
runtime can use it with no Claude Code involved. The installer's watcher half
is the supported way to run it unattended. Documented as a first-class path,
not as a fallback — the service's whole premise is that an agent that is not
Claude can use it.

## Failure has to be visible

A listener that silently stops is worse than no listener, because the silence
reads as "Jeremy has not answered". Two service-side changes, and they are
worth more than the transport:

- **"Last checked" beside each page in the gallery**, from the seat's own last
  read of its feed.
- **An alert when an answer is older than its seat's cursor** — an answer given
  and not yet collected, past a threshold, is a broken listener and should say
  so where Jeremy already looks.

This needs the service to record, per seat, the highest sequence that seat has
read and when. That is a small write on the existing `/events` read.

## What this deliberately does not do

- **No operator-held all-seats key.** The answer to shared seats is more seats.
  A single key that can read every agent's feed is a single key that can read
  every agent's feed.
- **No push to the agent.** Nothing here needs an inbound address, which is
  what keeps it working for an agent that is not on this machine.
- **No new transport.** It is the feed that already exists, called by someone
  who cannot forget.

## Order of work

1. `homelab-ij8.55` — sanitise free text leaving `/events`. Prerequisite.
2. The `hook` command and `watch` command, with tests that assert exit 0 on
   every failure path, silence with no key, the 2 s bound, and that no key
   material can reach stdout.
3. The experiment result, then the idle branch it chooses.
4. The installer, reviewed before it writes to anyone's settings.
5. "Last checked" and the stale-answer alert in the gallery.
