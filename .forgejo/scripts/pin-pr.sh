#!/usr/bin/env bash
# pin-pr.sh <sha> <image-ref> -- after main's image job, open the pull request on the
# homelab repo that moves artifacts onto that image. It changes ONE line: the `image:` in
# k8s/apps/artifacts/artifacts/app/deployment.yaml (homelab-qa3c, 15 Oct 2026). The
# homelab maintainer reviews and merges it; nothing here can reach homelab's main.
#
# It pushes only to the pin bot's own fork (homelab-pin-bot/homelab, Actions off), on a
# branch named artifacts-pin/<sha12>, with PIN_PR_TOKEN: a token of an account that only
# reads the homelab repo and has no access to this one. Warpgate's pin path does the same
# (runcove warpgate-ci/scripts/pin-bump.sh).
#
# Stands down (exit 0, nothing pushed) when homelab already pins this commit, or pins a
# commit that is NOT an ancestor of this one -- a newer run got there first, and this run
# must not move production back. Needs the full history of this repo (fetch-depth: 0).
#
# Exit 0: a PR is open for this image (new or already there), or a stated stand-down.
# Exit 64: refused (bad input, or the rewrite did not change exactly one line).
# Exit 65: a git or forge call failed.
#
# pin-pr.sh --selftest runs its checks against throwaway files and repos, offline.
set -u

FORGE="${PIN_FORGE:-https://git.tenfourty.site}"
UPSTREAM="${PIN_UPSTREAM:-jeremy/homelab}"
FORK="${PIN_FORK:-homelab-pin-bot/homelab}"
BASE_BRANCH="${PIN_BASE_BRANCH:-main}"
PIN_FILE="${PIN_FILE:-k8s/apps/artifacts/artifacts/app/deployment.yaml}"
IMAGE_REPO="git.tenfourty.site/runcove/artifacts"

say()     { printf 'pin-pr: %s\n' "$*"; }
refuse()  { printf 'pin-pr: REFUSING: %s\n' "$*" >&2; exit 64; }
failed()  { printf 'pin-pr: FAILED: %s\n' "$*" >&2; exit 65; }

# <repo>:<sha12>@sha256:<64 hex>, for this repo's image only.
is_image_ref() {
  printf '%s' "$1" | grep -Eqx "${IMAGE_REPO//./\\.}:[0-9a-f]{12}@sha256:[0-9a-f]{64}"
}

branch_for() { printf 'artifacts-pin/%s' "${1:0:12}"; }

# The current pinned sha12 in <file>; empty unless exactly one image line names our repo.
current_sha12() {
  local lines
  lines=$(grep -E "^[[:space:]]*image:[[:space:]]*${IMAGE_REPO//./\\.}:" "$1") || return 0
  [ "$(printf '%s\n' "$lines" | wc -l)" -eq 1 ] || return 0
  printf '%s\n' "$lines" | sed -E 's#.*:([0-9a-f]{12})(@sha256:[0-9a-f]{64})?[[:space:]]*$#\1#' \
    | grep -Ex '[0-9a-f]{12}'
}

# Rewrite the one image line in <file> to <image-ref>; fail unless exactly one line changed.
rewrite_pin() {
  local file="$1" ref="$2" before
  before=$(mktemp)
  cp "$file" "$before"
  sed -i -E "s#^([[:space:]]*image:[[:space:]]*)${IMAGE_REPO//./\\.}:[^[:space:]]*#\\1${ref}#" "$file"
  local changed
  changed=$(diff "$before" "$file" | grep -c '^>' || true)
  rm -f "$before"
  [ "$changed" = "1" ]
}

# Decide from <repo dir>, <current sha12>, <our sha>: prints go, same, or behind.
decide() {
  local dir="$1" cur="$2" ours="$3" full
  [ "${ours:0:12}" = "$cur" ] && { echo same; return 0; }
  full=$(git -C "$dir" rev-parse --verify --quiet "${cur}^{commit}") || { echo unknown; return 0; }
  if git -C "$dir" merge-base --is-ancestor "$full" "$ours"; then echo go; else echo behind; fi
}

# Open pin PRs of this fork that this one supersedes: branch artifacts-pin/<sha12> (not ours)
# whose commit is an ANCESTOR of <our sha> in <repo dir>. Reads the open-PR list from <json file>,
# prints one PR number per line. Anything it cannot read or resolve is left alone.
superseded_prs() { # <json file> <repo dir> <our sha> <our branch> <fork owner>
  command -v python3 >/dev/null 2>&1 || return 0
  local number sha12 full
  python3 - "$1" "$4" "$5" <<'PY' | while read -r number sha12; do
import json, re, sys
path, ours, owner = sys.argv[1:4]
try:
    prs = json.load(open(path))
except (OSError, ValueError):
    sys.exit(0)
for pr in prs if isinstance(prs, list) else []:
    head = pr.get("head") or {}
    ref = head.get("ref") or ""
    user = ((head.get("repo") or {}).get("owner") or {}).get("login") or ""
    m = re.fullmatch(r"artifacts-pin/([0-9a-f]{12})", ref)
    if m and ref != ours and user == owner and isinstance(pr.get("number"), int):
        print(pr["number"], m.group(1))
PY
    full=$(git -C "$2" rev-parse --verify --quiet "${sha12}^{commit}") || continue
    if git -C "$2" merge-base --is-ancestor "$full" "$3"; then printf '%s\n' "$number"; fi
  done
}

# One forge call; the token goes to curl on stdin as a config line, never on argv.
forge() { # <method> <path> <body-or-empty> <outfile>
  local method="$1" path="$2" body="$3" out="$4"
  if [ -n "$body" ]; then
    printf 'header = "Authorization: token %s"\n' "$PIN_PR_TOKEN" \
      | curl -sS --config - --max-time 30 -o "$out" -w '%{http_code}' -X "$method" \
          -H 'Content-Type: application/json' --data "$body" "$FORGE/api/v1$path"
  else
    printf 'header = "Authorization: token %s"\n' "$PIN_PR_TOKEN" \
      | curl -sS --config - --max-time 30 -o "$out" -w '%{http_code}' -X "$method" \
          "$FORGE/api/v1$path"
  fi
}

# The head commit of <repo>'s <branch>, or nothing.
branch_head() {
  local out="$work/branch.json"
  [ "$(forge GET "/repos/$1/branches/$2" "" "$out")" = 200 ] || return 0
  grep -oE '"id":"[0-9a-f]{40}"' "$out" | head -1 | cut -d'"' -f4
}

# The clone is shallow, and the forge refuses a shallow push to a fork that lacks
# its base commit ("shallow update not allowed"), so bring the fork's base branch
# level with the upstream's first. The forge answers 400 when they are already level.
sync_fork() {
  local fh uh code
  fh=$(branch_head "$FORK" "$BASE_BRANCH")
  uh=$(branch_head "$UPSTREAM" "$BASE_BRANCH")
  [ -n "$uh" ] || failed "could not read $UPSTREAM's $BASE_BRANCH"
  [ "$fh" = "$uh" ] && return 0
  code=$(forge POST "/repos/$FORK/sync_fork/$BASE_BRANCH" '{}' "$work/sync.json") || failed "the fork sync call failed"
  case "$code" in 200|204) say "synced $FORK's $BASE_BRANCH with $UPSTREAM" ;; *) failed "syncing $FORK answered HTTP $code" ;; esac
}

main() {
  local sha="${1:-}" ref="${2:-}"
  printf '%s' "$sha" | grep -Eqx '[0-9a-f]{40}' || refuse "'$sha' is not a full commit sha"
  is_image_ref "$ref" || refuse "'$ref' is not ${IMAGE_REPO}:<sha12>@sha256:<digest>"
  [ "${ref#"${IMAGE_REPO}":}" != "$ref" ] && [ "$(printf '%s' "${ref#"${IMAGE_REPO}":}" | cut -c1-12)" = "${sha:0:12}" ] \
    || refuse "the image tag does not match commit ${sha:0:12}"
  [ -n "${PIN_PR_TOKEN:-}" ] || refuse "PIN_PR_TOKEN is not set"

  local branch sha12="${sha:0:12}"
  work=$(mktemp -d)  # global: the EXIT trap runs after main returns
  trap 'rm -rf "$work"' EXIT
  branch=$(branch_for "$sha")
  case "$branch" in artifacts-pin/[0-9a-f]*) ;; *) refuse "unsafe branch name $branch" ;; esac

  # git reads the token through askpass, from the environment: never argv, never a file.
  # shellcheck disable=SC2016  # $1 and $PIN_PR_TOKEN expand when git runs it, not here
  printf '#!/bin/sh\ncase "$1" in Username*) echo homelab-pin-bot ;; *) printf %%s "$PIN_PR_TOKEN" ;; esac\n' > "$work/askpass"
  chmod 700 "$work/askpass"
  export GIT_ASKPASS="$work/askpass" GIT_TERMINAL_PROMPT=0

  git clone --quiet --depth 1 --branch "$BASE_BRANCH" "$FORGE/$UPSTREAM.git" "$work/homelab" \
    || failed "could not clone $UPSTREAM"
  local file="$work/homelab/$PIN_FILE" cur verdict
  [ -f "$file" ] || refuse "$PIN_FILE is not in $UPSTREAM"
  cur=$(current_sha12 "$file")
  [ -n "$cur" ] || refuse "$PIN_FILE does not have exactly one image line for $IMAGE_REPO"

  verdict=$(decide . "$cur" "$sha")
  case "$verdict" in
    same)    say "homelab already pins $sha12: nothing to do"; return 0 ;;
    behind)  say "homelab pins $cur, which is not an ancestor of $sha12: a newer run won; standing down"; return 0 ;;
    unknown) refuse "homelab pins $cur, which is not a commit in this repo's history" ;;
    go)      ;;
    *)       failed "could not decide ($verdict)" ;;
  esac

  rewrite_pin "$file" "$ref" || refuse "the rewrite did not change exactly one line of $PIN_FILE"
  git -C "$work/homelab" -c user.name=artifacts-ci -c user.email=ci@git.tenfourty.site \
    commit --quiet -m "artifacts: pin $sha12" \
    -m "Moves artifacts from $cur to $sha12 (runcove/artifacts@$sha). Opened by artifacts CI; only the image line changes." \
    -- "$PIN_FILE" || failed "commit failed"
  sync_fork
  # --force only ever touches the pin bot's own artifacts-pin/<sha12> branch on its fork.
  git -C "$work/homelab" push --quiet --force "$FORGE/$FORK.git" "HEAD:refs/heads/$branch" \
    || failed "push to $FORK failed"

  local body code
  # shellcheck disable=SC2016  # the backticks are Markdown in the PR body
  body=$(printf '{"title":"artifacts: pin %s","head":"%s:%s","base":"%s","body":"Moves artifacts from `%s` to `%s` ([runcove/artifacts@%s](%s/runcove/artifacts/commit/%s)). Opened by artifacts CI after the tests and image passed; only the `image:` line in `%s` changes."}' \
    "$sha12" "${FORK%%/*}" "$branch" "$BASE_BRANCH" "$cur" "$sha12" "$sha12" "$FORGE" "$sha" "$PIN_FILE")
  code=$(forge POST "/repos/$UPSTREAM/pulls" "$body" "$work/pr.json") || failed "the PR call failed"
  case "$code" in
    201) say "opened $(grep -oE '"html_url":"[^"]*/pulls/[0-9]+"' "$work/pr.json" | head -1 | cut -d'"' -f4)" ;;
    409|422) say "a PR for $branch is already open: the push above updated it" ;;
    *) failed "opening the PR returned HTTP $code" ;;
  esac
  close_superseded "$sha" "$branch"
}

# Close the older open pin PRs this one replaces, so they do not pile up. Best effort: the pin
# bot may lack the right to close a PR; that is said and never fails the job (exit 0 stays "a PR
# is open for this image"). Only PRs from the bot's own fork on artifacts-pin/ branches whose
# commit is an ancestor of this one are touched.
close_superseded() { # <our sha> <our branch>
  local n
  [ "$(forge GET "/repos/$UPSTREAM/pulls?state=open&limit=50" "" "$work/open.json")" = 200 ] \
    || { say "could not list open PRs: not closing superseded pins"; return 0; }
  for n in $(superseded_prs "$work/open.json" . "$1" "$2" "${FORK%%/*}"); do
    if [ "$(forge PATCH "/repos/$UPSTREAM/pulls/$n" '{"state":"closed"}' "$work/close.json")" = 201 ] \
       || grep -q '"state":"closed"' "$work/close.json" 2>/dev/null; then
      say "closed #$n, superseded by $2"
    else
      say "could not close #$n (superseded by $2); a person can close it"
    fi
  done
}

selftest() {
  local pass=0 d a b c dg
  t=$(mktemp -d)  # global: the EXIT trap runs after selftest returns
  trap 'rm -rf "$t"' EXIT
  bad()    { printf 'pin-pr selftest FAILED: %s\n' "$1" >&2; exit 1; }
  expect() { local why="$1"; shift; if "$@"; then pass=$((pass + 1)); else bad "$why"; fi; }
  refuses() { local why="$1"; shift; if "$@"; then bad "$why"; else pass=$((pass + 1)); fi; }
  dg="sha256:$(printf 'a%.0s' $(seq 64))"

  expect  "a good ref refused"            is_image_ref "$IMAGE_REPO:0123456789ab@$dg"
  refuses "a ref without digest accepted" is_image_ref "$IMAGE_REPO:0123456789ab"
  refuses "another repo accepted"         is_image_ref "evil.example/artifacts:0123456789ab@$dg"
  refuses "a short digest accepted"       is_image_ref "$IMAGE_REPO:0123456789ab@sha256:abc"
  expect  "branch name" [ "$(branch_for 0123456789abcdef0123456789abcdef01234567)" = "artifacts-pin/0123456789ab" ]

  printf 'spec:\n  containers:\n    - name: artifacts\n      image: %s:ab9be7105a92@%s\n      ports: []\n' "$IMAGE_REPO" "$dg" > "$t/d.yaml"
  expect  "current sha12 not read"        [ "$(current_sha12 "$t/d.yaml")" = "ab9be7105a92" ]
  expect  "a one-line rewrite refused"    rewrite_pin "$t/d.yaml" "$IMAGE_REPO:0123456789ab@$dg"
  expect  "the rewrite wrote the wrong line" grep -q "image: $IMAGE_REPO:0123456789ab@$dg\$" "$t/d.yaml"
  expect  "the rewrite changed the line count" [ "$(grep -c . "$t/d.yaml")" -eq 5 ]
  refuses "a no-op rewrite accepted"      rewrite_pin "$t/d.yaml" "$IMAGE_REPO:0123456789ab@$dg"
  printf '      image: %s:111111111111\n' "$IMAGE_REPO" >> "$t/d.yaml"
  expect  "two image lines read as one"   [ -z "$(current_sha12 "$t/d.yaml")" ]
  refuses "a two-line rewrite accepted"   rewrite_pin "$t/d.yaml" "$IMAGE_REPO:222222222222@$dg"

  d="$t/repo"
  git init -q "$d"
  git -C "$d" -c user.name=t -c user.email=t@t commit -q --allow-empty -m one
  a=$(git -C "$d" rev-parse HEAD)
  git -C "$d" -c user.name=t -c user.email=t@t commit -q --allow-empty -m two
  b=$(git -C "$d" rev-parse HEAD)
  git -C "$d" checkout -q "$a"
  git -C "$d" -c user.name=t -c user.email=t@t commit -q --allow-empty -m side
  c=$(git -C "$d" rev-parse HEAD)
  expect "an ancestor pin did not go"        [ "$(decide "$d" "${a:0:12}" "$b")" = go ]
  expect "a newer pin did not stand down"    [ "$(decide "$d" "${b:0:12}" "$a")" = behind ]
  expect "a diverged pin did not stand down" [ "$(decide "$d" "${b:0:12}" "$c")" = behind ]
  expect "the same pin did not read as same" [ "$(decide "$d" "${b:0:12}" "$b")" = same ]
  expect "an unknown pin was not unknown"    [ "$(decide "$d" "fedcba987654" "$b")" = unknown ]

  # superseded_prs: an older pin of OUR fork on an artifacts-pin/ branch is picked, a newer one,
  # another owner's, another branch name and ourselves are not.
  {
    printf '[{"number":5,"head":{"ref":"artifacts-pin/%s","repo":{"owner":{"login":"homelab-pin-bot"}}}},' "${a:0:12}"
    printf '{"number":6,"head":{"ref":"artifacts-pin/%s","repo":{"owner":{"login":"homelab-pin-bot"}}}},' "${c:0:12}"
    printf '{"number":7,"head":{"ref":"artifacts-pin/%s","repo":{"owner":{"login":"someone-else"}}}},' "${a:0:12}"
    printf '{"number":8,"head":{"ref":"warpgate-pin/%s","repo":{"owner":{"login":"homelab-pin-bot"}}}},' "${a:0:12}"
    printf '{"number":9,"head":{"ref":"artifacts-pin/%s","repo":{"owner":{"login":"homelab-pin-bot"}}}}]' "${b:0:12}"
  } > "$t/open.json"
  if command -v python3 >/dev/null 2>&1; then
    expect "an ancestor pin PR was not picked, or another was" \
      [ "$(superseded_prs "$t/open.json" "$d" "$b" "artifacts-pin/${b:0:12}" homelab-pin-bot | tr '\n' ' ')" = "5 " ]
    expect "a diverged pin PR was picked" \
      [ -z "$(superseded_prs "$t/open.json" "$d" "$a" "artifacts-pin/${a:0:12}" homelab-pin-bot)" ]
    printf 'not json' > "$t/bad.json"
    expect "an unreadable list picked something" [ -z "$(superseded_prs "$t/bad.json" "$d" "$b" x homelab-pin-bot)" ]
  fi

  printf 'pin-pr selftest: %d checks passed\n' "$pass"
}

case "${1:-}" in
  --selftest) selftest ;;
  *) main "$@" ;;
esac
