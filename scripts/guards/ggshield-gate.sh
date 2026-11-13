#!/usr/bin/env bash
# scripts/guards/ggshield-gate.sh — run ggshield over the staged diff and map
# its exit code to a decision this repo can live with.
#
# WHY A WRAPPER. ggshield has three outcomes and they must not be conflated:
# a finding (block the commit), a clean scan (allow it), and "could not scan"
# (no ggshield installed, no API key, no network, quota exhausted). Only a
# clean scan lets a commit through. A finding refuses it, and so does "could
# not scan": a gate that passes when it cannot look reads as a scanned repo
# when it is not. The way out is explicit, never silent: GGSHIELD_SKIP=1 lets
# one commit (or a run of them, offline) through and says loudly that nothing
# was scanned.
#
# THE SCAN USES ONLY ITS OWN FIXED SETTINGS: the environment variable
# GITGUARDIAN_DONT_LOAD_ENV=1, the default dashboard as --instance, and
# --config-path on an empty file of its own, which "ignores local and global
# config files" (ggshield --help). The instance variables are unset for the
# call. Measured with ggshield 1.56.1 and a dummy key: the machine's stored
# authentication still works under these settings (api-status healthy).
#
# THE ggshield IT RUNS is the one on PATH, and the script refuses one that
# resolves inside this repository's working tree (a repo virtualenv, a bin
# directory a tool adds): the scan is run by a program from outside the tree.
#
# SKIPPING is exactly GGSHIELD_SKIP=1. Any other value (0, empty, yes) scans.
#
# CI does not run this script or ggshield; it runs the guard selftests and the
# whole-tree scan for credentials on a command line. lefthook's own skips
# (LEFTHOOK=0, git commit --no-verify) are outside this gate.
#
# MEASURED on the management machine, 22 Sep 2026 (ij8.76), against ggshield 1.54.0:
#   * clean staged diff                    -> rc 0, "No secrets have been found"
#   * a throwaway RSA private key staged   -> rc 1, "1 secret detected"
#   * outside a git repo                   -> rc != 0 with "Not a git directory"
#   * `ggshield api-status`                -> healthy: ggshield IS
#     authenticated on this machine; ij8.2's
#     "needs a key from Jeremy" is about the scan-every-publish path, not
#     about this host being unable to scan at all.
#
# AND A TRAP WORTH KEEPING: a fabricated credential is not a positive control.
# A random `ghp_`+36 string and the canonical AWS documentation key both
# scanned CLEAN — GitGuardian checksum-validates GitHub tokens and allowlists
# the AWS example. The only fixture that proved the detector alive was a real
# (throwaway, never-installed) RSA private key. If you ever need to re-prove
# this gate against the live service, use `openssl genrsa`, not a made-up
# token, or you will "prove" a blind scanner works.
#
# Quota: the GitGuardian workspace's allowance is 10,000 documents a month and
# the daily transcript scan already wants it. One staged diff per commit is
# small, but GGSHIELD_SKIP=1 exists for a session that is about to make many
# commits offline.
#
# Usage:  scripts/guards/ggshield-gate.sh            # lefthook pre-commit
#         scripts/guards/ggshield-gate.sh --selftest # rc mapping, with a stub
set -uo pipefail
SELF="ggshield-gate"
CFG_FILE=""
INSTANCE="https://dashboard.gitguardian.com"

run_gate() {
  if [ "${GGSHIELD_SKIP:-0}" = "1" ]; then
    echo "$SELF: SKIP (GGSHIELD_SKIP=1) — NOTHING was scanned for secrets"
    return 0
  fi
  if ! command -v ggshield >/dev/null 2>&1; then
    echo "$SELF: REFUSED — ggshield is not installed, so NOTHING was scanned for secrets." >&2
    echo "  Install it, or opt out for this commit with GGSHIELD_SKIP=1." >&2
    return 1
  fi

  local gg top real
  gg=$(command -v ggshield)
  real=$(readlink -f -- "$gg" 2>/dev/null || printf '%s' "$gg")
  top=$(git rev-parse --show-toplevel 2>/dev/null || true)
  if [ -n "$top" ]; then
    top=$(readlink -f -- "$top" 2>/dev/null || printf '%s' "$top")
    case "$real" in
      "$top"/*)
        echo "$SELF: REFUSED — ggshield resolves inside the repository working tree, so NOTHING was scanned." >&2
        echo "  Put ggshield on PATH from outside the tree, or opt out for this commit with GGSHIELD_SKIP=1." >&2
        return 1
        ;;
    esac
  fi

  local out rc cfg
  cfg=$(mktemp) || { echo "$SELF: REFUSED — no temporary file; NOTHING was scanned." >&2; return 1; }
  CFG_FILE=$cfg
  # Removed on any exit; this script sets no other EXIT trap.
  trap 'rm -f "$CFG_FILE"' EXIT
  # No pipe: a pipeline's rc is the LAST command's, so `ggshield … | head`
  # reports head's success and a finding reads as clean.
  # --instance outranks GITGUARDIAN_INSTANCE and GITGUARDIAN_API_URL (measured);
  # both are also unset for the call.
  out=$(env -u GITGUARDIAN_INSTANCE -u GITGUARDIAN_API_URL GITGUARDIAN_DONT_LOAD_ENV=1 \
    ggshield --config-path "$cfg" --instance "$INSTANCE" secret scan pre-commit 2>&1)
  rc=$?
  rm -f "$cfg"

  case "$rc" in
    0)
      echo "$SELF: clean"
      return 0
      ;;
    1)
      printf '%s\n' "$out"
      echo "$SELF: REFUSED — ggshield found a secret in the staged diff (above)." >&2
      echo "Remove it from the change. If it is a real credential that reached a file," >&2
      echo "AGENTS.md's leak policy applies: vault it, redact every copy on this machine," >&2
      echo "and rotate it only if it left the homelab." >&2
      return 1
      ;;
    *)
      printf '%s\n' "$out" | sed 's/^/  /'
      echo "$SELF: REFUSED (rc=$rc, could not scan) — NOTHING was scanned for secrets." >&2
      echo "  Fix the cause (key, network, quota), or opt out for this commit with GGSHIELD_SKIP=1." >&2
      return 1
      ;;
  esac
}

selftest() {
  local fail=0 tmp stub_dir out rc
  tmp=$(mktemp -d)
  stub_dir="$tmp/bin"
  mkdir -p "$stub_dir" "$tmp/empty"
  trap 'rm -rf "$tmp"' RETURN

  # A stub ggshield whose exit code we choose: this proves THIS script's rc
  # mapping without spending a single call on the real service. It also
  # records its arguments and the environment fact the script pins.
  cat >"$stub_dir/ggshield" <<'EOF'
#!/usr/bin/env bash
{
  echo "args: $*"
  echo "dont_load_env=${GITGUARDIAN_DONT_LOAD_ENV-unset}"
  echo "env_instance=${GITGUARDIAN_INSTANCE-unset} env_api_url=${GITGUARDIAN_API_URL-unset}"
} >"${GGSHIELD_STUB_LOG:-/dev/null}"
echo "stub ggshield: rc ${GGSHIELD_STUB_RC:-0}"
exit "${GGSHIELD_STUB_RC:-0}"
EOF
  chmod +x "$stub_dir/ggshield"

  local self log
  self="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  log="$tmp/stub.log"

  check() { # check <name> <got-rc> <want-rc> <output> <must-contain>
    if [ "$2" = "$3" ] && grep -qi -- "$5" <<< "$4"; then
      printf '  ok    %s\n' "$1"
    else
      printf '  FAIL  %s (rc=%s want %s, looking for "%s")\n' "$1" "$2" "$3" "$5"
      printf '%s\n' "$4" | sed 's/^/        /'
      fail=1
    fi
  }

  out=$(PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 env -u GGSHIELD_SKIP bash "$self" 2>&1); rc=$?
  check "rc 0 (clean) allows the commit" "$rc" 0 "$out" "clean"

  out=$(PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=1 env -u GGSHIELD_SKIP bash "$self" 2>&1); rc=$?
  check "rc 1 (finding) BLOCKS the commit" "$rc" 1 "$out" "REFUSED"

  local n
  for n in 2 3 128; do
    out=$(PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=$n env -u GGSHIELD_SKIP bash "$self" 2>&1); rc=$?
    check "rc $n (cannot scan) BLOCKS, and says nothing was scanned" "$rc" 1 "$out" "NOTHING was scanned"
    check "rc $n names the opt-out" "$rc" 1 "$out" "GGSHIELD_SKIP=1"
  done

  # PATH is the empty dir ALONE: with /usr/bin on it the real ggshield is
  # found and the case proves nothing (measured — it scanned for real and
  # reported "clean", which would have passed a weaker assertion).
  # "$BASH", not `bash`, and no `env`: with an empty PATH neither is findable.
  out=$(unset GGSHIELD_SKIP; PATH="$tmp/empty" "$BASH" "$self" 2>&1); rc=$?
  check "ggshield absent BLOCKS, and says nothing was scanned" "$rc" 1 "$out" "NOTHING was scanned"
  check "ggshield absent names the opt-out" "$rc" 1 "$out" "GGSHIELD_SKIP=1"

  out=$(PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=1 GGSHIELD_SKIP=1 bash "$self" 2>&1); rc=$?
  check "GGSHIELD_SKIP=1 allows, wins even over a finding" "$rc" 0 "$out" "SKIP"
  check "GGSHIELD_SKIP=1 says loudly that nothing was scanned" "$rc" 0 "$out" "NOTHING was scanned"
  out=$(PATH="$tmp/empty" GGSHIELD_SKIP=1 "$BASH" "$self" 2>&1); rc=$?
  check "GGSHIELD_SKIP=1 allows with ggshield absent" "$rc" 0 "$out" "NOTHING was scanned"

  # The scan's settings are its own: the stub records what ggshield was called with.
  : >"$log"
  out=$(PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 GGSHIELD_STUB_LOG="$log" env -u GGSHIELD_SKIP -u GITGUARDIAN_DONT_LOAD_ENV bash "$self" 2>&1); rc=$?
  check "the scan runs" "$rc" 0 "$out" "clean"
  check "the scan sets GITGUARDIAN_DONT_LOAD_ENV=1" 0 0 "$(cat "$log")" "dont_load_env=1"
  check "the scan passes --config-path" 0 0 "$(cat "$log")" "args: --config-path "
  check "the scan is a pre-commit scan" 0 0 "$(cat "$log")" "secret scan pre-commit"
  check "the scan pins the instance" 0 0 "$(cat "$log")" "--instance https://dashboard.gitguardian.com "

  # Foreign settings ignored: the instance variables and files in the working
  # directory name another host; the stub must see the fixed instance and
  # none of those values.
  local other="$tmp/other"
  mkdir -p "$other"
  printf 'instance: https://other.example\n' >"$other/.gitguardian.yaml"
  printf 'GITGUARDIAN_INSTANCE=https://other.example\nGITGUARDIAN_API_URL=https://other.example/v1\n' >"$other/.env"
  : >"$log"
  out=$(cd "$other" && PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 GGSHIELD_STUB_LOG="$log" \
    GITGUARDIAN_INSTANCE=https://other.example GITGUARDIAN_API_URL=https://other.example/v1 \
    env -u GGSHIELD_SKIP bash "$self" 2>&1); rc=$?
  check "the scan runs with foreign settings present" "$rc" 0 "$out" "clean"
  check "foreign settings ignored: the instance is the fixed one" 0 0 "$(cat "$log")" "--instance https://dashboard.gitguardian.com "
  check "foreign settings ignored: GITGUARDIAN_INSTANCE is unset for the call" 0 0 "$(cat "$log")" "env_instance=unset"
  check "foreign settings ignored: GITGUARDIAN_API_URL is unset for the call" 0 0 "$(cat "$log")" "env_api_url=unset"
  if grep -q other.example "$log"; then
    printf '  FAIL  a foreign value reached ggshield\n'; sed 's/^/        /' "$log"; fail=1
  else
    printf '  ok    no foreign value reached ggshield\n'
  fi
  local cfg
  cfg=$(sed -n 's/^args: --config-path \([^ ]*\) .*/\1/p' "$log")
  if [ -n "$cfg" ] && [ "$cfg" != ".gitguardian.yaml" ] && [ "${cfg#"$PWD"/}" = "$cfg" ]; then
    printf '  ok    the config path is not the working directory'"'"'s own file\n'
  else
    printf '  FAIL  the config path (%s) is empty or inside the working directory\n' "$cfg"
    fail=1
  fi
  if [ -n "$cfg" ] && [ ! -e "$cfg" ]; then
    printf '  ok    the temporary config file is gone after the run\n'
  else
    printf '  FAIL  the temporary config file (%s) is still there\n' "$cfg"
    fail=1
  fi

  # Only exactly 1 skips.
  local v
  for v in 0 "" yes true 11; do
    out=$(PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=1 GGSHIELD_SKIP="$v" bash "$self" 2>&1); rc=$?
    check "GGSHIELD_SKIP='$v' still scans (a finding blocks)" "$rc" 1 "$out" "REFUSED"
  done
  out=$(PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 GGSHIELD_SKIP=0 bash "$self" 2>&1); rc=$?
  check "GGSHIELD_SKIP=0 scans (a clean scan says clean, not SKIP)" "$rc" 0 "$out" "clean"

  # A ggshield that resolves inside the working tree is refused; one outside is used.
  local tree="$tmp/tree"
  mkdir -p "$tree/.venv/bin"
  git -C "$tree" init -q 2>/dev/null
  cp "$stub_dir/ggshield" "$tree/.venv/bin/ggshield"
  out=$(cd "$tree" && PATH="$tree/.venv/bin:$PATH" GGSHIELD_STUB_RC=0 env -u GGSHIELD_SKIP bash "$self" 2>&1); rc=$?
  check "a ggshield inside the working tree is refused" "$rc" 1 "$out" "inside the repository"
  check "…and nothing was scanned, with the opt-out named" "$rc" 1 "$out" "GGSHIELD_SKIP=1"
  out=$(cd "$tree" && PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 env -u GGSHIELD_SKIP bash "$self" 2>&1); rc=$?
  check "a ggshield outside the working tree is used" "$rc" 0 "$out" "clean"

  echo "----"
  if [ "$fail" -eq 0 ]; then
    echo "$SELF selftest: ALL PASS"
    return 0
  fi
  echo "$SELF selftest: FAILURES ABOVE"
  return 2
}

case "${1:-}" in
  --selftest) selftest ;;
  "") run_gate ;;
  *) echo "usage: $0 [--selftest]" >&2; exit 3 ;;
esac
