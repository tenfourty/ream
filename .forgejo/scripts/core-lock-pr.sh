#!/usr/bin/env bash
# core-lock-pr.sh <sha> <core-ref> -- after main's image job, open (or update) the one pull
# request on runcove/ream-homelab that moves its `core.lock` to this core image, and ask the
# forge to merge it by itself when ream-homelab's checks succeed (docs/decisions.md, 9 Nov 2026;
# runbook docs/agent/runbooks/image-from-ream-homelab.md). ream-homelab then builds the deployed
# image and opens the homelab pin PR. Modelled on pin-pr.sh.
#
# CORE_LOCK_TOKEN (environment only, never an argument) belongs to an account that may push
# branches to and open and merge PRs on ream-homelab. It is read by curl from stdin and by git
# through askpass.
#
# Stands down (exit 0, nothing pushed) when core.lock already names this commit, or names a commit
# that is NOT an ancestor of this one (a newer run got there first). Needs the full history of
# this repo (fetch-depth: 0). One branch, core-lock/next, is force-pushed: one open PR at a time.
#
# Exit 0: a PR is open (new or updated) and asked to merge when checks succeed, or a stand-down.
# Exit 64: refused (bad input, missing token, or the rewrite did not change exactly one line).
# Exit 65: a git or forge call failed.
#
# core-lock-pr.sh --selftest runs its checks against throwaway files and repos, offline.
set -u

FORGE="${LOCK_FORGE:-https://git.tenfourty.site}"
TARGET="${LOCK_TARGET:-runcove/ream-homelab}"
BASE_BRANCH="${LOCK_BASE_BRANCH:-main}"
LOCK_FILE="${LOCK_FILE:-core.lock}"
BRANCH="core-lock/next"
IMAGE_REPO="git.tenfourty.site/runcove/artifacts-core"

say()     { printf 'core-lock-pr: %s\n' "$*"; }
refuse()  { printf 'core-lock-pr: REFUSING: %s\n' "$*" >&2; exit 64; }
failed()  { printf 'core-lock-pr: FAILED: %s\n' "$*" >&2; exit 65; }

# <repo>:<sha12>@sha256:<64 hex>, for the core image only, whole string.
is_core_ref() {
  case "$1" in *$'\n'*) return 1 ;; esac
  printf '%s' "$1" | grep -Eqx "${IMAGE_REPO//./\\.}:[0-9a-f]{12}@sha256:[0-9a-f]{64}"
}

# The sha12 in the lock file's one line; empty unless the file is exactly one valid ref.
current_sha12() {
  local line
  [ "$(grep -c . "$1")" -eq 1 ] || return 0
  line=$(grep . "$1")
  is_core_ref "$line" || return 0
  printf '%s' "$line" | sed -E 's#^[^:]*:([0-9a-f]{12})@.*#\1#'
}

# Replace the lock file's content with <ref>; fail unless the file changed.
rewrite_lock() {
  local file="$1" ref="$2"
  [ "$(cat "$file")" != "$ref" ] || return 1
  printf '%s\n' "$ref" > "$file"
}

# From <repo dir>, <current sha12>, <our sha>: prints go, same, behind or unknown.
decide() {
  local dir="$1" cur="$2" ours="$3" full
  [ "${ours:0:12}" = "$cur" ] && { echo same; return 0; }
  full=$(git -C "$dir" rev-parse --verify --quiet "${cur}^{commit}") || { echo unknown; return 0; }
  if git -C "$dir" merge-base --is-ancestor "$full" "$ours"; then echo go; else echo behind; fi
}

# One forge call; the token goes to curl on stdin as a config line, never on argv.
forge() { # <method> <path> <body-or-empty> <outfile>
  local method="$1" path="$2" body="$3" out="$4"
  if [ -n "$body" ]; then
    printf 'header = "Authorization: token %s"\n' "$CORE_LOCK_TOKEN" \
      | curl -sS --config - --max-time 30 -o "$out" -w '%{http_code}' -X "$method" \
          -H 'Content-Type: application/json' --data "$body" "$FORGE/api/v1$path"
  else
    printf 'header = "Authorization: token %s"\n' "$CORE_LOCK_TOKEN" \
      | curl -sS --config - --max-time 30 -o "$out" -w '%{http_code}' -X "$method" \
          "$FORGE/api/v1$path"
  fi
}

# The number of the open PR whose head branch is ours AND lives in TARGET itself (a fork's PR
# with the same branch name is never ours), from the open-PR list in <json file>.
open_pr_number() {
  python3 -I - "$1" "$BRANCH" "$TARGET" <<'PY'
import json, sys
path, branch, target = sys.argv[1:4]
try:
    prs = json.load(open(path))
except (OSError, ValueError):
    sys.exit(0)
for pr in prs if isinstance(prs, list) else []:
    head = pr.get("head") or {}
    if (head.get("ref") == branch and (head.get("repo") or {}).get("full_name") == target
            and isinstance(pr.get("number"), int)):
        print(pr["number"])
        break
PY
}

# Succeeds when the PR in <json file> has its head in TARGET itself, on our branch, at <sha>.
pr_head_is() {
  python3 -I - "$1" "$BRANCH" "$TARGET" "$2" <<'PY'
import json, sys
path, branch, target, sha = sys.argv[1:5]
try:
    head = json.load(open(path)).get("head") or {}
except (OSError, ValueError, AttributeError):
    sys.exit(1)
ok = (head.get("ref") == branch and (head.get("repo") or {}).get("full_name") == target
      and head.get("sha") == sha)
sys.exit(0 if ok else 1)
PY
}

# Refuse unless PR <n>'s head is the commit we just pushed to our own branch on TARGET. The forge
# moves a PR's head a moment after a push, so look a few times before refusing.
verify_pr_head() { # <n> <pushed sha>
  local tries
  for tries in 1 2 3 4 5 6; do
    [ "$(forge GET "/repos/$TARGET/pulls/$1" "" "$work/prcheck.json")" = 200 ] \
      || failed "could not read PR #$1 on $TARGET"
    pr_head_is "$work/prcheck.json" "$2" && return 0
    say "PR #$1 head not at the pushed commit yet (try $tries of 6)"
    sleep 5
  done
  refuse "PR #$1 does not have its head at $2 on $TARGET:$BRANCH; not touching it"
}

main() {
  local sha="${1:-}" ref="${2:-}"
  printf '%s' "$sha" | grep -Eqx '[0-9a-f]{40}' || refuse "'$sha' is not a full commit sha"
  is_core_ref "$ref" || refuse "'$ref' is not ${IMAGE_REPO}:<sha12>@sha256:<digest>"
  [ "$(printf '%s' "${ref#"${IMAGE_REPO}":}" | cut -c1-12)" = "${sha:0:12}" ] \
    || refuse "the image tag does not match commit ${sha:0:12}"
  [ -n "${CORE_LOCK_TOKEN:-}" ] \
    || refuse "CORE_LOCK_TOKEN is not set on this repo (docs/agent/runbooks/image-from-ream-homelab.md, step 1)"

  local sha12="${sha:0:12}"
  work=$(mktemp -d)  # global: the EXIT trap runs after main returns
  trap 'rm -rf "$work"' EXIT

  # git reads the token through askpass, from the environment: never argv, never a file.
  # shellcheck disable=SC2016  # $1 and $CORE_LOCK_TOKEN expand when git runs it, not here
  printf '#!/bin/sh\ncase "$1" in Username*) echo artifacts-ci ;; *) printf %%s "$CORE_LOCK_TOKEN" ;; esac\n' > "$work/askpass"
  chmod 700 "$work/askpass"
  export GIT_ASKPASS="$work/askpass" GIT_TERMINAL_PROMPT=0

  git clone --quiet --depth 1 --branch "$BASE_BRANCH" "$FORGE/$TARGET.git" "$work/target" \
    || failed "could not clone $TARGET"
  local file="$work/target/$LOCK_FILE" cur
  [ -f "$file" ] || refuse "$LOCK_FILE is not in $TARGET"
  cur=$(current_sha12 "$file")
  [ -n "$cur" ] || refuse "$LOCK_FILE in $TARGET is not exactly one ${IMAGE_REPO} ref"

  case "$(decide . "$cur" "$sha")" in
    same)    say "$LOCK_FILE already names $sha12: nothing to do"; return 0 ;;
    behind)  say "$LOCK_FILE names $cur, which is not an ancestor of $sha12: a newer run won; standing down"; return 0 ;;
    unknown) refuse "$LOCK_FILE names $cur, which is not a commit in this repo's history" ;;
    go)      ;;
    *)       failed "could not decide" ;;
  esac

  rewrite_lock "$file" "$ref" || refuse "the rewrite did not change $LOCK_FILE"
  git -C "$work/target" -c user.name=artifacts-ci -c user.email=ci@git.tenfourty.site \
    commit --quiet -m "core: lock $sha12" \
    -m "Moves core.lock from $cur to $sha12 (runcove/artifacts@$sha). Opened by artifacts CI; only the lock changes." \
    -- "$LOCK_FILE" || failed "commit failed"
  # --force only ever touches this script's own core-lock/next branch.
  git -C "$work/target" push --quiet --force "$FORGE/$TARGET.git" "HEAD:refs/heads/$BRANCH" \
    || failed "push to $TARGET failed"

  local title="core: lock $sha12" body n code pushed
  pushed=$(git -C "$work/target" rev-parse HEAD)
  # shellcheck disable=SC2016  # the backticks are Markdown in the PR body
  body=$(printf '{"title":"%s","head":"%s","base":"%s","body":"Moves `core.lock` from `%s` to `%s` ([runcove/artifacts@%s](%s/runcove/artifacts/commit/%s)). Opened by artifacts CI after the tests and core image passed; merges by itself when the checks on this repo succeed."}' \
    "$title" "$BRANCH" "$BASE_BRANCH" "$cur" "$sha12" "$sha12" "$FORGE" "$sha")
  [ "$(forge GET "/repos/$TARGET/pulls?state=open&limit=50" "" "$work/open.json")" = 200 ] \
    || failed "could not list open PRs on $TARGET"
  n=$(open_pr_number "$work/open.json")
  if [ -n "$n" ]; then
    verify_pr_head "$n" "$pushed"
    code=$(forge PATCH "/repos/$TARGET/pulls/$n" "$(printf '{"title":"%s"}' "$title")" "$work/pr.json") \
      || failed "the PR update call failed"
    [ "$code" = 201 ] || [ "$code" = 200 ] || failed "updating PR #$n returned HTTP $code"
    say "updated #$n (the push above moved its branch)"
  else
    code=$(forge POST "/repos/$TARGET/pulls" "$body" "$work/pr.json") || failed "the PR call failed"
    [ "$code" = 201 ] || failed "opening the PR returned HTTP $code"
    n=$(grep -oE '"number":[0-9]+' "$work/pr.json" | head -1 | cut -d: -f2)
    printf '%s' "$n" | grep -Eqx '[0-9]+' || failed "no PR number in the answer"
    say "opened $FORGE/$TARGET/pulls/$n"
  fi

  # Whichever PR it is, its head must be what we pushed before it is set to merge itself.
  verify_pr_head "$n" "$pushed"
  # Merge when checks succeed: the forge merges it itself once the combined suite is green.
  code=$(forge POST "/repos/$TARGET/pulls/$n/merge" \
    '{"Do":"merge","merge_when_checks_succeed":true,"delete_branch_after_merge":true}' "$work/merge.json") \
    || failed "the merge call failed"
  case "$code" in
    200|201|204) say "#$n will merge when checks succeed" ;;
    405|409)     say "#$n: merge already scheduled or not possible yet (HTTP $code); a person can look" ;;
    *)           failed "asking to merge #$n when checks succeed returned HTTP $code" ;;
  esac
}

selftest() {
  local pass=0 d a b c dg
  t=$(mktemp -d)  # global: the EXIT trap runs after selftest returns
  trap 'rm -rf "$t"' EXIT
  bad()    { printf 'core-lock-pr selftest FAILED: %s\n' "$1" >&2; exit 1; }
  expect() { local why="$1"; shift; if "$@"; then pass=$((pass + 1)); else bad "$why"; fi; }
  refuses() { local why="$1"; shift; if "$@"; then bad "$why"; else pass=$((pass + 1)); fi; }
  dg="sha256:$(printf 'a%.0s' $(seq 64))"

  expect  "a good ref refused"            is_core_ref "$IMAGE_REPO:0123456789ab@$dg"
  refuses "a ref without digest accepted" is_core_ref "$IMAGE_REPO:0123456789ab"
  refuses "the production image accepted" is_core_ref "git.tenfourty.site/runcove/artifacts:0123456789ab@$dg"
  refuses "another repo accepted"         is_core_ref "evil.example/artifacts-core:0123456789ab@$dg"
  refuses "a short digest accepted"       is_core_ref "$IMAGE_REPO:0123456789ab@sha256:abc"
  refuses "a trailing line accepted"      is_core_ref "$IMAGE_REPO:0123456789ab@$dg
extra"
  refuses "an upper-case tag accepted"    is_core_ref "$IMAGE_REPO:0123456789AB@$dg"

  printf '%s:ab9be7105a92@%s\n' "$IMAGE_REPO" "$dg" > "$t/core.lock"
  expect  "current sha12 not read"        [ "$(current_sha12 "$t/core.lock")" = "ab9be7105a92" ]
  expect  "a rewrite refused"             rewrite_lock "$t/core.lock" "$IMAGE_REPO:0123456789ab@$dg"
  expect  "the rewrite wrote the wrong content" [ "$(cat "$t/core.lock")" = "$IMAGE_REPO:0123456789ab@$dg" ]
  expect  "the rewrite changed the line count" [ "$(grep -c . "$t/core.lock")" -eq 1 ]
  refuses "a no-op rewrite accepted"      rewrite_lock "$t/core.lock" "$IMAGE_REPO:0123456789ab@$dg"
  printf 'junk\n' > "$t/core.lock"
  expect  "a junk lock read as a sha"     [ -z "$(current_sha12 "$t/core.lock")" ]
  printf '%s:111111111111@%s\n%s:222222222222@%s\n' "$IMAGE_REPO" "$dg" "$IMAGE_REPO" "$dg" > "$t/core.lock"
  expect  "two refs read as one"          [ -z "$(current_sha12 "$t/core.lock")" ]

  d="$t/repo"
  git init -q "$d"
  git -C "$d" -c user.name=t -c user.email=t@t commit -q --allow-empty -m one
  a=$(git -C "$d" rev-parse HEAD)
  git -C "$d" -c user.name=t -c user.email=t@t commit -q --allow-empty -m two
  b=$(git -C "$d" rev-parse HEAD)
  git -C "$d" checkout -q "$a"
  git -C "$d" -c user.name=t -c user.email=t@t commit -q --allow-empty -m side
  c=$(git -C "$d" rev-parse HEAD)
  expect "an ancestor lock did not go"        [ "$(decide "$d" "${a:0:12}" "$b")" = go ]
  expect "a newer lock did not stand down"    [ "$(decide "$d" "${b:0:12}" "$a")" = behind ]
  expect "a diverged lock did not stand down" [ "$(decide "$d" "${b:0:12}" "$c")" = behind ]
  expect "the same lock did not read as same" [ "$(decide "$d" "${b:0:12}" "$b")" = same ]
  expect "an unknown lock was not unknown"    [ "$(decide "$d" "fedcba987654" "$b")" = unknown ]

  if command -v python3 >/dev/null 2>&1; then
    printf '[{"number":4,"head":{"ref":"other"}},{"number":7,"head":{"ref":"%s","repo":{"full_name":"%s"}}}]' "$BRANCH" "$TARGET" > "$t/open.json"
    expect "our open PR not found"  [ "$(open_pr_number "$t/open.json")" = 7 ]
    printf '[{"number":4,"head":{"ref":"other"}}]' > "$t/open.json"
    expect "another PR picked"      [ -z "$(open_pr_number "$t/open.json")" ]
    printf '[{"number":8,"head":{"ref":"%s","repo":{"full_name":"evil/ream-homelab"}}}]' "$BRANCH" > "$t/open.json"
    expect "a fork's PR on our branch name picked" [ -z "$(open_pr_number "$t/open.json")" ]
    printf '{"head":{"ref":"%s","sha":"abc","repo":{"full_name":"%s"}}}' "$BRANCH" "$TARGET" > "$t/pr.json"
    expect  "a PR at the pushed sha refused" pr_head_is "$t/pr.json" abc
    refuses "a PR at another sha accepted" pr_head_is "$t/pr.json" abd
    printf '{"head":{"ref":"%s","sha":"abc","repo":{"full_name":"evil/x"}}}' "$BRANCH" > "$t/pr.json"
    refuses "a fork's PR head accepted" pr_head_is "$t/pr.json" abc
    printf 'not json' > "$t/pr.json"
    refuses "an unreadable PR accepted" pr_head_is "$t/pr.json" abc
    printf 'not json' > "$t/open.json"
    expect "an unreadable list picked something" [ -z "$(open_pr_number "$t/open.json")" ]
  fi

  printf 'core-lock-pr selftest: %d checks passed\n' "$pass"
}

case "${1:-}" in
  --selftest) selftest ;;
  *) main "$@" ;;
esac
