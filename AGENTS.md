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
| View counts: who read a page, per day and per viewer (`record`, best-effort, emits no event; read back by `report` through `GET /api/v1/pages/{id}/stats`, the MCP `page_stats` tool and `ream stats`); the design is `docs/agent/specs/2026-11-22-view-stats-design.md` | `src/ream/pageviews.py` |
| Rendering a page's picture: the browser runner, the wire and its checks, the renderer as its own process (`python -m ream.renderd`), and the store-facing background render; a screenshot on request (`rendercore.shoot`, over the browser's DevTools pipe; `/v1/shot` on the renderer), and an agent's screenshot of a page it can read (`shots.take`, served at `GET /api/v1/pages/{id}/screenshot`) | `src/ream/rendercore.py`, `renderwire.py`, `renderd.py`, `devtools.py`, `thumbs.py`, `shots.py`; running it apart: `docs/agent/runbooks/renderer-pod.md` |
| A page asking for a window-high frame (`<meta name="ream-frame" content="viewport">`): the name and value, read by the shim, the gallery's frame and the screenshot | `src/ream/pageframe.py` |
| HTML parsing: the one parser base every reader of a page uses (a new reader subclasses it; `tests/test_htmlbound.py` refuses any other) | `src/ream/htmlbound.py` |
| The logo: its drawings, and the gallery's bar mark and tab icon cut from them | `src/ream/brand/`, `src/ream/logo.py` |
| Writing a plug-in (the `ream.plugins` interfaces, `REAM_PLUGINS`, failure rules) | `docs/plugins.md` |
| The logo guide and the gallery's named design values (colours, type, spacing) | `docs/brand.md`, `src/ream/design.py` |
| Runbooks (cutover, rollback, backups, restore drill, CI logs) | `docs/agent/runbooks/` |
| Tests | `tests/` |
| Container (one Dockerfile, two targets: `full` has the browser and is the last stage, so a plain `docker build .` renders in-process; `app` has none and needs a renderer) | `Dockerfile`; `tests/test_dockerfile_targets.py` |
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
`REAM_INTERNAL_PORT` (also `ARTIFACTS_INTERNAL_PORT`; unset: no second listener, nothing changes) opens a second listener
on `REAM_BIND_HOST` that serves only `GET /healthz` and the partner inbound endpoint (on the partner host, as on the main
port); everything else there, whatever the Host header, is a plain 404. It is its own ASGI app that holds no gallery, API,
content or MCP app (`src/ream/internal_port.py`), so the health check and partner events get their own listener, which
serves nothing else. A whole number from 1 to 65535, not 8000 (the image's port); a port that cannot be bound stops start-up.
The image's CMD is unchanged. Tests: `tests/test_internal_port.py`.
`REAM_PLUGINS` picks which installed `ream.plugins` (or old `artifacts.plugins`) entry points load at server start (unset: all;
`none`: none; `a,b`: only those, and an unknown name stops start-up); see `src/ream/plugin_api.py`.
`REAM_MCP_PUBLIC_URL` turns on `/mcp` (MCP over stateless HTTP, `src/ream/mcp.py`) on that origin, which must be
the gallery origin or `https://<REAM_API_HOST>` (unset: 404); `REAM_MCP_ACCEPT_KEYS` says whether it takes agent keys
(unset: yes while `REAM_MCP_SIGN_IN` is off, no once it is on; `1` takes them beside sign-in; `0` refuses to start unless sign-in is on). How to try it, and to connect a Claude Code session with its agent key (`ream mcp-headers` as the entry's `headersHelper`): `docs/mcp.md`.
`REAM_SINKS_FILE` (also `ARTIFACTS_SINKS_FILE`; unset: none) names a TOML file of `[[sink]]` signed-webhook sinks that
need no plug-in: format, signing and the addresses it refuses are in `docs/plugins.md` and `src/ream/webhook.py`.
`REAM_SCAN_SLOTS` (also `ARTIFACTS_SCAN_SLOTS`; unset: 4) is how many scans of a page over 256 KiB one server process runs at once
(`src/ream/scanslots.py`): the rest wait up to 5 seconds, then the secret scan refuses the request (503 with `Retry-After`, nothing stored), the publish checks and the render findings say they did not run, the comment
listings answer 200 with the pins of the pages they could not read unknown (`anchor_state` "", and `quote_state` "" too unless the page's words were already read), and a reader
that a request needs answers 503 with `Retry-After`. The secret scan also has a time bound once it holds its slot
(`scan.SCAN_CPU_SECONDS`, 10 s of the scanning thread's own CPU time, checked before each shape and at each match; plus a wall-clock backstop of `SCAN_WALL_SECONDS_PER_SLOT` times the slot count, a slot-hold limit, acceptable only because a scan that can take a slot (a value over `SMALL_PAGE`, today a page body alone) is reachable only with a revocable credential, and every other scanned field is capped at 64 KiB or less before its scan; `tests/test_scan_call_sites.py` fails for a scan site no one has classified, and the backstop must become a per-scan wall bound if a slot-taking scan ever becomes reachable without such a credential): past either the scan raises `ScanTimeout`, a `ScanBusy`, and is refused the same way.
`REAM_RENDERER_URL` (also `ARTIFACTS_RENDERER_URL`; unset: nothing changes, the browser runs in the
service's own process as on a fresh install, in an image that has one: `full`, a plain `docker build .`) names a renderer running as its own process or pod, for resource
isolation: `python -m ream.renderd --port 8091`, from a `full` image (`runcove/artifacts-renderer`; the deployed service's
`app` image has no browser, so renderd cannot run from it). It must be `http://host:port` with an explicit port and
nothing else (no path, query, fragment or credentials), on a host other than the gallery's, `REAM_API_HOST` and
`REAM_FORMER_HOST`, or start-up refuses. Set, `thumbs.render_report` sends the stamped page there through
`renderwire.call` and never starts a browser itself (`REAM_BROWSER` is not read); a renderer that gives no usable
answer (down, slow past 30 s, busy, or an answer failing `renderwire`'s checks) stores the verdict `unchecked` with
one finding saying so, and is never replaced by a render in the service's own process. Preview answers 200 as
before, with `reason: "renderer_unavailable"` then. renderd takes no key: who may call it is the deployment's
network policy. Runbook: `docs/agent/runbooks/renderer-pod.md`. With neither it nor a browser found (an `app` image, no renderer named), start-up logs one WARNING that thumbnails, render checks and screenshots are off (`serve._warn_renderer_off`).

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
skip (`-rs` names them). CI installs Debian's chromium and runs the browser test files (those named in the
`browser tests` step of `.forgejo/workflows/ci.yml`, and of `sec.yml`) serially in their own
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

**A push to `main` asks to deploy.** CI tests, builds the `app` image (no browser) and pushes it as
`runcove/artifacts-core:<sha12>`, and the `full` image (the browser) as `runcove/artifacts:<sha12>` and
`runcove/artifacts-renderer:<sha12>`, and each of the three is also pushed as `runcove/ream:<sha12>`, `runcove/ream-core:<sha12>`
and `runcove/ream-renderer:<sha12>` with the same digest (the job fails if a Ream name's digest differs; the old names stay what the job outputs
and `core-lock` reads, until the contract step); the core and renderer digests differ: CI refuses a core digest equal to the full (renderer) one, and
ream-homelab's pin (`pin-pr.sh --check-renderer-lock`) is to refuse a `renderer.lock` digest equal to `core.lock`'s (that check is
added in ream-homelab, separately from this repo's change). Then the `core-lock` job (`.forgejo/scripts/core-lock-pr.sh`, secret
`CORE_LOCK_TOKEN`) opens a PR on `runcove/ream-homelab` moving its `core.lock` to that image (and `renderer.lock` to `runcove/artifacts-renderer:<sha12>` when that image was published), set to merge when that
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
| pre-commit | a secret in the staged change (ggshield), or a scan that could not run or did not provably finish (ggshield missing, no key, no network, a report that is not complete) | `GGSHIELD_SKIP=1` on your own machine (logged as nothing scanned); refused where `CI`, `FORGEJO_ACTIONS` or `GITHUB_ACTIONS` is set |
| pre-commit | a credential handed to a process as an argument | none — fix the call |
| pre-commit | ruff and shellcheck findings in staged files | none |
| prepare-commit-msg | `--amend` / `-c` / `-C` / `--fixup` / `--squash` | `AMEND_OVERRIDE=1` |
| prepare-commit-msg | …of a commit already on a remote | **also** `AMEND_PUSHED_OVERRIDE=1` |
| commit-msg | a non-conventional subject; a `Co-Authored-By` or "Generated with Claude Code" line | `COMMIT_MSG_OVERRIDE=1` |
| pre-push | an outgoing range that is not the count you announced | none: `PUSH_COUNT=N git push` |
| pre-push | a push of a `sec/*` branch or tag, or of the commits of a local one under any name (`HEAD~0`, `@{-1}`, a raw id, a merge), to any remote that is not exactly `runcove-sec/artifacts-sec` (the embargoed-fix path, `docs/agent/runbooks/embargoed-fix.md`) | none |
| pre-push | ruff findings, or unformatted files, anywhere in `src`/`tests` | none — `uv run ruff format <files>` |
| CI selftest step (`run-selftests.sh`); run it by hand before pushing a workflow change | in `.forgejo/workflows`, a job of `sec.yml` not on exactly `runs-on: runcove-sec` (the `image` job) or `runs-on: runcove-sec-unpriv` (every other job), or any other file's job without `github.repository == 'runcove/artifacts'` as the FIRST top-level `&&` conjunct of its `if:` (the `if:` is read exactly as written: a value with `${{` must be exactly `${{ ... }}` with nothing before or after it, a bare one has no leading or trailing whitespace, `[`, `]`, `{` or `}` outside a string literal is refused, `${{` and `}}` are refused even inside a string literal, and the whole `if:` must parse with the evaluator's lexer (only space, tab, newline and carriage return as whitespace outside a string literal; a word is letters, digits and `_ - . *`; a number is exactly the decimal form `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE]-?(0|[1-9][0-9]*))?` or the hex form `-?0x(0|[1-9a-fA-F][0-9a-fA-F]*)`, an integer fits 32 bits and a float is finite; any control character in 0x00-0x1F or 0x7F (C1 controls are not checked, as the lexer accepts them) but tab, newline and carriage return is refused anywhere, a string literal included; more than 50 nested parentheses is refused) and grammar) (during the forge repo's rename to `runcove/ream`, step 2 of 9, the conjunct may also be `github.repository == 'runcove/ream'` or a parenthesised `||` of only those two equality tests, `FORGE_REPO` in the guard; contracted back to `runcove/ream` alone after) or on the `runcove-sec` label; in `.github/workflows` (absent is fine), any job without `github.repository == 'tenfourty/ream'` (the public GitHub mirror) as such a first conjunct, read the same way, or on `runcove-sec` (the private copy's secrets must never reach a shared runner: `workflow-runner-guard.sh`) | none — fix the workflow; it fails closed, also on a missing `.forgejo/workflows` or PyYAML |

The secret scan is the exception to "degrade to a SKIP": it refuses when ggshield is missing or
cannot scan, and only `GGSHIELD_SKIP=1` lets that through, saying loudly that nothing was scanned; that switch is for a
person's own machine and is refused where `CI`, `FORGEJO_ACTIONS` or `GITHUB_ACTIONS` is set. A scan passes on its report, not on
ggshield's exit status alone (`scripts/guards/ggshield-judge.py`, run with `python3 -I`): exit 0, a JSON report that parses and holds
no errors entry, the scanned commit listed in it once, and no error line in ggshield's other output; anything less refuses. Because
ggshield's pre-commit scan has no JSON report, the gate scans the staged change as one commit made from a copy of the index (no ref
moved, the index untouched). It
runs ggshield with `GITGUARDIAN_DONT_LOAD_ENV=1`, `--config-path` on an empty temporary file and `--instance` on the default dashboard, so the
scan uses only its own fixed settings. It skips only on exactly `GGSHIELD_SKIP=1`, and refuses a `ggshield`, `python3` or `git` that resolves inside the working tree. It is started as `/bin/bash -p`, and before it runs any helper it refuses a `PATH` entry that is empty, relative or inside the working tree (a missing one is read through its nearest existing parent, resolved physically; a dangling link in it refuses), a `GIT_DIR` or `GIT_WORK_TREE` that does not name the repository it runs in, and a control character (a newline) in the directory it runs in, in `PATH`, in `GIT_DIR`, in `GIT_WORK_TREE` or in the `.git` file; its own `git` calls and ggshield's run with `core.fsmonitor` off. ggshield reads the commit with `git show`, where textconv and the `diff` attribute apply, and the gate's own `write-tree` runs a filter driver, so before those it refuses any `diff.<driver>.textconv`, `.command` or `.binary`, `filter.<driver>.clean`, `.smudge` or `.process`, or `attr.tree`, read with `--includes` as its own git and as ggshield's git read them, whether or not an attribute uses it today (attributes can come from a staged `.gitattributes`, `info/attributes`, `core.attributesFile` or the XDG file; git-lfs's `filter.lfs.clean`, `.smudge`, `.process` and `.required` are accepted only at exactly their stock values (`git-lfs clean -- %f`, `git-lfs smudge -- %f`, `git-lfs filter-process`, `true`; an identical value repeated across files is fine, a differing one is refused); any other `filter.lfs.*` key or value, any `lfs.extension.*`, or any other filter driver is refused; because that driver is let through, a changed path with a `filter` attribute set to anything is refused, from any source, read from the working tree and from the index alone, and so is a changed path whose new blob is a git-lfs pointer (at most 1024 bytes), because the content behind a pointer never reaches the scan (read as git-lfs's decoder reads it: whitespace trimmed, CR at line ends cut off, the `git-lfs`, hawser or `git-media` version line first; `skip_lfs: true` in `lefthook.yml` is only the second wall, since a personal `lefthook-local.yml` can turn it off and a manual `git lfs push` uploads whatever `.git/lfs/objects` holds), and it refuses a changed file whose `diff` attribute is unset (`-diff`, `binary`). For ggshield's git it unsets `GG_GIT_CONFIG`, `GIT_ATTR_SOURCE`, `GIT_EXTERNAL_DIFF` and `GIT_DIFF_OPTS`, ignores replace refs, grafts, notes and the commit-graph, reads pathspecs literally, runs from the top of the tree (ggshield names paths from the top but runs git where it runs), and pins the settings measured to change its patch (`SCAN_PINS` in the script, `log.showRoot` among them); `GIT_CONFIG` and `GIT_SHALLOW_FILE` are dropped for every git call, and it refuses when the shallow list names the commit made for the scan or when that commit's stored object does not hash to its id (its id can be predicted), or when an object on HEAD's side of the change does not: HEAD's commit, its tree and every tree and blob the comparison reads on that side (git does not check those ids when it compares; a file added and a submodule's id have none to check, and the scanned commit's own tree and blobs are not hashed). For a scan that would pass, the settings, attributes, shallow list and those objects are read again after it and any change refuses (a change made and undone during the scan is not seen). Only the settings and variables measured are covered, not every one git has; the header lists them. It also trusts, as limits, `GIT_INDEX_FILE` (git exports it in a pre-commit hook) and a `core.worktree` in `.git/config`. What still runs before it, and is not checked, is lefthook's own `sh`, the interpreter path (`/bin/bash`) and the environment the gate inherits (it drops `BASH_ENV`, `ENV` and the `PYTHON*` start-up variables for ggshield, not the proxy or CA ones). It also refuses when the index cannot be found, or when the scanned tree is not exactly what is staged (an intent-to-add entry aside). It refuses, too, when one of git's listings of the change does not finish within 120 s; `GGSHIELD_LIST_TIMEOUT` can only lower that bound, never raise it. The other guards but `workflow-runner-guard.sh` (which fails closed) still degrade to a
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
