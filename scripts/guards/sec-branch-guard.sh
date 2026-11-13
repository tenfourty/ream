#!/usr/bin/env bash
# scripts/guards/sec-branch-guard.sh — refuse a push of a `sec/*` ref to any remote that is not
# the private repo runcove-sec/artifacts-sec.
#
# WHY. An embargoed security fix is written on `sec/<topic>` branches in the PRIVATE repo
# runcove-sec/artifacts-sec (docs/agent/runbooks/embargoed-fix.md), because runcove/artifacts is public
# and mirrored: whatever is pushed there is readable by anyone, at once, and cannot be recalled.
# The runbook says to work in a clone whose only remote is artifacts-sec, but a rule in prose is
# the rule that fails on the day someone `git push origin sec/x` from the everyday clone, whose
# `origin` is the public repo. This is the mechanical back-up: it needs nothing but the pusher to
# be in a clone that ran `lefthook install`, like every other guard here.
#
# WHAT: a lefthook `pre-push` command. git runs the hook as `pre-push <remote name> <remote url>`
# and writes one line per ref being pushed on stdin:
#   <local ref> SP <local sha> SP <remote ref> SP <remote sha>
# (the url in $2 is the one git is about to push to: after any `insteadOf` / `pushInsteadOf`
# rewrite, and once per URL when a remote has several push URLs).
#
# A line is judged two ways, and refused to a URL that is not exactly runcove-sec/artifacts-sec on
# the forge (git.tenfourty.site, or for ssh forms only its ssh endpoint git-ssh.tenfourty.site)
# (exit 1, nothing is sent) when either says so:
#
# 1. BY NAME. Its local ref or its remote ref is, in any letter case, one of
#      sec/*   refs/heads/sec/*   refs/tags/sec/*   refs/remotes/<remote>/sec/*
#    (so `git push origin sec/x`, `HEAD:refs/heads/sec/x`, `sec/x:main`, `Sec/x`, a tag `sec/v1`,
#    `--mirror`, `--tags` and a refspec glob are all seen). A local ref of `HEAD` counts as the
#    branch HEAD points at.
#
# 2. BY CONTENT. A name is only what the pusher typed: `HEAD~0:main`, `@{0}:main` and `@{-1}:main`
#    (git hands the hook those strings as the local ref), a raw commit id, or a `main` with a sec
#    branch merged into it carry the fix under no sec name. So for every line whose local sha is not
#    zero, the sha is refused when it EQUALS OR DESCENDS FROM THE TIP of any LOCAL branch
#    (refs/heads/) or tag (refs/tags/) named sec/* in any letter case (`git merge-base
#    --is-ancestor <tip> <sha>`; an annotated tag is peeled to its commit). One exemption, only
#    for what is already there: a tip the remote ref's current commit already contains sends
#    nothing new, so a sec branch left sitting on an already-published `main` commit does not stop
#    an ordinary push. If git cannot answer (an error, an object that is no commit, a repository it
#    cannot read) the push is REFUSED: this fails closed, never open.
#
# No sec ref in the push and none held locally: silent, exit 0, nothing is looked at beyond the
# refs of the repository. To the private repo itself: exit 0 at once.
#
# THE URL. Only these forms pass, each with an optional `.git` and an optional trailing `/`:
#   https://[user[:pw]@]git.tenfourty.site/runcove-sec/artifacts-sec
#   ssh://[user@]git.tenfourty.site[:22]/runcove-sec/artifacts-sec
#   ssh://[user@]git-ssh.tenfourty.site[:22]/runcove-sec/artifacts-sec
#   [user@]git.tenfourty.site:runcove-sec/artifacts-sec            (the scp-like ssh form)
#   [user@]git-ssh.tenfourty.site:runcove-sec/artifacts-sec        (the scp-like ssh form)
# git-ssh.tenfourty.site is the forge's real ssh endpoint, so it is accepted for the two ssh forms
# ONLY: https on git-ssh.tenfourty.site fails (it is not the forge's web front door). No port on
# https (443 is implicit; a remote here that spells one is not the forge's front door),
# and on ssh only 22, the forge's ssh port and the default. Exact and lower-case: `runcove-sec/artifacts-sec-evil`, `runcove/artifacts` (the public repo),
# `runcove/artifacts-sec` (the private repo's OLD name, from before it moved to the runcove-sec org:
# the forge may redirect it, and a redirect is not the repository), another owner, another
# host (the GitHub mirror, `git.tenfourty.site.evil.example`, `git.tenfourty.site@evil.example`,
# and the lookalikes of the ssh host: `git-ssh.tenfourty.site.evil.example`,
# `evilgit-ssh.tenfourty.site`, `git-ssh-tenfourty.site`),
# plain http, git://, file://, a local path, an upper-case spelling, an empty or missing url all
# fail. A false refusal costs one retyped URL; a false pass costs a disclosed vulnerability, so
# anything this does not recognise is a refusal. A refusal prints the url with any user:password
# part replaced by `***`, never as given.
#
# DELETIONS ARE ALLOWED (a line whose local sha is all zeros: `git push origin :sec/x`,
# `--delete sec/x`). Chosen deliberately: a delete sends no object, only the name of a ref the
# remote already has, so it cannot put anything new on the public repo; and the only reason to
# delete a sec ref from a public remote is that one got there, where deleting it is the first step
# of the clean-up and refusing would stand in the way of it. It prints a loud note, because
# deleting does not undo anything: the commits stay fetchable by id, and forks, mirrors and the
# forge's activity feed may already hold the name and the content. Tell security.
#
# THE MAIN CONTROL. The separate clone with only the artifacts-sec remote
# (docs/agent/runbooks/embargoed-fix.md) is the main control; this guard is a back-up for the day
# someone is in the wrong clone. The release (the runbook's last step, a push of the private
# `main` to the public repo) is refused while a local sec/* branch or tag is still there: after
# security's yes, deleting those local refs is the deliberate act that lets it go. A sec branch
# with no commit of its own sitting on a `main` commit that has not been published yet stops the
# push of that `main` (it cannot be told from a fix): give the
# branch its first commit, or delete it.
#
# Pure bash and git; never skips. The wiring in lefthook.yml follows push-count-guard.sh's
# (`files:` + `{files}`, homelab trap 48), and scripts/tests/prepush-hooks-fire.sh drives it through
# a real lefthook and a real `git push`.
#
# HOW THE URL ARRIVES. lefthook pastes `{1}` and `{2}` into the command's shell text UNQUOTED
# (measured with lefthook 2.2.2: a remote path containing a space and `;touch X` split into two
# words and ran `touch X`), so a url with `&`, `;`, `$`, a quote or a space (a password in a url
# often has one) would be split, backgrounded or executed, and a `;true` suffix could turn a refusal
# into a pass. lefthook.yml therefore hands both over as the two lines of a QUOTED here-document on
# fd 3, which the shell never expands, and runs `--args-on-fd3`:
#   scripts/guards/sec-branch-guard.sh --args-on-fd3 3<<'ARGS'
#   {1}
#   {2}
#   ARGS
#
# Usage: sec-branch-guard.sh <remote name> <remote url>        < pre-push lines   (by hand, selftest)
#        sec-branch-guard.sh --args-on-fd3 3<<<$'name\nurl'    < pre-push lines   (lefthook)
# Selftest: scripts/guards/sec-branch-guard.sh --selftest
set -euo pipefail

SELF="sec-branch-guard"
ZERO_RE='^0+$'
SEC_HOST='git\.tenfourty\.site'
# The forge's ssh endpoint: accepted for the two ssh forms only, never for https.
SSH_HOSTS='(git|git-ssh)\.tenfourty\.site'
SEC_PATH='runcove-sec/artifacts-sec'
# userinfo has no '@' and no '/', so `host@evil` can never read as userinfo of the real host.
URL_RES=(
  "^https://([^/@]*@)?${SEC_HOST}/${SEC_PATH}(\.git)?/?\$"
  "^ssh://([^/@]*@)?${SSH_HOSTS}(:22)?/${SEC_PATH}(\.git)?/?\$"
  "^([^/@:]+@)?${SSH_HOSTS}:${SEC_PATH}(\.git)?/?\$"
)
SEC_REF_RE='^((refs/(heads|tags)/)|(refs/remotes/[^/]+/))?sec/.'

is_zero() { [[ "$1" =~ $ZERO_RE ]]; }

# The branch HEAD points at, as a full ref; nothing when detached or outside a repository.
head_ref() { git symbolic-ref -q HEAD 2>/dev/null || true; }

# 0 when <ref> is a sec/* name, in any letter case (nocasematch also folds the regex).
is_sec_name() {
  local r=1
  shopt -s nocasematch
  if [[ "$1" =~ $SEC_REF_RE ]]; then r=0; fi
  shopt -u nocasematch
  return "$r"
}

is_sec_ref() { # <ref name as git gave it>
  local ref="$1"
  if [ "$ref" = HEAD ]; then ref=$(head_ref); fi
  [ -n "$ref" ] && is_sec_name "$ref"
}

# The tips of this repository's local sec/* branches and tags, as SEC_TIPS entries "<refname> <commit>".
# Returns 1 on any git error or a sec ref that is no commit: the caller fails closed.
SEC_TIPS=()
SEC_TIPS_STATE=unloaded
load_sec_tips() {
  [ "$SEC_TIPS_STATE" = unloaded ] || { [ "$SEC_TIPS_STATE" = ok ]; return; }
  local out ref sha
  SEC_TIPS_STATE=error
  out=$(git for-each-ref --format='%(refname)' refs/heads refs/tags 2>/dev/null) || return 1
  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    is_sec_name "$ref" || continue
    sha=$(git rev-parse -q --verify "${ref}^{commit}" 2>/dev/null) || return 1
    SEC_TIPS+=("$ref $sha")
  done <<< "$out"
  SEC_TIPS_STATE=ok
}

# Prints the sec refs whose commits <local sha> carries to a remote ref now at <remote sha> (zeros
# when the ref is new there); returns 0 when it carries none, 1 when it carries some, 2 on an error.
carries_sec() {
  local sha="$1" remote_sha="$2" commit entry name tip rc found=1
  commit=$(git rev-parse -q --verify "${sha}^{commit}" 2>/dev/null) || return 2
  for entry in "${SEC_TIPS[@]}"; do
    name=${entry% *}; tip=${entry#* }
    if ! is_zero "$remote_sha"; then
      # Already contained in what the remote has: nothing new is sent. An error here (the remote's
      # commit is not known locally) just means "not known to be there".
      rc=0; git merge-base --is-ancestor "$tip" "$remote_sha" 2>/dev/null || rc=$?
      [ "$rc" -ne 0 ] || continue
    fi
    rc=0; git merge-base --is-ancestor "$tip" "$commit" 2>/dev/null || rc=$?
    case "$rc" in
      0) echo "$name"; found=0 ;;
      1) ;;
      *) return 2 ;;
    esac
  done
  [ "$found" -eq 1 ] && return 0
  return 1
}

# 0 when the url is exactly the artifacts-sec repository on the forge.
is_sec_url() {
  local url="$1" re
  # Whitespace or a control character is never part of a url we recognise.
  case "$url" in *[[:space:]]*|*[[:cntrl:]]*) return 1 ;; esac
  for re in "${URL_RES[@]}"; do
    if [[ "$url" =~ $re ]]; then return 0; fi
  done
  return 1
}

# The url for a message: the user:password part of a scheme://... url is never printed.
redact() {
  printf '%s' "$1" | tr -d '[:cntrl:]' | sed -E 's#^([A-Za-z][A-Za-z0-9+.-]*://)[^/]*@#\1***@#'
}

check() {
  local remote="${1:-}" url="${2:-}"
  local local_ref local_sha remote_ref remote_sha names rc
  local -a refused=() deleted=()
  # The private repository itself: nothing here can be refused.
  if is_sec_url "$url"; then return 0; fi

  # `|| [ -n ... ]`: a last line without its newline is still a line.
  while read -r local_ref local_sha remote_ref remote_sha || [ -n "${local_ref:-}" ]; do
    [ -z "${local_ref:-}" ] && continue
    remote_sha="${remote_sha:-}"
    if is_sec_ref "${remote_ref:-}"; then
      # A sec ref being removed from the remote: allowed, with a note (see the header).
      if is_zero "${local_sha:-x}"; then deleted+=("${remote_ref}"); continue; fi
      refused+=("${local_ref} -> ${remote_ref}  [a sec ref, by name]")
      continue
    fi
    is_zero "${local_sha:-x}" && continue
    if is_sec_ref "$local_ref"; then
      refused+=("${local_ref} -> ${remote_ref}  [a sec ref, by name]")
      continue
    fi
    # By content (see the header): the commits of a local sec branch or tag, under any other name.
    if ! load_sec_tips; then
      refused+=("${local_ref} -> ${remote_ref}  [git could not say whether it carries a sec ref: refused]")
      continue
    fi
    [ "${#SEC_TIPS[@]}" -gt 0 ] || continue
    rc=0; names=$(carries_sec "$local_sha" "$remote_sha") || rc=$?
    case "$rc" in
      0) ;;
      1) refused+=("${local_ref} -> ${remote_ref}  [carries the commits of $(printf '%s' "$names" | tr '\n' ' ' | sed 's/ $//')]") ;;
      *) refused+=("${local_ref} -> ${remote_ref}  [git could not say whether it carries a sec ref: refused]") ;;
    esac
  done

  if [ "${#deleted[@]}" -gt 0 ]; then
    {
      echo "$SELF: NOTE -- this push deletes ${deleted[*]} on $(redact "$url") (remote '${remote}')."
      echo "It is allowed so that a leaked sec ref can be cleaned up, but deleting does not undo the"
      echo "leak: the commits stay fetchable by id and may already be copied. If this ref was ever"
      echo "on that remote, tell security now (docs/agent/runbooks/embargoed-fix.md)."
    } >&2
  fi

  [ "${#refused[@]}" -gt 0 ] || return 0

  {
    echo "$SELF: REFUSED -- sec/* work may only be pushed to runcove-sec/artifacts-sec (private)."
    echo "This push goes to remote '${remote}' = $(redact "$url"), which is not that repository:"
    printf '  %s\n' "${refused[@]}"
    echo "A sec ref is refused by name, and any push whose commits are those of a local sec/* branch"
    echo "or tag (HEAD~0, @{-1}, a raw id, a merge of it) is refused whatever it is called."
    echo "runcove/artifacts is public and mirrored; a pushed ref cannot be recalled. Do embargoed"
    echo "work in the separate clone whose only remote is artifacts-sec (docs/agent/runbooks/"
    echo "embargoed-fix.md). Nothing was sent. There is no override."
  } >&2
  return 1
}

usage() {
  echo "usage: $0 <remote name> <remote url>   (reads git's pre-push lines on stdin)" >&2
  echo "       $0 --args-on-fd3 3<<<\$'name\\nurl'   (the same, name and url as two lines on fd 3: lefthook)" >&2
  echo "       $0 --selftest" >&2
}

# ---------------------------------------------------------------- selftest

# run_guard <url> <stdin text> [<cwd>]: the guard as a fresh subprocess, the way lefthook runs it.
# Sets OUT and CODE.
run_guard() {
  local url=$1 input=$2 cwd=${3:-$work}
  set +e
  OUT=$(cd "$cwd" && bash "$self" origin "$url" < <(printf '%s' "$input") 2>&1)
  CODE=$?
  set -e
}

selftest() {
  local stage self work rc=0
  stage=$(mktemp -d)
  self=$(cd -- "$(dirname -- "$0")" >/dev/null 2>&1 && pwd)/$(basename -- "$0")
  trap 'rm -rf "$stage"' RETURN
  # The guard asks git about the repository it runs in, so every case runs in a real one. A GIT_DIR
  # inherited from an outer hook must not redirect any of it.
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_COMMON_DIR
  work="$stage/plain"
  git init -q "$work"
  local sha=1111111111111111111111111111111111111111
  local sha2=2222222222222222222222222222222222222222
  local zero=0000000000000000000000000000000000000000
  local zero256=0000000000000000000000000000000000000000000000000000000000000000
  local pub=https://git.tenfourty.site/runcove/artifacts.git
  local seclne="refs/heads/sec/x $sha refs/heads/sec/x $zero"

  pass() { echo "PASS $1"; }
  fail() { echo "FAIL $1"; printf '%s\n' "$OUT" | sed 's/^/    /'; rc=1; }
  # expect_refused <label> <url> <stdin text> [<cwd>]
  expect_refused() {
    run_guard "$2" "$3" "${4:-$work}"
    if [ "$CODE" -eq 1 ] && grep -q "$SELF: REFUSED" <<< "$OUT"; then pass "$1"; else fail "$1 (code=$CODE)"; fi
  }
  # expect_carries <label> <url> <stdin text> [<cwd>]: refused BY CONTENT (names the sec ref found)
  expect_carries() {
    run_guard "$2" "$3" "${4:-$work}"
    if [ "$CODE" -eq 1 ] && grep -q "$SELF: REFUSED" <<< "$OUT" \
       && grep -q "carries the commits of refs/" <<< "$OUT"; then pass "$1"; else fail "$1 (code=$CODE)"; fi
  }
  # expect_ok <label> <url> <stdin text> [<cwd>]: exit 0 and no refusal
  expect_ok() {
    run_guard "$2" "$3" "${4:-$work}"
    if [ "$CODE" -eq 0 ] && ! grep -q "REFUSED" <<< "$OUT"; then pass "$1"; else fail "$1 (code=$CODE)"; fi
  }

  # --- (a) the url: exactly artifacts-sec passes, in every form git can hand over
  local u
  for u in \
    https://git.tenfourty.site/runcove-sec/artifacts-sec \
    https://git.tenfourty.site/runcove-sec/artifacts-sec.git \
    https://git.tenfourty.site/runcove-sec/artifacts-sec/ \
    https://git.tenfourty.site/runcove-sec/artifacts-sec.git/ \
    ssh://git@git.tenfourty.site/runcove-sec/artifacts-sec.git \
    ssh://git@git.tenfourty.site:22/runcove-sec/artifacts-sec.git \
    ssh://git.tenfourty.site:22/runcove-sec/artifacts-sec \
    ssh://git.tenfourty.site/runcove-sec/artifacts-sec.git \
    git@git.tenfourty.site:runcove-sec/artifacts-sec.git \
    git@git.tenfourty.site:runcove-sec/artifacts-sec \
    git.tenfourty.site:runcove-sec/artifacts-sec.git \
    ssh://git@git-ssh.tenfourty.site/runcove-sec/artifacts-sec.git \
    ssh://git@git-ssh.tenfourty.site:22/runcove-sec/artifacts-sec.git \
    ssh://git@git-ssh.tenfourty.site:22/runcove-sec/artifacts-sec/ \
    ssh://git-ssh.tenfourty.site:22/runcove-sec/artifacts-sec \
    ssh://git-ssh.tenfourty.site/runcove-sec/artifacts-sec.git \
    git@git-ssh.tenfourty.site:runcove-sec/artifacts-sec.git \
    git@git-ssh.tenfourty.site:runcove-sec/artifacts-sec \
    git-ssh.tenfourty.site:runcove-sec/artifacts-sec.git \
    "https://oauth2:${stage##*/}@git.tenfourty.site/runcove-sec/artifacts-sec.git"; do
    expect_ok "(a) sec ref to ${u%%${stage##*/}*}... allowed" "$u" "$seclne"$'\n'
  done

  # --- (b) the url: anything else is refused (the same literal line the real hook passes).
  # The first group is the repository's OLD name, runcove/artifacts-sec, before it moved to the
  # runcove-sec org: the forge may still redirect it, and a redirect must not count as the repo.
  # The ports: none on https (443 is implicit), only 22 on ssh.
  for u in \
    https://git.tenfourty.site:3000/runcove-sec/artifacts-sec.git \
    https://git.tenfourty.site:443/runcove-sec/artifacts-sec.git \
    https://git.tenfourty.site:/runcove-sec/artifacts-sec.git \
    ssh://git@git.tenfourty.site:2222/runcove-sec/artifacts-sec \
    ssh://git@git.tenfourty.site:222/runcove-sec/artifacts-sec.git \
    ssh://git@git.tenfourty.site:/runcove-sec/artifacts-sec.git \
    https://git.tenfourty.site/runcove/artifacts-sec \
    https://git.tenfourty.site/runcove/artifacts-sec.git \
    https://git.tenfourty.site/runcove/artifacts-sec.git/ \
    ssh://git@git.tenfourty.site/runcove/artifacts-sec.git \
    git@git.tenfourty.site:runcove/artifacts-sec.git \
    git@git.tenfourty.site:runcove/artifacts-sec \
    https://git-ssh.tenfourty.site/runcove-sec/artifacts-sec.git \
    https://git@git-ssh.tenfourty.site/runcove-sec/artifacts-sec \
    https://git-ssh.tenfourty.site:443/runcove-sec/artifacts-sec.git \
    http://git-ssh.tenfourty.site/runcove-sec/artifacts-sec.git \
    ssh://git@git-ssh.tenfourty.site:2222/runcove-sec/artifacts-sec.git \
    ssh://git@git-ssh.tenfourty.site:222/runcove-sec/artifacts-sec \
    ssh://git@git-ssh.tenfourty.site:/runcove-sec/artifacts-sec.git \
    ssh://git@git-ssh.tenfourty.site:3000/runcove-sec/artifacts-sec.git \
    ssh://git@git-ssh.tenfourty.site/runcove/artifacts-sec.git \
    git@git-ssh.tenfourty.site:runcove/artifacts-sec.git \
    git@git-ssh.tenfourty.site:runcove/artifacts-sec \
    ssh://git@git-ssh.tenfourty.site/runcove/artifacts.git \
    git@git-ssh.tenfourty.site:runcove/artifacts.git \
    git@git-ssh.tenfourty.site:runcove-sec/artifacts-sec-evil.git \
    ssh://git@git-ssh.tenfourty.site/runcove-sec/artifacts-sec.git.git \
    git@git-ssh.tenfourty.site:/runcove-sec/artifacts-sec.git \
    git@git-ssh.tenfourty.site:2222:runcove-sec/artifacts-sec.git \
    ssh://git@git-ssh.tenfourty.site.evil.example/runcove-sec/artifacts-sec.git \
    git@git-ssh.tenfourty.site.evil.example:runcove-sec/artifacts-sec.git \
    git-ssh.tenfourty.site.evil.example:runcove-sec/artifacts-sec.git \
    ssh://git@evilgit-ssh.tenfourty.site/runcove-sec/artifacts-sec.git \
    ssh://evilgit-ssh.tenfourty.site/runcove-sec/artifacts-sec.git \
    git@evilgit-ssh.tenfourty.site:runcove-sec/artifacts-sec.git \
    evilgit-ssh.tenfourty.site:runcove-sec/artifacts-sec.git \
    ssh://git@git-ssh-tenfourty.site/runcove-sec/artifacts-sec.git \
    git@git-ssh-tenfourty.site:runcove-sec/artifacts-sec.git \
    git-ssh-tenfourty.site:runcove-sec/artifacts-sec.git \
    ssh://git@gitxssh.tenfourty.site/runcove-sec/artifacts-sec.git \
    git@gitxssh.tenfourty.site:runcove-sec/artifacts-sec.git \
    ssh://git@sub.git-ssh.tenfourty.site/runcove-sec/artifacts-sec.git \
    git@sub.git-ssh.tenfourty.site:runcove-sec/artifacts-sec.git \
    ssh://git@git-ssh.tenfourty.site@evil.example/runcove-sec/artifacts-sec.git \
    ssh://git-ssh.tenfourty.site@evil.example/runcove-sec/artifacts-sec.git \
    ssh://Git-SSH.Tenfourty.Site/runcove-sec/artifacts-sec.git \
    Git@Git-Ssh.Tenfourty.Site:Runcove-Sec/Artifacts-Sec.git \
    ssh://git@git-ssh.tenfourty.site/ssh://git-ssh.tenfourty.site/runcove-sec/artifacts-sec.git \
    "$pub" \
    https://git.tenfourty.site/runcove/artifacts \
    https://git.tenfourty.site/runcove-sec/artifacts-sec-evil \
    https://git.tenfourty.site/runcove-sec/artifacts-sec-evil.git \
    https://git.tenfourty.site/runcove-sec/artifacts-sec2.git \
    https://git.tenfourty.site/runcove-sec/artifacts-sec.git.git \
    https://git.tenfourty.site/runcove-sec/xartifacts-sec.git \
    https://git.tenfourty.site/runcove-sec/artifacts \
    https://git.tenfourty.site/runcove-sec/artifacts-core.git \
    https://git.tenfourty.site/runcove-sec-evil/artifacts-sec.git \
    https://git.tenfourty.site/xruncove-sec/artifacts-sec.git \
    https://git.tenfourty.site/other/artifacts-sec.git \
    https://git.tenfourty.site/runcove-sec/artifacts-sec/extra \
    https://git.tenfourty.site/runcove-sec/artifacts-sec/../artifacts \
    https://git.tenfourty.site/runcove-sec/artifacts-sec.git?x=1 \
    https://git.tenfourty.site/runcove-evil/artifacts-sec.git \
    https://github.com/runcove-sec/artifacts-sec.git \
    https://git.tenfourty.site.evil.example/runcove-sec/artifacts-sec.git \
    https://evil.example/git.tenfourty.site/runcove-sec/artifacts-sec.git \
    https://git.tenfourty.site@evil.example/runcove-sec/artifacts-sec.git \
    https://a@b@git.tenfourty.site/runcove-sec/artifacts-sec.git \
    https://Git.Tenfourty.Site/runcove-sec/artifacts-sec.git \
    https://git.tenfourty.site/Runcove-Sec/Artifacts-Sec.git \
    http://git.tenfourty.site/runcove-sec/artifacts-sec.git \
    git://git.tenfourty.site/runcove-sec/artifacts-sec.git \
    file:///srv/runcove-sec/artifacts-sec.git \
    /srv/runcove-sec/artifacts-sec.git \
    ../artifacts-sec \
    git@evil.example:runcove-sec/artifacts-sec.git \
    git@git.tenfourty.site:/runcove-sec/artifacts-sec.git \
    git@git.tenfourty.site:runcove/artifacts.git \
    git@git.tenfourty.site:runcove-sec/artifacts-sec-evil.git \
    ssh://git@evil.example/runcove-sec/artifacts-sec.git \
    "https://git.tenfourty.site/runcove-sec/artifacts-sec.git " \
    $'https://git.tenfourty.site/runcove-sec/artifacts-sec.git\n' \
    ""; do
    expect_refused "(b) sec ref to [${u//$'\n'/\\n}] refused" "$u" "$seclne"$'\n'
  done
  # No url argument at all (the hook always passes one; a missing one is still a refusal).
  set +e
  OUT=$(cd "$work" && bash "$self" <<<"$seclne" 2>&1); CODE=$?
  set -e
  if [ "$CODE" -eq 1 ] && grep -q "REFUSED" <<< "$OUT"; then pass "(b) no arguments at all refused"; else fail "(b) no arguments at all (code=$CODE)"; fi

  # --- (a2) the lefthook form: name and url as two lines on fd 3, however odd the url is
  local hostile='https://user:p&ss;$(touch X)`touch Y`"q'"'"'x@evil.example/runcove/artifacts.git'
  set +e
  OUT=$(cd "$work" && bash "$self" <<<"$seclne" --args-on-fd3 3<<<"origin"$'\n'"$hostile" 2>&1); CODE=$?
  set -e
  if [ "$CODE" -eq 1 ] && grep -q "REFUSED" <<< "$OUT" && grep -qF 'https://***@evil.example/runcove/artifacts.git' <<< "$OUT" \
     && ! grep -q 'p&ss' <<< "$OUT"; then
    pass "(a2) fd 3: a url full of shell characters is read as data, refused, redacted"
  else fail "(a2) fd 3 hostile url (code=$CODE)"; fi
  set +e
  OUT=$(cd "$work" && bash "$self" <<<"$seclne" --args-on-fd3 3<<<"origin"$'\n'"git@git.tenfourty.site:runcove-sec/artifacts-sec.git" 2>&1); CODE=$?
  set -e
  if [ "$CODE" -eq 0 ] && [ -z "$OUT" ]; then pass "(a2) fd 3: the artifacts-sec url passes"; else fail "(a2) fd 3 sec url (code=$CODE)"; fi
  set +e
  OUT=$(cd "$work" && bash "$self" <<<"$seclne" --args-on-fd3 3<<<"origin"$'\n'"git@git-ssh.tenfourty.site:runcove-sec/artifacts-sec.git" 2>&1); CODE=$?
  set -e
  if [ "$CODE" -eq 0 ] && [ -z "$OUT" ]; then pass "(a2) fd 3: the artifacts-sec url on the ssh host passes"; else fail "(a2) fd 3 sec url on the ssh host (code=$CODE)"; fi
  set +e
  OUT=$(cd "$work" && bash "$self" <<<"$seclne" --args-on-fd3 3<<<"origin"$'\n'"https://git-ssh.tenfourty.site/runcove-sec/artifacts-sec.git" 2>&1); CODE=$?
  set -e
  if [ "$CODE" -eq 1 ] && grep -q "REFUSED" <<< "$OUT"; then pass "(a2) fd 3: https on the ssh host is refused"; else fail "(a2) fd 3 https on the ssh host (code=$CODE)"; fi
  set +e
  OUT=$(cd "$work" && bash "$self" <<<"$seclne" --args-on-fd3 2>&1); CODE=$?
  set -e
  if [ "$CODE" -eq 1 ] && grep -q "REFUSED" <<< "$OUT"; then pass "(a2) fd 3 absent: a sec ref is refused"; else fail "(a2) fd 3 absent (code=$CODE)"; fi

  # --- (c) every way to name a sec ref is seen, against the public url
  expect_refused "(c) sec/x"                       "$pub" "refs/heads/sec/x $sha refs/heads/sec/x $zero"$'\n'
  expect_refused "(c) HEAD:refs/heads/sec/x"       "$pub" "HEAD $sha refs/heads/sec/x $zero"$'\n'
  expect_refused "(c) sec/x:main (content to main)" "$pub" "refs/heads/sec/x $sha refs/heads/main $sha2"$'\n'
  expect_refused "(c) nested sec/a/b/c"            "$pub" "refs/heads/sec/a/b/c $sha refs/heads/sec/a/b/c $zero"$'\n'
  expect_refused "(c) tag under sec/"              "$pub" "refs/tags/sec/v1 $sha refs/tags/sec/v1 $zero"$'\n'
  expect_refused "(c) a remote-tracking sec ref"   "$pub" "refs/remotes/origin/sec/x $sha refs/heads/foo $zero"$'\n'
  expect_refused "(c) new ref, short names"        "$pub" "sec/x $sha sec/x $zero"$'\n'
  expect_refused "(c) forced update of sec/x"      "$pub" "refs/heads/sec/x $sha refs/heads/sec/x $sha2"$'\n'
  expect_refused "(c) sha-256 repository zero"     "$pub" "refs/heads/sec/x $sha refs/heads/sec/x $zero256"$'\n'
  expect_refused "(c) sec ref among others (--mirror)" "$pub" \
    "refs/heads/main $sha refs/heads/main $sha2"$'\n'"refs/heads/sec/x $sha refs/heads/sec/x $zero"$'\n'"refs/tags/v1 $sha refs/tags/v1 $zero"$'\n'
  # Credentials in a refused url are never echoed.
  local pw="pw-${stage##*/}"
  run_guard "https://user:${pw}@evil.example/runcove/artifacts.git" "$seclne"$'\n'
  if [ "$CODE" -eq 1 ] && ! grep -q "$pw" <<< "$OUT" && grep -q 'https://\*\*\*@evil.example/' <<< "$OUT"; then
    pass "(c) refusal redacts user:password in the url"
  else fail "(c) refusal redacts user:password (code=$CODE)"; fi

  # --- (d) what is not a sec ref passes to the public remote, silently
  expect_ok "(d) main"                             "$pub" "refs/heads/main $sha refs/heads/main $sha2"$'\n'
  expect_ok "(d) a plain tag"                      "$pub" "refs/tags/v1 $sha refs/tags/v1 $zero"$'\n'
  expect_ok "(d) a branch called secure/x"         "$pub" "refs/heads/secure/x $sha refs/heads/secure/x $zero"$'\n'
  expect_ok "(d) a branch called security/x"       "$pub" "refs/heads/security/x $sha refs/heads/security/x $zero"$'\n'
  expect_ok "(d) a branch called sec (no slash)"   "$pub" "refs/heads/sec $sha refs/heads/sec $zero"$'\n'
  expect_ok "(d) ci/sec-pipeline"                  "$pub" "refs/heads/ci/sec-pipeline $sha refs/heads/ci/sec-pipeline $zero"$'\n'
  expect_ok "(d) no lines at all"                  "$pub" ""
  if [ -z "$OUT" ]; then pass "(d) a clean push prints nothing"; else fail "(d) a clean push prints nothing"; fi
  # ...and a url that is not even recognisable is not looked at when no sec ref is pushed.
  expect_ok "(d) main to an odd url"               "/some/local/path" "refs/heads/main $sha refs/heads/main $sha2"$'\n'

  # --- (e) a sec ref and ordinary refs to the sec repository itself
  expect_ok "(e) sec/x to artifacts-sec"           https://git.tenfourty.site/runcove-sec/artifacts-sec.git "$seclne"$'\n'
  expect_ok "(e) main to artifacts-sec"            https://git.tenfourty.site/runcove-sec/artifacts-sec.git "refs/heads/main $sha refs/heads/main $sha2"$'\n'
  expect_ok "(e) sec/x to artifacts-sec, ssh host (ssh://)" ssh://git@git-ssh.tenfourty.site:22/runcove-sec/artifacts-sec.git "$seclne"$'\n'
  expect_ok "(e) sec/x to artifacts-sec, ssh host (scp)"    git@git-ssh.tenfourty.site:runcove-sec/artifacts-sec.git "$seclne"$'\n'

  # --- (f) deletions of a sec ref on the public remote: allowed, with a loud note
  run_guard "$pub" "(delete) $zero refs/heads/sec/x $sha"$'\n'
  if [ "$CODE" -eq 0 ] && grep -q "NOTE" <<< "$OUT" && grep -q "tell security" <<< "$OUT" \
     && ! grep -q REFUSED <<< "$OUT"; then
    pass "(f) deleting sec/x on the public remote allowed, with the note"
  else fail "(f) deleting sec/x on the public remote (code=$CODE)"; fi
  expect_ok "(f) deleting a tag under sec/"        "$pub" "(delete) $zero refs/tags/sec/v1 $sha"$'\n'
  # ...but a delete line does not excuse a push line beside it.
  expect_refused "(f) delete + push of another sec ref" "$pub" \
    "(delete) $zero refs/heads/sec/old $sha"$'\n'"refs/heads/sec/new $sha refs/heads/sec/new $zero"$'\n'
  run_guard https://git.tenfourty.site/runcove-sec/artifacts-sec.git "(delete) $zero refs/heads/sec/x $sha"$'\n'
  if [ "$CODE" -eq 0 ] && ! grep -q NOTE <<< "$OUT"; then
    pass "(f) deleting sec/x on artifacts-sec itself: allowed, no note"
  else fail "(f) deleting sec/x on artifacts-sec itself (code=$CODE)"; fi

  # --- (g) HEAD means the branch it points at (a real repository)
  local repo="$stage/repo"
  git init -q "$repo"
  git -C "$repo" symbolic-ref HEAD refs/heads/sec/topic
  expect_refused "(g) HEAD:main while on sec/topic" "$pub" "HEAD $sha refs/heads/main $sha2"$'\n' "$repo"
  expect_refused "(g) HEAD while on sec/topic, as git expands it" "$pub" "HEAD $sha refs/heads/sec/topic $zero"$'\n' "$repo"
  git -C "$repo" symbolic-ref HEAD refs/heads/main
  expect_ok "(g) HEAD:main while on main" "$pub" "HEAD $sha refs/heads/main $sha2"$'\n' "$repo"
  git -C "$repo" -c user.name=sec-branch-guard-selftest -c user.email=selftest@example.invalid \
    commit -q --allow-empty -m selftest
  git -C "$repo" checkout -q --detach
  expect_ok "(g) HEAD:main, detached" "$pub" "HEAD $sha refs/heads/main $sha2"$'\n' "$repo"
  # outside any repository git cannot answer, and a guard that cannot answer refuses
  run_guard "$pub" "HEAD $sha refs/heads/main $sha2"$'\n' "$stage"
  if [ "$CODE" -eq 1 ] && grep -q "REFUSED" <<< "$OUT" && grep -q "could not say" <<< "$OUT"; then
    pass "(g) outside any repository: refused, fails closed"
  else fail "(g) outside any repository (code=$CODE)"; fi

  # --- (c2) the sec/ match is case-insensitive
  expect_refused "(c2) Sec/x"                      "$pub" "refs/heads/Sec/x $sha refs/heads/Sec/x $zero"$'\n'
  expect_refused "(c2) SEC/x"                      "$pub" "refs/heads/SEC/x $sha refs/heads/SEC/x $zero"$'\n'
  expect_refused "(c2) short name SeC/x"           "$pub" "SeC/x $sha SeC/x $zero"$'\n'
  expect_refused "(c2) tag SEC/v1"                 "$pub" "refs/tags/SEC/v1 $sha refs/tags/SEC/v1 $zero"$'\n'
  expect_refused "(c2) local SEC/x to main"        "$pub" "refs/heads/SEC/x $sha refs/heads/main $sha2"$'\n'
  expect_refused "(c2) remote-tracking Origin/SEC" "$pub" "refs/remotes/origin/SEC/x $sha refs/heads/foo $zero"$'\n'
  expect_ok "(c2) a branch called SECURE/x"        "$pub" "refs/heads/SECURE/x $sha refs/heads/SECURE/x $zero"$'\n'
  expect_ok "(c2) deleting SEC/x"                  "$pub" "(delete) $zero refs/heads/SEC/x $sha"$'\n'

  # --- (h) BY CONTENT: the commits of a local sec branch or tag, under any other name. The lines
  # are the literal strings git hands the hook (captured from real pushes): `HEAD~0`, `@{0}` and
  # `@{-1}` come through as the local ref, a raw commit id as the local ref too.
  local cr="$stage/content" m0 m1 f g mg h u tagobj
  gx() { git -C "$cr" -c user.name=sec-branch-guard-selftest -c user.email=selftest@example.invalid "$@"; }
  git init -q "$cr"
  git -C "$cr" symbolic-ref HEAD refs/heads/main
  gx commit -q --allow-empty -m m0; m0=$(gx rev-parse HEAD)
  gx commit -q --allow-empty -m m1; m1=$(gx rev-parse HEAD)
  gx checkout -q -b sec/fix;  gx commit -q --allow-empty -m fix;      f=$(gx rev-parse HEAD)
  gx checkout -q -b fix2;     gx commit -q --allow-empty -m on-top;   g=$(gx rev-parse HEAD)
  gx checkout -q main; gx checkout -q -b merged; gx merge -q --no-ff -m merge sec/fix; mg=$(gx rev-parse HEAD)
  gx checkout -q main; gx checkout -q -b SEC/upper; gx commit -q --allow-empty -m upper; u=$(gx rev-parse HEAD)
  gx checkout -q main; gx checkout -q -b lonely; gx commit -q --allow-empty -m lonely; h=$(gx rev-parse HEAD)
  gx tag -a -m rel sec/rel "$h"; gx checkout -q main; gx branch -q -D lonely   # only an annotated sec tag holds h
  gx tag -a -m annotated v-fix "$f"; tagobj=$(gx rev-parse v-fix)
  gx checkout -q sec/fix; gx checkout -q main   # so that @{-1} is sec/fix
  local rm_main="refs/heads/main"
  expect_carries "(h) HEAD~0:main from the sec branch"  "$pub" "HEAD~0 $f $rm_main $m1"$'\n' "$cr"
  expect_carries "(h) @{0}:main"                        "$pub" "@{0} $f $rm_main $m1"$'\n' "$cr"
  expect_carries "(h) @{-1}:main (just left sec/fix)"   "$pub" "@{-1} $f $rm_main $m1"$'\n' "$cr"
  expect_carries "(h) a raw commit id"                  "$pub" "$f $f $rm_main $m1"$'\n' "$cr"
  expect_refused "(h) sec/fix^{commit}:main (caught by its name)"            "$pub" "sec/fix^{commit} $f $rm_main $m1"$'\n' "$cr"
  expect_carries "(h) a main with the fix merged in"    "$pub" "refs/heads/merged $mg $rm_main $m1"$'\n' "$cr"
  expect_carries "(h) the fix merged, pushed as HEAD"   "$pub" "HEAD $mg $rm_main $m1"$'\n' "$cr"
  expect_carries "(h) a new branch built on the fix"    "$pub" "refs/heads/fix2 $g refs/heads/fix2 $zero"$'\n' "$cr"
  expect_carries "(h) the fix under another branch name" "$pub" "refs/heads/foo $f refs/heads/foo $zero"$'\n' "$cr"
  expect_carries "(h) the fix under a plain tag name"   "$pub" "refs/tags/v1 $f refs/tags/v1 $zero"$'\n' "$cr"
  expect_carries "(h) an annotated tag of the fix"      "$pub" "refs/tags/v-fix $tagobj refs/tags/v-fix $zero"$'\n' "$cr"
  expect_carries "(h) a sha held only by a sec tag (peeled)" "$pub" "$h $h $rm_main $m1"$'\n' "$cr"
  expect_carries "(h) a branch SEC/upper, in capitals"  "$pub" "$u $u $rm_main $m1"$'\n' "$cr"
  run_guard "$pub" "HEAD~0 $f $rm_main $m1"$'\n' "$cr"
  if grep -q "carries the commits of refs/heads/sec/fix" <<< "$OUT" && grep -q 'HEAD~0 -> refs/heads/main' <<< "$OUT"; then
    pass "(h) the refusal names the sec ref it found"
  else fail "(h) the refusal names the sec ref it found"; fi
  # the sanity case: an ordinary push while sec branches exist but are not merged
  expect_ok "(h) main m1 over m0, sec branches present, none merged" "$pub" "$rm_main $m1 $rm_main $m0"$'\n' "$cr"
  expect_ok "(h) HEAD~1 (the parent of the fix) to a new branch"      "$pub" "HEAD~1 $m1 refs/heads/x $zero"$'\n' "$cr"
  expect_ok "(h) a plain tag on main"                                 "$pub" "refs/tags/v2 $m0 refs/tags/v2 $zero"$'\n' "$cr"
  # ...and the same pushes to the private repo are never refused
  expect_ok "(h) the fix to artifacts-sec as HEAD~0:main" https://git.tenfourty.site/runcove-sec/artifacts-sec.git "HEAD~0 $f $rm_main $m1"$'\n' "$cr"
  expect_ok "(h) a merge to artifacts-sec"                 git@git.tenfourty.site:runcove-sec/artifacts-sec.git "refs/heads/merged $mg $rm_main $m1"$'\n' "$cr"
  # a sha that is no object here cannot be judged: refused
  expect_refused "(h) a sha that is no object in this repo" "$pub" "$sha $sha $rm_main $m1"$'\n' "$cr"
  # the release: once the local sec refs are deleted, the merged main goes
  gx branch -q -D sec/fix SEC/upper; gx tag -d sec/rel >/dev/null
  expect_ok "(h) after the local sec refs are deleted, the merged main goes (the release)" \
    "$pub" "refs/heads/merged $mg $rm_main $m1"$'\n' "$cr"

  # --- (h2) a sec tip the remote already has is exempt; one it does not have is not
  local c2="$stage/content2"
  git init -q "$c2"; git -C "$c2" symbolic-ref HEAD refs/heads/main
  gx2() { git -C "$c2" -c user.name=sec-branch-guard-selftest -c user.email=selftest@example.invalid "$@"; }
  gx2 commit -q --allow-empty -m m0; m0=$(gx2 rev-parse HEAD)
  gx2 commit -q --allow-empty -m m1; m1=$(gx2 rev-parse HEAD)
  gx2 branch sec/empty "$m0"   # no commit of its own, on a commit the remote main already has
  expect_ok "(h2) main m1 over m0 with sec/empty at m0 (already published)" "$pub" "$rm_main $m1 $rm_main $m0"$'\n' "$c2"
  expect_carries "(h2) a new ref at m0: the remote has nothing to compare" "$pub" "refs/heads/y $m0 refs/heads/y $zero"$'\n' "$c2"
  gx2 branch -f sec/empty "$m1"   # now on a main commit that is not published yet
  expect_carries "(h2) sec/empty on the unpublished m1 stops that main (a commit-less branch looks like a fix)" \
    "$pub" "$rm_main $m1 $rm_main $m0"$'\n' "$c2"

  # --- (h3) a sec ref git cannot resolve to a commit: fail closed
  local c3="$stage/content3"
  git init -q "$c3"; git -C "$c3" symbolic-ref HEAD refs/heads/main
  git -C "$c3" -c user.name=x -c user.email=x@example.invalid commit -q --allow-empty -m m0
  git -C "$c3" tag sec/blob "$(printf 'x' | git -C "$c3" hash-object -w --stdin)"
  run_guard "$pub" "$rm_main $(git -C "$c3" rev-parse HEAD) $rm_main $zero"$'\n' "$c3"
  if [ "$CODE" -eq 1 ] && grep -q "could not say" <<< "$OUT"; then
    pass "(h3) a sec tag that is no commit: refused, fails closed"
  else fail "(h3) a sec tag that is no commit (code=$CODE)"; fi

  return "$rc"
}

case "${1:-}" in
  --selftest) selftest ;;
  --args-on-fd3)
    # A missing fd 3 or line leaves the url empty, which refuses a sec ref (fail closed).
    remote=""; url=""
    { IFS= read -r remote <&3 || true; IFS= read -r url <&3 || true; } 2>/dev/null
    check "$remote" "$url" ;;
  -h|--help) usage ;;
  *) check "$@" ;;
esac
