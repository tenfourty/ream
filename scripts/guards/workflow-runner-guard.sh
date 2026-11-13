#!/usr/bin/env bash
# scripts/guards/workflow-runner-guard.sh — refuse a Forgejo or GitHub workflow that could put a job
# from the PRIVATE copy of this repo onto a runner not dedicated to it, or a public job onto the dedicated ones.
#
# WHY. The same code lives in the public repo runcove/artifacts and in a private copy,
# runcove-sec/artifacts-sec, where an embargoed security fix is built (docs/agent/runbooks/
# embargoed-fix.md). The private copy has repo secrets (SEC_*), so its jobs run only on runners
# dedicated to it, and the public copy's jobs never do. The rules below keep it so:
#
#   1. sec.yml, the private copy's own workflow: the job `image` says `runs-on: runcove-sec` and
#      every other job `runs-on: runcove-sec-unpriv`, EXACTLY (a plain string; no other label, no
#      list, no expression), labels only the runners dedicated to that repo carry (runcove-sec is
#      the privileged one, for the image build only).
#   2. every OTHER workflow file (ci.yml and any added later): every job has a job-level `if:`
#      with `github.repository == 'runcove/artifacts'` as one of its top-level `&&` conjuncts.
#      A job whose `if:` is false is never dispatched to a runner, so in the private copy these
#      jobs never leave the forge. The gate is a conjunct, not just present: `gate || true`,
#      `!gate`, `(gate || x) && y` and the literal inside a string are all refused. And no job of
#      such a file has `runs-on` naming runcove-sec (or an expression, which could): the public
#      repo's jobs must not claim the dedicated runner.
#   A job-level `if:` that is not a string (`if: false`) is refused too: write the gate and then
#   `&& false`. A job with a `uses:` (a called workflow) is refused: what it runs is not on this page.
#   3. .github/workflows (absent is fine): the same as rule 2, except that the gate is
#      `github.repository == 'tenfourty/ream'`, the public GitHub mirror that publish.yml runs on and
#      that PyPI's trusted publisher is pinned to (gating on runcove/artifacts would stop publishing
#      there). No file in it is special (a sec.yml there is a public file), none may name runcove-sec.
#      Forgejo reads this folder only when .forgejo/workflows does not exist, and a forge setting or
#      version change could change that; the gate keeps its jobs out of the private copy either way.
#   4. No workflow file, in either directory, and no local action (action.yml or action.yaml
#      anywhere in the tree but .git), names SEC_RUNNER_NAME outside a comment (an env key at workflow, job
#      or step level, a step writing it to $GITHUB_ENV, any letter case): sec-private-check.sh reads
#      it as the name of the runner that took the job, and only the dedicated runner's own config
#      may set it. sec-private-check.sh also checks the name against the organization's runner list.
#   5. sec.yml's first job is `runner-guard`, which runs this guard on the real tree; it needs
#      nothing and names no secret, every other job waits for it through `needs:`, and a job that
#      names SEC_CHECK_TOKEN needs it directly (check_sec_order). A refusal must stop the run, so:
#      no job or step `if:` in sec.yml names a status function (success, always, failure,
#      cancelled, in any case or spacing); no job or step anywhere in sec.yml has continue-on-error
#      (any value); the guard job has no `if:` but the repository gate, no keys but name, runs-on,
#      timeout-minutes, if and steps, and exactly two steps: actions/checkout pinned to a commit
#      (name, uses and with: persist-credentials: false only), then the guard step (name and run only), whose run is exactly this
#      guard with --pinned-python. sec.yml's triggers are only push and workflow_dispatch, its
#      concurrency is exactly group sec-pipeline with cancel-in-progress false, its defaults are
#      at most run.shell: bash, and its workflow-level env sets only SEC_REGISTRY_OWNER and
#      SEC_REGISTRY_USER. Every `sec-private-check.sh repo` call passes, as its runner label, exactly
#      its job's runs-on.
#   6. sec.yml only: every step `uses:` is owner/repo@<40 hex commit> or docker://...@sha256:<digest>
#      (no tag, branch or local `./` action); every container and service image carries an
#      @sha256: digest; and, in every job but `image`, a container or service sets only
#      allow-listed `options:` flags (health checks and resource limits, no templating) and no
#      `volumes:` or `ports:`; a job's `container:` mapping (in every job, `image` too) holds only the
#      keys image, env and options, written in lower case. `services:` is allowed only where sec.yml
#      uses it today: SEC_SERVICES below (test, test-sqlite and core-bare: postgres; image: dind). On
#      any other job the key is refused in any letter case (even an empty mapping), the private
#      workflow's other jobs start no service containers; on those jobs the key is exactly
#      `services`, a mapping of only those service names, and each entry a mapping holding only the
#      keys image, env and options, in lower case (no ports, volumes, credentials, entrypoint, cmd),
#      in `image` too. `--privileged` is allowed in exactly one place: the `dind` service of the
#      `image` job at the digest DIND_IMAGE; every other container or service, in every job, is held
#      to the allow-listed health and limit flags. The two env values SEC_REGISTRY_OWNER (exactly runcove-sec) and SEC_REGISTRY_USER (a plain login)
#      are checked, and no job or step sets them again.
#
# FAIL CLOSED. Anything this cannot prove is a refusal: a missing .forgejo/workflows directory,
# one with no workflow in it, a file that does not parse, a workflow with a duplicate key, a merge
# key (<<), an anchor or alias, a second document, an explicit YAML tag (`!!str`, `!local`, `!`,
# on a key or a value), or a mapping key that is not a string (the plain `on` key aside, which
# PyYAML reads as true) (Forgejo's parser and this one must read it the same way), a symlink, or
# no jobs at all. A missing PyYAML is a failure, not a skip. Why the directory matters: Forgejo
# reads .github/workflows ONLY when .forgejo/workflows does not exist. This repo's
# .github/workflows/publish.yml (the GitHub mirror's PyPI release) is
# ignored while .forgejo/workflows exists; if that directory were ever renamed or emptied of its
# files, Forgejo would fall back to publish.yml. Rule 3 keeps it out of the private copy.
#
# HOW. Parsed with PyYAML (`python3` if it has it, else `uv run --with pyyaml`; CI's selftest step
# has uv). The `if:` is read by a small tokenizer for the expression syntax (single-quoted strings
# with '' as the escape, parentheses, && || ! == != < > <= >=), not by a regex: an `if:` that
# is wrapped in ${{ }} only as a whole is unwrapped, one that mixes ${{ }} with other text is refused.
# Workflow files are found recursively (*.yml, *.yaml in any letter case); `sec.yml` is the file
# at the top of the directory with exactly that name, so `sec.yaml` or `x/sec.yml` is a public file.
#
# Five invocations:
#   scripts/guards/workflow-runner-guard.sh                       # the real tree: both workflow directories, sec.yml's order, local actions
#   scripts/guards/workflow-runner-guard.sh --pinned-python       # the same, with its own pinned Python (sec.yml's guard job)
#   scripts/guards/workflow-runner-guard.sh --dir <dir>           # another directory of Forgejo workflow files
#   scripts/guards/workflow-runner-guard.sh --github-dir <dir>    # another directory of GitHub workflow files
#   scripts/guards/workflow-runner-guard.sh --selftest
# One more mode, for the test jobs of sec.yml: --install-uv <bin dir> puts the pinned uv there.
# It runs from scripts/guards/run-selftests.sh (CI's `guard selftests` step in ci.yml AND sec.yml).
# Run it before pushing a change to a workflow file.
#
# Exit codes: 0 ok, 1 refused (naming every file and job), 2 --selftest failed, 3 usage or cannot run.
set -uo pipefail
SELF="workflow-runner-guard"
FORGE_REPO="runcove/artifacts"  # the gate of .forgejo/workflows
GITHUB_REPO="tenfourty/ream"    # the gate of .github/workflows: the public GitHub mirror (publish.yml)
USAGE="usage: $0 [--pinned-python | --dir <dir> | --github-dir <dir> | --selftest | --install-uv <bin dir> | --help]"
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." >/dev/null 2>&1 && pwd)

PY=()
# pick_python — find a python3 that can import yaml; else uv with pyyaml. Sets PY, or returns 1.
pick_python() {
  if python3 -I -c 'import yaml' >/dev/null 2>&1; then
    PY=(python3 -I)
    return 0
  fi
  if command -v uv >/dev/null 2>&1 &&
    uv run --no-project --quiet --with 'pyyaml==6.0.3' python -I -c 'import yaml' >/dev/null 2>&1; then
    PY=(uv run --no-project --quiet --with 'pyyaml==6.0.3' python -I)
    return 0
  fi
  return 1
}

# The guard job in sec.yml runs `--pinned-python`: before reading anything it builds its own
# Python with PyYAML in a fresh temporary directory, from pinned downloads each checked against a
# hash written here, and uses only that. Nothing is installed into ~/.local/bin or PATH.
#   uv: the release tarball of UV_VERSION for this machine's architecture, checked against the
#     sha256 the release publishes beside it (uv-<arch>-unknown-linux-gnu.tar.gz.sha256);
#   Python: uv's own managed CPython GUARD_PYTHON, which uv checks against the hashes it carries;
#   PyYAML: one version from PyPI, wheels only, with --require-hashes (the cp312 Linux wheels).
UV_VERSION="0.13.5"
UV_SHA256_X86_64="f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f"
UV_SHA256_AARCH64="4bb69cf95ed13f23713bb7bc09f241896ea0c75b7c557e615a20604b82f9c403"
GUARD_PYTHON="3.12"
PYYAML_REQUIREMENT="pyyaml==6.0.3 \\
  --hash=sha256:ba1cc08a7ccde2d2ec775841541641e4548226580ab850948cbfda66a1befcdc \\
  --hash=sha256:9149cad251584d5fb4981be1ecde53a1ca46c891a79788c0df828d2f166bda28 \\
  --hash=sha256:41715c910c881bc081f1e8872880d3c650acf13dfa8214bad49ed4cede7c34ea \\
  --hash=sha256:8dc52c23056b9ddd46818a57b78404882310fb473d63f17b07d5c40421e47f8e"

# fetch_uv <dir> — download the pinned uv release for this machine into <dir>/uv (uv, uvx), checked
# against the sha256 above. Sets UV_BIN. rc 0 ok, 3 cannot.
fetch_uv() {
  local d=$1 arch sum
  case "$(uname -m)" in
    x86_64 | amd64) arch=x86_64 sum=$UV_SHA256_X86_64 ;;
    aarch64 | arm64) arch=aarch64 sum=$UV_SHA256_AARCH64 ;;
    *) echo "$SELF: REFUSED — no pinned uv for the architecture $(uname -m)" >&2; return 3 ;;
  esac
  if ! curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$d/uv.tar.gz" \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$arch-unknown-linux-gnu.tar.gz"; then
    echo "$SELF: REFUSED — could not download uv $UV_VERSION" >&2
    return 3
  fi
  if ! printf '%s  %s\n' "$sum" "$d/uv.tar.gz" | sha256sum -c --status -; then
    echo "$SELF: REFUSED — the uv $UV_VERSION download does not match its pinned sha256" >&2
    return 3
  fi
  mkdir "$d/uv" && tar -xzf "$d/uv.tar.gz" -C "$d/uv" --strip-components=1 || return 3
  UV_BIN="$d/uv/uv"
}

# install_uv <bin dir> — put the pinned uv and uvx into <bin dir> (the test jobs of sec.yml use it
# in place of a piped installer).
install_uv() {
  local t
  [ -n "${1:-}" ] || { echo "$USAGE" >&2; return 3; }
  t=$(mktemp -d) || return 3
  if ! fetch_uv "$t"; then rm -rf "$t"; return 3; fi
  mkdir -p "$1" && install "$t/uv/uv" "$t/uv/uvx" "$1/"
  local rc=$?
  rm -rf "$t"
  return "$rc"
}

# pinned_python — build the guard's Python as above in a new temporary directory (removed on exit)
# and set PY to it. rc 0 ok, 3 cannot (any download, hash or install failure).
pinned_python() {
  local d uv
  d=$(mktemp -d) || return 3
  PIN_DIR=$d # global, so the EXIT trap still sees it
  trap 'rm -rf "$PIN_DIR"' EXIT
  fetch_uv "$d" || return 3
  uv=$UV_BIN
  printf '%s\n' "$PYYAML_REQUIREMENT" >"$d/requirements.txt"
  mkdir "$d/home" || return 3
  # A clean environment: no UV_* or PIP_* setting from outside, every uv path inside $d.
  local -a envv=(env -i "PATH=/usr/local/bin:/usr/bin:/bin" "HOME=$d/home"
    "UV_NO_CONFIG=1" "UV_CACHE_DIR=$d/cache" "UV_PYTHON_INSTALL_DIR=$d/python"
    "UV_PYTHON_BIN_DIR=$d/python-bin" "UV_TOOL_DIR=$d/tools" "UV_TOOL_BIN_DIR=$d/tools-bin")
  if ! "${envv[@]}" "$uv" venv --quiet --python-preference only-managed --python "$GUARD_PYTHON" "$d/venv" ||
    ! "${envv[@]}" "$uv" pip install --quiet --python "$d/venv/bin/python" --require-hashes \
      --only-binary :all: --index-url https://pypi.org/simple -r "$d/requirements.txt"; then
    echo "$SELF: REFUSED — could not build the pinned Python with PyYAML" >&2
    return 3
  fi
  PY=("$d/venv/bin/python" -I)
  "${PY[@]}" -c 'import yaml' >/dev/null 2>&1 || {
    echo "$SELF: REFUSED — the pinned Python cannot import yaml" >&2
    return 3
  }
}

# check_dir <dir> [forgejo|github] — prints a refusal per problem and a final count line; rc 0 clean,
# 1 refused, 3 cannot run (no PyYAML). forgejo (the default): sec.yml and the gate runcove/artifacts,
# the directory must exist and hold a workflow. github: the gate tenfourty/ream, no special file,
# and a missing directory or one with no workflow is fine.
check_dir() {
  local kind=${2:-forgejo} repo
  case "$kind" in
    forgejo) repo=$FORGE_REPO ;;
    github) repo=$GITHUB_REPO ;;
    *) echo "$SELF: unknown kind $kind" >&2; return 3 ;;
  esac
  if [ "${#PY[@]}" -eq 0 ] && ! pick_python; then
    echo "$SELF: REFUSED — no PyYAML: neither python3 nor uv (with pyyaml) can parse the workflows, and an unchecked workflow is a refused one" >&2
    return 3
  fi
  "${PY[@]}" - "$1" "$kind" "$repo" <<'PYEOF'
import pathlib
import re
import sys

import yaml

SELF = "workflow-runner-guard"
KIND = sys.argv[2]  # "forgejo" or "github"
GATE_REPO = sys.argv[3]
SEC_FILE = "sec.yml" if KIND == "forgejo" else None  # in .github/workflows no file is special
SEC_LABEL = "runcove-sec"  # the privileged dedicated runner: the image job only
SEC_UNPRIV = "runcove-sec-unpriv"  # every other job of sec.yml
SEC_IMAGE_JOB = "image"
GATE = [("w", "github.repository"), ("op", "=="), ("str", GATE_REPO)]
MERGE_TAG = "tag:yaml.org,2002:merge"

problems = []


def refuse(where, why):
    problems.append(f"{SELF}: REFUSED {where}: {why}")


class KeyNotString(yaml.YAMLError):
    """A mapping key that does not load as a string (see Loader.construct_mapping)."""


class Loader(yaml.SafeLoader):
    """SafeLoader that refuses what a second parser could read differently."""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == MERGE_TAG:
                raise yaml.YAMLError("a merge key (<<) is not allowed")
            key = self.construct_object(key_node, deep=True)
            # Every mapping key must load as a string, so each rule below that looks a key up by
            # its name sees every key. The one exception is the plain key `on`, which this
            # YAML 1.1 parser reads as the boolean true (and the runner reads as the string "on").
            plain_on = (
                key is True and isinstance(key_node, yaml.ScalarNode)
                and key_node.value == "on" and key_node.style is None
            )
            if not isinstance(key, str) and not plain_on:
                raise KeyNotString(
                    f"line {key_node.start_mark.line + 1}: a mapping key that is not a string "
                    f"(a {type(key).__name__}): workflow files may use only string mapping keys [rule: string keys]"
                )
            if key in seen:
                raise yaml.YAMLError(f"duplicate key {key!r}")
            seen.add(key)
        return super().construct_mapping(node, deep)


USES_SHA = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}")
USES_DIGEST = re.compile(r"docker://[^\s@]+@sha256:[0-9a-f]{64}")
IMAGE_DIGEST = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
# The only `options:` flags a container or service may carry outside `image` (sec.yml): health
# checks and resource limits. Anything else, and any `${{ }}`, is refused.
ALLOWED_OPTIONS = {
    "--health-cmd", "--health-interval", "--health-timeout", "--health-retries", "--health-start-period",
    "--cpus", "--memory", "--memory-swap", "--pids-limit", "--shm-size",
}


# The only keys a job's `container:` mapping may hold, exactly as written (a mis-cased key is
# refused, not read: `OPTIONS` is not checked as `options`). Any job of sec.yml, `image` included.
CONTAINER_KEYS = ("image", "env", "options")
# The one image that may run with `--privileged`: the docker-in-docker service of the `image` job.
# Moving its digest is a change here as well as in sec.yml (the guard's selftest says which is stale).
DIND_IMAGE = "docker:28-dind@sha256:2a232a42256f70d78e3cc5d2b5d6b3276710a0de0596c145f627ecfae90282ac"
# The only jobs of sec.yml that start service containers, and the service names each may start. A
# service entry holds the same keys as a `container:` mapping. Adding a service is a change to this table.
SEC_SERVICES = {
    "test": ("postgres",),
    "test-sqlite": ("postgres",),
    "core-bare": ("postgres",),
    "image": ("dind",),
}
OPTION_VALUE = re.compile(r"[A-Za-z0-9._:/+-]+")


def option_problems(opts):
    """Why a container/service `options:` value is refused, or []."""
    if opts is None:
        return []
    if not isinstance(opts, str):
        return [f"`options` is {opts!r}, not a string"]
    # Each refusal names the rule that fired, so a selftest case can pin each rule on its own.
    if "${{" in opts:
        return ["`options` is templated (an expression could add any option) [rule: no templating]"]
    # No tokenizer is trusted: plain ASCII only, words split on single spaces, each exactly
    # --flag=value (an allowed flag and a plain non-empty value). No quote, backslash, tab, newline
    # or other control or non-ASCII character is accepted.
    if not opts.strip():
        return ["`options` is empty [rule: not empty]"]
    if any(not (" " <= c <= "~") or c in "\\'\"`" for c in opts):
        return ["`options` holds a character other than plain printable ASCII without quotes or backslashes [rule: characters]"]
    out = []
    for w in opts.split(" "):
        flag, eq, val = w.partition("=")
        if flag not in ALLOWED_OPTIONS:
            out.append(f"`options` has {w!r}, which is not an allowed flag ({', '.join(sorted(ALLOWED_OPTIONS))}) [rule: allowed flags]")
        elif not eq:
            out.append(f"`options` has {w!r}: an allowed flag must be written --flag=value [rule: --flag=value]")
        elif not OPTION_VALUE.fullmatch(val):
            out.append(f"`options` has {w!r}: the value must be plain (letters, digits and . _ : / + -) [rule: plain value]")
    return out


def describe_sec_services():
    return "; ".join(f"{j}: {', '.join(n)}" for j, n in SEC_SERVICES.items())


def check_sec_job_supply(where, jid, job):
    """sec.yml: every action pinned to a commit or digest, no host privilege outside `image`, images by digest."""
    for st in job.get("steps") if isinstance(job.get("steps"), list) else []:
        if isinstance(st, dict) and "uses" in st:
            u = st["uses"]
            if not (isinstance(u, str) and (USES_SHA.fullmatch(u) or USES_DIGEST.fullmatch(u))):
                refuse(where, f"`uses: {str(u)[:80]}` is not owner/repo@<40 hex commit> or docker://...@sha256:<digest> (no tag, branch or local action)")
    boxes = []
    if "container" in job:
        boxes.append(("container", job["container"]))
    allowed = SEC_SERVICES.get(jid, ())
    for key in job:
        if isinstance(key, str) and key.lower() == "services" and not allowed:
            refuse(where, f"`{key}` on job {jid}: the private workflow's jobs may start no service containers except {describe_sec_services()} [rule: services]")
        elif isinstance(key, str) and key.lower() == "services" and key != "services":
            refuse(where, f"job {jid} has `{key}`, which must be written exactly `services` (lower case) [rule: services]")
    svcs = job.get("services")
    if "services" in job and allowed:
        if not isinstance(svcs, dict):
            refuse(where, f"job {jid}: `services` is not a mapping [rule: services]")
        else:
            for k, v in svcs.items():
                if k not in allowed:
                    refuse(where, f"job {jid}: service `{k}` is not allowed (this job may start only {', '.join(allowed)}) [rule: services]")
                if not isinstance(v, dict):
                    refuse(where, f"job {jid}: service `{k}` is not a mapping [rule: service keys]")
                    continue
                for sk in v:
                    if sk not in CONTAINER_KEYS:
                        refuse(where, f"job {jid}: services.{k} has `{sk}`, which is not one of {', '.join(CONTAINER_KEYS)} (exact lower case) [rule: service keys]")
            boxes += [(f"services.{k}", v) for k, v in svcs.items() if isinstance(v, dict)]
    if isinstance(job.get("container"), dict):
        for key in job["container"]:
            if key not in CONTAINER_KEYS:
                refuse(where, f"container has `{key}`, which is not one of {', '.join(CONTAINER_KEYS)} (exact lower case) [rule: container keys]")
    for name, box in boxes:
        img = box if isinstance(box, str) else box.get("image") if isinstance(box, dict) else None
        if not (isinstance(img, str) and IMAGE_DIGEST.fullmatch(img.strip())):
            refuse(where, f"{name} image is not pinned by an @sha256: digest")
        if isinstance(box, dict):
            # `--privileged` has exactly one home: the dind service of the `image` job, at its pinned
            # image. Everything else (a container: anywhere, any other service, dind under another
            # job or at another digest) is held to the allow-listed health and limit flags.
            if (jid, name) == (SEC_IMAGE_JOB, "services.dind") and isinstance(img, str) and img.strip() == DIND_IMAGE:
                if box.get("options") not in (None, "--privileged"):
                    refuse(where, f"{name}: `options` is {box.get('options')!r}; the one privileged service may carry only `--privileged` [rule: dind options]")
                continue
            for why in option_problems(box.get("options")):
                refuse(where, f"{name}: {why}")


def tokenize(s):
    toks, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c == "'":
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise ValueError("an unterminated string")
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(s[j])
                j += 1
            toks.append(("str", "".join(buf)))
            i = j + 1
            continue
        if s[i : i + 2] in ("&&", "||", "==", "!=", "<=", ">="):
            toks.append(("op", s[i : i + 2]))
            i += 2
            continue
        if c in "()!,<>":
            toks.append(("op", c))
            i += 1
            continue
        if c in "&|=":
            raise ValueError(f"a stray {c!r}")
        j = i
        while j < n and not s[j].isspace() and s[j] not in "'()!,<>&|=":
            j += 1
        toks.append(("w", s[i:j]))
        i = j
    return toks


def split_top(toks, op):
    parts, depth = [[]], 0
    for t in toks:
        if t == ("op", "("):
            depth += 1
        elif t == ("op", ")"):
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced parentheses")
        if depth == 0 and t == ("op", op):
            parts.append([])
            continue
        parts[-1].append(t)
    if depth != 0:
        raise ValueError("unbalanced parentheses")
    return parts


def strip_parens(toks):
    while len(toks) >= 2 and toks[0] == ("op", "(") and toks[-1] == ("op", ")"):
        depth = 0
        for k, t in enumerate(toks):
            if t == ("op", "("):
                depth += 1
            elif t == ("op", ")"):
                depth -= 1
            if depth == 0 and k < len(toks) - 1:
                return toks  # the first ( closes before the end: not one wrapper
        toks = toks[1:-1]
    return toks


def conjuncts(toks):
    toks = strip_parens(toks)
    parts = split_top(toks, "&&")
    if len(parts) == 1:
        return [toks]
    out = []
    for p in parts:
        out += conjuncts(p)
    return out


def gate_problem(expr):
    """None if expr has the gate as a top-level conjunct, else why not."""
    s = expr.strip()
    if "${{" in s or "}}" in s:
        m = re.fullmatch(r"\$\{\{(.*)\}\}", s, re.S)
        if not m or "${{" in m.group(1) or "}}" in m.group(1):
            return "the `if:` mixes ${{ }} with other text"
        s = m.group(1)
    try:
        toks = strip_parens(tokenize(s))
        if len(split_top(toks, "||")) > 1:
            return "the `if:` has a top-level || (it could be true without the gate)"
        found = [c for c in conjuncts(toks) if c == GATE]
    except ValueError as e:
        return f"the `if:` could not be read ({e})"
    if not found:
        return f"the `if:` has no top-level conjunct `github.repository == '{GATE_REPO}'`"
    return None


def leaves(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, list):
        for x in v:
            yield from leaves(x)
    elif isinstance(v, dict):
        for x in v.values():
            yield from leaves(x)
    else:
        yield repr(v)


RUNNER_NAME_VAR = "sec_runner_name"


def texts(v):
    """Every key and every scalar of a parsed document, as text."""
    if isinstance(v, dict):
        for k, x in v.items():
            yield str(k)
            yield from texts(x)
    elif isinstance(v, list):
        for x in v:
            yield from texts(x)
    else:
        yield str(v)


def check_file(path, rel):
    """Returns (jobs seen, jobs that are sec jobs)."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        refuse(rel, f"cannot be read as UTF-8 ({e})")
        return 0, 0
    try:
        for ev in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(ev, yaml.AliasEvent) or getattr(ev, "anchor", None):
                refuse(rel, "an anchor or alias: a parser could read it differently from Forgejo's")
                return 0, 0
            # Any explicit tag (`!!str`, `!!binary`, `!local`, `!`), on a key or a value, a scalar,
            # a mapping or a sequence: PyYAML leaves `tag` None on every node written without one.
            if getattr(ev, "tag", None) is not None:
                line = ev.start_mark.line + 1
                refuse(rel, f"line {line}: an explicit YAML tag ({str(ev.tag)[:80]}): workflow files may not use explicit YAML tags [rule: no explicit tags]")
                return 0, 0
        docs = list(yaml.load_all(text, Loader=Loader))
    except KeyNotString as e:
        refuse(rel, str(e))
        return 0, 0
    except (yaml.YAMLError, TypeError, ValueError) as e:
        refuse(rel, f"does not parse cleanly ({str(e).splitlines()[0] if str(e) else type(e).__name__})")
        return 0, 0
    if len(docs) != 1:
        refuse(rel, f"{len(docs)} YAML documents (exactly one is read)")
        return 0, 0
    doc = docs[0]
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict) or not jobs:
        refuse(rel, "no `jobs:` mapping with a job in it")
        return 0, 0
    is_sec = rel == SEC_FILE
    # SEC_RUNNER_NAME is how sec-private-check.sh learns which runner took the job; only the
    # dedicated runner's own config (runner.envs) may set it. A workflow naming it anywhere (a key
    # of an env map, a step writing it to $GITHUB_ENV, any letter case) would set that answer from the workflow.
    # Rule 4 in the header.
    if any(RUNNER_NAME_VAR in t.casefold() for t in texts(doc)):
        refuse(rel, "names SEC_RUNNER_NAME: only the dedicated runner's config may set it")
    for jid, job in jobs.items():
        where = f"{rel} job {jid!s}"
        if not isinstance(job, dict):
            refuse(where, "is not a mapping")
            continue
        if "uses" in job:
            refuse(where, "has a job-level `uses:` (a called workflow is not checked here)")
        ro = job.get("runs-on")
        if is_sec:
            want = SEC_LABEL if jid == SEC_IMAGE_JOB else SEC_UNPRIV
            if not isinstance(ro, str) or ro != want:
                refuse(where, f"`runs-on` must be exactly the string {want} here, not {ro!r}")
            check_sec_job_supply(where, jid, job)
        else:
            for leaf in (leaves(ro) if "runs-on" in job else []):
                if SEC_LABEL in leaf.casefold():
                    refuse(where, f"`runs-on` names {SEC_LABEL}, the dedicated runner's label, in a public workflow")
                if "${{" in leaf:
                    refuse(where, "`runs-on` is an expression, which could name the dedicated runner")
            cond = job.get("if")
            if "if" not in job:
                refuse(where, f"has no job-level `if:` (the private copy would run it: gate it with `github.repository == '{GATE_REPO}'`)")
            elif not isinstance(cond, str):
                refuse(where, f"`if:` is {cond!r}, not a string: write the gate, then `&& false`")
            else:
                why = gate_problem(cond)
                if why:
                    refuse(where, why)
    return len(jobs), len(jobs) if is_sec else 0


def main():
    root = pathlib.Path(sys.argv[1])
    nfiles = njobs = nsec = 0
    if KIND == "github" and not root.exists() and not root.is_symlink():
        print(f"{SELF}: ok, {root} is absent: no GitHub workflow to hold")
        return 0
    if root.is_symlink():
        refuse(str(root), "is a symlink")
    elif not root.is_dir():
        refuse(str(root), "is not a directory" + (" (Forgejo would read .github/workflows instead)" if KIND == "forgejo" else ""))
    else:
        files = sorted(
            p for p in root.rglob("*") if p.suffix.lower() in (".yml", ".yaml") and not p.is_dir()
        )
        for p in root.rglob("*"):
            if p.is_symlink():
                refuse(p.relative_to(root).as_posix(), "is a symlink")
        for p in files:
            if p.is_symlink():
                continue
            nfiles += 1
            j, s = check_file(p, p.relative_to(root).as_posix())
            njobs += j
            nsec += s
        if nfiles == 0:
            if KIND == "forgejo":
                refuse(str(root), "holds no workflow file")
        elif njobs == 0:
            refuse(str(root), "holds no job")
    for line in problems:
        print(line)
    if problems:
        print(f"{SELF}: {len(problems)} refusal(s)")
        return 1
    if KIND == "forgejo":
        print(f"{SELF}: ok, {nfiles} workflow files, {njobs} jobs ({nsec} on the dedicated labels in {SEC_FILE}, "
              f"{njobs - nsec} gated to {GATE_REPO})")
    else:
        print(f"{SELF}: ok, {nfiles} GitHub workflow files, {njobs} jobs ({njobs} gated to {GATE_REPO}, "
              f"none on {SEC_LABEL})")
    return 0


sys.exit(main())
PYEOF
}

# check_sec_order <sec.yml> — rule 5: the guard runs before anything else in the private copy.
# The first job is `runner-guard`: it needs nothing, names no secret, and its steps are exactly the
# pinned checkout and the guard step, which runs `scripts/guards/workflow-runner-guard.sh` (the real
# tree, not the selftest). Every other
# job reaches it through `needs:`, and a job that names SEC_CHECK_TOKEN lists it directly, so a
# workflow the guard refuses never reaches a job that uses the token.
# Prints a refusal per problem; rc 0 ok, 1 refused, 3 cannot run.
check_sec_order() {
  if [ "${#PY[@]}" -eq 0 ] && ! pick_python; then
    echo "$SELF: REFUSED — no PyYAML: cannot read sec.yml's job order" >&2
    return 3
  fi
  "${PY[@]}" - "$1" <<'PYEOF'
import re
import sys

import yaml

SELF = "workflow-runner-guard"
GUARD = "runner-guard"
path = sys.argv[1]
problems = []
try:
    with open(path) as fh:
        doc = yaml.safe_load(fh)
    jobs = doc["jobs"]
    if not isinstance(jobs, dict) or not jobs or not all(isinstance(j, dict) for j in jobs.values()):
        raise ValueError("no jobs")
except Exception as e:
    print(f"{SELF}: {path}: could not be read as a workflow with jobs ({type(e).__name__})")
    sys.exit(1)


def needs(job):
    n = job.get("needs", [])
    if isinstance(n, str):
        return [n]
    return [x for x in n if isinstance(x, str)] if isinstance(n, list) else []


def texts(v):
    if isinstance(v, dict):
        for k, x in v.items():
            yield str(k)
            yield from texts(x)
    elif isinstance(v, list):
        for x in v:
            yield from texts(x)
    else:
        yield str(v)


SEC_GATE = "github.repository == 'runcove-sec/artifacts-sec'"
GUARD_JOB_KEYS = {"name", "runs-on", "timeout-minutes", "if", "steps"}
GUARD_STEP_KEYS = {"name", "run"}
CHECKOUT_STEP_KEYS = {"name", "uses", "with"}
CHECKOUT_WITH = {"persist-credentials": False}
# The guard job's first step: actions/checkout pinned to a commit (v4.4.0), never a moving tag.
CHECKOUT = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
# The guard job's second and last step runs exactly this line (leading and trailing space aside).
GUARD_RUN = ["scripts/guards/workflow-runner-guard.sh --pinned-python"]
# The only triggers sec.yml may have: the ones it uses.
TRIGGERS = {"push", "workflow_dispatch"}
# Exactly this workflow-level concurrency: one sec run at a time, a queued one never cancels another.
CONCURRENCY = {"group": "sec-pipeline", "cancel-in-progress": False}
# The only names the workflow-level env may set: the ones sec.yml uses.
WORKFLOW_ENV = {"SEC_REGISTRY_OWNER", "SEC_REGISTRY_USER"}
WORKFLOW_ENV_OWNER = "runcove-sec"


def keys(v):
    if isinstance(v, dict):
        for k, x in v.items():
            yield k
            yield from keys(x)
    elif isinstance(v, list):
        for x in v:
            yield from keys(x)


def run_lines(v):
    return [ln.strip() for ln in v.strip().splitlines()] if isinstance(v, str) else None


def unwrap(v):
    v = str(v).strip()
    if v.startswith("${{") and v.endswith("}}"):
        v = v[3:-2].strip()
    return v


# A status function (success, always, failure, cancelled; any letter case, any space before the
# parenthesis) replaces the implied success() of an if:, so the job or step can run after one it
# waits for failed: `always() && gate`, `!success()`, `success() || gate`. None is needed here.
STATUS_FN = re.compile(r"\b(success|always|failure|cancelled)\s*\(", re.IGNORECASE)


def status_fn(v):
    return v is not None and bool(STATUS_FN.search(str(v)))


if GUARD not in jobs:
    problems.append(f"sec.yml has no {GUARD} job: the workflow guard must run before any job that holds SEC_CHECK_TOKEN")
else:
    g = jobs[GUARD]
    if next(iter(jobs)) != GUARD:
        problems.append(f"sec.yml: {GUARD} is not the first job")
    if "needs" in g:
        problems.append(f"sec.yml: {GUARD} has needs: it must run first")
    if any("secrets." in t or "sec_check_token" in t.casefold() for t in texts(g)):
        problems.append(f"sec.yml: {GUARD} names a secret")
    steps = g.get("steps") if isinstance(g.get("steps"), list) else []
    # Exactly two steps: the pinned checkout, then the guard step. Nothing before, between or after
    # them can change what the guard step runs.
    if len(steps) != 2 or not all(isinstance(st, dict) for st in steps):
        problems.append(f"sec.yml: {GUARD} must have exactly two steps, the pinned checkout ({CHECKOUT}) and then the guard step")
    else:
        co, gs = steps
        if co.get("uses") != CHECKOUT:
            problems.append(f"sec.yml: the first step of {GUARD} is not uses: {CHECKOUT}")
        for k in sorted(set(map(str, co)) - CHECKOUT_STEP_KEYS):
            problems.append(f"sec.yml: the checkout step of {GUARD} has {k}: it may set only name, uses and with")
        if co.get("with") != CHECKOUT_WITH:
            problems.append(f"sec.yml: the checkout step of {GUARD} must have with: persist-credentials: false, and nothing else")
        if run_lines(gs.get("run")) != GUARD_RUN:
            problems.append(f"sec.yml: {GUARD} does not run scripts/guards/workflow-runner-guard.sh on the real tree "
                            "as its second step, with exactly the expected lines")
        for k in sorted(set(map(str, gs)) - GUARD_STEP_KEYS):
            problems.append(f"sec.yml: the guard step of {GUARD} has {k}: it may set only name and run")
    if "if" in g and unwrap(g["if"]) != SEC_GATE:
        problems.append(f"sec.yml: {GUARD} has an if: other than the repository gate {SEC_GATE}")
    for k in sorted(set(map(str, g)) - GUARD_JOB_KEYS):
        problems.append(f"sec.yml: {GUARD} has {k}: the guard job may set only {', '.join(sorted(GUARD_JOB_KEYS))}")
# No continue-on-error anywhere in the file (any job, any step, any value): a refusal must stop the run.
if any(str(k).strip().casefold() == "continue-on-error" for k in keys(doc)):
    problems.append("sec.yml has continue-on-error: no job or step in it may carry on past a failure")
# Every `sec-private-check.sh repo <repo> <label> ...` call gives, as <label>, exactly the plain
# runs-on of the job it is in: the check proves this job's runner carries that label.
PRIVATE_CHECK = re.compile(r"sec-private-check\.sh\s+repo\b")
PRIVATE_CHECK_ARGS = re.compile(r"sec-private-check\.sh\s+repo\s+(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s\"']+)\s+([A-Za-z0-9._-]+)(?=\s|$)")
for jid, job in jobs.items():
    for st in job.get("steps") if isinstance(job.get("steps"), list) else []:
        run = st.get("run") if isinstance(st, dict) else None
        if not isinstance(run, str):
            continue
        calls, args = len(PRIVATE_CHECK.findall(run)), PRIVATE_CHECK_ARGS.findall(run)
        if calls != len(args):
            problems.append(f"sec.yml: job {jid} calls sec-private-check.sh repo without a plain runner label as its second argument")
        for lab in args:
            if lab != job.get("runs-on"):
                problems.append(f"sec.yml: job {jid} calls sec-private-check.sh repo with the label {lab}, but runs on {job.get('runs-on')!r}")
# PyYAML reads an unquoted `on:` key as True; either spelling is the trigger list.
on_keys = [k for k in doc if k == "on" or k is True]
if len(on_keys) != 1:
    problems.append("sec.yml must have exactly one on: (its triggers)")
else:
    on = doc[on_keys[0]]
    names = [on] if isinstance(on, str) else on if isinstance(on, (list, dict)) else None
    if names is None or not names:
        problems.append("sec.yml: on: is not a trigger name, a list or a mapping of triggers")
    else:
        for t in sorted(map(str, set(names) - TRIGGERS)):
            problems.append(f"sec.yml: the trigger {t} is not allowed: only {', '.join(sorted(TRIGGERS))}")
if doc.get("concurrency") != CONCURRENCY:
    problems.append(f"sec.yml: the workflow-level concurrency must be exactly group: {CONCURRENCY['group']}, cancel-in-progress: false")
wenv = doc.get("env", {})
if not isinstance(wenv, dict):
    problems.append("sec.yml: the workflow-level env is not a plain mapping of names")
else:
    for k in sorted(map(str, set(wenv) - WORKFLOW_ENV)):
        problems.append(f"sec.yml: the workflow-level env sets {k}: it may set only {', '.join(sorted(WORKFLOW_ENV))}")
    # Where a value is fixed, check it: the owner is the one private org; the account is a plain login.
    if "SEC_REGISTRY_OWNER" in wenv and wenv["SEC_REGISTRY_OWNER"] != WORKFLOW_ENV_OWNER:
        problems.append(f"sec.yml: SEC_REGISTRY_OWNER must be exactly {WORKFLOW_ENV_OWNER}")
    if "SEC_REGISTRY_USER" in wenv and not (isinstance(wenv["SEC_REGISTRY_USER"], str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,38}", wenv["SEC_REGISTRY_USER"])):
        problems.append("sec.yml: SEC_REGISTRY_USER must be a plain lower-case login")
# No job or step re-sets them: only the workflow-level value counts.
for jid, job in jobs.items():
    for where, env in [(f"job {jid}", job.get("env"))] + [(f"a step of job {jid}", st.get("env")) for st in (job.get("steps") if isinstance(job.get("steps"), list) else []) if isinstance(st, dict)]:
        if isinstance(env, dict):
            for k in sorted(map(str, set(env) & WORKFLOW_ENV)):
                problems.append(f"sec.yml: {where} sets {k}: only the workflow-level env may")
for jid, job in jobs.items():
    if status_fn(job.get("if")):
        problems.append(f"sec.yml: job {jid} has an if: with a status function, which can run it after the guard failed")
    for st in job.get("steps") if isinstance(job.get("steps"), list) else []:
        if isinstance(st, dict) and status_fn(st.get("if")):
            problems.append(f"sec.yml: a step of job {jid} has an if: with a status function, which can run it after a failed step")
dflt = doc.get("defaults", {"run": {"shell": "bash"}})
if dflt != {"run": {"shell": "bash"}} and dflt != {}:
    problems.append("sec.yml: the workflow's defaults may set only run.shell: bash (the guard step runs with it)")
for jid, job in jobs.items():
    if jid == GUARD:
        continue
    seen, todo, reached = set(), list(needs(job)), False
    while todo:
        n = todo.pop()
        if n == GUARD:
            reached = True
            break
        if n in seen or n not in jobs or n == jid:
            continue
        seen.add(n)
        todo += needs(jobs[n])
    if not reached:
        problems.append(f"sec.yml: job {jid} does not wait for {GUARD} (directly or through needs)")
    if any("sec_check_token" in t.casefold() for t in texts(job)) and GUARD not in needs(job):
        problems.append(f"sec.yml: job {jid} names SEC_CHECK_TOKEN but does not need {GUARD} directly")
for line in problems:
    print(f"{SELF}: {line}")
if problems:
    sys.exit(1)
print(f"{SELF}: ok, sec.yml runs {GUARD} first and every other job waits for it")
PYEOF
}

# check_actions <repo root> — rule 4 for local actions: no action.yml or action.yaml (any letter
# case, anywhere under the root but .git; node_modules and .venv too, since a committed one could be
# a `uses: ./...` target) names SEC_RUNNER_NAME outside a comment, and none is a symlink or fails to parse.
check_actions() {
  if [ "${#PY[@]}" -eq 0 ] && ! pick_python; then
    echo "$SELF: REFUSED — no PyYAML: cannot read the local actions" >&2
    return 3
  fi
  "${PY[@]}" - "$1" <<'PYEOF'
import os
import pathlib
import sys

import yaml

SELF = "workflow-runner-guard"
root = pathlib.Path(sys.argv[1])
problems, n = [], 0


def texts(v):
    if isinstance(v, dict):
        for k, x in v.items():
            yield str(k)
            yield from texts(x)
    elif isinstance(v, list):
        for x in v:
            yield from texts(x)
    else:
        yield str(v)


for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = sorted(d for d in dirnames if d != ".git")
    for f in sorted(filenames):
        if f.lower() not in ("action.yml", "action.yaml"):
            continue
        p = pathlib.Path(dirpath) / f
        rel = p.relative_to(root).as_posix()
        n += 1
        if p.is_symlink():
            problems.append(f"{rel}: is a symlink")
            continue
        try:
            docs = list(yaml.safe_load_all(p.read_text()))
        except (yaml.YAMLError, UnicodeDecodeError, ValueError) as e:
            problems.append(f"{rel}: does not parse ({type(e).__name__})")
            continue
        if any("sec_runner_name" in t.casefold() for t in texts(docs)):
            problems.append(f"{rel}: names SEC_RUNNER_NAME: only the dedicated runner's config may set it")
for line in problems:
    print(f"{SELF}: {line}")
if problems:
    print(f"{SELF}: {len(problems)} refusal(s)")
    sys.exit(1)
print(f"{SELF}: ok, {n} local action file{'' if n == 1 else 's'}, none names SEC_RUNNER_NAME")
PYEOF
}

# ---- the selftest: fixture workflow directories, pass and refuse cases ----
selftest() {
  local fail=0
  tmp=$(mktemp -d) # global, so the EXIT trap still sees it
  trap 'rm -rf "$tmp"' EXIT
  local G="github.repository == 'runcove/artifacts'"

  # pubwf <dir> <file> <raw job line>... — a workflow, one job per argument (j1, j2, ...) whose
  # extra line is the argument verbatim; "-" adds no line.
  pubwf() {
    local d=$1 f=$2 n=0 a
    shift 2
    mkdir -p "$d"
    {
      printf 'name: x\n"on": push\njobs:\n'
      for a in "$@"; do
        n=$((n + 1))
        printf '  j%s:\n    runs-on: docker-light\n' "$n"
        [ "$a" = "-" ] || printf '    %s\n' "$a"
        printf '    steps:\n      - run: echo hi\n'
      done
    } >"$d/$f"
  }
  # secwf <dir> <file> <runs-on raw line>... — likewise, the argument is the runs-on line.
  secwf() {
    local d=$1 f=$2 n=0 a
    shift 2
    mkdir -p "$d"
    {
      printf 'name: s\n"on": push\njobs:\n'
      for a in "$@"; do
        n=$((n + 1))
        # an argument "image|<line>" is the job `image`; any other is s<n>
        case "$a" in
          image\|*) printf '  image:\n'; a=${a#image|} ;;
          *) printf '  s%s:\n' "$n" ;;
        esac
        [ "$a" = "-" ] || printf '    %s\n' "$a"
        printf '    steps:\n      - run: echo hi\n'
      done
    } >"$d/$f"
  }
  local H="github.repository == 'tenfourty/ream'"
  # run_case <kind> <label> <want rc> <dir> [want text in the output]; case_ checks a Forgejo
  # directory, gcase_ a GitHub one
  run_case() {
    local out rc kind=$1
    shift
    out=$(check_dir "$3" "$kind" 2>&1)
    rc=$?
    if [ "$rc" != "$2" ]; then
      printf '  FAIL  %s (rc=%s, want %s)\n%s\n' "$1" "$rc" "$2" "$(printf '%s\n' "$out" | sed 's/^/        /')"
      fail=1
    elif [ -n "${4:-}" ] && ! grep -qF -- "$4" <<< "$out"; then
      printf '  FAIL  %s (rc ok, but the output lacks "%s")\n%s\n' "$1" "$4" "$(printf '%s\n' "$out" | sed 's/^/        /')"
      fail=1
    else
      printf '  ok    %s\n' "$1"
    fi
  }
  case_() { run_case forgejo "$@"; }
  gcase_() { run_case github "$@"; }

  # --- passes ---
  local d=$tmp/p1
  pubwf "$d" ci.yml "if: \"$G\""
  secwf "$d" sec.yml "runs-on: runcove-sec-unpriv" "image|runs-on: runcove-sec"
  case_ "a gated public job, a sec job on runcove-sec-unpriv and image on runcove-sec" 0 "$d" "2 workflow files, 3 jobs (2 on the dedicated labels"
  d=$tmp/p2
  pubwf "$d" ci.yml \
    "if: \"$G && !contains(github.event.head_commit.message, '[skip ci]')\"" \
    "if: \"!contains(github.event.head_commit.message, '[skip ci]') && $G && github.ref == 'refs/heads/main'\"" \
    "if: \"($G)\"" \
    "if: \"${G//==/ == }\"" \
    "if: \"\${{ $G && always() }}\"" \
    "if: \"(github.ref == 'refs/heads/main' || github.event_name == 'push') && $G\"" \
    "if: \"($G && success()) && needs.a.result == 'success'\"" \
    "if: \"$G && false\"" \
    "if: >-
      $G &&
      needs.image.outputs.core_ref != ''"
  case_ "gates written as a conjunct in many forms" 0 "$d" "9 jobs"
  d=$tmp/p3
  pubwf "$d" ci.yml "if: \"$G\""
  case_ "no sec.yml at all is fine" 0 "$d"
  d=$tmp/p4
  pubwf "$d" ci.yml "if: \"$G\""
  pubwf "$d" other.yaml "if: \"$G\""
  case_ "a second public file, .yaml" 0 "$d" "2 workflow files"
  d=$tmp/p5
  pubwf "$d" ci.yml "if: \"$G\""
  printf 'ignored: not a workflow\n' >"$d/notes.txt"
  case_ "a non-yaml file in the directory is not read" 0 "$d" "1 workflow files"

  # --- refusals: the directory ---
  case_ "a missing directory" 1 "$tmp/nope" "is not a directory"
  mkdir -p "$tmp/empty"
  case_ "a directory with no workflow" 1 "$tmp/empty" "holds no workflow file"
  d=$tmp/r_nojobs
  mkdir -p "$d"
  printf 'name: x\n"on": push\njobs: {}\n' >"$d/ci.yml"
  case_ "a workflow with no job" 1 "$d" "no \`jobs:\` mapping"
  d=$tmp/r_symlink
  pubwf "$d" ci.yml "if: \"$G\""
  ln -s ci.yml "$d/alias.yml"
  case_ "a symlink in the directory" 1 "$d" "is a symlink"

  # --- refusals: a public job's gate ---
  d=$tmp/r1
  pubwf "$d" ci.yml "if: \"$G\"" -
  case_ "a public job with no if" 1 "$d" "ci.yml job j2: has no job-level"
  d=$tmp/r2
  pubwf "$d" ci.yml "if: \"github.repository != 'runcove-sec/artifacts-sec'\""
  case_ "the old negative gate" 1 "$d" "no top-level conjunct"
  d=$tmp/r3
  pubwf "$d" ci.yml "if: \"$G || true\""
  case_ "gate || true" 1 "$d" "top-level ||"
  pubwf "$d" ci.yml "if: \"true || $G\""
  case_ "true || gate" 1 "$d" "top-level ||"
  pubwf "$d" ci.yml "if: \"$G && github.ref == 'refs/heads/main' || always()\""
  case_ "gate && x || y" 1 "$d" "top-level ||"
  pubwf "$d" ci.yml "if: \"($G || true) && github.ref == 'refs/heads/main'\""
  case_ "(gate || true) && x" 1 "$d" "no top-level conjunct"
  pubwf "$d" ci.yml "if: \"($G || true)\""
  case_ "(gate || true) alone" 1 "$d" "top-level ||"
  pubwf "$d" ci.yml "if: \"! $G\""
  case_ "the gate negated" 1 "$d" "no top-level conjunct"
  pubwf "$d" ci.yml "if: \"$G == false\""
  case_ "the gate compared to false" 1 "$d" "no top-level conjunct"
  pubwf "$d" ci.yml "if: \"github.repository == 'runcove/artifacts-sec'\""
  case_ "another repo name" 1 "$d" "no top-level conjunct"
  pubwf "$d" ci.yml "if: \"github.repository == 'Runcove/Artifacts'\""
  case_ "the gate in another letter case" 1 "$d" "no top-level conjunct"
  pubwf "$d" ci.yml "if: \"github.repository != 'runcove/artifacts'\""
  case_ "the gate with !=" 1 "$d" "no top-level conjunct"
  pubwf "$d" ci.yml "if: \"contains(github.event.head_commit.message, '$G')\""
  case_ "the gate text inside a string" 1 "$d" "no top-level conjunct"
  pubwf "$d" ci.yml "if: \"github.event.repository.full_name == 'runcove/artifacts'\""
  case_ "a different context for the same name" 1 "$d" "no top-level conjunct"
  pubwf "$d" ci.yml "if: false"
  case_ "if: false (a boolean)" 1 "$d" "not a string"
  pubwf "$d" ci.yml "if: \"\""
  case_ "an empty if" 1 "$d" "no top-level conjunct"
  pubwf "$d" ci.yml "if: \"$G & true\""
  case_ "a stray single &" 1 "$d" "could not be read"
  pubwf "$d" ci.yml "if: \"($G && true\""
  case_ "unbalanced parentheses" 1 "$d" "could not be read"
  pubwf "$d" ci.yml "if: \"x \${{ $G }}\""
  case_ "\${{ }} mixed with other text" 1 "$d" "mixes"
  pubwf "$d" ci.yml "uses: ./.forgejo/workflows/x.yml" "if: \"$G\""
  case_ "a job-level uses" 1 "$d" "job-level \`uses:\`"
  d=$tmp/r4
  mkdir -p "$d"
  printf 'name: x\n"on": push\njobs:\n  a:\n    runs-on: docker-light\n    # %s\n    steps:\n      - run: echo hi\n' "$G" >"$d/ci.yml"
  case_ "the gate only in a comment" 1 "$d" "has no job-level"

  # --- refusals: where a job runs ---
  d=$tmp/r5
  mkdir -p "$d"
  wfjob() { # wfjob <runs-on raw> <if raw or -> — a one-job public workflow on stdout
    printf 'name: x\n"on": push\njobs:\n  a:\n    runs-on: %s\n' "$1"
    [ "$2" = "-" ] || printf '    if: "%s"\n' "$2"
    printf '    steps:\n      - run: echo\n'
  }
  wfjob runcove-sec - >"$d/ci.yml"
  case_ "a public job (no if) on runcove-sec" 1 "$d" "names runcove-sec"
  wfjob runcove-sec "$G" >"$d/ci.yml"
  case_ "a gated public job on runcove-sec" 1 "$d" "names runcove-sec"
  wfjob runcove-sec-unpriv "$G" >"$d/ci.yml"
  case_ "a gated public job on runcove-sec-unpriv" 1 "$d" "names runcove-sec"
  wfjob "[docker-light, Runcove-SEC]" "$G" >"$d/ci.yml"
  case_ "runcove-sec in a list, other letter case" 1 "$d" "names runcove-sec"
  wfjob "\${{ matrix.runner }}" "$G" >"$d/ci.yml"
  case_ "a public runs-on expression" 1 "$d" "is an expression"
  wfjob docker-light "$G" >"$d/ci.yml"
  printf '    runs-on: runcove-sec\n' >>"$d/ci.yml"
  case_ "a duplicate runs-on key" 1 "$d" "does not parse"
  d=$tmp/r6
  pubwf "$d" ci.yml "if: \"$G\""
  secwf "$d" sec.yml "runs-on: runcove-sec-unpriv" "runs-on: docker-light"
  case_ "a sec job on a shared label" 1 "$d" "sec.yml job s2"
  # image on runcove-sec (privileged), every other sec job on runcove-sec-unpriv, exactly.
  secwf "$d" sec.yml "runs-on: runcove-sec" "image|runs-on: runcove-sec"
  case_ "a test job on the privileged runcove-sec" 1 "$d" "sec.yml job s1: \`runs-on\` must be exactly the string runcove-sec-unpriv"
  secwf "$d" sec.yml "runs-on: runcove-sec-unpriv" "image|runs-on: runcove-sec-unpriv"
  case_ "image on runcove-sec-unpriv" 1 "$d" "sec.yml job image: \`runs-on\` must be exactly the string runcove-sec here"
  secwf "$d" sec.yml "runs-on: runcove-sec-unpriv" "image|runs-on: [runcove-sec]"
  case_ "image with a list" 1 "$d" "sec.yml job image: \`runs-on\` must be exactly"
  secwf "$d" sec.yml "runs-on: [runcove-sec-unpriv]" "image|runs-on: runcove-sec"
  case_ "an unprivileged job with a list" 1 "$d" "sec.yml job s1: \`runs-on\` must be exactly"
  secwf "$d" sec.yml "runs-on: runcove-sec-unpriv" "image|runs-on: \${{ vars.IMAGE_RUNNER }}"
  case_ "image with an expression" 1 "$d" "sec.yml job image: \`runs-on\` must be exactly"
  secwf "$d" sec.yml "runs-on: runcove-sec-unpriv-2" "image|runs-on: runcove-sec"
  case_ "an unprivileged label with a suffix" 1 "$d" "sec.yml job s1: \`runs-on\` must be exactly"
  secwf "$d" sec.yml "runs-on: runcove-sec-unpriv" "image|runs-on: runcove-sec:docker://x"
  case_ "image's label with a suffix" 1 "$d" "sec.yml job image: \`runs-on\` must be exactly"
  secwf "$d" sec.yml "runs-on: runcove-sec-unpriv" "runs-on: runcove-sec-unpriv" "image|runs-on: runcove-sec"
  case_ "the real shape: image on runcove-sec, the rest on runcove-sec-unpriv" 0 "$d" "3 on the dedicated labels in sec.yml"
  secwf "$d" sec.yml "runs-on: deploy-docker"
  case_ "a sec job on deploy-docker" 1 "$d" "must be exactly"
  secwf "$d" sec.yml "runs-on: [runcove-sec]"
  case_ "a sec job with a list" 1 "$d" "must be exactly"
  secwf "$d" sec.yml "runs-on: [runcove-sec, docker-light]"
  case_ "a sec job with a list of two labels" 1 "$d" "must be exactly"
  secwf "$d" sec.yml "runs-on: \${{ vars.RUNNER }}"
  case_ "a sec job with an expression" 1 "$d" "must be exactly"
  secwf "$d" sec.yml "runs-on: Runcove-Sec"
  case_ "a sec job in another letter case" 1 "$d" "must be exactly"
  secwf "$d" sec.yml "runs-on: 'runcove-sec '"
  case_ "a sec job with a trailing space" 1 "$d" "must be exactly"
  secwf "$d" sec.yml -
  case_ "a sec job with no runs-on" 1 "$d" "must be exactly"
  secwf "$d" sec.yml "runs-on: runcove-sec
    uses: ./.forgejo/workflows/ci.yml"
  case_ "a sec job with a uses" 1 "$d" "job-level \`uses:\`"
  d=$tmp/r7
  secwf "$d" sec.yml "runs-on: runcove-sec-unpriv"
  secwf "$d" sec.yaml "runs-on: runcove-sec"
  case_ "sec.yaml is a public file: no gate, on runcove-sec" 1 "$d" "sec.yaml job s1"
  d=$tmp/r8
  secwf "$d" sec.yml "runs-on: runcove-sec-unpriv"
  secwf "$d/sub" sec.yml "runs-on: runcove-sec-unpriv"
  case_ "sub/sec.yml is a public file" 1 "$d" "sub/sec.yml job s1"

  # --- refusals: a file two parsers could read two ways ---
  d=$tmp/r9
  mkdir -p "$d"
  printf 'name: x\n"on": push\njobs:\n  a:\n    runs-on: docker-light\n    if: "%s"\n    if: "false"\n    steps:\n      - run: echo\n' "$G" >"$d/ci.yml"
  case_ "a duplicate if key" 1 "$d" "duplicate key"
  printf 'name: x\n"on": push\njobs:\n  a: &base\n    runs-on: docker-light\n    if: "%s"\n    steps:\n      - run: echo\n  b: *base\n' "$G" >"$d/ci.yml"
  case_ "an anchor and alias" 1 "$d" "anchor or alias"
  printf 'name: x\n"on": push\njobs:\n  a:\n    <<: {runs-on: runcove-sec}\n    if: "%s"\n    steps:\n      - run: echo\n' "$G" >"$d/ci.yml"
  case_ "a merge key" 1 "$d" "merge key"
  printf 'name: x\n"on": push\njobs:\n  a:\n    runs-on: docker-light\n    if: "%s"\n    steps:\n      - run: echo\n---\njobs:\n  b:\n    runs-on: docker-light\n' "$G" >"$d/ci.yml"
  case_ "a second YAML document" 1 "$d" "2 YAML documents"
  printf 'name: x\njobs:\n  a: [unclosed\n' >"$d/ci.yml"
  case_ "a file that does not parse" 1 "$d" "does not parse"
  printf '\xff\xfe not utf-8\n' >"$d/ci.yml"
  case_ "a file that is not UTF-8" 1 "$d" "UTF-8"
  d=$tmp/r10
  mkdir -p "$d"
  printf 'name: x\n"on": push\njobs:\n  a: not-a-mapping\n' >"$d/ci.yml"
  case_ "a job that is not a mapping" 1 "$d" "is not a mapping"

  # --- the GitHub workflows (.github/workflows): the gate is the mirror's name, not the forge's ---
  d=$tmp/g_absent
  gcase_ "a missing .github/workflows is fine" 0 "$d/nope" "is absent"
  mkdir -p "$d"
  gcase_ "an empty .github/workflows is fine" 0 "$d" "0 GitHub workflow files"
  d=$tmp/g_p1
  pubwf "$d" publish.yml "if: \"$H\"" "if: \"$H\""
  gcase_ "two jobs gated to the GitHub mirror" 0 "$d" "1 GitHub workflow files, 2 jobs (2 gated to tenfourty/ream"
  d=$tmp/g_p2
  pubwf "$d" publish.yml \
    "if: \"$H && github.ref == 'refs/heads/main'\"" \
    "if: \"github.ref == 'refs/heads/main' && $H\"" \
    "if: \"(github.event_name == 'push' || github.event_name == 'workflow_dispatch') && $H\"" \
    "if: \"\${{ $H && always() }}\"" \
    "if: >-
      $H &&
      needs.build.result == 'success'"
  pubwf "$d" other.yaml "if: \"$H && false\""
  gcase_ "gates written as a conjunct in many forms" 0 "$d" "2 GitHub workflow files, 6 jobs"
  d=$tmp/g_r1
  pubwf "$d" publish.yml "if: \"$H\"" -
  gcase_ "a GitHub job with no if (the second of two)" 1 "$d" "publish.yml job j2: has no job-level"
  pubwf "$d" publish.yml "if: \"$G\""
  gcase_ "the forge's gate is not the mirror's" 1 "$d" "no top-level conjunct \`github.repository == 'tenfourty/ream'\`"
  pubwf "$d" publish.yml "if: \"$G && $H\""
  gcase_ "the forge's gate and the mirror's: gated to both, never true anywhere" 0 "$d" "1 jobs"
  pubwf "$d" publish.yml "if: \"github.repository != 'runcove-sec/artifacts-sec'\""
  gcase_ "a negative gate" 1 "$d" "no top-level conjunct"
  pubwf "$d" publish.yml "if: \"$H || true\""
  gcase_ "gate || true" 1 "$d" "top-level ||"
  pubwf "$d" publish.yml "if: \"true || $H\""
  gcase_ "true || gate" 1 "$d" "top-level ||"
  pubwf "$d" publish.yml "if: \"$H && github.ref == 'refs/heads/main' || always()\""
  gcase_ "gate && x || y" 1 "$d" "top-level ||"
  pubwf "$d" publish.yml "if: \"($H || true) && github.ref == 'refs/heads/main'\""
  gcase_ "(gate || true) && x" 1 "$d" "no top-level conjunct"
  pubwf "$d" publish.yml "if: \"! $H\""
  gcase_ "the gate negated" 1 "$d" "no top-level conjunct"
  pubwf "$d" publish.yml "if: \"github.repository == 'Tenfourty/Ream'\""
  gcase_ "the gate in another letter case" 1 "$d" "no top-level conjunct"
  pubwf "$d" publish.yml "if: \"github.repository == 'tenfourty/ream-sec'\""
  gcase_ "another repo name" 1 "$d" "no top-level conjunct"
  pubwf "$d" publish.yml "if: \"contains(github.event.head_commit.message, '$H')\""
  gcase_ "the gate text inside a string" 1 "$d" "no top-level conjunct"
  pubwf "$d" publish.yml "if: false"
  gcase_ "if: false (a boolean)" 1 "$d" "not a string"
  pubwf "$d" publish.yml "uses: ./.github/workflows/x.yml" "if: \"$H\""
  gcase_ "a job-level uses" 1 "$d" "job-level \`uses:\`"
  d=$tmp/g_r2
  mkdir -p "$d"
  printf 'name: x\n"on": push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    # %s\n    steps:\n      - run: echo hi\n' "$H" >"$d/publish.yml"
  gcase_ "the gate only in a comment" 1 "$d" "has no job-level"
  d=$tmp/g_r3
  mkdir -p "$d"
  wfjob() { # wfjob <runs-on raw> <if raw or -> — a one-job workflow on stdout
    printf 'name: x\n"on": push\njobs:\n  a:\n    runs-on: %s\n' "$1"
    [ "$2" = "-" ] || printf '    if: "%s"\n' "$2"
    printf '    steps:\n      - run: echo\n'
  }
  wfjob runcove-sec "$H" >"$d/publish.yml"
  gcase_ "a gated GitHub job on runcove-sec" 1 "$d" "names runcove-sec"
  wfjob "[ubuntu-latest, Runcove-SEC]" "$H" >"$d/publish.yml"
  gcase_ "runcove-sec in a list, other letter case" 1 "$d" "names runcove-sec"
  wfjob "\${{ matrix.runner }}" "$H" >"$d/publish.yml"
  gcase_ "a runs-on expression" 1 "$d" "is an expression"
  wfjob runcove-sec - >"$d/sec.yml"
  rm "$d/publish.yml"
  gcase_ "a sec.yml in .github/workflows is a public file: no gate, on runcove-sec" 1 "$d" "sec.yml job a: has no job-level"
  d=$tmp/g_r4
  pubwf "$d" publish.yml "if: \"$H\""
  ln -s publish.yml "$d/alias.yml"
  gcase_ "a symlink in .github/workflows" 1 "$d" "is a symlink"
  d=$tmp/g_r5
  mkdir -p "$d"
  printf 'name: x\n"on": push\njobs: {}\n' >"$d/publish.yml"
  gcase_ "a GitHub workflow with no job" 1 "$d" "no \`jobs:\` mapping"
  printf 'name: x\n"on": push\njobs:\n  a: &base\n    runs-on: ubuntu-latest\n    if: "%s"\n    steps:\n      - run: echo\n  b: *base\n' "$H" >"$d/publish.yml"
  gcase_ "an anchor and alias" 1 "$d" "anchor or alias"
  printf 'name: x\n"on": push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    if: "%s"\n    if: "false"\n    steps:\n      - run: echo\n' "$H" >"$d/publish.yml"
  gcase_ "a duplicate if key" 1 "$d" "duplicate key"
  printf 'name: x\njobs:\n  a: [unclosed\n' >"$d/publish.yml"
  gcase_ "a file that does not parse" 1 "$d" "does not parse"
  printf 'not a directory\n' >"$tmp/g_file"
  gcase_ ".github/workflows that is a file" 1 "$tmp/g_file" "is not a directory"
  ln -s g_p1 "$tmp/g_link"
  gcase_ ".github/workflows that is a symlink" 1 "$tmp/g_link" "is a symlink"
  # the two directories hold different gates: the mirror's name does not pass in .forgejo/workflows
  d=$tmp/g_x
  pubwf "$d" ci.yml "if: \"$H\""
  case_ "the mirror's gate in a Forgejo workflow" 1 "$d" "no top-level conjunct \`github.repository == 'runcove/artifacts'\`"
  # SEC_RUNNER_NAME comes only from the dedicated runner's own config: no workflow may name it,
  # as an env key at any level, in a step's script (>> "$GITHUB_ENV"), or in another letter case.
  d=$tmp/rn_env
  secwf "$d" sec.yml "runs-on: runcove-sec-unpriv"
  printf 'env:\n  SEC_RUNNER_NAME: dedicated-priv-1\n' >>"$d/sec.yml"
  case_ "SEC_RUNNER_NAME set in a workflow-level env" 1 "$d" "names SEC_RUNNER_NAME"
  d=$tmp/rn_run
  mkdir -p "$d"
  printf 'name: s\n"on": push\njobs:\n  s1:\n    runs-on: runcove-sec-unpriv\n    steps:\n      - run: echo "sec_runner_name=x" >> "$GITHUB_ENV"\n' >"$d/sec.yml"
  case_ "SEC_RUNNER_NAME written to GITHUB_ENV in a step" 1 "$d" "names SEC_RUNNER_NAME"
  # The gated public workflow first passes on its own (so its gate is really there), then gains
  # the variable in its job env and must be refused for that alone.
  d=$tmp/rn_pub
  pubwf "$d" ci.yml "if: \"$G\""
  case_ "the gated public workflow the next case extends passes" 0 "$d" "0 on the dedicated labels in sec.yml, 1 gated to runcove/artifacts"
  sed -i 's|^\(    if: .*\)$|\1\n    env: {SEC_RUNNER_NAME: x}|' "$d/ci.yml"
  case_ "SEC_RUNNER_NAME in a public workflow's job env" 1 "$d" "names SEC_RUNNER_NAME"
  run_case forgejo "...and that is its only refusal" 1 "$d" "1 refusal(s)"
  d=$tmp/rn_job
  mkdir -p "$d"
  printf 'name: s\n"on": push\njobs:\n  s1:\n    runs-on: runcove-sec-unpriv\n    env:\n      SEC_RUNNER_NAME: dedicated-priv-1\n    steps:\n      - run: echo hi\n' >"$d/sec.yml"
  case_ "SEC_RUNNER_NAME set in a job-level env" 1 "$d" "names SEC_RUNNER_NAME"
  d=$tmp/rn_step
  mkdir -p "$d"
  printf 'name: s\n"on": push\njobs:\n  s1:\n    runs-on: runcove-sec-unpriv\n    steps:\n      - run: echo hi\n        env:\n          Sec_Runner_Name: dedicated-priv-1\n' >"$d/sec.yml"
  case_ "SEC_RUNNER_NAME set in a step-level env" 1 "$d" "names SEC_RUNNER_NAME"
  d=$tmp/rn_comment
  secwf "$d" sec.yml "runs-on: runcove-sec-unpriv"
  printf '# SEC_RUNNER_NAME comes from the runner\n' >>"$d/sec.yml"
  case_ "SEC_RUNNER_NAME only in a comment passes" 0 "$d"

  # sec.yml's order (rule 5): the guard is the first job, and nothing runs before it.
  ocase_() { # <name> <want rc> <file> [<text the output must contain>]
    local out rc
    out=$(check_sec_order "$3" 2>&1)
    rc=$?
    if [ "$rc" != "$2" ]; then
      printf '  FAIL  %s (rc=%s, want %s)\n%s\n' "$1" "$rc" "$2" "$(printf '%s\n' "$out" | sed 's/^/        /')"; fail=1
    elif [ -n "${4:-}" ] && ! grep -qF -- "$4" <<< "$out"; then
      printf '  FAIL  %s (rc ok, but the output lacks "%s")\n%s\n' "$1" "$4" "$(printf '%s\n' "$out" | sed 's/^/        /')"; fail=1
    else
      printf '  ok    %s\n' "$1"
    fi
  }
  # ordwf <file> <guard job lines or -> <privacy needs> <test needs>: a sec.yml of three jobs.
  ordwf() {
    {
      printf 'name: sec\n"on": push\nconcurrency:\n  group: sec-pipeline\n  cancel-in-progress: false\njobs:\n'
      if [ "$2" != "-" ]; then
        printf '  runner-guard:\n    runs-on: runcove-sec\n%b    steps:\n      - uses: %s\n        with:\n          persist-credentials: false\n      - name: workflow guard\n' "$2" "$CO"
        printf '        run: scripts/guards/workflow-runner-guard.sh --pinned-python\n'
      fi
      printf '  privacy:\n    runs-on: runcove-sec\n%b    steps:\n      - env:\n          SEC_CHECK_TOKEN: ${{ secrets.SEC_CHECK_TOKEN }}\n        run: check\n' "$3"
      printf '  test:\n    runs-on: runcove-sec\n%b    steps:\n      - run: echo test\n' "$4"
    } >"$1"
  }
  local o=$tmp/order CO="actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
  mkdir -p "$o"
  ordwf "$o/ok.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  ocase_ "order: the guard first, everything after it" 0 "$o/ok.yml"
  ordwf "$o/none.yml" - "" "    needs: [privacy]\n"
  ocase_ "order: no runner-guard job" 1 "$o/none.yml" "has no runner-guard job"
  { printf 'name: sec\n"on": push\njobs:\n  privacy:\n    runs-on: runcove-sec\n    needs: [runner-guard]\n    steps:\n      - run: echo\n'
    printf '  runner-guard:\n    runs-on: runcove-sec\n    steps:\n      - run: scripts/guards/workflow-runner-guard.sh\n'; } >"$o/second.yml"
  ocase_ "order: the guard is not the first job" 1 "$o/second.yml" "is not the first job"
  ordwf "$o/selftest.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^        run: scripts/guards/workflow-runner-guard.sh --pinned-python$|        run: scripts/guards/workflow-runner-guard.sh --selftest|' "$o/selftest.yml"
  ocase_ "order: the guard job only runs the selftest" 1 "$o/selftest.yml" "does not run scripts/guards/workflow-runner-guard.sh"
  ordwf "$o/gneeds.yml" "    needs: [test]\n" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  ocase_ "order: the guard job needs another job" 1 "$o/gneeds.yml" "runner-guard has needs"
  ordwf "$o/gsecret.yml" "    env: {T: \"\${{ secrets.SEC_CHECK_TOKEN }}\"}\n" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  ocase_ "order: the guard job names a secret" 1 "$o/gsecret.yml" "runner-guard names a secret"
  ordwf "$o/tokindirect.yml" "" "    needs: [test]\n" "    needs: [runner-guard]\n"
  ocase_ "order: a token job reaching the guard only through another job" 1 "$o/tokindirect.yml" "privacy names SEC_CHECK_TOKEN but does not need runner-guard directly"
  ordwf "$o/loose.yml" "" "    needs: [runner-guard]\n" ""
  ocase_ "order: a job that does not wait for the guard" 1 "$o/loose.yml" "job test does not wait for runner-guard"
  ordwf "$o/str.yml" "" "    needs: runner-guard\n" "    needs: privacy\n"
  ocase_ "order: needs as a plain string counts" 0 "$o/str.yml"
  ordwf "$o/cycle.yml" "" "    needs: [runner-guard]\n" "    needs: [test]\n"
  ocase_ "order: a job that needs itself never reaches the guard" 1 "$o/cycle.yml" "job test does not wait for runner-guard"
  printf 'jobs: [unclosed\n' >"$o/junk.yml"
  ocase_ "order: a sec.yml that does not parse" 1 "$o/junk.yml" "could not be read"
  ocase_ "order: a missing sec.yml" 1 "$o/absent.yml" "could not be read"
  # A status function in an if: runs a job (or step) after the guard refused the tree.
  local SG="github.repository == 'runcove-sec/artifacts-sec'"
  ordwf "$o/always.yml" "" "    needs: [runner-guard]\n    if: \"always() && $SG\"\n" "    needs: [privacy]\n"
  ocase_ "order: a token job whose if: is always() && the gate" 1 "$o/always.yml" "job privacy has an if: with a status function"
  ordwf "$o/notsucc.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n    if: \"! Success ()\"\n"
  ocase_ "order: a job whose if: is ! Success ()" 1 "$o/notsucc.yml" "job test has an if: with a status function"
  ordwf "$o/succor.yml" "" "    needs: [runner-guard]\n    if: \"success() || $SG\"\n" "    needs: [privacy]\n"
  ocase_ "order: a job whose if: is success() || the gate" 1 "$o/succor.yml" "job privacy has an if: with a status function"
  ordwf "$o/cancel.yml" "" "    needs: [runner-guard]\n    if: \"\${{ CANCELLED() || $SG }}\"\n" "    needs: [privacy]\n"
  ocase_ "order: a wrapped, upper-case CANCELLED() in a job if:" 1 "$o/cancel.yml" "job privacy has an if: with a status function"
  ordwf "$o/stepfail.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^        run: check$|        run: check\n        if: failure()|' "$o/stepfail.yml"
  ocase_ "order: a step of a token job whose if: is failure()" 1 "$o/stepfail.yml" "a step of job privacy has an if: with a status function"
  # The guard job cannot be told to carry on past a refusal.
  ordwf "$o/gcoe.yml" "    continue-on-error: true\n" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  ocase_ "order: continue-on-error: true on the guard job" 1 "$o/gcoe.yml" "runner-guard has continue-on-error"
  ordwf "$o/gcoef.yml" "    continue-on-error: false\n" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  ocase_ "order: continue-on-error, any value, on the guard job" 1 "$o/gcoef.yml" "runner-guard has continue-on-error"
  ordwf "$o/scoe.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^      - name: workflow guard$|      - name: workflow guard\n        continue-on-error: true|' "$o/scoe.yml"
  ocase_ "order: continue-on-error on the guard step" 1 "$o/scoe.yml" "sec.yml has continue-on-error"
  ordwf "$o/sif.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^      - name: workflow guard$|      - name: workflow guard\n        if: github.ref == '"'x'"'|' "$o/sif.yml"
  ocase_ "order: an if: on the guard step" 1 "$o/sif.yml" "the guard step of runner-guard has if"
  ordwf "$o/sshell.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^      - name: workflow guard$|      - name: workflow guard\n        shell: sh -c true {0}|' "$o/sshell.yml"
  ocase_ "order: a shell: on the guard step" 1 "$o/sshell.yml" "the guard step of runner-guard has shell"
  ordwf "$o/gif.yml" "    if: \"github.ref == 'x'\"\n" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  ocase_ "order: an if: on the guard job other than the gate" 1 "$o/gif.yml" "runner-guard has an if: other than the repository gate"
  ordwf "$o/gifok.yml" "    if: \"$SG\"\n" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  ocase_ "order: the repository gate as the guard job's if: passes" 0 "$o/gifok.yml"
  ordwf "$o/genv.yml" "    env: {BASH_ENV: x}\n" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  ocase_ "order: the guard job sets env" 1 "$o/genv.yml" "runner-guard has env"
  ordwf "$o/dflt.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^jobs:$|defaults:\n  run:\n    shell: sh -c true {0}\njobs:|' "$o/dflt.yml"
  ocase_ "order: workflow defaults with another shell" 1 "$o/dflt.yml" "defaults may set only run.shell: bash"
  # continue-on-error anywhere in sec.yml, not only on the guard job.
  ordwf "$o/pcoe.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^        run: check$|        run: check\n        continue-on-error: true|' "$o/pcoe.yml"
  ocase_ "order: continue-on-error on a step of the privacy job" 1 "$o/pcoe.yml" "sec.yml has continue-on-error"
  ordwf "$o/tcoe.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n    continue-on-error: false\n"
  ocase_ "order: continue-on-error, any value, on another job" 1 "$o/tcoe.yml" "sec.yml has continue-on-error"
  ordwf "$o/xcoe.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^      - run: echo test$|      - run: echo test\n        continue-on-error: ${{ true }}|' "$o/xcoe.yml"
  ocase_ "order: continue-on-error as an expression on a step" 1 "$o/xcoe.yml" "sec.yml has continue-on-error"
  # The guard job's steps are exactly the pinned checkout and the guard step.
  ordwf "$o/sbefore.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i "s|^      - uses: $CO\$|      - run: echo x >> \"\$GITHUB_ENV\"\n&|" "$o/sbefore.yml"
  ocase_ "order: a step before the checkout in the guard job" 1 "$o/sbefore.yml" "runner-guard must have exactly two steps"
  ordwf "$o/sbetween.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i "s|^      - uses: $CO\$|&\n      - run: echo x >> \"\$GITHUB_PATH\"|" "$o/sbetween.yml"
  ocase_ "order: a step between the checkout and the guard step" 1 "$o/sbetween.yml" "runner-guard must have exactly two steps"
  ordwf "$o/safter.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^        run: scripts/guards/workflow-runner-guard.sh --pinned-python$|&\n      - run: echo after|' "$o/safter.yml"
  ocase_ "order: a step after the guard step" 1 "$o/safter.yml" "runner-guard must have exactly two steps"
  ordwf "$o/cotag.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i "s|^      - uses: $CO\$|      - uses: actions/checkout@v4|" "$o/cotag.yml"
  ocase_ "order: a checkout on a tag, not a commit" 1 "$o/cotag.yml" "the first step of runner-guard is not uses: $CO"
  ordwf "$o/coother.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i "s|^      - uses: actions/checkout@|      - uses: someone/checkout@|" "$o/coother.yml"
  ocase_ "order: another action at the pinned commit" 1 "$o/coother.yml" "the first step of runner-guard is not uses: $CO"
  ordwf "$o/srun.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^        run: scripts/guards/workflow-runner-guard.sh --pinned-python$|        run: \|\n          echo BASH_ENV=x >> "$GITHUB_ENV"\n          scripts/guards/workflow-runner-guard.sh --pinned-python|' "$o/srun.yml"
  ocase_ "order: an extra line in the guard step" 1 "$o/srun.yml" "does not run scripts/guards/workflow-runner-guard.sh"
  ordwf "$o/snopin.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^        run: scripts/guards/workflow-runner-guard.sh --pinned-python$|        run: scripts/guards/workflow-runner-guard.sh|' "$o/snopin.yml"
  ocase_ "order: the guard step without its pinned Python" 1 "$o/snopin.yml" "does not run scripts/guards/workflow-runner-guard.sh"
  # A sec-private-check.sh repo call names the label its own job runs on (privacy runs on runcove-sec here).
  ordwf "$o/pcok.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^        run: check$|        run: .forgejo/scripts/sec-private-check.sh repo "$GITHUB_REPOSITORY" runcove-sec "$SEC_REGISTRY_OWNER"|' "$o/pcok.yml"
  ocase_ "order: the private check given its job's label passes" 0 "$o/pcok.yml"
  ordwf "$o/pcother.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^        run: check$|        run: .forgejo/scripts/sec-private-check.sh repo "$GITHUB_REPOSITORY" runcove-sec-unpriv "$SEC_REGISTRY_OWNER"|' "$o/pcother.yml"
  ocase_ "order: the private check given another job's label" 1 "$o/pcother.yml" "calls sec-private-check.sh repo with the label runcove-sec-unpriv, but runs on 'runcove-sec'"
  ordwf "$o/pcnone.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^        run: check$|        run: .forgejo/scripts/sec-private-check.sh repo "$GITHUB_REPOSITORY" "$SEC_REGISTRY_OWNER"|' "$o/pcnone.yml"
  ocase_ "order: the private check without a label" 1 "$o/pcnone.yml" "without a plain runner label"
  ordwf "$o/pcvar.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^        run: check$|        run: .forgejo/scripts/sec-private-check.sh repo "$GITHUB_REPOSITORY" "$LABEL" "$SEC_REGISTRY_OWNER"|' "$o/pcvar.yml"
  ocase_ "order: the private check given a variable label" 1 "$o/pcvar.yml" "without a plain runner label"
  ordwf "$o/pctwo.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^        run: check$|        run: \|\n          .forgejo/scripts/sec-private-check.sh repo "$GITHUB_REPOSITORY" runcove-sec "$O"\n          .forgejo/scripts/sec-private-check.sh repo "$GITHUB_REPOSITORY" runcove-sec-unpriv "$O"|' "$o/pctwo.yml"
  ocase_ "order: a second private check in a step given another label" 1 "$o/pctwo.yml" "with the label runcove-sec-unpriv"
  # Only the triggers sec.yml uses.
  ordwf "$o/onprt.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^"on": push$|"on":\n  push:\n  pull_request_target:|' "$o/onprt.yml"
  ocase_ "order: a pull_request_target trigger" 1 "$o/onprt.yml" "the trigger pull_request_target is not allowed"
  ordwf "$o/onlist.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^"on": push$|"on": [push, schedule]|' "$o/onlist.yml"
  ocase_ "order: another trigger in a list" 1 "$o/onlist.yml" "the trigger schedule is not allowed"
  ordwf "$o/onbare.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^"on": push$|on: pull_request|' "$o/onbare.yml"
  ocase_ "order: an unquoted on: with another trigger" 1 "$o/onbare.yml" "the trigger pull_request is not allowed"
  ordwf "$o/ontwo.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^"on": push$|"on": push\non: pull_request_target|' "$o/ontwo.yml"
  ocase_ "order: both a quoted and an unquoted on:" 1 "$o/ontwo.yml" "must have exactly one on:"
  ordwf "$o/onnone.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i '/^"on": push$/d' "$o/onnone.yml"
  ocase_ "order: no on: at all" 1 "$o/onnone.yml" "must have exactly one on:"
  ordwf "$o/onok.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^"on": push$|"on":\n  push:\n    branches: [main]\n  workflow_dispatch:|' "$o/onok.yml"
  ocase_ "order: push and workflow_dispatch pass" 0 "$o/onok.yml"
  # Exactly one workflow-level concurrency block.
  ordwf "$o/ccnone.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i '/^concurrency:$/,/^  cancel-in-progress: false$/d' "$o/ccnone.yml"
  ocase_ "order: no concurrency block" 1 "$o/ccnone.yml" "concurrency must be exactly group: sec-pipeline"
  ordwf "$o/ccgroup.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^  group: sec-pipeline$|  group: sec-${{ github.ref_name }}|' "$o/ccgroup.yml"
  ocase_ "order: another concurrency group" 1 "$o/ccgroup.yml" "concurrency must be exactly group: sec-pipeline"
  ordwf "$o/cccancel.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^  cancel-in-progress: false$|  cancel-in-progress: true|' "$o/cccancel.yml"
  ocase_ "order: cancel-in-progress true" 1 "$o/cccancel.yml" "concurrency must be exactly group: sec-pipeline"
  # The workflow-level env sets only the names sec.yml uses.
  ordwf "$o/wenvbash.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^jobs:$|env:\n  SEC_REGISTRY_OWNER: runcove-sec\n  BASH_ENV: x\njobs:|' "$o/wenvbash.yml"
  ocase_ "order: BASH_ENV in the workflow-level env" 1 "$o/wenvbash.yml" "the workflow-level env sets BASH_ENV"
  ordwf "$o/wenvx.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^jobs:$|env:\n  sec_registry_owner: x\njobs:|' "$o/wenvx.yml"
  ocase_ "order: another name in the workflow-level env" 1 "$o/wenvx.yml" "the workflow-level env sets sec_registry_owner"
  ordwf "$o/wenvexpr.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i "s|^jobs:\$|env: \"\${{ fromJSON('{}') }}\"\njobs:|" "$o/wenvexpr.yml"
  ocase_ "order: the workflow-level env as an expression" 1 "$o/wenvexpr.yml" "the workflow-level env is not a plain mapping"
  ordwf "$o/wenvok.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^jobs:$|env:\n  SEC_REGISTRY_OWNER: runcove-sec\n  SEC_REGISTRY_USER: u\njobs:|' "$o/wenvok.yml"
  ocase_ "order: the two allowed names in the workflow-level env pass" 0 "$o/wenvok.yml"
  ordwf "$o/cowith.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^          persist-credentials: false$|          persist-credentials: true|' "$o/cowith.yml"
  ocase_ "order: the guard checkout keeps credentials" 1 "$o/cowith.yml" "must have with: persist-credentials: false"
  ordwf "$o/cowith2.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^          persist-credentials: false$|          persist-credentials: false\n          ref: x|' "$o/cowith2.yml"
  ocase_ "order: the guard checkout has another with: key" 1 "$o/cowith2.yml" "must have with: persist-credentials: false"
  ocase_ "order: the real sec.yml passes" 0 "$ROOT/.forgejo/workflows/sec.yml"
  ordwf "$o/wenvown.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^jobs:$|env:\n  SEC_REGISTRY_OWNER: someone-else\njobs:|' "$o/wenvown.yml"
  ocase_ "order: SEC_REGISTRY_OWNER with another value" 1 "$o/wenvown.yml" "SEC_REGISTRY_OWNER must be exactly runcove-sec"
  ordwf "$o/wenvusr.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^jobs:$|env:\n  SEC_REGISTRY_USER: "Bad User;x"\njobs:|' "$o/wenvusr.yml"
  ocase_ "order: SEC_REGISTRY_USER that is no plain login" 1 "$o/wenvusr.yml" "SEC_REGISTRY_USER must be a plain lower-case login"
  ordwf "$o/jenvown.yml" "" "    needs: [runner-guard]\n" "    needs: [privacy]\n"
  sed -i 's|^  test:$|  test:\n    env: {SEC_REGISTRY_OWNER: x}|' "$o/jenvown.yml"
  ocase_ "order: a job re-sets SEC_REGISTRY_OWNER" 1 "$o/jenvown.yml" "job test sets SEC_REGISTRY_OWNER"

  # Supply chain and host privilege in sec.yml (rule 6): every `uses:` a commit or digest, every
  # image a digest, only allow-listed options outside `image`.
  local SHA=11d5960a326750d5838078e36cf38b85af677262 DG=sha256:e3d24f6906d0760bf4da0dae872415391ededefad23fd471c47dc53b0ddf3c42
  local DIND=docker:28-dind@sha256:2a232a42256f70d78e3cc5d2b5d6b3276710a0de0596c145f627ecfae90282ac
  # supwf <dir> <job id> <runs-on> <extra job lines, printf %b> <step line>
  supwf() {
    mkdir -p "$1"
    printf 'name: s\n"on": push\njobs:\n  %s:\n    runs-on: %s\n%b    steps:\n      - %s\n' "$2" "$3" "$4" "$5" >"$1/sec.yml"
  }
  d=$tmp/sp_ok
  supwf "$d" test runcove-sec-unpriv "    services:\n      postgres:\n        image: postgres:18@$DG\n        options: --health-cmd=pg_isready\n" "uses: actions/checkout@$SHA"
  case_ "supply: a sha-pinned action, a digest image and a plain options value pass" 0 "$d"
  d=$tmp/sp_dock
  supwf "$d" s1 runcove-sec-unpriv "" "uses: docker://alpine@sha256:${DG#sha256:}"
  case_ "supply: a docker:// digest action passes" 0 "$d"
  local u
  for u in "someone/evil@main" "actions/checkout@v4" "./.forgejo/actions/x" "actions/checkout@${SHA:0:39}" "actions/checkout@${SHA^^}" "docker://alpine:3" "actions/checkout"; do
    d=$tmp/sp_u_${u//[^A-Za-z0-9]/_}
    supwf "$d" s1 runcove-sec-unpriv "" "uses: $u"
    case_ "supply: uses: $u" 1 "$d" "is not owner/repo@<40 hex commit>"
  done
  d=$tmp/sp_img
  supwf "$d" test runcove-sec-unpriv "    services:\n      postgres:\n        image: postgres:18\n" "run: echo"
  case_ "supply: a service image by tag" 1 "$d" "image is not pinned by an @sha256: digest"
  d=$tmp/sp_cimg
  supwf "$d" s1 runcove-sec-unpriv "    container: node:22\n" "run: echo"
  case_ "supply: a container image by tag" 1 "$d" "image is not pinned by an @sha256: digest"
  local opt
  for opt in "--privileged" "-v /var/run/docker.sock:/var/run/docker.sock" "--volume=/:/host" "--volume /:/host" "--mount type=bind,src=/,dst=/h" "--cap-add SYS_ADMIN" "--device /dev/kvm" "--pid=host" "--network=host" "--net host" "--security-opt seccomp=unconfined" "--userns=host" "--ipc=host" "--health-cmd x -v/x:/y" "--runtime runc" "--publish 80:80" "-P" "--sysctl a=b" "--tmpfs /t" "--group-add 0" "--add-host a:1.2.3.4" "--env-file /e" "--link x" "--health-cmd=x --init" "\${{ vars.OPTS }}" "--memory \${{ env.M }}" "--memory=\${{vars.M}}" "--cpus --privileged" "--memory 1g" "--cpus=" "--cpus=2  --memory=1g" "--cpus=\"2\"" "--cpus='2'"; do
    d=$tmp/sp_o_${opt//[^A-Za-z0-9]/_}
    supwf "$d" test runcove-sec-unpriv "    services:\n      postgres:\n        image: postgres:18@$DG\n        options: $opt\n" "run: echo"
    case_ "supply: service options $opt" 1 "$d" "options"
  done
  # Values YAML decodes to characters a shell-word splitter would treat differently: written as
  # double-quoted YAML scalars so the file holds the escape and the parsed value holds the character.
  local ctl
  for ctl in '--cpus=2\t--memory=1g' '--cpus=2\n--privileged' '--cpus=2\r--privileged' '--cpus=2\v--privileged' '--cpus=2\f--privileged' '--cpus=2\\--privileged' '--cpus=2\x00' '--cpus=2 --privileged' '--cpus=2 --memory=é1g'; do
    d=$tmp/sp_c_${ctl//[^A-Za-z0-9]/_}
    mkdir -p "$d"
    printf 'name: s\n"on": push\njobs:\n  test:\n    runs-on: runcove-sec-unpriv\n    services:\n      postgres:\n        image: postgres:18@%s\n        options: "%s"\n    steps:\n      - run: echo\n' "$DG" "$ctl" >"$d/sec.yml"
    case_ "supply: options with the escape $ctl" 1 "$d" "options"
  done
  # Each options rule pinned by a case that fails when that rule alone is dropped: the case
  # asserts the rule's own message, which names it (another rule may still refuse the input).
  local spec want
  for spec in \
    "--health-cmd --memory=1g|[rule: --flag=value]" \
    "--health-cmd=a,b|[rule: plain value]" \
    "--pid=host|[rule: allowed flags]" \
    "--cpus=|[rule: plain value]"; do
    opt=${spec%%|*} want=${spec#*|}
    d=$tmp/sp_r_${opt//[^A-Za-z0-9]/_}
    supwf "$d" test runcove-sec-unpriv "    services:\n      postgres:\n        image: postgres:18@$DG\n        options: $opt\n" "run: echo"
    case_ "supply: options $opt is refused by $want" 1 "$d" "services.postgres: \`options\` has '${opt%% *}'"
    case_ "supply: options $opt names $want" 1 "$d" "$want"
  done
  d=$tmp/sp_r_tab
  mkdir -p "$d"
  printf 'name: s\n"on": push\njobs:\n  test:\n    runs-on: runcove-sec-unpriv\n    services:\n      postgres:\n        image: postgres:18@%s\n        options: "--cpus=2\\t--memory=1g"\n    steps:\n      - run: echo\n' "$DG" >"$d/sec.yml"
  case_ "supply: options with a tab is refused by the character rule" 1 "$d" "services.postgres: \`options\` holds a character other than plain printable ASCII without quotes or backslashes [rule: characters]"
  # A job's container mapping: only image, env and options, in exact lower case; every job.
  d=$tmp/sp_ck_ok
  supwf "$d" s1 runcove-sec-unpriv "    container:\n      image: node:22@$DG\n      env:\n        A: b\n      options: --cpus=2\n" "run: echo"
  case_ "supply: a container with image, env and options passes" 0 "$d"
  local ck
  for ck in "credentials: {username: u, password: p}" "volumes: [\"/x:/y\"]" "ports: [\"80:80\"]" "network: host" "entrypoint: sh" "cmd: x" "Image: node:22@$DG" "OPTIONS: --privileged" "Env: {A: b}" "\"1\": x"; do
    d=$tmp/sp_ck_${ck//[^A-Za-z0-9]/_}
    supwf "$d" s1 runcove-sec-unpriv "    container:\n      image: node:22@$DG\n      $ck\n" "run: echo"
    ck=${ck%%:*}
    case_ "supply: container key $ck" 1 "$d" "container has \`${ck//\"/}\`, which is not one of image, env, options (exact lower case) [rule: container keys]"
  done
  d=$tmp/sp_ck_int
  supwf "$d" s1 runcove-sec-unpriv "    container:\n      image: node:22@$DG\n      1: x\n" "run: echo"
  case_ "supply: container key 1 (an int key)" 1 "$d" "[rule: string keys]"
  d=$tmp/sp_ck_img
  supwf "$d" image runcove-sec "    container:\n      image: node:22@$DG\n      options: --privileged\n      OPTIONS: --privileged\n" "run: echo"
  case_ "supply: the image job's container is held to the keys too" 1 "$d" "container has \`OPTIONS\`"
  d=$tmp/sp_twoflags
  supwf "$d" test runcove-sec-unpriv "    services:\n      postgres:\n        image: postgres:18@$DG\n        options: --cpus=2 --memory=1g\n" "run: echo"
  case_ "supply: --cpus=2 --memory=1g passes" 0 "$d"
  d=$tmp/sp_tmpl
  supwf "$d" test runcove-sec-unpriv "    services:\n      postgres:\n        image: postgres:18@$DG\n        options: --memory=\${{vars.M}}\n" "run: echo"
  case_ "supply: a templated value on an allowed flag" 1 "$d" "is templated"
  d=$tmp/sp_optok
  supwf "$d" test runcove-sec-unpriv "    services:\n      postgres:\n        image: postgres:18@$DG\n        options: --health-cmd=pg_isready --health-interval=5s --health-retries=5 --memory=2g --cpus=2\n" "run: echo"
  case_ "supply: the allow-listed health and limit options pass" 0 "$d"
  d=$tmp/sp_ports
  supwf "$d" test runcove-sec-unpriv "    services:\n      postgres:\n        image: postgres:18@$DG\n        ports: [\"5432:5432\"]\n" "run: echo"
  case_ "supply: service ports" 1 "$d" "has \`ports\`"
  # `ports` is refused on any service, in the `image` job too (a service holds only image, env and options).
  d=$tmp/sp_imgports
  supwf "$d" image runcove-sec "    services:\n      dind:\n        image: $DIND\n        options: --privileged\n        ports: [\"2375:2375\"]\n" "run: echo"
  case_ "supply: the image job's dind service is held to the keys too (ports)" 1 "$d" "services.dind has \`ports\`"
  d=$tmp/sp_copt
  supwf "$d" s1 runcove-sec-unpriv "    container:\n      image: node:22@$DG\n      options: --privileged\n" "run: echo"
  case_ "supply: container options --privileged" 1 "$d" "container: \`options\` has '--privileged'"
  d=$tmp/sp_vol
  supwf "$d" s1 runcove-sec-unpriv "    container:\n      image: node:22@$DG\n      volumes: [\"/var/run/docker.sock:/var/run/docker.sock\"]\n" "run: echo"
  case_ "supply: container volumes" 1 "$d" "has \`volumes\`"
  d=$tmp/sp_imgjob
  supwf "$d" image runcove-sec "    services:\n      dind:\n        image: $DIND\n        options: --privileged\n        env:\n          DOCKER_TLS_CERTDIR: \"\"\n" "uses: actions/checkout@$SHA"
  case_ "supply: the image job may run dind --privileged at its pinned digest" 0 "$d"

  # --privileged has exactly one home: the dind service of the `image` job at DIND_IMAGE.
  d=$tmp/sp_dind_other_digest
  supwf "$d" image runcove-sec "    services:\n      dind:\n        image: docker:28-dind@$DG\n        options: --privileged\n" "run: echo"
  case_ "supply: dind --privileged at another digest" 1 "$d" "services.dind: \`options\` has '--privileged', which is not an allowed flag"
  d=$tmp/sp_dind_other_opt
  supwf "$d" image runcove-sec "    services:\n      dind:\n        image: $DIND\n        options: --privileged --cpus=2\n" "run: echo"
  case_ "supply: the pinned dind carries only --privileged" 1 "$d" "the one privileged service may carry only \`--privileged\` [rule: dind options]"
  local jb
  for jb in test test-sqlite core-bare; do
    d=$tmp/sp_pg_priv_$jb
    supwf "$d" "$jb" runcove-sec-unpriv "    services:\n      postgres:\n        image: postgres:18@$DG\n        options: --privileged\n" "run: echo"
    case_ "supply: postgres under $jb with --privileged" 1 "$d" "services.postgres: \`options\` has '--privileged', which is not an allowed flag"
  done
  d=$tmp/sp_pg_priv_image
  supwf "$d" image runcove-sec "    services:\n      postgres:\n        image: postgres:18@$DG\n        options: --privileged\n" "run: echo"
  case_ "supply: postgres under image with --privileged" 1 "$d" "service \`postgres\` is not allowed (this job may start only dind)"
  d=$tmp/sp_ctr_priv_image
  supwf "$d" image runcove-sec "    container:\n      image: node:22@$DG\n      options: --privileged\n" "run: echo"
  case_ "supply: a container with --privileged, even in the image job" 1 "$d" "container: \`options\` has '--privileged'"
  d=$tmp/sp_dind_in_test
  supwf "$d" test runcove-sec-unpriv "    services:\n      dind:\n        image: $DIND\n        options: --privileged\n" "run: echo"
  case_ "supply: dind under a job other than image" 1 "$d" "service \`dind\` is not allowed (this job may start only postgres)"
  case_ "supply: dind under a job other than image, --privileged too" 1 "$d" "services.dind: \`options\` has '--privileged'"

  # services: only where sec.yml uses it today (SEC_SERVICES); the same keys as a container.
  local sv_msg="the private workflow's jobs may start no service containers except test: postgres; test-sqlite: postgres; core-bare: postgres; image: dind [rule: services]"
  d=$tmp/sp_sv_none
  supwf "$d" s1 runcove-sec-unpriv "" "run: echo"
  case_ "services: a job without services passes" 0 "$d"
  d=$tmp/sp_sv_real
  supwf "$d" core-bare runcove-sec-unpriv "    services:\n      postgres:\n        image: postgres:18@$DG\n        env:\n          POSTGRES_HOST_AUTH_METHOD: trust\n" "run: echo"
  case_ "services: postgres under core-bare, as sec.yml has it, passes" 0 "$d"
  d=$tmp/sp_sv_image
  supwf "$d" s1 runcove-sec-unpriv "    services:\n      postgres:\n        image: postgres:18@$DG\n" "run: echo"
  case_ "services: a service with an image on a job not in the table" 1 "$d" "\`services\` on job s1: $sv_msg"
  d=$tmp/sp_sv_empty
  supwf "$d" s1 runcove-sec-unpriv "    services: {}\n" "run: echo"
  case_ "services: an empty mapping on a job not in the table" 1 "$d" "\`services\` on job s1: $sv_msg"
  d=$tmp/sp_sv_null
  supwf "$d" s1 runcove-sec-unpriv "    services:\n" "run: echo"
  case_ "services: a null value on a job not in the table" 1 "$d" "\`services\` on job s1: $sv_msg"
  local sk
  for sk in Services SERVICES sErvices; do
    d=$tmp/sp_sv_case_$sk
    supwf "$d" s1 runcove-sec-unpriv "    $sk:\n      postgres:\n        image: postgres:18@$DG\n" "run: echo"
    case_ "services: the key written $sk on a job not in the table" 1 "$d" "\`$sk\` on job s1: $sv_msg"
    d=$tmp/sp_sv_case_ok_$sk
    supwf "$d" test runcove-sec-unpriv "    $sk:\n      postgres:\n        image: postgres:18@$DG\n" "run: echo"
    case_ "services: the key written $sk on an allowed job" 1 "$d" "job test has \`$sk\`, which must be written exactly \`services\` (lower case) [rule: services]"
  done
  d=$tmp/sp_sv_emptycase
  supwf "$d" s1 runcove-sec-unpriv "    Services: {}\n" "run: echo"
  case_ "services: an empty mis-cased mapping on a job not in the table" 1 "$d" "\`Services\` on job s1: $sv_msg"
  d=$tmp/sp_sv_othername
  supwf "$d" test runcove-sec-unpriv "    services:\n      redis:\n        image: redis:7@$DG\n" "run: echo"
  case_ "services: another service name under an allowed job" 1 "$d" "job test: service \`redis\` is not allowed (this job may start only postgres)"
  d=$tmp/sp_sv_extra
  supwf "$d" test runcove-sec-unpriv "    services:\n      postgres:\n        image: postgres:18@$DG\n      Postgres:\n        image: postgres:18@$DG\n" "run: echo"
  case_ "services: a mis-cased service name beside the allowed one" 1 "$d" "job test: service \`Postgres\` is not allowed"
  d=$tmp/sp_sv_notmap
  supwf "$d" test runcove-sec-unpriv "    services:\n      postgres: postgres:18@$DG\n" "run: echo"
  case_ "services: a service that is a bare string" 1 "$d" "job test: service \`postgres\` is not a mapping"
  d=$tmp/sp_sv_listval
  supwf "$d" test runcove-sec-unpriv "    services: [postgres]\n" "run: echo"
  case_ "services: the key is not a mapping on an allowed job" 1 "$d" "job test: \`services\` is not a mapping [rule: services]"
  for sk in "credentials: {username: u, password: p}" "volumes: [\"/x:/y\"]" "ports: [\"5432:5432\"]" "network: host" "entrypoint: sh" "cmd: x" "Image: postgres:18@$DG" "OPTIONS: --cpus=2" "Env: {A: b}" "\"1\": x"; do
    d=$tmp/sp_sv_key_${sk//[^A-Za-z0-9]/_}
    supwf "$d" test runcove-sec-unpriv "    services:\n      postgres:\n        image: postgres:18@$DG\n        $sk\n" "run: echo"
    sk=${sk%%:*}
    case_ "services: a service key $sk" 1 "$d" "job test: services.postgres has \`${sk//\"/}\`, which is not one of image, env, options (exact lower case) [rule: service keys]"
  done
  d=$tmp/sp_sv_key_int
  supwf "$d" test runcove-sec-unpriv "    services:\n      postgres:\n        image: postgres:18@$DG\n        1: x\n" "run: echo"
  case_ "services: a service key 1 (an int key)" 1 "$d" "[rule: string keys]"

  # --- refusals: explicit YAML tags and mapping keys that are not strings ---
  # Workflow files may not use explicit YAML tags (on a key or a value) or non-string mapping keys.
  # The base64 words below are built here from plain key names, so no opaque blob is pasted.
  local tg="[rule: no explicit tags]" sk_rule="[rule: string keys]" b64_services b64_container b64_uses
  b64_services=$(printf services | base64)
  b64_container=$(printf container | base64)
  b64_uses=$(printf uses | base64)
  d=$tmp/tag_services
  supwf "$d" s1 runcove-sec-unpriv "    !!binary $b64_services:\n      redis:\n        image: redis:7\n" "run: echo"
  case_ "tags: a !!binary key spelling services on a sec.yml job" 1 "$d" "$tg"
  d=$tmp/tag_container
  supwf "$d" s1 runcove-sec-unpriv "    !!binary $b64_container: node:22\n" "run: echo"
  case_ "tags: a !!binary key spelling container on a sec.yml job" 1 "$d" "$tg"
  d=$tmp/tag_uses
  supwf "$d" s1 runcove-sec-unpriv "" "!!binary $b64_uses: actions/checkout@v4"
  case_ "tags: a !!binary key spelling uses in a sec.yml step" 1 "$d" "$tg"
  d=$tmp/tag_local
  pubwf "$d" ci.yml "if: !local \"$G\""
  case_ "tags: a !local tag on a value" 1 "$d" "$tg"
  d=$tmp/tag_str
  pubwf "$d" ci.yml "if: !!str \"$G\""
  case_ "tags: a !!str tag (any explicit tag is refused)" 1 "$d" "$tg"
  d=$tmp/tag_map
  pubwf "$d" ci.yml "if: \"$G\"" "env: !!map {A: b}"
  case_ "tags: a !!map tag on a mapping" 1 "$d" "$tg"
  d=$tmp/tag_bare
  pubwf "$d" ci.yml "if: ! \"$G\""
  case_ "tags: the bare ! tag" 1 "$d" "$tg"
  d=$tmp/tag_ghkey
  mkdir -p "$d"
  printf 'name: x\n"on": push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    if: "%s"\n    !!binary %s: [x]\n    steps:\n      - run: echo\n' "$H" "$(printf runs-on | base64)" >"$d/publish.yml"
  gcase_ "tags: a !!binary key in a GitHub workflow" 1 "$d" "$tg"
  d=$tmp/key_seq
  pubwf "$d" ci.yml "if: \"$G\"" "? [a]
    : x"
  case_ "keys: a sequence as a mapping key" 1 "$d" "$sk_rule"
  d=$tmp/key_int
  pubwf "$d" ci.yml "if: \"$G\"" "env: {1: x}"
  case_ "keys: an int mapping key" 1 "$d" "$sk_rule"
  d=$tmp/key_null
  pubwf "$d" ci.yml "if: \"$G\"" "~: x"
  case_ "keys: a null mapping key" 1 "$d" "$sk_rule"
  d=$tmp/key_true
  pubwf "$d" ci.yml "if: \"$G\"" "true: x"
  case_ "keys: a boolean key other than a plain on" 1 "$d" "$sk_rule"
  d=$tmp/key_on
  mkdir -p "$d"
  printf 'name: x\non: push\njobs:\n  a:\n    runs-on: docker-light\n    if: "%s"\n    env: {"1": x}\n    steps:\n      - run: echo\n' "$G" >"$d/ci.yml"
  case_ "keys: a plain on: and a quoted \"1\" key pass" 0 "$d" "1 workflow files"

  # Local actions (rule 4 reaches them too): no action.yml/action.yaml names SEC_RUNNER_NAME.
  acase_() { # <name> <want rc> <root> [<text>]
    local out rc
    out=$(check_actions "$3" 2>&1)
    rc=$?
    if [ "$rc" != "$2" ]; then
      printf '  FAIL  %s (rc=%s, want %s)\n%s\n' "$1" "$rc" "$2" "$(printf '%s\n' "$out" | sed 's/^/        /')"; fail=1
    elif [ -n "${4:-}" ] && ! grep -qF -- "$4" <<< "$out"; then
      printf '  FAIL  %s (rc ok, but the output lacks "%s")\n%s\n' "$1" "$4" "$(printf '%s\n' "$out" | sed 's/^/        /')"; fail=1
    else
      printf '  ok    %s\n' "$1"
    fi
  }
  local a=$tmp/act
  mkdir -p "$a/none"
  acase_ "actions: a tree with no local action passes" 0 "$a/none"
  mkdir -p "$a/env/.forgejo/actions/setup"
  printf 'name: setup\nruns:\n  using: composite\n  steps:\n    - run: echo hi\n      shell: bash\n      env:\n        SEC_RUNNER_NAME: dedicated-priv-1\n' >"$a/env/.forgejo/actions/setup/action.yml"
  acase_ "actions: a composite action.yml setting it in a step env" 1 "$a/env" ".forgejo/actions/setup/action.yml: names SEC_RUNNER_NAME"
  mkdir -p "$a/ghenv/x"
  printf 'name: x\nruns:\n  using: composite\n  steps:\n    - run: echo "Sec_Runner_Name=c01" >> "$GITHUB_ENV"\n      shell: bash\n' >"$a/ghenv/x/action.yaml"
  acase_ "actions: an action.yaml writing it to GITHUB_ENV" 1 "$a/ghenv" "x/action.yaml: names SEC_RUNNER_NAME"
  mkdir -p "$a/comment/x"
  printf '# SEC_RUNNER_NAME comes from the runner\nname: x\nruns:\n  using: composite\n  steps:\n    - run: echo hi\n      shell: bash\n' >"$a/comment/x/action.yml"
  acase_ "actions: naming it only in a comment passes" 0 "$a/comment" "1 local action file"
  mkdir -p "$a/junk/x"
  printf 'runs: [unclosed\n' >"$a/junk/x/action.yml"
  acase_ "actions: an action.yml that does not parse" 1 "$a/junk" "does not parse"
  mkdir -p "$a/link/x"
  ln -s ../../env/.forgejo/actions/setup/action.yml "$a/link/x/action.yml"
  acase_ "actions: a symlinked action.yml" 1 "$a/link" "is a symlink"
  mkdir -p "$a/upper/X"
  printf 'runs:\n  env: {SEC_RUNNER_NAME: x}\n' >"$a/upper/X/Action.YML"
  acase_ "actions: the file name in another letter case" 1 "$a/upper" "names SEC_RUNNER_NAME"
  mkdir -p "$a/nm/node_modules/x"
  printf 'runs:\n  env: {SEC_RUNNER_NAME: x}\n' >"$a/nm/node_modules/x/action.yml"
  acase_ "actions: one under node_modules is read too" 1 "$a/nm" "node_modules/x/action.yml: names SEC_RUNNER_NAME"
  mkdir -p "$a/venv/.venv/x"
  printf 'runs:\n  env: {SEC_RUNNER_NAME: x}\n' >"$a/venv/.venv/x/action.yml"
  acase_ "actions: one under .venv is read too" 1 "$a/venv" ".venv/x/action.yml: names SEC_RUNNER_NAME"

  echo "----"
  if [ "$fail" -eq 0 ]; then
    echo "$SELF selftest: ALL PASS"
    return 0
  fi
  echo "$SELF selftest: FAILURES ABOVE"
  return 2
}

# run_checks — the real tree: both workflow directories, sec.yml's order, local actions.
run_checks() {
  local rc1 rc2 rc3 rc4 rc=0 r
  check_dir "$ROOT/.forgejo/workflows" forgejo
  rc1=$?
  check_dir "$ROOT/.github/workflows" github
  rc2=$?
  check_sec_order "$ROOT/.forgejo/workflows/sec.yml"
  rc3=$?
  check_actions "$ROOT"
  rc4=$?
  for r in "$rc1" "$rc2" "$rc3" "$rc4"; do [ "$r" -gt "$rc" ] && rc=$r; done
  return "$rc"
}

case "${1:-}" in
  --install-uv)
    install_uv "${2:-}"
    exit $?
    ;;
  --pinned-python)
    pinned_python || exit 3
    run_checks
    exit $?
    ;;
  --selftest)
    selftest
    exit $?
    ;;
  "")
    run_checks
    exit $?
    ;;
  --dir | --github-dir)
    [ -n "${2:-}" ] || {
      echo "$USAGE" >&2
      exit 3
    }
    if [ "$1" = "--dir" ]; then check_dir "$2" forgejo; else check_dir "$2" github; fi
    exit $?
    ;;
  -h | --help)
    sed -n '2,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *)
    echo "$USAGE" >&2
    exit 3
    ;;
esac
