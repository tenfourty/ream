# Artifacts on SQLite: slice plan (10 Oct 2026)

Spec: `docs/agent/specs/2026-10-09-sqlite-design.md` (§8 Slices is the source; this file is the
working order). Each slice lands on `main` by itself, with the full suite green. Slices 1–4 change
nothing on Postgres: same responses, same refusals.

| # | Slice | Files (main) | Done when | Verify |
|---|---|---|---|---|
| 1 | **Time in Python.** No `now()`, `interval`, `make_interval` or `extract(epoch …)` in SQL; every timestamp is computed in Python (aware UTC) and passed as a parameter. Column defaults stay in `schema.sql` for now (Postgres only) | decisions, store, email_share, connect, keys, comments, frame_pass, idempotency, events, gallery, admin | `git grep -nE "now\\(\\)|interval '|make_interval|extract\\(epoch" src/artifacts/*.py` finds nothing; suite green on Postgres | full suite |
| 2 | **Portable SQL.** `= any(%s)` → expanded `in (…)`; `array(select …)`; `lateral` → correlated subquery; `delete … using` → `where exists`; `greatest` → a portable form; `is [not] distinct from`; casts; `seat_read` stores `''` for an unlabelled session | store, comments, holds, gallery, decisions, keys, rename, events, schema.sql | the inventory's non-portable rows other than locks are gone; suite green | full suite |
| 3 | **The connection layer.** `Database` owns transactions (plain on Postgres, savepoints for nesting), its own `Rollback`, eager fetching and error mapping (`RevisionMismatch` from a primary-key clash); `for update`/advisory behind a helper; `ctid` → primary key; `octet_length` → `length(cast(… as blob))`; `%s::jsonb` → the adapter | db, store, idempotency, rename, events | no `psycopg` import outside `db.py`; suite green | full suite |
| 4 | **NUL and surrogate refusals in the layer** for every str and JSON parameter, with the errors callers see today | db, tests | the `DataError` tests pass without relying on Postgres | full suite |
| 5 | **SQLite backend.** `schema_sqlite.sql` (declared types, `user_version` steps), pragmas, `BEGIN IMMEDIATE`, the type adapters and converters, `DATABASE_URL=sqlite:///…`; the test fixtures run on SQLite; CI runs both | db, schema_sqlite.sql, conftest, ci.yml | the full suite green on SQLite and on Postgres | both suites |
| 6 | **In-process purge and backup by wall clock** (04:00 UTC backup, then purge; start-up catch-up); remove `deploy/purge-cronjob.yaml`. Deployment changes (Recreate, fsGroup, `/data`, `SQLITE_TMPDIR`) go as a fork PR on jeremy/homelab once the manifests have moved there | serve, admin, a new `maintenance.py`; homelab PR | tests for order and catch-up; homelab's PVC exists first | full suite |
| 7 | **`migrate-from-postgres` / `migrate-to-postgres`** with the canonical-JSON verifier; the runbook `docs/agent/runbooks/sqlite.md`; a rehearsal on a pg_dump copy | admin, a new `migrate.py`, docs | the verifier catches a changed row in both directions; the rehearsal passes | full suite + rehearsal |
| 8 | **The cutover** (announced in #agents; Flux suspended; a one-off Job; smoke tests) | homelab manifests | live on SQLite, verified end to end | live smoke |
| 9 | **After the rollback week:** remove the Postgres paths, CNPG and its CronJob (Jeremy's yes) | db, homelab | — | full suite |

Docs for each slice: `AGENTS.md`, `README.md`, the `conftest.py` header and `docs/agent/design.md`
change in the slice that changes what they describe (spec §6).
