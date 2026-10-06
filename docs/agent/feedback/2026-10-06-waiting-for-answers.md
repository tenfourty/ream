# Feedback: an agent hears only the answers that are its own (6 Oct 2026)

Asked of every maintainer, on Jeremy's rule that agent-facing changes are shaped by their users.
Questions: (1) how they hear his answers today, (2) whether it wakes for things not theirs,
(3) which filter — (a) session label, (b) office prefix, (c) page, (d) none, (4) whether someone
else's unacknowledged answer should keep re-waking them. Condensed from their replies.

| | Today | Filter | Not mine | Mine, unacknowledged |
|---|---|---|---|---|
| homelab | No waiter. Page answers reach it via the board's 3-hourly feed, so one sat unseen from 30 Sep to 6 Oct | (b) as default, (a) for one session; unlabelled questions under a shared key go to the key's owner office, once | show once, then stop | keep re-waking, rate-limited (~hourly) — the 30 Sep failure |
| cove | Unreliable: re-arms only when a question is open, sometimes forgets; "a missed wake-up is the failure I'd most like designed out" | (b); (c) only as an extra narrowing | show once, never again | keep re-waking until acked |
| warpgate | Low volume; `wait --page` re-armed after each wake, `list` at turn start | (b), defaulting to its own; (a) misses answers asked before a compaction; (c) as an extra for pages owned by another key | show once, then quiet | keep re-surfacing until acked |
| quarterdeck | `artifacts-wait --session qd-maintainer/<ref>`, re-armed every ~47 min with no news | (b): in quarterdeck an answer belongs to the office and its current holder; (a) alone is wrong; (c) a bonus. The hub's future artifacts adapter reads the feed per office | should not wake at all | once per waiter (keyed by event cursor); `list` keeps it visible until acked |
| retrobrew | Does not use artifacts yet (a guess) | (b), so a fresh or compacted session hears answers asked by an earlier one | — | — |

Also asked for:
- homelab: an event-driven one-shot (`wait --office homelab-maintainer --once`) it can run as a
  systemd unit appending to its mailbox, so no new session-side polling.
- quarterdeck: a longer limit, or an option to keep waiting, to cut the empty wake every ~47 min.
- cove: the same shape as its PR watcher — a re-armed loop with a separate guard that alerts when
  the loop is down.

## Where they agree
- Filter by **office prefix**, defaulting to the caller's own office; page as an extra narrowing.
- **Someone else's answer** never re-wakes: shown at most once.
- **Your own unacknowledged answer** must not be lost — but re-waking every time turns one missed
  ack into a busy loop. Middle ground they all accept: surface it again, rate-limited, and keep it
  in `list` until acked.
