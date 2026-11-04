# Generated and kept-current pages (design, 4 Nov 2026)

Tracker item runcove-5t3oj.15. Status: **proposed; nothing built; section 8 is waiting on
Jeremy.** Shaped by the offices' answers of 7 Oct 2026 (`docs/agent/feedback/2026-10-07-generated-pages.md`:
warpgate, cove, retrobrew, homelab, quarterdeck; all five replied). Works beside the item that
shows each round's changed parts and flags pages that render broken (runcove-5t3oj.56, section 6);
the two divide the work there.

## 1. Problem

A page a script regenerates, or a hand-kept board an agent promised to keep current, looks the same
whether it was checked a minute ago or its timer died a week ago. Four things go wrong:

- **Checked and changed are one time.** An identical republish is a no-op by design
  (`store.patch`: "an identical body is not a new version", homelab-ij8.41), so `updated_at` and
  `version.created_at` only move on a real change. A board that renders every 30 minutes and
  changes four times a day shows "5 hours ago" while perfectly healthy; homelab: "a
  checked-but-unchanged page looks identical to a dead timer". Agents work around it by
  stamping a time and a checksum into the page themselves (retrobrew, quarterdeck, PR 132).
- **A dead timer is silent.** A publish failure is invisible unless the script checks its exit
  code (homelab); a broken renderer leaves a page that looks current but old (retrobrew); a
  hand-published office page sat unrepublished 30 Sep to 7 Oct while work moved hourly, "until
  Jeremy asked" (cove).
- **Jeremy cannot tell which part moved** on a whole-page republish. Section updates already
  name their part on the tile (`gallery._part_line`, ruling 1a of 6 Oct); a whole republish says
  nothing (homelab: "most republishes change one line and Jeremy cannot tell which").
- **Nobody can ask the server "when did this last succeed?"** except by reading a time the
  publisher typed into the page.

## 2. Goals, non-goals, anti-goals

Goals:
- `--touch`: "checked, still current", no new version, no change to the wall's order or to
  anything he has read (cove, retrobrew, homelab, quarterdeck: all four who wanted a checked time).
- A **checked** time distinct from a **changed** time, both service-filled; the existing
  `<!--artifacts:updated-->` marker (`stamp.py`, `MARKER`) keeps meaning "changed".
- A **declared refresh interval**, set by the publisher, never inferred; the wall flags a page when
  it lapses (all five; warpgate and homelab: inference misfires on pages quiet for days or at night).
  Works for hand-kept pages as much as generated ones (cove).
- A publisher-declared **failure**: `--touch --status failed` shows on the wall at once
  (retrobrew). The server's own record of the **last successful check** is what the stale flag
  reads (homelab), so a script that dies before it calls anything is caught by lapse alone.
- Name the parts that changed on a whole republish: the publisher's `--note` shows on the tile
  (warpgate, cove, retrobrew, quarterdeck: "(b)"), and the service names one thing on its own,
  a question slot appearing or disappearing (cove's exception). Anything beyond that waits on
  Jeremy (8.3).
- An API twin for every wall control (AGENTS.md), scoped to the calling key's own agent; Postgres and SQLite.

Non-goals (v1): inferring an interval from history; a notification to Jeremy outside the wall (no
Telegram, no email); a general scheduler or a service that runs the agent's job (the service
flags, the agent's timer still runs); an interval he sets from the wall (8.5); automatic
`--if-revision` on every whole republish (cove asked; section 7 says why it is separate).

Anti-goals:
- **A touch that looks like work.** A touch must not reorder the wall (`updated_at` stays), must not
  mark a page unread, must not make a history entry, and must not mint a revision. Otherwise a 30-minute
  timer drowns the page it keeps current, which is the thing `store.patch` was written to stop.
- **A "current" flag that lies.** A page whose last check was a failure is never also shown as
  fresh; the failed state wins until a later ok check or a real publish.
- **A flag that can be walked.** `touch` on a page that is not the key's answers exactly as "no
  such page" does.
- **A flag nobody can clear.** A page an agent retires needs a way to stop being flagged
  (`refresh_every` cleared, or archive).

## 3. What exists, and what it leaves open

Checked against the code, 4 Nov 2026.

| Piece | Where | Covers | Leaves open |
|---|---|---|---|
| `<!--artifacts:updated-->` | `stamp.py` `MARKER`, `apply`; filled at serve time from the live version's `created_at`, so it never enters the revision hash | "changed" time inside the page | no "checked" time; not on the wall tile |
| `--note` on a whole update | `PatchBody.note`, `store.page_note`, stored in `version.note`; only stored when something moved | a history line | not shown on the tile; whole-page versions have no `section` |
| `--if-revision` | `PatchBody.expected_revision`/`if_revision` (`api.py`), `store.PageChanged`, `RevisionMismatch` | refuse over a hand edit | not default (cove) |
| `--preview PNG` | `client.py` | render without publishing | none |
| Section updates | `PATCH /artifacts/{id}/sections/{el}`, `sections.py`, `version.section`, `version.notify` (`schema.sql`) | name a part on the tile | whole republish only |
| Part line on the tile | `gallery._part_line`, fed by `gallery._wall` (`version.section` of the current version, shown only while unread) | "Running now: added" | whole-page versions have `part` null |
| Identical republish | `store.patch`: returns the stored revision, bumps `updated_at` only for a title change | no history spam | proves nothing about "checked" |
| Show what changed | `pagediff.render_changes_safely`, `gallery` (`Showing what changed since v..`) | in-page diff on a review round | not a list of parts |
| Nightly job | `maintenance.py` (one daemon thread, 04:00 UTC) | a model for a periodic task | no per-minute tick |

## 4. Shape

### 4.1 Data

Both databases (SQLite next free step in `db.py` `_SQLITE_STEPS`, after 15; Postgres `schema.sql`
with `add column if not exists`, as `archived_at` and `folder_id` were added). Four nullable
columns on `artifact`; no new table:

- `refresh_every integer null`: the declared interval in seconds, 60 to 31 536 000. Null means no
  promise and no flag.
- `checked_at timestamptz null`: the last time the publisher vouched for the page (a touch, any
  publish including an identical one, a section update, or declaring an interval).
- `check_status text null check (check_status in ('ok','failed'))`: the last vouching's verdict.
- `check_note text null`: at most 200 characters, for `failed` ("renderer exited 2").

These are **shelf facts**, like folder, archive and pin: they make no version, they do not touch
`updated_at`, and an identical republish still mints none. Every call that sets them sets
`checked_at` from the database clock (`now()` / the SQLite equivalent already used in `store`),
never from the caller.

**Changed** is the current version's `created_at`; it is the time the marker already shows. **Last
good check** is `max(changed, checked_at)` when `check_status` is not `failed`. These are computed in
Python after the rows are read, not in SQL: timestamp arithmetic differs between the two drivers and
only `db.py` talks to one.

**Lapsed**: `refresh_every` is set, the page is live (not archived or deleted), and `now - last good
check > 2.5 * refresh_every`. The 2.5 is retrobrew's number, fixed for v1 (one number the offices agree
on; a per-page multiplier is a later field). **Failed**: `check_status = 'failed'`, flagged at once.
Declaring `refresh_every` counts as a check (sets `checked_at`), so a freshly declared interval on an
old page does not flag at once.

### 4.2 The agent side

- **Touch:** `POST /artifacts/{id}/touch` with `{"status"?: "ok"|"failed" (default ok), "note"?: str,
  "if_revision"?: str, "refresh_every"?: int|0}`. No body is required (like `rename` and `pin`, the
  `PatchBody` "body required" trap does not apply). `if_revision` makes the touch say "still current
  *as of this revision*": a page changed since answers `409` with the same body as `PageChanged`, so a
  timer never vouches for a page someone hand-edited. Returns `{"checked_at", "changed_at", "revision",
  "version", "status", "refresh_every", "lapses_at"}` where `lapses_at` is null without an interval. A
  retry with the same `Idempotency-Key` is the same result (`idempotency.py`). Missing or someone
  else's page: `404`, identical (`store._scoped`).
- **Any publish counts as a check.** `POST /artifacts`, `PATCH /artifacts/{id}`, an identical
  republish and a section update set `checked_at = now`, `check_status = ok`, and clear `check_note`.
  This is the server-side "last successful publish" homelab asked for, and it means a generator that
  already republishes every round needs no new call. Touch exists for the round where it would
  render and upload an unchanged page for nothing.
- **Declare:** `refresh_every` on `POST /artifacts` and `PATCH` (seconds; `0` or `null` on PATCH
  clears; omitted keeps), and on `touch`. CLI: `--refresh-every 30m` (also `2h`, `1d`),
  `--no-refresh`. `GET /artifacts/{id}` and the list rows gain `refresh_every`, `checked_at`,
  `changed_at`, `check_status`, `check_note`, `lapsed` (bool), `lapses_at`. A value below 60 s or above a
  year is a `422` naming `refresh_every`.
- **CLI:** `artifacts-publish --touch ID [--status failed] [--note "renderer exited 2"]
  [--if-revision REV]`; it prints `checked, still current (changed 3 h ago)` or `marked failed`. The
  exit code is non-zero on `409` and on any refusal, so a timer script that checks it catches a
  rejection. `--touch` without an id on the same line as a publish is refused (one verb at a time, as
  `--move` is).
- **Lapse nudge to the publisher:** the first time a page lapses or fails, one event is written for its
  agent (`events.append`, kind `page.lapsed`, carrying id, interval and last good check), so a session
  listening on its seat hears that its own timer stopped; cove's `artifacts-decide wait` reaping shows
  waiting alone is unreliable, so this is a page event on the same inbox, not a new channel. Raised by a
  new small tick next to `maintenance.py` (the existing thread wakes once a day, so this is a second
  daemon thread, every 5 minutes, idempotent by a `lapse_notified_at` column cleared by any check).
  This is the one item beyond the wall, kept in its own slice (section 6) so it can be dropped.

### 4.3 The wall (Jeremy)

Everything here is proposed; 8.1 and 8.2 decide it.

- Each tile and list row shows **changed** as today's age. When the page has ever been touched or has a
  declared interval and its last check is newer than its change by more than a minute, a second small
  line shows **checked** ("changed 5 h ago, checked 4 min ago").
- A lapsed or failed page carries a flag on the picture, in the place and the style of the "N
  questions for you" chip (`gallery._wall`, `flag`), in amber, not red: "Stale: expected every 30 min,
  last checked 3 d ago", or "Failed 14:02: renderer exited 2". It does not reorder the page, and does not
  count as a question.
- A wall-wide line beside the waiting total ("2 pages stale") links to `?filter=stale`, the same flat
  list as `?filter=waiting`.
- The page's own view carries the same facts in its header strip (changed, checked, interval).
- Archived pages are never flagged. A page inside a folder shows its flag in the folder (folders spec
  3.3: pages in a folder leave the main grid); the folder tile gains a "1 stale" count the way it carries
  "N waiting", so a stale page is not hidden by being filed.
- Controls: none that only he can do in v1. Everything he sees is read-only, so the API twin is the read
  fields in 4.2; the write side is the agent's by design (the publisher is the one who knows its
  interval). 8.5 asks whether he gets a mute.

### 4.4 Naming the parts on a whole republish

Two sources, same display as a section update's part line (`_part_line`), shown only while unread:

1. **The publisher's `--note`**, already stored on the version (`version.note`). Shown on the tile as
   the part line when the version has no `section` (today a whole-page version has `part` null).
   Warpgate, cove, retrobrew and quarterdeck preferred this: the renderer knows what moved and says it
   in its own words ("2 slices landed", retrobrew; ids repeat across versions, so a service guess would
   be wrong there).
2. **Service-derived, only for question slots**: `store.patch` compares the set of decision slot ids
   in the old and new bodies and appends "slot added: X" or "slot removed: Y" to the line. This is
   cove's exception (a new decision slot, a card removed) and is deterministic. The slot ids are found
   the way a section update already finds an id (`sections.locate_many`). No heading diff, no text diff.

Stored as a nullable `version.parts` text column (1 line, 200 characters; one place to read for
the wall, the history and the page header). Whole-page versions only; a section update keeps `section`.

### 4.5 Where this stops and the broken-render item starts

The two items touch the same tile and the same version row, so they split by **who knows**:

| | This item (.15) | The render-broken item (.56) |
|---|---|---|
| Publisher says | its note, its interval, its `failed` | none |
| Service works out | question-slot appear/disappear; lapse | heading-level "which parts moved" per review round; "this page renders broken" |
| Reader sees | checked/changed times, stale and failed chips | per-round changed-part list; broken-render chip |

Contract between them, so neither rebuilds the other:
- This item adds `version.parts` and the flag chip with a `kind` (`stale`, `failed`). The other item
  **writes into `parts`** (its computed heading diff, when 8.3 says automatic) and **adds a kind**
  (`broken`) to the same chip; it builds no second column and no second chip.
- This item does not compute a page diff or look at rendered output. `pagediff.py` and `thumbs.py` stay
  the other item's.
- A page whose publisher says `failed` and whose render is also broken shows one chip, `failed` first
  (the publisher's own word beats a guess).
- Build order: this item's slice 1 and 3 land first because they create the column and the chip.
  (I could read the other item's title and body only; it had no spec on 4 Nov, so this split is a
  proposal to its owner, not an agreement.)

## 5. What is reused

The `archive`/`pin`/`rename` pattern: a store write scoped by `_scoped`, an `/api/v1` twin, `Idempotency-Key`
(`idempotency.py`). `store.patch`'s identical-body branch for the "any publish counts as a check" write.
`stamp.py` unchanged, so the marker and the revision hash are unaffected. `gallery._wall`'s `part`/`flag`
columns and `_part_line`. The folder tile's waiting count for the stale count. `events.append` for the
nudge. `onboarding.py` text (the "A page a script regenerates whole" paragraph) gets `--touch` and
`--refresh-every`. Both-database SQL: plain updates with `%s` placeholders; time arithmetic in Python.

## 6. Slices (agent-hours)

1. **Data and touch** (3 h). Columns on both databases, `store.touch`, `POST /artifacts/{id}/touch`,
   declare on publish/patch, any publish records a check, read fields on `GET`, scoping tests, a
   SQLite and a Postgres run in CI. The wall is unchanged. Lets cove and retrobrew start declaring at once.
2. **CLI** (1.5 h). `--touch`, `--status`, `--refresh-every`, `--no-refresh`, exit codes, `--help`;
   reinstall the tool.
3. **The wall** (4 h). Checked/changed line, stale and failed chip, `?filter=stale` and its total,
   folder tile count, page header strip. Live proof on his phone. Waits on 8.1 and 8.2.
4. **Part names** (3 h). `version.parts`, note on the tile, slot appear/disappear. Waits on 8.3.
5. **Lapse nudge** (2 h). The tick thread, `page.lapsed` event, `lapse_notified_at`. Waits on 8.4;
   droppable.
6. **Docs** (1.5 h). `agents.txt`/`onboarding.py` section, `news.py` entry, runcove `skills/artifacts/SKILL.md`,
   homelab `lab-artifacts` skill (docs are part of done); one worked timer example with an exit-code check.

Total about 15 agent-hours. Slices 1 and 2 (4.5 h) are agent-only and need no decision.

## 7. Risks

- **A touch is a write per interval per page.** At 30 minutes across tens of pages that is trivial on
  both databases; the touch updates one row and takes no version lock. The rate limit (`ratelimit.py`)
  applies as on any write.
- **The clock.** `checked_at` is the database's, never the caller's, for the reason `stamp.py` gives.
- **Lapse in Python reads at most the wall's 200 rows** (`limit 200` in `gallery._wall`); the stale
  filter needs its own query for pages beyond it, as the folder counts do.
- **Pods restart on every push to main**, so the nudge tick must not assume it ran continuously:
  `lapse_notified_at` makes it idempotent, and a restart may delay a nudge by one interval, never repeat it.
- **Default `--if-revision`** (cove) is a different change: it alters every generator's failure mode
  (a hand edit now makes the timer fail). It is out of v1; `--touch --if-revision` and the existing flag
  cover it for anyone who opts in. Raise it again after the offices have used touch.
- **The slot check** reads two bodies on every whole update. It is the same parse a section update does, bounded
  by the body size cap, and a page with no slots does no work.
- **A flag Jeremy learns to ignore.** A noisy interval (declared too tight) trains him to skip amber.
  Mitigations are the 2.5 multiple, amber not red, and no count in the top-line total unless 8.1 says so.
- **`rename.py` and `transfer`:** no new column holds an agent name, so neither needs a list change;
  `transfer` keeps the interval, and the new owner's next check resets it.

## 8. Decisions for Jeremy

Each is a choice about what he sees; (a) is the recommendation. Recorded here once he answers.

8.1 **How a stale or failed page shows on the wall.**
(a) An amber tag on the page's picture ("Stale: expected every 30 min, last checked 3 days ago" or
"Failed 14:02: renderer exited 2"); the page stays where it is in the order; a line beside "questions
waiting on you" says "2 pages stale" and opens just those. (b) The amber tag only, with no count line.
(c) The tag, and a stale or failed page also moves up to sit with the pages that need you.

8.2 **How "changed" and "checked" times show.**
(a) Today's age stays "changed 5 h ago", and a second small line "checked 4 min ago" shows on pages
that are being checked, only when it is newer than the change. (b) One time on the tile, the last check;
when it changed is on the page's own header. (c) Keep one time (changed) on the tile as now, and show
checked only when you open the page.

8.3 **Naming what moved on a whole republish.**
(a) The tile shows the agent's own one-line note ("2 slices landed"), and the service adds only when a
question slot appears or disappears. (b) The same, and the service also names changed headings on its
own. (c) Only the agent's own note, nothing added by the service.

8.4 **Should the agent be told when its own page goes stale?**
(a) Yes, once, when a page lapses or the agent marks it failed; it arrives like any other message for
that agent and repeats only after the page has been fine again. (b) No, only the wall shows it.

8.5 **Can you set or silence a page's interval yourself?**
(a) Not in the first version: only the agent that publishes a page sets its interval and clears it.
(b) Yes: a page's menu gets "Expect quiet until..." to mute the flag for a chosen time, with the same
action available to the agent through the API.
