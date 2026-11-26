#!/bin/bash -p
unset CDPATH
# scripts/guards/ggshield-gate.sh — run ggshield over the staged change and
# allow the commit only when the scan provably finished and found nothing.
#
# WHY A WRAPPER. ggshield has three outcomes and they must not be conflated:
# a finding (block the commit), a clean scan (allow it), and "could not scan,
# or not all of it" (no ggshield installed, no API key, no network, quota
# exhausted, a request the service refused). Only a clean scan lets a commit
# through. A finding refuses it, and so does anything else: a gate that passes
# when it cannot look reads as a scanned repo when it is not. The way out is
# explicit, never silent: GGSHIELD_SKIP=1 lets one commit (or a run of them,
# offline) through and says loudly that nothing was scanned.
#
# CLEAN IS READ FROM ggshield's REPORT, NOT ITS EXIT STATUS. The verdict is
# ggshield-judge.py's (beside this file, run with `python3 -I`; its header has
# the whole rule): exit 0, a JSON report that parses, no errors entry in it,
# the scanned commit listed in it exactly once, one file result for each document
# ggshield sends for it (the judge predicts those by running git itself, in the
# environment this script gives it: the pinned settings, literal pathspecs, no
# replace refs, the top of the tree; it reads the --config-path file this script
# passes, and refuses a scan with none), and no error line in ggshield's other
# output. Anything short of that refuses. The judge answers with an exit
# status and one word, and the gate takes only these pairs (the whole of its
# stdout must be the word): 0 clean allows; 1 finding, 1 finding-incomplete (an
# incident listed in a scan that did not finish) and 1 finding-unread (ggshield
# exited 1 and no finding could be read) refuse as a finding, with no opt-out
# named; 2 skipped (ggshield left a file out: a re-run leaves it out again) and
# 2 incomplete refuse as not scanned. Any other pair refuses as not scanned.
#
# WHAT IS SCANNED. `ggshield secret scan pre-commit` has no JSON report (with
# --json it still prints text; measured, ggshield 1.56.1), so the staged
# change is scanned as a commit instead: `git write-tree` on a COPY of the
# index the hook was given (the real index is not touched, and `git commit --
# <paths>` hands the hook its own index of exactly what will be committed),
# `git commit-tree` on that tree with HEAD as its parent (unsigned, no ref
# moved, left unreferenced for git's own clean-up), then
# `ggshield secret scan --json commit-range` over that one commit. Its tree is
# the tree the real commit will have. While a merge is being concluded the
# commit's one parent is still HEAD, so everything the merge brings in is
# scanned. The index must be found where git says it is, and it must be
# exactly the tree that was written (`git diff-index --cached
# --ita-invisible-in-index --quiet`: an intent-to-add entry is not in the
# commit and is not a difference): no index, a difference or an error refuses,
# so a staged change is never reported clean from a scan of another tree. When
# the tree lists no changed file it must be HEAD's own tree, or the gate refuses.
# "Every document handed over" is therefore that one commit: the report must
# list it, once, and no other. Files are not compared one by one,
# because ggshield itself leaves some out (binaries, deletions); a request the
# service refused shows as an error line or an errors entry (measured).
#
# THE SCAN USES ONLY ITS OWN FIXED SETTINGS: the environment variable
# GITGUARDIAN_DONT_LOAD_ENV=1, the default dashboard as --instance, and
# --config-path on an empty file of its own, which "ignores local and global
# config files" (ggshield --help). The instance variables are unset for the
# call. Measured with ggshield 1.56.1 and a dummy key: the machine's stored
# authentication still works under these settings (api-status healthy).
#
# THE ggshield IT RUNS is the one on PATH, and the script refuses one that
# resolves inside this repository's working tree (a repo virtualenv, a bin
# directory a tool adds): the scan is run by a program from outside the tree.
# The same holds for the python3 that runs the judge and for git, each checked
# against the directory the hook runs in, the nearest directory above it that
# holds .git, and the top git names. Before any program runs, PATH itself is
# proven with bash builtins only: an empty or relative entry, or one inside the
# working tree (one that does not exist is read through its longest existing
# ancestor, resolved physically, with the missing parts appended; a dangling symlink among
# them cannot be read and refuses), refuses (a helper
# such as env or readlink planted there would otherwise decide the verdict). So
# are GIT_DIR and GIT_WORK_TREE: set, they must name the repository the hook runs
# in (the nearest directory above the current one holding .git, and that tree's
# own git directory, which in a linked worktree is where its .git file points), or
# the gate refuses before running anything; a run from outside the tree with
# either set is refused too, and so is a git directory kept apart from its work
# tree. A control character (a newline) in the directory the hook runs in, a
# PATH entry, GIT_DIR, GIT_WORK_TREE or the .git file refuses: command
# substitution drops a trailing newline, so a name ending in one would be read as
# another.
#
# GIT SETTINGS THAT RUN A PROGRAM OR HIDE CONTENT. ggshield reads the commit
# with porcelain (measured, 1.56.1: `git -c core.quotePath=false -c
# safe.directory=* show <commit> --raw -z`, then `... --raw -z --patch -- <paths>`,
# with GIT_CONFIG_SYSTEM empty and GIT_CONFIG_GLOBAL set to GG_GIT_CONFIG or
# empty), so textconv and the diff attribute apply there: a textconv program can
# replace a staged file's content, and a file marked -diff or binary is shown as
# "Binary files differ", and either way the secret never reaches the scan. The
# gate's own write-tree runs a filter driver's program (filter.<x>.process,
# measured with GIT_TRACE). Attributes can come from a staged or unstaged
# .gitattributes, info/attributes, core.attributesFile, the XDG attributes file,
# attr.tree or GIT_ATTR_SOURCE, so the gate does not try to follow them to a
# driver. Before any git call that could run one, it REFUSES when any
# diff.<x>.textconv, diff.<x>.command, diff.<x>.binary, filter.<x>.clean,
# filter.<x>.smudge, filter.<x>.process, lfs.extension.<x>.<anything> or
# attr.tree is set (git-lfs's own stock filter.lfs.* excepted: see below),
# whether or not an attribute uses it today. The settings are read with --includes twice: as the
# gate's own git reads them (system, global, XDG, local, worktree and the
# command line), and as ggshield's git does (its own environment and -c
# options, safe.directory=* included). And it REFUSES when the diff attribute
# is unset (-diff, binary) for a changed path, read with check-attr in both
# environments. The other settings and variables MEASURED to change
# ggshield's patch are pinned for ggshield's git instead (only those: see the
# end of this section): GG_GIT_CONFIG, GIT_ATTR_SOURCE, GIT_EXTERNAL_DIFF,
# GIT_DIFF_OPTS and GIT_SHALLOW_FILE are unset, GIT_NO_REPLACE_OBJECTS=1 and
# GIT_GRAFT_FILE=/dev/null are set (a replace ref or a graft changed the parent
# and hid the change), and a GIT_CONFIG_COUNT group (command-line scope, above
# every file) pins core.fsmonitor, core.bigFileThreshold (1 hid the content),
# colour, format.pretty, the diff prefixes, diff.relative (from a subdirectory
# it dropped the file), i18n.logOutputEncoding, log.showSignature,
# log.showRoot (off, it emptied the patch of a first commit, or of any commit
# the shallow list names) and core.commitGraph (off: a planted commit-graph
# gave the scanned commit another tree) (see SCAN_PINS). Also for ggshield's
# git: GIT_LITERAL_PATHSPECS=1 with the glob, noglob and icase variables
# unset (a path is never read as pathspec magic; ggshield 1.56.1 itself dies on
# a staged path that starts with ":" before it asks for the patch, measured,
# so this is a second wall, not a hole closed), the scan run from the top of
# the tree (ggshield hands git show the paths named from the top while its git
# runs where ggshield runs: from a subdirectory the patch held nothing,
# measured with ggshield 1.56.1 and GIT_TRACE),
# and no notes shown (GIT_NOTES_REF and GIT_NOTES_DISPLAY_REF both empty; a
# note named by either, core.notesRef or notes.displayRef is not shown).
# GIT_CONFIG is unset for every git call: `git config` reads only that file,
# while show and write-tree ignore it, so set it would blind the settings
# check. The id of the commit made for the scan can be predicted (its tree,
# parent and the dates passed through), so the gate REFUSES when the shallow
# list names it (git reads a line by its leading id, in either case; listed,
# git would show it as a first commit), and when that commit's object, read as
# git reads it, does not hash to its id (git show does not check that: a
# planted object naming HEAD's tree, loose or in an alternate store, emptied
# the patch). Every object on HEAD's side of the change is hash-checked the
# same way: HEAD's commit, its tree, and each tree and blob the comparison
# reads on that side (git does not check a tree's or a blob's id when it
# compares them: a loose object planted under HEAD's id for a file or a
# directory, holding the staged content, emptied the patch, measured with the
# stub that reads as ggshield 1.56.1 does). A file added has no such object
# and a submodule's id names none here, so neither is hashed. The objects of
# the scanned commit's own tree are not hash-checked; that commit's object is
# (above). Those objects are read by one `git cat-file --batch` and hashed by
# one python3, so the check costs the same few processes for 2 files or 2000.
# git's two listings of the change (diff-tree) are bounded by a watchdog in
# plain bash (no coreutils `timeout`, which macOS lacks): one that does not
# finish in 120 s, or the lower GGSHIELD_LIST_TIMEOUT, is stopped and refuses
# instead of holding the commit. A HEAD whose commit fails git's own check when the gate peels it is
# read as no first commit, so the whole tree is scanned. A shallow HEAD (a --depth clone) is not refused:
# the change is still shown in full. Probed against ggshield's two show calls and found to change nothing
# in what reaches the scan: diff.<x>.command (no --ext-diff), diff.external,
# core.pager and pager.show (no terminal), log.showSignature with gpg.program
# (the scanned commit is unsigned), filter drivers, working-tree-encoding,
# core.autocrlf, core.eol, core.safecrlf, core.whitespace, diff.algorithm,
# diff.context=0, diff.interHunkContext, diff.renames=copies,
# diff.renameLimit, diff.suppressBlankEmpty, diff.indentHeuristic,
# diff.wsErrorHighlight, diff.colorMoved, diff.dirstat, diff.submodule,
# diff.ignoreSubmodules, diff.trustExitCode, log.follow, log.diffMerges,
# log.decorate, log.excludeDecoration, log.initialDecorationSet, log.date,
# log.mailmap, log.abbrevCommit, core.abbrev, pretty.medium (an alias cannot
# replace the built-in format), i18n.commitEncoding, core.commentChar,
# core.sparseCheckout, core.sparseCheckoutCone, index.sparse, core.splitIndex,
# core.multiPackIndex, core.ignoreCase, core.precomposeUnicode, column.ui,
# status.*, skip-worktree, objects/info/alternates (an object planted there is
# caught by the hash check); diff.orderFile only reorders the files;
# core.maxTreeDepth set low makes git show fail aloud (measured), which the
# judge should refuse (inferred, not run through ggshield). These are the
# settings measured, not a proof that no other one exists: a git setting or
# variable not listed here is outside what this gate defends. The gate's own
# git calls run with core.fsmonitor and core.commitGraph off, no replace refs
# and no grafts, and an inherited GIT_CONFIG_COUNT group is dropped.
# GIT_CONFIG_PARAMETERS is dropped for ggshield's git. A machine whose git
# config holds git-lfs's filter.lfs.* is accepted only when every filter.lfs
# key present is one of clean, smudge, process and required, at exactly git-lfs's
# stock value (`git-lfs clean -- %f`, `git-lfs smudge -- %f`,
# `git-lfs filter-process`, `true`); an identical value repeated across files
# (the package's system file and a user's `git lfs install`) is the same value,
# but any differing value, any other key, or another driver (a name that differs
# only in case included) is refused as above, and so is any lfs.extension.* (git-lfs
# runs those programs during clean). Because that driver is let through, the gate
# also REFUSES (1) any changed path whose filter attribute is set to anything,
# read with check-attr as the gate's git and as ggshield's git read them, from the
# working tree and with --cached (the index only, so a staged .gitattributes
# counts); attr.tree stays refused; and (2) any changed path whose new blob is a
# git-lfs pointer (at most 1024 bytes, first line exactly `version
# https://git-lfs.github.com/spec/v1` or the legacy `version
# https://hawser.github.com/spec/v1`), read from the scanned tree with cat-file:
# the attribute that made a pointer can be gone by commit time (`git lfs
# untrack`), and the content behind it sits in .git/lfs/objects, never in the
# scan. The gate's own write-tree may run the stock git-lfs program before those
# refusals. Limits outside the repository's reach: `skip_lfs: true` in lefthook.yml
# (so lefthook runs no `git lfs pre-push`) can be turned off again by a person's
# own lefthook-local.yml, and a manual `git lfs push` uploads whatever
# .git/lfs/objects holds regardless. The pointer refusal is the wall; skip_lfs is
# the second one.
# CHECKED BEFORE, READ AGAIN AFTER: the settings, attributes, shallow list,
# the scanned commit's object and the objects on HEAD's side are read before ggshield's git reads them, and
# lefthook runs pre-commit jobs in parallel, so something could change them in
# between. For a scan that would pass they are read again once it ends, and
# any difference refuses; a change made and undone while the scan ran is not
# seen (a stated limit).
# The identity check below assumes an honest git, one found outside the tree.
#
# WHAT STILL RUNS BEFORE THIS GATE, and is not checked by it: lefthook's own
# `sh` (it starts the hook through one), the interpreter the shebang names
# (/bin/bash -p, an absolute path, so no PATH lookup; -p ignores BASH_ENV, ENV,
# CDPATH and exported functions, and the first lines unset CDPATH anyway), and
# the ENVIRONMENT the gate inherits. The scan call drops BASH_ENV, ENV and the
# PYTHON* variables that change how a Python ggshield starts (PYTHONPATH,
# PYTHONHOME, PYTHONSTARTUP, PYTHONUSERBASE, PYTHONINSPECT) and the instance
# variables; HTTPS_PROXY, HTTP_PROXY, REQUESTS_CA_BUNDLE and SSL_CERT_FILE are
# left alone on purpose (a person may need them), so a hostile environment
# that sets them is outside what this gate defends. Two more are trusted, as
# stated limits: GIT_INDEX_FILE (git itself exports it in a pre-commit hook, for
# commit -a and for a partial commit; the index it names is compared with the tree
# that was scanned, but it is not checked to be this repository's), and
# core.worktree in .git/config (a setting that needs write access to .git/config
# and makes git read another tree).
#
# SKIPPING is exactly GGSHIELD_SKIP=1. Any other value (0, empty, yes) scans.
# It is a switch for a person's own machine only: where CI, FORGEJO_ACTIONS or
# GITHUB_ACTIONS is set (to anything, even empty) GGSHIELD_SKIP=1 is refused.
#
# CI does not run this script or ggshield; it runs the guard selftests and the
# whole-tree scan for credentials on a command line. lefthook's own skips
# (LEFTHOOK=0, git commit --no-verify) are outside this gate.
#
# MEASURED on the management machine, 22 Sep 2026 (ij8.76), against ggshield 1.54.0:
#   * a throwaway RSA private key staged   -> rc 1, "1 secret detected"
#   * outside a git repo                   -> rc != 0 with "Not a git directory"
#   * `ggshield api-status`                -> healthy: ggshield IS
#     authenticated on this machine; ij8.2's
#     "needs a key from Jeremy" is about the scan-every-publish path, not
#     about this host being unable to scan at all.
#
# AND A TRAP WORTH KEEPING: a fabricated credential is not a positive control.
# A random `ghp_`+36 string and the canonical AWS documentation key both
# scanned CLEAN — GitGuardian checksum-validates GitHub tokens and allowlists
# the AWS example. The only fixture that proved the detector alive was a real
# (throwaway, never-installed) RSA private key. If you ever need to re-prove
# this gate against the live service, use `openssl genrsa`, not a made-up
# token, or you will "prove" a blind scanner works.
#
# Quota: the GitGuardian workspace's allowance is 10,000 documents a month and
# the daily transcript scan already wants it. One staged diff per commit is
# small, but GGSHIELD_SKIP=1 exists for a session that is about to make many
# commits offline.
#
# Usage:  scripts/guards/ggshield-gate.sh            # lefthook pre-commit
#         scripts/guards/ggshield-gate.sh --selftest # the verdicts, with a stub
set -uo pipefail
SELF="ggshield-gate"
SCRATCH=""
INSTANCE="https://dashboard.gitguardian.com"
# Bash builtins only: no helper program has been proven yet when this line runs.
case "${BASH_SOURCE[0]}" in
  /*) JUDGE="$(cd -- "${BASH_SOURCE[0]%/*}/" >/dev/null 2>&1 && pwd)/ggshield-judge.py" ;;
  */*) JUDGE="$(cd -- "./${BASH_SOURCE[0]%/*}" >/dev/null 2>&1 && pwd)/ggshield-judge.py" ;;
  *) JUDGE="$(pwd)/ggshield-judge.py" ;;
esac

# physdir <dir>: bash builtins only. Sets PHYS to the physical path of <dir>. Returns 1 when it cannot be
# resolved and 2 when the path holds a control character (a newline included: command substitution drops
# a trailing newline, so a sentinel is appended and taken off again, and a tree named with one cannot
# be read as another).
physdir() {
  # A relative value is read from here, as git reads it: bash would take a bare - as $OLDPWD.
  case "$1" in
    /*) ;;
    *) set -- "./$1" ;;
  esac
  PHYS=$(cd -P -- "$1" >/dev/null 2>&1 && pwd -P 2>/dev/null; printf x)
  PHYS=${PHYS%x}
  [ -n "$PHYS" ] || return 1
  PHYS=${PHYS%$'\n'}
  [ -n "$PHYS" ] || return 1
  [[ $PHYS == *[[:cntrl:]]* ]] && return 2
  return 0
}

# path_proven [<tree top>]: bash builtins only (cd -P, pwd -P), so that no program a PATH entry could
# hold has run yet. Refuses an empty entry (the current directory), a relative one (also .), one with a
# control character, and one inside the working tree, which is taken as the directory the hook runs in
# AND the nearest directory above it holding .git (found without git, which cannot be trusted yet), AND
# the top git names once the caller has it. An entry that does not exist is read through its longest
# existing ancestor, resolved physically, with the missing components appended. A current directory
# with a control character in its path refuses. Prints why and returns 1 on the first entry refused.
path_proven() {
  local rest=${PATH:-}: entry real here up d t p tail anc rc
  physdir . ; rc=$?
  case "$rc" in
    0) here=$PHYS ;;
    2)
      echo "$SELF: REFUSED — the directory the hook runs in has a control character (a newline) in its path, so NOTHING was scanned for secrets." >&2
      return 1
      ;;
    *)
      echo "$SELF: REFUSED — the current directory cannot be resolved, so NOTHING was scanned for secrets." >&2
      return 1
      ;;
  esac
  up=
  d=$here
  while [ -n "$d" ]; do
    if [ -e "$d/.git" ]; then up=$d; break; fi
    d=${d%/*}
  done
  while [ -n "$rest" ]; do
    entry=${rest%%:*}
    rest=${rest#*:}
    if [[ $entry == *[[:cntrl:]]* ]]; then
      echo "$SELF: REFUSED — PATH has an entry with a control character (a newline) in it, so NOTHING was scanned for secrets." >&2
      return 1
    fi
    case "$entry" in
      "")
        echo "$SELF: REFUSED — PATH has an empty entry (the current directory), so NOTHING was scanned for secrets." >&2
        echo "  Remove it from PATH, or opt out for this commit with GGSHIELD_SKIP=1." >&2
        return 1
        ;;
      /*) ;;
      *)
        echo "$SELF: REFUSED — PATH has a relative entry ($entry), so NOTHING was scanned for secrets." >&2
        echo "  Make it absolute or remove it, or opt out for this commit with GGSHIELD_SKIP=1." >&2
        return 1
        ;;
    esac
    physdir "$entry"; rc=$?
    if [ "$rc" = 2 ]; then
      echo "$SELF: REFUSED — the PATH entry $entry resolves to a path with a control character (a newline) in it, so NOTHING was scanned for secrets." >&2
      return 1
    fi
    if [ "$rc" = 0 ]; then
      real=$PHYS
    else
      # An entry that does not exist is read through its longest existing ancestor, resolved
      # physically, with the missing components appended: with link -> repo, an entry of
      # link/.not-there/bin is inside the tree. A symlink among the missing components (a dangling
      # one) cannot be read, and so refuses.
      case "$entry" in
        */.. | */../* | */. | */./*)
          echo "$SELF: REFUSED — the PATH entry $entry does not exist and holds . or .. , so NOTHING was scanned for secrets." >&2
          return 1
          ;;
      esac
      p=$entry
      tail=
      anc=
      while [ -n "$p" ]; do
        if [ -L "$p" ]; then
          echo "$SELF: REFUSED — the PATH entry $entry passes through a symlink that leads nowhere ($p), so NOTHING was scanned for secrets." >&2
          return 1
        fi
        tail=/${p##*/}$tail
        p=${p%/*}
        [ -n "$p" ] || break
        physdir "$p"; rc=$?
        if [ "$rc" = 2 ]; then
          echo "$SELF: REFUSED — the PATH entry $entry passes through a path with a control character (a newline) in it, so NOTHING was scanned for secrets." >&2
          return 1
        fi
        if [ "$rc" = 0 ]; then anc=$PHYS; break; fi
        anc=
      done
      real=${anc%/}$tail
    fi
    for t in "$here" "$up" "${1:-}"; do
      [ -n "$t" ] || continue
      case "$real" in
        "$t" | "$t"/*)
          echo "$SELF: REFUSED — the PATH entry $entry resolves inside the repository working tree, so NOTHING was scanned for secrets." >&2
          echo "  Take it off PATH, or opt out for this commit with GGSHIELD_SKIP=1." >&2
          return 1
          ;;
      esac
    done
  done
  return 0
}

# git_env_proven: bash builtins only. GIT_DIR and GIT_WORK_TREE make git work in a repository other than
# the one the hook runs in, so when either is set both must point at the directory the hook runs in:
# the nearest directory above the current one that holds .git (found without git) is the work tree
# GIT_WORK_TREE names, and the git directory GIT_DIR names is that tree's own (its .git directory, or
# where its .git file points, which is what git exports in a linked worktree). All compared as physical
# paths. An empty value, a control character (a newline) in either value, in the current directory's
# path or anywhere in the .git file, or a current directory outside the tree, refuses. A git
# directory kept apart from its work tree (as --git-dir and --work-tree give it, or a bare repository)
# is refused and said to be. Unset: nothing to prove.
git_env_proven() {
  local here up d line gd want v rc apart=
  [ -n "${GIT_DIR+set}" ] || [ -n "${GIT_WORK_TREE+set}" ] || return 0
  physdir . ; rc=$?
  here=
  [ "$rc" != 0 ] || here=$PHYS
  for v in GIT_DIR GIT_WORK_TREE; do
    if [ -n "${!v+set}" ] && [[ ${!v} == *[[:cntrl:]]* ]]; then rc=2; fi
  done
  if [ "$rc" = 2 ]; then
    echo "$SELF: REFUSED — GIT_DIR, GIT_WORK_TREE or the directory the hook runs in has a control character (a newline) in it, so NOTHING was scanned for secrets." >&2
    echo "  Unset them, or opt out for this commit with GGSHIELD_SKIP=1." >&2
    return 1
  fi
  up=
  d=$here
  while [ -n "$d" ]; do
    if [ -e "$d/.git" ]; then up=$d; break; fi
    d=${d%/*}
  done
  if [ -n "$up" ] && [ -n "${GIT_WORK_TREE+set}" ]; then
    want=
    if [ -n "$GIT_WORK_TREE" ] && physdir "$GIT_WORK_TREE"; then want=$PHYS; fi
    [ -n "$want" ] && [ "$want" = "$up" ] || up=
  fi
  if [ -n "$up" ] && [ -n "${GIT_DIR+set}" ]; then
    gd=
    if [ -d "$up/.git" ]; then
      gd=$up/.git
    elif [ -f "$up/.git" ]; then
      line=
      IFS= read -r -d '' line <"$up/.git" || :
      line=${line%$'\n'}
      if [[ $line == *[[:cntrl:]]* ]]; then
        echo "$SELF: REFUSED — the .git file of the tree holds a control character (a newline) beyond its one line, so NOTHING was scanned for secrets." >&2
        return 1
      fi
      case "$line" in
        "gitdir: "?*) gd=${line#gitdir: } ;;
      esac
      case "$gd" in
        "" | /*) ;;
        *) gd=$up/$gd ;;
      esac
    fi
    if [ -n "$gd" ] && physdir "$gd"; then gd=$PHYS; else gd=; fi
    want=
    if [ -n "$GIT_DIR" ] && physdir "$GIT_DIR"; then want=$PHYS; fi
    [ -n "$gd" ] && [ -n "$want" ] && [ "$want" = "$gd" ] || { up=; apart=1; }
  fi
  [ -n "$up" ] && return 0
  [ -z "${GIT_DIR+set}" ] || apart=1
  echo "$SELF: REFUSED — GIT_DIR or GIT_WORK_TREE is set and does not name the repository this hook runs in, so NOTHING was scanned for secrets." >&2
  if [ -n "$apart" ]; then
    echo "  A git directory kept apart from its work tree (GIT_DIR as --git-dir and --work-tree give it, or a bare repository) is not supported by this gate:" >&2
    echo "  run from the top of the tree whose own .git is the git directory, or opt out for this commit with GGSHIELD_SKIP=1." >&2
  else
    echo "  Unset them, or run from the top of the tree, or opt out for this commit with GGSHIELD_SKIP=1." >&2
  fi
  return 1
}

# ggit: git for the gate's own reads of the repository, with core.fsmonitor off (an inherited setting of
# it would run a program inside write-tree and diff-index), and with replace refs and grafts ignored. A
# filter driver's program also runs in write-tree: settings_proven refuses those before it is called,
# except git-lfs's own stock one (its stock program may run there; filters_proven and pointers_proven then refuse
# what it would have changed).
GGIT_OPTS=(-c core.fsmonitor=false -c core.commitGraph=false)
ggit() { GIT_NO_REPLACE_OBJECTS=1 GIT_GRAFT_FILE=/dev/null git "${GGIT_OPTS[@]}" "$@"; }

# glist <out file> <what> <git arguments...>: ggit with its output to <out file>, stopped after LIST_TIMEOUT
# seconds (120; the environment variable GGSHIELD_LIST_TIMEOUT can lower it, never raise it). A listing of
# the change that does not finish (a tree too large to walk) refuses, naming <what>, instead of holding the
# commit for good; one git cannot make refuses as before. The bound needs no `timeout` program: git runs
# in the background, a watchdog subshell sleeps the limit, marks that it fired and stops git (TERM, then
# KILL 5 s later), and any listing the watchdog fired on refuses, even one that finished as it fired. The
# watchdog holds none of the gate's output, and is stopped and reaped when git ends (glist_stop); a stopped
# watchdog stops its own sleep, also one it had started but not yet named. A gate that ends while a listing
# runs (Ctrl-C, or TERM to the gate: a background child of a non-interactive bash ignores SIGINT and
# SIGQUIT, so git would walk on) stops git and the watchdog too, from the EXIT trap (glist_stop).
# Three limits, all from the watchdog being its own process, which signals git by its process id, not
# through the gate's job table. (1) If git ends at the very moment the limit runs out, the gate can reap it,
# and the system give its id to another process, in the moment before the gate stops the watchdog; that
# process would get the TERM (and 5 s later the KILL, but only if the watchdog is still running then, and
# the gate stops it as soon as git is reaped). (2) A gate killed with SIGKILL runs no EXIT trap: its
# watchdog is left, and up to the limit plus 5 s later TERMs and KILLs git's id, which by then may name
# another process. (3) The same happens, on bash 5.2, when a TERM ends the gate just after the watchdog's
# fork and before bash records it as a job (see glist_stop): the trap cannot reach that watchdog.
LIST_TIMEOUT=120
# shellcheck disable=SC2034  # set by gate_pid, read by the trap string
GATE_PID=
# The id of the process that runs this: field 1 of /proc/self/stat, read by the builtin `read` in that very
# process. Not BASHPID: on bash 5.2 a subshell TERMed just after its fork runs its EXIT trap with a stale
# BASHPID (the parent's), so a guard on it passed in the watchdog. Where there is no /proc, BASHPID or $$,
# and there bash 5.2 can still lose the scratch under a live gate: a spurious refusal, never a wrong pass.
gate_pid() { if [ -r /proc/self/stat ]; then read -r "$1" _ </proc/self/stat; else printf -v "$1" %s "${BASHPID:-$$}"; fi; }
# shellcheck disable=SC2034  # set by gate_pid in the trap string
GATE_TRAP_ME=
GATE_EXIT_TRAP='gate_pid GATE_TRAP_ME; if [ "$GATE_TRAP_ME" = "$GATE_PID" ]; then glist_stop; rm -rf "$SCRATCH"; fi'
GLIST_DOG=
GLIST_ACTIVE=
glist() {
  local out=$1 what=$2 secs=$LIST_TIMEOUT rc gpid fired="$SCRATCH/glist-fired"
  shift 2
  if [ -n "${GGSHIELD_LIST_TIMEOUT-}" ]; then
    [[ $GGSHIELD_LIST_TIMEOUT =~ ^[0-9]{1,6}$ ]] && [ "$((10#$GGSHIELD_LIST_TIMEOUT))" -ge 1 ] \
      || { could_not_scan "GGSHIELD_LIST_TIMEOUT is not a whole number of seconds above 0"; return 1; }
    [ "$((10#$GGSHIELD_LIST_TIMEOUT))" -lt "$secs" ] && secs=$((10#$GGSHIELD_LIST_TIMEOUT))
  fi
  rm -f -- "$fired" || { could_not_scan "no temporary file"; return 1; }
  # Set before the fork and cleared once glist_stop has run: the EXIT trap lists jobs only while it is set,
  # so a gate that ends outside a listing (its scratch directory already removed) runs no `jobs -l` at all.
  GLIST_ACTIVE=1
  GIT_NO_REPLACE_OBJECTS=1 GIT_GRAFT_FILE=/dev/null git "${GGIT_OPTS[@]}" "$@" </dev/null >"$out" 2>/dev/null &
  gpid=$!
  # The watchdog's TERM trap only marks that it was stopped and stops a sleep it has named; after each sleep
  # is started and named the mark is read again, so a TERM between the two leaves no sleep behind. Its sleep
  # is stopped with KILL, which nothing catches: with TERM a `sleep 5` was seen left running, most likely a
  # TERM that reached the child after bash forked it and before it ran sleep, while it still held the trap.
  (
    s='' stop=''
    trap 'stop=1; [ -z "$s" ] || kill -KILL "$s" 2>/dev/null' TERM
    sleep "$secs" & s=$!
    [ -z "$stop" ] || { kill -KILL "$s" 2>/dev/null; exit 0; }
    wait "$s"; s=''
    [ -z "$stop" ] || exit 0
    : >"$fired"
    kill -TERM "$gpid" 2>/dev/null
    sleep 5 & s=$!
    [ -z "$stop" ] || { kill -KILL "$s" 2>/dev/null; exit 0; }
    wait "$s"; s=''
    [ -z "$stop" ] || exit 0
    kill -KILL "$gpid" 2>/dev/null
  ) </dev/null >/dev/null 2>&1 &
  GLIST_DOG=$!
  wait "$gpid"; rc=$?
  glist_stop || { GLIST_ACTIVE=; could_not_scan "the gate could not list its own background jobs to stop them"; return 1; }
  GLIST_ACTIVE=
  if [ -e "$fired" ]; then
    could_not_scan "git's listing of $what did not finish within $secs s (a tree too large to walk)"
  elif [ "$rc" = 0 ]; then
    return 0
  else
    could_not_scan "git could not list $what"
  fi
  return 1
}
# glist_stop: while a listing is in progress (GLIST_ACTIVE), TERM every background job of the gate, then
# reap glist's watchdog; otherwise nothing. Every background job the gate starts is glist's (its git and its
# watchdog), and a job is normally in bash's table from the fork, before `$!` is read, so this also stops a
# git or a watchdog whose id was never stored (a signal that ended the gate just after the fork). Not
# always: on bash 5.2 a TERM that lands in the parent just after the fork, before bash records the job, can
# run the EXIT trap first. After git's fork, git runs to the end of its listing and exits, unsignalled,
# and no watchdog has started; after the watchdog's fork, that watchdog is left (limit (3) above). Called when git has ended, it stops the watchdog; from
# the EXIT trap, whatever runs. Jobs are signalled through bash's job table (`kill %<job>`), never by a bare
# process id: bash signals a job's process only while it has not reaped it, so an id the system has since
# given to another process is never signalled. If `jobs -l` cannot be read it still TERMs jobs %1 to %8
# (a gate starts at most six: a git and a watchdog for each of its three listings) and returns 1, so glist
# refuses.
glist_stop() {
  local spec rest n st=0
  [ -n "$GLIST_ACTIVE" ] || return 0
  if jobs -l 2>/dev/null >"$SCRATCH/glist-jobs"; then
    # A line is "[<n>]<mark> <pid> <state> <command>"; a command's further lines start with no "[<n>]".
    while read -r spec rest; do
      [[ $spec =~ ^\[([0-9]+)\] ]] || continue
      kill -TERM "%${BASH_REMATCH[1]}" 2>/dev/null
    done <"$SCRATCH/glist-jobs"
  else
    st=1
    for n in 1 2 3 4 5 6 7 8; do kill -TERM "%$n" 2>/dev/null; done
  fi
  if [ -n "$GLIST_DOG" ]; then
    wait "$GLIST_DOG" 2>/dev/null
    GLIST_DOG=
  fi
  return "$st"
}

# The settings ggshield's git runs with, as one GIT_CONFIG_COUNT group (command-line scope, so they
# outrank every config file). Each changed or hid ggshield's patch when set otherwise, or could run a
# program in it (measured against its own git show call; see the header).
SCAN_PINS=(core.fsmonitor=false core.bigFileThreshold=512m color.ui=never color.diff=never
  format.pretty=medium diff.noprefix=false diff.mnemonicPrefix=false diff.srcPrefix=a/ diff.dstPrefix=b/
  diff.relative=false i18n.logOutputEncoding=UTF-8 log.showSignature=false advice.graftFileDeprecated=false
  log.showRoot=true core.commitGraph=false)

# scan_env: sets, in the current shell (always a subshell), the environment ggshield and its git get. The
# settings check and the scan call both use it, so the check reads what the scan's git reads.
scan_env() {
  local i=0 kv
  unset GITGUARDIAN_INSTANCE GITGUARDIAN_API_URL BASH_ENV ENV
  unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE PYTHONINSPECT GIT_CONFIG_PARAMETERS
  unset GG_GIT_CONFIG GIT_ATTR_SOURCE GIT_EXTERNAL_DIFF GIT_CONFIG GIT_DIFF_OPTS GIT_SHALLOW_FILE
  unset GIT_GLOB_PATHSPECS GIT_NOGLOB_PATHSPECS GIT_ICASE_PATHSPECS
  # Literal pathspecs: ggshield hands the staged paths to git show, and a path is never to be read as magic
  # (":(exclude)x"). ggshield 1.56.1 dies on a staged path starting with ":" before that call (measured), so
  # this is a second wall. Notes are not
  # shown: both variables empty show none, whatever core.notesRef, notes.displayRef or a notes ref holds
  # (measured; a named ref was shown, so any ref name is not enough), and nothing of them belongs in the scan.
  export GITGUARDIAN_DONT_LOAD_ENV=1 GIT_NO_REPLACE_OBJECTS=1 GIT_GRAFT_FILE=/dev/null GIT_LITERAL_PATHSPECS=1
  export GIT_NOTES_REF='' GIT_NOTES_DISPLAY_REF=''
  for kv in "${SCAN_PINS[@]}"; do
    export "GIT_CONFIG_KEY_$i=${kv%%=*}" "GIT_CONFIG_VALUE_$i=${kv#*=}"
    i=$((i + 1))
  done
  export GIT_CONFIG_COUNT=$i
}

# scangit: git as ggshield 1.56.1 runs it (measured): its two -c options, no system config, and
# GG_GIT_CONFIG (unset by scan_env) or nothing as the global config. Only inside a scan_env subshell.
scangit() { GIT_CONFIG_SYSTEM='' GIT_CONFIG_GLOBAL="${GG_GIT_CONFIG-}" git -c core.quotePath=false -c safe.directory='*' "$@"; }

# settings_proven: refuses (saying which key, never its value) when git, as the gate's git or as
# ggshield's reads it, has a setting that runs a program on content or hides it: diff.<x>.textconv,
# .command or .binary, filter.<x>.clean, .smudge or .process, or attr.tree. Both readings are needed:
# the gate's reads system, global and XDG config, which ggshield's does not, and ggshield's trusts
# every directory (safe.directory=*), where the gate's would skip the repository's own config.
settings_proven() {
  local view f rc rec key found="" want val
  for view in gate scan; do
    f=$SCRATCH/settings-$view
    if [ "$view" = gate ]; then
      ggit config --includes -z --get-regexp '^(diff|filter|attr|lfs\.extension)\.' >"$f" 2>/dev/null </dev/null
      rc=$?
    else
      (scan_env; scangit config --includes -z --get-regexp '^(diff|filter|attr|lfs\.extension)\.' >"$f" 2>/dev/null </dev/null)
      rc=$?
    fi
    # 0 is some found, 1 with nothing printed is none; anything else is not knowing.
    case "$rc" in
      0) ;;
      1) [ ! -s "$f" ] || { could_not_scan "git's settings could not be read"; return 1; } ;;
      *) could_not_scan "git's settings could not be read (exit $rc)"; return 1 ;;
    esac
    while IFS= read -r -d '' rec; do
      key=${rec%%$'\n'*}
      key=${key//[[:cntrl:]]/?}
      case "$key" in
        filter.lfs.clean | filter.lfs.smudge | filter.lfs.process | filter.lfs.required)
          # git-lfs's stock values, exactly (an identical value repeated across files is the same value; a
          # differing one refuses): the one filter driver let through. filters_proven then refuses any changed
          # path with a filter attribute, and pointers_proven any changed path holding an LFS pointer.
          case "$key" in
            filter.lfs.clean) want='git-lfs clean -- %f' ;;
            filter.lfs.smudge) want='git-lfs smudge -- %f' ;;
            filter.lfs.process) want='git-lfs filter-process' ;;
            *) want=true ;;
          esac
          val=
          case "$rec" in *$'\n'*) val=${rec#*$'\n'} ;; esac
          if [ "$val" = "$want" ]; then
            :
          else
            case " $found " in
              *" $key "*) ;;
              *) found="$found $key" ;;
            esac
          fi
          ;;
        diff.?*.textconv | diff.?*.command | diff.?*.binary | filter.?*.clean | filter.?*.smudge | filter.?*.process | attr.tree)
          case " $found " in
            *" $key "*) ;;
            *) found="$found $key" ;;
          esac
          ;;
        lfs.extension.?* | filter.lfs.?*)
          # lfs.extension.<x>.* names programs git-lfs runs during clean; any other key under the lfs driver
          # (filter.lfs.foo) is not the stock set
          case " $found " in
            *" $key "*) ;;
            *) found="$found $key" ;;
          esac
          ;;
      esac
    done <"$f"
  done
  [ -n "$found" ] || return 0
  echo "$SELF: REFUSED — git is set to run a program on file content or to hide it from the scan (found:$found), so NOTHING was scanned for secrets." >&2
  echo "  The gate refuses any diff.<driver>.textconv, .command or .binary, filter.<driver>.clean, .smudge or .process, and attr.tree," >&2
  echo "  except git-lfs's own filter.lfs.clean, .smudge, .process and .required at exactly their stock values, and any lfs.extension.<x>.*," >&2
  echo "  wherever it is set and whether or not an attribute uses it today: attributes can come from a staged .gitattributes," >&2
  echo "  info/attributes, core.attributesFile or the XDG attributes file. Remove the setting (git-lfs's stock filter.lfs.* values are accepted, a changed one is not)," >&2
  echo "  or on your own machine opt out for this commit with GGSHIELD_SKIP=1." >&2
  return 1
}

# shallow_proven <commit>: refuses when the shallow list names the commit to scan (GIT_SHALLOW_FILE, which
# names another list, is unset for every git call). git takes a commit
# listed there as having no parent, and ggshield's git would then show its whole tree, or (with
# log.showRoot off) nothing at all (measured). The commit is new, so no honest list names it; its id can be
# predicted, so a list can. Bash builtins only; the list is where git names it.
shallow_proven() {
  local f line
  f=$(ggit rev-parse --git-path shallow 2>/dev/null; printf x)
  f=${f%x}
  f=${f%$'\n'}
  [ -n "$f" ] && [[ $f != *[[:cntrl:]]* ]] || { could_not_scan "git could not name its shallow list"; return 1; }
  [ -e "$f" ] || return 0
  [ -f "$f" ] && [ -r "$f" ] || { could_not_scan "the shallow list cannot be read"; return 1; }
  # git reads a line by its leading hex digits, in either case, and ignores what follows them (measured:
  # upper case, a CR, a space or other text after the id all counted), so the line is read the same way.
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line:0:${#1}}
    # In lower case for the id's hex letters (bash 3.2, macOS's /bin/bash, has no ${line,,}); the id is
    # lower-case hex, so no other letter can match either way.
    line=${line//A/a}; line=${line//B/b}; line=${line//C/c}
    line=${line//D/d}; line=${line//E/e}; line=${line//F/f}
    if [ "$line" = "$1" ]; then
      echo "$SELF: REFUSED — the shallow list ($f) names the commit made for the scan, so git would not show its change as it is and NOTHING was scanned for secrets." >&2
      echo "  Take that line out, or on your own machine opt out for this commit with GGSHIELD_SKIP=1." >&2
      return 1
    fi
  done <"$f"
  return 0
}

# objects_proven <commit>: refuses unless the commit object, read as git reads it, hashes to its own id.
# git show does not check an object's content against its id, and the id of the commit made for the scan
# can be predicted, so an object planted under that id (loose, or in an alternate store) is read instead of
# the one written, with any tree or parent (measured: one naming HEAD's tree emptied the patch). Hashed with
# --literally, which only hashes (without it git also judges the format). HEAD's side is checked by
# base_proven. A commit-graph is not read (core.commitGraph=false, for the gate's git and the scan's): a
# planted one gave the commit another tree the same way (measured).
objects_proven() {
  local got
  got=$(ggit cat-file commit "$1" </dev/null 2>/dev/null | ggit hash-object --literally -t commit --stdin 2>/dev/null)
  if [ "$got" != "$1" ]; then
    echo "$SELF: REFUSED — the object stored for commit $1 does not hash to that id, so git would not show the staged change as it is and NOTHING was scanned for secrets." >&2
    echo "  Check the repository (git fsck), or on your own machine opt out for this commit with GGSHIELD_SKIP=1." >&2
    return 1
  fi
  return 0
}

# hashed <list> <out>: every object in <list> (lines "<type> <id>"), read as git reads it, hashes to its id
# and has that type. One `cat-file --batch` reads them all and one python3 hashes what it prints (sha1 or
# sha256, by the id's length). A missing object, another type, a short or overlong answer, or a cat-file that
# fails is a mismatch. On a mismatch the first id that failed is written to <out> (empty when the answer as a
# whole could not be read) and it returns 1. $py is run_gate's python3, proven outside the tree.
hashed() {
  local list=$1 res=$2 st id
  : >"$res" || return 1
  [ -r "$list" ] || return 1
  # The ids alone, one per line, read with builtins bash 3.2 has (macOS's /bin/bash: no mapfile).
  while read -r _ id; do printf '%s\n' "$id"; done <"$list" \
    | ggit cat-file --batch 2>/dev/null | "$py" -I -c '
import hashlib, sys
want = [line.split() for line in open(sys.argv[1], encoding="ascii")]
f = sys.stdin.buffer
def fail(oid=""):
    open(sys.argv[2], "w").write(oid)
    sys.exit(1)
for kind, oid in want:
    head = f.readline()
    parts = head[:-1].split(b" ") if head.endswith(b"\n") else []
    if len(parts) != 3 or parts[0] != oid.encode() or parts[1] != kind.encode() or not parts[2].isdigit():
        fail(oid)
    size = int(parts[2])
    if len(oid) not in (40, 64):
        fail(oid)
    h = hashlib.new("sha1" if len(oid) == 40 else "sha256")
    h.update(b"%s %d\0" % (kind.encode(), size))
    left = size
    while left:
        chunk = f.read(min(left, 1 << 20))
        if not chunk:
            fail(oid)
        h.update(chunk)
        left -= len(chunk)
    if f.read(1) != b"\n" or h.hexdigest() != oid:
        fail(oid)
if f.read(1):
    fail()
' "$list" "$res"
  st=("${PIPESTATUS[@]}")
  [ "${st[*]}" = "0 0 0" ]
}

# base_proven <HEAD's commit> <the staged tree>: refuses unless every object on HEAD's side of the
# change, read as git reads it, hashes to its own id: HEAD's commit, its tree, and each tree and blob
# the comparison reads on that side (diff-tree -t lists every tree it descends into). git does not check a
# tree's or a blob's id when it compares them, so an object planted loose (or in an alternate store) under
# HEAD's id for a file or directory, holding the staged content, made that part of the change look
# unchanged and the patch empty (measured with the stub, which reads as ggshield 1.56.1 does). A file
# added has no old side (the null id), and a submodule's id names no object here: neither is hashed.
# HEAD's commit and tree are hashed before the listing, the objects it names in one pass after it.
base_proven() {
  local head=$1 tree=$2 id bad prc
  local refuse="$SELF: REFUSED — an object on HEAD's side of the change does not hash to its id"
  printf 'commit %s\n' "$head" >"$SCRATCH/base-ids"
  hashed "$SCRATCH/base-ids" "$SCRATCH/base-bad" || { echo "$refuse (HEAD's commit $head), so git would not show the staged change as it is and NOTHING was scanned for secrets." >&2; base_advice; return 1; }
  id=$(ggit rev-parse -q --verify "$head^{tree}" 2>/dev/null) && [ ${#id} = ${#tree} ] \
    || { could_not_scan "git could not name HEAD's tree"; return 1; }
  printf 'tree %s\n' "$id" >"$SCRATCH/base-ids"
  hashed "$SCRATCH/base-ids" "$SCRATCH/base-bad" || { echo "$refuse (HEAD's tree $id), so git would not show the staged change as it is and NOTHING was scanned for secrets." >&2; base_advice; return 1; }
  glist "$SCRATCH/base-raw" "the objects HEAD's side of the change is read from" \
    diff-tree -r -t --no-renames --no-abbrev --raw -z "$head" "$tree" || return 1
  # A record is ":<old mode> <new mode> <old id> <new id> <status>" and then the path, each ended by NUL.
  # One python3 reads them all and writes "<type> <id>" for each old side to hash; it answers a word when
  # the listing cannot be read.
  bad=$("$py" -I -c '
import re, sys
parts = open(sys.argv[1], "rb").read().split(b"\0")
if parts[-1] != b"":
    sys.exit("form")
parts.pop()
if len(parts) % 2:
    sys.exit("nopath")
out = []
for meta in parts[0::2]:
    m = re.fullmatch(rb":([0-7]{6}) [0-7]{6} ([0-9a-f]+) [0-9a-f]+ [A-Z]", meta)
    if not m:
        sys.exit("form")
    mode, oid = m.groups()
    if len(oid) != int(sys.argv[3]):
        sys.exit("length")
    if oid.strip(b"0") == b"" or mode in (b"000000", b"160000"):
        continue
    out.append(("tree " if mode == b"040000" else "blob ") + oid.decode() + "\n")
open(sys.argv[2], "w").write("".join(out))
' "$SCRATCH/base-raw" "$SCRATCH/base-ids" "${#tree}" 2>&1 </dev/null)
  prc=$?
  case "$prc:$bad" in
    0:) ;;
    *:nopath) could_not_scan "git listed an object of HEAD's side without its path"; return 1 ;;
    *:length) could_not_scan "git listed an object id of HEAD's side of the wrong length"; return 1 ;;
    *:form) could_not_scan "git listed HEAD's side of the change in a form the gate cannot read (parser exited $prc: form)"; return 1 ;;
    *)
      # Name the cause, not only that there was one: the parser's last output line (a traceback's last line
      # names the error, e.g. FileNotFoundError), without control characters, at most 200 characters.
      local said=empty
      [ -n "$bad" ] && said=non-empty
      bad=${bad##*$'\n'}
      bad=${bad//[^[:print:]]/}
      could_not_scan "the listing of HEAD's side of the change could not be parsed (parser exited $prc, its output $said${bad:+: ${bad:0:200}})"
      return 1
      ;;
  esac
  [ -s "$SCRATCH/base-ids" ] || return 0
  if ! hashed "$SCRATCH/base-ids" "$SCRATCH/base-bad"; then
    bad=$(<"$SCRATCH/base-bad")
    # Named once: ${bad:-...} alone would give the id again after "object $bad", since it expands to $bad
    # whenever $bad is set.
    if [ -n "$bad" ]; then bad="object $bad"; else bad="an object git could not read in full"; fi
    echo "$refuse ($bad), so git would not show the staged change as it is and NOTHING was scanned for secrets." >&2
    base_advice
    return 1
  fi
  return 0
}
base_advice() {
  echo "  Check the repository (git fsck), or on your own machine opt out for this commit with GGSHIELD_SKIP=1." >&2
}

# attrs_proven <file of the changed paths, -z, from the top> <the way from here to the top>: refuses when
# the diff attribute is unset (-diff, or the binary macro) for a changed path, as either git reads it:
# git show prints such a file as "Binary files differ" and its content never reaches the scan. Every path
# must be answered for, or the check did not happen.
attrs_proven() {
  local view f rc p a v n=0 got hidden=0
  while IFS= read -r -d '' p; do
    printf '%s%s\0' "$2" "$p"
    n=$((n + 1))
  done <"$1" >"$SCRATCH/attr-paths"
  [ "$n" -gt 0 ] || return 0
  for view in gate scan; do
    f=$SCRATCH/attrs-$view
    if [ "$view" = gate ]; then
      ggit check-attr -z --stdin diff <"$SCRATCH/attr-paths" >"$f" 2>/dev/null
      rc=$?
    else
      (scan_env; scangit check-attr -z --stdin diff <"$SCRATCH/attr-paths" >"$f" 2>/dev/null)
      rc=$?
    fi
    [ "$rc" = 0 ] || { could_not_scan "git could not read the attributes of the staged files (exit $rc)"; return 1; }
    got=0
    while IFS= read -r -d '' p && IFS= read -r -d '' a && IFS= read -r -d '' v; do
      [ "$a" = diff ] || { could_not_scan "git answered for an attribute it was not asked about"; return 1; }
      got=$((got + 1))
      [ "$v" != unset ] || hidden=$((hidden + 1))
    done <"$f"
    [ "$got" = "$n" ] || { could_not_scan "git answered for $got of the $n staged files' attributes"; return 1; }
  done
  [ "$hidden" -gt 0 ] || return 0
  echo "$SELF: REFUSED — git is told to show a staged file as binary (its diff attribute is unset: -diff or binary), so its content would not reach the scan and NOTHING was scanned for secrets." >&2
  echo "  Take the attribute off the changed files (a .gitattributes, info/attributes, core.attributesFile or the XDG attributes file)," >&2
  echo "  or on your own machine opt out for this commit with GGSHIELD_SKIP=1." >&2
  return 1
}

# filters_proven <file of the changed paths, -z, from the top>: refuses when
# a changed path has a filter attribute set to anything (git-lfs's filter.lfs.* settings are let through, so
# no filter may name a driver). Read with check-attr as either git reads it, from the working tree and from
# the index only (--cached, so a staged .gitattributes counts). Every path must be answered for.
filters_proven() {
  local view mode f rc p a v n=0 got set=0
  while IFS= read -r -d '' p; do
    n=$((n + 1))
  done <"$1"
  [ "$n" -gt 0 ] || return 0
  for view in gate scan; do
    for mode in tree index; do
      f=$SCRATCH/filters-$view-$mode
      if [ "$view" = gate ]; then
        if [ "$mode" = tree ]; then
          ggit check-attr -z --stdin filter <"$SCRATCH/attr-paths" >"$f" 2>/dev/null
        else
          ggit check-attr --cached -z --stdin filter <"$SCRATCH/attr-paths" >"$f" 2>/dev/null
        fi
        rc=$?
      else
        if [ "$mode" = tree ]; then
          (scan_env; scangit check-attr -z --stdin filter <"$SCRATCH/attr-paths" >"$f" 2>/dev/null)
        else
          (scan_env; scangit check-attr --cached -z --stdin filter <"$SCRATCH/attr-paths" >"$f" 2>/dev/null)
        fi
        rc=$?
      fi
      [ "$rc" = 0 ] || { could_not_scan "git could not read the filter attributes of the staged files (exit $rc)"; return 1; }
      got=0
      while IFS= read -r -d '' p && IFS= read -r -d '' a && IFS= read -r -d '' v; do
        [ "$a" = filter ] || { could_not_scan "git answered for an attribute it was not asked about"; return 1; }
        got=$((got + 1))
        [ "$v" = unspecified ] || set=$((set + 1))
      done <"$f"
      [ "$got" = "$n" ] || { could_not_scan "git answered for $got of the $n staged files' filter attributes"; return 1; }
    done
  done
  [ "$set" -gt 0 ] || return 0
  echo "$SELF: REFUSED — a staged file has a filter attribute, which would run a filter driver's program, so NOTHING was scanned for secrets." >&2
  echo "  Take the attribute off the changed files (a .gitattributes, info/attributes, core.attributesFile or the XDG attributes file)," >&2
  echo "  or on your own machine opt out for this commit with GGSHIELD_SKIP=1." >&2
  return 1
}

# pointers_proven <base tree> <tree to scan>: refuses when a changed path's new blob is a git-lfs pointer
# (1024 bytes or fewer; read as git-lfs's decoder reads it: the blob's leading and trailing whitespace
# trimmed, a CR at a line's end cut off, and the first line exactly `version https://git-lfs.github.com/spec/v1`
# or one of the legacy forms `version https://hawser.github.com/spec/v1`, `version http://git-media.io/v/2`). A pointer is what git-lfs's clean filter stages: the content
# sits in .git/lfs/objects, never reaches the scan, and is uploaded by `git lfs pre-push`. The attribute that
# made it can be gone by commit time (git lfs untrack), so the blob itself is read, from the tree being
# scanned, with cat-file (it runs no filter). The blobs are named by oid, so they cannot change under the
# scan and are not read again after it.
pointers_proven() {
  local raw=$SCRATCH/ptr-raw oids=$SCRATCH/ptr-oids sizes=$SCRATCH/ptr-sizes small=$SCRATCH/ptr-small
  local blobs=$SCRATCH/ptr-blobs found
  glist "$raw" "the changed blobs of the commit to scan" \
    diff-tree -r --no-renames --raw -z --no-abbrev "$1" "$2" || return 1
  cat >"$SCRATCH/ptr.py" <<'PTRPY'
import sys
mode, src, dst = sys.argv[1:4]
ptr_lines = ("version https://git-lfs.github.com/spec/v1", "version https://hawser.github.com/spec/v1",
             "version http://git-media.io/v/2")
data = open(src, "rb").read()
if mode == "oids":
    parts = data.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    if len(parts) % 2:
        sys.exit(2)
    out = []
    for meta in parts[0::2]:
        f = meta.split(b" ")
        if len(f) != 5 or not f[0].startswith(b":"):
            sys.exit(2)
        if f[4][:1] == b"D" or f[1] == b"160000":
            continue
        out.append(f[3])
    open(dst, "wb").write(b"".join(o + b"\n" for o in sorted(set(out))))
elif mode == "small":
    oids = open(sys.argv[4], "rb").read().split()
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if len(lines) != len(oids):
        sys.exit(2)
    out = []
    for o, l in zip(oids, lines):
        f = l.split(b" ")
        if len(f) != 3 or f[0] != o or f[1] != b"blob" or not f[2].isdigit():
            sys.exit(2)
        if int(f[2]) <= 1024:
            out.append(o)
    open(dst, "wb").write(b"".join(o + b"\n" for o in out))
elif mode == "count":
    want = open(sys.argv[4], "rb").read().split()
    pos, found, seen = 0, 0, 0
    while pos < len(data):
        nl = data.index(b"\n", pos)
        f = data[pos:nl].split(b" ")
        if len(f) != 3 or f[1] != b"blob" or not f[2].isdigit():
            sys.exit(2)
        size = int(f[2])
        body = data[nl + 1:nl + 1 + size]
        if len(body) != size or data[nl + 1 + size:nl + 2 + size] != b"\n":
            sys.exit(2)
        pos = nl + 2 + size
        seen += 1
        # git-lfs's decoder trims the whole blob's whitespace (Unicode's too, as Go does) first, then reads lines with any CR at the end cut off.
        if body.decode("utf-8", "replace").strip().split("\n", 1)[0].rstrip("\r") in ptr_lines:
            found += 1
    if seen != len(want):
        sys.exit(2)
    open(dst, "w").write("%d\n" % found)
PTRPY
  "$py" -I "$SCRATCH/ptr.py" oids "$raw" "$oids" || { could_not_scan "the changed blobs could not be listed"; return 1; }
  [ -s "$oids" ] || return 0
  ggit cat-file --batch-check <"$oids" >"$sizes" 2>/dev/null || { could_not_scan "git could not read the sizes of the changed blobs"; return 1; }
  "$py" -I "$SCRATCH/ptr.py" small "$sizes" "$small" "$oids" || { could_not_scan "git's answer about the changed blobs could not be read"; return 1; }
  [ -s "$small" ] || return 0
  ggit cat-file --batch <"$small" >"$blobs" 2>/dev/null || { could_not_scan "git could not read the small changed blobs"; return 1; }
  "$py" -I "$SCRATCH/ptr.py" count "$blobs" "$SCRATCH/ptr-count" "$small" || { could_not_scan "the small changed blobs could not be read"; return 1; }
  found=$(cat "$SCRATCH/ptr-count" 2>/dev/null) || found=
  case "$found" in
    *[!0-9]* | "") could_not_scan "the count of LFS pointers among the changed blobs is unknown"; return 1 ;;
  esac
  [ "$found" -gt 0 ] || return 0
  echo "$SELF: REFUSED — $found changed file(s) are git-lfs pointers, so their real content never reaches the scan and NOTHING of it was scanned for secrets." >&2
  echo "  The content sits in .git/lfs/objects and would be uploaded by git lfs pre-push. Stage the file without the lfs filter," >&2
  echo "  or on your own machine opt out for this commit with GGSHIELD_SKIP=1." >&2
  return 1
}

# outside_tree <program path> <tree top, or empty>: false when the program resolves inside the tree.
outside_tree() {
  local real top
  real=$(readlink -f -- "$1" 2>/dev/null || printf '%s' "$1")
  [ -n "$2" ] || return 0
  top=$(readlink -f -- "$2" 2>/dev/null || printf '%s' "$2")
  case "$real" in
    "$top"/*) return 1 ;;
  esac
  return 0
}

could_not_scan() { # could_not_scan <why>
  echo "$SELF: REFUSED — $1, so the staged change was NOT scanned for secrets." >&2
  echo "  Fix the cause, or on your own machine opt out for this commit with GGSHIELD_SKIP=1." >&2
  return 1
}

leak_policy() {
  echo "Remove it from the change. If it is a real credential that reached a file," >&2
  echo "AGENTS.md's leak policy applies: vault it, redact every copy on this machine," >&2
  echo "and rotate it only if it left the homelab." >&2
}

run_gate() {
  if [ "${GGSHIELD_SKIP:-0}" = "1" ]; then
    local v
    for v in CI FORGEJO_ACTIONS GITHUB_ACTIONS; do
      if [ -n "${!v+set}" ]; then
        echo "$SELF: REFUSED — GGSHIELD_SKIP=1 is a switch for a person's own machine and $v is set here." >&2
        echo "  NOTHING was scanned for secrets, and an automated run may not skip the scan." >&2
        return 1
      fi
    done
    echo "$SELF: SKIP (GGSHIELD_SKIP=1) — NOTHING was scanned for secrets"
    return 0
  fi
  # Before any program is run: the git variables that name another repository, and PATH itself.
  git_env_proven || return 1
  path_proven || return 1
  # An inherited GIT_CONFIG_COUNT group would set configuration for every git run from here, ggshield's
  # included; it is dropped. GIT_CONFIG_PARAMETERS, which `git -c` sets for a hook, is kept for the gate's
  # own git calls (their core.fsmonitor=false comes after it) and dropped for ggshield's.
  local cv
  for cv in "${!GIT_CONFIG_KEY_@}" "${!GIT_CONFIG_VALUE_@}"; do unset "$cv"; done
  unset GIT_CONFIG_COUNT
  # GIT_ATTR_SOURCE reads attributes from a tree of its choosing (a -diff there hid the change); dropped for
  # every git run from here, so the attribute check and the scan read the same attributes. GIT_CONFIG makes
  # `git config` read that one file and nothing else, while show and write-tree ignore it: set, it would
  # blind the settings check (measured); dropped for every git run from here. GIT_SHALLOW_FILE names a
  # shallow list other than the repository's; dropped too, so the shallow check reads the one git reads.
  unset GIT_ATTR_SOURCE GIT_CONFIG GIT_SHALLOW_FILE
  if ! command -v ggshield >/dev/null 2>&1; then
    echo "$SELF: REFUSED — ggshield is not installed, so NOTHING was scanned for secrets." >&2
    echo "  Install it, or opt out for this commit with GGSHIELD_SKIP=1." >&2
    return 1
  fi

  local gg top py gitbin here
  physdir . || { could_not_scan "the current directory cannot be resolved"; return 1; }
  here=$PHYS
  gg=$(command -v ggshield)
  # Each program is checked against the directory the hook runs in and, once git is proven, against
  # the top git names: a git inside the tree could name any top.
  gitbin=$(command -v git 2>/dev/null) || { could_not_scan "git is not installed"; return 1; }
  if ! outside_tree "$gitbin" "$here"; then
    echo "$SELF: REFUSED — git resolves inside the repository working tree, so NOTHING was scanned." >&2
    echo "  Put git on PATH from outside the tree, or opt out for this commit with GGSHIELD_SKIP=1." >&2
    return 1
  fi
  # A sentinel keeps a trailing newline in the name git prints (command substitution drops them all), and
  # physdir reads the result physically and refuses a control character. Outside a repository top stays
  # empty: physdir "" would read the current directory.
  local rc
  top=$(ggit rev-parse --show-toplevel 2>/dev/null; printf x)
  top=${top%x}
  top=${top%$'\n'}
  if [ -n "$top" ]; then
    physdir "$top"; rc=$?
    case "$rc" in
      0) top=$PHYS ;;
      2)
        echo "$SELF: REFUSED — the top of the tree has a control character in its path, so NOTHING was scanned for secrets." >&2
        return 1
        ;;
      *) could_not_scan "the top of the tree git names cannot be resolved"; return 1 ;;
    esac
  fi
  path_proven "$top" || return 1
  if ! outside_tree "$gitbin" "$top"; then
    echo "$SELF: REFUSED — git resolves inside the repository working tree, so NOTHING was scanned." >&2
    echo "  Put git on PATH from outside the tree, or opt out for this commit with GGSHIELD_SKIP=1." >&2
    return 1
  fi
  if ! outside_tree "$gg" "$here" || ! outside_tree "$gg" "$top"; then
    echo "$SELF: REFUSED — ggshield resolves inside the repository working tree, so NOTHING was scanned." >&2
    echo "  Put ggshield on PATH from outside the tree, or opt out for this commit with GGSHIELD_SKIP=1." >&2
    return 1
  fi
  py=$(command -v python3 2>/dev/null) || { could_not_scan "there is no python3 to read ggshield's report with"; return 1; }
  if ! outside_tree "$py" "$here" || ! outside_tree "$py" "$top"; then
    echo "$SELF: REFUSED — python3 resolves inside the repository working tree, so NOTHING was scanned." >&2
    echo "  Put python3 on PATH from outside the tree, or opt out for this commit with GGSHIELD_SKIP=1." >&2
    return 1
  fi
  [ -f "$JUDGE" ] || { could_not_scan "ggshield-judge.py is not beside this script"; return 1; }

  SCRATCH=$(mktemp -d) || { could_not_scan "no temporary directory"; return 1; }
  # Removed on any exit, with glist's git and watchdog stopped first; this script sets no other EXIT trap.
  # Only by the gate's own process: a subshell forked while the trap is set (glist's watchdog) holds the trap
  # until bash resets it, and a TERM that reaches it in that window (glist_stop sends one as soon as git ends)
  # runs this trap in the subshell, which removed the scratch directory under the running gate (measured: the
  # listing file gone before the parser read it, "git listed HEAD's side ... in a form the gate cannot read").
  # BASHPID was no guard: on bash 5.2 that subshell runs the trap with a stale BASHPID equal to the gate's
  # (measured in bash 5.2.15: scratch lost in 21 of 2500 runs, 0 of 2500 with /proc/self/stat). gate_pid reads
  # the true id from /proc/self/stat; where there is no /proc it falls back to BASHPID, or $$ (always passes).
  gate_pid GATE_PID
  # shellcheck disable=SC2064  # the trap is a constant string; its variables are read when it runs
  trap "$GATE_EXIT_TRAP" EXIT
  local cfg="$SCRATCH/config" idx="$SCRATCH/index" src tree parent commit range base basetree same scanned
  : >"$cfg" || { could_not_scan "no temporary file"; return 1; }
  # Before write-tree, the first git call that could run a filter driver's program.
  settings_proven || return 1

  # The commit that stands for the staged change. write-tree runs on a copy:
  # it may rewrite the index file it reads, and other hooks run beside this one.
  src=${GIT_INDEX_FILE:-$(ggit rev-parse --git-path index 2>/dev/null)}
  # No index file where git says it is: write-tree would build the EMPTY tree and the scan would
  # cover nothing, so that is a refusal, never a clean scan.
  if [ -z "$src" ] || [ ! -f "$src" ]; then
    could_not_scan "the index file (${src:-no path}) was not found"
    return 1
  fi
  cp -- "$src" "$idx" || { could_not_scan "the index could not be copied"; return 1; }
  tree=$(GIT_INDEX_FILE="$idx" ggit write-tree 2>/dev/null) && [ -n "$tree" ] \
    || { could_not_scan "git could not write the staged tree (not a repository, or an unmerged path)"; return 1; }
  parent=$(ggit rev-parse -q --verify 'HEAD^{commit}' 2>/dev/null || true)
  commit=$(GIT_AUTHOR_NAME="$SELF" GIT_AUTHOR_EMAIL="$SELF@localhost" \
    GIT_COMMITTER_NAME="$SELF" GIT_COMMITTER_EMAIL="$SELF@localhost" \
    ggit commit-tree --no-gpg-sign ${parent:+-p "$parent"} -m "staged change, for the secret scan" "$tree" </dev/null 2>/dev/null) \
    && [ -n "$commit" ] || { could_not_scan "git could not make the commit to scan"; return 1; }
  range=${parent:+$parent..}$commit
  shallow_proven "$commit" || return 1
  objects_proven "$commit" || return 1

  # What is scanned must be exactly what is staged: the index, read where git put it, is compared with
  # the tree written from its copy. 0 is identical, 1 differs, anything else is an error; both
  # refuse. An intent-to-add entry (git add -N) is not in the index's tree and is not in the commit.
  base=${parent:-$(ggit hash-object -t tree /dev/null 2>/dev/null)}
  [ -n "$base" ] || { could_not_scan "git could not name the tree the staged change is compared with"; return 1; }
  if [ -n "$parent" ]; then base_proven "$parent" "$tree" || return 1; fi
  GIT_INDEX_FILE="$src" ggit diff-index --cached --ita-invisible-in-index --quiet "$tree" </dev/null 2>/dev/null
  same=$?
  case "$same" in
    0) ;;
    1) could_not_scan "the staged index is not exactly the tree that was scanned"; return 1 ;;
    *) could_not_scan "git could not compare the staged index with the tree to scan (exit $same)"; return 1 ;;
  esac
  # The files of that tree's change: their count for the message, their names for the attribute check.
  glist "$SCRATCH/names" "the files of the commit to scan" \
    diff-tree -r --no-renames --name-only -z "$base" "$tree" || return 1
  scanned=$(tr -cd '\0' <"$SCRATCH/names" | wc -c) || { could_not_scan "the files of the commit to scan could not be counted"; return 1; }
  case "$scanned" in
    *[!0-9]* | "") could_not_scan "the files of the commit to scan could not be counted"; return 1 ;;
  esac
  # "Nothing to scan" passes only when proven: with no file listed the tree written must be the one
  # the commit starts from (HEAD's, or the empty tree before the first commit), whatever the
  # comparisons above said.
  if [ "$scanned" -eq 0 ]; then
    basetree=$base
    if [ -n "$parent" ]; then
      basetree=$(ggit rev-parse -q --verify "$parent^{tree}" 2>/dev/null) \
        || { could_not_scan "git could not name HEAD's tree"; return 1; }
    fi
    if [ "$tree" != "$basetree" ]; then
      could_not_scan "no file is listed as changed, yet the tree to scan $tree is not HEAD's tree"
      return 1
    fi
  fi
  # The changed paths are named from the top; check-attr runs here, so they are handed over from here.
  local cdup
  cdup=$(ggit rev-parse --show-cdup 2>/dev/null; printf x)
  cdup=${cdup%x}
  cdup=${cdup%$'\n'}
  if [[ $cdup == *[[:cntrl:]]* ]] || [[ -n $cdup && $cdup != */ ]]; then
    could_not_scan "git could not say where the top of the tree is from here"
    return 1
  fi
  attrs_proven "$SCRATCH/names" "$cdup" || return 1
  filters_proven "$SCRATCH/names" || return 1
  pointers_proven "$base" "$tree" || return 1

  # No pipe: a pipeline's rc is the LAST command's. The judge keeps ggshield's
  # two outputs apart and in memory, and answers with an exit status and one
  # word; both must agree, and any other pair is not a pass.
  # --instance outranks GITGUARDIAN_INSTANCE and GITGUARDIAN_API_URL (measured);
  # both are also unset for the call.
  local word rc
  # ggshield's git runs where ggshield runs, and it hands git show the paths named from the top: from a
  # subdirectory git reads them as below it and the patch held nothing (measured with ggshield 1.56.1 and
  # GIT_TRACE: cwd the subdirectory, paths from the top). So the scan runs from the top, with GIT_DIR and
  # GIT_WORK_TREE (proven above to name this repository) made absolute first.
  word=$(
    scan_env
    if [ -n "${GIT_DIR-}" ]; then physdir "$GIT_DIR" && export GIT_DIR="$PHYS" || exit 3; fi
    if [ -n "${GIT_WORK_TREE-}" ]; then physdir "$GIT_WORK_TREE" && export GIT_WORK_TREE="$PHYS" || exit 3; fi
    cd -- "$top" || exit 3
    exec "$py" -I "$JUDGE" --label "$SELF" "$range" -- "$gg" --config-path "$cfg" --instance "$INSTANCE"
  )
  rc=$?
  # The settings, attributes, shallow list, the commit's object and HEAD's side were read before ggshield's git read them, and other
  # pre-commit jobs run beside this one (lefthook runs them in parallel). For a scan that would pass they
  # are read again now, and a change while the scan ran refuses. A change made and undone within the scan
  # is not seen: a stated limit. Any other answer refuses already, and keeps its own message.
  if [ "$rc:$word" = 0:clean ]; then
    local again
    for again in settings-gate settings-scan attrs-gate attrs-scan filters-gate-tree filters-gate-index filters-scan-tree filters-scan-index; do
      mv -f -- "$SCRATCH/$again" "$SCRATCH/$again.before" 2>/dev/null || : >"$SCRATCH/$again.before"
    done
    settings_proven || { rm -rf "$SCRATCH"; return 1; }
    attrs_proven "$SCRATCH/names" "$cdup" || { rm -rf "$SCRATCH"; return 1; }
    filters_proven "$SCRATCH/names" || { rm -rf "$SCRATCH"; return 1; }
    shallow_proven "$commit" || { rm -rf "$SCRATCH"; return 1; }
    objects_proven "$commit" || { rm -rf "$SCRATCH"; return 1; }
    if [ -n "$parent" ]; then base_proven "$parent" "$tree" || { rm -rf "$SCRATCH"; return 1; }; fi
    for again in settings-gate settings-scan attrs-gate attrs-scan filters-gate-tree filters-gate-index filters-scan-tree filters-scan-index; do
      [ -e "$SCRATCH/$again" ] || : >"$SCRATCH/$again"
      if ! cmp -s -- "$SCRATCH/$again.before" "$SCRATCH/$again"; then
        rm -rf "$SCRATCH"
        could_not_scan "git's settings or the staged files' attributes changed while the scan ran"
        return 1
      fi
    done
  fi
  rm -rf "$SCRATCH"

  case "$rc:$word" in
    0:clean)
      if [ "$scanned" -gt 0 ]; then
        echo "$SELF: $scanned staged file(s) scanned"
      else
        echo "$SELF: the staged change holds no file, so there was nothing to scan"
      fi
      echo "$SELF: clean"
      return 0
      ;;
    1:finding)
      echo "$SELF: REFUSED — ggshield found a secret in the staged change (above)." >&2
      leak_policy
      return 1
      ;;
    1:finding-incomplete)
      echo "$SELF: REFUSED — ggshield listed a secret in a scan that did not complete (above): the staged change fails, and a second run is not needed." >&2
      leak_policy
      return 1
      ;;
    1:finding-unread)
      echo "$SELF: REFUSED — ggshield exited 1 (it found problems) but the judge could not read a finding in full (above): the staged change fails." >&2
      leak_policy
      return 1
      ;;
    2:skipped)
      echo "$SELF: REFUSED — ggshield skipped a file it cannot scan (over its size limit, an encoding it cannot detect, or a file it cannot read), so the staged change was NOT scanned for secrets in full." >&2
      echo "  A re-run skips the same file: change or leave out the file, or on your own machine opt out for this commit with GGSHIELD_SKIP=1." >&2
      return 1
      ;;
    *)
      echo "$SELF: REFUSED (could not scan, or the scan did not provably finish) — the staged change counts as NOT scanned for secrets." >&2
      echo "  Fix the cause (key, network, quota; \`ggshield api-status\` says more), or on your own machine opt out for this commit with GGSHIELD_SKIP=1." >&2
      return 1
      ;;
  esac
}

selftest() {
  local fail=0 tmp stub_dir out rc
  tmp=$(mktemp -d)
  stub_dir="$tmp/bin"
  mkdir -p "$stub_dir" "$tmp/empty"
  trap 'rm -rf "$tmp"' RETURN

  # A stub ggshield whose exit code and report we choose: this proves THIS
  # script's verdicts without spending a single call on the real service. It
  # also records its arguments, the environment facts the script pins, and
  # the tree and files of the commit it was asked to scan.
  cat >"$stub_dir/ggshield" <<'EOF'
#!/usr/bin/env bash
range=${*: -1}
id=${range##*..}
rc=${GGSHIELD_STUB_RC:-0}
mode=${GGSHIELD_STUB_MODE:-auto}
{
  echo "args: $*"
  echo "dont_load_env=${GITGUARDIAN_DONT_LOAD_ENV-unset}"
  echo "env_instance=${GITGUARDIAN_INSTANCE-unset} env_api_url=${GITGUARDIAN_API_URL-unset}"
  echo "gg_max_doc_size=${GG_MAX_DOC_SIZE-unset}"
  echo "bash_env=${BASH_ENV-unset} env_file=${ENV-unset}"
  echo "git_config=${GIT_CONFIG_COUNT-unset}/${GIT_CONFIG_KEY_0-unset}/${GIT_CONFIG_VALUE_0-unset} params=${GIT_CONFIG_PARAMETERS-unset}"
  echo "pythonpath=${PYTHONPATH-unset} pythonhome=${PYTHONHOME-unset} pythonstartup=${PYTHONSTARTUP-unset}"
  echo "pythonuserbase=${PYTHONUSERBASE-unset} pythoninspect=${PYTHONINSPECT-unset}"
  echo "no_replace=${GIT_NO_REPLACE_OBJECTS-unset} graft_file=${GIT_GRAFT_FILE-unset} gg_git_config=${GG_GIT_CONFIG-unset} attr_source=${GIT_ATTR_SOURCE-unset} ext_diff=${GIT_EXTERNAL_DIFF-unset}"
  echo "config_file=${GIT_CONFIG-unset} shallow_file=${GIT_SHALLOW_FILE-unset} literal=${GIT_LITERAL_PATHSPECS-unset}"
  echo "notes_ref=${GIT_NOTES_REF-unset} notes_display=${GIT_NOTES_DISPLAY_REF-unset}"
  echo "commits=$(git rev-list "$range" -- 2>/dev/null | wc -l)"
  echo "tree=$(git rev-parse -q --verify "$id^{tree}" 2>/dev/null)"
  echo "files=$(git diff-tree --root --no-commit-id --name-only -r "$id" 2>/dev/null | tr '\n' ' ')"
} >"${GGSHIELD_STUB_LOG:-/dev/null}"
# show: read the commit as ggshield 1.56.1 does (its git environment and its two git show calls, measured:
# the header lists the paths, split as ggshield splits it, and the patch is asked for those paths) and
# report a finding exactly when the marker line reaches the patch. GGSHIELD_STUB_DURING runs while the
# scan runs, to change something under it.
if [ "$mode" = show ]; then
  hdr=$(GIT_CONFIG_SYSTEM= GIT_CONFIG_GLOBAL="${GG_GIT_CONFIG-}" git -c core.quotePath=false -c safe.directory='*' \
    show "$id" --raw -z 2>/dev/null | python3 -I -c '
import re, sys
info, *lines = re.split("[\n\0]:", sys.stdin.buffer.read().decode("utf-8", "replace"))
sys.stdout.write("NOTES_SEEN\0" if "GATE_NOTE_TEXT" in info else "")
for x in lines:
    prefix, path, *rest = x.rstrip("\0").split("\0")
    if rest:
        sys.stdout.write(path + "\0")
        path = rest[0]
    sys.stdout.write(path + "\0")
' | tr '\0' '\n'; printf '\n%s' "${PIPESTATUS[*]}")
  # ggshield's own parse fails on a header it cannot split (a path that starts with ":"): it dies.
  hdr_rc=${hdr##*$'\n'}
  hdr=${hdr%$'\n'*}
  [ -z "${GGSHIELD_STUB_DURING-}" ] || sh -c "$GGSHIELD_STUB_DURING"
  case "$hdr" in
    NOTES_SEEN*) echo "notes_seen=yes" >>"${GGSHIELD_STUB_LOG:-/dev/null}"; hdr=${hdr#NOTES_SEEN} ;;
    *) echo "notes_seen=no" >>"${GGSHIELD_STUB_LOG:-/dev/null}" ;;
  esac
  paths=()
  while IFS= read -r p; do [ -z "$p" ] || paths+=("$p"); done <<<"$hdr"
  if [ "$hdr_rc" != "0 0 0" ]; then
    mode=dead
  elif [ "${#paths[@]}" -gt 0 ] && GIT_CONFIG_SYSTEM= GIT_CONFIG_GLOBAL="${GG_GIT_CONFIG-}" git -c core.quotePath=false -c safe.directory='*' \
    show "$id" --raw -z --patch -- "${paths[@]}" 2>/dev/null | grep -aq 'GATE_STUB_MARKER'; then
    mode=finding rc=1
  else
    mode=clean rc=0
  fi
fi
if [ "$mode" = auto ]; then
  case "$rc" in
    0) mode=clean ;;
    1) mode=finding ;;
    *) mode=dead ;;
  esac
fi
# docnames: the paths of the commit under scan that ggshield sends as documents (the header of its first git
# show call; a rename's old path too), one JSON string per line, for the commit's "entities_with_incidents"
# as the real ggshield lists them and as the judge checks them.
docnames() {
  python3 -I -c '
import json, os, re, subprocess, sys
env = dict(os.environ, GIT_CONFIG_SYSTEM="", GIT_CONFIG_GLOBAL=os.environ.get("GG_GIT_CONFIG", ""), LANG="C")
def git(*a):
    return subprocess.run(["git", "-c", "core.quotePath=false", "-c", "safe.directory=*", *a], env=env,
                          stdin=subprocess.DEVNULL, capture_output=True).stdout.decode("utf-8", "replace")
sha = sys.argv[1]
info, *lines = re.split("[\n\0]:", git("show", sha, "--raw", "-z"))
paths = []
for x in lines:
    prefix, path, *rest = x.rstrip("\0").split("\0")
    paths += [path] if not rest else [path, rest[0]]
patch = git("show", sha, "--raw", "-z", "--patch", "--", *paths) if paths else ""
toks = patch.split("\0diff ", 1)
for d in (re.split(r"^diff ", toks[1], flags=re.M) if len(toks) == 2 else []):
    if "\n@@" in d:
        sys.stdout.write(json.dumps(re.match(r"--git a/.* b/(.*)\n", d).group(1)) + "\n")
' "$id"
}
scan() { # scan <commit id> <incidents in it>
  local ents="" f first=1 inc="" n
  if [ "$1" = "$id" ]; then
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      inc='[]' n=0
      if [ "$2" != 0 ] && [ "$first" = 1 ]; then inc='[{"type": "Stub Detector", "occurrences": [{"match": "st**ub"}]}]' n=1; fi
      [ "$first" = 1 ] || ents="$ents, "
      ents="$ents{\"mode\": \"NEW\", \"filename\": $f, \"incidents\": $inc, \"total_incidents\": $n}"
      first=0
    done < <(docnames)
  fi
  if [ -z "$ents" ] && [ "$2" != 0 ]; then
    ents='{"mode": "NEW", "filename": "stub.txt", "incidents": [{"type": "Stub Detector", "occurrences": [{"match": "st**ub"}]}], "total_incidents": 1}'
  fi
  [ -z "$ents" ] || ents="\"entities_with_incidents\": [$ents], "
  printf '{"id": "%s", "type": "commit", %s"total_incidents": %s}' "$1" "$ents" "$2"
}
report() { # report <scans, comma separated> <total incidents>
  printf '{"id": "stub", "type": "commit-range", "scans": [%s], "total_incidents": %s}\n' "$1" "$2"
}
case "$mode" in
  clean) report "$(scan "$id" 0)" 0 ;;
  finding | hidden-finding) report "$(scan "$id" 1)" 1 ;;
  dead) echo "Error: stub ggshield could not scan" >&2 ;;
  errors) report "{\"id\": \"$id\", \"type\": \"commit\", \"errors\": [{\"files\": [], \"description\": \"ServiceUnavailableError: stub\"}], \"total_incidents\": 0}" 0 ;;
  text) printf '\nNo secrets have been found\n\n' ;;
  cut) report "$(scan "$id" 0)" 0 | head -c 40 ;;
  twice-json) report "$(scan "$id" 0)" 0; report "$(scan "$id" 0)" 0 ;;
  silent) ;;
  error-line) report "$(scan "$id" 0)" 0; echo "Error: 413:stub refusal" >&2 ;;
  coloured-error-line) report "$(scan "$id" 0)" 0; printf '\033[31mError:\033[0m stub\n' >&2 ;;
  log-error-line) report "$(scan "$id" 0)" 0; echo "2026-11-17 10:00:00,000 4242 [E] stub" >&2 ;;
  scanning-failed) report "$(scan "$id" 0)" 0; echo "Warning: Scanning failed for a chunk" >&2 ;;
  may-be-incomplete) report "$(scan "$id" 0)" 0; echo "note - Results may be incomplete." >&2 ;;
  finding-error-line) report "$(scan "$id" 1)"; echo "Error: 413:stub refusal" >&2 ;;
  skipped-file) report "$(scan "$id" 0)" 0; echo "Skipped big.bin: file too large" >&2 ;;
  hang) sleep 20; report "$(scan "$id" 0)" 0 ;;
  harmless-stderr) report "$(scan "$id" 0)" 0; echo "Scanning 1 commit" >&2 ;;
  no-commit) report "" 0 ;;
  other-commit) report "$(scan 0000000000000000000000000000000000000000 0)" 0 ;;
  extra-commit) report "$(scan "$id" 0), $(scan 0000000000000000000000000000000000000000 0)" 0 ;;
  commit-twice) report "$(scan "$id" 0), $(scan "$id" 0)" 0 ;;
  wrong-type) printf '{"id": "stub", "type": "path", "scans": [%s], "total_incidents": 0}\n' "$(scan "$id" 0)" ;;
  bad-total) report "$(scan "$id" 0)" 1 ;;
  *) echo "stub ggshield: unknown mode $mode" >&2; exit 99 ;;
esac
exit "$rc"
EOF
  chmod +x "$stub_dir/ggshield"

  local self log repo
  self="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  log="$tmp/stub.log"

  # Every case runs in a scratch repository with one commit and a staged
  # change, with the variables this script reads cleared: CI sets CI, and a
  # selftest started from a hook inherits git's own variables.
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX
  unset GGSHIELD_SKIP CI FORGEJO_ACTIONS GITHUB_ACTIONS
  unset GGSHIELD_STUB_RC GGSHIELD_STUB_MODE GGSHIELD_STUB_LOG GGSHIELD_STUB_DURING
  # The machine's own git settings and attributes stay out of every case (a runner's git-lfs filter.lfs.*
  # would refuse them all): no system or global config, a scratch HOME, no system attributes.
  mkdir -p "$tmp/home"
  export HOME="$tmp/home" GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_GLOBAL=/dev/null GIT_ATTR_NOSYSTEM=1
  unset XDG_CONFIG_HOME GG_GIT_CONFIG GIT_ATTR_SOURCE GIT_EXTERNAL_DIFF GIT_NO_REPLACE_OBJECTS GIT_GRAFT_FILE
  unset GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT GIT_CONFIG GIT_SHALLOW_FILE GIT_DIFF_OPTS GIT_NOTES_REF GIT_NOTES_DISPLAY_REF
  unset GIT_LITERAL_PATHSPECS GIT_GLOB_PATHSPECS GIT_NOGLOB_PATHSPECS GIT_ICASE_PATHSPECS
  mkrepo() { # mkrepo <dir>: one commit, then a staged new file and a staged edit
    mkdir -p "$1"
    git -C "$1" init -q 2>/dev/null
    printf 'one\n' >"$1/kept.txt"
    git -C "$1" add kept.txt
    git -C "$1" -c user.name=t -c user.email=t@localhost -c commit.gpgsign=false \
      -c core.hooksPath=/dev/null commit -q -m base
    printf 'two\n' >>"$1/kept.txt"
    printf 'new\n' >"$1/staged.txt"
    git -C "$1" add kept.txt staged.txt
  }
  repo="$tmp/repo"
  mkrepo "$repo"
  gate() { # gate [NAME=value...]: the gate in $repo, the stub first on PATH
    (cd "$repo" && env PATH="$stub_dir:$PATH" "$@" bash "$self" 2>&1)
  }

  check() { # check <name> <got-rc> <want-rc> <output> <must-contain>
    if [ "$2" = "$3" ] && grep -qi -- "$5" <<< "$4"; then
      printf '  ok    %s\n' "$1"
    else
      printf '  FAIL  %s (rc=%s want %s, looking for "%s")\n' "$1" "$2" "$3" "$5"
      printf '%s\n' "$4" | sed 's/^/        /'
      fail=1
    fi
  }

  local head_before status_before
  head_before=$(git -C "$repo" rev-parse HEAD)
  status_before=$(git -C "$repo" status --porcelain)

  out=$(gate GGSHIELD_STUB_RC=0); rc=$?
  check "rc 0 with a clean report allows the commit" "$rc" 0 "$out" "$SELF: clean"
  # A clean run prints no shell error (its EXIT trap once listed jobs into the removed scratch directory and
  # printed "No such file or directory"). check only looks for a substring, so the absence is tested here.
  if grep -qE 'No such file|: line [0-9]+: ' <<< "$out"; then
    printf '  FAIL  a clean run printed a shell error\n'; printf '%s\n' "$out" | sed 's/^/        /'; fail=1
  else
    printf '  ok    …and printed no shell error\n'
  fi

  out=$(gate GGSHIELD_STUB_RC=1); rc=$?
  check "rc 1 with a finding in the report BLOCKS the commit" "$rc" 1 "$out" "found a secret"
  check "…and names the detector and the file" "$rc" 1 "$out" "detector 'Stub Detector', file 'kept.txt'"
  if grep -q 'st\*\*ub' <<< "$out"; then
    printf '  FAIL  the matched text was printed\n'; fail=1
  else
    printf '  ok    the matched text is not printed\n'
  fi

  local n
  for n in 2 3 128; do
    out=$(gate GGSHIELD_STUB_RC=$n); rc=$?
    check "rc $n (cannot scan) BLOCKS, and says the change was not scanned" "$rc" 1 "$out" "NOT scanned"
    check "rc $n names the opt-out" "$rc" 1 "$out" "GGSHIELD_SKIP=1"
  done

  # Exit 0 is not a pass by itself: the report and the other output decide.
  local m
  for m in errors text cut twice-json silent error-line coloured-error-line log-error-line \
    scanning-failed may-be-incomplete no-commit other-commit extra-commit commit-twice \
    wrong-type skipped-file; do
    out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_MODE=$m); rc=$?
    check "rc 0, report '$m': BLOCKS, not scanned" "$rc" 1 "$out" "NOT scanned"
  done
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_MODE=errors); rc=$?
  check "an errors entry is named, by kind only" "$rc" 1 "$out" "error kinds reported: ServiceUnavailableError"
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_MODE=error-line); rc=$?
  check "an error line is named, by kind only" "$rc" 1 "$out" "error kinds reported: 413"
  if grep -q 'stub refusal' <<< "$out"; then
    printf '  FAIL  a line of ggshield'"'"'s own text was printed\n'; fail=1
  else
    printf '  ok    no line of ggshield'"'"'s own text is printed\n'
  fi
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_MODE=no-commit); rc=$?
  check "a report covering less than was handed over says so" "$rc" 1 "$out" "commits scanned are not the range's"
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_MODE=skipped-file); rc=$?
  check "a file ggshield skipped is named as skipped, not scanned" "$rc" 1 "$out" "skipped 1 file(s)"
  # A scan that does not finish is stopped at the judge's time limit (lowered here), not waited for.
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_MODE=hang GGSHIELD_JUDGE_TIMEOUT=2); rc=$?
  check "a scan that stalls is stopped at the time limit, not a pass" "$rc" 1 "$out" "did not finish within 2 s"
  check "…and counts as not scanned" "$rc" 1 "$out" "NOT scanned"
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_MODE=harmless-stderr); rc=$?
  check "rc 0, a clean report and a harmless other line allows" "$rc" 0 "$out" "$SELF: clean"
  out=$(gate GGSHIELD_STUB_RC=1 GGSHIELD_STUB_MODE=clean); rc=$?
  check "rc 1 with no finding in the report BLOCKS, as a finding it could not read" "$rc" 1 "$out" "could not read a finding"
  out=$(gate GGSHIELD_STUB_RC=1 GGSHIELD_STUB_MODE=error-line); rc=$?
  check "rc 1 with an error line BLOCKS" "$rc" 1 "$out" "REFUSED"

  # The judge's verdicts for a finding it could not take as a plain one: each refuses as a FINDING, with
  # no invitation to opt out and no claim that nothing was scanned (a secret was seen); a skipped file
  # is "not scanned" and says that a re-run skips it again.
  refused_as_finding() { # refused_as_finding <name> <output> <must-contain>
    check "$1" 1 1 "$2" "$3"
    if grep -q 'GGSHIELD_SKIP\|NOT scanned' <<< "$2"; then
      printf '  FAIL  %s: it calls the change not scanned or names the opt-out\n' "$1"; fail=1
    else
      printf '  ok    …and it neither calls the change not scanned nor names the opt-out\n'
    fi
  }
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_MODE=hidden-finding); rc=$?
  check "rc 0 with an incident in the report is a finding in a scan that did not finish: BLOCKS" "$rc" 1 "$out" "listed a secret in a scan that did not complete"
  refused_as_finding "rc 0 with an incident in the report: refused as a finding" "$out" "leak policy"
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_MODE=bad-total); rc=$?
  refused_as_finding "rc 0 with a count above 0 and no incident listed: refused as a finding" "$out" "listed a secret in a scan that did not complete"
  out=$(gate GGSHIELD_STUB_RC=1 GGSHIELD_STUB_MODE=finding-error-line); rc=$?
  check "rc 1 with a finding and an error line BLOCKS" "$rc" 1 "$out" "listed a secret in a scan that did not complete"
  refused_as_finding "rc 1 with a finding and an error line: refused as a finding" "$out" "vault it"
  out=$(gate GGSHIELD_STUB_RC=1 GGSHIELD_STUB_MODE=clean); rc=$?
  refused_as_finding "rc 1 and nothing the judge could read: refused as a finding" "$out" "could not read a finding"
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_MODE=skipped-file); rc=$?
  check "a skipped file says a re-run skips it again" "$rc" 1 "$out" "re-run skips the same file"

  # The word and the exit status must be one of the pairs the judge makes; any other pair, and any
  # stdout that is more than the one word, is refused as not scanned. A judge that is a stand-in
  # here (the gate copied beside it) prints the word and exits as it is told.
  local fj="$tmp/fj" pair
  mkdir -p "$fj"
  cp -- "$self" "$fj/ggshield-gate.sh"
  printf 'import os, sys\nprint(os.environ["FAKE_WORD"].replace("+", "\\n"))\nsys.exit(int(os.environ["FAKE_RC"]))\n' >"$fj/ggshield-judge.py"
  fgate() { # fgate <rc> <word, + for a newline>
    (cd "$repo" && env PATH="$stub_dir:$PATH" FAKE_RC="$1" FAKE_WORD="$2" bash "$fj/ggshield-gate.sh" 2>&1)
  }
  out=$(fgate 0 clean); rc=$?
  check "a stand-in judge saying 0 clean allows (the harness itself works)" "$rc" 0 "$out" "$SELF: clean"
  for pair in 0:finding 0:finding-incomplete 0:finding-unread 0:skipped 0:incomplete 0:nonsense 0: \
    1:clean 1:incomplete 1:nonsense 1: 2:clean 2:finding 2:finding-incomplete 2:finding-unread 2:nonsense \
    3:clean 3:finding 5:clean 0:clean+clean 0:nonsense+clean 0:clean+nonsense; do
    out=$(fgate "${pair%%:*}" "${pair#*:}"); rc=$?
    check "judge exit ${pair%%:*} with '${pair#*:}' is refused, not scanned" "$rc" 1 "$out" "REFUSED"
    if grep -q "$SELF: clean" <<< "$out"; then
      printf '  FAIL  judge exit %s with %s was taken as clean\n' "${pair%%:*}" "'${pair#*:}'"; fail=1
    fi
  done
  out=$(fgate 1 finding-incomplete); rc=$?
  check "a stand-in judge's 1 finding-incomplete is refused as a finding" "$rc" 1 "$out" "listed a secret in a scan that did not complete"
  out=$(fgate 1 finding-unread); rc=$?
  check "a stand-in judge's 1 finding-unread is refused as a finding" "$rc" 1 "$out" "could not read a finding"
  out=$(fgate 2 skipped); rc=$?
  check "a stand-in judge's 2 skipped is refused, not scanned" "$rc" 1 "$out" "skipped a file it cannot scan"
  out=$(fgate 2 incomplete); rc=$?
  check "a stand-in judge's 2 incomplete is refused, not scanned" "$rc" 1 "$out" "NOT scanned"

  # PATH is the empty dir ALONE: with /usr/bin on it the real ggshield is
  # found and the case proves nothing (measured — it scanned for real and
  # reported "clean", which would have passed a weaker assertion).
  # "$BASH", not `bash`, and no `env`: with an empty PATH neither is findable.
  out=$(cd "$repo" && PATH="$tmp/empty" "$BASH" "$self" 2>&1); rc=$?
  check "ggshield absent BLOCKS, and says nothing was scanned" "$rc" 1 "$out" "NOTHING was scanned"
  check "ggshield absent names the opt-out" "$rc" 1 "$out" "GGSHIELD_SKIP=1"

  out=$(gate GGSHIELD_STUB_RC=1 GGSHIELD_SKIP=1); rc=$?
  check "GGSHIELD_SKIP=1 allows, wins even over a finding" "$rc" 0 "$out" "SKIP"
  check "GGSHIELD_SKIP=1 says loudly that nothing was scanned" "$rc" 0 "$out" "NOTHING was scanned"
  out=$(cd "$repo" && PATH="$tmp/empty" GGSHIELD_SKIP=1 "$BASH" "$self" 2>&1); rc=$?
  check "GGSHIELD_SKIP=1 allows with ggshield absent" "$rc" 0 "$out" "NOTHING was scanned"

  # A subshell TERMed just after its fork must not run the gate's EXIT trap (it removed the gate's scratch
  # directory: measured with the unguarded trap, hundreds of 400 forks did). Same trap string as the gate's.
  # gate_pid names the process it runs in: a subshell's answer differs from its parent's (a stale or inherited
  # id, as bash 5.2's BASHPID gives a just-forked subshell in its trap, would make them equal).
  local gp_me gp_sub
  gate_pid gp_me
  gp_sub=$(gate_pid GATE_SUB_PID; echo "$GATE_SUB_PID")
  check "gate_pid in a subshell is not the parent's id" 0 0 "$([ -n "$gp_me" ] && [ -n "$gp_sub" ] && [ "$gp_me" != "$gp_sub" ] && echo differ)" "differ"
  local lost rd="$tmp/trapdir"
  lost=$(
    n=0
    for _ in $(seq 300); do
      mkdir -p "$rd"
      if ! (
        # shellcheck disable=SC2034  # read by the trap string
        gate_pid GATE_PID; SCRATCH=$rd; GLIST_ACTIVE=
        # shellcheck disable=SC2064
        trap "$GATE_EXIT_TRAP" EXIT
        ( trap ':' TERM; sleep 5 & s=$!; wait "$s" ) </dev/null >/dev/null 2>&1 &
        w=$!
        kill -TERM "%1" 2>/dev/null
        wait "$w" 2>/dev/null
        [ -d "$SCRATCH" ]
      ); then n=$((n + 1)); fi
    done
    echo "$n"
  )
  check "a watchdog-like subshell TERMed after its fork leaves the scratch directory alone (300 tries)" 0 0 "lost=$lost" "lost=0"

  # The skip is for a person's own machine: refused wherever an automated run says it is one.
  local v
  for v in CI FORGEJO_ACTIONS GITHUB_ACTIONS; do
    out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_SKIP=1 "$v=true"); rc=$?
    check "GGSHIELD_SKIP=1 with $v=true is REFUSED" "$rc" 1 "$out" "REFUSED"
    check "…and it names $v and says nothing was scanned" "$rc" 1 "$out" "$v is set here"
    out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_SKIP=1 "$v="); rc=$?
    check "GGSHIELD_SKIP=1 with $v set but empty is REFUSED" "$rc" 1 "$out" "NOTHING was scanned"
    out=$(cd "$repo" && PATH="$tmp/empty" GGSHIELD_SKIP=1 "$BASH" -c "export $v=1; exec \"\$BASH\" \"\$0\"" "$self" 2>&1); rc=$?
    check "GGSHIELD_SKIP=1 with $v=1 and ggshield absent is REFUSED" "$rc" 1 "$out" "REFUSED"
    out=$(gate GGSHIELD_STUB_RC=0 "$v=true"); rc=$?
    check "$v=true without the skip scans as anywhere (clean allows)" "$rc" 0 "$out" "$SELF: clean"
    out=$(gate GGSHIELD_STUB_RC=1 GGSHIELD_SKIP=0 "$v=true"); rc=$?
    check "$v=true with GGSHIELD_SKIP=0 scans (a finding blocks)" "$rc" 1 "$out" "found a secret"
  done

  # The scan's settings are its own: the stub records what ggshield was called with.
  : >"$log"
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_LOG="$log" GITGUARDIAN_DONT_LOAD_ENV=0); rc=$?
  check "the scan runs" "$rc" 0 "$out" "clean"
  check "the scan sets GITGUARDIAN_DONT_LOAD_ENV=1" 0 0 "$(cat "$log")" "dont_load_env=1"
  : >"$log"
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_LOG="$log" GG_MAX_DOC_SIZE=1); rc=$?
  check "GG_MAX_DOC_SIZE in the environment does not reach ggshield" 0 0 "$(cat "$log")" "gg_max_doc_size=unset"
  check "the scan passes --config-path" 0 0 "$(cat "$log")" "args: --config-path "
  check "the scan asks for a JSON report of a commit range" 0 0 "$(cat "$log")" "secret scan --json commit-range "
  check "the scan pins the instance" 0 0 "$(cat "$log")" "--instance https://dashboard.gitguardian.com "
  check "the range holds exactly one commit" 0 0 "$(cat "$log")" "^commits=1$"
  check "that commit's change is the staged change" 0 0 "$(cat "$log")" "^files=kept.txt staged.txt $"
  if [ "$(git -C "$repo" rev-parse HEAD)" = "$head_before" ] \
    && [ "$(git -C "$repo" status --porcelain)" = "$status_before" ] \
    && [ "$(git -C "$repo" for-each-ref --format='%(refname)' | wc -l)" = 1 ]; then
    printf '  ok    the scans moved no ref and left the index and working tree as they were\n'
  else
    printf '  FAIL  a scan moved a ref or changed the index or working tree\n'; fail=1
  fi

  # The commit to scan is never signed: with signing on and a signing program
  # that records being called and fails, the scan still runs and it was not called.
  printf '#!/bin/sh\necho called >>%q\nexit 1\n' "$tmp/signer.log" >"$tmp/signer"
  chmod +x "$tmp/signer"
  git -C "$repo" config commit.gpgsign true
  git -C "$repo" config gpg.program "$tmp/signer"
  out=$(gate GGSHIELD_STUB_RC=0); rc=$?
  check "with commit.gpgsign on, the scan still runs" "$rc" 0 "$out" "$SELF: clean"
  if [ -e "$tmp/signer.log" ]; then
    printf '  FAIL  the signing program was called\n'; fail=1
  else
    printf '  ok    …and the signing program is never called\n'
  fi
  git -C "$repo" config --unset commit.gpgsign
  git -C "$repo" config --unset gpg.program

  # Foreign settings ignored: the instance variables and files in the working
  # directory name another host; the stub must see the fixed instance and
  # none of those values.
  printf 'instance: https://other.example\n' >"$repo/.gitguardian.yaml"
  printf 'GITGUARDIAN_INSTANCE=https://other.example\nGITGUARDIAN_API_URL=https://other.example/v1\n' >"$repo/.env"
  : >"$log"
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_LOG="$log" \
    GITGUARDIAN_INSTANCE=https://other.example GITGUARDIAN_API_URL=https://other.example/v1); rc=$?
  check "the scan runs with foreign settings present" "$rc" 0 "$out" "clean"
  check "foreign settings ignored: the instance is the fixed one" 0 0 "$(cat "$log")" "--instance https://dashboard.gitguardian.com "
  check "foreign settings ignored: GITGUARDIAN_INSTANCE is unset for the call" 0 0 "$(cat "$log")" "env_instance=unset"
  check "foreign settings ignored: GITGUARDIAN_API_URL is unset for the call" 0 0 "$(cat "$log")" "env_api_url=unset"
  if grep -q other.example "$log"; then
    printf '  FAIL  a foreign value reached ggshield\n'; sed 's/^/        /' "$log"; fail=1
  else
    printf '  ok    no foreign value reached ggshield\n'
  fi
  rm -f "$repo/.gitguardian.yaml" "$repo/.env"
  local cfg
  cfg=$(sed -n 's/^args: --config-path \([^ ]*\) .*/\1/p' "$log")
  if [ -n "$cfg" ] && [ "$cfg" != ".gitguardian.yaml" ] && [ "${cfg#"$repo"/}" = "$cfg" ]; then
    printf '  ok    the config path is not the working directory'"'"'s own file\n'
  else
    printf '  FAIL  the config path (%s) is empty or inside the working directory\n' "$cfg"
    fail=1
  fi
  if [ -n "$cfg" ] && [ ! -e "$cfg" ] && [ ! -e "$(dirname "$cfg")" ]; then
    printf '  ok    the temporary config file and its directory are gone after the run\n'
  else
    printf '  FAIL  the temporary config file (%s) or its directory is still there\n' "$cfg"
    fail=1
  fi

  # Only exactly 1 skips.
  for v in 0 "" yes true 11; do
    out=$(gate GGSHIELD_STUB_RC=1 GGSHIELD_SKIP="$v"); rc=$?
    check "GGSHIELD_SKIP='$v' still scans (a finding blocks)" "$rc" 1 "$out" "found a secret"
  done
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_SKIP=0); rc=$?
  check "GGSHIELD_SKIP=0 scans (a clean scan says clean, not SKIP)" "$rc" 0 "$out" "$SELF: clean"

  # A ggshield or python3 that resolves inside the working tree is refused; one outside is used.
  mkdir -p "$repo/.venv/bin" "$repo/.tools/bin"
  cp "$stub_dir/ggshield" "$repo/.venv/bin/ggshield"
  out=$(cd "$repo" && PATH="$repo/.venv/bin:$PATH" GGSHIELD_STUB_RC=0 bash "$self" 2>&1); rc=$?
  check "a PATH entry holding ggshield inside the working tree is refused" "$rc" 1 "$out" "PATH entry .* resolves inside the repository"
  check "…and nothing was scanned, with the opt-out named" "$rc" 1 "$out" "GGSHIELD_SKIP=1"
  mkdir -p "$tmp/link-gg" "$tmp/link-py" "$tmp/link-git"
  ln -s "$repo/.venv/bin/ggshield" "$tmp/link-gg/ggshield"
  out=$(cd "$repo" && PATH="$tmp/link-gg:$PATH" GGSHIELD_STUB_RC=0 bash "$self" 2>&1); rc=$?
  check "a ggshield linked from outside to a file inside the working tree is refused" "$rc" 1 "$out" "ggshield resolves inside the repository"
  printf '#!/bin/sh\necho clean\nexit 0\n' >"$repo/.tools/bin/python3"
  chmod +x "$repo/.tools/bin/python3"
  out=$(cd "$repo" && PATH="$repo/.tools/bin:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 bash "$self" 2>&1); rc=$?
  check "a PATH entry holding python3 inside the working tree is refused" "$rc" 1 "$out" "PATH entry .* resolves inside the repository"
  ln -s "$repo/.tools/bin/python3" "$tmp/link-py/python3"
  out=$(cd "$repo" && PATH="$tmp/link-py:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 bash "$self" 2>&1); rc=$?
  check "a python3 linked from outside to a file inside the working tree is refused" "$rc" 1 "$out" "python3 resolves inside the repository"
  out=$(gate GGSHIELD_STUB_RC=0); rc=$?
  check "a ggshield outside the working tree is used" "$rc" 0 "$out" "$SELF: clean"
  printf '#!/bin/sh\nexec %q "$@"\n' "$(command -v git)" >"$repo/.tools/bin/git"
  chmod +x "$repo/.tools/bin/git"
  out=$(cd "$repo" && PATH="$repo/.tools/bin:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 bash "$self" 2>&1); rc=$?
  check "a PATH entry holding git inside the working tree is refused" "$rc" 1 "$out" "PATH entry .* resolves inside the repository"
  check "…and nothing was scanned, with the opt-out named" "$rc" 1 "$out" "GGSHIELD_SKIP=1"
  ln -s "$repo/.tools/bin/git" "$tmp/link-git/git"
  out=$(cd "$repo" && PATH="$tmp/link-git:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 bash "$self" 2>&1); rc=$?
  check "a git linked from outside to a file inside the working tree is refused" "$rc" 1 "$out" "git resolves inside the repository"
  rm -rf "$repo/.venv" "$repo/.tools"

  # PATH is proven before any helper program runs: a helper planted where PATH finds it first (env,
  # readlink, ...) must never decide anything. Each case plants a helper that would let the gate pass.
  local plant="$repo/.planted" ran="$tmp/git-ran" plant_git
  plant_git=$(command -v git)
  mkdir -p "$plant/bin" "$repo/relbin"
  planted_env() { # planted_env <dir>: an env that answers like the judge saying "clean"
    printf '#!/bin/sh\necho clean\nexit 0\n' >"$1/env"
    chmod +x "$1/env"
  }
  not_clean() { # not_clean <name> <output>
    if grep -q "$SELF: clean" <<< "$2"; then
      printf '  FAIL  %s: it passed as clean\n' "$1"; fail=1
    else
      printf '  ok    …and it did not pass as clean\n'
    fi
  }
  planted_env "$plant/bin"
  out=$(cd "$repo" && PATH="$plant/bin:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a planted env in a PATH directory inside the tree is refused" "$rc" 1 "$out" "PATH entry"
  not_clean "a planted env in the tree" "$out"
  rm -f "$plant/bin/env"
  # a readlink that calls every path outside the tree, and a git that records that it ran
  printf '#!/bin/sh\nprintf "%%s\\n" /outside/the/tree\n' >"$plant/bin/readlink"
  printf '#!/bin/sh\necho ran >>%q\nexec %q "$@"\n' "$ran" "$plant_git" >"$plant/bin/git"
  chmod +x "$plant/bin/readlink" "$plant/bin/git"
  rm -f "$ran"
  out=$(cd "$repo" && PATH="$plant/bin:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a planted readlink cannot let a git inside the tree run" "$rc" 1 "$out" "PATH entry"
  if [ -e "$ran" ]; then
    printf '  FAIL  the git inside the tree was run\n'; fail=1
  else
    printf '  ok    …and the git inside the tree never ran\n'
  fi
  rm -f "$plant/bin/readlink" "$plant/bin/git"
  planted_env "$repo/relbin"
  planted_env "$repo"
  out=$(cd "$repo" && PATH="relbin:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a relative PATH entry is refused" "$rc" 1 "$out" "relative"
  not_clean "a relative PATH entry" "$out"
  out=$(cd "$repo" && PATH=".:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a PATH entry of . is refused" "$rc" 1 "$out" "relative"
  not_clean "a PATH entry of ." "$out"
  out=$(cd "$repo" && PATH=":$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "an empty PATH entry (first) is refused" "$rc" 1 "$out" "empty"
  not_clean "an empty PATH entry (first)" "$out"
  out=$(cd "$repo" && PATH="$stub_dir:$PATH:" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "an empty PATH entry (last) is refused" "$rc" 1 "$out" "empty"
  not_clean "an empty PATH entry (last)" "$out"
  out=$(cd "$repo" && PATH="$stub_dir::$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "an empty PATH entry (middle) is refused" "$rc" 1 "$out" "empty"
  rm -rf "$repo/relbin" "$repo/env" "$plant"

  # What runs before the gate's own first line: the interpreter its shebang names (looked up on no
  # PATH), CDPATH, exported functions and BASH_ENV. Each row plants something that would give a pass
  # or run a program from inside the tree; the gate is run through its shebang, as lefthook does.
  local mk="$tmp/marker-bin" mark="$tmp/marker-ran"
  mkdir -p "$mk" "$plant/bin"
  printf '#!/bin/sh\necho ran >>%q\nexec %q "$@"\n' "$mark" "$plant_git" >"$mk/git"
  chmod +x "$mk/git"
  printf '#!/bin/sh\necho "%s: clean"\nexit 0\n' "$SELF" >"$plant/bin/bash"
  chmod +x "$plant/bin/bash"
  out=$(cd "$repo" && PATH="$plant/bin:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$self" 2>&1); rc=$?
  check "a planted bash in a PATH directory inside the tree is not what runs the gate" "$rc" 1 "$out" "PATH entry"
  not_clean "a planted bash in the tree" "$out"
  rm -rf "$plant"

  # CDPATH: the gate is started by a relative path, the way lefthook does, with a CDPATH that holds
  # a directory of the same name containing a judge that says "clean".
  local cdp="$tmp/cdp"
  mkdir -p "$repo/gcopy" "$cdp/gcopy"
  cp -- "$self" "$repo/gcopy/ggshield-gate.sh"
  cp -- "$(dirname -- "$self")/ggshield-judge.py" "$repo/gcopy/ggshield-judge.py"
  printf 'open("%s", "a").write("ran\\n")\nprint("clean")\n' "$tmp/judge-ran" >"$cdp/gcopy/ggshield-judge.py"
  rm -f "$tmp/judge-ran"
  out=$(cd "$repo" && CDPATH="$cdp" GGSHIELD_STUB_RC=0 PATH="$stub_dir:$PATH" "$BASH" gcopy/ggshield-gate.sh 2>&1); rc=$?
  check "a judge beside the script is used, whatever CDPATH holds" "$rc" 0 "$out" "$SELF: clean"
  if [ -e "$tmp/judge-ran" ]; then
    printf '  FAIL  a judge found through CDPATH was run\n'; fail=1
  else
    printf '  ok    …and the judge CDPATH pointed at was never run\n'
  fi
  rm -rf "$repo/gcopy"

  # An exported function named cd and a BASH_ENV file, each running a git from the marker directory:
  # neither may run, and the gate still passes as it did.
  rm -f "$mark"
  out=$(cd "$repo" && env "BASH_FUNC_cd%%=() { \"$mk/git\" --version >/dev/null; builtin cd \"\$@\"; }" \
    PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$self" 2>&1); rc=$?
  check "an exported function named cd is not imported" "$rc" 0 "$out" "$SELF: clean"
  if [ -e "$mark" ]; then
    printf '  FAIL  the exported cd function ran\n'; fail=1
  else
    printf '  ok    …and it never ran\n'
  fi
  printf '"%s/git" --version >/dev/null\n' "$mk" >"$tmp/benv"
  rm -f "$mark"
  out=$(cd "$repo" && BASH_ENV="$tmp/benv" PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$self" 2>&1); rc=$?
  check "a BASH_ENV file is not read" "$rc" 0 "$out" "$SELF: clean"
  if [ -e "$mark" ]; then
    printf '  FAIL  the BASH_ENV file ran\n'; fail=1
  else
    printf '  ok    …and it never ran\n'
  fi

  # What reaches ggshield: BASH_ENV and ENV (a child shell would read them), and the variables that
  # change how a Python program starts. The proxy and CA variables are left alone on purpose.
  : >"$log"
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_LOG="$log" BASH_ENV=/dev/null ENV=/dev/null PYTHONPATH=/x \
    PYTHONHOME=/x PYTHONSTARTUP=/x PYTHONUSERBASE=/x PYTHONINSPECT=1); rc=$?
  check "the scan runs with those variables set" "$rc" 0 "$out" "clean"
  check "BASH_ENV and ENV do not reach ggshield" 0 0 "$(cat "$log")" "bash_env=unset env_file=unset"
  check "PYTHONPATH, PYTHONHOME and PYTHONSTARTUP do not reach ggshield" 0 0 "$(cat "$log")" "pythonpath=unset pythonhome=unset pythonstartup=unset"
  check "PYTHONUSERBASE and PYTHONINSPECT do not reach ggshield" 0 0 "$(cat "$log")" "pythonuserbase=unset pythoninspect=unset"

  # Run from a subdirectory: the tree is also the nearest directory above that holds .git, so a PATH
  # directory at the top is refused before git has been asked anything. A PATH entry that does not
  # exist is read through its existing ancestor: inside the tree, refused.
  mkdir -p "$repo/sub" "$repo/.tools/bin"
  printf '#!/bin/sh\nprintf "%%s\\n" /outside/the/tree\n' >"$repo/.tools/bin/readlink"
  printf '#!/bin/sh\necho ran >>%q\nexec %q "$@"\n' "$mark" "$plant_git" >"$repo/.tools/bin/git"
  chmod +x "$repo/.tools/bin/readlink" "$repo/.tools/bin/git"
  rm -f "$mark"
  out=$(cd "$repo/sub" && PATH="$repo/.tools/bin:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "from a subdirectory, a PATH directory at the top of the tree is refused" "$rc" 1 "$out" "PATH entry"
  if [ -e "$mark" ]; then
    printf '  FAIL  the git inside the tree was run\n'; fail=1
  else
    printf '  ok    …and the git inside the tree never ran\n'
  fi
  rm -rf "$repo/.tools" "$repo/sub"
  out=$(cd "$repo" && PATH="$repo/.not-there/bin:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a PATH entry that does not exist, inside the tree, is refused" "$rc" 1 "$out" "PATH entry"
  not_clean "a missing PATH entry in the tree" "$out"
  # A missing entry is read through its longest existing ancestor, resolved physically: through a link
  # to the tree it is inside the tree, whatever the entry's own spelling says.
  local other="$tmp/other"
  mkrepo "$other"
  ln -s "$repo" "$tmp/lnk-in"
  ln -s "$tmp/outside" "$tmp/lnk-out"
  ln -s "$repo/.nope" "$tmp/lnk-dangling"
  mkdir -p "$tmp/outside"
  out=$(cd "$repo" && PATH="$tmp/lnk-in/.not-there/bin:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a missing PATH entry under a link to the tree is refused" "$rc" 1 "$out" "PATH entry .* resolves inside the repository"
  not_clean "a missing PATH entry under a link to the tree" "$out"
  out=$(cd "$repo" && PATH="$tmp/lnk-in/a/b/.not-there/bin:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "…also with several missing components" "$rc" 1 "$out" "PATH entry .* resolves inside the repository"
  out=$(cd "$repo" && PATH="$tmp/lnk-dangling/bin:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a PATH entry through a dangling link is refused" "$rc" 1 "$out" "symlink that leads nowhere"
  not_clean "a PATH entry through a dangling link" "$out"
  out=$(cd "$repo" && PATH="$tmp/lnk-out/.not-there/bin:$tmp/not-there/bin:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "missing PATH entries outside the tree (one under a link to outside it) are allowed" "$rc" 0 "$out" "$SELF: clean"
  rm -f "$tmp/lnk-in" "$tmp/lnk-out" "$tmp/lnk-dangling"

  # GIT_DIR and GIT_WORK_TREE are proven with builtins before any program runs: set, they must name the
  # repository the hook runs in. Each hostile row runs from a directory outside the tree with a git and a
  # readlink planted in a PATH directory inside it; neither may run.
  mkdir -p "$repo/.tools/bin"
  printf '#!/bin/sh\nprintf "%%s\\n" /outside/the/tree\n' >"$repo/.tools/bin/readlink"
  printf '#!/bin/sh\necho ran >>%q\nexec %q "$@"\n' "$mark" "$plant_git" >"$repo/.tools/bin/git"
  chmod +x "$repo/.tools/bin/readlink" "$repo/.tools/bin/git"
  local hostile
  for hostile in "GIT_DIR=$repo/.git GIT_WORK_TREE=$repo" "GIT_DIR=$repo/.git" "GIT_WORK_TREE=$repo"; do
    rm -f "$mark"
    # shellcheck disable=SC2086 # the words of $hostile are the assignments
    out=$(cd "$tmp/outside" && env $hostile PATH="$repo/.tools/bin:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
    check "from outside the tree, '$hostile' is refused" "$rc" 1 "$out" "GIT_DIR or GIT_WORK_TREE is set"
    not_clean "'$hostile' from outside the tree" "$out"
    if [ -e "$mark" ]; then
      printf '  FAIL  the git planted in the tree was run\n'; fail=1
    else
      printf '  ok    …and the git planted in the tree never ran\n'
    fi
  done
  rm -rf "$repo/.tools"
  mkdir -p "$repo/sub"
  out=$(cd "$repo" && env GIT_WORK_TREE="$tmp/outside" PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a GIT_WORK_TREE that is not the tree the hook runs in is refused" "$rc" 1 "$out" "GIT_DIR or GIT_WORK_TREE is set"
  out=$(cd "$repo" && env GIT_DIR="$other/.git" PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a GIT_DIR of another repository is refused" "$rc" 1 "$out" "GIT_DIR or GIT_WORK_TREE is set"
  out=$(cd "$repo" && env GIT_DIR= PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "an empty GIT_DIR is refused" "$rc" 1 "$out" "GIT_DIR or GIT_WORK_TREE is set"
  out=$(cd "$repo" && env GIT_WORK_TREE= PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "an empty GIT_WORK_TREE is refused" "$rc" 1 "$out" "GIT_DIR or GIT_WORK_TREE is set"
  # What a normal run looks like: the variables naming this very repository still pass.
  out=$(cd "$repo" && env GIT_DIR="$repo/.git" GIT_WORK_TREE="$repo" PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "GIT_DIR and GIT_WORK_TREE naming this repository pass, from the top" "$rc" 0 "$out" "$SELF: clean"
  out=$(cd "$repo/sub" && env GIT_DIR="$repo/.git" GIT_WORK_TREE="$repo" PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "…and from a subdirectory" "$rc" 0 "$out" "$SELF: clean"
  rmdir "$repo/sub"
  # A linked worktree: its .git is a file, and git exports GIT_DIR as the directory that file names.
  local lwmain="$tmp/lwmain" lw="$tmp/lw"
  mkrepo "$lwmain"
  git -C "$lwmain" worktree add -q "$lw" -b lwb >/dev/null 2>&1
  printf 'in the worktree\n' >"$lw/lw.txt"
  git -C "$lw" add lw.txt
  mkdir -p "$lw/sub"
  out=$(cd "$lw" && env GIT_DIR="$lwmain/.git/worktrees/lw" PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a linked worktree's GIT_DIR (as git exports it in a hook) passes" "$rc" 0 "$out" "$SELF: clean"
  out=$(cd "$lw/sub" && env GIT_DIR="$lwmain/.git/worktrees/lw" GIT_WORK_TREE="$lw" PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "…also from a subdirectory of it, with its GIT_WORK_TREE" "$rc" 0 "$out" "$SELF: clean"
  out=$(cd "$lw" && env GIT_DIR="$lwmain/.git" PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "the main repository's git directory is not a linked worktree's: refused" "$rc" 1 "$out" "GIT_DIR or GIT_WORK_TREE is set"
  out=$(cd "$tmp/outside" && env GIT_DIR="$repo/.git" GIT_WORK_TREE="$repo" PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a git directory kept apart from its work tree is refused, and the refusal says so" "$rc" 1 "$out" "kept apart from its work tree"
  out=$(cd "$repo" && env GIT_DIR="$other/.git" PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "…also for a GIT_DIR of another repository" "$rc" 1 "$out" "kept apart from its work tree"

  # A control character, a newline above all, in a path the gate reads: command substitution drops a
  # trailing newline, so a name that ends in one was read as the name without it.
  local nl=$'\n' nltree
  nltree="$tmp/nltree"$'\n'
  mkrepo "$nltree"
  mkdir -p "$nltree/.tools/bin"
  printf '#!/bin/sh\necho ran >>%q\nexec %q "$@"\n' "$mark" "$plant_git" >"$nltree/.tools/bin/git"
  chmod +x "$nltree/.tools/bin/git"
  rm -f "$mark"
  out=$(cd "$nltree" && PATH="$nltree/.tools/bin:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a tree whose directory name ends in a newline is refused, with a git planted in it" "$rc" 1 "$out" "control character"
  not_clean "a tree named with a newline, with a git in its .tools/bin" "$out"
  if [ -e "$mark" ]; then
    printf '  FAIL  the git planted in the tree named with a newline was run\n'; fail=1
  else
    printf '  ok    …and the git planted in it never ran\n'
  fi
  out=$(cd "$nltree" && PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a tree named with a newline is refused even with a clean PATH" "$rc" 1 "$out" "control character"
  not_clean "a tree named with a newline" "$out"
  out=$(cd "$nltree" && PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 GIT_DIR="$nltree/.git" "$BASH" "$self" 2>&1); rc=$?
  check "…and with GIT_DIR set" "$rc" 1 "$out" "control character"
  mkdir -p "$tmp/pathnl"
  out=$(cd "$repo" && PATH="$stub_dir:$tmp/pathnl$nl:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a PATH entry with a newline in it is refused" "$rc" 1 "$out" "control character"
  not_clean "a PATH entry with a newline in it" "$out"
  cp -R "$repo/.git" "$repo/.git$nl"
  out=$(cd "$repo" && env GIT_DIR="$repo/.git$nl" PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a GIT_DIR ending in a newline, with a copied directory of that name, is refused" "$rc" 1 "$out" "control character"
  not_clean "a GIT_DIR ending in a newline" "$out"
  rm -rf "$repo/.git$nl"
  out=$(cd "$repo" && env GIT_WORK_TREE="$repo$nl" PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a GIT_WORK_TREE ending in a newline is refused" "$rc" 1 "$out" "control character"
  # A value of - : bash's cd reads it as $OLDPWD (inherited from the environment), git as a directory of that name.
  cp -R "$repo/.git" "$repo/-"
  out=$(cd "$repo" && env OLDPWD="$repo/.git" GIT_DIR=- PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a GIT_DIR of - is not read as OLDPWD: refused" "$rc" 1 "$out" "GIT_DIR or GIT_WORK_TREE is set"
  not_clean "a GIT_DIR of - with OLDPWD set to the git directory" "$out"
  out=$(cd "$repo" && env OLDPWD="$repo" GIT_WORK_TREE=- PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a GIT_WORK_TREE of - is not read as OLDPWD: refused" "$rc" 1 "$out" "GIT_DIR or GIT_WORK_TREE is set"
  rm -rf "$repo/-"
  mkdir -p "$tmp/gf"
  printf 'gitdir: %s\n/second/line\n' "$lwmain/.git/worktrees/lw" >"$tmp/gf/.git"
  out=$(cd "$tmp/gf" && env GIT_DIR="$lwmain/.git/worktrees/lw" PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 "$BASH" "$self" 2>&1); rc=$?
  check "a .git file with more than its one gitdir line is refused" "$rc" 1 "$out" "control character"

  # Configuration handed to git through the environment or the repository can run a program inside the
  # gate's own git calls (core.fsmonitor): the gate turns it off for them and drops a GIT_CONFIG_COUNT
  # group; the scan never sees either, and `git -c` (GIT_CONFIG_PARAMETERS) keeps working in a hook.
  printf '#!/bin/sh\necho ran >>%q\nexit 1\n' "$tmp/fsm-ran" >"$tmp/fsm"
  chmod +x "$tmp/fsm"
  rm -f "$tmp/fsm-ran"
  : >"$log"
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_LOG="$log" GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0="$tmp/fsm"); rc=$?
  check "a GIT_CONFIG_COUNT group setting core.fsmonitor does not stop a clean scan" "$rc" 0 "$out" "$SELF: clean"
  if [ -e "$tmp/fsm-ran" ]; then
    printf '  FAIL  the program named by core.fsmonitor was run\n'; fail=1
  else
    printf '  ok    …and the program it names never ran\n'
  fi
  check "…and the scan sees no GIT_CONFIG_COUNT group but the gate's own, turning core.fsmonitor off" 0 0 "$(cat "$log")" "git_config=[0-9][0-9]*/core.fsmonitor/false"
  out=$(gate GGSHIELD_STUB_RC=0 GIT_CONFIG_PARAMETERS="'core.fsmonitor'='$tmp/fsm'"); rc=$?
  check "core.fsmonitor in GIT_CONFIG_PARAMETERS does not stop a clean scan" "$rc" 0 "$out" "$SELF: clean"
  if [ -e "$tmp/fsm-ran" ]; then
    printf '  FAIL  the program named in GIT_CONFIG_PARAMETERS was run\n'; fail=1
  else
    printf '  ok    …and the program it names never ran\n'
  fi
  git -C "$repo" config core.fsmonitor "$tmp/fsm"
  out=$(gate GGSHIELD_STUB_RC=0); rc=$?
  git -C "$repo" config --unset core.fsmonitor
  check "core.fsmonitor in the repository's config does not stop a clean scan" "$rc" 0 "$out" "$SELF: clean"
  if [ -e "$tmp/fsm-ran" ]; then
    printf '  FAIL  the program named in the repository config was run\n'; fail=1
  else
    printf '  ok    …and the program it names never ran\n'
  fi
  : >"$log"
  out=$(gate GGSHIELD_STUB_RC=0 GGSHIELD_STUB_LOG="$log" GIT_CONFIG_PARAMETERS="'user.name'='t'"); rc=$?
  check "a harmless GIT_CONFIG_PARAMETERS still passes" "$rc" 0 "$out" "$SELF: clean"
  check "…and does not reach the scan" 0 0 "$(cat "$log")" "params=unset"

  # Settings and attributes that make git run a program on content or hide it from ggshield's own
  # `git show` (the stub's show mode reads the commit as ggshield does and reports a finding exactly when
  # the marker line reaches the patch). Each case stages the marker; before the fix each one passed it
  # clean, ran a program, or both.
  local hp="$tmp/hide" hran="$tmp/hide-ran" mk
  printf '#!/bin/sh\necho ran >>%q\necho REPLACED\n' "$hran" >"$hp"
  chmod +x "$hp"
  # A long-running filter driver that speaks git's filter protocol, passes content through and notes it ran.
  cat >"$tmp/proto-filter" <<PROTO
#!/usr/bin/python3 -I
import sys
open("$hran", "a").write("ran\\n")
i, o = sys.stdin.buffer, sys.stdout.buffer
def rd():
    n = i.read(4)
    if not n: sys.exit(0)
    if n == b"0000": return None
    return i.read(int(n, 16) - 4)
def wr(b): o.write(b"%04x" % (len(b) + 4) + b)
def flush(): o.write(b"0000"); o.flush()
while rd() is not None: pass
wr(b"git-filter-server\\n"); wr(b"version=2\\n"); flush()
while rd() is not None: pass
wr(b"capability=clean\\n"); wr(b"capability=smudge\\n"); flush()
while True:
    while rd() is not None: pass
    data = b""
    while (c := rd()) is not None: data += c
    wr(b"status=success\\n"); flush()
    if data: wr(data)
    flush(); flush()
PROTO
  chmod +x "$tmp/proto-filter"
  # A stand-in for the git-lfs program (the stock lfs settings name it), noting its own runs apart from $hran.
  sed "s#$hran#$tmp/lfs-ran#" "$tmp/proto-filter" >"$tmp/proto-filter-lfs"
  printf '#!/bin/sh\ncase "$1" in filter-process) exec %q ;; *) exec cat ;; esac\n' "$tmp/proto-filter-lfs" >"$stub_dir/git-lfs"
  chmod +x "$tmp/proto-filter-lfs" "$stub_dir/git-lfs"
  mkmark() { # mkmark <dir>: mkrepo, plus a staged file holding the marker line
    mkrepo "$1"
    printf 'GATE_STUB_MARKER\n' >"$1/marked.txt"
    git -C "$1" add marked.txt
  }
  mgate() { # mgate <dir> [NAME=value...]: the gate in <dir> with the stub reading the commit as ggshield does
    local d=$1
    shift
    (cd "$d" && env PATH="$stub_dir:$PATH" GGSHIELD_STUB_MODE=show "$@" bash "$self" 2>&1)
  }
  refused_hidden() { # refused_hidden <name> <rc> <output> <must-contain>: refused before any program ran
    check "$1: REFUSED, not scanned" "$2" 1 "$3" "$4"
    if grep -q "$SELF: clean" <<< "$3" || [ -e "$hran" ]; then
      printf '  FAIL  %s: passed clean, or the program ran\n' "$1"; fail=1
    else
      printf '  ok    …and nothing passed clean and no program ran\n'
    fi
    rm -f "$hran"
  }
  local setting="git is set to run a program on file content or to hide it" asbin="told to show a staged file as binary"
  # The positive control: the marker reaches the stub's patch and is refused as a finding.
  mk="$tmp/mk0"; mkmark "$mk"
  out=$(mgate "$mk"); rc=$?
  check "the stub reading as ggshield does sees a staged marker (positive control)" "$rc" 1 "$out" "found a secret"
  mk="$tmp/mk1"; mkmark "$mk"
  git -C "$mk" config diff.hide.textconv "$hp"
  echo '* diff=hide' >"$mk/.git/info/attributes"
  out=$(mgate "$mk"); rc=$?
  refused_hidden "diff.<x>.textconv in .git/config with info/attributes" "$rc" "$out" "$setting"
  check "…and the refusal names the key" "$rc" 1 "$out" "diff.hide.textconv"
  mk="$tmp/mk2"; mkmark "$mk"
  git -C "$mk" config diff.hide.textconv "$hp"
  echo '* diff=hide' >"$mk/.gitattributes"
  git -C "$mk" add .gitattributes
  out=$(mgate "$mk"); rc=$?
  refused_hidden "diff.<x>.textconv with only a staged .gitattributes" "$rc" "$out" "$setting"
  mk="$tmp/mk3"; mkmark "$mk"
  printf '[diff "hide"]\n\ttextconv = %s\n' "$hp" >"$tmp/gg-git-config"
  echo '* diff=hide' >"$mk/.git/info/attributes"
  : >"$log"
  out=$(mgate "$mk" GG_GIT_CONFIG="$tmp/gg-git-config" GGSHIELD_STUB_LOG="$log"); rc=$?
  check "textconv through GG_GIT_CONFIG does not reach ggshield's git: the marker is seen" "$rc" 1 "$out" "found a secret"
  check "…GG_GIT_CONFIG is unset for the scan" 0 0 "$(cat "$log")" "gg_git_config=unset"
  [ ! -e "$hran" ] || { printf '  FAIL  the textconv program named in GG_GIT_CONFIG ran\n'; fail=1; }
  rm -f "$hran"
  mk="$tmp/mk4"; mkmark "$mk"
  mkdir -p "$tmp/xdg/git"
  echo '* diff=hide' >"$tmp/xdg/git/attributes"
  git -C "$mk" config diff.hide.textconv "$hp"
  out=$(mgate "$mk" XDG_CONFIG_HOME="$tmp/xdg"); rc=$?
  refused_hidden "the XDG attributes file with a textconv driver" "$rc" "$out" "$setting"
  mk="$tmp/mk4b"; mkmark "$mk"
  printf '[diff "hide"]\n\ttextconv = %s\n' "$hp" >"$tmp/xdg/git/config"
  out=$(cd "$mk" && env -u GIT_CONFIG_GLOBAL PATH="$stub_dir:$PATH" GGSHIELD_STUB_MODE=show XDG_CONFIG_HOME="$tmp/xdg" bash "$self" 2>&1); rc=$?
  refused_hidden "a textconv driver in the XDG config file, read as the gate's git reads it" "$rc" "$out" "$setting"
  rm -f "$tmp/xdg/git/config"
  mk="$tmp/mk5"; mkmark "$mk"
  git -C "$mk" config filter.pf.process "$tmp/proto-filter"
  echo '* filter=pf' >"$mk/.git/info/attributes"
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a long-running filter driver (filter.<x>.process) that speaks the protocol" "$rc" "$out" "$setting"
  check "…and the refusal names the key" "$rc" 1 "$out" "filter.pf.process"
  mk="$tmp/mk5b"; mkmark "$mk"
  git -C "$mk" config filter.cs.clean "$hp"
  git -C "$mk" config filter.cs.smudge "$hp"
  echo '* filter=cs' >"$mk/.git/info/attributes"
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a clean/smudge filter driver" "$rc" "$out" "$setting"
  mk="$tmp/mk6"; mkmark "$mk"
  git -C "$mk" config diff.hide.command "$hp"
  echo '* diff=hide' >"$mk/.git/info/attributes"
  out=$(mgate "$mk"); rc=$?
  refused_hidden "diff.<x>.command" "$rc" "$out" "$setting"
  mk="$tmp/mk7"; mkmark "$mk"
  git -C "$mk" config diff.hide.binary true
  echo '* diff=hide' >"$mk/.git/info/attributes"
  out=$(mgate "$mk"); rc=$?
  refused_hidden "diff.<x>.binary" "$rc" "$out" "$setting"
  mk="$tmp/mk8"; mkmark "$mk"
  echo 'marked.txt -diff' >"$mk/.git/info/attributes"
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a -diff attribute on a changed path, no setting" "$rc" "$out" "$asbin"
  mk="$tmp/mk9"; mkmark "$mk"
  echo '*.txt binary' >"$mk/.gitattributes"
  out=$(mgate "$mk"); rc=$?
  refused_hidden "the binary attribute in an unstaged .gitattributes" "$rc" "$out" "$asbin"
  mk="$tmp/mk9b"; mkmark "$mk"
  mkdir -p "$mk/sub"
  echo 'marked.txt -diff' >"$mk/.git/info/attributes"
  # From a subdirectory the index is named as git names it in a hook, absolute (a relative --git-path is
  # not read from there; the gate then refuses, as it always has).
  out=$(mgate "$mk/sub" GIT_INDEX_FILE="$mk/.git/index"); rc=$?
  refused_hidden "a -diff attribute, the gate run from a subdirectory" "$rc" "$out" "$asbin"
  mk="$tmp/mk9c"; mkmark "$mk"
  echo 'unchanged.txt -diff' >"$mk/.git/info/attributes"
  out=$(mgate "$mk"); rc=$?
  check "a -diff attribute on paths not changed is not refused (the marker is seen)" "$rc" 1 "$out" "found a secret"
  mk="$tmp/mk10"; mkmark "$mk"
  local attrtree
  attrtree=$(cd "$mk" && b=$(printf '* -diff\n' | git hash-object -w --stdin) \
    && printf '100644 blob %s\t.gitattributes\n' "$b" | git mktree)
  git -C "$mk" config attr.tree "$attrtree"
  out=$(mgate "$mk"); rc=$?
  refused_hidden "attr.tree" "$rc" "$out" "$setting"
  mk="$tmp/mk11"; mkmark "$mk"
  attrtree=$(cd "$mk" && b=$(printf '* -diff\n' | git hash-object -w --stdin) \
    && printf '100644 blob %s\t.gitattributes\n' "$b" | git mktree)
  : >"$log"
  out=$(mgate "$mk" GIT_ATTR_SOURCE="$attrtree" GGSHIELD_STUB_LOG="$log"); rc=$?
  check "GIT_ATTR_SOURCE naming a -diff tree does not reach ggshield's git: the marker is seen" "$rc" 1 "$out" "found a secret"
  check "…GIT_ATTR_SOURCE is unset for the scan" 0 0 "$(cat "$log")" "attr_source=unset"
  mk="$tmp/mk12"; mkmark "$mk"
  git -C "$mk" config core.bigFileThreshold 1
  out=$(mgate "$mk"); rc=$?
  check "core.bigFileThreshold=1 is pinned for ggshield's git: the marker is seen" "$rc" 1 "$out" "found a secret"
  mk="$tmp/mk13"; mkmark "$mk"
  mkdir -p "$mk/sub"
  git -C "$mk" config diff.relative true
  out=$(mgate "$mk/sub" GIT_INDEX_FILE="$mk/.git/index"); rc=$?
  check "diff.relative from a subdirectory is pinned off: the marker is seen" "$rc" 1 "$out" "found a secret"
  mk="$tmp/mk13b"; mkmark "$mk"
  mkdir -p "$mk/sub"
  out=$(mgate "$mk/sub" GIT_INDEX_FILE="$mk/.git/index"); rc=$?
  check "from a subdirectory the scan still reads a file at the top (it runs from the top): the marker is seen" "$rc" 1 "$out" "found a secret"
  # A replace ref swapping HEAD for a commit that already holds the staged tree: read through it, the change is empty.
  mk="$tmp/mk14"; mkmark "$mk"
  local swapc
  swapc=$(cd "$mk" && GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@localhost GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@localhost \
    git commit-tree --no-gpg-sign -m swap "$(git write-tree)" </dev/null)
  git -C "$mk" replace HEAD "$swapc"
  : >"$log"
  out=$(mgate "$mk" GGSHIELD_STUB_LOG="$log"); rc=$?
  check "a replace ref for HEAD is ignored by the gate's and ggshield's git: the marker is seen" "$rc" 1 "$out" "found a secret"
  check "…GIT_NO_REPLACE_OBJECTS=1 for the scan" 0 0 "$(cat "$log")" "no_replace=1"
  # A graft giving the scanned commit a parent that already holds its tree. The commit is predictable
  # because the date is fixed here, and made exactly as the gate makes it.
  mk="$tmp/mk15"; mkmark "$mk"
  local gc gp
  gc=$(cd "$mk" && GIT_AUTHOR_DATE='2026-01-01T00:00:00Z' GIT_COMMITTER_DATE='2026-01-01T00:00:00Z' \
    GIT_AUTHOR_NAME="$SELF" GIT_AUTHOR_EMAIL="$SELF@localhost" GIT_COMMITTER_NAME="$SELF" GIT_COMMITTER_EMAIL="$SELF@localhost" \
    git commit-tree --no-gpg-sign -p "$(git rev-parse HEAD)" -m "staged change, for the secret scan" "$(git write-tree)" </dev/null)
  gp=$(cd "$mk" && GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@localhost GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@localhost \
    git commit-tree --no-gpg-sign -m graft "$(git write-tree)" </dev/null)
  printf '%s %s\n' "$gc" "$gp" >"$mk/.git/info/grafts"
  : >"$log"
  out=$(mgate "$mk" GIT_AUTHOR_DATE='2026-01-01T00:00:00Z' GIT_COMMITTER_DATE='2026-01-01T00:00:00Z' GGSHIELD_STUB_LOG="$log"); rc=$?
  check "a graft on the scanned commit is ignored by ggshield's git: the marker is seen" "$rc" 1 "$out" "found a secret"
  check "…GIT_GRAFT_FILE=/dev/null for the scan" 0 0 "$(cat "$log")" "graft_file=/dev/null"
  # Harmless settings of the same sections still pass: a funcname and an algorithm, with and without the marker.
  mk="$tmp/mk16"; mkmark "$mk"
  git -C "$mk" config diff.hide.xfuncname '^(.*)$'
  git -C "$mk" config diff.algorithm histogram
  git -C "$mk" config filter.none.required false
  echo '* diff=hide' >"$mk/.git/info/attributes"
  out=$(mgate "$mk"); rc=$?
  check "harmless diff and filter settings do not stop a scan (the marker is seen)" "$rc" 1 "$out" "found a secret"
  mk="$tmp/mk17"; mkrepo "$mk"
  git -C "$mk" config diff.hide.xfuncname '^(.*)$'
  git -C "$mk" config diff.algorithm histogram
  echo '* diff=hide' >"$mk/.git/info/attributes"
  out=$(mgate "$mk"); rc=$?
  check "harmless diff settings with nothing secret staged pass clean" "$rc" 0 "$out" "$SELF: clean"

  # git-lfs's stock filter.lfs.* values are the one filter driver let through; any change to them, an extra
  # key, another driver, or a filter attribute on a changed path still refuses.
  lfsstock() { # lfsstock <dir>: the four stock values git-lfs's package writes
    git -C "$1" config filter.lfs.clean 'git-lfs clean -- %f'
    git -C "$1" config filter.lfs.smudge 'git-lfs smudge -- %f'
    git -C "$1" config filter.lfs.process 'git-lfs filter-process'
    git -C "$1" config filter.lfs.required true
  }
  local fattr="has a filter attribute"
  mk="$tmp/lfs1"; mkmark "$mk"; lfsstock "$mk"
  out=$(mgate "$mk"); rc=$?
  check "stock filter.lfs.* values pass the settings check (the marker is seen)" "$rc" 1 "$out" "found a secret"
  mk="$tmp/lfs1b"; mkrepo "$mk"; lfsstock "$mk"
  out=$(mgate "$mk"); rc=$?
  check "stock filter.lfs.* values with nothing secret staged pass clean" "$rc" 0 "$out" "$SELF: clean"
  mk="$tmp/lfs1c"; mkmark "$mk"; lfsstock "$mk"
  echo '*.dat -filter' >"$mk/.git/info/attributes"
  out=$(mgate "$mk"); rc=$?
  check "stock filter.lfs.* values and an attribute on a path not changed pass (the marker is seen)" "$rc" 1 "$out" "found a secret"
  mk="$tmp/lfs2"; mkmark "$mk"; lfsstock "$mk"
  git -C "$mk" config filter.lfs.clean 'git-lfs clean %f'
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a changed filter.lfs.clean value" "$rc" "$out" "$setting"
  check "…and the refusal names the key, not the value" "$rc" 1 "$out" "filter.lfs.clean"
  mk="$tmp/lfs2b"; mkmark "$mk"; lfsstock "$mk"
  git -C "$mk" config filter.lfs.required false
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a changed filter.lfs.required value" "$rc" "$out" "filter.lfs.required"
  mk="$tmp/lfs3"; mkmark "$mk"; lfsstock "$mk"
  git -C "$mk" config filter.lfs.extra x
  out=$(mgate "$mk"); rc=$?
  refused_hidden "an extra key under filter.lfs" "$rc" "$out" "filter.lfs.extra"
  mk="$tmp/lfs4"; mkmark "$mk"; lfsstock "$mk"
  git -C "$mk" config --add filter.lfs.clean 'git-lfs clean -- %f'
  out=$(mgate "$mk"); rc=$?
  check "an identical stock value repeated passes (the marker is seen)" "$rc" 1 "$out" "found a secret"
  mk="$tmp/lfs4b"; mkmark "$mk"; lfsstock "$mk"
  git -C "$mk" config --add filter.lfs.clean 'git-lfs clean %f'
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a differing repeat of filter.lfs.clean" "$rc" "$out" "filter.lfs.clean"
  mk="$tmp/lfs4c"; mkmark "$mk"; lfsstock "$mk"
  git -C "$mk" config lfs.extension.ext.clean 'x %f'
  out=$(mgate "$mk"); rc=$?
  refused_hidden "an lfs.extension setting" "$rc" "$out" "lfs.extension.ext.clean"
  mk="$tmp/lfs4d"; mkmark "$mk"; lfsstock "$mk"
  git -C "$mk" config lfs.url 'https://example.invalid/lfs'
  out=$(mgate "$mk"); rc=$?
  check "another lfs.* setting (lfs.url) is not refused (the marker is seen)" "$rc" 1 "$out" "found a secret"
  mk="$tmp/lfs5"; mkmark "$mk"; lfsstock "$mk"
  git -C "$mk" config filter.other.clean 'git-lfs clean -- %f'
  out=$(mgate "$mk"); rc=$?
  refused_hidden "another filter driver with the lfs value" "$rc" "$out" "filter.other.clean"
  mk="$tmp/lfs5b"; mkmark "$mk"; lfsstock "$mk"
  git -C "$mk" config filter.LFS.clean 'git-lfs clean -- %f'
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a filter driver whose name differs from lfs only in case" "$rc" "$out" "filter.LFS.clean"
  mk="$tmp/lfs5c"; mkmark "$mk"; lfsstock "$mk"
  git -C "$mk" config -f "$tmp/lfs-inc" filter.lfs.clean 'git-lfs clean -- %f'
  git -C "$mk" config include.path "$tmp/lfs-inc"
  out=$(mgate "$mk"); rc=$?
  check "an identical stock value repeated in an included file passes (the marker is seen)" "$rc" 1 "$out" "found a secret"
  git -C "$mk" config -f "$tmp/lfs-inc" filter.lfs.clean 'git-lfs clean %f'
  out=$(mgate "$mk"); rc=$?
  refused_hidden "filter.lfs.clean differing in an included file" "$rc" "$out" "filter.lfs.clean"
  rm -f "$tmp/lfs-inc"
  # With the lfs driver tolerated, a filter attribute on a changed path refuses, from any source.
  mk="$tmp/lfs6"; mkmark "$mk"; lfsstock "$mk"
  echo 'marked.txt filter=lfs' >"$mk/.git/info/attributes"
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a filter attribute in info/attributes" "$rc" "$out" "$fattr"
  mk="$tmp/lfs7"; mkmark "$mk"; lfsstock "$mk"
  echo 'marked.txt filter=lfs' >"$mk/.gitattributes"
  PATH="$stub_dir:$PATH" git -C "$mk" add .gitattributes
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a filter attribute in a staged .gitattributes" "$rc" "$out" "$fattr"
  mk="$tmp/lfs7b"; mkmark "$mk"; lfsstock "$mk"
  echo 'marked.txt filter=lfs' >"$mk/.gitattributes"
  PATH="$stub_dir:$PATH" git -C "$mk" add .gitattributes
  rm -f "$mk/.gitattributes"
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a filter attribute in a staged .gitattributes deleted from the working tree" "$rc" "$out" "$fattr"
  mk="$tmp/lfs7c"; mkmark "$mk"; lfsstock "$mk"
  echo 'marked.txt filter=lfs' >"$mk/.gitattributes"
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a filter attribute in an unstaged .gitattributes" "$rc" "$out" "$fattr"
  mk="$tmp/lfs8"; mkmark "$mk"; lfsstock "$mk"
  echo 'marked.txt filter=lfs' >"$tmp/lfs-attrs"
  git -C "$mk" config core.attributesFile "$tmp/lfs-attrs"
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a filter attribute in core.attributesFile" "$rc" "$out" "$fattr"
  mk="$tmp/lfs9"; mkmark "$mk"; lfsstock "$mk"
  mkdir -p "$tmp/xdg-lfs/git"
  echo 'marked.txt filter=lfs' >"$tmp/xdg-lfs/git/attributes"
  out=$(mgate "$mk" XDG_CONFIG_HOME="$tmp/xdg-lfs"); rc=$?
  refused_hidden "a filter attribute in the XDG attributes file" "$rc" "$out" "$fattr"
  mk="$tmp/lfs10"; mkmark "$mk"; lfsstock "$mk"
  attrtree=$(cd "$mk" && b=$(printf 'marked.txt filter=lfs\n' | git hash-object -w --stdin) \
    && printf '100644 blob %s\t.gitattributes\n' "$b" | git mktree)
  git -C "$mk" config attr.tree "$attrtree"
  out=$(mgate "$mk"); rc=$?
  refused_hidden "attr.tree with the lfs settings" "$rc" "$out" "$setting"
  mk="$tmp/lfs11"; mkmark "$mk"; lfsstock "$mk"
  echo 'other.txt filter=lfs' >"$mk/.git/info/attributes"
  out=$(mgate "$mk"); rc=$?
  check "a filter attribute on a path not changed is not refused (the marker is seen)" "$rc" 1 "$out" "found a secret"
  # An LFS pointer staged with no attribute left (git lfs untrack after the add) refuses; other small files pass.
  local ptrmsg="are git-lfs pointers"
  mk="$tmp/ptr1"; mkrepo "$mk"
  printf 'version https://git-lfs.github.com/spec/v1\noid sha256:%064d\nsize 12\n' 0 >"$mk/secrets.env"
  git -C "$mk" add secrets.env
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a staged LFS pointer with no filter attribute" "$rc" "$out" "$ptrmsg"
  mk="$tmp/ptr2"; mkrepo "$mk"
  printf 'version https://hawser.github.com/spec/v1\noid sha256:%064d\nsize 12\n' 0 >"$mk/old.env"
  git -C "$mk" add old.env
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a staged legacy (hawser) LFS pointer" "$rc" "$out" "$ptrmsg"
  mk="$tmp/ptr2b"; mkrepo "$mk"
  printf 'version http://git-media.io/v/2\noid sha256:%064d\nsize 12\n' 0 >"$mk/media.env"
  git -C "$mk" add media.env
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a staged legacy (git-media) LFS pointer" "$rc" "$out" "$ptrmsg"
  mk="$tmp/ptr2c"; mkrepo "$mk"
  printf '\n  \nversion https://git-lfs.github.com/spec/v1\noid sha256:%064d\nsize 12\n' 0 >"$mk/lead.env"
  git -C "$mk" add lead.env
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a staged LFS pointer after a blank line" "$rc" "$out" "$ptrmsg"
  mk="$tmp/ptr2d"; mkrepo "$mk"
  printf 'version https://git-lfs.github.com/spec/v1\r\noid sha256:%064d\r\nsize 12\r\n' 0 >"$mk/crlf.env"
  git -C "$mk" add crlf.env
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a staged LFS pointer with CRLF line ends" "$rc" "$out" "$ptrmsg"
  mk="$tmp/ptr2e"; mkrepo "$mk"
  printf '\xc2\xa0version https://git-lfs.github.com/spec/v1\noid sha256:%064d\nsize 12\n' 0 >"$mk/nbsp.env"
  git -C "$mk" add nbsp.env
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a staged LFS pointer after a no-break space" "$rc" "$out" "$ptrmsg"
  mk="$tmp/ptr2f"; mkrepo "$mk"
  printf '\xc2\x85version https://git-lfs.github.com/spec/v1\noid sha256:%064d\nsize 12\n' 0 >"$mk/nel.env"
  git -C "$mk" add nel.env
  out=$(mgate "$mk"); rc=$?
  refused_hidden "a staged LFS pointer after a NEL character" "$rc" "$out" "$ptrmsg"
  mk="$tmp/ptr3"; mkrepo "$mk"
  printf 'version 1\nnot a pointer\n' >"$mk/plain.txt"
  printf 'version https://git-lfs.github.com/spec/v1x\n' >"$mk/near.txt"
  printf 'note\nversion https://git-lfs.github.com/spec/v1\n' >"$mk/second.txt"
  git -C "$mk" add plain.txt near.txt second.txt
  out=$(mgate "$mk"); rc=$?
  check "small files that are not pointers pass clean" "$rc" 0 "$out" "$SELF: clean"
  mk="$tmp/ptr4"; mkrepo "$mk"
  { printf 'version https://git-lfs.github.com/spec/v1\n'; head -c 2000 /dev/zero | tr '\0' x; } >"$mk/big.txt"
  git -C "$mk" add big.txt
  out=$(mgate "$mk"); rc=$?
  check "a file over 1024 bytes that starts with the pointer line passes clean" "$rc" 0 "$out" "$SELF: clean"
  mk="$tmp/ptr5"; mkrepo "$mk"
  git -C "$mk" rm -q --cached kept.txt
  out=$(mgate "$mk"); rc=$?
  check "a deletion and an addition with no pointer pass clean" "$rc" 0 "$out" "$SELF: clean"

  # GIT_CONFIG makes `git config` read that one file and nothing else, while show and write-tree ignore
  # it: an empty file there hid a textconv driver from the settings check, and the program ran.
  mk="$tmp/mk20"; mkmark "$mk"
  git -C "$mk" config diff.hide.textconv "$hp"
  echo '* diff=hide' >"$mk/.git/info/attributes"
  : >"$tmp/empty-git-config"
  out=$(mgate "$mk" GIT_CONFIG="$tmp/empty-git-config"); rc=$?
  refused_hidden "a textconv driver with GIT_CONFIG naming an empty file" "$rc" "$out" "$setting"
  mk="$tmp/mk20b"; mkmark "$mk"
  : >"$log"
  out=$(mgate "$mk" GIT_CONFIG="$tmp/empty-git-config" GGSHIELD_STUB_LOG="$log"); rc=$?
  check "GIT_CONFIG does not reach ggshield's git: the marker is seen" "$rc" 1 "$out" "found a secret"
  check "…GIT_CONFIG is unset for the scan" 0 0 "$(cat "$log")" "config_file=unset"
  # log.showRoot off: git show prints no file for a commit without a parent.
  mk="$tmp/mk21"; mkdir -p "$mk"; git -C "$mk" init -q 2>/dev/null
  printf 'GATE_STUB_MARKER\n' >"$mk/marked.txt"
  git -C "$mk" add marked.txt
  git -C "$mk" config log.showRoot false
  out=$(mgate "$mk"); rc=$?
  check "log.showRoot=false before the first commit is pinned on: the marker is seen" "$rc" 1 "$out" "found a secret"
  # The shallow list: the commit made for the scan is predictable (the dates are fixed here, and it is made
  # exactly as the gate makes it), and a list naming it makes git show it as a first commit.
  predc() { # predc <dir>: the id the gate's commit will have with the fixed dates
    (cd "$1" && p=$(git rev-parse -q --verify 'HEAD^{commit}') ; GIT_AUTHOR_DATE='2026-01-01T00:00:00Z' GIT_COMMITTER_DATE='2026-01-01T00:00:00Z' \
      GIT_AUTHOR_NAME="$SELF" GIT_AUTHOR_EMAIL="$SELF@localhost" GIT_COMMITTER_NAME="$SELF" GIT_COMMITTER_EMAIL="$SELF@localhost" \
      git commit-tree --no-gpg-sign ${p:+-p "$p"} -m "staged change, for the secret scan" "$(git write-tree)" </dev/null)
  }
  dgate() { mgate "$@" GIT_AUTHOR_DATE='2026-01-01T00:00:00Z' GIT_COMMITTER_DATE='2026-01-01T00:00:00Z'; }
  mk="$tmp/mk22"; mkmark "$mk"
  git -C "$mk" config log.showRoot false
  gc=$(predc "$mk")
  printf '%s\n' "$gc" >"$mk/.git/shallow"
  out=$(dgate "$mk"); rc=$?
  check "a shallow list naming the commit made for the scan is refused" "$rc" 1 "$out" "shallow list"
  not_clean "a shallow list naming the commit made for the scan" "$out"
  printf '%s\r\n' "${gc^^}" >"$mk/.git/shallow"
  out=$(dgate "$mk"); rc=$?
  check "…and so is its id in upper case ending in a CR, as git reads that line" "$rc" 1 "$out" "shallow list"
  rm -f "$mk/.git/shallow"
  printf '%s\n' "$gc" >"$tmp/other-shallow"
  : >"$log"
  out=$(dgate "$mk" GIT_SHALLOW_FILE="$tmp/other-shallow" GGSHIELD_STUB_LOG="$log"); rc=$?
  check "GIT_SHALLOW_FILE naming the commit does not reach ggshield's git: the marker is seen" "$rc" 1 "$out" "found a secret"
  check "…GIT_SHALLOW_FILE is unset for the scan" 0 0 "$(cat "$log")" "shallow_file=unset"
  git -C "$mk" rev-parse HEAD >"$mk/.git/shallow"
  out=$(dgate "$mk"); rc=$?
  check "a shallow HEAD (a --depth clone) is not refused, and the change is shown in full: the marker is seen" "$rc" 1 "$out" "found a secret"
  rm -f "$mk/.git/shallow"
  # Notes are not shown, whichever ref holds them: the old fixed ref, the default, or a configured one.
  mk="$tmp/mk23"; mkmark "$mk"
  gc=$(predc "$mk")
  git -C "$mk" -c user.name=t -c user.email=t@localhost notes add -m GATE_NOTE_TEXT "$gc"
  git -C "$mk" -c user.name=t -c user.email=t@localhost notes --ref=refs/notes/ggshield-gate-shows-no-notes add -m GATE_NOTE_TEXT "$gc"
  git -C "$mk" -c user.name=t -c user.email=t@localhost notes --ref=refs/notes/mine add -m GATE_NOTE_TEXT "$gc"
  git -C "$mk" config core.notesRef refs/notes/mine
  git -C "$mk" config notes.displayRef 'refs/notes/*'
  : >"$log"
  out=$(dgate "$mk" GIT_NOTES_REF=refs/notes/ggshield-gate-shows-no-notes GGSHIELD_STUB_LOG="$log"); rc=$?
  check "notes on the commit made for the scan: the marker is seen" "$rc" 1 "$out" "found a secret"
  check "…and no note reaches ggshield's git show" 0 0 "$(cat "$log")" "^notes_seen=no$"
  check "…though each note is there to be shown (the case is not empty)" 0 0 "$(git -C "$mk" notes --ref=refs/notes/mine show "$gc" 2>&1; git -C "$mk" notes show "$gc" 2>&1)" "GATE_NOTE_TEXT"
  # A staged file named with pathspec magic (":!x"): ggshield 1.56.1's header parse dies on a path that
  # starts with ":" (measured: "not enough values to unpack"), which the stub repeats, so it refuses; and
  # ggshield's git reads pathspecs literally besides.
  mk="$tmp/mk24"; mkmark "$mk"
  printf 'x\n' >"$mk/:!marked.txt"
  GIT_LITERAL_PATHSPECS=1 git -C "$mk" add ':!marked.txt'
  : >"$log"
  out=$(dgate "$mk" GGSHIELD_STUB_LOG="$log"); rc=$?
  check "a staged file named \":!marked.txt\" beside the marker does not pass" "$rc" 1 "$out" "REFUSED"
  not_clean "a staged file named \":!marked.txt\"" "$out"
  check "…GIT_LITERAL_PATHSPECS=1 for the scan" 0 0 "$(cat "$log")" "literal=1"
  # Read again after the scan: a setting or an attribute changed while ggshield ran refuses a clean scan.
  mk="$tmp/mk25"; mkrepo "$mk"
  out=$(mgate "$mk" GGSHIELD_STUB_DURING="git config diff.late.xfuncname x"); rc=$?
  check "a diff setting added while the scan ran refuses" "$rc" 1 "$out" "changed while the scan ran"
  not_clean "a diff setting added while the scan ran" "$out"
  mk="$tmp/mk25b"; mkrepo "$mk"
  out=$(mgate "$mk" GGSHIELD_STUB_DURING="echo '* diff=late' >>.git/info/attributes"); rc=$?
  check "an attribute added while the scan ran refuses" "$rc" 1 "$out" "changed while the scan ran"
  not_clean "an attribute added while the scan ran" "$out"
  mk="$tmp/mk-late-filter"; mkrepo "$mk"; lfsstock "$mk"
  out=$(mgate "$mk" GGSHIELD_STUB_DURING="echo 'staged.txt filter=lfs' >>.git/info/attributes"); rc=$?
  check "a filter attribute added while the scan ran refuses" "$rc" 1 "$out" "$fattr"
  mk="$tmp/mk25c"; mkrepo "$mk"
  out=$(mgate "$mk"); rc=$?
  check "…and with nothing changed the same repository passes clean" "$rc" 0 "$out" "$SELF: clean"
  # A commit-graph planted for the commit made for the scan, naming HEAD's tree: read, the change is empty.
  mk="$tmp/mk26"; mkmark "$mk"
  gc=$(predc "$mk")
  printf '%s\n' "$gc" | git -C "$mk" commit-graph write --stdin-commits 2>/dev/null
  rm -f "$mk/.git/objects/${gc:0:2}/${gc:2}"
  chmod u+w "$mk/.git/objects/info/commit-graph"
  python3 -I -c '
import hashlib, sys
p, t, pt = sys.argv[1:]
b = bytearray(open(p, "rb").read())
i = b.find(bytes.fromhex(t))
assert i > 0
b[i:i + 20] = bytes.fromhex(pt)
b[-20:] = hashlib.sha1(bytes(b[:-20])).digest()
open(p, "wb").write(b)
' "$mk/.git/objects/info/commit-graph" "$(git -C "$mk" write-tree)" "$(git -C "$mk" rev-parse 'HEAD^{tree}')"
  out=$(dgate "$mk"); rc=$?
  check "a commit-graph planted for the commit made for the scan is not read: the marker is seen" "$rc" 1 "$out" "found a secret"
  # An object planted under the predicted id, naming HEAD's tree: git does not check it against its id.
  forge() { # forge <dir> <id> <tree>: replaces the loose commit <id> with one naming <tree>
    local o="$1/.git/objects/${2:0:2}/${2:2}"
    git -C "$1" cat-file commit "$2" | sed "1s/.*/tree $3/" >"$tmp/forged"
    rm -f "$o"
    python3 -I -c '
import sys, zlib
d = open(sys.argv[1], "rb").read()
open(sys.argv[2], "wb").write(zlib.compress(b"commit %d\0" % len(d) + d))
' "$tmp/forged" "$o"
  }
  mk="$tmp/mk27"; mkmark "$mk"
  gc=$(predc "$mk")
  forge "$mk" "$gc" "$(git -C "$mk" rev-parse 'HEAD^{tree}')"
  out=$(dgate "$mk"); rc=$?
  check "an object planted under the id of the commit made for the scan is refused" "$rc" 1 "$out" "does not hash to that id"
  not_clean "an object planted under the id of the commit made for the scan" "$out"
  mk="$tmp/mk27b"; mkmark "$mk"
  forge "$mk" "$(git -C "$mk" rev-parse HEAD)" "$(git -C "$mk" write-tree)"
  out=$(mgate "$mk"); rc=$?
  # git checks the hash when it peels HEAD, so the gate reads it as no first commit and scans the whole tree.
  check "HEAD's object replaced by one naming the staged tree does not pass" "$rc" 1 "$out" "REFUSED"
  not_clean "HEAD's object replaced by one naming the staged tree" "$out"
  # An object on HEAD's side planted under its id with the staged content: git does not check a tree's or a
  # blob's id when it compares them, so read, the patch was empty. Every object the diff compares against
  # is hashed (a packed HEAD would not be read from the planted copy, so these stay loose).
  plant() { # plant <dir> <type> <id> <content file>: replaces the loose object <id> with <content file>
    local o="$1/.git/objects/${3:0:2}/${3:2}"
    rm -f "$o"
    python3 -I -c '
import sys, zlib
d = open(sys.argv[2], "rb").read()
open(sys.argv[3], "wb").write(zlib.compress(sys.argv[1].encode() + b" %d\0" % len(d) + d))
' "$2" "$4" "$o"
  }
  mkdir_mark() { # mkdir_mark <dir>: mkrepo with dir/secret.txt committed benign, then staged with the marker
    mkrepo "$1"
    mkdir -p "$1/dir"
    printf 'benign\n' >"$1/dir/secret.txt"
    git -C "$1" add dir/secret.txt
    git -C "$1" -c user.name=t -c user.email=t@localhost -c commit.gpgsign=false \
      -c core.hooksPath=/dev/null commit -q -m dir
    printf 'GATE_STUB_MARKER\n' >"$1/dir/secret.txt"
    git -C "$1" add dir/secret.txt
  }
  mk="$tmp/mk27c"; mkdir_mark "$mk"
  out=$(mgate "$mk"); rc=$?
  check "a marker staged over a committed file in a directory is seen (control for the planted objects)" "$rc" 1 "$out" "found a secret"
  mk="$tmp/mk27d"; mkdir_mark "$mk"
  git -C "$mk" cat-file blob :dir/secret.txt >"$tmp/plant"
  plant "$mk" blob "$(git -C "$mk" rev-parse HEAD:dir/secret.txt)" "$tmp/plant"
  out=$(mgate "$mk"); rc=$?
  check "a blob on HEAD's side planted with the staged content is refused" "$rc" 1 "$out" "on HEAD's side of the change does not hash to its id"
  not_clean "a blob on HEAD's side planted with the staged content" "$out"
  mk="$tmp/mk27e"; mkdir_mark "$mk"
  printf 'other\n' >"$mk/other.txt"
  git -C "$mk" add other.txt
  git -C "$mk" cat-file tree "$(git -C "$mk" write-tree):dir" >"$tmp/plant"
  plant "$mk" tree "$(git -C "$mk" rev-parse HEAD:dir)" "$tmp/plant"
  out=$(mgate "$mk"); rc=$?
  check "a tree on HEAD's side planted with the staged tree is refused" "$rc" 1 "$out" "on HEAD's side of the change does not hash to its id"
  not_clean "a tree on HEAD's side planted with the staged tree" "$out"
  mk="$tmp/mk27h"; mkdir_mark "$mk"
  printf 'other\n' >"$mk/other.txt"
  git -C "$mk" add other.txt
  git -C "$mk" cat-file blob :dir/secret.txt >"$tmp/plant"
  gc=$(git -C "$mk" rev-parse HEAD:dir/secret.txt)
  # The planted copy is made beside the repository and moved in by the stub while the scan runs.
  mkdir -p "$tmp/mk27h-src/.git/objects/${gc:0:2}"
  plant "$tmp/mk27h-src" blob "$gc" "$tmp/plant"
  out=$(mgate "$mk" GGSHIELD_STUB_DURING="rm -f .git/objects/${gc:0:2}/${gc:2} && cp $tmp/mk27h-src/.git/objects/${gc:0:2}/${gc:2} .git/objects/${gc:0:2}/"); rc=$?
  check "a blob on HEAD's side planted while the scan ran is refused" "$rc" 1 "$out" "on HEAD's side of the change does not hash to its id"
  not_clean "a blob on HEAD's side planted while the scan ran" "$out"
  mk="$tmp/mk27f"; mkdir_mark "$mk"
  printf 'benign again\n' >"$mk/dir/secret.txt"
  git -C "$mk" add dir/secret.txt
  out=$(mgate "$mk"); rc=$?
  check "…and with nothing planted a benign edit in the directory passes clean" "$rc" 0 "$out" "$SELF: clean"
  # An object on HEAD's side that is missing: cat-file --batch answers "<id> missing" for it, which the
  # batch's reader takes as a mismatch and names.
  mk="$tmp/mk27k"; mkdir_mark "$mk"
  gc=$(git -C "$mk" rev-parse HEAD:dir/secret.txt)
  rm -f "$mk/.git/objects/${gc:0:2}/${gc:2}"
  out=$(mgate "$mk"); rc=$?
  check "a blob on HEAD's side that is missing is refused" "$rc" 1 "$out" "on HEAD's side of the change does not hash to its id (object $gc)"
  not_clean "a blob on HEAD's side that is missing" "$out"
  # A submodule's id is not an object here: it is not hashed, and changing it passes.
  mk="$tmp/mk27g"; mkrepo "$mk"
  git -C "$mk" update-index --add --cacheinfo "160000,$(git -C "$mk" rev-parse HEAD),sub"
  git -C "$mk" -c user.name=t -c user.email=t@localhost -c commit.gpgsign=false \
    -c core.hooksPath=/dev/null commit -q -m sub
  git -C "$mk" update-index --cacheinfo "160000,$(git -C "$mk" rev-parse HEAD),sub"
  out=$(mgate "$mk"); rc=$?
  check "a changed submodule id passes clean (it is not an object here)" "$rc" 0 "$out" "$SELF: clean"
  # git's listings of the change are bounded in time. A tree on HEAD's side planted to name itself as its
  # own subdirectory: git 2.56 stops at its tree-depth limit, an older git may walk on; either way it is
  # refused within the bound and never passes.
  local t0 secs
  mk="$tmp/mk27i"; mkdir_mark "$mk"
  gc=$(git -C "$mk" rev-parse HEAD:dir)
  python3 -I -c '
import binascii, sys
open(sys.argv[2], "wb").write(b"40000 dir\0" + binascii.unhexlify(sys.argv[1]))
' "$gc" "$tmp/plant"
  plant "$mk" tree "$gc" "$tmp/plant"
  t0=$SECONDS
  out=$(mgate "$mk" GGSHIELD_LIST_TIMEOUT=5); rc=$?
  secs=$((SECONDS - t0))
  check "a tree on HEAD's side that names itself is refused" "$rc" 1 "$out" "REFUSED"
  not_clean "a tree on HEAD's side that names itself" "$out"
  check "…within the time bound ($secs s)" "$((secs < 30 ? 0 : 1))" 0 "$out" "REFUSED"
  # A listing that does not finish: a git whose diff-tree never returns. With HEAD, the first listing is the
  # one of HEAD's side; before the first commit it is the list of files to scan.
  local slowgit="$tmp/gitslow"
  mkdir -p "$slowgit"
  printf '#!/bin/sh\ns=; k=; for a; do if [ -n \"$k\" ]; then k=; elif [ \"$a\" = -c ]; then k=1; else s=$a; break; fi; done\ncase "$s" in diff-tree) exec sleep 60 ;; esac\nexec %q "$@"\n' "$(command -v git)" >"$slowgit/git"
  chmod +x "$slowgit/git"
  t0=$SECONDS
  out=$(cd "$repo" && PATH="$slowgit:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 GGSHIELD_LIST_TIMEOUT=2 bash "$self" 2>&1); rc=$?
  secs=$((SECONDS - t0))
  check "a listing of HEAD's side that does not finish is stopped at the time limit" "$rc" 1 "$out" "did not finish within 2 s"
  check "…and counts as not scanned" "$rc" 1 "$out" "NOT scanned"
  check "…and was stopped, not waited for ($secs s)" "$((secs < 30 ? 0 : 1))" 0 "$out" "REFUSED"
  mk="$tmp/mk27j"; mkdir -p "$mk"
  git -C "$mk" init -q 2>/dev/null
  printf 'first\n' >"$mk/first.txt"
  git -C "$mk" add first.txt
  t0=$SECONDS
  out=$(cd "$mk" && PATH="$slowgit:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 GGSHIELD_LIST_TIMEOUT=2 bash "$self" 2>&1); rc=$?
  secs=$((SECONDS - t0))
  check "before the first commit, a list of files that does not finish is stopped at the time limit" "$rc" 1 "$out" "files of the commit to scan did not finish within 2 s"
  check "…and was stopped, not waited for ($secs s)" "$((secs < 30 ? 0 : 1))" 0 "$out" "REFUSED"
  out=$(cd "$repo" && PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 GGSHIELD_LIST_TIMEOUT=0 bash "$self" 2>&1); rc=$?
  check "a time limit of 0 is refused" "$rc" 1 "$out" "GGSHIELD_LIST_TIMEOUT is not a whole number of seconds above 0"
  # The limit in force is read from the watchdog's sleep: a sleep first on PATH records its argument, and a
  # git whose diff-tree waits (up to 30 s) until one is recorded, so the watchdog is never stopped before it.
  local sleepdir="$tmp/sleeprec" waitgit="$tmp/gitwait" slept="$tmp/slept"
  mkdir -p "$sleepdir" "$waitgit"
  printf '#!/bin/sh
echo "$1" >>%q
exec %q "$@"
' "$slept" "$(command -v sleep)" >"$sleepdir/sleep"
  printf '#!/bin/sh
s=; k=; for a; do if [ -n \"$k\" ]; then k=; elif [ \"$a\" = -c ]; then k=1; else s=$a; break; fi; done
if [ "$s" = diff-tree ]; then n=0; while [ ! -s %q ] && [ "$n" -lt 300 ]; do %q 0.1; n=$((n + 1)); done; fi
exec %q "$@"
' \
    "$slept" "$(command -v sleep)" "$(command -v git)" >"$waitgit/git"
  chmod +x "$sleepdir/sleep" "$waitgit/git"
  : >"$slept"
  out=$(cd "$repo" && PATH="$waitgit:$sleepdir:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 GGSHIELD_LIST_TIMEOUT=600 bash "$self" 2>&1); rc=$?
  check "a time limit above 120 s is not refused" "$rc" 0 "$out" "$SELF: clean"
  check "…and is lowered to 120 (the watchdog slept: $(tr '\n' ' ' <"$slept"))" \
    "$( [ -s "$slept" ] && ! grep -qvx 120 "$slept"; echo $?)" 0 "$out" "$SELF: clean"
  : >"$slept"
  out=$(cd "$repo" && PATH="$waitgit:$sleepdir:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 GGSHIELD_LIST_TIMEOUT=7 bash "$self" 2>&1); rc=$?
  check "a time limit of 7 s is the one the watchdog sleeps ($(tr '\n' ' ' <"$slept"))" \
    "$( [ -s "$slept" ] && ! grep -qvx 7 "$slept"; echo $?)" 0 "$out" "$SELF: clean"
  # A listing git cannot make (diff-tree exits 3) refuses as not scanned, without waiting for the limit.
  local failgit="$tmp/gitfail"
  mkdir -p "$failgit"
  printf '#!/bin/sh
s=; k=; for a; do if [ -n \"$k\" ]; then k=; elif [ \"$a\" = -c ]; then k=1; else s=$a; break; fi; done
case "$s" in diff-tree) exit 3 ;; esac
exec %q "$@"
' "$(command -v git)" >"$failgit/git"
  chmod +x "$failgit/git"
  t0=$SECONDS
  out=$(cd "$repo" && PATH="$failgit:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 bash "$self" 2>&1); rc=$?
  secs=$((SECONDS - t0))
  check "a listing of HEAD's side that git cannot make is refused" "$rc" 1 "$out" "git could not list the objects HEAD's side of the change is read from"
  check "…and counts as not scanned" "$rc" 1 "$out" "NOT scanned"
  check "…at once, not at the time limit ($secs s)" "$((secs < 30 ? 0 : 1))" 0 "$out" "REFUSED"
  # A parser that fails with an error of its own is refused as not scanned, and the refusal names that error:
  # the listing file is removed once git has written it (Linux only: the wrapper finds the file through /proc).
  if [ -d /proc/self/fd ]; then
    local gonegit="$tmp/gitgone"
    mkdir -p "$gonegit"
    printf '#!/bin/sh
s=; k=; for a; do if [ -n \"$k\" ]; then k=; elif [ \"$a\" = -c ]; then k=1; else s=$a; break; fi; done
case "$s" in diff-tree) %q "$@"; f=$(readlink /proc/$$/fd/1) && rm -f -- "$f"; exit 0 ;; esac
exec %q "$@"
' "$(command -v git)" "$(command -v git)" >"$gonegit/git"
    chmod +x "$gonegit/git"
    out=$(cd "$repo" && PATH="$gonegit:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 bash "$self" 2>&1); rc=$?
    check "a listing the parser cannot open is refused" "$rc" 1 "$out" "could not be parsed (parser exited 1, its output non-empty"
    check "…and the refusal names the parser's error" "$rc" 1 "$out" "non-empty: FileNotFoundError"
    check "…and counts as not scanned" "$rc" 1 "$out" "NOT scanned"
  fi
  # A gate stopped while git lists (TERM to the gate) leaves no git behind: a background child of a
  # non-interactive bash ignores SIGINT and SIGQUIT, so only the gate's EXIT trap stops it. The git records
  # its process id and never returns; the gate is stopped once it has, and that process must be gone. (TERM,
  # not INT: this selftest's own background children ignore SIGINT, so the gate it starts could not get one.)
  local pidgit="$tmp/gitpid" gpidf="$tmp/git-pid" gp gpid n
  running() { # running <pid>: the process is there and not a zombie (an orphan's zombie can outlive the
    # gate where nothing reaps it, as in a container whose first process does not)
    local st
    kill -0 "$1" 2>/dev/null || return 1
    [ -r "/proc/$1/stat" ] || return 0
    st=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
    st=${st##*) }
    [ "${st%% *}" != Z ]
  }
  mkdir -p "$pidgit"
  printf '#!/bin/sh
s=; k=; for a; do if [ -n \"$k\" ]; then k=; elif [ \"$a\" = -c ]; then k=1; else s=$a; break; fi; done
case "$s" in diff-tree) echo $$ >%q; exec sleep 60 ;; esac
exec %q "$@"
' "$gpidf" "$(command -v git)" >"$pidgit/git"
  chmod +x "$pidgit/git"
  rm -f "$gpidf"
  (cd "$repo" && exec env PATH="$pidgit:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 bash "$self" >"$tmp/stopped-out" 2>&1) &
  gp=$!
  n=0
  while [ ! -s "$gpidf" ] && [ "$n" -lt 300 ]; do sleep 0.1; n=$((n + 1)); done
  gpid=$(cat "$gpidf" 2>/dev/null)
  kill -TERM "$gp" 2>/dev/null
  wait "$gp"; rc=$?
  n=0
  while [ -n "$gpid" ] && running "$gpid" && [ "$n" -lt 50 ]; do sleep 0.1; n=$((n + 1)); done
  out=$(cat "$tmp/stopped-out")
  check "a gate stopped mid-listing ends by the signal (rc $rc)" "$rc" 143 "listing started: ${gpid:-no}" "listing started: [0-9]"
  not_clean "a gate stopped mid-listing" "$out"
  if [ -n "$gpid" ] && ! running "$gpid"; then
    printf '  ok    …and the git it started is gone\n'
  else
    printf '  FAIL  a gate stopped mid-listing left its git (%s) running\n' "${gpid:-never started}"; fail=1
    [ -z "$gpid" ] || kill -KILL "$gpid" 2>/dev/null
  fi
  # The bound needs no `timeout` program (macOS has none without coreutils): on a PATH of links to the
  # tools the gate and its stubs use, with no timeout or gtimeout among them, a listing that does not
  # finish is still stopped and refused, and a normal one passes.
  local notmo="$tmp/notimeout" tool tp
  mkdir -p "$notmo"
  for tool in bash sh env git python3 mktemp rm tr wc sleep cat sed grep head tail cut chmod id mkdir \
    cp mv date dirname basename readlink realpath ls touch sort awk cmp diff pwd; do
    tp=$(type -P "$tool") && ln -s "$tp" "$notmo/$tool"
  done
  if PATH="$notmo" type -P timeout gtimeout >/dev/null 2>&1; then
    printf '  FAIL  the PATH without timeout still finds one\n'; fail=1
  else
    printf '  ok    the PATH without timeout finds none\n'
  fi
  t0=$SECONDS
  out=$(cd "$repo" && PATH="$slowgit:$stub_dir:$notmo" GGSHIELD_STUB_RC=0 GGSHIELD_LIST_TIMEOUT=2 bash "$self" 2>&1); rc=$?
  secs=$((SECONDS - t0))
  check "with no timeout program, a listing that does not finish is stopped at the time limit" "$rc" 1 "$out" "did not finish within 2 s"
  check "…and counts as not scanned" "$rc" 1 "$out" "NOT scanned"
  check "…and was stopped, not waited for ($secs s)" "$((secs < 30 ? 0 : 1))" 0 "$out" "REFUSED"
  out=$(cd "$repo" && PATH="$stub_dir:$notmo" GGSHIELD_STUB_RC=0 bash "$self" 2>&1); rc=$?
  check "with no timeout program, a normal listing passes clean" "$rc" 0 "$out" "$SELF: clean"
  # The top of the tree is read through a sentinel and physdir: a core.worktree ending in a newline was read
  # as the name without it (command substitution drops it) before that change, and passed.
  mk="$tmp/mk28"; mkdir -p "$mk" "$tmp/wt28"$'\n'
  git -C "$mk" init -q 2>/dev/null
  git -C "$mk" config core.worktree "$tmp/wt28"$'\n'
  printf 'one\n' >"$tmp/wt28"$'\n'"/a.txt"
  (cd "$tmp/wt28"$'\n' && GIT_DIR="$mk/.git" git add a.txt)
  out=$(mgate "$mk"); rc=$?
  check "a core.worktree ending in a newline is refused (the top is read through a sentinel)" "$rc" 1 "$out" "control character"
  not_clean "a core.worktree ending in a newline" "$out"

  # No repository, no first commit yet, and an unmerged path.
  mkdir -p "$tmp/plain"
  out=$(cd "$tmp/plain" && PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 GIT_CEILING_DIRECTORIES="$tmp" bash "$self" 2>&1); rc=$?
  check "outside a repository BLOCKS, not scanned" "$rc" 1 "$out" "NOT scanned"
  local fresh="$tmp/fresh"
  mkdir -p "$fresh"
  git -C "$fresh" init -q 2>/dev/null
  printf 'first\n' >"$fresh/first.txt"
  git -C "$fresh" add first.txt
  : >"$log"
  out=$(cd "$fresh" && PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 GGSHIELD_STUB_LOG="$log" bash "$self" 2>&1); rc=$?
  check "before the first commit the staged files are scanned as one commit" "$rc" 0 "$out" "$SELF: clean"
  check "…holding the staged file" 0 0 "$(cat "$log")" "^files=first.txt $"
  check "…and only that commit" 0 0 "$(cat "$log")" "^commits=1$"

  # The index must be found, and the tree scanned must be exactly what it stages: a gate that cannot
  # find the index, or builds a tree other than what is staged, scanned the wrong thing and must not
  # call that clean.
  out=$(gate GGSHIELD_STUB_RC=0 GIT_INDEX_FILE="$tmp/no-such-index"); rc=$?
  check "GIT_INDEX_FILE naming no file BLOCKS, not scanned" "$rc" 1 "$out" "NOT scanned"
  check "…and says the index was not found" "$rc" 1 "$out" "index file .* was not found"
  local noindex="$tmp/noindex"
  mkrepo "$noindex"
  rm -f "$noindex/.git/index"
  out=$(cd "$noindex" && PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 bash "$self" 2>&1); rc=$?
  check "no index file in the repository BLOCKS, not scanned" "$rc" 1 "$out" "NOT scanned"
  # A git whose write-tree answers HEAD's tree (the staged change left out of it): the counts disagree.
  local shim="$tmp/gitshim" realgit
  realgit=$(command -v git)
  mkdir -p "$shim"
  printf '#!/bin/sh\ns=; k=; for a; do if [ -n \"$k\" ]; then k=; elif [ \"$a\" = -c ]; then k=1; else s=$a; break; fi; done\nif [ "$s" = write-tree ]; then exec %q rev-parse "HEAD^{tree}"; fi\nexec %q "$@"\n' "$realgit" "$realgit" >"$shim/git"
  chmod +x "$shim/git"
  out=$(cd "$repo" && PATH="$shim:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 bash "$self" 2>&1); rc=$?
  check "a tree that lacks the staged files BLOCKS, not scanned" "$rc" 1 "$out" "NOT scanned"
  check "…and says the staged index is not the tree scanned" "$rc" 1 "$out" "not exactly the tree that was scanned"
  out=$(gate GGSHIELD_STUB_RC=0); rc=$?
  check "a clean scan says how many staged files it covered" "$rc" 0 "$out" "2 staged file(s) scanned"
  # A git whose write-tree answers a tree of its own with as many changed files as are staged (counts
  # equal, content not): what is scanned is not what is staged, so it is refused.
  local swap="$tmp/swap" alt
  mkrepo "$swap"
  alt=$(cd "$swap" && export GIT_INDEX_FILE="$tmp/altindex" \
    && "$realgit" read-tree HEAD \
    && blob=$(printf 'swapped\n' | "$realgit" hash-object -w --stdin) \
    && "$realgit" update-index --add --cacheinfo "100644,$blob,kept.txt" \
    && "$realgit" update-index --add --cacheinfo "100644,$blob,staged.txt" \
    && "$realgit" write-tree)
  printf '#!/bin/sh\ns=; k=; for a; do if [ -n \"$k\" ]; then k=; elif [ \"$a\" = -c ]; then k=1; else s=$a; break; fi; done\nif [ "$s" = write-tree ]; then echo %q; exit 0; fi\nexec %q "$@"\n' "$alt" "$realgit" >"$shim/git"
  out=$(cd "$swap" && PATH="$shim:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 bash "$self" 2>&1); rc=$?
  check "a swapped tree with the same number of changed files BLOCKS, not scanned" "$rc" 1 "$out" "NOT scanned"
  check "…and says the staged index is not the tree scanned" "$rc" 1 "$out" "not exactly the tree that was scanned"
  # An intent-to-add file (git add -N) beside a real staged change is not a mismatch: what is scanned
  # is what will be committed, and the file is not in it.
  local ita="$tmp/ita"
  mkrepo "$ita"
  printf 'later\n' >"$ita/ita.txt"
  git -C "$ita" add -N ita.txt
  : >"$log"
  out=$(cd "$ita" && PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 GGSHIELD_STUB_LOG="$log" bash "$self" 2>&1); rc=$?
  check "an intent-to-add file beside a staged change is scanned and allowed" "$rc" 0 "$out" "2 staged file(s) scanned"
  check "…the scan holding the staged files and not the intent-to-add one" 0 0 "$(cat "$log")" "^files=kept.txt staged.txt $"

  # Nothing staged to scan passes only when it is proven: the tree written must be HEAD's own. A git whose
  # diff-index and diff-tree list nothing (both counts 0) beside a real staged change is refused.
  local blind="$tmp/gitblind"
  mkdir -p "$blind"
  printf '#!/bin/sh\ns=; k=; for a; do if [ -n \"$k\" ]; then k=; elif [ \"$a\" = -c ]; then k=1; else s=$a; break; fi; done\ncase "$s" in diff-index | diff-tree) exit 0 ;; esac\nexec %q "$@"\n' "$realgit" >"$blind/git"
  chmod +x "$blind/git"
  out=$(cd "$repo" && PATH="$blind:$stub_dir:$PATH" GGSHIELD_STUB_RC=0 bash "$self" 2>&1); rc=$?
  check "counts of 0 beside a changed tree BLOCKS, not scanned" "$rc" 1 "$out" "NOT scanned"
  check "…and says the tree is not HEAD's" "$rc" 1 "$out" "is not HEAD's tree"
  # A merge that keeps our side (a staged change of no file, tree equal to HEAD's) is still nothing to scan.
  local ours="$tmp/ours"
  mkrepo "$ours"
  git -C "$ours" -c user.name=t -c user.email=t@localhost -c commit.gpgsign=false \
    -c core.hooksPath=/dev/null commit -q -m ours
  git -C "$ours" checkout -q -b side HEAD~1
  printf 'side\n' >"$ours/side.txt"
  git -C "$ours" add side.txt
  git -C "$ours" -c user.name=t -c user.email=t@localhost -c commit.gpgsign=false \
    -c core.hooksPath=/dev/null commit -q -m side
  git -C "$ours" checkout -q -
  git -C "$ours" -c user.name=t -c user.email=t@localhost merge -q -s ours --no-commit --no-ff side >/dev/null 2>&1
  out=$(cd "$ours" && PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 bash "$self" 2>&1); rc=$?
  check "a merge keeping our side (tree equal to HEAD's) is still nothing to scan" "$rc" 0 "$out" "nothing to scan"
  check "…and allowed" "$rc" 0 "$out" "$SELF: clean"

  # A merge being concluded: scanned against HEAD alone, the index left as it was.
  local merging="$tmp/merging" index_before
  mkrepo "$merging"
  git -C "$merging" -c user.name=t -c user.email=t@localhost -c commit.gpgsign=false \
    -c core.hooksPath=/dev/null commit -q -m ours
  git -C "$merging" checkout -q -b side HEAD~1
  printf 'side\n' >"$merging/side.txt"
  git -C "$merging" add side.txt
  git -C "$merging" -c user.name=t -c user.email=t@localhost -c commit.gpgsign=false \
    -c core.hooksPath=/dev/null commit -q -m side
  git -C "$merging" checkout -q -
  git -C "$merging" -c user.name=t -c user.email=t@localhost merge -q --no-commit --no-ff side >/dev/null 2>&1
  index_before=$(sha256sum <"$merging/.git/index")
  : >"$log"
  out=$(cd "$merging" && PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 GGSHIELD_STUB_LOG="$log" bash "$self" 2>&1); rc=$?
  check "a merge being concluded is scanned" "$rc" 0 "$out" "$SELF: clean"
  check "…as one commit holding what the merge brings in" 0 0 "$(cat "$log")" "^files=side.txt $"
  check "…and only that commit" 0 0 "$(cat "$log")" "^commits=1$"
  if [ -f "$merging/.git/MERGE_HEAD" ] && [ "$(sha256sum <"$merging/.git/index")" = "$index_before" ]; then
    printf '  ok    …with the merge still in progress and the index file untouched\n'
  else
    printf '  FAIL  the scan changed the index file or ended the merge\n'; fail=1
  fi

  # From a real pre-commit hook: what is scanned is what is committed, for a
  # plain commit, `commit -a`, and `commit -- <path>` beside another staged file.
  local hooked="$tmp/hooked" want
  mkrepo "$hooked"
  mkdir -p "$tmp/hooks"
  printf '#!/usr/bin/env bash\nexec bash %q\n' "$self" >"$tmp/hooks/pre-commit"
  chmod +x "$tmp/hooks/pre-commit"
  commit() { # commit <git commit arguments...>: in $hooked, through the hook, with the stub
    : >"$log"
    (cd "$hooked" && PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC="${STUB_RC:-0}" GGSHIELD_STUB_LOG="$log" \
      git -c user.name=t -c user.email=t@localhost -c commit.gpgsign=false \
      -c core.hooksPath="$tmp/hooks" commit -q "$@" 2>&1)
  }
  scanned_is_committed() { # scanned_is_committed <name> <rc> <output>
    want=$(git -C "$hooked" rev-parse 'HEAD^{tree}')
    check "$1: the commit is made" "$2" 0 "$3" "$SELF: clean"
    check "$1: the tree scanned is the tree committed" 0 0 "$(cat "$log")" "^tree=$want$"
    check "$1: one commit was scanned" 0 0 "$(cat "$log")" "^commits=1$"
  }
  out=$(commit -m "plain"); rc=$?
  scanned_is_committed "git commit" "$rc" "$out"
  printf 'three\n' >>"$hooked/kept.txt"
  out=$(commit -a -m "all"); rc=$?
  scanned_is_committed "git commit -a" "$rc" "$out"
  check "git commit -a: the edit it picked up was scanned" 0 0 "$(cat "$log")" "^files=kept.txt $"
  printf 'four\n' >>"$hooked/kept.txt"
  printf 'more\n' >>"$hooked/staged.txt"
  git -C "$hooked" add kept.txt staged.txt
  out=$(commit -m "one path" -- staged.txt); rc=$?
  scanned_is_committed "git commit -- <path>" "$rc" "$out"
  check "git commit -- <path>: only that path was scanned" 0 0 "$(cat "$log")" "^files=staged.txt $"
  want=$(git -C "$hooked" rev-parse HEAD)
  out=$(STUB_RC=1 commit -m "refused"); rc=$?
  check "a finding stops a real commit" "$rc" 1 "$out" "found a secret"
  if [ "$(git -C "$hooked" rev-parse HEAD)" = "$want" ] && [ -n "$(git -C "$hooked" diff --cached --name-only)" ]; then
    printf '  ok    …and HEAD has not moved, with the change still staged\n'
  else
    printf '  FAIL  a refused commit moved HEAD or lost the staged change\n'; fail=1
  fi

  # The same through real hooks, from a subdirectory and from a linked worktree: what git exports to the
  # hook there (GIT_DIR in a linked worktree, GIT_INDEX_FILE and GIT_PREFIX from a subdirectory) passes.
  hook_commit() { # hook_commit <directory> <git commit arguments...>
    local dir=$1
    shift
    : >"$log"
    (cd "$dir" && PATH="$stub_dir:$PATH" GGSHIELD_STUB_RC=0 GGSHIELD_STUB_LOG="$log" \
      git -c user.name=t -c user.email=t@localhost -c commit.gpgsign=false \
      -c core.hooksPath="$tmp/hooks" commit -q "$@" 2>&1)
  }
  mkdir -p "$hooked/subdir"
  printf 'five\n' >>"$hooked/kept.txt"
  out=$(hook_commit "$hooked/subdir" -a -m "from a subdirectory"); rc=$?
  check "git commit -a from a subdirectory passes the gate" "$rc" 0 "$out" "$SELF: clean"
  check "…and scanned the edit" 0 0 "$(cat "$log")" "^files=kept.txt $"
  git -C "$hooked" worktree add -q "$tmp/hlw" -b hlwb >/dev/null 2>&1
  mkdir -p "$tmp/hlw/subdir"
  printf 'linked\n' >"$tmp/hlw/linked.txt"
  git -C "$tmp/hlw" add linked.txt
  out=$(hook_commit "$tmp/hlw" -m "from a linked worktree"); rc=$?
  check "git commit from a linked worktree passes the gate" "$rc" 0 "$out" "$SELF: clean"
  check "…and scanned the staged file" 0 0 "$(cat "$log")" "^files=linked.txt $"
  printf 'linked again\n' >>"$tmp/hlw/linked.txt"
  out=$(hook_commit "$tmp/hlw/subdir" -a -m "from a linked worktree's subdirectory"); rc=$?
  check "git commit -a from a subdirectory of a linked worktree passes the gate" "$rc" 0 "$out" "$SELF: clean"
  check "…and scanned the edit" 0 0 "$(cat "$log")" "^files=linked.txt $"

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
  "") run_gate ;;
  *) echo "usage: $0 [--selftest]" >&2; exit 3 ;;
esac
