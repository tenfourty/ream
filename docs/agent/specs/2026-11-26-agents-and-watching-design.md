# Agents: personal or shared, ownership by agent, the via line, watching pages (26 Nov 2026)

Status: slice 1 built (see "Slice 1 as built"); slices 2-4 designed, not built. Step 3 of the company plan. Written up from the architect's blueprint and the
artifacts office's decisions of 24 and 26 Nov 2026 (reversible), after checking the blueprint against the code
(the corrections are listed under "Where this differs from the blueprint").

## Goal and source

The source is design v10, approved by Jeremy on 24 Nov 2026, step 3: an agent belongs either to a person
(a personal agent) or to the deployment (a shared agent; step 4 makes that a team). A personal agent acts for
one person, shows as "Alice, via Claude.ai", and reads and watches what its person can. A shared agent shows
its own name. Ownership is by agent, never by session. An agent can follow a page it can read.

The outcome: with a second person present, Ream says who an agent is, sends a question to the person it
belongs to, lets that person's app read what that person can read, and tells an agent when a page it follows
changes. With one person (the homelab today) nothing it serves changes, except new routes that nothing
calls yet.

## The model

Three questions, answered in one new module, `src/ream/agents.py`:

- `person_of(conn, agent) -> str | None`: the person the agent belongs to, or None for a shared agent.
  Derived from `mcp_agent.person` (`schema.sql:1224-1231`); slice 4 adds the `agent_person` table for keys.
- `page_owners(conn, agent) -> tuple[str, ...]`: who owns that agent's pages. A personal agent: its person.
  A shared agent: `shared_owner()`, which reads `settings.owner_id()` today and becomes the team's
  maintainers in step 4. That one function is the only place step 4 changes for this question.
- `shown(conn, agent) -> Shown`: the via line. `Shown(agent, person, via, team)` with `.text()`.

Deriving rather than copying matters for rollback: an older image's `mcp_apps.agent_for` after a rollback
would meet an app agent with no copied row and read it as shared. With the derivation, slice 1 needs no
migration at all.

Key agents are shared unless they have an `agent_person` row (slice 4). An agent can never change whether
it is personal or shared: no `/api/v1` or MCP route takes or sets an agent's person (the same asymmetry as
answering a decision, AGENTS.md "Every human action has an agent equivalent"). `/agents/me` will report it,
read-only, in slice 4.

### The via line

1. A personal agent with an app: `<person name>, via <app host>`. The person name is `owner_name` for the
   owner (the rule of `people._person`, `people.py:36-41`), otherwise `person.name`, failing that
   `person.id`. The host is the checked `mcp_agent.host`.
2. A personal key agent (slice 4): `<person name>, via <agent name>`.
3. A shared agent: `<agent name>`; `team` is None in step 3 and step 4 renders `gg-ops (Platform)`.
4. A shared agent can never fake a via line: `", via "` is written only by rules 1 and 2, and an agent name
   cannot hold a comma or a space (`keys.AGENT_NAME`, `keys.py:22`: lower-case letters, digits, hyphens).
5. The person name passes `text.sanitize`, is capped at 64 characters and is escaped where drawn. It is for
   display only: every stored or returned `agent` field stays the agent's name.
6. A person's own gallery acts keep showing their own name, as today.
7. Wording that says "your": `_asker_line` (`gallery.py:4607-4620`) writes "your Claude (host)". It is true
   only for the reader the app belongs to; for anyone else it reads "<person name>'s Claude (host)".

Today every person's app would be shown with the owner's name: `mcp_apps.via(host)` returns
`f"{settings.owner_name()}, via {host}"` for every host (`mcp_apps.py:194-196`) and `display_name` calls it
(`:199-203`). Its callers: `gallery.py:1956` and `:1988` (the Connected apps list, owner-only),
`gallery.py:4604` (`_passed_on`), `mcp_tools.py:1769` (the comment's `author_name`) and `mcp.py:512`
(`whoami`'s `shown_as`). The settled-question cards get their hosts from a dict built at `gallery.py:7199`
(`app_hosts`, agent name to host); it becomes a dict of `Shown`.

### Reach

`Reach` (`reach.py:31-47`) gains `person: str | None`. Today `mcp_apps.reach_of` gives every app
`READS_AS_OWNER` (`mcp_apps.py:356-372`), which is right only because the owner is the only person who can
sign in (`people.OPEN_SIGN_IN = False`, `people.py:24`). After slice 2:

- An app whose person is the owner reads as the owner, as now.
- An app whose person is anyone else reads through `access.person_reads(conn, person, page)`: pages of that
  person's personal agents, plus pages open to agents (`artifact.agents_can_read`, `schema.sql:575-589`),
  never the trash and never the owner's closed pages.
- An app may change the pages of any of its person's personal agents (as now, for the owner's).
- A key keeps `of_key` exactly, unchanged, until slice 4 gives a personal key wider reads (never wider
  writes: decision D).

The two page checks that use the reach are `store._owned` (`store.py:763-800`: `widened(agent)` then
`reads_all()` for a reader, `writes_others()` for a writer, otherwise the key's `agent = %s or
agents_can_read`) and `store.list_readable` (`store.py:2037-2069`, whose `or %s` is `wide.reads_all()`).
`widened()` matches on the agent (`reach.py:68-75`), so background work for another agent never inherits it.
`access.py` does not exist yet: `access.can_read` in the people spec is a description of intent, not a
function (`grep -rn can_read src` finds only `agents_can_read` uses). Slice 2 creates the module.

### Ownership by agent

`decisions.addressed_ok_id` and `addressed_sql` (`decisions.py:243-258`) decide "who may answer a question
with no `addressed_to`" with `settings.owner_id()`. They take a person and a column, never a page. With
`page_owners`, the rule becomes: `addressed_to` null means the owners of the page's agent. The id form takes
the page's agent too; the SQL form joins `artifact.agent` and asks `agents.owns_sql(person_expr, agent_col)`.
A personal agent's questions go to its person by default and `addressed_to` is never written (the people spec
warns that a route must not write it until no older image runs, `2026-11-24-people-design.md:169`). A shared
agent's questions go to the owner.

Gallery shelf controls (archive, rename, pin, share) stay behind `people.require_owner`; step 4 turns them into
role checks and grants, so making them per-owner now would be thrown away.

### Watching

A watch is a row `(agent, artifact_id)`. An agent may watch any page it can read at that moment: a key, its own
pages and pages open to agents; an app, whatever its person can read (`access.person_reads`). Anything else
answers 404, word for word as for a page that does not exist. Watching your own page is accepted and adds
nothing, because the page's own agent already hears about it. "Private until shared" arrives in step 4 by
changing only the readability predicate.

What a watcher gets: events in its own `/events` feed, written by `watches.fan_out` inside the transaction of
the change, through `events.emit` so the outbox and after-commit sinks see them as they see any event. A
watcher is skipped when it is the actor, when it is the page's own agent, when it can no longer read the page,
and, for a comment, when it already receives that comment as `comment_added` (the owner, and the agents in a
thread: `comments.py:960-990`). Two new kinds, added to `events.KINDS` (`events.py:35-80`):

- `watched_page_changed`: a version was minted. `ref_id` is the version number; `summary` is the title plus
  the version's note or section.
- `watched_comment_added`: anyone else's comment, summarised as `comment_added` is.

Watchers get no answers, decisions, receipts or key events: those stay with the asker. Kind names are stored
and never renamed. `event.kind` has no CHECK (the clause was taken out; validation is `events.append`,
`events.py:178`), so new kinds need no migration. A sink gets a kind only if it names it
(`sinks.matching`, `sinks.py:372-373`), so no existing plug-in or doorbell is rung by these.

Opt-in on the feed: `GET /api/v1/events` gains `include=watched`, extending the one existing idiom
(`include=receipts`, `api.py:3915`) to a comma list. `events.list_for_agent` gains `include_watched=False`
beside `include_receipts` and leaves watched rows out by default (`events.py:346-350` is the model).
`wait_for_agent` gains the same parameter; without it a watched event never wakes a waiter (so every existing
waiter behaves as before), and a caller that passes it is woken by one. MCP `whats_new` (`mcp_tools.py:943`)
passes it, so its rows include them; the kind is the label, and the summary is fenced with the actor as any
event's is (`_event_writer`, `mcp_tools.py:903`). `wait_for_answer` does not include them.

Limits: at most one `watched_page_changed` per watcher and page every 10 minutes (decision C), and at most 200
watches per agent (a PUT past it answers 409; a repeat PUT of an existing watch is 200). Losing access: the
watch stays but goes silent and drops out of the agent's list, as if it were never there; deleting the page
cascades it away. Step 5 hook: data behind a page will emit `watched_data_changed` through the same fan-out.

Where the hooks go. There is no version-minted event today: a grep for `events.emit` finds callers only in
`comments.py`, `decisions.py`, `formanswer.py` and `store.py` (questions, archive and restore), none for a new
version. A version is minted in exactly one place after the first, `store.patch` (the `insert into version`
at `store.py:1039`; `patch_section` and `patch_edits` call `patch`, `store.py:1455`, `:1565`; `publish` writes
version 1, which has no watchers). `patch` has no after-commit hook, so slice 3 adds one: fan out inside the
`conn.transaction()`, hold the `Emission`s, and call `.fire()` after the `with` block exits, as
`comments.add` does for its own (`comments.py:1000-1004`). The comment hook sits beside the owner and replier
emissions in `comments.add` (`comments.py:938-990`).

## Data model

Slice 3, Postgres at the end of `schema.sql` (`create table if not exists`, each part guarded) and SQLite
step 41 (the highest step today is 40, `db.py:1223`; `SQLITE_SCHEMA_VERSION = max(_SQLITE_STEPS)`,
`db.py:1246`):

- `page_watch(agent text not null, artifact_id text not null references artifact(id) on delete cascade,
  created_at timestamptz not null default now(), session text, changed_notified_at timestamptz,
  primary key (agent, artifact_id))`, and an index on `artifact_id`.
- `changed_notified_at` holds the coalescing window, so it needs no query over `event`. Set in the fan-out's
  transaction.

Slice 4, SQLite step 42: `agent_person(agent text primary key, person text not null, set_at, set_by)`, no
foreign key to `person` (as `page_read.person`), so `change-owner` can rewrite it; `owner._COLUMNS` gains
`agent_person.person`.

Precedent for a per-agent table merged by rename: `seat_grant`, `agent_pickup`, `_merge_*` in
`rename.py:204-218`. `event.agent` is already in `rename._PLAIN` (`rename.py:54`), so watcher events follow a
rename; `page_watch` needs `rename._merge_watches` (primary key `(agent, artifact_id)`, so a merge keeps one
row per page). `migrate.py` has no table list: it reads both schemas from the databases and refuses a
difference (`migrate.py:128-158`, `:235-250`), but `tests/test_migrate.py::_seed` insists on a row in every
table (`test_migrate.py:44-46`), so slice 3 adds a `page_watch` row there and slice 4 an `agent_person` row.

## Slices

Each lands on its own, in this order. CI only: no suite on the management machine (AGENTS.md).

1. **Agents belong to someone (code only).** `agents.py` (new); `mcp_apps.via` and `display_name` call into
   it, with callers changed as listed above; `decisions.addressed_ok_id` and `addressed_sql` become
   page-aware (`owns_sql` for the SQL form) and their callers pass the page's agent
   (`forms_ui.py:75`, `formanswer.py:446`, `:621`, `decisions.py:1903`, `:2131`, `:2762`, `:3008`,
   `gallery.py:3639`, `:4987`, `:5575`, `:5656`). `tests/test_owner_id_sites.py` SITES: the two rows for
   `addressed_ok_id` and `addressed_sql` change to read `agents.shared_owner`, classified SEED; the other
   PAGE_OWNER rows (`record_elsewhere`, `formanswer.relay`, `partner_in`, `readside`, ...) are each reviewed
   and either move to `page_owners` or stay with a stated reason; none is left unclassified.
   Tests: `tests/test_agent_belongs.py` (two people with `OPEN_SIGN_IN` flipped: a personal agent's question
   can be answered only by its person, a shared agent's only by the owner, anyone else gets the 404 a
   missing decision gets) and `tests/test_via_line.py` (a second person's app shows their name; a shared
   agent shows its name; a hostile person name is sanitised and escaped; the comma rule holds; the "your
   Claude" wording; the owner's served text is byte-identical, golden). Guard: no API or MCP response
   changes its `agent` field. Homelab: byte-identical.
2. **Signed-in apps read as their person.** `reach.py` (`person`), `access.py` (new, `person_reads`),
   `mcp_apps.reach_of`, the readability checks in `store._owned` and `store.list_readable`, and `_scoped`
   (it widens the same way, `reach.py:1-12`). Test: `tests/test_person_reach.py`, building a second person's
   `App` directly: it reads only its person's personal agents' pages and pages open to agents, never the
   owner's closed page, with identical 404s; the owner's app is unchanged; a background nudge for another
   agent inside the app's request sees key reach. Homelab: byte-identical.
3. **Watching** (needs slice 2's predicate). Schema as above; `watches.py` (new: `watch`, `unwatch`, `listing`,
   `fan_out(conn, page, kind, ...)`); `events.KINDS`, `include_watched` in `list_for_agent` and
   `wait_for_agent`, `include=watched` on the `/events` route; fan-out calls in `store.patch` and
   `comments.add`; routes `PUT` and `DELETE /api/v1/pages/{id}/watch` and `GET /api/v1/watches` (both words,
   `_BothWords`); MCP tools `watch_page`, `unwatch_page`, `watches` (`ream:read`) and `whats_new`'s
   inclusion; CLI `ream watch`, `unwatch`, `watches`; `rename._merge_watches`. Docs: this spec's status line,
   `docs/mcp.md`, the skill text served from `onboarding.py`, a news entry in `news.py` (added, never
   edited). Bump `CLIENT_LEVEL` only, not `MIN_CLIENT_LEVEL` (`clientlevel.py`: new verbs, nothing an old tool
   gets wrong). Tests: `tests/test_watch.py` (watch, unwatch, list; an unknown page and a closed page give
   identical 404s; only readers receive the fan-out; a page closed later goes silent; the watcher's own comment
   is not echoed; no double for a comment the watcher already gets as `comment_added`; the window holds; the
   cap; rename merges; destroy cascades), `tests/test_watch_events_opt_in.py` (`/events`, `wait` and
   callers without the flag byte-identical without the flag; a waiter does not wake), `tests/test_watch_migration.py`
   (both backends), and extensions to `tests/test_migrate.py` and `tests/test_rename_agent.py`.
   Homelab: existing responses byte-identical; the routes are new.
4. **Personal machines (keys).** `agent_person`; `artifacts-admin set-agent-person` and `clear-agent-person`;
   a control on the owner's Machines page (behind `require_owner`); `belongs_to` and `shown_as` on
   `/agents/me`, read-only; the `/api/v1` auth dependency sets `reach.acting` for a personal key, so its
   reads widen while its writes stay on its own pages; `rename` refuses to merge agents that belong to
   different people; `owner._COLUMNS` gains `agent_person.person`. Tests: `tests/test_personal_key.py`; a
   route walk proving no API or MCP route takes or sets an agent's person (modelled on
   `test_no_gallery_route_takes_a_person_id_from_the_request`, `tests/test_owner_id_sites.py:159`);
   extensions to `test_rename_agent.py` and the `change-owner` test in `tests/test_owner.py`. Homelab: does
   nothing until the owner marks a machine personal.

## Slice 1 as built (26 Nov 2026)

`src/ream/agents.py`: `person_of`, `page_owners`, `shared_owner` (SEED), `owner_sql`/`owns_sql`, `shown` and
`Shown` (with `.text()`), plus `app_shown`, `owner_app`, `unknown_app`, `person_label` for callers that hold the
pieces and no connection. No migration: everything is derived from `mcp_agent`.

- `mcp_apps.via` and `display_name` are gone; their callers use `agents.shown(conn, agent).text()`
  (`mcp.py` `whoami`, `mcp_tools.py` comment `author_name`), `agents.app_shown` (the Connected apps list in
  `gallery._apps_section`) or the `Shown` dict that replaces `app_hosts` (`_passed_on`, `_asker_line`). The OAuth
  consent screen (`oauth._consent_page`) also wrote the line and now calls `agents.owner_app`: a sixth site the
  blueprint did not list.
- `decisions.addressed_ok_id(conn, person_id, addressed_to, agent)`, `addressed_ok` and `may_answer` take the
  page's agent; `addressed_ok_owners(person_id, addressed_to, owners)` is the rule for a caller that already holds
  the owners (`forms_ui.payload`, the gallery's `_render_decisions` via its new `page_owners` argument).
  `addressed_sql(column, person_expr="%s", agent_col="artifact.agent")` lost its `person_id` argument: it is
  `coalesce(column, agents.owner_sql(agent_col)) = person_expr`, so the person still appears once and every
  caller's positional parameters are unchanged.
- The relay's default "said by" (`record_elsewhere`, `formanswer.relay`) is now the page's first owner, not the
  deployment owner. The signed-in-app checks (`by_app`) still use the owner id: an app is the owner's until
  slice 2.
- `_asker_line` takes the reader's person id (rule 7).

Where it departs from the text above:

- `owner_sql` writes the shared owner as a quoted literal (checked against `[a-z0-9-]+`), not a bound parameter:
  a parameter would sit in the middle of four long wall queries whose positional parameters each caller
  builds by hand. `shared_owner()` is still the one place the owner is read.
- `addressed_sql` is built on `owner_sql` and not on `owns_sql`, which is the same text with the person put
  first; this keeps the person to one occurrence.
- `Shown` is `(agent, person, person_name, via, team)`: `person` is the person's id (rule 7 compares it with
  the reader), `person_name` the shown name.
- Fourteen PAGE_OWNER rows were reviewed in `tests/test_owner_id_sites.py`: two moved to `page_owners`, the rest
  stay for the reason stated there. `_provenance`'s "<owner name> said ..." is not changed: it is not in the
  slice's list and the stored `answered_by` is what names who said it.
- Tests: `tests/test_agent_belongs.py`, `tests/test_via_line.py`. Not run on Postgres here (CI does).

## Guards

- No `agent` field in any API or MCP response changes meaning (slice 1 test).
- "Not yours" answers identically to "no such page", for reading and for watching.
- No route takes or sets an agent's person.
- Watched kinds appear on no feed that did not ask, and wake no waiter that did not ask.
- Widened reach never reaches background work for another agent (`widened()` matches on the agent).
- The via line is built only in `agents.shown`; `tests/test_via_line.py` fails for any other site that
  writes `", via "`.
- `tests/test_owner_id_sites.py` stays complete: every owner-id read is classified.

## Office decisions (24 and 26 Nov 2026, reversible)

- A. **Watcher events are separate kinds, opt-in on `/events`.** Lost: reusing `comment_added`, which would
  wake existing waiters and confuse consumers that assume an event is about their own page.
- B. **Fan out when the change is written.** Lost: joining at read time. Write-time leaves `list_for_agent`,
  `latest_seq`, `seat_read` and rename unchanged and checks access at the moment of the change; read-time puts
  a join and an access check into every poll, at 2 Hz per waiter (`events._LOOK_EVERY`).
- C. **Coalesce `watched_page_changed` to one per 10 minutes per watcher and page.** Lost: one event per
  version. Timer-driven boards would flood watchers until step 5's data rows exist.
- D. **Personal keys widen reads, not writes.** Lost: writing across all of the person's agents as apps do.
  A key is a machine; an app is the person in the room.
- E. **No setting gates watching.** Lost: an off-by-default switch. It only adds routes, and the design puts
  the homelab first.
- F. **Page owners do not see who watches in step 3.** Lost: a watcher list on the page. Left to step 4's
  readers' stats.

## The one operator question, after slice 4 lands

A deployment act, undoable: (a) keep every homelab agent shared (recommended: nothing changes, pages closed to
agents stay closed); (b) mark Jeremy's own machines personal, so they read his closed pages.

## Risks

- Events outlive access: a watcher keeps an event written while it could read the page, as an email would.
  Documented, not prevented.
- Coalescing drops the later change: a version minted inside the window sends no event of its own, so a
  watcher is told "changed" at version N and learns of N+2 only by reading or by the next window. The summary
  says later changes are folded in.
- A cursor skips watched rows for a caller that did not ask: `x-ream-seen` advances over them
  (`events.safe_cursor`), so an agent that wants them must always pass the flag.
- Fan-out cost in event and outbox rows: bounded by the cap and the window.
- Slice 1 is wider than it looks: fourteen PAGE_OWNER rows exist (`tests/test_owner_id_sites.py:28-54`)
  and eleven callers of the answering predicates change signature. Land it alone, with the sites test.
- Display names come from OIDC at the company: sanitised, display-only, and the comma rule stops forgery.
- `page_owners` hides that a shared agent's owner is the deployment owner; step 4 changes only that function.
- Other offices must review before building (design v10): the via line rules and that `agent` stays the stored
  name; watching's routes, tools, verbs, two permanent kind names, `include=watched`, the 10 minutes and the
  cap; default question routing; personal keys reading closed pages and writing only their own. Send to every
  publishing office, quarterdeck (which reads events) and the homelab maintainer (keys).

## Rollback

Slices 1 and 2 are code only. Slice 3's table is unread by an older image, so watches go silent and the new
kinds sit unread in `event`. Slice 4's table ignored means personal keys narrow back to key reach, so a
rollback fails closed.

## Where this differs from the blueprint

- Not a two-site change: the PAGE_OWNER readers of the owner id are fourteen rows, and the answering
  predicates take no page, so slice 1 changes signatures at eleven callers (list in slice 1).
- `via` and `display_name` have five call sites and take a host only; the "your Claude" wording at
  `gallery.py:4607` and the `app_hosts` dict at `gallery.py:7199` also carry the owner's name.
- `access.can_read` does not exist; `access.py` is created in slice 2.
- `migrate.py` has no table list; the real obligation is a seed row in `tests/test_migrate.py`.
- `event.kind` has no CHECK, so no migration for the new kinds.
- The feed flag is `include=watched` (the existing `include=receipts` idiom), not `include_watched=1`.
- Coalescing needs state: `page_watch.changed_notified_at`.
- `store.patch` emits nothing today and has no after-commit hook; slice 3 adds one.
- Next free SQLite step confirmed: 41 (40 is the highest).

## Not checked

Callers outside this repo that read `/events` (inferred, not seen); `docs/decisions.md`,
`docs/agent/design.md` and `implementation-plan.md` (not read); the people spec's `access.can_read` line is
left uncorrected there. Nothing here was run: docs only.
