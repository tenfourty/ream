# Feedback: is the agent listening, and comments pinned to a spot (28 Oct 2026)

Design that came of the listening answers: `docs/agent/specs/2026-10-29-where-an-answer-goes.md`.

Two items, asked together before either was designed:

- **runcove-5t3oj.26**, showing on a page whether the agent that asked is waiting for an answer;
- **runcove-5t3oj.53**, comments pinned to an element or a text selection, with images attached.

Asked of every office on 28 Oct at 23:14 UTC (#bots thread 1793229284.566489):
(1) how you wait for an answer today, and for how long; (2) what the page should say when no
session is waiting; (3) whether you would read the status through an API; (4) what you need to
act on a pinned comment; (5) whether you can read an attached image, and what size or format
troubles you; (6) what should happen to a pinned comment when a republish removes its spot.

Ten offices answered within a minute; ch followed about three minutes later.

| | (1) How it waits | (2) Page says, when nobody waits | (3) Status API | (4) Pinned comment needs | (5) Images | (6) Spot gone |
|---|---|---|---|---|---|---|
| runcove-pmo | `artifacts-decide wait --timeout 0 --office` in the background, session start to restart (hours); a gap while a new session starts | "not listening right now; your answer is delivered to runcove-pmo in Slack" | yes, at session start, to avoid a second waiter | quote + nearest heading, then element id; stable ids on sections, not rows; screenshot only for layout | local path; URL only without auth; PNG/JPEG, a few MB; very tall captures unreadable | outdated in place with its quote, **and** listed at page level |
| warpgate | not on pages; quarterdeck asks | where it goes ("delivered to the warpgate office in Slack") | probably not | quote + heading; ids only on tables | local path, PNG/JPEG, a few MB; an authed URL needs an allowed fetch | outdated in place with its quote, never moved silently |
| wolff | not yet; would use the bridge, not a blocking wait | where it goes; "not listening" reads as lost | yes, so a restarted session doesn't ask twice | quote + heading; id a bonus; would add ids if anchors use them | local path, PNG/JPEG; over ~5 MB or ~4000 px is a problem | outdated in place with quote, **and** at page level |
| cove | `artifacts-decide wait` in the background plus `list`/`comments` each tick (20-60 min); a wait lasts until restart or compaction | where and when: "reaches the office at its next tick (within the hour)" | yes, read-only and scoped to its own office | quote + nearest heading id; selector only if stable; markdown pages have heading ids, HTML doesn't | local path, PNG/JPEG/WebP under ~5 MB; not an authed URL | outdated in place with quote, **and** at page level |
| jeremys-mac | mostly quarterdeck asks; on pages, a background wait of minutes to hours that dies with the session and with the laptop's sleep | "no session is waiting; your answer is delivered to the jeremys-mac office in Slack" | yes, at session start | quote + heading; ids on headings/sections, not promised stable; screenshot for layout only | local path, PNG/JPEG/WebP; authed URL harder; over ~5 MB or very tall is a nuisance | outdated in place with quote, **and** shown at the top; never closed silently |
| harland | quarterdeck asks; on pages, `list` at its next turn | "delivered to harland in Slack", true whether or not a session is up | yes, if cheap | quote + heading, plus element id when there is one; screenshot only for visual spots (charts) | local path or fetchable URL; PNG/JPEG/WebP under ~5 MB; HEIC or very large is the problem | outdated in place with quote; no silent fallback |
| gg-ops | `list` at the next routine check (15 min to 4 h); nothing holds a wait open | "delivered to gg-ops in Slack; it acts on it at its next check" | yes, one cheap call | quote + heading; id if present; generated pages mostly have no stable ids | local path, PNG/JPEG under ~5 MB and 4000 px a side | outdated in place with quote; page level only if the quote is found nowhere |
| homelab | quarterdeck asks, woken by its mailbox watcher; no open wait | "delivered to the homelab maintainer in Slack; it acts at its next wake" | yes, read-only given a page id | quote + heading; decision-card ids are stable, others not | local path; URL only without auth; PNG/JPEG under ~5 MB; tall captures lose detail | outdated in place with quote; never moved silently |
| retrobrew | not on pages; quarterdeck asks | "delivered to the office in Slack" | n/a today | quote + heading; bead ids are its stable anchors | local path, PNG/JPEG/WebP; over ~5 MB is a problem | outdated with quote; its board is redrawn every 30 min, so spots move often |
| quarterdeck | `artifacts-wait --session`, 40 rounds of 280 s (~3 h), re-armed while the session lives | "delivered to the office in Slack" | yes, to see if another session already waits | quote + heading; would add section ids if anchors key on them | local path, PNG/JPEG, a few MB; scale down phone captures | outdated in place with quote, **and** at page level |
| ch | not at all; quarterdeck buttons in #hq-ch, never blocked on a page | "delivered to the ch office in Slack" | yes, read-only, so a second session doesn't ask or wait twice | quote + heading; element id when one exists (ids on headings only) | local path, PNG/JPEG under ~5 MB; huge or animated images not useful | outdated, pinned to its quote, **and** shown at page level |

## What it shapes

**Listening (runcove-5t3oj.26)**

- **Every one of the eleven says the same about the empty state:** a bare "not listening" reads to
  Jeremy as "your answer is lost". The page must say **where the answer goes and when it is
  acted on**, e.g. "no session is waiting; your answer is delivered to cove in Slack, acted on
  at its next check". So the item is "where your answer goes", of which "a session is waiting
  now" is the best case, not a listening light.
- **Most offices don't hold a wait open at all.** Eight route Jeremy's questions through
  quarterdeck asks, or read `list` at their next check. Only pmo, cove and quarterdeck hold a
  long background wait, and theirs die with the session. "Waiting right now" will be false most
  of the time, which is why the delivery route has to carry the message.
- **Ten of eleven would read the status through the API**, almost all for one reason: a fresh
  session checking whether another session of the same office already waits, before arming a
  second one. Read-only and scoped to the calling key's own agent (cove), as every agent route is.

**Pinned comments (runcove-5t3oj.53)**

- **Anchor on the quoted text and the nearest heading.** All eleven name these two first; element
  ids are a bonus, and most pages carry stable ids only on headings, sections or tables. A CSS
  selector alone would break on generated pages (gg-ops, retrobrew, homelab). Several would add
  section ids if the anchor uses them, so prefer an id when the page has one.
- **Images: a local download path, PNG/JPEG/WebP, about 5 MB and 4000 px a side.** An
  authenticated URL is a problem for most sessions, so the CLI should fetch images to a path.
  Very tall full-page captures lose detail when downscaled; HEIC is unreadable to one office.
- **A comment whose spot is gone stays where it was, marked "outdated", showing its quote.**
  None want it moved silently. Six (pmo, wolff, cove, jeremys-mac, quarterdeck, ch) also want it
  listed at page level so it can't be missed; gg-ops would re-find the quote if it moved.
- A screenshot of the spot is wanted only for layout or chart comments, so it is optional.
