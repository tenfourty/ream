# People: the owner becomes person #1 (24 Nov 2026)

Status: slices 1 and 2 built; slices 3 and 4 designed, not built. Written up from the
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
  nor written; this change cannot be rolled back by image alone: an older image on a migrated database
  fails on every page open, and pins made meanwhile are lost, so the way back is restoring the pre-upgrade
  backup, `docs/agent/runbooks/sqlite.md`); `frame_pass.person`, set when the gallery mints a pass and read by nothing yet.
  Migrated rows take the `service_meta` owner id; a database that holds such rows and records no
  owner is refused rather than guessed. SQLite does it in step 37 (a function: rebuilds and copies);
  Postgres at the end of `schema.sql`, each part guarded by its own "already done" test (the column or
  the table), because `schema.sql` runs at every start and a pin the owner removed must not return.
  Postgres adds `person` as the last column and swaps the primary key; SQLite rebuilds the tables
  with `person` first; the copier matches columns by name.
- Slice 3: `decision.addressed_to` (null = page owners); `decisions.may_answer(person, d)`;
  `form_answer(decision_id, person, answer_record, answered_at, pk(decision_id, person))` filled from
  `decision.answer_record` (kept); `receipt.person`; `answers[]` on form reads (agent-visible, so
  offices review first).
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

## Not yet checked

Checked in slice 2: every `page_read` / `human_pref` / `pinned_at` call site (the grep is the list in
"Slice 2 as built"), the Postgres schema of the rebuilt tables, and `migrate.copy`'s column handling
(by name). Still open: today's CSRF handling (slice 4) and receipt-seen per reader (slice 3).
