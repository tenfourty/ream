# Folders on the wall (design, 2 Nov 2026)

Tracker item runcove-5t3oj.61. Shape settled by Jeremy on 2 Nov 2026 (decision
`d_01M6DDFDYA419AHQVGPQ6AMV5K`, (a)): an agent can put a page in a folder when it publishes; he
can move pages between folders; a folder shows as one tile on his wall that opens to its pages.
Shaped by the offices' answers (`docs/agent/feedback/2026-11-02-folders.md`, eight publishing
offices: quarterdeck, ch, runcove-pmo, homelab, gg-ops, jeremys-mac, warpgate, retrobrew). Section
8 records his answers of 2 Nov 2026; two of them (8.2, and the archive rule in 3.4) go against what
the offices asked for.

## 1. Problem

The wall is one flat list of tiles, newest first. runcove-pmo alone has 37 pages; bosun's two daily
digests, one-off reports and project boards all sit side by side with the pages he must answer.
He cannot tidy by topic, and an agent cannot say "these belong together". Archive is the only
tidying tool, and it is the wrong one: it hides a page, it does not group it.

## 2. Goals, non-goals, anti-goals

Goals:
- An agent names a folder when it publishes (`--folder roadmap`); the folder is created on first
  use and the publish result **says so** when it did (all eight offices: a typo must be visible).
- He sees one tile per folder and opens it to its pages; he moves pages in and out, several at
  once by a multi-select on the wall.
- One level. A page is in at most one folder, or none: foldering is **opt-in per page** and the
  standing boards stay loose (quarterdeck, pmo, homelab, warpgate).
- API twin for every folder action, scoped to the calling key's own agent (AGENTS.md).
- The data model leaves room for a folder that holds several agents' pages, each page still
  showing its publisher (homelab).

Non-goals (v1): nesting (all eight said no); folder names derived by the service (all eight said
fixed); agents publishing into another agent's folder; folder sharing or permissions.

Anti-goals:
- **A folder hiding a question.** A question open to him is never invisible because its page is
  in a folder: the folder tile carries the count ("2 waiting") and the wall-wide "questions
  waiting on you" total still includes foldered pages. The offices (all eight; pmo and ch
  strongest) asked for the page to also stay loose on the wall; he chose the count alone (8.2),
  so the page shows only inside its folder.
- **A folder archive that loses a question unseen.** Archiving a folder archives every page in it,
  and archiving a page **sets its open questions aside** (runcove-5t3oj.64, 1 line: they stay open
  and unanswered, the asking agent is told, and nothing shows them - his wall, his waiting count,
  any office's waiting list - while the page is archived; unarchiving brings them back open and
  tells the agent). This folder design relies on that mechanism and does not define it. He is
  **warned first** of the page count and the set-aside question count, and an agent confirms in a
  second call (3.2, 3.4). Jeremy's ruling of 2 Nov 2026, which replaces his 21 Sep rule that an
  archived page's questions are dropped.
- **A rename that breaks an address.** The old slug keeps working, as agent renames do.

## 3. Shape

### 3.1 Data

Both databases, one column and two tables (SQLite step 11 in `db.py` `_SQLITE_STEPS`; Postgres
`schema.sql` with `if not exists`, as `archived_at` and `pinned_at` were added):

- `artifact.folder_id text null references folder(id)`, plus an index. Null is the ordinary state.
- `folder(id, owner, slug, title, archived_at, created_at)`, unique `(owner, slug)`. `owner` is an
  agent name, or the reserved name `jeremy` (`keys.RESERVED_AGENT_NAMES`) for a folder he makes;
  never null, because a unique index over a nullable column treats nulls as distinct on both
  databases and would allow duplicates. `artifact.agent` is untouched: **the publisher is still on
  every page**, so a folder of his holding several agents' pages shows each one's name.
- `folder_alias(owner, old_slug, folder_id)`, unique `(owner, old_slug)`: written by a rename.

The folder is a **shelf fact**, like archive, pin and rename: it makes no version, and an
identical republish still mints none.

Slugs: lowercase letters, digits and hyphens, 1-40 characters, validated like agent names; anything
else is a `422` naming `folder`. `title` is the display name, defaulting to the slug and set once
(`--folder-title "Cove public split"`; homelab asked for it). The `id` is stable; slugs are not.

### 3.2 The agent side

- **Publish:** `POST /artifacts` takes `folder` (and optional `folder_title`). Result gains
  `"folder": {"slug": "roadmap", "created": true}` (null when none); the CLI prints
  `created new folder 'roadmap'` on a first use, and plainly names the folder otherwise.
- **Update:** `PATCH /artifacts/{id}` takes `folder`; omitted leaves it, `""` takes the page out.
  `PatchBody` requires a body today, so an agent that only wants to re-file a page uses the move
  route below, which sends none (as `rename` does for the same reason).
- **Move, bulk:** `POST /artifacts/move` `{"ids": [...], "folder": "roadmap" | null}`. Result
  `{"moved": [...], "missing": [...]}`; `missing` is both "no such page" and "not yours", never
  told apart, so a key cannot learn which ids are real. This is pmo's 37-page sort. CLI:
  `artifacts-publish --move ID [ID ...] --folder x` / `--no-folder`.
- **List:** `GET /folders` (slug, title, page count, open questions, archived) and
  `GET /folders/{slug}/artifacts` (the same row shape as `GET /artifacts`, which also gains
  `folder`). CLI: `--folders`, `--list --folder x`.
- **Archive:** `POST /folders/{slug}/archive` `{"archived": bool, "confirm"?: bool}`; `archived`
  is required and has no default, as on the page route. Archiving is **two steps**: without
  `confirm: true` the call changes nothing and returns `{"would_archive": {"pages": 12,
  "questions_set_aside": 2}}`; with it, the folder and its pages are archived as 3.4 says, and
  the result reports the same two numbers as done. Unarchiving needs no confirm and reports the
  pages restored and the questions brought back.
- **Rename:** `POST /folders/{slug}/rename` `{"slug"?, "title"?}`. The old slug resolves through
  `folder_alias` for every folder route and for `--folder` on publish. Renaming to a slug that is
  in use is a `409`.
- A slug names the caller's **own** folder only; one that is not theirs answers as "no folder",
  `404`. The first use of an unknown slug on publish or move creates it (3.2 result says so);
  the read and rename routes never create.

### 3.3 The wall (Jeremy)

- A folder is one tile in the grid and one row in the list, built next to the page tiles: its
  title, "12 pages", the newest page's age, its publishers' names, and an open-question count
  when there is one. It sorts by its newest page, and by "needs him" like a page does: a folder
  holding any open question sorts among the pages that wait.
- It opens to its pages at `/f/{folder id}`: the same tile and list views, same filters. Inside
  a folder, pages sort **newest first** (by last publish), with pinned pages and pages that wait
  on him above the rest as on the wall (promised to Bosun, whose daily digests fill one folder).
- **Pages in a folder leave the main grid, with no exception** (8.2): a page with an open
  question shows only inside its folder, and its tile in the folder carries the usual "waiting"
  mark. The folder tile's count is the way to it. The wall-wide "N questions waiting on you" at the
  top counts **before** grouping, so it includes foldered pages, and its link (`?filter=waiting`)
  lists those pages flat, folders or not (my call: that link is "show me them").
- **Multi-select** (8.3, new UI, no equivalent on the wall today): a select mode that ticks tiles
  or rows, then "Move to folder" (an existing one, or New folder) or "Take out of folder" for the
  whole batch in one request. Its agent twin is the bulk move in 3.2.
- Controls, each with its agent twin from 3.2: a "Move to folder" entry in a page's tile menu (a
  list of folders plus "New folder"), "Rename folder" and "Archive folder" on the folder tile's
  menu, and a "Take out of folder" on a page inside one. His folders are unscoped (he may move any
  page on his wall), proved by the same-origin form guard like pin and archive.
- **Archive folder asks first.** A gallery-drawn sheet, not a browser `confirm()`: "Archive
  'roadmap'? 12 pages go to Archived. 2 questions waiting on you will be set aside, and the agents
  that asked are told; they come back when you unarchive." With Archive and Cancel. Zero open
  questions still shows the page count.
- **As built (controls).** A folder he makes is owned by the reserved `jeremy`; "Move to folder"
  (`/a/{id}/folder`) offers his folders plus the page's own agent's, and "New folder" makes one of
  his. Rename changes the title only (the slug is what agents type). Archive goes through
  `/f/{id}/archive`, a page that shows the same `would_archive` preview the API returns and
  changes nothing until its Archive button. Agent keys cannot name `jeremy`, so no agent route
  reaches his folders; a slug he uses that an agent also publishes with is the agent's own folder.
- An archived folder is one tile in the Archived tab (8.5), with its pages inside it; its
  "Unarchive" restores it (3.4).
- A folder with no pages (all moved out, none yet in) is not drawn; it exists until archived.

### 3.4 Archive, unarchive, delete

**Archiving a folder** (Jeremy, 2 Nov 2026) sets `folder.archived_at` and archives every live page
in it, exactly as a single-page archive does: each page's `archived_at` is set and its open
questions are **set aside** by the .64 mechanism (the asking agent is told; they leave every
waiting list until the page is unarchived). Nothing is closed. It runs in one transaction. Pages already archived on their own are left as they
were. A new column `artifact.archived_via_folder text null` records the folder id on exactly the
pages this archive archived.

**Unarchiving a folder** clears `folder.archived_at` and unarchives only the pages with
`archived_via_folder = that folder`, clearing the marker; their set-aside questions come back
open and the asking agents are told (.64). A page that was archived on its own
**before** the folder was archived stays archived (my call: he archived it on purpose, the folder
did not). Unarchiving or archiving a page by itself, or moving it out of the folder, clears its
marker, so a later folder unarchive never touches it. A question set aside on such a page comes back only
when that page itself is unarchived.

An archived folder takes no new pages: a publish or move naming it is a `409` that says it is
archived and to unarchive it first, rather than silently reviving or hiding the page.
Binning a page leaves `folder_id` set, so restoring it puts it back.

When an agent rename merges a folder into a same-slug folder the new name already owns, the
survivor keeps its own state: if it is archived, the other folder's live pages join it and stay
live (an identity rename must not archive pages and set questions aside as a side effect), and
if it is live, the other folder's archived pages stay archived on their own, their marker
cleared so a later folder archive or unarchive never touches them.

Open questions are found as the wall already finds them: `decision.answer is null and
closed_at is null` per page (the `waiting` subquery in `gallery._wall`, which .64 makes ignore
set-aside questions); a folder's count is the sum over its live pages, and the same query over the
live pages gives the "set aside" number for the warning.

## 4. What is reused

The `archive`/`pin`/`rename` pattern: store write scoped by `_scoped` (`agent=None` for the
gallery, the key's agent for the API), a gallery form route behind the same-origin guard, a
`/api/v1` twin, and a tile menu entry (`_tile_menu`). `rename.py`'s alias idea for slugs. The
`waiting` subquery and the sort already on the wall. Both-database SQL: only `db.py` talks to a
driver; the new queries use plain joins and `%s` placeholders.

## 5. Onboarding, CLI, skills (docs are done)

`agents.txt` gets a "folders" section (publish with `--folder`, what the result says, bulk move,
the open-question rule); `--help` for each flag; runcove `skills/artifacts/SKILL.md` and the
homelab `lab-artifacts` skill each get a short paragraph. Docs round per slice.

## 6. Slices

1. Schema on both databases, store writes, publish/patch `folder`, `GET /folders`, list a
   folder's pages. Pages are foldered; the wall is unchanged. (Lets pmo sort at once.)
2. Bulk move, rename routes, aliases; archive with its two-step confirm and unarchive
   (`archived_via_folder`); `rename.py` and `transfer` updated (7).
3. Gallery: folder tile with the waiting count, `/f/{id}`, the wall's grouping and total, his
   move/rename controls and the archive warning sheet. Live proof on his phone.
4. Gallery: the multi-select on the wall (its own slice, new UI, and a second live proof).
5. Docs, `agents.txt`, skills.

## 7. Risks

- **`rename.py` rewrites every column that holds an agent name** and a test holds it to the list;
  `folder.owner` is a new one and must join it, or a renamed agent leaves orphaned folders.
- **`transfer` moves a page to another agent**; its `folder_id` would then point at the old
  owner's folder. It clears the folder on transfer (the new owner sorts it), unless the folder is
  his.
- **The wall's `limit 200`** applies to pages before any grouping: folder tiles and counts need
  their own query, or a big folder's count is capped.
- **A typo makes a visible new folder** (by design). A typo'd folder can be fixed with a move
  and an archive; no merge in v1.
- **Setting aside in bulk.** Archiving a folder can set many questions aside and tell several
  agents at once. The warning and the agent's two-step confirm are the guard, and each page records
  the archive as it does for a single page.
- **A question he never sees.** With 8.2 (c) a waiting page is only inside its folder. The tile
  count, the wall-wide total and the Waiting link are what keep it visible; a folder tile that
  failed to show its count would hide a question, so it needs a test of its own.
- **`archived_via_folder` drift.** Every path that writes a page's `archived_at` (archive, bin,
  restore, transfer, duplicate) must clear or ignore it; a missed one lets a folder unarchive
  revive a page he archived by hand.
- **One level means no folder tree in the API**, so a future nesting would need a `parent_id`.
  The column can be added without a rewrite.

## 8. Decisions for Jeremy

**Answered 2 Nov 2026: (a) on 8.1, (c) on 8.2, (b) on 8.3, (a) on 8.5; 8.4 not asked.** One line
each: a folder he makes holds any agent's pages, mixed, each keeping its publisher's name (agents'
own folders stay one agent each); a page with an open question shows only in its folder, the tile
carries the count, and the wall-wide waiting total still includes it (the offices asked otherwise);
the multi-select ships in the first version; an archived folder is one tile in the Archived tab.
The same day he ruled that archiving a folder warns first and then archives its pages, setting
their questions **aside**, not closed (runcove-5t3oj.64, which folders depend on: the agent is
told, nothing shows them while archived, unarchiving brings them back open). Sections 2, 3.2, 3.3,
3.4 and 7 carry it; it replaces his 21 Sep rule.

8.1 **A folder you make yourself: whose pages can it hold?**
(a) Pages from any agent, and it can be mixed: each page keeps its publisher's name on the tile.
Agents' own folders stay one agent each. (b) Only one agent's pages, like an agent's own folder.
(c) Pages from several agents, but only ones you move in; agents still cannot publish into it.

8.2 **A page with an open question, inside a folder.**
(a) Both: the page also shows on the wall by itself, and the folder tile shows a count ("2 waiting").
(b) Only the page on the wall, the tile shows no count. (c) Only a count on the tile, the page
shows only inside the folder.

8.3 **Moving many pages at once on the wall.**
(a) First version moves one page at a time from its menu; a multi-select comes later if you
want it (agents can move many at once through the API today). (b) Build a multi-select on the
wall in the first version.

8.4 **A folder with nothing open and nothing new in it.**
(a) Sits among the tiles in newest-first order, no special treatment. (b) Drops to the bottom
once none of its pages has changed for thirty days.
*Not asked: the design takes (a), the wall's existing order; (b) is a later change if the wall gets crowded.*

8.5 **Where an archived folder goes.**
(a) The Archived tab, as one tile; open-question pages stay on the wall. (b) Gone from the wall
and shown only when you open the Archived tab and search.
*As asked, before the archive rule: (a)'s "open-question pages stay on the wall" no longer applies, because archiving a folder now archives its pages and sets their questions aside (3.4).*
