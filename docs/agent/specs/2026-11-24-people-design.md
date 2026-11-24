# People: the owner becomes person #1 (24 Nov 2026)

Status: slice 1 built (this commit); slices 2 to 4 designed, not built. Written up from the
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
- Slice 2: `page_read` (pk person, artifact_id) and `human_pref` (pk person, key) rebuilt;
  `page_pin(person, artifact_id, pinned_at)` filled from `artifact.pinned_at` (kept, unread);
  `frame_pass.person`. Migrated rows take the `service_meta` owner id.
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
classifies every remaining `settings.owner_id()` call as request person, page owner (step 4) or
deployment seed, so a new call has to say which.

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

## Open questions (asked of the owner)

- D. The id for a new person: a readable slug (as the owner's id is) or an opaque one. Slice 1 does
  not need it; slice 4 does.
- B. Whether the homelab turns on in-app sign-in after slice 4.

## Not yet checked

All `page_read` / `human_pref` / `pinned_at` call sites; today's CSRF handling; receipt-seen per
reader; the Postgres schema of the rebuilt tables; `migrate.copy` column order. Slice 2 starts with
that grep.
