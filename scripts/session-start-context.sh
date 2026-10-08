#!/usr/bin/env bash
# session-start-context.sh -- points a fresh or compacted artifacts session at
# its own state before it believes anything it remembers.
#
# WHY THIS EXISTS (homelab-ij8.77, under homelab-tay.13; Jeremy 2026-09-20,
# retro item 3): "It's super important we don't lose state or trust in our
# state." Compaction is where this seat loses state, and the dangerous part is
# not the loss -- it is that a compacted session does not know it has lost
# anything. It comes back holding a summary that reads like knowledge.
#
# That matters more here than in most seats. This one publishes pages Jeremy
# READS AS CURRENT. A session that republishes a status page confidently from a
# compaction summary puts a stale claim in front of him with a fresh timestamp
# on it, which is the exact failure this whole service exists to prevent.
#
# The beads are the state of record -- progress, decisions, open questions, and
# which beliefs were verified rather than merely told. They survive a
# compaction intact; a summary does not.
#
# Deliberately unconditional and deliberately dumb: it checks nothing and
# decides nothing. SessionStart fires once per session and once per compaction,
# so an unconditional reminder costs a few hundred tokens at a moment when the
# session has just lost its context, and a missed check is the failure it
# exists to prevent. Mirrors ~/repos/homelab/scripts/ops-post-compact-context.sh.
#
# WIRING: SessionStart in .claude/settings.json, passing --event so the printed
# JSON names the hook that actually fired. `bd prime --hook-json` is wired
# separately and carries the tracker itself; this script says what to DO with
# it.
#
# THERE IS NO PostCompact HOOK, AND THAT IS THE MEASURED ANSWER, NOT AN
# OVERSIGHT (21 Sep 2026, Claude Code 2.1.278, read out of the installed
# binary):
#
#   * The hook OUTPUT schema has no PostCompact variant at all. Emitting
#     {"hookSpecificOutput":{"hookEventName":"PostCompact",...}} fails
#     validation, and the whole hook output is discarded -- Jeremy sees a red
#     "PostCompact [...] failed" in his terminal and the session gets nothing.
#   * The PostCompact runner returns only `userDisplayMessage`. Unlike
#     PreCompact (`newCustomInstructions`) and SessionStart
#     (`additionalContext`), it has NO path into the model's context. Even
#     well-formed output could only ever be printed at the human.
#   * Both compaction paths call the SessionStart hook with source="compact"
#     immediately afterwards. That firing DOES inject, and it is what delivers
#     this reminder after a compaction.
#
# So the reminder arrives exactly once per compaction, through SessionStart.
# This script refuses to print hook JSON for any event that cannot carry it
# (see EVENTS_THAT_INJECT): a reminder that is silently thrown away is worse
# than none, because the log says it fired.
#
# USAGE
#   scripts/session-start-context.sh --event SessionStart
#   scripts/session-start-context.sh              # defaults to SessionStart
#   scripts/session-start-context.sh --selftest
#
# EXIT CODES: 0 always for --event/bare. A hook that can fail session start is
# worse than one that says nothing. --selftest returns 0 only if every check
# passes.
set -uo pipefail

MSG='You are the artifacts maintainer (~/repos/artifacts; formerly the artifacts seat). How this office works here -- its epic, keys, forge identity, deploy path, coordination and how to work with Jeremy -- is in ~/repos/runcove/docs/offices/artifacts-maintainer.md: read it first; it points at the shared ways of working (~/repos/runcove/ways-of-working/), and the repo instruction file is generic. Before you act on anything you think you know: (1) your state of record is the runcove-5t3oj beads in the shared runcove tracker (moved from homelab-ij8 on 6 Oct 2026; history stays there), not your memory and not a compaction summary -- `bd -C ~/repos/runcove prime`, then `bd -C ~/repos/runcove list --parent runcove-5t3oj`; (2) the newest "HANDOFF:" item in that list is the last handoff -- read it; (3) separate what you have VERIFIED this session from what you were TOLD -- a page this seat publishes is read as current, so a claim carried over from a summary and republished is a stale claim with a fresh timestamp on it; (4) before you republish any page, re-read the bead it reports on. Ruled by Jeremy 2026-09-20 (retro item 3: compaction is the main memory loss).'

# The events whose output this harness actually injects into the session. Kept
# as data rather than an `if`, because the interesting case is the one that is
# NOT here: see the PostCompact note above. If a later Claude Code grows a
# PostCompact output variant, adding the word here is the whole change.
EVENTS_THAT_INJECT=" SessionStart "

usage() { sed -n '2,60p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# Where each firing is recorded. ONE LINE PER RUN, no content: which hook
# fired, from which source, and when -- so "did this actually fire on a real
# compaction?" is a question with an answer on disk instead of an inference
# from my own context.
#
# WHY IT IS NEEDED (21 Sep 2026). The acceptance criteria on homelab-ij8.77
# asks for one REAL compaction, not just the pipe-test. I went looking for the
# evidence after three auto-compactions and could not produce it: the reminder
# is in the session's context, but the transcript re-attaches CLAUDE.md, the
# session context and the date after a compaction and records no hook output,
# so a reminder CARRIED OVER from session start and one RE-INJECTED at the
# compaction look identical from the inside. That is the same failure this
# script exists to warn about, one level up -- believing something because it
# is in front of me rather than because I checked it.
#
# `source=` is what makes the line evidence rather than a tally: SessionStart
# fires at startup, at resume, at clear, at fork AND at compaction, and only
# `source=compact` proves the reminder was re-delivered to a session that had
# just lost its context.
#
# Outside the repo on purpose: a file the hook writes on every session start
# has no business in a git tree, where it would show up as a dirty working
# directory at the worst moment.
LOG="${ARTIFACTS_HOOK_LOG:-$HOME/.claude/artifacts-seat-hooks.log}"
LOG_KEEP=200

# note_firing <event> <source> -- append one line, keep the tail bounded, and
# NEVER fail the hook: this is evidence, not a precondition (see EXIT CODES).
note_firing() {
  local event="$1" source="${2:-}" stamp
  stamp=$(date '+%Y-%m-%d %H:%M:%S%z') || return 0
  {
    mkdir -p "$(dirname "$LOG")" 2>/dev/null &&
      printf '%s\t%s\tsource=%s\tpid=%s\n' \
        "$stamp" "$event" "${source:-unknown}" "$$" >>"$LOG" &&
      if [[ $(wc -l <"$LOG" 2>/dev/null || echo 0) -gt $((LOG_KEEP * 2)) ]]; then
        tail -n "$LOG_KEEP" "$LOG" >"$LOG.trim" && mv "$LOG.trim" "$LOG"
      fi
  } 2>/dev/null || true
  return 0
}

# read_source -- the `source` (SessionStart) or `trigger` (the compact hooks)
# out of the payload on stdin, or empty.
#
# The payload is read for the LOG ONLY; nothing this script prints depends on
# it, so a harness that sends nothing loses evidence and not the reminder.
# Bounded three ways, because a hook that hangs delays session start itself:
# skipped outright when stdin is a terminal (a human running it by hand),
# `timeout 1` on the read, and every failure swallowed.
read_source() {
  local payload=""
  [[ -t 0 ]] && return 0
  payload=$(timeout 1 cat 2>/dev/null) || true
  [[ -n "$payload" ]] || return 0
  printf '%s' "$payload" | jq -r '(.source // .trigger // empty)' 2>/dev/null || true
}

# cmd_run <hook-event-name> -- log the firing, then print the reminder if and
# only if this harness can deliver it.
#
# The message carries the moment it was assembled and the hook that assembled
# it, so a session can tell a FRESH reminder from one carried through a
# compaction by reading its own first line -- and so "read the clock before
# stamping anything" is true of the reminder itself.
cmd_run() {
  local event="${1:-SessionStart}" stamped source
  source=$(read_source)
  note_firing "$event" "$source"
  case "$EVENTS_THAT_INJECT" in
    *" $event "*) ;;
    *) return 0 ;;   # fired, recorded, and deliberately silent
  esac
  stamped="$MSG Injected by the $event hook at $(date '+%H:%M %Z on %Y-%m-%d')."
  jq -cn --arg ev "$event" --arg ctx "$stamped" \
    '{hookSpecificOutput: {hookEventName: $ev, additionalContext: $ctx}}'
}

# Asserts positive facts about the output, never the absence of an error: an
# empty string satisfies "no error appeared" and would pass a lazier check.
cmd_selftest() {
  local rc=0 out fails=0

  # A selftest never writes to the REAL log. That log is evidence about when
  # the hook actually fired, and a line put there by a test is a line that
  # says a compaction happened when none did. Caught 21 Sep, by a selftest
  # that had already put two.
  local real_log="$LOG" selftest_dir
  selftest_dir=$(mktemp -d)
  LOG="$selftest_dir/hooks.log"

  # SessionStart: the one event this harness injects, so the one that must
  # print a real message.
  out=$(cmd_run SessionStart </dev/null) || rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "FAIL: --event SessionStart exited $rc" >&2
    fails=$((fails + 1))
    rc=0
  elif ! printf '%s' "$out" | jq -e . >/dev/null 2>&1; then
    echo "FAIL: --event SessionStart did not print valid JSON" >&2
    fails=$((fails + 1))
  else
    local name ctx
    name=$(printf '%s' "$out" | jq -r '.hookSpecificOutput.hookEventName // empty')
    ctx=$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext // empty')
    [[ "$name" == "SessionStart" ]] || { echo "FAIL: SessionStart named itself '$name'" >&2; fails=$((fails + 1)); }
    (( ${#ctx} > 200 )) || { echo "FAIL: SessionStart context is ${#ctx} chars, expected a real message" >&2; fails=$((fails + 1)); }
    # The things the message must actually carry, or it is decoration. The
    # last is the stamp: without it a carried-over reminder and a fresh one
    # are indistinguishable, which is the whole point of writing one.
    for needle in 'bd -C ~/repos/runcove prime' 'handoff' 'VERIFIED' 'Injected by the SessionStart hook at'; do
      case "$ctx" in
        *"$needle"*) ;;
        *) echo "FAIL: SessionStart context never mentions '$needle'" >&2; fails=$((fails + 1)) ;;
      esac
    done
  fi

  # PostCompact: this harness has no output variant for it and no path from it
  # into the session's context, so the only correct output is NONE. Asserted
  # because the wrong version of this was live for a day and failed loudly in
  # Jeremy's terminal at every compaction (see the header).
  out=$(cmd_run PostCompact </dev/null) || rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "FAIL: --event PostCompact exited $rc" >&2
    fails=$((fails + 1))
    rc=0
  fi
  [[ -z "$out" ]] || {
    echo "FAIL: PostCompact printed ${#out} chars; this harness rejects all of them" >&2
    fails=$((fails + 1))
  }

  # The firing is recorded, and recorded for the event that fired, with the
  # source that fired it. Asserted by running against a scratch log and
  # counting what lands in it -- the absence of an error would be satisfied by
  # a log nothing was ever written to.
  local before after
  before=$(wc -l <"$LOG" 2>/dev/null || echo 0)
  printf '{"hook_event_name":"SessionStart","source":"compact"}' | cmd_run SessionStart >/dev/null
  printf '{"hook_event_name":"PostCompact","trigger":"auto"}' | cmd_run PostCompact >/dev/null
  after=$(wc -l <"$LOG" 2>/dev/null || echo 0)
  (( after == before + 2 )) || {
    echo "FAIL: two firings added $((after - before)) log line(s)" >&2
    fails=$((fails + 1))
  }
  grep -q 'PostCompact' "$LOG" 2>/dev/null || { echo "FAIL: the log never names PostCompact" >&2; fails=$((fails + 1)); }
  # The whole point of the source field: a compaction must be distinguishable
  # from a startup in the evidence, or the log cannot answer the question the
  # bead asks.
  grep -q 'SessionStart	source=compact' "$LOG" 2>/dev/null || {
    echo "FAIL: a payload with source=compact was not recorded as one" >&2
    fails=$((fails + 1))
  }
  # ...and a firing with no payload still lands, rather than hanging or being
  # dropped: the harness is not obliged to send one.
  grep -q 'source=unknown' "$LOG" 2>/dev/null || {
    echo "FAIL: a firing with no payload on stdin was not recorded" >&2
    fails=$((fails + 1))
  }
  [[ -s "$real_log" ]] || true  # never created by a selftest; see above

  # The command the message tells the session to run must actually work from
  # THIS directory -- the beads live in another repo, and a reminder naming a
  # command that errors is worse than no reminder.
  if ! bd -C "$HOME/repos/runcove" ready >/dev/null 2>&1; then
    echo "FAIL: 'bd -C ~/repos/runcove ready' does not work from $(pwd)" >&2
    fails=$((fails + 1))
  fi

  rm -rf "$selftest_dir"
  LOG="$real_log"

  if [[ $fails -eq 0 ]]; then
    echo "selftest: all checks passed"
    return 0
  fi
  echo "selftest: $fails check(s) failed" >&2
  return 1
}

case "${1:-}" in
  --selftest) cmd_selftest </dev/null ;;
  --help | -h) usage ;;
  --event) cmd_run "${2:-SessionStart}" ;;
  "") cmd_run SessionStart ;;
  *) usage >&2; exit 2 ;;
esac
