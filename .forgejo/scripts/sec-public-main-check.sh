#!/usr/bin/env bash
# sec-public-main-check.sh -- an embargoed fix's image is hand-pinned to production, so it must carry
# everything public main has: refuse unless public main of runcove/artifacts is an ancestor of the
# commit being built. Without it, a private copy that had not merged public main produced a pinnable
# image that would have rolled production back 40 commits (22 Nov 2026). Runbook:
# docs/agent/runbooks/embargoed-fix.md, step 4.
#
#   sec-public-main-check.sh [<url>]          needs SEC_PUBLIC_READ_TOKEN in the environment; run in the checkout, with the full history (not shallow);
#                                             the commit built is $GITHUB_SHA
#   sec-public-main-check.sh --selftest       offline, against local repositories
#
# Public main is read with SEC_PUBLIC_READ_TOKEN (read of runcove/artifacts only): the organization
# is limited-visibility, so an anonymous read is refused. The token rides in git's environment as an
# extra header, never on a command line, and is required for an https address (a file:// address,
# used by the selftest, needs none). Anything it cannot establish -- the address unreadable, no `main`, a second answer, a shallow
# checkout, a fetch that fails or lands on another commit than the one listed -- is a FAILURE, never
# a skip: "could not tell" stops the build, and the SEC BUILT line is never printed.
set -euo pipefail

PUBLIC_URL_DEFAULT="https://git.tenfourty.site/runcove/artifacts.git"

# gitr <seconds> <git args>: git bounded by a timeout (a timeout is a failure), with the read token
# as a header when there is one; the token is never an argument.
gitr() {
  local lim="$1"
  shift
  if [ -n "${SEC_PUBLIC_READ_TOKEN:-}" ]; then
    # Assignments before the command go in the environment of timeout and git; `env NAME=value`
    # would put the header on env's own argv, readable in /proc by any process.
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="http.https://git.tenfourty.site/.extraheader" \
      GIT_CONFIG_VALUE_0="Authorization: basic $(printf 'sec:%s' "$SEC_PUBLIC_READ_TOKEN" | base64 -w0)" \
      GIT_TERMINAL_PROMPT=0 timeout "$lim" git "$@"
  else
    GIT_TERMINAL_PROMPT=0 timeout "$lim" git "$@"
  fi
}

check() {
  local url="$1" built="$2" listed pub fetched
  case "$url" in
    file://*) ;;
    *) [ -n "${SEC_PUBLIC_READ_TOKEN:-}" ] \
         || { echo "sec-public-main-check: SEC_PUBLIC_READ_TOKEN is not set on this repo, so public main cannot be read" >&2; return 1; } ;;
  esac
  [[ "$built" =~ ^[0-9a-f]{40}$ ]] || { echo "sec-public-main-check: no commit to check" >&2; return 1; }
  [ "$(git rev-parse HEAD)" = "$built" ] \
    || { echo "sec-public-main-check: the checkout is not the commit being built" >&2; return 1; }
  [ "$(git rev-parse --is-shallow-repository)" = "false" ] \
    || { echo "sec-public-main-check: the checkout is shallow, so ancestry cannot be judged" >&2; return 1; }
  listed=$(gitr 60 ls-remote "$url" refs/heads/main </dev/null) \
    || { echo "sec-public-main-check: could not read public main" >&2; return 1; }
  [ "$(printf '%s\n' "$listed" | grep -c .)" = "1" ] \
    || { echo "sec-public-main-check: public main did not list exactly once" >&2; return 1; }
  pub=${listed%%[[:space:]]*}
  [ "${listed#*[[:space:]]}" = "refs/heads/main" ] \
    || { echo "sec-public-main-check: the one ref listed is not refs/heads/main" >&2; return 1; }
  [[ "$pub" =~ ^[0-9a-f]{40}$ ]] || { echo "sec-public-main-check: public main is not a commit id" >&2; return 1; }
  gitr 300 fetch --quiet --no-tags "$url" refs/heads/main </dev/null \
    || { echo "sec-public-main-check: could not fetch public main" >&2; return 1; }
  fetched=$(git rev-parse --verify "FETCH_HEAD^{commit}")
  [ "$fetched" = "$pub" ] \
    || { echo "sec-public-main-check: public main moved while it was read; run again" >&2; return 1; }
  if git merge-base --is-ancestor "$pub" "$built"; then
    echo "public main ${pub:0:12} is in the commit built ${built:0:12}"
  else
    echo "REFUSED: public main ${pub:0:12} is not in the commit built ${built:0:12}." >&2
    echo "Merge public main into the private copy and run again; an image without it would roll production back." >&2
    return 1
  fi
}

selftest() {
  local d fails=0
  d=$(mktemp -d)
  trap 'rm -rf "$d"' RETURN
  unset SEC_PUBLIC_READ_TOKEN
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
  g() { git -c user.name=t -c user.email=t@t -c commit.gpgsign=false "$@"; }
  g init -q -b main "$d/pub"
  g -C "$d/pub" commit -q --allow-empty -m a
  g -C "$d/pub" commit -q --allow-empty -m b
  g clone -q "file://$d/pub" "$d/ahead"
  g -C "$d/ahead" commit -q --allow-empty -m c
  g init -q -b main "$d/behind"
  g -C "$d/behind" fetch -q "file://$d/pub" main
  g -C "$d/behind" checkout -q -b w FETCH_HEAD~1
  g clone -q "file://$d/pub" "$d/shallow" --depth 1
  g init -q -b other "$d/nomain"
  g -C "$d/nomain" commit -q --allow-empty -m z
  # more histories: unrelated roots, a second ref that tail-matches main, main only under a prefix
  g init -q -b main "$d/unrelated"
  g -C "$d/unrelated" commit -q --allow-empty -m u
  g init -q -b main "$d/duppub"
  g -C "$d/duppub" commit -q --allow-empty -m a
  g -C "$d/duppub" update-ref refs/heads/refs/heads/main HEAD
  g init -q -b x/main "$d/prefixed"
  g -C "$d/prefixed" commit -q --allow-empty -m p
  head_of() { git -C "$1" rev-parse HEAD; }
  # expect <pass|fail> <name> <dir> <url> [<built>: default the dir's HEAD]
  expect() {
    local want="$1" name="$2" dir="$3" url="$4" built rc=0
    built=${5-$(head_of "$dir")}
    (cd "$dir" && check "$url" "$built") >/dev/null 2>&1 || rc=$?
    if { [ "$want" = pass ] && [ "$rc" = 0 ]; } || { [ "$want" = fail ] && [ "$rc" != 0 ]; }; then
      echo "ok   $name"
    else echo "FAIL $name (exit $rc)"; fails=$((fails + 1)); fi
  }
  # refused_with <name> <dir> <url> <built> <text the refusal must hold>
  refused_with() {
    local name="$1" dir="$2" url="$3" built="$4" text="$5" msg rc=0
    msg=$(cd "$dir" && check "$url" "$built" 2>&1) || rc=$?
    if [ "$rc" != 0 ] && [[ "$msg" == *"$text"* ]]; then echo "ok   $name"
    else echo "FAIL $name (exit $rc): $msg"; fails=$((fails + 1)); fi
  }
  local pubu="file://$d/pub" https="https://git.tenfourty.site/runcove/artifacts.git"
  expect pass "core contains public main" "$d/ahead" "$pubu"
  expect pass "core is public main itself" "$d/pub" "$pubu"
  expect fail "core behind public main" "$d/behind" "$pubu"
  expect fail "unrelated history" "$d/unrelated" "$pubu"
  expect fail "shallow checkout" "$d/shallow" "$pubu"
  expect fail "public url unreadable" "$d/ahead" "file://$d/missing"
  expect fail "public url has no main" "$d/ahead" "file://$d/nomain"
  expect fail "main only under another prefix" "$d/ahead" "file://$d/prefixed"
  refused_with "a second ref lists for main" "$d/ahead" "file://$d/duppub" "$(head_of "$d/ahead")" "exactly once"
  expect fail "built commit is not the checkout's HEAD" "$d/ahead" "$pubu" "$(head_of "$d/pub")"
  expect fail "built commit does not exist" "$d/ahead" "$pubu" "$(printf 'a%.0s' $(seq 40))"
  expect fail "built commit is empty" "$d/ahead" "$pubu" ""
  expect fail "built commit is short" "$d/ahead" "$pubu" "$(head_of "$d/ahead" | cut -c1-12)"
  refused_with "https url with the token unset is refused for the token" "$d/ahead" "$https" "$(head_of "$d/ahead")" \
    "SEC_PUBLIC_READ_TOKEN is not set"
  SEC_PUBLIC_READ_TOKEN="" refused_with "https url with the token set but empty is refused for the token" \
    "$d/ahead" "$https" "$(head_of "$d/ahead")" "SEC_PUBLIC_READ_TOKEN is not set"
  # a fetch that lands on another commit than the one listed: gitr replaced, in a subshell, by one whose
  # fetch reads another repository
  (
    # shellcheck disable=SC2329  # called by check, which shellcheck cannot see through
    gitr() {
      local lim="$1"
      shift
      if [ "$1" = fetch ]; then git fetch --quiet --no-tags "file://$d/ahead" refs/heads/main; else git "$@"; fi
    }
    refused_with "a fetch landing on another commit" "$d/ahead" "$pubu" "$(head_of "$d/ahead")" "moved while it was read"
  ) || fails=$((fails + 1))
  [ "$fails" = 0 ] || { echo "selftest: $fails failed" >&2; return 1; }
  echo "selftest: ok"
}

case "${1:-}" in
  --selftest) selftest ;;
  *) check "${1:-$PUBLIC_URL_DEFAULT}" "${GITHUB_SHA:-}" ;;
esac
