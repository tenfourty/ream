#!/usr/bin/env bash
# scripts/tests/prepush-hooks-fire.sh — durable regression test for the
# lefthook pre-push skip hole (homelab trap 48), ported here with the guards
# on 22 Sep 2026 (ij8.76).
#
# WHY THIS EXISTS AND NOT JUST `push-count-guard.sh --selftest`: that selftest
# invokes the guard directly as a bash subprocess with fabricated stdin. It
# proves the SCRIPT's logic and nothing about whether lefthook ever calls it.
# The hole was a lefthook DISPATCH decision — it hard-codes every `pre-push`
# command to skip, printing "(skip) no matching push files", whenever
# `git diff --name-only HEAD @{push}` is empty (a deletion-only push, an
# all-`--allow-empty` push, a non-HEAD ref push) — not a bug in any guard.
# lefthook.yml's fix (a `files:` command paired with a `{files}` reference in
# `run:`) works by tripping an EARLIER, unconditional return in lefthook's own
# dispatch, which is a side effect of its internal order, not a documented
# switch. A lefthook upgrade could reorder that and reopen the hole with the
# YAML looking exactly as it does today. Only a REAL `git push` through a REAL
# `lefthook install`, with this repo's actual lefthook.yml and actual guard
# scripts, can show it — so that is what this does, in a throwaway repo it
# builds and destroys.
#
# FAILS CLOSED when lefthook is not on PATH: a skipped test is the silent hole
# again.
#
# Usage: scripts/tests/prepush-hooks-fire.sh
set -euo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." >/dev/null 2>&1 && pwd)

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

command -v lefthook >/dev/null 2>&1 || fail \
  "lefthook not found on PATH — cannot verify the real pre-push dispatch."
command -v git >/dev/null 2>&1 || fail "git not found on PATH"

STAGE=$(mktemp -d)
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

BARE="$STAGE/origin.git"
SEED="$STAGE/seed"
CLONE="$STAGE/work"

# Name the branch explicitly everywhere: with init.defaultBranch unset (CI's
# git, a fresh machine) a bare repo's HEAD points at refs/heads/master, which
# never exists here, and the clone checks out nothing.
git init -q --bare "$BARE"
git -C "$BARE" symbolic-ref HEAD refs/heads/main
git init -q "$SEED"
git -C "$SEED" config user.name prepush-hooks-fire-seed
git -C "$SEED" config user.email prepush-hooks-fire-seed@example.invalid
git -C "$SEED" checkout -q -b main
echo seed > "$SEED/seed.txt"
git -C "$SEED" add seed.txt
git -C "$SEED" commit -q -m "chore: seed"
git -C "$SEED" remote add origin "$BARE"
git -C "$SEED" push -q origin main
rm -rf "$SEED"

git clone -q -b main "$BARE" "$CLONE"
git -C "$CLONE" config user.name prepush-hooks-fire
git -C "$CLONE" config user.email prepush-hooks-fire@example.invalid

# Only the pre-push section of the real lefthook.yml: the rest wires
# pre-commit and commit-msg jobs whose behaviour would break the setup commits
# below. The section is copied verbatim so nobody has to keep a second copy in
# step — that is the whole point of this test.
awk '/^pre-push:/{f=1} f{print}' "$REPO_ROOT/lefthook.yml" > "$CLONE/lefthook.yml"
grep -q '^pre-push:' "$CLONE/lefthook.yml" || fail "no pre-push: section found in $REPO_ROOT/lefthook.yml"

mkdir -p "$CLONE/scripts/guards" "$CLONE/src" "$CLONE/tests"
cp "$REPO_ROOT/scripts/guards/push-count-guard.sh" "$REPO_ROOT/scripts/guards/sec-branch-guard.sh" "$CLONE/scripts/guards/"
chmod +x "$CLONE/scripts/guards/push-count-guard.sh" "$CLONE/scripts/guards/sec-branch-guard.sh"
# The pre-push ruff job lints src and tests; give the throwaway clone a
# minimal, clean pair so the job has something real to pass on.
printf 'x = 1\n' > "$CLONE/src/ok.py"
printf 'def test_ok() -> None:\n    assert True\n' > "$CLONE/tests/test_ok.py"

git -C "$CLONE" add lefthook.yml scripts src tests
git -C "$CLONE" commit -q -m "chore: wire the real pre-push config"
git -C "$CLONE" push -q origin HEAD:main
( cd "$CLONE" && lefthook install ) >/dev/null

rc=0

# Runs a git command in $CLONE with PUSH_COUNT unset, so push-count-guard
# always REFUSES rather than OKs — the assertion only cares whether it ran at
# all, and a refusal is the loudest proof that it did.
assert_fired() {
  local label=$1; shift
  local out
  out=$(cd "$CLONE" && env -u PUSH_COUNT "$@" 2>&1) || true

  if grep -q '(skip)' <<< "$out"; then
    echo "FAIL ($label): lefthook skipped a pre-push command — the hole is back:" >&2
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    rc=1
    return
  fi
  if ! grep -q 'push-count-guard: REFUSED -- PUSH_COUNT is not set' <<< "$out"; then
    echo "FAIL ($label): push-count-guard's own refusal text is absent — it did not run:" >&2
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    rc=1
    return
  fi
  # The ruff job's OWN output, not lefthook's summary line: a job name in the
  # summary is printed for a skipped command too, so it proves nothing.
  # Either shape counts — the clone has no venv, so "SKIP (not installed)" is
  # the honest result on a machine where ruff is not on PATH.
  if ! grep -qE 'ruff: SKIP \(not installed\)|All checks passed' <<< "$out"; then
    echo "FAIL ($label): the ruff pre-push command produced no output of its own — it did not run:" >&2
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    rc=1
    return
  fi
  echo "PASS ($label): both pre-push commands ran (no skip), both markers present"
}

# Case 1: an all-`--allow-empty`-commits push — the exact shape that sailed
# through with no PUSH_COUNT ever asked, live, before the fix.
git -C "$CLONE" commit -q --allow-empty -m "chore: empty case"
assert_fired "empty-commit-only push" git push origin HEAD:main

# Case 2: a deletion push. Setting up the branch to delete is not the case
# under test; case 1 refused, so its commit is still local-only and the count
# is computed, never hand-carried.
setup_count=$(cd "$CLONE" && git rev-list --count HEAD --not --remotes)
( cd "$CLONE" && PUSH_COUNT="$setup_count" git push -q origin HEAD:throwaway )
assert_fired "deletion push" git push origin :throwaway

# Case 3: an ordinary push with the right count must still SUCCEED — a test
# that only ever proves refusals would pass just as happily against a guard
# that refuses everything.
# Counted against the DESTINATION ref, not `--not --remotes`: case 2 pushed
# this same HEAD to a throwaway branch, so "not on any remote" is 0 while the
# push to main still carries a commit. Getting this wrong made case 3 fail on
# its first run, which is the shape the guard exists to catch.
( cd "$CLONE" && git fetch -q origin )
count=$(cd "$CLONE" && git rev-list --count refs/remotes/origin/main..HEAD)
if ( cd "$CLONE" && PUSH_COUNT="$count" git push origin HEAD:main ) >/dev/null 2>&1; then
  echo "PASS (ordinary push with the announced count): allowed"
else
  echo "FAIL (ordinary push with the announced count): refused, PUSH_COUNT=$count" >&2
  rc=1
fi

# Cases 4-6: sec-branch-guard, through the same real dispatch. "origin" here is a local bare repo,
# which is not runcove-sec/artifacts-sec, so a sec/* ref must be refused -- with the RIGHT push count
# announced, so that it is sec-branch and not push-count that stops the push.
( cd "$CLONE" && git fetch -q origin )
HEAD_SHA=$(cd "$CLONE" && git rev-parse HEAD)
count=$(cd "$CLONE" && git rev-list --count refs/remotes/origin/main..HEAD)

assert_sec_refused() {
  local label=$1 ref=$2; shift 2
  local out
  out=$(cd "$CLONE" && PUSH_COUNT="${PC:-0}" "$@" 2>&1) && {
    echo "FAIL ($label): the push succeeded -- a sec ref reached a remote that is not artifacts-sec" >&2
    rc=1
    return
  }
  if grep -q '(skip)' <<< "$out"; then
    echo "FAIL ($label): lefthook skipped a pre-push command:" >&2
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    rc=1
    return
  fi
  if ! grep -q 'sec-branch-guard: REFUSED' <<< "$out"; then
    echo "FAIL ($label): sec-branch-guard's refusal is absent -- it did not run, or it let the push through:" >&2
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    rc=1
    return
  fi
  if git -C "$BARE" rev-parse -q --verify "$ref" >/dev/null; then
    echo "FAIL ($label): $ref exists on the remote after a refused push" >&2
    rc=1
    return
  fi
  echo "PASS ($label): refused by sec-branch-guard, nothing reached the remote"
}

[ "$count" -eq 0 ] || { echo "FAIL: setup, expected the clone level with origin/main, got $count ahead" >&2; exit 1; }
# Case 4: a sec branch, named either way round.
assert_sec_refused "sec branch push" refs/heads/sec/x git push origin HEAD:refs/heads/sec/x
git -C "$CLONE" branch sec/y
assert_sec_refused "sec branch push by its short name" refs/heads/sec/y git push origin sec/y
# Case 5: a tag under sec/.
git -C "$CLONE" tag sec/v1
assert_sec_refused "sec tag push" refs/tags/sec/v1 git push origin sec/v1
# The two refs above stood on an already-published commit; they are deleted so that they do not
# stand in the way of the ordinary pushes below (a commit-less sec branch cannot be told from a fix).
git -C "$CLONE" branch -q -D sec/y
git -C "$CLONE" tag -d sec/v1 >/dev/null
# Case 6: deleting a sec ref that is already on the remote is allowed (the clean-up of a leak),
# and says so loudly. The ref is planted straight into the bare repo: the guard under test
# would not let this clone push it.
git -C "$BARE" update-ref refs/heads/sec/leaked "$HEAD_SHA"
out=$(cd "$CLONE" && PUSH_COUNT=0 git push origin :sec/leaked 2>&1) && del_ok=1 || del_ok=0
if [ "$del_ok" -eq 1 ] && grep -q 'sec-branch-guard: NOTE' <<< "$out" \
   && ! git -C "$BARE" rev-parse -q --verify refs/heads/sec/leaked >/dev/null; then
  echo "PASS (sec ref deletion): allowed, with the note, and the ref is gone"
else
  echo "FAIL (sec ref deletion): ok=$del_ok" >&2
  printf '%s\n' "$out" | sed 's/^/    /' >&2
  rc=1
fi

# Case 7: a remote url full of shell characters. lefthook pastes {1}/{2} into its shell text
# unquoted (measured with 2.2.2), which would split such a url and run its `;touch` part; the
# hook hands them over in a quoted here-document instead, so the url is data. The guard must
# still decide on the real url, show it intact, and nothing the url spells may execute.
HOSTILE="$STAGE/h o&s;touch $STAGE/PWN1;\$(touch $STAGE/PWN2)\`touch $STAGE/PWN3\`.git"
git init -q --bare "$HOSTILE"
git -C "$CLONE" remote add hostile "$HOSTILE"
out=$(cd "$CLONE" && PUSH_COUNT=0 git push hostile HEAD:refs/heads/sec/x 2>&1) && sec_ok=1 || sec_ok=0
if [ "$sec_ok" -eq 0 ] && grep -q 'sec-branch-guard: REFUSED' <<< "$out" \
   && grep -qF "h o&s;touch" <<< "$out" \
   && ! git -C "$HOSTILE" rev-parse -q --verify refs/heads/sec/x >/dev/null; then
  echo "PASS (url with shell characters): sec ref refused, the url shown intact"
else
  echo "FAIL (url with shell characters): sec push ok=$sec_ok" >&2
  printf '%s\n' "$out" | sed 's/^/    /' >&2
  rc=1
fi
( cd "$CLONE" && PUSH_COUNT=0 git push -q hostile HEAD:refs/heads/main ) >/dev/null 2>&1 || {
  echo "FAIL (url with shell characters): an ordinary push to that remote was refused" >&2; rc=1; }
if [ -e "$STAGE/PWN1" ] || [ -e "$STAGE/PWN2" ] || [ -e "$STAGE/PWN3" ]; then
  echo "FAIL (url with shell characters): the url was EXECUTED by the hook" >&2
  rc=1
else
  echo "PASS (url with shell characters): nothing the url spells ran"
fi

# Case 8: the commits of a sec branch under another name -- BY CONTENT, the strings git really
# hands the hook (HEAD~0, @{-1}, a raw id, a merge). The right count is announced each time.
git -C "$CLONE" switch -q -c sec/real
git -C "$CLONE" commit -q --allow-empty -m "chore: a commit on a sec branch"
SEC_SHA=$(git -C "$CLONE" rev-parse HEAD)
PC=1 assert_sec_refused "HEAD~0 from a sec branch" refs/heads/leak1 git push origin HEAD~0:refs/heads/leak1
git -C "$CLONE" switch -q main
PC=1 assert_sec_refused "@{-1} just after leaving it" refs/heads/leak2 git push origin '@{-1}:refs/heads/leak2'
PC=1 assert_sec_refused "a raw commit id" refs/heads/leak3 git push origin "$SEC_SHA:refs/heads/leak3"
git -C "$CLONE" merge -q --no-ff -m "chore: merge" sec/real
PC=2 assert_sec_refused "main with the sec branch merged in" refs/heads/leak4 git push origin main:refs/heads/leak4
# The release: once the local sec refs are deleted, the merged main goes (the deliberate act the
# runbook asks for).
git -C "$CLONE" branch -q -D sec/real
if ( cd "$CLONE" && PUSH_COUNT=2 git push origin main:refs/heads/released ) >/dev/null 2>&1 \
   && git -C "$BARE" rev-parse -q --verify refs/heads/released >/dev/null; then
  echo "PASS (release): after the local sec refs are deleted the merged main is pushed"
else
  echo "FAIL (release): the merged main was refused although no local sec ref is left" >&2
  rc=1
fi

exit "$rc"
