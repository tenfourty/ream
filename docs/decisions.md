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

## Selecting text always offers Comment, even in pin mode, until commenting is redesigned (5 Nov 2026)

**Chosen:** a text selection made while pin mode is on leaves pin mode and shows the selection bar
(Comment, Remove this, Looks good). A click that picks an element still works as before. Why: pin
mode held the bar back with nothing on screen saying so, and Jeremy, who never meant to turn it on,
could not comment on any page. This is a stopgap. The whole reader-side comment experience is being
redesigned from the best existing tools, and that redesign may replace pin mode entirely.

**Not chosen:**
- No stopgap, wait for the redesign: leaves a reader who presses C by accident unable to comment
  for days.
- Keep holding the bar back but show a strip saying pin mode is on: still blocks what the reader
  plainly asked for, which is to comment on the words selected.

**Evidence:** confirmed (Jeremy, 5 Nov 2026)

## Commenting: a corner button replaces pin mode, one chord shortcut, a page button, Open and Resolved tabs (5 Nov 2026)

**Chosen:** commenting on a chart, table or picture is a small Comment button in that element's
corner, shown when the pointer is over it (or it has keyboard focus, or after a first tap on
touch); pressing it starts a comment on that element. Pin mode, the Pin button and the bare C key
are removed. The only commenting shortcut is Ctrl+Shift+M (Cmd+Shift+M), which comments on the
selected words. A "Comment on page" button in the page bar starts a whole-page comment. The
comments panel has Open and Resolved tabs, and a resolved thread can be reopened. Why: pin mode
was a loud state the reader entered by accident, could not see the way out of, and blocked the
page's own clicks and the selection bar while on (it left Jeremy unable to comment on 5 Nov);
a button next to the thing itself needs no mode and no key to learn, and what agents receive is
unchanged.

**Not chosen:**
- A loud picking mode that ends after one comment: still a mode to enter, to see and to leave,
  and still swallows the page's clicks while it lasts.
- Both the corner button and a picking mode: two ways to do one thing, and the mode's faults stay.
- Keeping C as a shortcut shown on the button: a single letter fires by accident, in a page that
  has its own keys, and was how the mode got turned on unmeant.
- A whole-page comment only at the panel's foot: not found by a reader who has not opened the
  panel.
- A resolved fold at the bottom of the panel: resolved threads sit below every open one and are
  not found again; tabs keep both lists in view one click apart.

**Evidence:** confirmed (Jeremy, 5 Nov 2026)

## Inside a folder, the All tab names the folder and hides when it is alone (5 Nov 2026)

**Chosen:** on a folder's page the first tab reads "All in <folder name>" (the name escaped), and
when it is the only tab the tab row is not drawn at all. Waiting on you, Pinned and the other
tabs still appear once they have something in them, as on the wall. Why: "All" inside a folder was
already selected and reloaded the same view, so it read as a way out of the folder and did
nothing; the label now says what it covers, and a button that can do nothing is not shown.

**Not chosen:**
- Make All leave the folder and show the whole wall: the Gallery link already does that, and two
  ways out of one folder confuse.
- Keep it as it was: a selected button labelled "All" that reloads the same view, which a reader
  takes for a way out.

**Evidence:** confirmed (Jeremy, 5 Nov 2026)

## A sent form shows as a read-only summary with a Change answers button (5 Nov 2026)

**Chosen:** once Jeremy has sent a form, the page replaces it with a read-only summary: plain text,
each question and the answer he sent, and when. A Change answers button under it brings the form
back with his answers filled in, and the page bar then offers Send again; after he sends, the
summary returns. Reloading the page shows the summary again while his send stands. The button is a
real button, reachable by Tab, and focus lands on it (or on the form's first field) when the view
changes. What agents receive and the API do not change: only what the gallery draws. Why: a filled
form with a Sent bar and a Send again button read to him as strange, as if something were still
to be filled in.

**Not chosen:**
- Keep the form but locked read-only, with an Edit button: it still looks like a form to fill in,
  and the strangeness he named stays.
- Keep it as it was (an editable filled form, a Sent bar and Send again): the thing he found
  strange.

**Evidence:** confirmed (Jeremy, 5 Nov 2026)
