#!/usr/bin/env bash
# scripts/guards/secret-on-cmdline-guard.sh — refuse a commit that puts a
# credential on a process's argv, where every local process can read it for
# the life of the call via `ps` or /proc/<pid>/cmdline.
#
# Ported from ~/repos/homelab/scripts/secret-on-cmdline-guard.sh (ij8.76,
# 21 Sep 2026) and extended: this repo is mostly Python, and the argv shape
# here is a subprocess argument list, not just a curl line in a shell script.
# AGENTS.md's rule is the prose version — "Never put a secret on a command
# line" — and the whole reason `ARTIFACTS_SEAT=<seat> artifacts-key -- <cmd>`
# hands the key over on fd 3 instead of in an argument.
#
# Three invocations:
#   scripts/guards/secret-on-cmdline-guard.sh            # staged files (lefthook pre-commit)
#   scripts/guards/secret-on-cmdline-guard.sh --tree     # every tracked file (CI)
#   scripts/guards/secret-on-cmdline-guard.sh --selftest
#
# WHAT IS SCANNED. .sh and .py only. The shell patterns are the homelab's,
# found and fixed there on 2026-09-09. The Python patterns catch a header or
# a credential built by interpolation and handed to a subprocess as an
# argument — deliberately NOT `headers={"Authorization": f"Bearer {token}"}`,
# which is the CORRECT shape (an httpx/requests header never reaches argv) and
# appears throughout src/artifacts. A guard that flagged it would be turned
# off within a day.
#
# DELIBERATELY NOT SCANNED: a token inside a URL (?apikey=$VAR) — moving a
# header off argv does not fix a URL, and the fix is the API's own
# alternative; and .md files, which document these shapes on purpose.
#
# Exit codes: 0 clean, 1 a forbidden pattern was found (naming every
# file:line), 2 --selftest failed, 3 usage.
set -uo pipefail
SELF="secret-on-cmdline-guard"

# One extended-regexp alternation per language, applied with `grep -nE`.
# Keep these in step with the header above if a form is added.
PATTERN_SH='-H "Authorization: (Bearer|token) \$|-u "?\$[A-Za-z_][A-Za-z0-9_]*:|-H "[^"]*([Aa][Pp][Ii]-?[Kk]ey)[^"]*\$'
# An argv element that is -H/--header/-u, immediately followed by an
# f-string: a credential interpolated into a command line.
PATTERN_PY='["'"'"']--?(H|u|header)["'"'"']\s*,\s*f["'"'"']'

# scan_paths <path>... — prints one line per offending file:line, returns 1 if
# anything was found. Files of other types, and missing files (a staged
# rename's old name, a deletion) are skipped, not errors.
#
# This script's OWN file is skipped: its header documents the forbidden forms
# verbatim and --selftest's positive controls must contain them (a detector
# that cannot demonstrate what it catches is a worse detector). It never calls
# anything with a real credential, so there is nothing real to miss. The
# homelab learned the other half of this the hard way — homelab-4bp, where 11
# of 32 "real hits" in a secret scan were the scanners naming their own
# patterns.
scan_paths() {
  local f hits=0 out self pattern
  self="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  for f in "$@"; do
    case "$f" in
      *.sh) pattern="$PATTERN_SH" ;;
      *.py) pattern="$PATTERN_PY" ;;
      *) continue ;;
    esac
    [ -f "$f" ] || continue
    [ "$(cd "$(dirname "$f")" 2>/dev/null && pwd)/$(basename "$f")" = "$self" ] && continue
    out="$(grep -nE -- "$pattern" "$f" 2>/dev/null)" || continue
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      printf '%s: FORBIDDEN secret-on-argv pattern — %s:%s\n' "$SELF" "$f" "$line"
      hits=$((hits + 1))
    done <<<"$out"
  done
  [ "$hits" -eq 0 ]
}

advice() {
  echo "$SELF: refusing — a credential is on a command line above." >&2
  echo "  In shell: read the key inside the script and pass it on stdin or fd 3" >&2
  echo "            (ARTIFACTS_SEAT=<seat> artifacts-key -- <cmd> is the house shape)." >&2
  echo "  In Python: pass the header in the HTTP client's headers= argument, or hand" >&2
  echo "            the value to the subprocess on stdin or through its environment —" >&2
  echo "            never as an argv element." >&2
}

usage() { sed -n '2,30p' "$0"; }

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

    # POSITIVE CONTROLS — the guard must actually catch these. A pattern that
    # never matches would otherwise sail through a selftest that only checks
    # the clean case.
    cat >"$tmp/bad-bearer.sh" <<'EOF'
#!/usr/bin/env bash
curl -sS -H "Authorization: Bearer $token" "$url"
EOF
    check "catches Authorization: Bearer \$var in shell" \
      "$(scan_paths "$tmp/bad-bearer.sh" >/dev/null 2>&1; echo $?)" 1

    cat >"$tmp/bad-token.sh" <<'EOF'
curl -H "Authorization: token $tok" "$url"
EOF
    check "catches Authorization: token \$var in shell" \
      "$(scan_paths "$tmp/bad-token.sh" >/dev/null 2>&1; echo $?)" 1

    cat >"$tmp/bad-apikey.sh" <<'EOF'
curl -H "X-Api-Key: $API_KEY" "$url"
EOF
    check "catches X-Api-Key: \$var in shell" \
      "$(scan_paths "$tmp/bad-apikey.sh" >/dev/null 2>&1; echo $?)" 1

    cat >"$tmp/bad-basicauth.sh" <<'EOF'
curl -u "$user:$pass" "$url"
EOF
    check "catches -u \"\$var:...\" in shell" \
      "$(scan_paths "$tmp/bad-basicauth.sh" >/dev/null 2>&1; echo $?)" 1

    cat >"$tmp/bad_subprocess.py" <<'EOF'
import subprocess

def publish(path: str, token: str) -> None:
    subprocess.run(
        ["curl", "-sS", "-H", f"Authorization: Bearer {token}", "https://example.invalid"],
        check=True,
    )
EOF
    check "catches a header f-string on subprocess argv in Python" \
      "$(scan_paths "$tmp/bad_subprocess.py" >/dev/null 2>&1; echo $?)" 1

    # NEGATIVE CONTROLS — the correct shapes must NOT trip the guard, or
    # src/artifacts stops being committable.
    cat >"$tmp/good_headers.py" <<'EOF'
import httpx

def publish(url: str, token: str) -> httpx.Response:
    return httpx.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
EOF
    check "does not flag headers={...} in an HTTP client call" \
      "$(scan_paths "$tmp/good_headers.py" >/dev/null 2>&1; echo $?)" 0

    cat >"$tmp/good_stdin.py" <<'EOF'
import subprocess

def publish(token: str) -> None:
    subprocess.run(["artifacts-publish", "--from-stdin"], input=token, text=True, check=True)
EOF
    check "does not flag a subprocess that takes the value on stdin" \
      "$(scan_paths "$tmp/good_stdin.py" >/dev/null 2>&1; echo $?)" 0

    cat >"$tmp/good-fd3.sh" <<'EOF'
#!/usr/bin/env bash
# the house shape: the key arrives on fd 3, never on a command line
key=$(cat <&3)
curl -sS -H "@-" "$url" <<<"Authorization: Bearer $key"
EOF
    check "does not flag the fd-3 / stdin shape" \
      "$(scan_paths "$tmp/good-fd3.sh" >/dev/null 2>&1; echo $?)" 0

    cat >"$tmp/mention.md" <<'EOF'
Never write `curl -H "Authorization: Bearer $T" https://example/` — that is the bad shape.
EOF
    check "skips .md (docs document these shapes on purpose)" \
      "$(scan_paths "$tmp/mention.md" >/dev/null 2>&1; echo $?)" 0

    # The self-exemption must actually engage: this file contains every
    # forbidden form above, in its header and in these fixtures.
    check "does not flag its own file (self-exemption)" \
      "$(scan_paths "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")" >/dev/null 2>&1; echo $?)" 0

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
    mapfile -t files < <(git -C "$repo" ls-files -- '*.sh' '*.py' 2>/dev/null)
    abs=(); for f in "${files[@]}"; do abs+=("$repo/$f"); done
    if [ "${#abs[@]}" -eq 0 ]; then
      echo "$SELF: no tracked .sh/.py files found (unexpected, but not a violation)"
      exit 0
    fi
    if scan_paths "${abs[@]}"; then
      echo "$SELF: clean (${#abs[@]} tracked .sh/.py files scanned)"
      exit 0
    else
      advice
      exit 1
    fi
    ;;

  --help|-h)
    usage
    exit 0
    ;;

  "")
    # lefthook pre-commit: scan what THIS commit actually touches, read from
    # the commit's own index. `git diff --cached` and not lefthook's
    # {staged_files}: when a commit is made with a pathspec, git points
    # GIT_INDEX_FILE at a temporary index holding exactly the paths being
    # committed, and `git diff --cached` reads that — so this stays correct
    # whether or not a pathspec was used (homelab, measured 2026-09-04).
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
