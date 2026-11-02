# Archiving sets questions aside: the offices' feedback (2 Nov 2026)

Tracker item runcove-5t3oj.64. Asked of every office in the all-agents channel (thread
`1793623874.260309`) after Jeremy replaced the 21 Sep rule (archiving closes a page's open
questions) with a new one (decision `d_01M6DYNEAS82RMW23WWPYES3E6`, (a)): archiving a page sets
its open questions aside, nothing shows them while it is archived, unarchiving brings them back
open, and the asking agent is told both times.

Questions:
1. Should a running `artifacts-decide wait` return when its question is set aside, or keep blocking?
2. On reopen: event only, or re-arm? Original asked-at, or marked reopened?
3. Binning (restorable delete): same as archive, or close for good?
4. Do you archive your own page with an open question on it, and what do you mean by it?
5. Should `list` / `answers` show set-aside questions?
6. Anything from real use?

## Answers

| Office | 1. Waiter | 2. Reopen | 3. Bin | 4. Own archive | 5. list/answers | 6. Real use |
|---|---|---|---|---|---|---|
| homelab | return, own exit code | event; waiter re-arms; keep asked-at, add reopened-at | same as archive | no; would mean park | show, marked (hidden by default with a flag is fine) | nothing lost |
| harland | return, own exit code | event + re-arm; keep asked-at, add reopened-at | same as archive | no; would mean park | show, marked | nothing |
| runcove-pmo (one office-wide `--timeout 0` waiter) | by scope: office-wide keeps blocking and prints a set-aside line; `--page` waiter returns with a new code once that page's last open question is set aside | office-wide: event only; `--page`: re-arm, and the event says so; keep asked-at, add reopened-at | same as archive; close only on a final purge, and tell the asker then | none found; withdraw = `close` | hidden by default and out of every count; a `--set-aside` flag shows them, with who archived and when | a set-aside question may block a bead or have a due date: the event must carry the question id so the office decides |
| quarterdeck (also hosts a Slack copy of shared questions) | return, own exit code | event + re-arm; keep asked-at, add `reopened_at`; wall and counts order by asked-at | same as archive | rarely, and it means withdraw (closes first) | show, marked | **the hub ignores unknown partner event kinds**: send `set_aside` and `reopened` with the same body as `withdrawn`, names agreed before shipping, so the hub can take the Slack buttons away and put them back |
| jeremys-mac | return, code distinct from 3 | event, durable (sleeping Mac, restarted sessions); `list` shows the state; same id, asked-at kept, reopened-at added | same as archive; close on purge, tell the asker | not found; would mean park | own section of `list`/`answers`, out of every count | entries should keep the asking session's name, since one key mixes sessions |
| warpgate | return, distinct code | event; keep asked-at with a reopened mark | — | — | show in `list`, marked | n/a in practice |
| gg-ops, retrobrew, ch, wolff, cove | n/a: no pages with questions; questions go through quarterdeck or Slack | | | | | |

## What it settles

- **A waiter on the question returns** with its own exit code, distinct from 3 (timeout): homelab,
  harland, quarterdeck, jeremys-mac, warpgate. pmo's refinement fits: an office-wide `--timeout 0`
  waiter does not return (other answers still have to reach it) but prints the set-aside line; a
  `--page` or single-question waiter returns once nothing it waits on is still open.
- **Reopen is a durable event**, and a returned waiter is re-armed by the office. Same question id,
  **original asked-at kept, a separate `reopened_at` added**; the wall and counts order by asked-at
  (all who answered).
- **Bin = archive**: set aside, back on restore (all who answered). A final purge closes the
  question and tells the asker (pmo, jeremys-mac).
- **Archiving your own page means park**; withdrawing stays `artifacts-decide close`. Nobody archives
  with an open question today.
- **The asker can see its set-aside questions; nothing that counts what waits on Jeremy does.**
  Split between "shown, marked, by default" (homelab, harland, quarterdeck, warpgate, jeremys-mac:
  own section) and "hidden by default, `--set-aside` flag" (pmo). Shown in their own marked section
  of the asker's `list` and `answers` meets both: never in a count, never mistaken for open.
- **The event carries the question id, page, who archived and when, and the asking session**
  (pmo, jeremys-mac).
- **Partner events for the hub**: two new kinds, `set_aside` and `reopened`, shaped like
  `withdrawn`. Names go to quarterdeck before shipping; the hub lands its side first.
