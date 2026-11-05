# Artifacts decisions

What was decided about the artifacts service, why, and why each other option lost: one entry per
decision, newest last. The rule and the entry format are runcove's `ways-of-working/decisions.md`.
The designs under `docs/agent/specs/` hold the detail a decision led to; this file holds the choice.

## Archiving a page sets its open questions aside instead of closing them (2 Nov 2026)

**Chosen:** archiving or binning a page sets aside every question on it that is still open. While
the page is archived or binned, its questions show nowhere Jeremy looks (his wall, his waiting count,
the page itself, any office's list of what waits on him), and he cannot answer them. Unarchiving or
restoring brings them back open and unanswered, keeping their place by when they were first asked.
The agent that asked is told both times. Archiving a folder does the same to every page in it. Why:
he archives a page to get it off his wall, not to throw its questions away, and a question that
comes back with its page saves the agent asking again. This replaces the 21 Sep 2026 rule that
archiving or binning a page closes its open questions.

**Not chosen:**
- Keep the 21 Sep rule, archiving closes the questions: hides them just as well, but a question is
  lost for good if the page comes back, and the agent has to notice and ask again.
- Leave the questions open with nothing else changed, as before 21 Sep: the waiting agent could not
  tell "not answered yet" from "never going to be shown", and an office's waiting list once put such
  a question in front of him 24 minutes after its page had left his wall.

**Evidence:** confirmed (Jeremy, 2 Nov 2026)

## The Older pages link is proven by CI until a real view passes 200 pages (5 Nov 2026)

**Chosen:** the Older pages link on the wall (it shows the next 200 pages of a view past the
200-page cap) counts as proven by CI's tests, which page the wall, a folder and the Archived tab,
search and filters included. The maintainer checks it in the browser the first time a real view
passes 200 pages. Why: no view in production holds more than 200 pages yet (the widest held 162 on
5 Nov 2026), so there is nothing for him to tap, and the tests already cover what a tap would show.

**Not chosen:**
- Fill a throwaway folder with 205 blank pages so he can tap the link at once, then delete them:
  real proof today, but it clutters the wall, the events and every office's reads to prove what the
  tests already prove.
- Leave it unproven until a real view passes 200 pages, then ask him: keeps the work open for weeks
  with nothing left to do on it, and puts the check on him when the maintainer can make it.

**Evidence:** confirmed (Jeremy, 5 Nov 2026)

## The company deployment's design restarts now, and building waits for the core clean-up (5 Nov 2026)

**Chosen:** design work on the company deployment (artifacts for the whole of GitGuardian)
restarts now. Every question still open on it (four from 26 Oct and four from his 27 Oct page
comments) is withdrawn from where it was asked and asked again in one new thread in
#hq-artifacts. Each question is first revised against what has changed since then, and any that
newer work already settles is dropped. The existing design page is republished as a new version,
not replaced by a new page. That version takes in what has shipped since 27 Oct (pinned comments,
forms, receipts), the core-and-plug-ins design, and the 4 Nov ruling that the core stays
single-reader until this design adds many readers. Building the company deployment comes after
the core clean-up. Why: he expects to start the company deployment soon, and its questions were
scattered over two threads and partly overtaken by later work.

**Not chosen:**
- Build the company deployment before the core clean-up: it would add many readers on top of
  the household-specific code the clean-up removes, so that code would have to be untangled twice.
- Move only the four 27 Oct questions and leave the 26 Oct four in their own thread: keeps two
  threads for one design, which is what made the open questions hard to follow.

**Evidence:** confirmed (Jeremy, 5 Nov 2026)
