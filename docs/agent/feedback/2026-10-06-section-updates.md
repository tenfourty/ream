# Feedback: updating one section of a page (6 Oct 2026)

Asked of every maintainer before any design, on Jeremy's rule that agent-facing changes are
shaped by the agents who use them. Questions: (1) which pages and how they update them today,
(2) what goes wrong, (3) how to address a section, (4) which operations, (5) concurrent updates,
(6) history, notification, checks. Answers condensed from their messages; each is from real use
unless marked.

## warpgate maintainer — "Where the Warpgate CI Stands" (~40 KB)
1. Source kept locally, edited by string-replace with a once-only assert, republished whole;
   2–6×/day in a release, ~daily otherwise. Edits: add a row at the TOP of "Running now", change
   one row, swap a header chip.
2. The real failure is local source drifting from live: two release rows believed published were
   in neither the local source nor the live page. Whole-page republish from a stale source silently
   drops content. No races; size, cost, CSP fine.
3. (a) element ids already in the HTML; markers would be a second thing to keep in sync.
4. Insert as FIRST child (newest on top) > replace by id > append last child > remove (rare).
   Also: get one element by id, to edit what is live.
5. Different ids: both land. Same id changed since the read revision: refuse "re-fetch". An
   optional `--if-revision` on any update, whole-page republish included.
6. Own version each. Re-run the page check on the result; refuse an unknown id, never append
   elsewhere. No notification by default; a flag for "this one matters". Echo the new revision.

## homelab maintainer — the ops board (~140 KB, regenerated every 3 h), decisions page, arc status pages
1. Always whole-page: the board is rebuilt from sources; status pages from notes.
2. Not races. A whole-page rebuild silently dropped a promise to Jeremy, with no diff ("a rewrite
   drops promises", 20 Sep). Cost: 140 KB to change one row, and every republish reads to him as
   "updated" even when one line moved.
3. (a) element ids; refuse loudly on zero or several matches.
4. Replace and append most (append for logs and "what changed today"); insert-after-id
   occasionally; remove rarely (prefer replacing with a closed state).
5. Refuse with "page changed, re-fetch" via a revision precondition (If-Match). Silent merges are
   how text gets lost.
6. Own version each, with the section id in the version note and a history diff of just that
   section. Re-run the page check every time. No notification by default; a publisher flag.
   Keep decision slots: refuse if a replace drops a `data-artifact-decision` placeholder.

## quarterdeck maintainer — design pages (generated whole from markdown) and a pilot status page
1. Generated pages should stay whole-page (the markdown is the source). Section updates fit a
   status page: what is live, what waits on whom, a running log; a few times a day.
2. No lost edits or races. Friction: keeping rollback snapshots, and remembering to fetch before
   editing rather than regenerating from memory.
3. (a) element ids; refuse zero or several matches.
4. Replace, then append; remove and insert rarely.
5. Different sections: both land. Same section changed since fetched: refuse "section changed,
   re-fetch", via a per-section revision. Refusing the whole page for an unrelated section would
   push everyone back to whole-page republishes.
6. Own version each, so rollback stays "fetch version N, publish". Re-run the checks on the whole
   resulting page (a secret can span a section boundary). No notification by default. Later,
   quarterdeck may append a per-office decision log or handover history — append to a stable id
   would be enough; not in the pilot.

## cove maintainer — "Cove on the Forge" status page
1. A Progress block generated from `bd list` into `<!-- PROGRESS:BEGIN/END -->`, status text and
   question cards by hand; rebuilt whole and `--update`d. Should be several times an hour on a busy
   day; less in practice, because whole-page rebuilds are a chore.
2. Lost local source (scratchpad gone after a compaction; generator rebuilt from the published
   page's markers). The page falls behind beads. A full republish carried a layout regression
   into an untouched part (20 Sep, a clipped cell on his phone). No races, size or CSP problems.
3. (a) element ids; markers are what is improvised without them. A (b)-like fallback for
   unstructured pages would be nice, not needed.
4. Replace by a wide margin (progress block, status line); append for a log or "landed today";
   remove rarely; insert never.
5. Different sections: both land, without needing the whole-page base. Same section: refuse
   "changed, re-fetch" — optional, via an if-match version, so a generator that owns its section
   can overwrite.
6. Own version each, labelled with the section id. No notification by default; `--notify`. Re-run
   the check on the merged result; reject a fragment that leaves its section unbalanced. Decision
   slots outside the section survive; warn if a replace removes an open decision's slot.

## retrobrew maintainer — does not publish yet (a guess, not evidence)
A field-test results board updated a row at a time would want append and replace by id; different
sections merged.

## Where they agree
- Address by **element id** already in the HTML; refuse zero or several matches.
- **Replace** and **append** first; insert-first (warpgate) and get-by-id (warpgate) wanted; remove rare.
- **Own version** per update, labelled with the section; page check on the **whole result**.
- **Never silently drop a decision slot**.
- **No notification by default**; an explicit flag.
- The failure it fixes: whole-page republishes from stale local sources silently drop content.

## Where they differ
- Concurrency: per-section precondition, both land when sections differ (cove, quarterdeck,
  warpgate, retrobrew) vs a whole-page precondition (homelab). Both can be offered: an optional
  page revision and an optional section revision, the caller choosing.
