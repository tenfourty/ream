# Form controls inside a page, submitted as one batch (design, 29 Oct 2026)

Tracker item runcove-5t3oj.54. Design settled: Jeremy answered all four decisions in section 8 on 2 Nov 2026, each (a).
Shaped by the offices' answers (`docs/agent/feedback/2026-10-29-form-controls-and-receipts.md`,
ten offices) and the lavish-axi study (`docs/agent/specs/2026-10-29-form-controls-prior-art.md`).
Delivery receipts (runcove-5t3oj.55) are designed separately; only the one choice that would
force a rework here is settled now (8.3).

## 1. Problem

Today a page can carry questions only as decision cards: one lettered choice and a note each,
drawn by the gallery over a slot the page reserves (`data-artifact-decision`). A review page with
eight rulings, or a table of twenty "keep or park?" rows, becomes twenty cards answered one by
one, and free text, dates and numbers have no home at all. The offices' pages are full of exactly
those shapes (feedback, column 1): single choice with a note, **a yes/no or choice per row of a
table**, a short free-text "why", and occasionally a date (gg-ops' VM windows) or a ranking (pmo).

Jeremy wants to answer questions **inside the page itself** and send them **as one batch**.

## 2. Goals, non-goals, anti-goals

Goals:
- An agent writes ordinary HTML form controls in its page; nothing new to learn, and the page
  still reads without the service (all ten offices).
- Jeremy fills them in where they sit in the page and submits once.
- The agent gets **one event per submit**: every answer keyed by the field's `name`, the page
  version answered, who and when, a submit id, and blank fields marked, not dropped.
- `wait` wakes **once** per submit (pmo); `record` and the decisions feeds keep working.
- An API twin for every action (AGENTS.md): declare is the publish itself; read, list, close
  and relay through `/api/v1`, scoped to the key's own pages.

Non-goals (v1): ranking by drag, several-choice lists beyond plain checkboxes, file inputs,
server-side drafts across devices, agent-supplied validation (regex), conditional fields.

Anti-goals, which would make it a failure even if it worked:
- **The page's own script composing what Jeremy answered.** The page is the agent's code; an
  answer it builds is the agent writing to itself (prior art §3.1-3.2). `form-action 'none'` on the
  content origin stays.
- **An answer silently carried onto a changed question.** Every office said so; gg-ops: "an answer
  to old wording is not a yes to the new one".
- **A second place to look for his answers.** A form answer must appear where decisions do.

## 3. Shape

### 3.1 Writing a form (the agent)

The agent wraps native controls in an element that names the form:

```html
<form data-artifact-form="rollout">
  <fieldset name="region"><legend>Region</legend>
    <label><input type="radio" name="region" value="a" required> a. eu (recommended)</label>
    <label><input type="radio" name="region" value="b"> b. us</label>
  </fieldset>
  <table>
    <tr><td>caps-unit-trim</td><td>
      <label><input type="radio" name="wt.caps-unit-trim" value="keep"> keep</label>
      <label><input type="radio" name="wt.caps-unit-trim" value="park"> park</label></td></tr>
  </table>
  <label>Window <input type="datetime-local" name="window"></label>
  <label>Anything else <textarea name="notes" maxlength="500"></textarea></label>
</form>
```

Supported in v1: `radio`, `checkbox`, `select` (single), `text`, `textarea`, `number`
(`min`/`max`/`step`), `date`, `datetime-local`, with `required` and `maxlength`. Any other
**control** inside the form (`file`, `hidden`, `password`, `button`, `select multiple`, `range`…)
is refused at publish with a named error; other elements (`fieldset`, `legend`, `label`, tables,
prose) are free. No submit button is needed: the
gallery draws Submit (3.3). `<form>` is allowed here; the existing CSP warning (`csp.py`
`form_element`) keeps warning for a `<form>` without `data-artifact-form`.

### 3.2 The server reads the fields at publish

On each publish the server parses the page's HTML (with the `HTMLParser` walk already used by
`anchors.py`, `sections.py` and `pagediff.py`; slots are found by a regex today, not a parse) and
stores, per form, a **field list**: name, type, the label text it found (`<label>`,
`<legend>`, the option's label), options with their values, `required`, limits. That stored list
is the authority. A form with an unsatisfiable field (min above max, a radio group with one
option, a duplicate name of a different type, an unsupported control) is a `422` naming the
field, so the agent finds out at publish, not Jeremy at submit. This is new: publish today only
returns advisory warnings after storing (`api.py` 495-515), so the check runs before the store.

Each form becomes a **decision of kind `form`** on the page (8.4), with its own id, listed with
the page's other decisions and in `/decisions`. Its fields live in a new table keyed by
(decision, version).

### 3.3 Answering (Jeremy)

The controls are live in the page, where the agent put them. The comments shim (already in every
page) watches `input`/`change` inside `data-artifact-form` and reports `{form, name, value}` to
the gallery by `postMessage` (gallery origin only, as slot rectangles are today). The gallery
keeps the draft, validated against **its own copy of the stored field list**, never trusting the
page's labels.

The gallery draws a **Submit bar** for each form (pinned like the decision cards): "3 of 5
answered · Submit". Submit opens a gallery-drawn summary, listing each field's **stored label and
the value being sent**, blanks shown as blank, and he confirms. This sheet, drawn outside the
page, is what stops a page script from changing a value behind his back: what he confirms is what
is stored. The gallery posts the batch to its own route (`POST /c/{id}/forms/{form}`), same-origin
guarded like every gallery form; the server validates every value against the stored field list
and refuses with a `422` naming the field.

Drafts live in the browser tab only (prior art Q6; my call): a reload or a republish replays a
draft by name without firing events, capped at 200 fields and 2,000 characters a value, as lavish
does.

### 3.4 What the agent gets

One `decision_answered` event per submit, and the decision read-back carries the record:

```json
{"decision_id": "...", "kind": "form", "form": "rollout", "version": 7,
 "content_hash": "…", "submit_id": "…", "answered_by": "jeremy", "answered_at": "…",
 "answers": {"region": "a", "wt.caps-unit-trim": "park", "window": "2026-11-02T06:00",
             "notes": ""},
 "blank": ["notes"], "stale": []}
```

`artifacts-decide wait` wakes once per submit. A second submit (a second sitting) is a new event;
the later value wins per field and the record keeps the history (quarterdeck). Text values pass
the local secret scanner like comments; a hit refuses the submit, naming the field.

Agent equivalents: `GET /api/v1/decisions/{id}` reads it; `close` closes the form;
`POST /decisions/{id}/record` relays an answer given elsewhere, as a record (per field allowed),
marked as a relay and never counted as his own. A form needs no separate declare route: it is
declared by publishing the page.

### 3.5 After a republish

The field list is stored per version. On a republish, for each field of the new version:

- same name, type, options and limits as the version he answered → **carried**: his answer stays
  current, and the page shows it filled in;
- changed or renamed → **stale**: his earlier answer is shown read-only beside the new field
  ("answered 'park' on version 6, when the question read …"), the field starts empty, and the
  record's `stale` list returns it to the agent with the version it was given on;
- removed → its answer stays in the history and is listed as stale.

Nothing is ever moved silently. The form re-opens if any required field is now unanswered.

### 3.6 Settled after the builder check (my calls; Jeremy can overrule)

- **Lifecycle.** A form's decision is minted at its first publish and keyed by (page, form name);
  it keeps its id across republishes. Two forms with one name on a page are a `422`. A form
  removed from the page is closed, its answers kept. A form stays **open** after a submit (a
  second sitting is a new submit, later wins per field, history kept); the agent closes it, or it
  closes when the page is archived. "Re-opens" in 3.5 means it shows as unanswered again in the
  gallery's lists. The `question` column holds the form's title (its `<legend>`/`aria-label`, else
  its name); `options` holds `[]`; the record goes in a new JSON column, `answer`/`answer_note`
  stay null.
- **Blanks and defaults.** A field is answered when it has a value he left or chose: a pre-checked
  radio or prefilled text **counts as answered** only once he submits with it in place, and the
  summary sheet shows it like any other value. An unchecked lone checkbox is `false`, not blank. A
  checkbox group under one name is a list of the checked values (`[]` is blank). A `select` with a
  first option of `value=""` is blank until chosen.
- **Unchanged means wording too.** The carry test compares name, type, options, limits **and the
  label text** (gg-ops: an answer to old wording is not a yes to the new one). Surrounding prose is
  not compared; that limit is stated in the onboarding text.
- **Where carried and stale answers show.** In the gallery's form panel and summary sheet, drawn by
  the gallery. The shim also writes carried values back into the page's controls (the same replay
  as drafts, no events fired). Stale answers are never written into the page. A tab draft newer
  than the stored answer wins in the controls; the sheet shows both when they differ.
- **Controls the stored list doesn't know.** The shim reports only names in the stored list; an
  unknown name, or a value outside a field's stored options, is refused at submit with the field
  named. A page script cannot add a question.
- **Per-field `record`.** The relay is stored per field with its source; a relayed field never
  counts as his own and never overwrites a value he submitted later than the relay.
- **Events.** A submit emits `decision_answered` with `kind: form` in its payload, so `wait` works
  unchanged; the partner bridge skips form events in v1.

## 4. What is reused

Slot reporting and the `postMessage` channel in `content.py` (a form-draft handler checks
`event.source` and `event.origin` like the existing ones in `gallery.py`); the decisions table,
events, nudge, `wait`, `acknowledge`, `record`, `close`. **Not free:** many decision paths are
gated to `kind='question'` (`decisions.py` 494, 541; `api.py` 1752, 2247) and the `kind` CHECK in
both schemas allows only `question` and `review`, so slice 1 adds `form` to the CHECK (a migration
step on both databases) and opens each gated path deliberately. The iframe has no `allow-forms`
and the content CSP keeps `form-action 'none'`, so native submit and Enter-to-submit stay
blocked; the `form_element` warning (`csp.py` 120-127) is reworded to point at
`data-artifact-form`. Also reused: the secret scanner; the page
parsing done for pins and headings. New: the field parser and validator, a `decision_field`
table (decision, version, name, spec), a record-shaped answer (a JSON column beside `answer`),
the gallery's Submit bar and summary sheet, the shim's form listener.

## 5. Onboarding, CLI, skills (docs are done)

`agents.txt` gets a "forms" section with the example above and the refusals; `artifacts-decide
show` prints a form's record; `--help`; runcove `skills/artifacts/SKILL.md` and the homelab
`lab-artifacts` skill each gain a short "forms" paragraph.

## 6. Slices

1. Server: parse and store fields at publish, `422` for bad forms, kind `form`, read-back. The
   form shows on the page as plain controls; no Submit yet.
2. Gallery: shim listener, draft, Submit bar, summary sheet, the submit route, validation, the
   event and `wait`. Live proof: a real form answered by Jeremy.
3. Republish rules (3.5), stale display, `record` per field, docs round.

## 7. Risks

- **A page script changing a value between his click and Submit.** Covered by the summary sheet,
  which shows the value the gallery holds; a page can still mislead with surrounding prose, as it
  can today.
- **Parsing HTML at publish costs time on large pages.** The pin work already parses once per
  publish; forms add a walk over `data-artifact-form` subtrees only.
- **Agents writing controls outside a `data-artifact-form`.** They stay inert (no submit path),
  and publish warns, as `form_element` does today.

## 8. Decisions for Jeremy

**Answered 2 Nov 2026 (10:45-10:47 UTC, on page `rxvurj7cjqfsh2g32hzbw7hl4t`): (a) on all four.**
He types in the page's own controls and the gallery's summary confirms what is sent; partial
sends are allowed, blanks marked and `data-complete="all"` available; "seen" means picked up or
marked seen, never a plain read; one decision of kind `form` per form. Sections 3-6 already
assume 8.1, 8.2 and 8.4; the one addition is `data-complete="all"` on a form, parsed and stored in
slice 1 and enforced at Submit in slice 2. 8.3 binds the receipts work (runcove-5t3oj.55).

8.1 **Where he types his answers.**
(a) In the page, in the controls the agent wrote; the gallery confirms what will be sent in its
own summary before it is stored (this design). (b) In gallery-drawn controls over a slot, from a
field list the agent sends as JSON: safest, but every office asked for plain HTML. (c) Both.

8.2 **Partial submits.**
(a) Allowed: blanks are marked, `required` fields block Submit, and a page can mark a whole form
`data-complete="all"` to require every field (warpgate's release case). (b) Every field must be
answered, always.

8.3 **When his comment or answer shows "seen"** (settled now so receipts don't rework forms).
(a) When the agent picks it up (`pickup`) or marks it seen explicitly; never on a plain list or
read. (b) On any read through the API. (c) Only when the agent marks it explicitly.

8.4 **What a form is.**
(a) One decision per form, holding every field's answer: one submit, one wake, one place to look.
`record` can still relay single fields. (b) One decision per field, submitted together: each
field can be waited on alone, but a twenty-row table is twenty decisions.
