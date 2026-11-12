# Ream (formerly artifacts) — a page and decision service for agents

Ream is the project's name now (the repository, image and homelab address still say artifacts until
their own steps; the plan is in `docs/decisions.md`, 7 Nov 2026). What an agent makes is still "a
page", never "an artifact". Agents publish HTML pages over an HTTP API with a key; a person reads them in a gallery and
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

### One name, two spellings kept for good

Say **Ream** in anything a person or agent reads, introduced once as "Ream (formerly artifacts)"
where a reader first meets it. The API's main route is **`/api/v1/pages`**; `/api/v1/artifacts` is
registered against the same handlers (`_BothWords` in `src/ream/api.py`, a route added once is
served under both words; not a redirect, because agents POST) and never goes away. The clients and
the onboarding text use `/pages` and teach `ream <verb>` first, mentioning that the `artifacts-*`
commands still work. Never changed: the `{id}` page addresses on the content domain, stored names
(cookies, `data-artifact-*` attributes, `<!--artifacts:updated-->` markers, table and event-kind
names), and the old news entries in `src/ream/news.py` (pinned by digest tests: add, never edit).

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
The one route scoped otherwise is recording a relay (`POST /decisions/{id}/record`): any key may
record what he said elsewhere on any agent's open plain question, because an ops seat often hears
the answer meant for another; the recording key is stored as `recorded_by`, never as his tap
(`tests/test_record_answer.py`). A signed-in app's relay stays within its reach and never replaces
his own tap.

A request is identified by its key, never by a name it claims. Renaming an agent
(`artifacts-admin rename-agent`) moves everything it owns, and its old key keeps working as the
new name; published pages never change owner by accident. Every revocation (`revoke-key`, the
Machines page, a rotation, `rename-agent --revoke-keys`) goes through `keys.revoke_in`, which
always records `revoked_by` and `revoked_reason` on the key row and, when the agent has a live
page, writes an `agent_key_revoked` event there (not for a routine weekly rotation); the 401 shows
the reason to whoever holds the key. A fold refuses a key used in the last 24 hours unless `--force`.

## WHAT

| Piece | Where |
|---|---|
| The service (FastAPI + Postgres) | `src/ream/` |
| The agent CLIs (`ream <verb>`; the old `artifacts-publish`, `artifacts-decide`, … too) | `src/ream/cli.py`, `client.py`, `decide.py` |
| Setting names, new and old (`REAM_X`, `ARTIFACTS_X`) | `src/ream/envnames.py` |
| The old import name `artifacts` (a shim; `artifacts.X` is the `ream.X` module) | `src/artifacts/__init__.py` |
| Admin commands (keys, scopes, renames) | `src/ream/admin.py` -> `artifacts-admin` |
| MCP at `/mcp` (transport, registries, the content fence; the tools, resources and prompts) | `src/ream/mcp.py`, `src/ream/mcp_tools.py`; trying it: `docs/mcp.md` |
| The owner: settings, the stored id, its start-up guard | `src/ream/settings.py`, `src/ream/owner.py` |
| The Postgres <-> SQLite copier and its verifier | `src/ream/migrate.py` |
| HTML parsing: the one parser base every reader of a page uses (a new reader subclasses it; `tests/test_htmlbound.py` refuses any other) | `src/ream/htmlbound.py` |
| The logo: its drawings, and the gallery's bar mark and tab icon cut from them | `src/ream/brand/`, `src/ream/logo.py` |
| Writing a plug-in (the `ream.plugins` interfaces, `REAM_PLUGINS`, failure rules) | `docs/plugins.md` |
| The logo guide and the gallery's named design values (colours, type, spacing) | `docs/brand.md`, `src/ream/design.py` |
| Runbooks (cutover, rollback, backups, restore drill, CI logs) | `docs/agent/runbooks/` |
| Tests | `tests/` |
| Container | `Dockerfile` |
| Deployment values | the deployment's own repo (for the homelab, the homelab repo); this repo keeps none |
| CI -> image -> pin PR | `.forgejo/workflows/ci.yml`, `.forgejo/scripts/pin-pr.sh`, `core-lock-pr.sh` |
| An embargoed security fix: built in the private `runcove-sec/artifacts-sec` and pinned by hand before it is public | `.forgejo/workflows/sec.yml` (on the dedicated runner labels only: `runcove-sec` for the image build, `runcove-sec-unpriv` for every other job), `.forgejo/scripts/sec-private-check.sh`, `scripts/guards/workflow-runner-guard.sh`; runbook `docs/agent/runbooks/embargoed-fix.md` |
| PyPI release (`ream-artifacts`, from the public GitHub mirror by trusted publishing; a release is a version bump on main) | `.github/workflows/publish.yml` (GitHub only; the forge runs `.forgejo/`; every job gated to `github.repository == 'tenfourty/ream'`, held by `workflow-runner-guard.sh`) |
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
REAM_TEST_DB=sqlite uv run pytest -q -n auto --maxprocesses 8      # the same suite on SQLite
```

Settings: every server variable is a field of `ream.settings.Settings`, read at call time with
`settings.current()` (tests: `settings.override(...)`, no caching, no `__repr__` that prints a value).
`scripts/guards/no-environ-guard.sh` refuses `os.environ` in `src/ream` outside `settings.py`,
`envnames.py` (the one place a setting is read under both its names), `serve.py`, `admin.py` and the
command-line tools. The shim package `src/artifacts/` reads no environment and may hold only `__init__.py` and `py.typed`; the same guard scans it and refuses any other file there. Nothing of a house is a default: a fresh install
(nothing set) serves `http://localhost:8000`, and a server bound to a non-loopback address
(`REAM_BIND_HOST`, set to `0.0.0.0` by the image) without `REAM_GALLERY_ORIGIN` refuses to
start. A deployment sets `REAM_GALLERY_ORIGIN`, `REAM_CONTENT_DOMAIN`, the owner trio and,
behind a proxy, `REAM_TRUSTED_IP_HEADER`.
`REAM_FORMER_HOST` (also `ARTIFACTS_FORMER_HOST`; unset: nothing changes) names the gallery's previous host
after a move: on it `/api/v1/*` (both words) and `/healthz` are served as on the gallery host, never redirected
(agents' clients refuse redirects), and every other path answers 308 to the same path and query on
`REAM_GALLERY_ORIGIN`; agents.txt, skill.md and agent.json there advertise only the gallery origin. It must
differ from the gallery host and `REAM_API_HOST`. Runbook: `docs/agent/runbooks/former-host.md`.
`REAM_PLUGINS` picks which installed `ream.plugins` (or old `artifacts.plugins`) entry points load at server start (unset: all;
`none`: none; `a,b`: only those, and an unknown name stops start-up); see `src/ream/plugin_api.py`.
`REAM_MCP_PUBLIC_URL` turns on `/mcp` (MCP over stateless HTTP, `src/ream/mcp.py`) on that origin, which must be
the gallery origin or `https://<REAM_API_HOST>` (unset: 404); `REAM_MCP_ACCEPT_KEYS` says whether it takes agent keys
(unset: yes while `REAM_MCP_SIGN_IN` is off, no once it is on; `1` takes them beside sign-in; `0` refuses to start unless sign-in is on). How to try it, and to connect a Claude Code session with its agent key (`ream mcp-headers` as the entry's `headersHelper`): `docs/mcp.md`.
`REAM_SINKS_FILE` (also `ARTIFACTS_SINKS_FILE`; unset: none) names a TOML file of `[[sink]]` signed-webhook sinks that
need no plug-in: format, signing and the addresses it refuses are in `docs/plugins.md` and `src/ream/webhook.py`.
`REAM_SCAN_SLOTS` (also `ARTIFACTS_SCAN_SLOTS`; unset: 4) is how many scans of a page over 256 KiB one server process runs at once
(`src/ream/scanslots.py`): the rest wait up to 5 seconds, then the publish checks and the render findings say they did not run and a reader
that a request needs answers 503 with `Retry-After`.

Tools update themselves (`src/ream/clientlevel.py`, `selfupdate.py`; decisions.md, 9 Nov 2026): every call sends
`X-Ream-Client: <level>`, `/api/v1` answers `X-Ream-Client-Min` and, for an older
tool, `-Outdated: true` (the server names no ref; tools install `main` of their own source). **Bump `CLIENT_LEVEL` and `MIN_CLIENT_LEVEL` by hand** in a commit that needs new tools. The tool installs
only from its own `REAM_INSTALL_SOURCE`, never from the server (with none set, from what uv's
receipt records: a git address, or the tools index it was installed from, reinstalled with the same
`--no-index --find-links`, never a bare package name); plug-ins installed with it (`--with`,
`--with-executables-from`) are read from the same receipt and passed back, and if one's source
cannot be repeated safely the tool does not update itself and prints the manual line;
`REAM_NO_SELF_UPDATE=1` turns it off.

**The install name is `ream-artifacts`** (decisions.md, 10 Nov 2026; `ream` on public PyPI is a
stranger's package; the commands and import names are unchanged). A tool whose receipt says `ream`
or `artifacts` hops to it on its next update, and `selfupdate._retire_old_tools` uninstalls the old
one. **The tools index must also carry a wheel under the old name `ream`**: a tool installed before
the rename runs the old self-update, which asks the index for `ream`, and with only `ream_artifacts-*`
there every such tool would stay stale for good. So the Dockerfile builds the wheel twice (the second
with only the distribution name swapped to `ream`, same version and code), `onboarding.wheels()` lists
both, and `/api/v1/tools/` and `/api/v1/tools/<name>` serve both (`tests/test_tools.py`). The documents
and install lines name only `ream-artifacts`. Do not drop the `ream` wheel while a tool with `ream` in
its receipt may exist.

The service runs on Postgres or SQLite, chosen by `DATABASE_URL` (`postgresql://…` or
`sqlite:////data/artifacts.db`). Only `src/ream/db.py` talks to a driver; SQL elsewhere must
read the same on both (`docs/agent/specs/2026-10-09-sqlite-design.md`). CI runs the suite on both.

The browser tests (comments panel, hover cards, thumbnails, shim placement) need a headless
chromium: `REAM_BROWSER=<path>`, or `chromium`/`chrome-headless-shell` on PATH; without one they
skip (`-rs` names them). CI installs Debian's chromium and runs those four files serially in their own
step of the `test-sqlite` job, which runs beside the Postgres suite's `test` job, with `REAM_REQUIRE_BROWSER=1` (the parallel runs hide the browser: `REAM_BROWSER=/nonexistent/browser`), which
makes a missing browser fail the run at start instead of skipping -- so a green CI run means they ran.

CI's `core-bare` job runs the whole suite on both databases with `REAM_PLUGINS=none` and no
plug-in installed (core stands alone); a plug-in's breaker state and failure counts are shared
through the `sink_breaker` and `plugin_failure` tables (`sinks.bind`).

**On the management machine, never run the test suite** (Jeremy, 13 Oct 2026: no test-suite runs there). Push the
change to a `ci/<name>` branch instead: CI runs the full suite on it and never builds an image or
deploys from it. Land on `main` once that run is green, then delete the branch.
Reading a run's log without a browser, safely: `docs/agent/runbooks/ci-logs.md`.

**Old names work for good** (docs/decisions.md, "Ream inside the code too"). The package is `ream`;
`import artifacts` and `artifacts.<module>` give the same module objects as `ream.<module>` (the
shim in `src/artifacts/`). Every setting is `REAM_<NAME>`, and `ARTIFACTS_<NAME>` is read as well
(`envnames.py`): set to the same value is fine, set to different values refuses to start, naming both
variables and no value. The `artifacts-*` commands, the `X-Artifacts-*` headers (both sets are
sent) and the `artifacts.plugins` entry-point group stay. Never renamed, because stored or
published: the SQLite `artifacts_now()`, cookie names, `data-artifact-*` attributes,
`<!--artifacts:updated-->` markers, table and column names, `service_meta` keys, event kinds. The
`artifacts-key` wrapper reads only `ARTIFACTS_SEAT`.

**Declare which session you are on every publish.** A key may be shared by several sessions;
without a label a page says only which KEY published it. `REAM_SESSION="<agent>/<session
ref>"` rides on the page, version, decision and events. A page published without it stays
unattributed until its next real change -- an identical republish mints no version.

**A push to `main` asks to deploy.** CI tests, builds an image and pushes it as
`runcove/artifacts-core:<sha12>`, then the `core-lock` job (`.forgejo/scripts/core-lock-pr.sh`, secret
`CORE_LOCK_TOKEN`) opens a PR on `runcove/ream-homelab` moving its `core.lock` to that image, set to merge when that
repo's checks succeed. ream-homelab builds the deployed image and opens the homelab pin PR, which the homelab maintainer
merges. Core's own `pin` job (`pin-pr.sh`) is off. How it flows, and rollback: `docs/agent/runbooks/image-from-ream-homelab.md`.

After changing a CLI, reinstall it where agents run it (`uv tool install <checkout> --force`).

**The owner is a setting** (`docs/agent/specs/2026-11-04-core-and-plugins-design.md`, 3.2). The one
person who reads the gallery is `REAM_OWNER_ID` (default `owner`; lower-case letters, digits,
hyphens), `REAM_OWNER_NAME` (default `the owner`) and `REAM_OWNER_PRONOUN` (`they`, `he` or
`she`; default `they`), read at call time through `settings.current()`; a test changes them with
`settings.override(...)`, and the suite runs as our deployment does (`jeremy`/`Jeremy`/`he`, set in
`tests/conftest.py`). The id is **stored** (`by`, `author`, `answered_by`, `actor`, a folder's owner),
so the first start records it in `service_meta` and every later start refuses a different one
(`owner.check`). A deployment whose rows say another id sets the variable to that id; to move a
database to a new one run `artifacts-admin change-owner <new-id> [--dry-run]`, then set the variable
and restart. No agent may be named the owner id. Do not write the id as a literal in code: read
`settings.owner_id()` (SQL takes it as a bound parameter).

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
| pre-commit | a secret in the staged diff (ggshield), or a scan that could not run (ggshield missing, no key, no network) | `GGSHIELD_SKIP=1` (logged as nothing scanned) |
| pre-commit | a credential handed to a process as an argument | none — fix the call |
| pre-commit | ruff and shellcheck findings in staged files | none |
| prepare-commit-msg | `--amend` / `-c` / `-C` / `--fixup` / `--squash` | `AMEND_OVERRIDE=1` |
| prepare-commit-msg | …of a commit already on a remote | **also** `AMEND_PUSHED_OVERRIDE=1` |
| commit-msg | a non-conventional subject; a `Co-Authored-By` or "Generated with Claude Code" line | `COMMIT_MSG_OVERRIDE=1` |
| pre-push | an outgoing range that is not the count you announced | none: `PUSH_COUNT=N git push` |
| pre-push | a push of a `sec/*` branch or tag, or of the commits of a local one under any name (`HEAD~0`, `@{-1}`, a raw id, a merge), to any remote that is not exactly `runcove-sec/artifacts-sec` (the embargoed-fix path, `docs/agent/runbooks/embargoed-fix.md`) | none |
| pre-push | ruff findings, or unformatted files, anywhere in `src`/`tests` | none — `uv run ruff format <files>` |
| CI selftest step (`run-selftests.sh`); run it by hand before pushing a workflow change | in `.forgejo/workflows`, a job of `sec.yml` not on exactly `runs-on: runcove-sec` (the `image` job) or `runs-on: runcove-sec-unpriv` (every other job), or any other file's job without `github.repository == 'runcove/artifacts'` as a top-level `&&` conjunct of its `if:` or on the `runcove-sec` label; in `.github/workflows` (absent is fine), any job without `github.repository == 'tenfourty/ream'` (the public GitHub mirror) as such a conjunct, or on `runcove-sec` (the private copy's secrets must never reach a shared runner: `workflow-runner-guard.sh`) | none — fix the workflow; it fails closed, also on a missing `.forgejo/workflows` or PyYAML |

The secret scan is the exception to "degrade to a SKIP": it refuses when ggshield is missing or
cannot scan, and only `GGSHIELD_SKIP=1` lets that through, saying loudly that nothing was scanned. It
runs ggshield with `GITGUARDIAN_DONT_LOAD_ENV=1`, `--config-path` on an empty temporary file and `--instance` on the default dashboard, so the
scan uses only its own fixed settings. It skips only on exactly `GGSHIELD_SKIP=1`, and refuses a `ggshield` that resolves inside the working tree. The other guards but `workflow-runner-guard.sh` (which fails closed) still degrade to a
clean SKIP when their tool is missing, saying so loudly. Each has a `--selftest`; CI runs all of them, plus the whole-tree scan for credentials on a command line, on every
push. CI does not run ggshield. lefthook's own skips (`LEFTHOOK=0`, `git commit --no-verify`) are outside the gate: nothing
in this repo stops them, so the secret scan runs only where the hook does.

What is NOT mechanical, and still yours to hold:

- Stage explicit paths; commit with a pathspec. `git commit -- <paths>` commits the **working
  tree** of those paths, so read `git status --porcelain -- <paths>` first.
- Never `git stash` in a tree another session might share.
- **`git reset --hard` discards uncommitted work in tracked files.** Prefer `git checkout <ref>`
  or reset a single path.
- Announce a push, with hashes, before making it, and verify what actually went out against the
  remote (`git merge-base --is-ancestor <hash> origin/main` after a fetch). `PUSH_COUNT`
  mechanises the count, not the hashes, and a rebase keeps the count and changes every hash.
