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
  [ "$(git rev-parse --is-shallow-repository)" = "false" ] \
    || { echo "sec-public-main-check: the checkout is shallow, so ancestry cannot be judged" >&2; return 1; }
  listed=$(gitr 60 ls-remote "$url" refs/heads/main </dev/null) \
    || { echo "sec-public-main-check: could not read public main" >&2; return 1; }
  [ "$(printf '%s\n' "$listed" | grep -c .)" = "1" ] \
    || { echo "sec-public-main-check: public main did not list exactly once" >&2; return 1; }
  pub=${listed%%[[:space:]]*}
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
  expect() { # expect <pass|fail> <name> <dir> <url>
    local want="$1" name="$2" dir="$3" url="$4" rc=0
    (cd "$dir" && check "$url" "$(git rev-parse HEAD)") >/dev/null 2>&1 || rc=$?
    if { [ "$want" = pass ] && [ "$rc" = 0 ]; } || { [ "$want" = fail ] && [ "$rc" != 0 ]; }; then
      echo "ok   $name"
    else echo "FAIL $name (exit $rc)"; fails=$((fails + 1)); fi
  }
  expect pass "core contains public main" "$d/ahead" "file://$d/pub"
  expect pass "core is public main itself" "$d/pub" "file://$d/pub"
  expect fail "core behind public main" "$d/behind" "file://$d/pub"
  expect fail "shallow checkout" "$d/shallow" "file://$d/pub"
  expect fail "public url unreadable" "$d/ahead" "file://$d/missing"
  local msg rc=0
  msg=$(cd "$d/ahead" && check "https://git.tenfourty.site/runcove/artifacts.git" "$(git rev-parse HEAD)" 2>&1) || rc=$?
  if [ "$rc" != 0 ] && [[ "$msg" == *"SEC_PUBLIC_READ_TOKEN is not set"* ]]; then
    echo "ok   https url with no token is refused for the token, before any network"
  else echo "FAIL https url with no token (exit $rc): $msg"; fails=$((fails + 1)); fi
  expect fail "public url has no main" "$d/ahead" "file://$d/nomain"
  [ "$fails" = 0 ] || { echo "selftest: $fails failed" >&2; return 1; }
  echo "selftest: ok"
}

case "${1:-}" in
  --selftest) selftest ;;
  *) check "${1:-$PUBLIC_URL_DEFAULT}" "${GITHUB_SHA:-}" ;;
esac
