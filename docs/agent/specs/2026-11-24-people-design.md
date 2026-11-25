# People: the owner becomes person #1 (24 Nov 2026)

Status: slices 1, 2 and 3a built; slice 3b (the agent-visible `answers[]`) and slice 4 designed, not built. Written up from the
architect's blueprint and the artifacts office's decisions of 24 Nov 2026 (reversible).

## What and why

Ream has one reader, the owner (`REAM_OWNER_ID`), and the gallery trusts the sign-in proxy in front of
it. To let others read and answer (step 2 of the company plan) the owner has to become one person
among several without a flag day. The seam is `people.current(request) -> Person`, a FastAPI
dependency. With gallery sign-in off (the default) it returns the owner from settings, exactly
today's behaviour. With `REAM_GALLERY_SIGN_IN=on` (slice 4) it returns the person behind a hashed
`__Host-ream_session` cookie issued by Ream's own OIDC (reusing `oidc.begin` / `oidc.finish`). No
identity header is ever read, in either mode.

Access stays single-owner through step 2: `access.can_read(person, page)` (owner reads all, others
none). `people.OPEN_SIGN_IN = False` (precedent `clientlevel.COMMENTS_REQUIRED`) refuses
`REAM_SIGN_IN_WHO=anyone`; step 4 flips it.

## Data model (new SQLite steps from 36, the same in `schema.sql`; stored names kept)

- Slice 1 (built): `person(id pk, issuer, subject, email, name, created_at, last_seen_at,
  disabled_at, unique(issuer, subject))`. `id` follows the OWNER_ID pattern. A person is keyed on
  (issuer, subject), never on email. `owner.check` seeds the owner (insert on conflict do nothing) and
  copies issuer and subject from the `service_meta` pin `oidc_owner_sub` ("<issuer> <sub>") when
  there is one. `owner._COLUMNS` gains `person.id`, so `change-owner` moves the row.
- Slice 2 (built): `page_read` (pk person, artifact_id) and `human_pref` (pk person, key) rebuilt;
  `page_pin(person, artifact_id, pinned_at)` filled once from `artifact.pinned_at` (kept only as a record, neither read
  nor written; no image rollback: an older image on a migrated database
  fails on every page open, and pins made meanwhile are lost. On SQLite restore the newest nightly snapshot
  (`docs/agent/runbooks/sqlite.md`, § "A real restore"; writes since it are lost, so snapshot just before
  upgrading); on Postgres restore the deployment's own pg_dump backup (no runbook here); otherwise roll
  forward with a fix); `frame_pass.person`, set when the gallery mints a pass and read by nothing yet.
  Migrated rows take the `service_meta` owner id; a database that holds such rows and records no
  owner is refused rather than guessed. SQLite does it in step 37 (a function: rebuilds and copies);
  Postgres at the end of `schema.sql`, each part guarded by its own "already done" test (the column or
  the table), because `schema.sql` runs at every start and a pin the owner removed must not return.
  Postgres adds `person` as the last column and swaps the primary key; SQLite rebuilds the tables
  with `person` first; the copier matches columns by name.
- Slice 3a (built): `decision.addressed_to` (null = page owners); `decisions.may_answer(person, d)`;
  `form_answer(decision_id, person, answer_record, answered_at, pk(decision_id, person))` filled once
  from `decision.answer_record` (kept, still what the agent reads); `receipt.person`. SQLite step 38;
  Postgres at the end of `schema.sql`, its backfill guarded by the new `receipt.person` column.
  Migrated rows take the `service_meta` owner id, and rows with no recorded owner are refused.
- Slice 3b (not built): `answers[]` on form reads. It is agent-visible, so offices review its shape first.
  An unapplied relay already gives a person's row their own `answered_at`, never another person's
  (see "Addressed questions as built"); 3b still needs its own rule for what the agent reads.
- Stays single-owner until steps 3 and 4: `can_read`, MCP `owner_login` / `NotOwner`, page
  ownership, archive, rename, share, machines and keys (behind `people.require_owner`), plug-in answer
  sources, `pageviews` kind owner, email shares.

## Slice 1 as built

`src/ream/people.py`: `Person(id, name, is_owner)`, `current(request)`, `require_owner` (404 for anyone
else, as for a page that is not there), `OPEN_SIGN_IN`. The gallery handlers that mean "the request's
person" (answer a decision, comment, resolve, reopen, delete, restore, stage a comment picture) take
`person = people.CURRENT` and use `person.id`; the Machines handlers take `people.REQUIRE_OWNER`.
Values are identical to `settings.owner_id()`; no served text changes. `tests/test_owner_id_sites.py`
classifies every read of the owner id (calls, `.owner_id` attribute reads, imports) as request person,
page owner (step 4) or deployment seed, so a new one has to say which. The same file walks the real
gallery router's dependency trees and fails unless every write route (and the page view, which marks
a page read) depends on `people.current`, and the Machines and Connect reads on `require_owner`.
The forms submit route takes `people.CURRENT` and passes `person.id` to `formanswer.submit`.

## Guards (for the later slices; each is a test)

Identity headers ignored (Remote-User, X-Forwarded-User, X-Auth-Request-Email, in both modes);
the session cookie never reaches the content domain (browser test); no route takes a person id; "not
yours" answers identically to "no such page"; gallery `Cache-Control: private, no-store`; a fresh
token per sign-in and an Origin check on POST (verify today's CSRF handling first).

## Slices

1. Person #1 and the request's person, no behaviour change (this one).
2. Per-person state (read marks, preferences, pins, frame passes). Tests: `test_per_person_state.py`
   (two people, `OPEN_SIGN_IN` flipped), migration from a step-35 database on both backends,
   `migrate.copy` round trip.
3. Per-person answers and receipts.
4. Gallery sign-in, off by default: settings, `person_session` (12 h), `/signin`, `/signin/callback`,
   `/signout`, `oidc.person_login`, `Settings.problems`; runbook `docs/agent/runbooks/gallery-sign-in.md`.

Each lands alone and the homelab stays byte-identical until a setting is set.

## Office decisions (24 Nov 2026, reversible)

- A. No second person before sharing: step 2 lands plumbing; others sign in only when step 4 flips
  `OPEN_SIGN_IN`.
- C. Per-person state has no agent-key twin (a key has no person); its twin is the person's own
  signed-in app (step 3). This is the one place the "every human action has an agent equivalent"
  rule is met by the app, not by a key route.

## Decided later on 24 Nov 2026

- D. The id of a new person is a readable id taken from their login, fixed once given and never
  reused. People and agents share one name space: a person's id that an agent already holds is
  refused, and an agent name that a person already holds is refused (the owner id's rule today,
  extended to every person). A person is matched by (issuer, subject), never by email. Slice 4
  builds this.

- B. The homelab's Ream keeps its login gate and never turns on gallery sign-in. GitGuardian runs its
  own separate instance; sign-in is proven in tests until that instance exists. No open questions
  remain.
- Slice 4 must fill person #1's issuer and subject from `REAM_OIDC_OWNER_SUBJECT` when it is set
  (no `oidc_owner_sub` pin is written then, so `owner.check` finds nothing to copy).

## Slice 2 as built

The wall (`_wall`, `_folder_pages`) joins the request person's `page_read` (`pr`) and `page_pin` (`pp`)
rows (`_PERSON_JOINS`, two parameters); the unread test, the pin order and the "Older pages" cursor read
those. `comments.unseen_reply(read_at)` replaces the constant `UNSEEN_REPLY`: the wall passes `pr.read_at`,
the inbox joins `page_read` for its person (`inbox.gather/count/items(person=)`). The detail page marks the
page read for the request's person; the pin route stays owner-only (`require_owner`: access is single-owner)
but writes `page_pin` for the person it was given; `/a/{id}/frame` and the page view mint passes with the
person. `holds.get_mode/set_mode/set_review_mode` take a person (default the owner: an agent opening a review
asks for the owner's preference). Agent routes and MCP read "has he read it" (`seen_version`, `seen_at`) and
`pinned_at` from the owner's rows (`folders.his()`), and an agent's pin writes the owner's `page_pin`.
Decision C holds: no route takes a person id and none reads or writes another person's state.
Tests: `tests/test_per_person_state.py` (two people by overriding `people.current`, `OPEN_SIGN_IN`
flipped), `tests/test_people_slice2_migration.py` (both backends), `tests/test_migrate.py`'s copy round
trip, and `tests/test_owner.py` (`change-owner` moves the new columns).

## Slice 3a as built

Storage and internal logic only: nothing an agent reads over `/api/v1` or MCP changes
(`decisions.public` drops `addressed_to`; `answer_record` and every receipt are as before).
- `decisions.may_answer(person, d)` (and `addressed_ok(person, addressed_to)` for a caller that holds
  only the column): the person a question is addressed to, else the page's owners (today the owner).
  The gallery's answer routes pass the request's person to `decisions.answer(person=)` and
  `formanswer.submit(person=)`; anyone else gets `store.NotFound`, the 404 of a decision that is not
  there. No agent route sets `addressed_to` yet (nothing sets it today).
- A form is answered per person: `formanswer.submit` merges a person's submit into their own
  `form_answer` row (not another person's) and also writes `decision.answer_record`, so the agent's
  record is the latest submitter's. With one person that is the record as before. How several
  people's answers reach the agent is slice 3b's `answers[]`. A relay (`formanswer.relay`) is
  the addressed person's (the owner's when `addressed_to` is null) and merges into their row. A republish brings every person's row onto the new
  version (`forms._reconcile_record`). The gallery's form bar (`forms_ui.payload`) draws the
  request person's own record.
- `receipt.person` is the decision's `answered_by` when the receipt was written, never sent to an
  agent. The gallery says nothing of a receipt or a pickup on an answer someone else gave
  (`_render_decisions(person=)`).
- `comments._HIS_THREAD` is per person: a reply is unseen for the person who wrote in its thread
  (`me.person`, joined by `comments.ME_JOIN` beside `pr` and `pp`), no longer for any human. A reply
  under a note left on a decision (`anchor.kind = 'decision'`) is still every reader's.
- `change-owner` moves `decision.addressed_to`, `form_answer.person` and `receipt.person`.
- Tests: `tests/test_people_answers.py` (two people), `tests/test_people_slice3_migration.py` (both
  backends), `tests/test_migrate.py`'s copy round trip, `tests/test_owner.py`,
  `tests/test_per_person_state.py` (the thread is per person).
- Rolling back is safe for answers, in two cases an older image can produce on a migrated database
  (its code runs; the new columns and table are unread by it):
  1. It writes a submit or relay: only `decision.answer_record` changes, so a person's `form_answer`
     row goes stale or is missing. The backfill does not run again, but every reader and writer of a
     person's record picks the newer one by one rule (`formanswer.newest_record`): when the
     decision's `answer_record` says `answered_by` is that person and the row is missing or names
     another `submit_id`, the decision's record is used. The form bar (`forms_ui.payload`) draws it,
     and the next submit or relay (including a relay that changes nothing) merges into it and writes
     it back as the row.
  2. It republishes a page with a changed field: it reconciles only `decision.answer_record` (the
     changed answer blanked and moved to `stale`), and the row stays on the old version with the
     same `submit_id`, so `newest_record` returns the row. Every reader and writer then brings the
     record onto the form's latest version, read-only, through `formanswer.on_latest` (the writes
     before merging, `forms_ui.payload` before `formrecord.gallery_view`), so the bar does not carry
     the old answer into the new question and shows it stale, as before slice 3. The row itself is
     rewritten by the next write.
  With one person this is the old behaviour; with several it stays right because another person's
  record never names them. Not covered: an older image's record for a person who is not the
  decision's `answered_by` (it cannot make one). Tests: `tests/test_people_answers.py` (an older
  image's write, stale and missing row; submit, relay, an unapplied relay, and the form bar; an older
  image's republish; an unapplied relay's `answered_at`).
  Once a route writes `addressed_to`, an older image ignores `may_answer` on reads and writes: it
  draws the buttons and takes an answer from the owner on a question addressed to someone else, and
  files a relay as the owner's. Do not write `addressed_to` from a route until no older image runs.

## Addressed questions as built (before anything sets `addressed_to`)

Nothing writes `addressed_to` from a route yet; these two gaps are closed so that when one does, a
question for a person other than the owner behaves right everywhere. Agent-facing output is
unchanged (`decisions.public` still drops `addressed_to`; the agent feeds, `across_seats` and
`open_counts` with no person, read every row as before).
- **A relay files under the person the question is addressed to**, else the owner. `formanswer.relay`
  merges into that person's `form_answer` row and writes `answered_by` (record, history entry,
  decision) as them; `decisions.record_elsewhere` writes `answered_by` (row and event body) as them,
  so the receipt's person follows (`receipt.person` is the decision's `answered_by`). An unapplied
  relay changes nothing: it leaves the decision's `answer_record` alone unless that is already this
  person's, and gives their row their own `answered_at` (the decision's when the decision is theirs,
  else the row's), never another person's.
- **Reads follow `decisions.may_answer`.** `decisions.addressed_ok_id` / `addressed_sql` are the
  id and SQL forms of it (the owner is `settings.owner_id()`). `across_seats(person=)` and
  `open_counts(person)` take it, and the inbox (`gather`, `count`) passes the request's person. The
  wall's per-page "waiting" count, its waiting sort and cursor (`_wall_waiting(person)`) and the
  folder tiles' count use `me.person`. `_render_decisions` draws no card for an open question the
  reader may not answer as a read-only card (the question, whom it is for, no form, hold or live
  switch; the slot and "held on another page" text stay true) and no change-your-answer card or hint
  on a settled one; the settled heading counts only what the reader may answer, adding "N settled for
  someone else". `forms_ui.payload` draws no submit bar for a form addressed to someone else.
- **A signed-in app relays only for the owner** (only the owner signs in): `record_elsewhere` and
  `formanswer.relay` with `by_app` refuse a question addressed to anyone else as `store.NotFound`,
  the answer for a decision out of reach. A key's relay keeps the rule above.
- **Partner (Slack) items are the owner's**: `/pq/.../answer` is owner-only, so the inbox lists and
  counts them, and shows their status block, only to the owner. Tests: `tests/test_people_addressed.py`.
- Known item for when a second person can sign in: the owner-only archive sheet's "N questions
  waiting on you" (`gallery.py`, `folder_archive_sheet`) counts every question the folder holds.
- Not changed, on purpose: the owner-only archive-folder sheet and the agent API's folder preview count
  every question (`store.set_folder_archived`), and the deleted tab's count of questions set aside
  (`store.list_deleted`); neither is a count of what a person may answer.

## Not yet checked

Checked in slice 2: every `page_read` / `human_pref` / `pinned_at` call site (the grep is the list in
"Slice 2 as built"), the Postgres schema of the rebuilt tables, and `migrate.copy`'s column handling
(by name). Still open: today's CSRF handling (slice 4) and receipt-seen per reader (the decision's `acknowledged_at` is one value; slice 3b).
