#!/usr/bin/env bash
# scripts/guards/no-environ-guard.sh — refuse a read of the process environment in
# src/artifacts outside the modules that may make one.
#
# Every setting the SERVER reads goes through `artifacts.settings` (call-time, one object,
# `settings.override(...)` in tests; docs/agent/specs/2026-11-04-core-and-plugins-design.md 3.1).
# A stray `os.environ` is a setting nobody validates at start-up, nobody lists in the docs and
# no test can override.
#
# ALLOWED (by file name, in src/artifacts/):
#   settings.py   the one reader
#   serve.py      start-up switches (DATABASE_URL, ARTIFACTS_MAINTENANCE, ARTIFACTS_PACK_VERSIONS)
#   admin.py      the operator CLI (DATABASE_URL, SQLITE_TMPDIR, `env:NAME` DSN indirection)
#   client.py decide.py republish.py news.py
#                 the command-line tools' OWN variables (key file, session label, XDG dirs,
#                 wait timeout): they run on other machines with no server settings
# A single line may opt out with a trailing `# env-ok: <reason>`.
#
# Three invocations:
#   scripts/guards/no-environ-guard.sh            # staged files (lefthook pre-commit)
#   scripts/guards/no-environ-guard.sh --tree     # every tracked file (CI)
#   scripts/guards/no-environ-guard.sh --selftest
#
# Exit codes: 0 clean, 1 a read was found (naming every file:line), 2 --selftest failed, 3 usage.
set -uo pipefail
SELF="no-environ-guard"

ALLOWED=" settings.py serve.py admin.py client.py decide.py republish.py news.py "
PATTERN='os\.environ|os\.getenv|from os import .*(environ|getenv)|\bgetenv\('

# scan_paths <path>... — one line per offending file:line; returns 1 if any.
scan_paths() {
  local f hits=0 out base line
  for f in "$@"; do
    case "$f" in
      */src/artifacts/*.py | src/artifacts/*.py) ;;
      *) continue ;;
    esac
    [ -f "$f" ] || continue
    base="$(basename "$f")"
    case "$ALLOWED" in *" $base "*) continue ;; esac
    out="$(grep -nE -- "$PATTERN" "$f" 2>/dev/null | grep -v '# env-ok:')" || continue
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      printf '%s: environment read outside artifacts.settings — %s:%s\n' "$SELF" "$f" "$line"
      hits=$((hits + 1))
    done <<<"$out"
  done
  [ "$hits" -eq 0 ]
}

advice() {
  echo "$SELF: refusing — read it through artifacts.settings instead:" >&2
  echo "  add a field to Settings (settings.py: the dataclass and _ENV), then use" >&2
  echo "  settings.current().<field> at the point of use. A secret stays a file path." >&2
}

case "${1:-}" in
  --selftest)
    fail=0
    check() { # check <name> <got-rc> <want-rc>
      if [ "$2" = "$3" ]; then
        printf '  ok    %s\n' "$1"
      else
        printf '  FAIL  %s (rc=%s, want %s)\n' "$1" "$2" "$3"
        fail=1
      fi
    }
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    mkdir -p "$tmp/src/artifacts" "$tmp/tests"

    printf 'import os\nx = os.environ.get("A")\n' >"$tmp/src/artifacts/bad.py"
    check "catches os.environ.get in a service module" \
      "$(scan_paths "$tmp/src/artifacts/bad.py" >/dev/null 2>&1; echo $?)" 1
    printf 'import os\nx = os.environ["A"]\n' >"$tmp/src/artifacts/bad2.py"
    check "catches os.environ[...]" \
      "$(scan_paths "$tmp/src/artifacts/bad2.py" >/dev/null 2>&1; echo $?)" 1
    printf 'import os\nx = os.getenv("A")\n' >"$tmp/src/artifacts/bad3.py"
    check "catches os.getenv" \
      "$(scan_paths "$tmp/src/artifacts/bad3.py" >/dev/null 2>&1; echo $?)" 1
    printf 'from os import environ\n' >"$tmp/src/artifacts/bad4.py"
    check "catches from os import environ" \
      "$(scan_paths "$tmp/src/artifacts/bad4.py" >/dev/null 2>&1; echo $?)" 1

    printf 'import os\nx = os.environ.get("A")\n' >"$tmp/src/artifacts/settings.py"
    check "allows settings.py" \
      "$(scan_paths "$tmp/src/artifacts/settings.py" >/dev/null 2>&1; echo $?)" 0
    for name in serve admin client decide republish news; do
      printf 'import os\nx = os.environ.get("A")\n' >"$tmp/src/artifacts/$name.py"
      check "allows $name.py" \
        "$(scan_paths "$tmp/src/artifacts/$name.py" >/dev/null 2>&1; echo $?)" 0
    done
    printf 'import os\nx = os.environ.get("A")  # env-ok: a child process needs it\n' \
      >"$tmp/src/artifacts/marked.py"
    check "allows a line marked env-ok" \
      "$(scan_paths "$tmp/src/artifacts/marked.py" >/dev/null 2>&1; echo $?)" 0
    printf 'import os\nx = os.environ.get("A")\n' >"$tmp/tests/test_x.py"
    check "ignores tests/" \
      "$(scan_paths "$tmp/tests/test_x.py" >/dev/null 2>&1; echo $?)" 0
    printf 'import os\ny = os.getpid()\n' >"$tmp/src/artifacts/fine.py"
    check "does not flag other os calls" \
      "$(scan_paths "$tmp/src/artifacts/fine.py" >/dev/null 2>&1; echo $?)" 0

    echo "----"
    if [ "$fail" -eq 0 ]; then
      echo "$SELF selftest: ALL PASS"
      exit 0
    else
      echo "$SELF selftest: FAILURES ABOVE"
      exit 2
    fi
    ;;

  --tree)
    repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    mapfile -t files < <(git -C "$repo" ls-files -- 'src/artifacts/*.py' 2>/dev/null)
    abs=()
    for f in "${files[@]}"; do abs+=("$repo/$f"); done
    if [ "${#abs[@]}" -eq 0 ]; then
      echo "$SELF: no tracked src/artifacts/*.py files found (unexpected)" >&2
      exit 1
    fi
    if scan_paths "${abs[@]}"; then
      echo "$SELF: clean (${#abs[@]} tracked service modules scanned)"
      exit 0
    else
      advice
      exit 1
    fi
    ;;

  --help | -h)
    sed -n '2,24p' "$0"
    exit 0
    ;;

  "")
    mapfile -t staged < <(git diff --cached --name-only --diff-filter=ACM 2>/dev/null)
    if [ "${#staged[@]}" -eq 0 ]; then
      exit 0
    fi
    if scan_paths "${staged[@]}"; then
      exit 0
    else
      advice
      exit 1
    fi
    ;;

  *)
    echo "usage: $0 [--tree | --selftest | --help]" >&2
    exit 3
    ;;
esac
