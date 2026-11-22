# View stats: the design for runcove-5t3oj.158.1, 22 Nov 2026

From a house-designer lane; decisions settled by the office (below). Locations marked (inferred) were not read.

## Shape
Two portable tables, one module `src/ream/pageviews.py`. Count where a reader actually receives a page body, never inside `store`.
Rejected: a row per load (unbounded), reusing `event` (wakes waiting agents), counting at `frame_pass.spend` (inflates on frame reloads).

## Storage
- `schema.sql` (idempotent create if not exists); SQLite numbered step in `_SQLITE_STEPS` (`db.py` ~609; 35 was next on 22 Nov, re-check). `migrate.py` parity: both schemas identical tables/columns.
- `page_view_day(artifact_id, day, kind, views)`, PK all three. `day` UTC text YYYY-MM-DD. `kind` in owner|external|agent, validated in code (no CHECK). FK to artifact, cascade.
- `page_viewer(artifact_id, kind, viewer, views, last_at, counted_at)`, PK (artifact_id, kind, viewer). viewer = '' for the owner (never the owner id), agent name for an agent, `email_share.id` for a named recipient, '' for the anonymous link.
- `record()`: one transaction: insert on conflict do nothing (rowcount 1 = first view, counts); else update where counted_at < cutoff (counts); else bump last_at only. A counted view upserts `page_view_day`. Same on-conflict shape as `store.mark_read` (~store.py:2909).
- Retention: none at first (office decision C below). No prune.
- `service_meta` key `views_since`, written once at migration; replies carry `since`.
- `email_share.last_opened_at` kept, written by the same call; move its write out of `email_share.device_ok` (~186, writes on every check) into `record_open`.
- `record()` is best-effort: exceptions logged by type, request carries on. Emits no event.

## What counts
Dedupe window 30 min per (page, kind, viewer); 5 min for the anonymous link.
- owner: gallery detail GET `/a/{id}`, beside `store.mark_read` (inferred). Not: frame loads (`content._serve` after `frame_pass.spend`), changes view, re-minted pass.
- external named: `content.emailed` (~content.py:1258) after `shared_page_body` succeeds. Not: confirm page, 404, wrong code.
- external anonymous: `content.shared` (~content.py:1165) after `shared_page` succeeds.
- agent: OTHER agents' body reads: `/versions/{n}` body route (~api.py:2494), live-body route, body branches of `_read_page` (~mcp_tools.py:439). Never the page's own agent (decision A). Not: listings, /render, /changes, comments.
- Screenshots and thumbnails read through `store`, so never count. `api.py` ~1629/1687 call `store.get_body(as_reader=True)` internally: hence recording at surfaces.

## Privacy
Stored: the email already in `email_share`, counts, timestamps. Never IP, user agent, device cookie. Named recipients: totals and last open only, no per-day series. A withdrawn/replaced share keeps its row; API groups by email and flags `withdrawn`.

## API, MCP, CLI
- `GET /api/v1/pages/{id}/stats?days=30` (register as `/artifacts/{artifact_id}/stats`; both words via `_BothWords`). `store.get_owned(db, agent, id)` without as_reader; NotFound -> 404 "no such page", identical for unknown and not-yours.
- Reply: `{artifact_id, since, days:[{day, owner, external, agent}] zero-filled, totals, shares:[{email, opens, last_opened_at, created_at, withdrawn}]}`.
- MCP `page_stats` (read-only, idempotent; page_id, days 1-365). Add `day` and `last_opened_at` to `_REAM`; keep `email` fenced.
- CLI `ream stats PAGE-ID [--days N] [--json]` (cli.py `_OTHER`, `_HELP`). Bump CLIENT_LEVEL/MIN_CLIENT_LEVEL; news.py entry.

## Gallery
"Readers" link on the page controls -> `GET /a/{id}/stats`, server-rendered, `escape()`, no script. 30-day CSS bars per kind, per-recipient table, "counting since". Share panel shows `opens`.

## Slices (each lands alone)
1. Storage + recorder, unwired. tests/test_pageviews.py (windows, upsert counts, cascade, both DBs) + migrate parity test.
2. Recording at the four sites. tests/test_view_recording.py incl. negatives: screenshot, /thumb, frame-pass spend, /_shared.css, publisher's own read, confirm page without device, 404. Update tests/test_email_share.py for device_ok split.
3. API, MCP, CLI, news, docs (docs/mcp.md, onboarding.py skill text, shipped line under 20 Nov decisions.md entry). tests/test_page_stats_api.py (other agent == unknown id, both words), MCP tool test, client test.
4. Gallery panel. tests/test_gallery_stats.py (escaping, empty state, counting since).

## Risks
- Link-preview bots count on `/s/{token}/` (named shares immune). UA skip-list can follow.
- Anonymous counts approximate.
- `rename-agent` must also rewrite `page_viewer where kind='agent'` (rename.py not read).
- Every gallery open writes (trivial).
- No backfill; `since` says so.

## Office decisions (22 Nov, artifacts-maintainer; reversible, within the owner's 20 Nov answers)
A. The publishing agent's own reads are not counted (its polling would swamp the numbers).
B. Anonymous link-share readers count as external, unnamed (the owner asked especially for external pages).
C. No retention prune at first: rows are small (pages x 3 kinds x days); keeping is the cheap undo, deleting is not. Revisit if size matters.
D. (22 Nov, reversible) The publisher exemption covers every agent the caller can write: a signed-in app is exempt on the pages of all its person's app agents (`reach.writes()`), a key only on its own agent's. An owner-reach app reading a page it cannot write still counts as an agent view under its app-agent name.
