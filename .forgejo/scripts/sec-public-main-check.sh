#!/usr/bin/env bash
# sec-public-main-check.sh -- an embargoed fix's image is hand-pinned to production, so it must carry
# everything public main has: refuse unless public main of runcove/artifacts is an ancestor of the
# commit being built. Without it, a private copy that had not merged public main produced a pinnable
# image that would have rolled production back 40 commits (22 Nov 2026). Runbook:
# docs/agent/runbooks/embargoed-fix.md, step 4.
#
#   sec-public-main-check.sh [<public-url>]   run in the checkout, with the full history (not shallow);
#                                             the commit built is $GITHUB_SHA
#   sec-public-main-check.sh --selftest       offline, against local repositories
#
# Public main is read anonymously (the repository is public; no credential is used or needed).
# Anything it cannot establish -- the address unreadable, no `main`, a second answer, a shallow
# checkout, a fetch that fails or lands on another commit than the one listed -- is a FAILURE, never
# a skip: "could not tell" stops the build, and the SEC BUILT line is never printed.
set -euo pipefail

PUBLIC_URL_DEFAULT="https://git.tenfourty.site/runcove/artifacts.git"

check() {
  local url="$1" built="$2" listed pub fetched
  [[ "$built" =~ ^[0-9a-f]{40}$ ]] || { echo "sec-public-main-check: no commit to check" >&2; return 1; }
  [ "$(git rev-parse --is-shallow-repository)" = "false" ] \
    || { echo "sec-public-main-check: the checkout is shallow, so ancestry cannot be judged" >&2; return 1; }
  listed=$(GIT_TERMINAL_PROMPT=0 timeout 60 git ls-remote "$url" refs/heads/main </dev/null) \
    || { echo "sec-public-main-check: could not read public main" >&2; return 1; }
  [ "$(printf '%s\n' "$listed" | grep -c .)" = "1" ] \
    || { echo "sec-public-main-check: public main did not list exactly once" >&2; return 1; }
  pub=${listed%%[[:space:]]*}
  [[ "$pub" =~ ^[0-9a-f]{40}$ ]] || { echo "sec-public-main-check: public main is not a commit id" >&2; return 1; }
  GIT_TERMINAL_PROMPT=0 timeout 300 git fetch --quiet --no-tags "$url" refs/heads/main </dev/null \
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
  expect fail "public url has no main" "$d/ahead" "file://$d/nomain"
  [ "$fails" = 0 ] || { echo "selftest: $fails failed" >&2; return 1; }
  echo "selftest: ok"
}

case "${1:-}" in
  --selftest) selftest ;;
  *) check "${1:-$PUBLIC_URL_DEFAULT}" "${GITHUB_SHA:-}" ;;
esac
