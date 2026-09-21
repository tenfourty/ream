#!/usr/bin/env bash
# scripts/guards/ggshield-gate.sh — run ggshield over the staged diff and map
# its exit code to a decision this repo can live with.
#
# WHY A WRAPPER. ggshield has three outcomes and they must not be conflated:
# a finding (block the commit), a clean scan (allow it), and "could not scan"
# — no ggshield installed, no API key, no network, quota exhausted. The third
# must NOT block: a commit gate that fails closed on a missing external
# service stops every commit on a machine that never had the tool, which is
# how guards get deleted. It must also not pass silently, or a repo whose key
# expired believes it is scanned when it is not. So: allow, and say loudly
# that nothing was scanned. CI runs the same scan with the dependency present
# and treats "could not scan" as a hard failure, which is where a blind gate
# is caught.
#
# MEASURED on baba, 22 Sep 2026 (ij8.76), against ggshield 1.54.0:
#   * clean staged diff                    -> rc 0, "No secrets have been found"
#   * a throwaway RSA private key staged   -> rc 1, "1 secret detected"
#   * outside a git repo                   -> rc != 0 with "Not a git directory"
#   * `ggshield api-status`                -> healthy, workspace 538208,
#     token scopes: scan. ggshield IS authenticated on this machine; ij8.2's
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
# the daily transcript scan already wants it (homelab-9yik). One staged diff
# per commit is small, but GGSHIELD_SKIP=1 exists for a session that is about
# to make many commits offline.
#
# Usage:  scripts/guards/ggshield-gate.sh            # lefthook pre-commit
#         scripts/guards/ggshield-gate.sh --selftest # rc mapping, with a stub
set -uo pipefail
SELF="ggshield-gate"

run_gate() {
  if [ "${GGSHIELD_SKIP:-0}" = "1" ]; then
    echo "$SELF: SKIP (GGSHIELD_SKIP=1) — nothing was scanned for secrets"
    return 0
  fi
  if ! command -v ggshield >/dev/null 2>&1; then
    echo "$SELF: SKIP (ggshield not installed) — nothing was scanned for secrets"
    return 0
  fi

  local out rc
  # No pipe: a pipeline's rc is the LAST command's, so `ggshield … | head`
  # reports head's success and a finding reads as clean (homelab-0z2).
  out=$(ggshield secret scan pre-commit 2>&1)
  rc=$?

  case "$rc" in
    0)
      echo "$SELF: clean"
      return 0
      ;;
    1)
      printf '%s\n' "$out"
      echo "$SELF: REFUSED — ggshield found a secret in the staged diff (above)." >&2
      echo "Remove it from the change. If it is a real credential that reached a file," >&2
      echo "CLAUDE.md's leak policy applies: vault it, redact every copy on this machine," >&2
      echo "and rotate it only if it left the homelab." >&2
      return 1
      ;;
    *)
      printf '%s\n' "$out" | sed 's/^/  /'
      echo "$SELF: SKIP (rc=$rc, could not scan) — NOTHING was scanned for secrets."
      echo "  CI runs this scan with the dependency present and fails on rc=$rc."
      return 0
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
  # mapping without spending a single call on the real service.
  cat >"$stub_dir/ggshield" <<'EOF'
#!/usr/bin/env bash
echo "stub ggshield: rc ${GGSHIELD_STUB_RC:-0}"
exit "${GGSHIELD_STUB_RC:-0}"
EOF
  chmod +x "$stub_dir/ggshield"

  local self
  self="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

  check() { # check <name> <got-rc> <want-rc> <output> <must-contain>
    if [ "$2" = "$3" ] && printf '%s' "$4" | grep -qi -- "$5"; then
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

  out=$(PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=2 env -u GGSHIELD_SKIP bash "$self" 2>&1); rc=$?
  check "rc 2 (cannot scan) allows, and says nothing was scanned" "$rc" 0 "$out" "NOTHING was scanned"

  out=$(PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=128 env -u GGSHIELD_SKIP bash "$self" 2>&1); rc=$?
  check "an unexpected rc allows, and says nothing was scanned" "$rc" 0 "$out" "NOTHING was scanned"

  # PATH is the empty dir ALONE: with /usr/bin on it the real ggshield is
  # found and the case proves nothing (measured — it scanned for real and
  # reported "clean", which would have passed a weaker assertion).
  # "$BASH", not `bash`, and no `env`: with an empty PATH neither is findable.
  out=$(unset GGSHIELD_SKIP; PATH="$tmp/empty" "$BASH" "$self" 2>&1); rc=$?
  check "ggshield absent -> SKIP, not a blocked commit" "$rc" 0 "$out" "not installed"

  out=$(PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=1 GGSHIELD_SKIP=1 bash "$self" 2>&1); rc=$?
  check "GGSHIELD_SKIP=1 wins even over a finding" "$rc" 0 "$out" "GGSHIELD_SKIP=1"

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
