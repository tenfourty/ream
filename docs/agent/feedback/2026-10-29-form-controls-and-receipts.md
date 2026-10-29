# Feedback: form controls inside a page, and delivery receipts (29 Oct 2026)

Two items, asked together before either was designed:

- **runcove-5t3oj.54**, form controls inside a page, answered and submitted as one batch;
- **runcove-5t3oj.55**, delivery receipts on feedback: seen, being worked on, answered.

Asked of every office on 29 Oct at 11:17 UTC (#bots thread 1793273849.537579):
(1) which pages would carry in-page questions, and of what kinds; (2) how to write them: HTML
form elements with a `name`, a Markdown syntax, or a JSON block; (3) what a submit must return;
(4) what happens to an answer when a republish renames, removes or changes its field; (5) whether
an in-page question is also a decision; (6) which receipt states, set from where; (7) whether a
read through the API marks a comment seen.

Ten offices answered within a minute. Several said their use would be light: cove asks Jeremy
only through quarterdeck in #hq-cove (his ruling of 10 Oct), warpgate and retrobrew route every
choice through quarterdeck asks, and wolff rarely publishes.

| | (1) Kinds | (2) Write | (3) Submit returns | (4) After a republish | (5) Decision? | (6) Receipts | (7) Read = seen? |
|---|---|---|---|---|---|---|---|
| ch | single choice most; yes/no per table row; short free text | HTML `name` | keyed by name, version, who; partial if empties named | tied to its version; "answered on an earlier version" | yes; a row of yes/no = several decisions | seen + answered auto, working by CLI; auto on `pickup` only | no: a triage read is not "heard" |
| warpgate | none today; yes/no per row, single choice | HTML `name` | keyed, version, submitter; **no partial** | stale, never carried | yes, one audit trail | seen on `pickup`; working, answered by CLI | no |
| harland | single choice; free text; yes/no per row | HTML `name` | keyed, version, submitter, time; partial with blanks marked | stale on the new version | single choice yes; free text and rows plain fields | seen on `pickup`; working, answered (with link) by CLI | no |
| retrobrew | single choice; yes/no per row; free-text note | HTML `name` | keyed, version, who; partial with blanks | stale; a removed field's answer kept in history | yes | seen on `list`/`pickup`; working, answered (with link) by CLI | yes |
| cove | none today; single choice per row; note | HTML `name` | keyed, version, partial flag | "answered on an earlier version" | yes (handoffs track decision ids) | seen + answered by CLI; no "working" (noise) | no |
| wolff | single choice; yes/no per row; free text | HTML `name` | keyed, version; partial if marked | stale | yes | seen on `list`/`pickup`; working, answered by CLI | yes |
| gg-ops | single choice per finding; yes/no per row; **date/time**; note on any field | HTML `name` | keyed, version **and content hash**, who, when; each field answered or blank | same name and options keeps it; otherwise a stale list, shown and returned: "an answer to old wording is not a yes to the new one" | each field, or one marked as such | seen, answered by CLI; add **working with a note** and **blocked** | no: a subagent's read that then fails |
| runcove-pmo | single choice; yes/no per row; free text; occasional **ranking**; rare number/date | HTML `name` (JSON close second) | keyed, version, who, when, blanks; partial yes | stale if removed or options changed; republishes twice a day | each field can be; **one submit wakes the waiter once** | seen auto on `list`/`comments`; working by CLI; answered auto on `reply` | yes |
| quarterdeck | 3-8 rulings per review, single choice + optional note; rare yes/no per row | HTML `name` (JSON second; no new Markdown) | keyed, version, **submit id**; skipped fields marked; two sittings = two events, later wins per field | carried only if name and options unchanged; else stale to both; removed kept in history | each field, grouped by page | seen, working, answered, **won't do** with a reason; by CLI; if automatic, seen on `pickup` only | no: listing during restarts is not taking it in |
| homelab | single choice per decision and per row; short "why"; rare number | HTML `name` | keyed, page id, version, identity, time; partial with blanks | kept on the old name and version, flagged orphaned | yes, each field; matters most (today it asks through quarterdeck to be able to wait) | seen on `list`/`pickup`; working, answered (with link) by CLI | yes, but a morning sweep by another process should not count |
| jeremys-mac | yes/no per row; one free text; single choices | HTML `name` | keyed, version, who; partial if empties named | stale on the new version | yes | seen on `pickup`; working, answered by CLI; answered auto on a page reply | `pickup` yes, `list` no |

## What it shapes

**Form controls (runcove-5t3oj.54)**

- **Plain HTML form elements with a `name`, unanimously.** Every office already writes or
  generates HTML, and a page must stay readable without the service. A Markdown syntax was
  rejected outright (quarterdeck); a JSON block is a distant second (pmo, quarterdeck).
- **Three kinds carry nearly all the use:** single choice (lettered, recommendation first), a
  yes/no or choice **per row of a table**, and a short free-text note beside either. Date/time
  (gg-ops), number and ranking (pmo) are rare. Several choices: nobody needs it yet.
- **One event per submit**, keyed by field `name`, carrying the page version (gg-ops: and its
  content hash), who answered and when, and a submit id. Blank fields are **marked, not
  dropped**. Nine allow a partial submit; warpgate does not ("half a batch on a release decision
  is worse than none"), so a page should be able to require a complete submit. Answering in two
  sittings is two events, the later winning per field (quarterdeck).
- **An answer is bound to the version it was given on.** It carries forward only when the field's
  name and options are unchanged (gg-ops, quarterdeck); otherwise it is **stale**, shown to Jeremy
  and returned to the agent, and a removed field's answer stays in the history. Never re-mapped
  silently: every office says so.
- **Each question can be a decision**, so `wait`, `record` and the audit trail work unchanged:
  ten of ten. harland would keep free text and table rows as plain fields of the submit; pmo needs
  one submit to wake a waiter **once**, not once per field.

**Delivery receipts (runcove-5t3oj.55)**

- **"seen" splits the offices.** Four (retrobrew, wolff, pmo, homelab) want a read through the
  API to mark it seen, because a deliberate step would be forgotten. Six (ch, warpgate, harland,
  cove, gg-ops, quarterdeck) want it deliberate or on `pickup` only, because a list or a
  subagent's read is not "heard", and a false "seen" tells Jeremy he was heard when he was not.
  jeremys-mac sits between: `pickup` yes, `list` no. **Seen on `pickup` (and an explicit call),
  never on `list`** satisfies nine of ten; this goes to Jeremy as a decision.
- **"working" and "answered" are deliberate**, from the CLI. `answered` carries a link (commit,
  PR, thread), and pmo and jeremys-mac would set it automatically on a reply. Asked for besides:
  a one-line note on "working" and a "blocked" state (gg-ops), "won't do" with a reason
  (quarterdeck). cove would drop "working" as noise.
