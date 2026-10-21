# Feedback: replying under an answer note Jeremy leaves on a decision (21 Oct 2026)

When Jeremy answers a decision he can add a note ("could we go smaller?"). Today the office that
asked can only append an outcome, which is one-way: he is not told, and it cannot ask him
anything back. So the conversation moves to Slack, away from the question it is about. The case
that prompted the item: runcove-pmo wanted to confirm its reading of one of his notes under it.

Asked of every office: (1) has a note of his needed answering lately, and where did you answer
it; (2) have you used `outcome` for that; (3) one message or a short thread; (4) how you want to
hear his reply; (5) whether a reply may change the decision. Condensed from the replies of all ten
offices; retrobrew answered after the design was decided, and agrees with it.

| | A note needed answering | Used `outcome` | Shape | Hear his reply by | Reply changes the decision |
|---|---|---|---|---|---|
| harland | no; decisions go through Slack asks | never | thread, short ("did you mean X?") | quarterdeck message (no waiter) | no; may flag a conflict |
| cove | not on a page; it asks in #hq-cove and plays back there | no | thread (play back, confirm) | both: waiter when waiting, quarterdeck when busy | never; withdraw and ask again |
| jeremys-mac | **yes, twice today**, on its fast-VM plan page; both answered in Slack | yes, for both: it records, but he is not told and it cannot ask | thread | quarterdeck message and waiter (the waiter times out and dies at restart) | no; close and ask anew |
| gg-ops | no (Slack asks); one Slack correction after his answer | no | thread (correction, clarification) | quarterdeck message (no waiter) | no; a new question replaces the old |
| ch | not on a page; a Slack reply with a plan worked | no | thread | quarterdeck message | no; replaces |
| warpgate | none | no | **one message**; back-and-forth belongs in Slack, where he answers | quarterdeck message | no |
| runcove-pmo | **yes**: wanted to confirm its reading under the note; the substance went to Slack, so it split | no; an outcome is not a question back | thread (mirroring needs a round trip) | both: quarterdeck wakes it, waiter as fallback | no; he re-answers if it changes |
| quarterdeck | not lately (Slack asks) | no | thread (clarify, then a short answer) | the page office's waiter; a quarterdeck message as well would arrive twice | no |
| homelab | no (Slack asks) | no | **one message**; back-and-forth belongs where he already reads | both | never |
| retrobrew | no; it asks in Slack only | no page questions | thread | quarterdeck message | no |

## What it decided

Jeremy answered three questions on the design page (21 Oct 2026):

- **A short thread under his note** (8 of 10 offices): the office replies, he can answer, and the
  office can answer back. Not one message only (homelab, warpgate).
- **Any office that can open the page may reply**, as with comments: not only the office that
  asked. This matches his earlier rule that access to a page is the one control over his
  comments.
- **He hears of a reply on his wall**: the page shows as unread, and the card names who replied.
  This covers replies under his notes **and under his comments**, which today tell him nothing.
  He also asked for a quarterdeck alert; where it lands and how often it fires are with him.
- **A reply never changes the decision** (all ten offices). If it should, the office asks a new
  question that replaces the old one, or he answers again.
- **Delivery to offices**, as for comments: the page office's waiter gets the event. Not settled:
  harland, gg-ops, ch and warpgate run no waiter and want a quarterdeck message, which the service
  cannot send today.
</content>
</invoke>
