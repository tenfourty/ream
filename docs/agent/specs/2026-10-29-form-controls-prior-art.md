# Prior art: form controls inside a published page (research, 29 Oct 2026)

Researched for runcove-5t3oj.54 (and the edge of .55, delivery receipts) at Jeremy's request. The
comparison is lavish-axi (https://github.com/kunchenguid/lavish-axi, read on `main` on 29 Oct 2026
through raw.githubusercontent.com, nothing cloned or run). Paths below are lavish paths; line numbers
are from that day's `main` and will drift. Artifacts paths are in this repo. Claims without a path are
unverified.

Not verified: lavish's server-side handling of a queued prompt's `data` (I read the SDK and the docs,
not all 1,155 lines of `src/session-store.js`), so "no server validation" below means "none described
or visible", not "none exists".

## 1. How lavish does it: there is no form feature

Lavish has no field schema. The page author writes ordinary HTML controls and a few lines of
JavaScript, and the "form" is a convention for calling one function.

**Declaring fields.** Native `<input>`, `<select>`, `<textarea>` inside a wrapper carrying
`data-lavish-question="<key>"`. The playbook says "Pair each question with a Submit or Queue answer
control that sends exactly one prompt for the final answer" and gives a radio example and a
checkbox-batch example (`src/playbooks.js` 195-228). Native controls and ARIA-role widgets are
interactive automatically; anything custom needs `data-lavish-action` (`src/playbooks.js` 205, 208).
Pages meant as forms set `data-lavish-annotate="off"` on `<html>` (`src/playbooks.js` 204), because
otherwise a click annotates rather than operates the control.

**Collecting values.** The author's own `onsubmit` handler reads `FormData` or `:checked` inputs and
calls `window.lavish.queuePrompt(text, {tag, text, element, data, queueKey})`
(`src/artifact-sdk.js` 1218-1248). `data` is appended to the prompt as
`"\n\nContext data:\n" + JSON.stringify(data, null, 2)` (1233). So what the agent receives is prose
plus a JSON block inside the same string, not a typed record.

**Batching.** Two levels. One question is one prompt, queued once (the playbook forbids queueing from
`change` handlers, 206 and 213-214). Several prompts then sit in a queue in the reader's tab
(`sessionStorage`) and go out together on "Send to Agent", or earlier if the page calls
`sendQueuedPrompts()` (`src/artifact-sdk.js` 1250-1252, `docs/invariants.md` 43). Re-answering a
question before sending replaces the earlier unsent answer: prompts sharing `_lavishQueueKey` replace
each other (`docs/invariants.md` 43). The key is derived at `src/artifact-sdk.js` 18-90: explicit
`queueKey`, else the enclosing `data-lavish-question`, else `radio:`/`checkbox:`/`field:` plus form scope
plus control name. The agent never sees the key; it is stripped before POST.

**What the agent gets, and how.** The agent runs `lavish-axi poll <file>`, which long-polls and returns
`{prompts, artifact_failures, next_step, dom_snapshot}` in that order, deliberately so truncation drops
the snapshot first (`docs/invariants.md` 61, `src/cli.js` 592-617). `next_step` is a long instruction
telling the agent to poll again after acting and to reply with `--agent-reply` (`src/cli.js` 357).
Delivery is a destructive take under a store lock; a poll that disconnects before the write puts the
batch back, prepended so order holds (`docs/invariants.md` 63-64). The `/prompts` route is same-origin
guarded: only lavish's own chrome can queue, because what lands there is treated as the reader's
instructions (`docs/invariants.md` 47).

**When the page changes after answers.** A live reload replaces the document. The SDK reports the
values of controls inside `data-lavish-question` scopes to the chrome on every `input`/`change`
(`src/artifact-sdk.js` 2113-2150, 2216-2222), and the chrome replays them into the new document
(`restoreReviewState`, 2161-2178). Matching is by key (question, name, type, value), falling back to
question plus position. Limits: 200 controls, 2,000 characters per value, passwords/files/buttons
skipped. It sets `.value`/`.checked` directly and fires no events, so handlers do not re-queue old
answers (2169-2170). Controls outside a `data-lavish-question` scope are deliberately left alone:
"Lavish cannot tell an intentional reset from a preserved answer" (2084-2085). Answers already sent
are history and are not re-opened; a changed question simply has no stored match and starts empty.

**Validation.** Browser-native only (`required`, `type`, `pattern` via the author's form). Nothing in
the SDK checks the answer against a declared schema, and the agent cannot ask for "a number between 1
and 10" other than by writing the HTML for it. The playbook asks authors to make prompts "specific
enough that the agent can act without asking a follow-up" (`src/playbooks.js` 213). The only
size-limit machinery I saw is for image attachments.

**Receipts.** `docs/delivery-acks.md` describes per-note Seen / Working / Done, stamped on the chat
entry. Seen is `delivered_at`, set when a poll takes the batch. Working is set by the first
artifact reload after delivery (the file watcher), explicitly weak evidence ("the file changed", not "the
agent is on your note", lines 94-96). Done is set by the next agent reply, so a progress reply reads as
Done (89-93). Stamps are positional rather than by id, a failed poll un-stamps its own take only, and
old transcripts are marked `receipt: "none"` rather than guessed (lines 55-80). The reader sees three
greyed emoji under each note. No agent action is needed for any of it. It applies to all feedback, not
to form answers specially.

## 2. What would carry over

- **Batch as the unit, sent once.** One explicit Submit, never per-keystroke or per-click. Matches
  what the gallery's decisions do already and is the property Jeremy asked for.
- **Replace-by-key until sent.** A reader who changes their mind before submitting replaces rather
  than stacks. Here the key is the field id.
- **Draft survives a republish, quietly.** Replay values by key with fallback to position, set values
  without firing events, cap count and length, skip passwords. All of it is cheap and each rule is
  there for a reason lavish already hit.
- **Stamps on the entry, not a side table**, and Seen/Done need no new duty from the agent
  (`docs/delivery-acks.md` rejects an explicit `ack` command for that reason). Our `acknowledge`
  (`decisions.py` 1751) is already an explicit Seen. Lavish's lesson is to derive what we can from
  facts the server holds anyway (the agent fetched it; the agent published a new version).
- **Honest reasons before Seen.** "No agent is listening" beats a blank. We have `GET /listening`
  (`api.py` 2712) for the same fact.
- **Un-stamp on a failed delivery.** Our events journal is read by `since=<seq>`, so Seen should be
  stamped on the agent's successful read, not on the answer being stored.
- **Data in a block, prose on top.** Worth copying only the idea that the agent gets both a
  readable summary and structured values. Ours should be a typed JSON record, not a string.

## 3. What conflicts with this service

1. **`form-action 'none'` and a page that cannot submit.** `content.py` 33-37 sets `form-action
   'none'`, `connect-src 'none'` on old versions, and a content origin that "only answers a framed
   request holding a capability token". `csp.py` 121-127 warns authors who write a `<form>` and points
   them at `data-artifact-decision`. This is deliberate: "the trust boundary that stops a page forging
   an answer". Lavish's model, where the page's own JavaScript composes the reply, is the thing this
   boundary exists to refuse. The page is the agent's code; an answer built by it is the agent writing
   to itself.
2. **The page cannot be trusted to say what was answered.** In lavish the chrome only trusts that the
   click came from its own origin; the content of the prompt is whatever the page's script built (and
   the trust is fine, since the page and the agent are the same party and the reader is the only
   reviewer). Here the reader's answer is evidence, and the design says the agent may not answer "as
   him". So the values must be read by the gallery (or by our shim and checked by the gallery) and
   stored against the field definitions the server holds, never composed by page script.
3. **Existing precedent: the gallery draws the control, not the page.** Decisions already work this
   way. The page marks `data-artifact-decision="<id>"`, the shim reports each slot's rectangle by
   `postMessage` to the gallery origin only (`content.py` 262-303), and the gallery lifts its own
   control over the slot (the page reserves `min-height`). Form controls can follow: the page names
   a slot, the gallery draws the controls from a schema stored server-side. Native `<input>` inside
   the sandboxed iframe with `allow-same-origin` and a gallery-readable value is the alternative
   (section 4, Q1).
4. **Fields have to be stored server-side, so they are an API object, not markup.** "Every human
   action has an agent equivalent" and "scope is the calling key's own pages": the agent must be able
   to declare the fields, read the batch, and fetch the state, all through `/api/v1`, and only for
   its own page (not-yours answers identically to no-such-page). A control present only as HTML in a
   body fails the first of those and cannot be validated or listed. It also means a form is a sibling of
   `POST /artifacts/{id}/decisions` (`api.py` 1883), not of the comments shim.
5. **A batch is not a decision, and the current decision model is one letter.**
   `_validate_options` (`decisions.py` 236-262) takes two to `MAX_OPTIONS` lettered options with only
   `letter` and `label`, and `answer` takes one `letter` and a `note` (1422-1436). Free text, numbers,
   multi-select and "several questions as one submission" do not fit without a new object. A whole
   form could be one new decision `kind` (the column already exists, `decisions.py` 138) with a
   `fields` list and an answer that is a record, or a separate table; see Q2.
6. **Republish semantics.** Versions are paths and only the current version is live (D11, design.md
   58-140). Lavish keeps one document per session and reloads it in place. Here an answer is
   made against version N of a form. If N+1 changes the fields, the stored answer needs to say which
   version and which field ids it answered, and a reader looking at N+1 must see which values were
   carried, which were dropped and which are new. We already do something like this for decision slots
   whose id no longer resolves (`decisions.py` 2108-2135, `unresolved_slots`).
7. **No secrets in answers, and answers are scanned.** Pages, comments and state read-backs all pass
   the local scanner (design.md D12, D15); a field answer is also reader-written content that an agent
   will read. A text field is a path for a pasted credential. Lavish skips `password` inputs from
   replay only; it has no equivalent of D15's "refuse the read loudly".
8. **Agent delivery is pull, not a long-poll that destroys on take.** Ours is the events journal
   (`GET /api/v1/events?since=`) plus an optional nudge (`nudge.py`, never blocking the request). That
   is better than lavish's take-and-restore: nothing to restore, and a lost reader just re-reads. It
   means the batch needs a stable id, a sequence number, and idempotent submission.
9. **Sandbox flags are not a lever.** The iframe has `allow-scripts allow-same-origin allow-popups
   allow-popups-to-escape-sandbox` (`gallery.py` 5201). The content origin differs from the gallery
   origin, so the page can touch its own origin but not the gallery's. Nothing about forms needs a
   flag to be widened, and none should be.

## 4. Open design questions

Q1. **Who draws the controls: gallery or page?** (a) Gallery draws them over a slot from a stored
schema (decision precedent, stays inside `form-action 'none'`, reader sees one consistent control
set, the page cannot lie about labels). (b) Page writes native controls and the shim reports values
to the gallery by `postMessage` (richer layout inside prose, but the gallery must still check values
against a server-held schema, and a page can show a label that differs from the stored one). I would
start with (a), because the existing slot machinery works and the trust boundary stays put.

Q2. **A new decision `kind`, or a new object?** A form answered once, replaced on re-answer, closed
by the agent, appearing in the same unanswered lists and on the same `/decisions` feeds, is a
decision in every way except its answer shape. Reusing the table keeps the outcomes, replies,
`acknowledge`, uncollected-alert and cross-seat code. The cost is widening `answer` from one letter to
a validated record and teaching every consumer of `letter`.

Q3. **Field vocabulary.** A short list, each with its server-side check: `choice` (one of lettered
options), `multi` (subset, min/max), `text` (max length), `number` (min, max, step, integer), `bool`.
Plus `required`, `default`, and `label`. Do we allow the agent to attach a regex? I would say no for
v1: a regex is the agent writing validation that can trap Jeremy in a field he cannot satisfy.

Q4. **Validation: where and what the reader sees.** Server is the authority (agent keys and the
gallery both submit), the gallery repeats the checks for instant feedback, and a refusal names the
field. Does the agent also get told when it publishes a form that can never be satisfied (min above
max, no options)? A `422` at declaration is the cheap version.

Q5. **A page changes after answers.** Options: (a) an answer binds to a form version; a republish
that changes field ids or types starts a new form, with the old answer shown read-only and its
matching fields offered as a prefill the reader must confirm; (b) a republish that leaves ids and
types alone keeps the draft and the answer. Lavish's replay, set silently, is fine for drafts but
wrong for submitted answers, which must not appear to have been given to the new questions.
Never silently carry a submitted answer onto a changed field.

Q6. **Drafts before Submit.** Kept in the reader's browser only (lavish: tab storage) or server-side so
Jeremy can start on his phone and finish at his desk? Server-side drafts are an extra table and an
extra agent-invisible state; browser-only is simpler and matches "the wall is his".

Q7. **Submission is one batch, but can part of it be sent?** If a form has ten fields and he knows
three, is "submit what I have" allowed, leaving the agent a record with missing optional values? I
would say yes if fields are marked optional, and required fields block Submit for the form (not for the
page).

Q8. **Agent equivalent.** Humans submit in the gallery; the agent's twin is the `record` route
(`api.py` 2296), which stores a relay with its source. A form answer relayed by the agent needs the
same marking, and must not count as Jeremy's own for any "answered" alert.

Q9. **Receipts (.55) design choice now, to avoid a rework later.** Lavish stamps Seen on delivery and
Done on any reply. Here: Seen = the agent fetched the batch (read through `events` or the decision
GET), not the answer being stored; Being worked on = an explicit agent call only (`acknowledge` is
that today), because the file-watcher evidence lavish uses does not exist for us and a republish is
not proof; Answered = the outcome or reply on the decision (`api.py` 2371, 2438). Mark Seen as
"fetched", not "read". Do we want an agent-visible reason when nobody is listening?

## 5. Suggested shape for the first slice (a sketch, not a decision)

```json
POST /api/v1/artifacts/{id}/decisions
{ "kind": "form", "slot": "plan-form", "title": "Rollout settings",
  "fields": [
    {"id": "region", "type": "choice", "label": "Region", "required": true,
     "options": [{"letter": "a", "label": "eu"}, {"letter": "b", "label": "us"}]},
    {"id": "replicas", "type": "number", "label": "Replicas", "min": 1, "max": 9, "integer": true},
    {"id": "notes", "type": "text", "label": "Anything else", "max": 500}
  ] }
```

Answer as the agent reads it, one record:
`{"decision_id": "...", "form_version": 2, "answered_at": "...", "answers": {"region": "a",
"replicas": 3, "notes": ""}, "carried_from_version": null, "seen_at": null}`.

Pieces to reuse unchanged: slot reporting (`content.py`), `unresolved_slots`, scan on text values,
events journal, nudge, `acknowledge`, `record`. Pieces to add: the `fields` validator beside
`_validate_options`, a record-shaped `answer`, the gallery renderer, a CSP-warning change (the
`form_element` hint stays true; it just needs to name the new slot), and the version-binding rule
from Q5.

## 6. What not to copy

- Prose-with-JSON-appended as the delivery format. Give the agent a typed record.
- Value matching by position as a fallback for submitted (as opposed to draft) answers.
- Treating the page's script as a trusted composer of the reader's answer.
- Weak Working evidence presented as a fact. Lavish documents its own weakness (`docs/delivery-acks.md`
  94-96); we should not ship a signal we already know is wrong, or label it plainly.
