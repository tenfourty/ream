#!/usr/bin/env bash
# scripts/guards/push-count-guard.sh — refuse a push whose outgoing range is
# not the N commits the pusher announced.
#
# Ported from the deployment's own push-count guard (21 Sep 2026); the logic is
# theirs, the reasons are this repo's.
#
# WHY HERE. AGENTS.md: "Announce a push, with hashes, before making it.
# Verify what actually went out against the remote." A push from this repo is
# a production change — CI builds an image from it and Flux rolls it out
# minutes later, unattended — and the outgoing range moves under the
# announcement in a way that is specific to this repo: CI pushes a
# `deploy: … [skip ci]` commit back onto main after every build, so the next
# push must fetch and rebase first, and a rebase changes the hashes you
# announced. It does not change the COUNT, which is exactly why a count is
# the right mechanical form: it stays true across the rebase you are supposed
# to do, and breaks the moment the range is not the one you looked at.
# A green deploy can also mean "stood down" when a second push lands behind
# the first, so pushing an unexamined range is how a feature silently fails
# to ship.
#
# WHAT: a lefthook `pre-push` command. It reads the standard pre-push lines on
# stdin, one per ref being pushed:
#   <local ref> SP <local sha1> SP <remote ref> SP <remote sha1>
# For each line: a local sha of all zeros is a deletion and pushes zero
# commits (skipped); a remote sha of all zeros means the REF is new to the
# remote (a new branch, or a tag), whose count is every commit reachable from
# the local sha that is NOT already reachable from some remote-tracking ref
# (`git rev-list --count <local sha> --not --remotes`) — NOT a flat rev-list
# of the local sha's whole history, because a new ref usually shares history
# with what the remote already has: a tag on an already-pushed commit pushes
# zero new commits, and counting its full ancestry would demand an absurd
# PUSH_COUNT. Counts are summed across lines (normally there is one). The sum
# must equal PUSH_COUNT:
#   - PUSH_COUNT unset or empty  -> REFUSED. An opt-in guard only ever stops
#     the careful, so unset must refuse, not pass through.
#   - PUSH_COUNT not a non-negative integer -> REFUSED (usage error).
#   - PUSH_COUNT != the actual sum -> REFUSED. Two usual causes: the range
#     moved after you counted it (fetch + rebase, or CI's deploy commit
#     landed), or PUSH_COUNT came from a stale remote-tracking ref.
#   - PUSH_COUNT == the actual sum -> OK, exit 0.
#
# SCOPE: this guard catches a range that changed size after you counted it.
# It cannot catch a wrong announcement, and it cannot catch a rebase that
# kept the count and changed every hash — only reachability afterwards
# (`git merge-base --is-ancestor <hash> origin/main` after a fetch) does that,
# which is why AGENTS.md asks for that check too.
#
# LEFTHOOK DISPATCH (homelab trap 48): lefthook hard-codes every `pre-push`
# command to skip — "(skip) no matching push files", exit 0, command never
# run — whenever `git diff --name-only HEAD @{push}` is empty (a deletion-only
# push, an all-`--allow-empty` push, a non-HEAD ref push), with no per-command
# YAML option to disable it. A script cannot defend against that: the decision
# is made before it is invoked. Closed in lefthook.yml by pairing `files:`
# with a `{files}` reference in `run:`, which trips an earlier, unconditional
# return in lefthook's dispatch. Durable regression:
# scripts/tests/prepush-hooks-fire.sh — it drives a real `lefthook install`
# and a real `git push`, because --selftest below invokes this script directly
# and cannot see a lefthook-level skip at all.
#
# There is no override. There is no emergency case for pushing an unknown
# number of commits to a branch that deploys itself.
#
# Usage: PUSH_COUNT=<n> git push
# Selftest: scripts/guards/push-count-guard.sh --selftest
set -euo pipefail

ZERO_RE='^0+$'
SCOPE_LINE="Scope: this guard catches a range that changed size after you counted it; it cannot catch a wrong announcement, only reachability afterwards does that."

is_zero() { [[ "$1" =~ $ZERO_RE ]]; }

usage() {
  echo "usage: PUSH_COUNT=<n> git push   -- n is the commit count you announced" >&2
  echo "       $0 --selftest" >&2
}

# Reads pre-push lines from stdin. Sets TOTAL (sum of outgoing commits across
# every ref line) and RANGE_TEXT (one labelled hash+subject block per
# non-deletion line, each capped at 30 lines).
read_pushed_range() {
  TOTAL=0
  LINES=0
  RANGE_TEXT=""
  local local_ref local_sha remote_ref remote_sha n
  while read -r local_ref local_sha remote_ref remote_sha; do
    [ -z "${local_ref:-}" ] && continue
    LINES=$((LINES + 1))
    if is_zero "$local_sha"; then
      continue # deletion — pushes zero commits
    fi
    if is_zero "$remote_sha"; then
      n=$(git rev-list --count "$local_sha" --not --remotes)
      RANGE_TEXT+="# $local_ref -- new ref (remote $remote_ref absent), $n commit(s) not already on a remote-tracking ref:"$'\n'
      # `git log -n 30`, never `git log | head -30`: under `set -euo pipefail`
      # a range longer than head's window kills git with SIGPIPE, the pipeline
      # returns 141 and the guard DIES instead of deciding.
      RANGE_TEXT+="$(git log -n 30 --format='%h %s' "$local_sha" --not --remotes)"$'\n'
    else
      n=$(git rev-list --count "$remote_sha".."$local_sha")
      RANGE_TEXT+="# $local_ref vs remote $remote_ref, $n commit(s):"$'\n'
      RANGE_TEXT+="$(git log -n 30 --format='%h %s' "$remote_sha".."$local_sha")"$'\n'
    fi
    TOTAL=$((TOTAL + n))
  done
}

print_range() {
  echo "Outgoing range ($TOTAL commit(s)):" >&2
  printf '%s\n' "$RANGE_TEXT" | sed 's/^/  /' >&2
}

check() {
  local input
  input=$(cat)
  read_pushed_range <<< "$input"

  # No ref lines at all. Measured 22 Sep 2026 on a real non-fast-forward push:
  # git still runs the hook, but hands it nothing, so the guard read "0
  # commits" and asked for PUSH_COUNT=0 — advice that is both useless and
  # impossible to satisfy, since the remote rejects the push anyway. Say what
  # is actually wrong instead.
  if [ "${LINES:-0}" -eq 0 ]; then
    echo "push-count-guard: REFUSED -- git passed this hook no refs at all." >&2
    echo "That is what a non-fast-forward push looks like from in here: fetch and" >&2
    echo "rebase (CI pushes a 'deploy: … [skip ci]' commit onto main after every" >&2
    echo "build), then re-count and push again." >&2
    return 1
  fi

  if [ -z "${PUSH_COUNT:-}" ]; then
    echo "push-count-guard: REFUSED -- PUSH_COUNT is not set." >&2
    print_range
    echo "Announce these $TOTAL hash(es), then PUSH_COUNT=$TOTAL git push." >&2
    echo "$SCOPE_LINE" >&2
    return 1
  fi

  if ! [[ "$PUSH_COUNT" =~ ^[0-9]+$ ]]; then
    echo "push-count-guard: REFUSED -- PUSH_COUNT=\"$PUSH_COUNT\" is not a non-negative integer." >&2
    usage
    return 1
  fi

  if [ "$PUSH_COUNT" -ne "$TOTAL" ]; then
    echo "push-count-guard: REFUSED -- PUSH_COUNT=$PUSH_COUNT but the outgoing range has $TOTAL commit(s)." >&2
    print_range
    echo "Two usual causes: (1) the range moved after you counted it -- you fetched and" >&2
    echo "rebased, or CI's deploy commit landed; re-count and re-announce. (2) your N came" >&2
    echo "from a stale remote-tracking ref -- fetch first." >&2
    echo "$SCOPE_LINE" >&2
    return 1
  fi

  echo "push-count-guard: OK, $TOTAL commit(s)"
}

# Runs the guard as a fresh subprocess against $clone, feeding it $1 on stdin
# and the remaining args to `env`. Sets OUT and CODE. Always starts from an
# unset PUSH_COUNT so no ambient value leaks into a case.
run_guard() {
  local line=$1; shift
  set +e
  OUT=$(cd "$clone" && printf '%s\n' "$line" | env -u PUSH_COUNT "$@" bash "$self" 2>&1)
  CODE=$?
  set -e
}

selftest() {
  local stage
  stage=$(mktemp -d)
  local self
  self=$(cd -- "$(dirname -- "$0")" >/dev/null 2>&1 && pwd)/$(basename -- "$0")
  local zero="0000000000000000000000000000000000000000"
  local rc=0
  local clone bare gc

  trap 'rm -rf "$stage"' RETURN

  bare="$stage/origin.git"
  clone="$stage/work"
  git init -q --bare "$bare"
  git clone -q "$bare" "$clone"
  gc=(-c user.name=push-count-guard-selftest -c user.email=selftest@example.invalid)

  git -C "$clone" "${gc[@]}" commit -q --allow-empty -m "commit0"
  git -C "$clone" push -q origin HEAD:main
  git -C "$clone" fetch -q origin
  local remote0
  remote0=$(git -C "$clone" rev-parse refs/remotes/origin/main)

  git -C "$clone" "${gc[@]}" commit -q --allow-empty -m "commit1"
  git -C "$clone" "${gc[@]}" commit -q --allow-empty -m "commit2"
  local local2
  local2=$(git -C "$clone" rev-parse HEAD)
  local line_ab="refs/heads/main $local2 refs/heads/main $remote0"

  # (a) PUSH_COUNT unset, 2 outgoing -> refused, names PUSH_COUNT, shows 2 lines
  run_guard "$line_ab"
  if [ "$CODE" -ne 0 ] && grep -q "PUSH_COUNT" <<< "$OUT" \
     && [ "$(grep -cE '^  [0-9a-f]{7,40} ' <<< "$OUT")" -eq 2 ]; then
    echo "PASS (a) unset refused, names PUSH_COUNT, shows 2 lines"
  else
    echo "FAIL (a) unset with 2 outgoing: code=$CODE"; printf '%s\n' "$OUT" | sed 's/^/    /'; rc=1
  fi

  # (b) PUSH_COUNT=1, 2 outgoing -> refused, both numbers present
  run_guard "$line_ab" PUSH_COUNT=1
  if [ "$CODE" -ne 0 ] && grep -q "PUSH_COUNT=1" <<< "$OUT" && grep -q "2 commit" <<< "$OUT"; then
    echo "PASS (b) PUSH_COUNT=1 with 2 outgoing refused, both numbers present"
  else
    echo "FAIL (b) PUSH_COUNT=1 with 2 outgoing: code=$CODE"; printf '%s\n' "$OUT" | sed 's/^/    /'; rc=1
  fi

  # (c) PUSH_COUNT=2, 2 outgoing -> OK
  run_guard "$line_ab" PUSH_COUNT=2
  if [ "$CODE" -eq 0 ] && grep -q "OK, 2 commit" <<< "$OUT"; then
    echo "PASS (c) PUSH_COUNT=2 with 2 outgoing allowed"
  else
    echo "FAIL (c) PUSH_COUNT=2 with 2 outgoing: code=$CODE"; printf '%s\n' "$OUT" | sed 's/^/    /'; rc=1
  fi

  # (d) new branch (remote sha zeros): the count is commits not already on a
  # remote-tracking ref, not the whole local history.
  git -C "$clone" checkout -q -b feature
  git -C "$clone" "${gc[@]}" commit -q --allow-empty -m "commit3"
  local local3 fullcount
  local3=$(git -C "$clone" rev-parse HEAD)
  fullcount=$(git -C "$clone" rev-list --count "$local3" --not --remotes)
  local line_d="refs/heads/feature $local3 refs/heads/feature $zero"
  run_guard "$line_d" "PUSH_COUNT=$fullcount"
  if [ "$CODE" -eq 0 ] && grep -q "OK, $fullcount commit" <<< "$OUT"; then
    echo "PASS (d) new branch, PUSH_COUNT=$fullcount (commits not already on a remote) allowed"
  else
    echo "FAIL (d) new branch: code=$CODE fullcount=$fullcount"; printf '%s\n' "$OUT" | sed 's/^/    /'; rc=1
  fi

  # (g) a new ref at a commit origin already has -> 0, not the full history.
  git -C "$clone" push -q origin "$local3":main
  git -C "$clone" checkout -q -b already-pushed "$local3"
  local line_g="refs/heads/already-pushed $local3 refs/heads/already-pushed $zero"
  run_guard "$line_g" PUSH_COUNT=0
  if [ "$CODE" -eq 0 ] && grep -q "OK, 0 commit" <<< "$OUT"; then
    echo "PASS (g) new branch at an already-pushed commit, PUSH_COUNT=0 allowed"
  else
    echo "FAIL (g) new branch at already-pushed commit: code=$CODE"; printf '%s\n' "$OUT" | sed 's/^/    /'; rc=1
  fi
  run_guard "$line_g" PUSH_COUNT=1
  if [ "$CODE" -ne 0 ]; then
    echo "PASS (g2) PUSH_COUNT=1 refused for that same line (0 is the true count)"
  else
    echo "FAIL (g2) PUSH_COUNT=1 wrongly allowed for a 0-new-commit new ref"; printf '%s\n' "$OUT" | sed 's/^/    /'; rc=1
  fi

  # (h) a tag on that already-pushed commit — same shape.
  git -C "$clone" tag v0-test "$local3"
  local line_h="refs/tags/v0-test $local3 refs/tags/v0-test $zero"
  run_guard "$line_h" PUSH_COUNT=0
  if [ "$CODE" -eq 0 ] && grep -q "OK, 0 commit" <<< "$OUT"; then
    echo "PASS (h) tag on an already-pushed commit, PUSH_COUNT=0 allowed"
  else
    echo "FAIL (h) tag on already-pushed commit: code=$CODE"; printf '%s\n' "$OUT" | sed 's/^/    /'; rc=1
  fi

  # (i) no ref lines at all (a non-fast-forward push, measured live) -> refused
  # with the real reason, whatever PUSH_COUNT says.
  run_guard "" PUSH_COUNT=1
  if [ "$CODE" -ne 0 ] && grep -q "no refs at all" <<< "$OUT"; then
    echo "PASS (i) empty stdin refused, and names the non-fast-forward cause"
  else
    echo "FAIL (i) empty stdin: code=$CODE"; printf '%s\n' "$OUT" | sed 's/^/    /'; rc=1
  fi
  run_guard "" PUSH_COUNT=0
  if [ "$CODE" -ne 0 ]; then
    echo "PASS (i2) empty stdin refused even with PUSH_COUNT=0"
  else
    echo "FAIL (i2) empty stdin wrongly allowed with PUSH_COUNT=0"; printf '%s\n' "$OUT" | sed 's/^/    /'; rc=1
  fi

  # (e) PUSH_COUNT=abc -> usage refusal
  run_guard "$line_ab" PUSH_COUNT=abc
  if [ "$CODE" -ne 0 ] && grep -qi "usage\|not a non-negative integer" <<< "$OUT"; then
    echo "PASS (e) PUSH_COUNT=abc refused with usage"
  else
    echo "FAIL (e) PUSH_COUNT=abc: code=$CODE"; printf '%s\n' "$OUT" | sed 's/^/    /'; rc=1
  fi

  # (f) deletion line -> zero commits, PUSH_COUNT=0 accepted, and only for it
  local line_del="refs/heads/main $zero refs/heads/main $remote0"
  run_guard "$line_del" PUSH_COUNT=0
  if [ "$CODE" -eq 0 ] && grep -q "OK, 0 commit" <<< "$OUT"; then
    echo "PASS (f) deletion line skipped, PUSH_COUNT=0 allowed"
  else
    echo "FAIL (f) deletion line: code=$CODE"; printf '%s\n' "$OUT" | sed 's/^/    /'; rc=1
  fi
  run_guard "$line_ab" PUSH_COUNT=0
  if [ "$CODE" -ne 0 ]; then
    echo "PASS (f2) PUSH_COUNT=0 refused when the range is not a deletion"
  else
    echo "FAIL (f2) PUSH_COUNT=0 wrongly allowed with 2 real outgoing commits"; printf '%s\n' "$OUT" | sed 's/^/    /'; rc=1
  fi

  return "$rc"
}

case "${1:-}" in
  --selftest) selftest ;;
  "") check ;;
  *) usage; exit 2 ;;
esac
