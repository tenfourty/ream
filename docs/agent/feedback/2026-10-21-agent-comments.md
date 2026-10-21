# Feedback: an agent comments on another office's page (21 Oct 2026)

On 21 Oct 2026 Jeremy asked that agents be able to comment on pages the way people do, and chose
to hand the work to this office (decision `d_01M5F62PPB5D3KC6C70K9NN9HS`, runcove-5t3oj.36). Two
rules came with the hand-off: an agent's comment never counts as his answer to a decision, and
every comment shows which office wrote it. The case that prompted it: ch reviewed cove's
start-at-boot design through a forge branch and Slack, away from the passages it was about.

Asked of every office: (1) have you reviewed another office's work lately, and how; (2) how you
want to hear of a comment on your own page; (3) whether page id plus quoted text is enough to
anchor one; (4) whether another office may say "looks good" or "remove this"; (5) whether your key
is your office's alone. Condensed from the replies of all ten offices.

| | Cross-office review today | Hear of a comment by | Anchor | Verdicts | Key |
|---|---|---|---|---|---|
| harland | forge PRs; a report file on a runcove branch | quarterdeck message (no waiter) | quote, with a heading fallback when the text changed | as advice from that office | shared (jbrown-dev), sets the session label |
| homelab | forge PR reviews, discussion in Slack | quarterdeck message only | quote; section for repeated phrases | as that office's own review, shown as such | its own |
| ch | forge branches and PRs, verdicts in Slack | quarterdeck message (no waiter) | quote plus optional heading; record the page version | as opinion ("ch's review: yes"), never a resolved state | shared; a session id cannot tell ch from harland |
| jeremys-mac | PR comments and Slack; ch's view of its plan page stayed in Slack | both, relying on quarterdeck | quote; heading fallback for repeats | his alone; an office may recommend | the Mac's, also used by Jeremy's sessions |
| warpgate | forge PR reviews; answered a page's question in Slack | quarterdeck message only | quote; **refuse a quote that is not unique** | opinion yes, verdict no | its own |
| gg-ops | Slack threads and bead comments | both | quote; section fallback for repeats | his alone; "I'd remove this, because" as a comment | shared; wants the office set by the server, hard to fake |
| retrobrew | none | quarterdeck message (restarts often) | quote; **keep the comment when a redraw removes the quote** | his alone; concerns and questions only | its own |
| cove | daily; a private branch plus Slack after attachments failed | quarterdeck message, a thread per page | quote; no line numbers | as that office's verdict, labelled, input to the owner | none yet |
| runcove-pmo | Slack threads on plans and estimates | both | quote | his alone; suggestions only | its own |
| quarterdeck | none confirmed | quarterdeck message, or both | quote; heading fallback | his alone; suggest with a reason | its own |

## What it decided

- **Delivery is a quarterdeck message**, asked for by all ten. Seven run no waiter or lose it at
  every restart. The `comment_added` event still goes to the page owner's waiter for offices that
  run one (gg-ops, runcove-pmo, jeremys-mac). How the message is sent is agreed with quarterdeck;
  the service knows the page's owning key and session label, not its office.
- **Anchor: page id plus quoted text**, with an optional section heading (harland, homelab, ch,
  jeremys-mac, gg-ops, quarterdeck). A quote that matches more than once in its section is
  refused, and the refusal names the section option (warpgate). The comment records the page
  version it was made on (ch; the table already has `version_n`), and it is kept, with its
  original quote, when a later version drops the passage (retrobrew). Comments on a page today
  behave this way already (`quote_state` "changed").
- **No verdicts from agents.** An agent's comment on another office's page is a plain comment.
  The `remove` and `looks_good` kinds stay Jeremy's. Four offices wanted a labelled office verdict
  and six wanted none. Both sides agreed that nothing an office writes should read as approval,
  and a body that says "looks good from ch's side" already gives the labelled opinion.
- **Which office wrote it** is the hard part. Four offices share jbrown-dev's key, and a session
  label is set by the caller (ch, gg-ops). Shown today: the key's agent and the session label.
  Whether to give each office on a shared machine its own key goes to Jeremy, since minting keys
  is his yes.
- **Not in this item:** a reply thread under an answer note Jeremy left on a decision
  (runcove-pmo). That is a separate change to decisions, filed on its own.
</content>
</invoke>
