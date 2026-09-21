#!/usr/bin/env bash
# scripts/guards/run-selftests.sh — run every guard's own selftest, plus the
# whole-tree scans and the lefthook dispatch regression.
#
# A guard that lives only in the clone that installed it is not a guard for an
# agent on another machine, so this runs in CI too (ij8.76). One difference
# there: CI has no lefthook, so it is invoked with --no-lefthook, which prints
# a loud NOT PROVEN line instead of silently passing. Pinning a lefthook
# binary in CI would close that, and is the one piece of this port left open.
#
# Usage: scripts/guards/run-selftests.sh [--no-lefthook]
set -uo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." >/dev/null 2>&1 && pwd)
cd "$ROOT" || exit 1

SKIP_LEFTHOOK=0
[ "${1:-}" = "--no-lefthook" ] && SKIP_LEFTHOOK=1

fails=0
run() { # run <label> <command...>
  local label="$1"; shift
  local out rc
  out=$("$@" 2>&1); rc=$?
  if [ "$rc" -eq 0 ]; then
    printf '✔ %s\n' "$label"
  else
    printf '✖ %s (rc=%s)\n' "$label" "$rc"
    printf '%s\n' "$out" | sed 's/^/    /'
    fails=$((fails + 1))
  fi
}

echo "== guard selftests =="
run "amend-guard --selftest"            scripts/guards/amend-guard.sh --selftest
run "push-count-guard --selftest"       scripts/guards/push-count-guard.sh --selftest
run "commit-msg-guard --selftest"       scripts/guards/commit-msg-guard.sh --selftest
run "secret-on-cmdline-guard --selftest" scripts/guards/secret-on-cmdline-guard.sh --selftest
run "ggshield-gate --selftest"          scripts/guards/ggshield-gate.sh --selftest

echo
echo "== whole-tree scans (the pre-commit hook only ever sees a staged diff) =="
run "secret-on-cmdline-guard --tree"    scripts/guards/secret-on-cmdline-guard.sh --tree

echo
echo "== lefthook dispatch =="
if [ "$SKIP_LEFTHOOK" -eq 1 ]; then
  echo "! prepush-hooks-fire NOT RUN (--no-lefthook): whether lefthook actually"
  echo "  dispatches the pre-push guards is UNPROVEN in this environment."
else
  run "prepush-hooks-fire"              scripts/tests/prepush-hooks-fire.sh
fi

echo
echo "== this clone =="
if [ -f .git/hooks/pre-commit ] && grep -q lefthook .git/hooks/pre-commit 2>/dev/null; then
  echo "✔ lefthook hooks are installed here"
else
  echo "! lefthook hooks are NOT installed in this clone — run: lefthook install"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "all guard selftests passed"
  exit 0
fi
echo "$fails guard check(s) FAILED"
exit 1
