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
# WIRING: SessionStart and PostCompact in .claude/settings.json, each passing
# --event so the printed JSON names the hook that actually fired. `bd prime
# --hook-json` is wired separately and carries the tracker itself; this script
# says what to DO with it.
#
# USAGE
#   scripts/session-start-context.sh --event SessionStart|PostCompact
#   scripts/session-start-context.sh              # defaults to SessionStart
#   scripts/session-start-context.sh --selftest
#
# EXIT CODES: 0 always for --event/bare. A hook that can fail session start is
# worse than one that says nothing. --selftest returns 0 only if every check
# passes.
set -uo pipefail

MSG='You are the artifacts seat (~/repos/artifacts). Before you act on anything you think you know: (1) your state of record is the ij8 beads, not your memory and not a compaction summary -- `bd -C ~/repos/homelab prime`, then `bd -C ~/repos/homelab ready`; (2) read the newest ~/repos/artifacts/docs/agent/handoff-*.md, then CLAUDE.md; (3) separate what you have VERIFIED this session from what you were TOLD -- a page this seat publishes is read as current, so a claim carried over from a summary and republished is a stale claim with a fresh timestamp on it; (4) before you republish any page, re-read the bead it reports on. Ruled by Jeremy 2026-09-20 (retro item 3: compaction is the main memory loss).'

usage() { sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# cmd_run <hook-event-name> -- never reads stdin. SessionStart and PostCompact
# need none of it, and a hook that blocked on an undrained pipe would delay
# session start itself.
cmd_run() {
  local event="${1:-SessionStart}"
  jq -cn --arg ev "$event" --arg ctx "$MSG" \
    '{hookSpecificOutput: {hookEventName: $ev, additionalContext: $ctx}}'
}

# Asserts positive facts about the output, never the absence of an error: an
# empty string satisfies "no error appeared" and would pass a lazier check.
cmd_selftest() {
  local rc=0 out fails=0

  for ev in SessionStart PostCompact; do
    out=$(cmd_run "$ev") || rc=$?
    if [[ $rc -ne 0 ]]; then
      echo "FAIL: --event $ev exited $rc" >&2
      fails=$((fails + 1))
      rc=0
      continue
    fi
    if ! printf '%s' "$out" | jq -e . >/dev/null 2>&1; then
      echo "FAIL: --event $ev did not print valid JSON" >&2
      fails=$((fails + 1))
      continue
    fi
    local name ctx
    name=$(printf '%s' "$out" | jq -r '.hookSpecificOutput.hookEventName // empty')
    ctx=$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext // empty')
    [[ "$name" == "$ev" ]] || { echo "FAIL: $ev named itself '$name'" >&2; fails=$((fails + 1)); }
    (( ${#ctx} > 200 )) || { echo "FAIL: $ev context is ${#ctx} chars, expected a real message" >&2; fails=$((fails + 1)); }
    # The three things the message must actually carry, or it is decoration.
    for needle in 'bd -C ~/repos/homelab prime' 'handoff' 'VERIFIED'; do
      case "$ctx" in
        *"$needle"*) ;;
        *) echo "FAIL: $ev context never mentions '$needle'" >&2; fails=$((fails + 1)) ;;
      esac
    done
  done

  # The command the message tells the session to run must actually work from
  # THIS directory -- the beads live in another repo, and a reminder naming a
  # command that errors is worse than no reminder.
  if ! bd -C "$HOME/repos/homelab" ready >/dev/null 2>&1; then
    echo "FAIL: 'bd -C ~/repos/homelab ready' does not work from $(pwd)" >&2
    fails=$((fails + 1))
  fi

  if [[ $fails -eq 0 ]]; then
    echo "selftest: all checks passed"
    return 0
  fi
  echo "selftest: $fails check(s) failed" >&2
  return 1
}

case "${1:-}" in
  --selftest) cmd_selftest ;;
  --help | -h) usage ;;
  --event) cmd_run "${2:-SessionStart}" ;;
  "") cmd_run SessionStart ;;
  *) usage >&2; exit 2 ;;
esac
