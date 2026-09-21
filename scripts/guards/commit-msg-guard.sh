#!/usr/bin/env bash
# scripts/guards/commit-msg-guard.sh — the two commit-message rules CLAUDE.md
# states in prose, made mechanical: conventional commits, and no attribution
# trailers.
#
# WHY (ij8.76, 22 Sep 2026). Both rules have already been broken here by
# somebody who had read them:
#
#   * FIVE commits on 19 Sep 2026 carry a Co-Authored-By trailer — 4a37bad,
#     c2ff307, 3d9a474, 7522c95, 7796b0e — against CLAUDE.md's "Conventional
#     commits, no co-authored-by". It is not carelessness: the harness re-offers
#     that trailer, and a "Generated with Claude Code" line, in a system
#     reminder at the start of every session, so the rule has to be refused
#     once per commit, forever, by hand. That is exactly the shape a hook
#     should carry.
#   * The conventional-commit rule has held: all 285 subjects in this repo's
#     history match, measured before this guard was written. A gate that
#     would have blocked existing work is a gate that gets disabled, so it was
#     worth checking first.
#
# NOT A RULE HERE: subject length. The longest subject in this repo is 194
# characters and that is the house style; a guard nobody's history obeys is
# noise.
#
# Override: COMMIT_MSG_OVERRIDE=1 git commit … (it prints that it was used).
#
# Usage (lefthook commit-msg): scripts/guards/commit-msg-guard.sh <msg-file>
#        scripts/guards/commit-msg-guard.sh --selftest
set -uo pipefail
SELF="commit-msg-guard"

TYPES='feat|fix|docs|test|refactor|chore|ci|build|perf|style|revert|deploy'

# check_message <file> — prints findings, returns 1 if the message is refused.
check_message() {
  local file="$1" body subject problems=0

  # Drop git's comment lines and everything after the verbose-diff scissors,
  # which is where a `Co-authored-by:` MENTION in a staged diff would sit —
  # that is the diff being committed, not the message.
  body=$(sed -e '/^# *-\+ >8 -\+/,$d' -e '/^#/d' "$file")
  subject=$(printf '%s\n' "$body" | sed -e '/^[[:space:]]*$/d' | head -n 1)

  if [ -z "$subject" ]; then
    # An empty message: git aborts on its own, and refusing here would only
    # replace git's clearer error.
    return 0
  fi

  if printf '%s\n' "$body" | grep -qiE '^[[:space:]]*co-authored-by:'; then
    echo "$SELF: REFUSED — the message carries a Co-Authored-By trailer." >&2
    echo "  CLAUDE.md: conventional commits, no co-authored-by. Five commits on" >&2
    echo "  19 Sep 2026 slipped this rule; the harness re-offers the trailer every" >&2
    echo "  session, which is why it is a hook and not a paragraph." >&2
    problems=1
  fi

  if printf '%s\n' "$body" | grep -qiE 'generated with \[?claude code|🤖 generated with'; then
    echo "$SELF: REFUSED — the message carries a 'Generated with Claude Code' line." >&2
    echo "  Same rule, same reason as the trailer above." >&2
    problems=1
  fi

  case "$subject" in
    Merge\ *|Revert\ \"*|fixup!\ *|squash!\ *) return "$problems" ;;
  esac

  if ! printf '%s\n' "$subject" | grep -qE "^($TYPES)(\([a-zA-Z0-9._/-]+\))?!?: .+"; then
    echo "$SELF: REFUSED — the subject is not a conventional commit." >&2
    echo "  got:      $subject" >&2
    echo "  expected: <type>(<scope>)?: <summary>, type one of: ${TYPES//|/, }" >&2
    problems=1
  fi

  if [ "$problems" -ne 0 ]; then
    echo "  Override (it will be visible in the log): COMMIT_MSG_OVERRIDE=1 git commit …" >&2
  fi
  return "$problems"
}

main() {
  local file="${1:-}"
  if [ -z "$file" ] || [ ! -f "$file" ]; then
    echo "usage: $0 <commit-message-file>" >&2
    exit 3
  fi
  if [ "${COMMIT_MSG_OVERRIDE:-0}" = "1" ]; then
    # Still say what would have been refused: an override that hides the
    # finding teaches nothing.
    if ! check_message "$file" >/dev/null 2>&1; then
      echo "$SELF: OVERRIDDEN by COMMIT_MSG_OVERRIDE=1 — this message would have been refused" >&2
    fi
    exit 0
  fi
  check_message "$file"
  exit $?
}

selftest() {
  local fail=0 tmp self
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' RETURN
  self="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

  case_msg() { # case_msg <name> <want-rc> <message...>
    local name="$1" want="$2"; shift 2
    printf '%s\n' "$@" > "$tmp/msg"
    local out rc
    out=$(bash "$self" "$tmp/msg" 2>&1); rc=$?
    if [ "$rc" = "$want" ]; then
      printf '  ok    %s\n' "$name"
    else
      printf '  FAIL  %s (rc=%s, want %s)\n' "$name" "$rc" "$want"
      printf '%s\n' "$out" | sed 's/^/        /'
      fail=1
    fi
  }

  case_msg "a conventional subject passes" 0 "feat(comments): the drawer switches scope"
  case_msg "a scopeless conventional subject passes" 0 "docs: the evening's three fixes"
  case_msg "a breaking-change marker passes" 0 "feat!: the key is hashed at rest"
  case_msg "a bare sentence is refused" 1 "made the drawer switch scope"
  case_msg "an unknown type is refused" 1 "wip: half a slice"
  case_msg "a merge commit passes" 0 "Merge branch 'main' into main"
  case_msg "a revert passes" 0 "Revert \"feat: the key is hashed at rest\""
  case_msg "a Co-Authored-By trailer is refused" 1 \
    "feat: a good subject" "" "Co-Authored-By: Someone <someone@example.invalid>"
  case_msg "a lowercase co-authored-by trailer is refused too" 1 \
    "feat: a good subject" "" "co-authored-by: someone <someone@example.invalid>"
  case_msg "a Generated-with line is refused" 1 \
    "feat: a good subject" "" "🤖 Generated with [Claude Code](https://claude.com/claude-code)"
  case_msg "a comment line mentioning the trailer does not refuse" 0 \
    "feat: a good subject" "# Co-Authored-By: this line is a git comment"
  case_msg "an empty message is left to git" 0 "" "# please enter a commit message"

  # The scissors case: a verbose commit shows the staged DIFF below the
  # scissors line. A diff that adds a co-authored-by trailer to some other
  # file must not be read as this message's trailer.
  printf '%s\n' \
    "docs: the guard's own rules" \
    "" \
    "# ------------------------ >8 ------------------------" \
    "diff --git a/x b/x" \
    "+Co-Authored-By: Someone <someone@example.invalid>" > "$tmp/msg"
  if bash "$self" "$tmp/msg" >/dev/null 2>&1; then
    printf '  ok    %s\n' "a trailer inside the verbose diff is not the message's"
  else
    printf '  FAIL  %s\n' "a trailer inside the verbose diff was read as the message's"
    fail=1
  fi

  # Override, proved on a message that is otherwise refused.
  printf '%s\n' "wip: half a slice" > "$tmp/msg"
  if COMMIT_MSG_OVERRIDE=1 bash "$self" "$tmp/msg" >/dev/null 2>&1; then
    printf '  ok    %s\n' "COMMIT_MSG_OVERRIDE=1 lets a refused message through"
  else
    printf '  FAIL  %s\n' "COMMIT_MSG_OVERRIDE=1 did not let a refused message through"
    fail=1
  fi

  # And through REAL git, installed as a raw commit-msg hook: the selftest
  # above calls the script with a file path, which is the literal input git
  # passes, but only a real commit proves the wiring shape.
  local work hooks_dir rc
  work=$(mktemp -d)
  git init -q "$work"
  git -C "$work" config user.email "selftest@example.invalid"
  git -C "$work" config user.name "commit-msg-guard-selftest"
  hooks_dir=$(git -C "$work" rev-parse --git-path hooks)
  cat >"$work/$hooks_dir/commit-msg" <<EOF
#!/usr/bin/env bash
exec "$self" "\$@"
EOF
  chmod +x "$work/$hooks_dir/commit-msg"
  echo one >"$work/f.txt"; git -C "$work" add f.txt

  rc=0; ( cd "$work" && git commit -q -m "not conventional at all" ) >/dev/null 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '  ok    %s\n' "real git commit refused for a non-conventional subject"
  else
    printf '  FAIL  %s\n' "real git commit ALLOWED a non-conventional subject"
    fail=1
  fi

  rc=0
  ( cd "$work" && git commit -q -m "feat: a real commit through the real hook" ) >/dev/null 2>&1 || rc=$?
  if [ "$rc" -eq 0 ]; then
    printf '  ok    %s\n' "real git commit allowed for a conventional subject"
  else
    printf '  FAIL  %s\n' "real git commit REFUSED a conventional subject (rc=$rc)"
    fail=1
  fi
  rm -rf "$work"

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
  *) main "$@" ;;
esac
