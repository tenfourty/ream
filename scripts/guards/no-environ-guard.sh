#!/usr/bin/env bash
# scripts/guards/no-environ-guard.sh — refuse a read of the process environment in
# src/ream outside the modules that may make one.
#
# Every setting the SERVER reads goes through `ream.settings` (call-time, one object,
# `settings.override(...)` in tests; docs/agent/specs/2026-11-04-core-and-plugins-design.md 3.1).
# A stray `os.environ` is a setting nobody validates at start-up, nobody lists in the docs and
# no test can override.
#
# ALLOWED (by file name, in src/ream/):
#   settings.py   the one reader of the server settings
#   envnames.py   the one place that reads a setting under its new (REAM_) and old (ARTIFACTS_) name
#   serve.py      start-up switches (DATABASE_URL, REAM_MAINTENANCE, REAM_PACK_VERSIONS)
#   admin.py      the operator CLI (DATABASE_URL, SQLITE_TMPDIR, `env:NAME` DSN indirection)
#   client.py decide.py republish.py news.py selfupdate.py
#                 the command-line tools' OWN variables (key file, session label, XDG dirs,
#                 wait timeout): they run on other machines with no server settings
# The shim package src/artifacts/ is held to the same rule and to more: it may contain only
# __init__.py and py.typed (a module added there would be a second copy of the code, not an
# alias), and its __init__.py reads no environment at all.
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

ALLOWED=" settings.py envnames.py serve.py admin.py client.py decide.py republish.py news.py selfupdate.py "
PATTERN='os\.environ|os\.getenv|from os import .*(environ|getenv)|\bgetenv\('

# scan_paths <path>... — one line per offending file:line; returns 1 if any.
scan_paths() {
  local f hits=0 out base line
  for f in "$@"; do
    case "$f" in
      */src/artifacts/* | src/artifacts/*)
        base="$(basename "$f")"
        case "$base" in
          __init__.py | py.typed) ;;
          *)
            printf '%s: src/artifacts may hold only __init__.py and py.typed — %s\n' "$SELF" "$f"
            hits=$((hits + 1))
            continue
            ;;
        esac
        out="$(grep -nE -- "$PATTERN" "$f" 2>/dev/null | grep -v '# env-ok:')" || continue
        while IFS= read -r line; do
          [ -n "$line" ] || continue
          printf '%s: environment read in the shim — %s:%s\n' "$SELF" "$f" "$line"
          hits=$((hits + 1))
        done <<<"$out"
        continue
        ;;
      */src/ream/*.py | src/ream/*.py) ;;
      *) continue ;;
    esac
    [ -f "$f" ] || continue
    base="$(basename "$f")"
    case "$ALLOWED" in *" $base "*) continue ;; esac
    out="$(grep -nE -- "$PATTERN" "$f" 2>/dev/null | grep -v '# env-ok:')" || continue
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      printf '%s: environment read outside ream.settings — %s:%s\n' "$SELF" "$f" "$line"
      hits=$((hits + 1))
    done <<<"$out"
  done
  [ "$hits" -eq 0 ]
}

advice() {
  echo "$SELF: refusing — read it through ream.settings instead:" >&2
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
    mkdir -p "$tmp/src/ream" "$tmp/src/artifacts" "$tmp/tests"

    printf 'import os\nx = os.environ.get("A")\n' >"$tmp/src/ream/bad.py"
    check "catches os.environ.get in a service module" \
      "$(scan_paths "$tmp/src/ream/bad.py" >/dev/null 2>&1; echo $?)" 1
    printf 'import os\nx = os.environ["A"]\n' >"$tmp/src/ream/bad2.py"
    check "catches os.environ[...]" \
      "$(scan_paths "$tmp/src/ream/bad2.py" >/dev/null 2>&1; echo $?)" 1
    printf 'import os\nx = os.getenv("A")\n' >"$tmp/src/ream/bad3.py"
    check "catches os.getenv" \
      "$(scan_paths "$tmp/src/ream/bad3.py" >/dev/null 2>&1; echo $?)" 1
    printf 'from os import environ\n' >"$tmp/src/ream/bad4.py"
    check "catches from os import environ" \
      "$(scan_paths "$tmp/src/ream/bad4.py" >/dev/null 2>&1; echo $?)" 1

    printf 'import os\nx = os.environ.get("A")\n' >"$tmp/src/ream/settings.py"
    check "allows settings.py" \
      "$(scan_paths "$tmp/src/ream/settings.py" >/dev/null 2>&1; echo $?)" 0
    for name in envnames serve admin client decide republish news; do
      printf 'import os\nx = os.environ.get("A")\n' >"$tmp/src/ream/$name.py"
      check "allows $name.py" \
        "$(scan_paths "$tmp/src/ream/$name.py" >/dev/null 2>&1; echo $?)" 0
    done
    printf 'import os\nx = os.environ.get("A")  # env-ok: a child process needs it\n' \
      >"$tmp/src/ream/marked.py"
    check "allows a line marked env-ok" \
      "$(scan_paths "$tmp/src/ream/marked.py" >/dev/null 2>&1; echo $?)" 0
    printf 'import os\nx = os.environ.get("A")\n' >"$tmp/tests/test_x.py"
    check "ignores tests/" \
      "$(scan_paths "$tmp/tests/test_x.py" >/dev/null 2>&1; echo $?)" 0
    printf 'import os\ny = os.getpid()\n' >"$tmp/src/ream/fine.py"
    check "does not flag other os calls" \
      "$(scan_paths "$tmp/src/ream/fine.py" >/dev/null 2>&1; echo $?)" 0

    printf 'import ream\n' >"$tmp/src/artifacts/__init__.py"
    : >"$tmp/src/artifacts/py.typed"
    check "allows the shim's __init__.py and py.typed" \
      "$(scan_paths "$tmp/src/artifacts/__init__.py" "$tmp/src/artifacts/py.typed" >/dev/null 2>&1; echo $?)" 0
    printf 'x = 1\n' >"$tmp/src/artifacts/settings.py"
    check "refuses another module in src/artifacts" \
      "$(scan_paths "$tmp/src/artifacts/settings.py" >/dev/null 2>&1; echo $?)" 1
    printf 'import os\nx = os.getenv("A")\n' >"$tmp/src/artifacts/__init__.py"
    check "refuses an environment read in the shim" \
      "$(scan_paths "$tmp/src/artifacts/__init__.py" >/dev/null 2>&1; echo $?)" 1

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
    mapfile -t files < <(git -C "$repo" ls-files -- 'src/ream/*.py' 'src/artifacts/*' 2>/dev/null)
    abs=()
    for f in "${files[@]}"; do abs+=("$repo/$f"); done
    if [ "${#abs[@]}" -eq 0 ]; then
      echo "$SELF: no tracked src/ream/*.py files found (unexpected)" >&2
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
