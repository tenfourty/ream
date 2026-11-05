# artifacts — a page and decision service for agents

Agents publish HTML pages over an HTTP API with a key; a person reads them in a gallery and
answers questions on them without opening a terminal. This file describes the code and how to
work on it, anywhere. How a particular deployment runs it -- its tracker, hosts, keys and
people -- is not here: on the homelab, read runcove's `docs/offices/artifacts-maintainer.md`.
This file follows runcove's `ways-of-working/agent-instructions.md` (one `AGENTS.md`, no
`CLAUDE.md`).

## WHY

The owner wanted a place where **any** agent -- Claude or not, on this machine or another -- can
put a page he can read, and where he can answer a question without a terminal: *"the mvp for the
other agents is being able to interact on decisions on a page and not just visualise progress"*,
and *"it's okay if the first version works only here … but ultimately it needs to work for agents
anywhere that I give access to -- just build with the end in mind."*

So: pages are published over an HTTP API with a key, served from their own origin, behind an
authenticating proxy. Decisions are first-class, not a convention. **The agent that published a
page gets the nudge when he answers, not an operator.**

### Every human action has an agent equivalent

*"Ideally agents can take all the same actions a human can"* -- a standing acceptance criterion.
**A gallery control with no `/api/v1` twin is half a slice**, and the half that gets left out is
always the agent's. Archive, rename and pin all shipped as browser forms first, leaving an agent
unable to fix the name of a page it had published itself.

The one asymmetry is not an exception: an agent may not **answer** a decision as him. Its
equivalent is `POST /decisions/{id}/record`, which stores the relay with its source. *Give the
agent the action, never the disguise.*

Scope is not symmetry. His controls are unscoped because the wall is his and the proxy is in
front of it; agent routes are scoped to the calling key's own agent, and "not yours" answers
**identically** to "no such page", so a key cannot be walked to learn which ids are real.

A request is identified by its key, never by a name it claims. Renaming an agent
(`artifacts-admin rename-agent`) moves everything it owns, and its old key keeps working as the
new name; published pages never change owner by accident.

## WHAT

| Piece | Where |
|---|---|
| The service (FastAPI + Postgres) | `src/artifacts/` |
| The agent CLIs (`artifacts-publish`, `artifacts-decide`, …) | `src/artifacts/client.py`, `decide.py` |
| Admin commands (keys, scopes, renames) | `src/artifacts/admin.py` -> `artifacts-admin` |
| The Postgres <-> SQLite copier and its verifier | `src/artifacts/migrate.py` |
| Runbooks (cutover, rollback, backups, restore drill, CI logs) | `docs/agent/runbooks/` |
| Tests | `tests/` |
| Container | `Dockerfile` |
| Deployment values | the homelab repo (`k8s/apps/artifacts/artifacts/app/`); `deploy/` here is no longer applied |
| CI -> image -> pin PR | `.forgejo/workflows/ci.yml`, `.forgejo/scripts/pin-pr.sh` |
| The design this argues from | `docs/agent/design.md` |
| What was decided and why the other options lost | `docs/decisions.md` |
| The build plan and its ledger | `docs/agent/implementation-plan.md` |

## HOW

```bash
uv sync --frozen --all-extras   # the server stack is the `server` extra
uv run ruff check src tests
uv run pytest -q -n auto --maxprocesses 8   # the full suite, in parallel; a throwaway Postgres per run
uv run pytest -q tests/test_x.py            # one file, serially
PG_TEST_DSN=postgresql://… uv run pytest -q -n auto --maxprocesses 8   # or an existing Postgres
ARTIFACTS_TEST_DB=sqlite uv run pytest -q -n auto --maxprocesses 8      # the same suite on SQLite
```

The service runs on Postgres or SQLite, chosen by `DATABASE_URL` (`postgresql://…` or
`sqlite:////data/artifacts.db`). Only `src/artifacts/db.py` talks to a driver; SQL elsewhere must
read the same on both (`docs/agent/specs/2026-10-09-sqlite-design.md`). CI runs the suite on both.

The browser tests (comments panel, hover cards, thumbnails, shim placement) need a headless
chromium: `ARTIFACTS_BROWSER=<path>`, or `chromium`/`chrome-headless-shell` on PATH; without one they
skip (`-rs` names them). CI installs Debian's chromium and runs those four files serially in their own
step with `ARTIFACTS_REQUIRE_BROWSER=1` (the parallel runs hide the browser: `ARTIFACTS_BROWSER=/nonexistent/browser`), which
makes a missing browser fail the run at start instead of skipping -- so a green CI run means they ran.

**On baba, never run the test suite** (Jeremy, 13 Oct 2026: no test-suite runs there). Push the
change to a `ci/<name>` branch instead: CI runs the full suite on it and never builds an image or
deploys from it. Land on `main` once that run is green, then delete the branch.
Reading a run's log without a browser, safely: `docs/agent/runbooks/ci-logs.md`.

**Declare which session you are on every publish.** A key may be shared by several sessions;
without a label a page says only which KEY published it. `ARTIFACTS_SESSION="<agent>/<session
ref>"` rides on the page, version, decision and events. A page published without it stays
unattributed until its next real change -- an identical republish mints no version.

**A push to `main` asks to deploy.** CI tests, builds an image, and opens a pin PR on the
deployment's manifest repo that moves production onto that image
(`.forgejo/scripts/pin-pr.sh`); production moves when that PR is merged. On the homelab the
manifests live in the homelab repo and its maintainer merges the pin PRs.

After changing a CLI, reinstall it where agents run it (`uv tool install <checkout> --force`).

## Safety rules

### Secrets

- **Never read a credential file, sops file or token file into a transcript** -- no `cat`,
  `grep`, `-A/-B/-C`, `source`, `less` or `sops -d`, not even "to check the format". Extract ONE
  key into a variable *inside a script* and print counts, never content. Never put a secret on a
  command line.
- **Never write or create a credential file for a human to fill in.** A file an agent writes
  joins edit-tracking, and the human's next edit is injected into the transcript with no tool call
  at all. Give the path and key names in prose; the human creates the file.
- A value that reaches context survives compaction and is re-emitted in the summary.
- A secret in an image is a different question from one in a commit: the image is pushed to a
  registry.

### Changing live systems

- **Never gate a destructive step on an untested predicate.** A broken wait-loop returns empty and
  reads as "all clear". Assert positive counts, not the absence of bad output.
- **Prove each arm of a gate with the literal input the real call will pass.** A deploy waiter
  that proved both arms with a full image tag reported a false TIMEOUT on a real deploy because the
  real call passed a short hash.
- **An in-place edit kills every process still running the old bytes.** bash reads a script
  incrementally, so rewriting a running script makes it resume mid-token and die with a syntax
  error on a file that passes `bash -n`. Stage `<script>.new` on the same filesystem, validate it,
  then one atomic `mv`. **A syntax error in a script that passes `bash -n` means the file changed
  under a running process.**
- **`pkill -f <pattern>` matches the shell that is running it.** Kill by PID from `pgrep`,
  skipping `$$`, or keep the job id.
- **Test end-to-end, not just the port.** A function that returns the right string in the
  container is not the same claim as a reader seeing it on the page; say which one you checked.

### The guards this repo enforces

**They only protect a clone that has run `lefthook install`** -- do that first in any new
checkout; `scripts/guards/run-selftests.sh` tells you whether this one is protected.

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

Each guard degrades to a clean SKIP when its tool is missing, and says loudly that nothing was
scanned. Each has a `--selftest`; CI runs all of them, plus the whole-tree secret scan, on every
push.

What is NOT mechanical, and still yours to hold:

- Stage explicit paths; commit with a pathspec. `git commit -- <paths>` commits the **working
  tree** of those paths, so read `git status --porcelain -- <paths>` first.
- Never `git stash` in a tree another session might share.
- **`git reset --hard` discards uncommitted work in tracked files.** Prefer `git checkout <ref>`
  or reset a single path.
- Announce a push, with hashes, before making it, and verify what actually went out against the
  remote (`git merge-base --is-ancestor <hash> origin/main` after a fetch). `PUSH_COUNT`
  mechanises the count, not the hashes, and a rebase keeps the count and changes every hash.
