#!/usr/bin/env bash
# scripts/guards/amend-guard.sh — lefthook prepare-commit-msg guard: refuses
# every `git commit --amend` / -c / -C / --fixup / --squash, and refuses
# unconditionally when the commit being rewritten is already on a remote.
#
# Ported from ~/repos/homelab/scripts/amend-guard.sh (ij8.76, 21 Sep 2026),
# with the WHY rewritten for this repo. The homelab's reason is a working
# tree several sessions share. This repo's reasons are different and just as
# mechanical:
#
#   * A HASH THIS SEAT HAS REPORTED IS A PROMISE. 19 Sep 2026: an implementer
#     amended a commit whose hash it had already reported; the review package
#     had been built from that hash and dispatched, so the reviewer was
#     reading four changes while the tree held five. It surfaced only because
#     a test count disagreed by three. That incident is why an untracked
#     .git/hooks/prepare-commit-msg existed here from 19 Sep — this script
#     replaces it, tracked (ij8.5).
#   * A PUSH HERE IS A PRODUCTION CHANGE. CI builds an image from the pushed
#     commit and Flux rolls it out minutes later, unattended. Rewriting a
#     commit that is already on origin changes what the forge will build from
#     and diverges local main from the remote that deployed.
#
# Two independent gates, in order:
#
#   GATE 1 (unconditional): ANY reuse of an existing commit's identity —
#     --amend, -c, -C, --fixup, --squash (and the --reuse-message= /
#     --reedit-message= spellings) — is refused, pushed or not. Override:
#     AMEND_OVERRIDE=1, honest only when you have just run
#     `git log -1 --format=%H` and it printed the hash you created yourself
#     and have told no one about.
#
#   GATE 2 (reached only once GATE 1 is overridden): when a specific existing
#     commit is actually being rewritten — --amend always rewrites HEAD; a
#     bare -c/-C names its own target — and that commit is already reachable
#     from a remote-tracking branch, refuse anyway. Override:
#     AMEND_PUSHED_OVERRIDE=1. Bare --fixup/--squash rewrite nothing, so
#     GATE 2 does not apply to them.
#
# Amending a pushed commit therefore needs BOTH overrides:
#   AMEND_OVERRIDE=1 AMEND_PUSHED_OVERRIDE=1 git commit --amend ...
#
# HOW THE TARGET IS DETECTED (the part that is easy to get wrong)
#
# git's prepare-commit-msg hook gets up to three arguments: the message file,
# the "source", and — only when -c/-C/--amend reused an existing commit's
# message AND no -m/-F override was also given — that commit's sha, with
# source "commit". Measured against real git (homelab, 2026-09-05 and
# 2026-09-12), that leaves holes exactly where this seat lives:
#
#   - `--amend -m "..."` — how Claude Code always commits — reports source
#     "message" and NO third argument. The amended commit is invisible to the
#     hook arguments.
#   - bare `--fixup=<sha>` / `--squash=<sha>` also report "message", no sha.
#   - bare `-c <sha>` / `-C <sha>` DO report "commit" with the sha.
#
# So detection has two paths: git's documented arguments, and a fallback that
# walks the ancestor process tree via /proc to find the `git ... commit ...`
# invocation that triggered this hook and reads its argv directly. Linux only,
# which is every machine this repo is worked on.
#
# Usage (as the lefthook prepare-commit-msg hook):
#   scripts/guards/amend-guard.sh <msg-file> <source> [<sha>]
#   scripts/guards/amend-guard.sh --selftest
set -euo pipefail

# sanitise_arg ARG -> ARG, unless it is an unsubstituted lefthook positional
# placeholder ({1}, {2}, {3}), in which case the empty string.
#
# MEASURED 22 Sep 2026, lefthook 2.1.12, and it had silently disabled GATE 2
# for the only commit shape this seat ever uses. lefthook substitutes a
# positional placeholder only for an argument git actually passed; git passes
# TWO arguments for `git commit --amend -m "..."` (the message file and
# "message"), so the `{3}` in lefthook.yml's `run:` arrives at this script as
# the three literal characters "{3}". That is a non-empty $3, so the guard
# took it for a target sha, asked git whether "{3}" was pushed, got "malformed
# object name", read that as "not pushed" — and allowed the amend of a commit
# that was already on origin. GATE 1 had fired correctly the whole time, which
# is what made it look like it worked.
#
# The selftest could not have caught this: it calls the script with a
# well-formed sha, which is precisely the tidier input shape CLAUDE.md warns
# about. Only a real `git commit --amend -m` through a real lefthook install
# shows it. Wiring anything else as `{n}` inherits the same trap.
sanitise_arg() {
  case "$1" in
    "{1}"|"{2}"|"{3}") printf '' ;;
    *) printf '%s' "$1" ;;
  esac
}

# sha_is_pushed SHA -> 0 if SHA is already reachable from some remote-tracking
# branch, 1 otherwise (including: SHA does not resolve, or there are no
# remotes at all).
sha_is_pushed() {
  local sha="$1"

  if ! git rev-parse --verify --quiet "${sha}^{commit}" >/dev/null; then
    return 1
  fi

  if [[ -n "$(git branch -r --contains "$sha" 2>/dev/null)" ]]; then
    return 0
  fi

  if git rev-parse --verify --quiet origin/main >/dev/null 2>&1 && \
     git merge-base --is-ancestor "$sha" origin/main 2>/dev/null; then
    return 0
  fi

  return 1
}

# get_commit_argv -> fills the global array COMMIT_ARGV with the arguments
# following "commit" in the `git ... commit ...` ancestor process that
# triggered this hook. Empty (and returns 1) when /proc is unavailable or no
# such ancestor is found within the depth limit.
COMMIT_ARGV=()
get_commit_argv() {
  COMMIT_ARGV=()
  [[ -d /proc ]] || return 1

  local pid="$$" depth=0
  while (( depth < 20 )) && [[ "$pid" != "1" ]] && [[ -d "/proc/$pid" ]]; do
    local cmdline_file="/proc/$pid/cmdline"
    if [[ -r "$cmdline_file" ]]; then
      local -a argv=()
      while IFS= read -r -d '' arg; do argv+=("$arg"); done <"$cmdline_file"

      if (( ${#argv[@]} > 0 )) && [[ "$(basename "${argv[0]}")" == "git" ]]; then
        local i commit_idx=-1
        for i in "${!argv[@]}"; do
          if [[ "${argv[$i]}" == "commit" ]]; then
            commit_idx=$i
            break
          fi
        done
        if (( commit_idx >= 0 )); then
          COMMIT_ARGV=("${argv[@]:$((commit_idx + 1))}")
          return 0
        fi
        # The `git` ancestor that triggered this hook was not a `commit`
        # invocation: nothing further up is relevant either.
        return 1
      fi
    fi

    local ppid
    ppid=$(awk '/^PPid:/{print $2}' "/proc/$pid/status" 2>/dev/null) || return 1
    [[ -n "$ppid" ]] || return 1
    pid="$ppid"
    depth=$((depth + 1))
  done

  return 1
}

# argv_has_reuse_flag -> 0 if COMMIT_ARGV contains any flag that reuses an
# existing commit's identity. Deliberately does not chase a no-space short
# form like `-c<sha>`; the arguments path above catches bare -c/-C anyway.
argv_has_reuse_flag() {
  (( ${#COMMIT_ARGV[@]} == 0 )) && return 1
  local a
  for a in "${COMMIT_ARGV[@]}"; do
    case "$a" in
      --amend|-c|-C|--fixup|--squash|--fixup=*|--squash=*|--reuse-message=*|--reedit-message=*)
        return 0 ;;
    esac
  done
  return 1
}

# argv_has_amend -> 0 if COMMIT_ARGV contains --amend specifically. --amend
# always replaces HEAD, so GATE 2 has a target even when git gave us no sha.
argv_has_amend() {
  (( ${#COMMIT_ARGV[@]} == 0 )) && return 1
  local a
  for a in "${COMMIT_ARGV[@]}"; do
    [[ "$a" == "--amend" ]] && return 0
  done
  return 1
}

refuse_reuse() {
  {
    echo "amend-guard: REFUSED — rewriting a commit's identity."
    echo
    echo "--amend / -c / -C / --fixup / --squash all reuse an existing commit."
    echo "A hash this seat has reported — to Jeremy, to ops, in a bead note — is a"
    echo "promise about what that hash contains (19 Sep 2026: an amend after the"
    echo "review package had been built from the hash; the reviewer read four"
    echo "changes while the tree held five)."
    echo
    echo "Make a NEW commit instead:"
    echo "  git commit -m \"...\" -- <paths>"
    echo
    echo "Override only when you have just run \`git log -1 --format=%H\` and it"
    echo "printed a hash you created yourself and have told nobody about:"
    echo "  AMEND_OVERRIDE=1 git commit --amend ..."
  } >&2
}

refuse_pushed() {
  local sha="$1"
  {
    echo "amend-guard: REFUSED — $sha is already reachable from a remote-tracking branch."
    echo
    echo "A push here is a production change: CI builds an image from the pushed"
    echo "commit and Flux rolls it out minutes later, unattended. Rewriting a"
    echo "commit origin already has diverges local main from the remote that"
    echo "deployed, and changes what the forge believes it built."
    echo
    echo "Instead, make a new commit:"
    echo "  git commit -m \"...\" -- <paths>"
    echo
    echo "If this really must be amended (and you accept the force-push and the"
    echo "history rewrite that follow), override BOTH gates:"
    echo "  AMEND_OVERRIDE=1 AMEND_PUSHED_OVERRIDE=1 git commit --amend ..."
  } >&2
}

main() {
  if [[ "${1:-}" == "--selftest" ]]; then
    selftest
    exit $?
  fi

  local source sha
  source=$(sanitise_arg "${2:-}")
  sha=$(sanitise_arg "${3:-}")
  # A sha that does not resolve is not a target. Belt and braces around the
  # placeholder case above, and around anything else that hands this hook a
  # string where a commit was expected.
  if [[ -n "$sha" ]] && ! git rev-parse --verify --quiet "${sha}^{commit}" >/dev/null 2>&1; then
    sha=""
  fi
  get_commit_argv || true

  local risky=0
  if [[ "$source" == "commit" && -n "$sha" ]]; then
    risky=1
  elif argv_has_reuse_flag; then
    risky=1
  fi

  if (( ! risky )); then
    exit 0
  fi

  if [[ "${AMEND_OVERRIDE:-}" != "1" ]]; then
    refuse_reuse
    exit 1
  fi
  echo "amend-guard: GATE 1 OVERRIDDEN by AMEND_OVERRIDE=1 — proceeding on the caller's word that HEAD is its own unreported commit" >&2

  local target=""
  if [[ -n "$sha" ]]; then
    target="$sha"
  elif argv_has_amend; then
    target="$(git rev-parse -q --verify HEAD)" || target=""
  fi

  if [[ -z "$target" ]]; then
    exit 0
  fi

  if [[ "${AMEND_PUSHED_OVERRIDE:-}" == "1" ]]; then
    if sha_is_pushed "$target"; then
      echo "amend-guard: GATE 2 OVERRIDDEN by AMEND_PUSHED_OVERRIDE=1 — rewriting $target, which is already on a remote" >&2
    fi
    exit 0
  fi

  if sha_is_pushed "$target"; then
    refuse_pushed "$target"
    exit 1
  fi

  exit 0
}

selftest() {
  local fails=0
  local script_path
  script_path="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

  local work origin
  work=$(mktemp -d)
  origin=$(mktemp -d)
  git init -q --bare "$origin"
  git init -q "$work"
  git -C "$work" config user.email "selftest@example.invalid"
  git -C "$work" config user.name "amend-guard-selftest"
  git -C "$work" remote add origin "$origin"

  local main_branch
  main_branch=$(git -C "$work" symbolic-ref --short HEAD 2>/dev/null || echo main)

  echo "one" >"$work/file.txt"
  git -C "$work" -c commit.gpgsign=false add file.txt
  git -C "$work" -c commit.gpgsign=false commit -q -m "first commit"
  git -C "$work" push -q origin HEAD:main
  git -C "$work" fetch -q origin
  local pushed_sha
  pushed_sha=$(git -C "$work" rev-parse HEAD)

  echo "two" >"$work/file2.txt"
  git -C "$work" -c commit.gpgsign=false add file2.txt
  git -C "$work" -c commit.gpgsign=false commit -q -m "second commit (unpushed)"
  local unpushed_sha
  unpushed_sha=$(git -C "$work" rev-parse HEAD)

  local msgfile
  msgfile=$(mktemp)
  echo "test message" >"$msgfile"

  run_case() {
    local desc="$1" expect="$2" env_extra="$3"
    shift 3
    local rc=0 out
    # shellcheck disable=SC2086  # env_extra is a deliberate word-split list
    out=$(cd "$work" && env $env_extra "$script_path" "$@" 2>&1) || rc=$?
    if (( rc == expect )); then
      echo "PASS: $desc (exit $rc)"
    else
      fails=$((fails + 1))
      echo "FAIL: $desc (expected exit $expect, got $rc)"
      echo "$out" | sed 's/^/    /'
    fi
  }

  # --- arguments path: git's documented hook arguments (source "commit"
  # with a sha), which covers bare -c/-C and an --amend with no -m.
  run_case "(a) plain amend of an unpushed commit refused (GATE 1)" 1 "" \
    "$msgfile" "commit" "$unpushed_sha"
  run_case "(b) AMEND_OVERRIDE=1 allows amend of an unpushed commit" 0 "AMEND_OVERRIDE=1" \
    "$msgfile" "commit" "$unpushed_sha"
  run_case "(c) plain amend of a pushed commit refused (GATE 1)" 1 "" \
    "$msgfile" "commit" "$pushed_sha"
  run_case "(d) AMEND_OVERRIDE=1 alone does NOT allow amend of a pushed commit (GATE 2)" 1 "AMEND_OVERRIDE=1" \
    "$msgfile" "commit" "$pushed_sha"
  run_case "(e) both overrides allow amend of a pushed commit" 0 "AMEND_OVERRIDE=1 AMEND_PUSHED_OVERRIDE=1" \
    "$msgfile" "commit" "$pushed_sha"
  run_case "(f) an ordinary commit passes" 0 "" \
    "$msgfile" "message"

  # The literal-placeholder shape lefthook hands over when git passed fewer
  # arguments than lefthook.yml names (see sanitise_arg): the guard must treat
  # "{3}" as no sha at all, not as a target that cannot be resolved.
  run_case "(f2) a literal {3} placeholder is not a target: ordinary commit still passes" 0 "" \
    "$msgfile" "message" "{3}"
  run_case "(f3) a literal {3} with an unresolvable {2} source passes too" 0 "" \
    "$msgfile" "{2}" "{3}"
  # An unresolvable sha is discarded rather than asked about: git never passes
  # one, and "is 000…0 pushed?" answers "no", which is the wrong side to fail
  # on. With no sha the guard falls back to the process-tree path, which in a
  # direct call like this one finds no `git commit` ancestor at all — so an
  # ordinary exit 0 here is the discard working, not the guard missing.
  run_case "(f4) an unresolvable sha is discarded, not answered about" 0 "" \
    "$msgfile" "commit" "0000000000000000000000000000000000000000"

  # --- process-tree fallback: REAL git invocations, which is where git's own
  # hook arguments go blind. Installed as a raw git hook so this test needs no
  # lefthook.
  local hooks_dir
  hooks_dir=$(git -C "$work" rev-parse --git-path hooks)
  cat >"$work/$hooks_dir/prepare-commit-msg" <<EOF
#!/usr/bin/env bash
exec "$script_path" "\$@"
EOF
  chmod +x "$work/$hooks_dir/prepare-commit-msg"

  run_hook_case() {
    local desc="$1" want_success="$2"
    shift 2
    local rc=0
    ( cd "$work" && "$@" ) >/dev/null 2>&1 || rc=$?
    if { [[ "$want_success" == "1" ]] && (( rc == 0 )); } || \
       { [[ "$want_success" == "0" ]] && (( rc != 0 )); }; then
      echo "PASS: $desc (exit $rc)"
    else
      fails=$((fails + 1))
      echo "FAIL: $desc (exit $rc)"
    fi
  }

  # HEAD is unpushed_sha here.
  run_hook_case "(g) amend -m of an unpushed commit refused (process-tree fallback)" 0 \
    git -c commit.gpgsign=false commit --amend -m "second commit, amended" -q
  run_hook_case "(h) AMEND_OVERRIDE=1 allows amend -m of an unpushed commit (fallback)" 1 \
    env AMEND_OVERRIDE=1 git -c commit.gpgsign=false commit --amend -m "second commit, amended again" -q

  git -C "$work" checkout -q "$pushed_sha"
  run_hook_case "(i) amend -m of a PUSHED commit refused with AMEND_OVERRIDE alone (fallback)" 0 \
    env AMEND_OVERRIDE=1 git -c commit.gpgsign=false commit --amend -m "first commit, amended" -q
  run_hook_case "(j) both overrides allow amend -m of a pushed commit (fallback)" 1 \
    env AMEND_OVERRIDE=1 AMEND_PUSHED_OVERRIDE=1 git -c commit.gpgsign=false commit --amend -m "first commit, amended again" -q

  # Bare --fixup=<sha>: source "message", no sha, no --amend — invisible to
  # the arguments path entirely. It rewrites nothing, so once GATE 1 is
  # overridden GATE 2 must NOT fire even though the target is pushed.
  git -C "$work" checkout -q "$main_branch"
  echo "three" >"$work/file3.txt"
  git -C "$work" add file3.txt
  run_hook_case "(k) --fixup of a pushed commit refused without override (fallback)" 0 \
    git -c commit.gpgsign=false commit --fixup="$pushed_sha" -q
  run_hook_case "(l) AMEND_OVERRIDE=1 allows --fixup (no rewrite target, GATE 2 must not fire)" 1 \
    env AMEND_OVERRIDE=1 git -c commit.gpgsign=false commit --fixup="$pushed_sha" -q

  rm -f "$msgfile"
  rm -rf "$work" "$origin"

  echo
  if (( fails > 0 )); then
    echo "amend-guard selftest: $fails case(s) FAILED"
    return 1
  fi
  echo "amend-guard selftest: all cases passed"
  return 0
}

main "$@"
