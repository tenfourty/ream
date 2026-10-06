# artifacts — the homelab's page-hosting service

## START HERE, every session and after every compaction

**The `runcove-5t3oj` beads are this seat's state of record** — progress, decisions, open
questions, and which beliefs were verified rather than merely told. Not your
memory, and not a compaction summary. Before acting on anything you think you
know:

```bash
bd -C ~/repos/runcove prime                          # the shared runcove tracker, primed
bd -C ~/repos/runcove list --parent runcove-5t3oj    # this seat's work
ls docs/agent/handoff-*.md           # then read the newest one
```

Then separate what you have **verified this session** from what you were
**told**. That distinction matters more in this seat than in most: the pages
this service publishes are read as current, so a claim carried over from a
summary and republished is a stale claim with a fresh timestamp on it — which
is the failure the whole service exists to prevent.

The `SessionStart` hook in `.claude/settings.json` says the same thing
mechanically, and it is the ONLY one that can: measured in Claude Code 2.1.278
on 21 Sep, a `PostCompact` hook has no path into a session's context at all
(its output is displayed at the human and discarded), while SessionStart fires
again with `source=compact` at every compaction and its context is injected.
A PostCompact hook was wired here for a day and every firing was rejected. The
hook is the reminder; this section is the rule, and the rule is what survives
if the hook is ever disabled. Ruled by Jeremy, 2026-09-20: *"It's super
important we don't lose state or trust in our state."*

## WHY

Jeremy wanted a place where **any** agent — Claude or not, on this machine or
another — can put a page he can read, and where he can answer a question
without opening a terminal. The brief, in his words: *"the mvp for the other
agents is being able to interact on decisions on a page and not just visualise
progress"*, and *"it's okay if the first version works only here and in the
homelab but ultimately it needs to work for agents anywhere that I give access
to — just build with the end in mind."*

So: pages are published over an HTTP API with a key, served from their own
origin, fronted by Warpgate and authed by Authelia. Decisions are first-class,
not a convention. **The agent that published a page gets the nudge when he
answers, not the operator.**

### Every human action has an agent equivalent

Jeremy, 20 Sep 2026: *"ideally agents can take all the same actions a human
can."* A standing acceptance criterion, not a one-off request. **A gallery
control with no `/api/v1` twin is half a slice** — and the half that gets left
out is always the agent's, because his is the one he asked to see. Archive,
rename and pin all shipped as browser forms first, leaving a seat unable to fix
the name of a page it had published itself.

The one asymmetry is not an exception: an agent may not **answer** a decision as
him. Its equivalent is `POST /decisions/{id}/record`, which stores the relay
with its source. *Give the agent the action, never the disguise.*

Scope is not symmetry. His controls are unscoped because the wall is his and
Authelia is in front of it; the agent routes are scoped to the calling key's own
agent, and "not yours" answers **identically** to "no such page" so a key cannot
be walked to learn which ids are real.

## WHAT

| Piece | Where |
|---|---|
| The service (FastAPI + Postgres) | `src/artifacts/` |
| Tests | `tests/` — `uv run pytest -q -n auto --maxprocesses 8` |
| Container | `Dockerfile` |
| Deployment values | `deploy/` |
| CI → image → deploy | `.forgejo/workflows/ci.yml` |
| The design this argues from | `docs/agent/design.md` |
| The build plan and its ledger | `docs/agent/implementation-plan.md` |

**Lives in the homelab repo, not here** (`~/repos/homelab`):

- `k8s/apps/artifacts/**` — the GitOps manifests. **Flux watches the homelab
  repo**, so the cluster changes only when that repo changes. A change to
  `deploy/artifacts-config.env` here is inert until the homelab side picks it
  up — check which of the two actually holds the value before editing.
- `scripts/artifacts-wait.sh`, `scripts/lib/artifacts_wait_render.py` and the
  four waiter tests — the client every homelab session runs.
- `scripts/artifacts-status.py` + `docs/pages/artifacts-status.tmpl.html` — the
  status page this arc publishes for Jeremy.
- `scripts/ops-decisions-page.sh`, `scripts/lib/ops-decisions-artifacts-client.py`
  — the operator's decisions page.
- `docs/services/artifacts/{readme,incidents}.md` — the service's durable facts.
  **Durable facts go there, not in agent memory and not only here.**

Those stayed because other sessions run them every day; moving them would have
broken peers mid-shift. Change them in the homelab repo, under its rules.

## HOW

```bash
uv sync --frozen --all-extras   # the server stack is the `server` extra
uv run ruff check src tests
uv run pytest -q -n auto --maxprocesses 8   # the full suite, in parallel; a throwaway Postgres per run
uv run pytest -q tests/test_x.py   # one file, serially
PG_TEST_DSN=postgresql://…  uv run pytest -q -n auto --maxprocesses 8   # or an existing Postgres
```

**Declare which session you are, on every publish.** A key is shared by every
session on the machine, so without this a page says only which KEY published it
— and on 22 Sep 2026 that produced a wrong attribution and four of Jeremy's
answers read as belonging to a departed seat. Set it once per shell:

```bash
export ARTIFACTS_SESSION="artifacts-seat/<this session's ref>"
```

`artifacts-publish --list` then marks your own pages `(you)`, and every question
you pose says who asked it. A page whose live version was published without it
stays unattributed until its next real change — an identical republish mints no
version, so this arrives as pages move, never retroactively.

Push to `main` on `git.tenfourty.site/runcove/artifacts` runs CI, builds
`git.tenfourty.site/runcove/artifacts:<sha12>`, and **pushes a
`deploy: … [skip ci]` commit back onto main**. Two consequences that have both
bitten:

- Your next push is a **non-fast-forward** unless you fetch and rebase first.
  This is normal, not a conflict with another agent.
- A deploy lands minutes after the push, unattended. Nothing gates it. Treat a
  push here as a production change, because it is one.

The live pod is `-n artifacts` in the homelab cluster
(`KUBECONFIG=$HOME/.kube/config-homelab`). It serves the gallery and the
content origin on one port, discriminated by `Host`.

## Tracking

Since 6 Oct 2026 (Jeremy's ruling) the work lives in the **shared runcove
tracker**, under the epic `runcove-5t3oj`, labelled `artifacts` + `repo:artifacts`
(plus `repo:homelab` / `repo:runcove` when a change lands there). Follow
runcove's conventions in `~/repos/runcove/.beads/PRIME.md` and
`scripts/tracker-board/RITUALS.md`: `BEADS_ACTOR=artifacts-seat` on every write,
`bd update <id> --claim` when you start, a daily `health:` comment on the epic
and `update:` on each claimed item, and `bd dolt commit` after every write.
**They are the state of record, not a to-do list** — see START HERE above.

```bash
export BEADS_ACTOR=artifacts-seat
bd -C ~/repos/runcove list --parent runcove-5t3oj
bd -C ~/repos/runcove show runcove-5t3oj.1
```

The history before the move -- the design, about 115 finished items and every
ruling -- stays at `homelab-ij8.*` in the homelab tracker (`bd -C ~/repos/homelab
show homelab-ij8.69`); each moved item links back to it. Do not start a beads
workspace of this repo's own: splitting the tracker is how an arc loses its history.

`bd update --notes` **replaces** the notes and warns only afterwards, with no
history and no export. Always `--append-notes`.

**A bead update is also a page update** (Jeremy, 19 Sep 2026): when a bead
changes for work that has a page on this service, republish that page. For this
seat's own board that is now one command, which redraws every page listed in
`~/.config/artifacts/pages.toml` from the tracker and mints nothing when
nothing changed:

```bash
ARTIFACTS_SEAT=baba_operator artifacts-key -- artifacts-republish
```

For any other page: `artifacts-publish <file> --update <id>`. Until 22 Sep the
first of those could not authenticate at all (it read the cleartext key file,
which was vaulted on the 21st) and said so only to its own log — which is how
the board silently went a day stale.

## Safety Rules

### Secrets

- **Never read a credential file, sops file or token file into a transcript** —
  no `cat`, `grep`, `-A/-B/-C`, `source`, `less` or `sops -d`, not even "to
  check the format". Extract ONE key into a variable *inside a script* and
  print counts, never content. Never put a secret on a command line.
- **Never `Write`/`Edit`/create a credential file for a human to fill in.** A
  file an agent writes joins Claude Code's edit-tracking, and the human's next
  edit is injected into the transcript with no tool call at all. Give the path
  and the key names in prose and let Jeremy create the file. Seven leaks
  13–17 Sep 2026; the seventh needed no tool call.
- A value that reaches context survives compaction and is re-emitted in the
  summary.
- **Leak policy**: a secret that never left this machine is vaulted (sops or
  another encrypted store, never a cleartext copy) and redacted from every
  transcript, log and scratch file (`[REDACTED…]`); no rotation. A secret that
  left the homelab (a push, a message, an external service, a device off the
  LAN) is rotated.
- **This repo is not mirrored off the homelab** (only `runcove/warpgate` is,
  measured 2026-09-20). Re-measure rather than assume it, but on that reading a
  secret reaching a commit here is in the *vault-and-redact* case, not the
  rotate case. A secret reaching the **image** is a different question — the
  image is pushed to the forge registry.

### Changing live infrastructure

- **Claude Code runs on baba** — never SSH to baba, run commands directly.
- **Never start VMs or make network changes** without Jeremy's explicit
  approval.
- **Never gate a destructive step on an untested predicate.** A broken
  wait-loop returns empty and reads as "all clear". Assert positive counts, not
  the absence of bad output. The homelab's
  `agent_docs/verification-and-gating.md` is the long version and is worth
  reading — it is a list of real failures, each with the shape that caused it.
- **Prove each arm of a gate with the literal input the real call will pass.**
  Proving both arms is not enough if the probe used a tidier input shape:
  20 Sep 2026, a deploy waiter proved DEPLOYED and TIMEOUT with a full image
  tag, then reported a false TIMEOUT on a real deploy because the real call
  passed a short hash.
- **An in-place edit kills every process still running the old bytes.** bash
  reads a script incrementally by byte offset for as long as it runs, so
  rewriting a running script makes it resume mid-token and die with a syntax
  error on a file that passes `bash -n`. Stage `<script>.new` somewhere on the
  same filesystem, validate it there, then one atomic `mv`. The diagnostic is
  worth memorising: **a syntax error in a script that passes `bash -n` means
  the file changed under a running process, not that the script is broken.**
- **`pkill -f <pattern>` matches the shell that is running it.** Three times on
  21 Sep a `pkill -f "port-forward…"` killed this session's own shell (exit
  144), losing a heredoc and a written bead note with it: the tool's command
  line contains the pattern, so the pattern matches itself. Kill by PID from
  `pgrep`, skipping `$$`, or start the process with a job id you keep.
- **Test end-to-end, not just the port.** A function that returns the right
  string in the container is not the same claim as a reader seeing it on the
  page; say which one you actually checked.

### The guards this repo enforces

Jeremy asked for the homelab's guards here on 22 Sep 2026, and they are
installed. **They only protect a clone that has run `lefthook install`** — do
that first in any new checkout, and `scripts/guards/run-selftests.sh` will tell
you whether this one is protected. Every rule below used to be a paragraph in
this file that somebody who had read it broke anyway.

| When | What is refused | Override |
|---|---|---|
| pre-commit | a secret in the staged diff (ggshield) | `GGSHIELD_SKIP=1` |
| pre-commit | a credential handed to a process as an argument | none — fix the call |
| pre-commit | ruff and shellcheck findings in staged files | none |
| prepare-commit-msg | `--amend` / `-c` / `-C` / `--fixup` / `--squash` | `AMEND_OVERRIDE=1` |
| prepare-commit-msg | …of a commit already on a remote | **also** `AMEND_PUSHED_OVERRIDE=1` |
| commit-msg | a non-conventional subject; a `Co-Authored-By` or "Generated with Claude Code" line | `COMMIT_MSG_OVERRIDE=1` |
| pre-push | an outgoing range that is not the count you announced | none: `PUSH_COUNT=N git push` |
| pre-push | ruff findings, or unformatted files, anywhere in `src`/`tests` | none — `uv run ruff format <files>` |

Each guard degrades to a clean SKIP when its tool is missing, so a machine
without ggshield can still commit — and says loudly that nothing was scanned,
because a gate that fails closed on a missing external service is a gate
somebody deletes. Each has a `--selftest`; CI runs all of them, plus the
whole-tree secret scan, on every push.

What is NOT mechanical here, and still yours to hold:

- Stage explicit paths; commit with a pathspec. Remember that
  `git commit -- <paths>` commits the **working tree** of those paths, so read
  `git status --porcelain -- <paths>` first.
- Never `git stash` in a tree another session might share.
- **`git reset --hard` discards uncommitted work in tracked files.** 22 Sep
  2026: a `--hard` used to tidy up after a detached-HEAD experiment silently
  took an unrelated, uncommitted CI change with it. Prefer `git checkout <ref>`
  or reset a single path.
- Announce a push, with hashes, before making it, and verify what actually went
  out against the remote (`git merge-base --is-ancestor <hash> origin/main`
  after a fetch), never against your own announcement. `PUSH_COUNT` mechanises
  the count; it cannot mechanise the hashes, and a rebase (which CI's
  `deploy: … [skip ci]` commit forces on you) keeps the count and changes every
  hash.

## Language for Jeremy

He does not read the plans. Anything he reads — Telegram, a page, the board,
a terminal recap, commit subjects he skims — names the **outcome** ("the
questions he hasn't answered are now the first thing on the page"), never plan
coordinates (GATE 4, Phase 2, T2.1, ij8.71). Decisions go to him as ordered
sets with lettered options, recommendation first as (a), because that is how he
answers.

Read the clock before stamping anything — including time-of-day *words*.
"Tonight" written at 18:38 was wrong seven times in one file.

## Working with Jeremy

- **Stay in your lane.** This seat does artifacts. Homelab page triage, cluster
  reads and the waiting queue belong to the operator session. If something you
  see touches this arc, send ops one line; otherwise leave it.
- **Don't idle.** Finishing the pushed work is not the end of the shift. Take
  the top unblocked item off the arc's queue and announce it rather than
  waiting to be told.
- **No external comms.** Never post, mail or open issues anywhere. Draft the
  text; he approves or sends it.
- **Update by default.** Update unless there is a *named* showstopper; anything
  held for him is asked the same day, with the risk stated.
- **Ack with an emoji**: 👀 on receipt, ✅ when done. A pure emoji reply can be
  the whole answer.
- **Report health, not noise**: lead with health and real incidents (impact,
  fix, prevention); false positives get one count line.
- A reminder list is a claim — verify every "pending on you" line against the
  changelog before writing it. Two such items had been done for days.
