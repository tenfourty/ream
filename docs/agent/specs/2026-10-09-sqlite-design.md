# Artifacts on SQLite: design (9 Oct 2026)

## 1. Intent

Ruled by Jeremy on 9 Oct 2026 (multi-user rulings, 4b; backups, (a)):

- **Goal.** Artifacts runs on SQLite instead of Postgres: one process, one database file, no
  database server. It is the shape a small cove VM wants, and the switch starts now, independent
  of the later move into cove.
- **Non-goals.** Multi-user, cove auth and the move into a cove VM (designed separately, not
  built). Changing the agent API or anything an agent or reader sees.
- **Success criteria.**
  1. The full test suite passes on SQLite, in CI and locally, with no Postgres anywhere.
  2. Production runs on SQLite, with every page, version, decision, comment, event and key
     carried over and checked: row counts and a checksum per table match.
  3. Nothing an agent or reader does behaves differently: same API, same responses, same refusals.
  4. A nightly snapshot reaches the off-site backup, so at most 24 h of data is at risk (as today).
  5. Rollback to Postgres stays possible for a week after the cutover, without losing what was
     written on SQLite in the meantime (the copier runs in reverse, with the same verifier).
- **Constraints.**
  - Production image: Python 3.12, SQLite 3.40.1, measured in the pod.
  - Storage and the off-site path are homelab's: a 5Gi `linstor-thin-retain` RWO PVC, Deployment
    strategy `Recreate`, and a homelab CronJob that copies the app's nightly snapshot from the PVC to
    `artifacts-backup-nfs`, which VolSync already ships off-site daily. The app pod mounts no NFS.
  - Litestream to a new bucket on the QNAP's S3 follows later.
  - The cutover is a production change: announced in #agents first. Removing CNPG afterwards
    deletes data, so it needs Jeremy's yes.

## 2. Approach

**Make the SQL portable while still on Postgres, then switch.** Every slice except the switch
itself lands on Postgres with the suite green. The switch is then a connection layer and a
schema, not a rewrite. Both databases work during the rollback week. After it, Postgres goes.

### Today's Postgres dependencies (surveyed 9 Oct)

- **Connections:** 119 `db.connect()` sites, 224 `conn.execute`. Positional `%s` only, rows read by
  tuple index, and about 30 `conn.transaction()` blocks, some nested (savepoints). `rename_agent`'s
  dry run rolls back by raising `psycopg.Rollback`.
- **Schema:** re-run on every start under an advisory lock. It has about 50
  `alter … if not exists`, one `do $$` block, constraint changes and backfills.
- **SQL:** `for update` (13 places, plus one `skip locked` with `ctid`), advisory locks (3), about
  60 `now()` and 14 `interval` expressions, 22 `default now()` columns, `= any(array)` (6),
  `array(select …)`, `lateral`, `delete … using`, `extract(epoch)`, `greatest`, `%s::jsonb`, and
  `octet_length`, which SQLite 3.40.1 lacks (it arrived in 3.43).
- **Computed values:** some queries return expressions, not columns, that must come back typed:
  `max(last_read_at)` (`events.py`), `max(v.created_at)` (`keys.py`), booleans such as
  `expires_at > now()` (`connect.py`) and `claimed_by is not null and …` (`decisions.py`).
- **Types:** 5 jsonb columns, decoded to Python automatically; about 45 timestamptz; 2 bytea;
  booleans.
- **Errors:** `UniqueViolation` carries the race reasoning in `store.patch`. `DataError` (a NUL
  byte, lone surrogates) is pinned by tests.
- **Tests:** a throwaway Postgres server per run. 37 test files use raw SQL, some Postgres-only
  (`pg_stat_activity`, `generate_series`).

### Decisions

| Area | Decision | Why |
|---|---|---|
| Driver | stdlib `sqlite3`, autocommit (`isolation_level=None`), one connection per `connect()` as today; every result fetched eagerly by the layer | No new dependency; the call shape stays the same; an unfinished `update … returning` cursor never holds a write transaction open |
| Pragmas, per connection | `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `synchronous=NORMAL` (FULL until 20 Oct 2026), `cache_size` 16 MiB, `mmap_size` 256 MiB; connections reused (up to 4 idle); `pragma optimize` nightly | Concurrent readers; cascades work. **Changed 20 Oct 2026 (Jeremy, runcove-5t3oj.34):** tuned for speed. NORMAL drops the per-commit disk wait (about 7 ms on this DRBD volume) at the cost of the last few seconds of commits on a power cut or kernel panic on the node; never corruption |
| Writes and locking | On SQLite, `conn.transaction()` opens `BEGIN IMMEDIATE`; a nested one is a `SAVEPOINT`; a dry run rolls back through the layer's own `Rollback`. On Postgres it stays an ordinary transaction | SQLite has one writer at a time. `BEGIN IMMEDIATE` serialises writers before they read, which covers every `for update` and advisory-lock path that reads *inside* its transaction (idempotency, rename, connect, email codes, transfer, review claims and verdicts). It also avoids `SQLITE_BUSY_SNAPSHOT` when a read turns into a write |
| Reads before a transaction | Unchanged, and no stronger than today: `store.patch`, `decisions.ask` and `decisions.record` read in autocommit first. The patch race stays caught by the `version` primary key, and the `rowcount` guards stay | Not a regression: Postgres behaves the same today |
| One writer, so no slow work inside a transaction | No network call (the ggshield second opinion, nudges) and no second writing connection inside a transaction. A test pins it for the idempotency path | A transaction on SQLite blocks every other writer in the process |
| `for update`, advisory locks | Behind a helper that emits them on Postgres and nothing on SQLite, removed with Postgres | The serialisation above replaces them |
| Placeholders | The connection layer turns `%s` into `?` | One place, and safe: there are no other `%` characters in the SQL (a test pins that) |
| Time | Timestamps computed in Python and passed as parameters, never `now()` or `interval` in SQL. One adapter writes every datetime: an aware time converted to UTC, a naive one treated as UTC (`said_at` arrives from callers), always formatted `%Y-%m-%d %H:%M:%S.%f+00:00` (6 digits even when zero; not the deprecated stdlib adapter). Column defaults use the same 6-digit form, computed in Python at insert, not SQLite's millisecond `%f` | One format makes text comparison and ordering correct |
| Typed results | Columns are declared `TIMESTAMPTZ`, `BOOLEAN` and `JSONB` in `schema_sqlite.sql`, and connections open with `detect_types=PARSE_DECLTYPES \| PARSE_COLNAMES` and converters of our own. A computed expression carries its type explicitly: `max(last_read_at) as "m [TIMESTAMPTZ]"`. A test covers each expression site above | Callers keep receiving aware datetimes, bools, lists and dicts; SQLite only knows the declared type of a plain column |
| JSON | TEXT with `check (json_valid(…))`; the adapter encodes on write | — |
| Booleans | INTEGER 0/1 under a `BOOLEAN` declaration | No 0 or 1 leaking into JSON responses |
| Bytes | BLOB | — |
| `event.seq` | `INTEGER PRIMARY KEY AUTOINCREMENT` | An event number is never reused after a delete, as today |
| `seat_read` | An unlabelled session is stored as `''`, not NULL, under a plain unique index | SQLite has no `nulls not distinct` |
| Schema | A fresh final-shape `schema_sqlite.sql`, versioned with `PRAGMA user_version` and numbered steps. Each step's statements run one by one inside `BEGIN IMMEDIATE`, never through `executescript` (which commits implicitly) | SQLite cannot run `alter … if not exists` or `do $$`; a version number replaces "re-run everything" |
| Errors | `sqlite3.IntegrityError` mapped by `sqlite_errorname` (`SQLITE_CONSTRAINT_PRIMARYKEY` = the patch race). `sqlite3.OperationalError` joins `psycopg.OperationalError` in the wait loop's ride-out (`events.py`) | — |
| NUL and surrogates | The layer refuses a NUL in every str parameter and in every JSON-encoded value, with the same error type callers see today. A lone surrogate raises `UnicodeEncodeError` in sqlite3; the layer maps it to the same refusal | Postgres refuses both in any text or jsonb value, and the tests pin it |
| Rows by key order | `ctid` becomes `rowid`; `octet_length(body)` becomes `length(cast(body as blob))` | — |
| Purge and backup | Run in-process **by wall clock**, not "every 24 h from start", because every push to main restarts the pod: the backup at 04:00 UTC and the purge after it, so anything purged is in a backup first. At start-up, if the newest backup on the share is over 24 h old, it runs at once (replacing the CronJob's `startingDeadlineSeconds`). `deploy/purge-cronjob.yaml` and its kustomization entry go in the same slice | A second pod cannot share an RWO volume, and a timer reset by every deploy might never fire |
| Nightly backup, how | `VACUUM INTO /data/backups/.tmp-…`, then rename to `artifacts-sqlite-<UTC date>.db`, uncompressed (restic compresses and dedups; gzip defeats its dedup); 7 kept on the PVC, matched only by the `artifacts-sqlite-` prefix (never the pg_dumps' `artifacts-db-*`). A homelab CronJob running as root, hardened like authelia's, mounts the PVC read-only with podAffinity to the app's node and copies new snapshots to `artifacts-backup-nfs`. The WAL grows during the copy; harmless at this size | The QNAP share refuses non-root writers and `fsGroup` does nothing on NFS; an NFS mount in the app pod would tie the gallery to the NAS (a NAS outage would leave the pod stuck in ContainerCreating) |
| Storage layout | One 5Gi PVC mounted at `/data`: the database, its `-wal` and `-shm` (`/data/artifacts.db`), `/data/backups`, and `SQLITE_TMPDIR=/data/tmp`, not `/tmp`. Peak use during a backup was estimated here at about 2 GB; **wrong**: the SQLite file was 3.23 GB at the cutover (Postgres's TOAST compression hid it), so the volume holds about (KEEP + 2) times that, and it was grown to 30Gi on 20 Oct 2026 (runbook, "Sizing"). The pod gains `fsGroup: 10001` so uid 10001 can create files on the fresh, root-owned linstor volume; `readOnlyRootFilesystem` stays | — |
| Rollouts | Deployment strategy `Recreate`. **Accepted behaviour change**: each deploy has a few seconds' gap with 502s on held polls and the gallery, where today's rolling update overlaps. Clients already retry on 5xx (the onboarding says so; checked for `artifacts-decide wait` and the gallery's watch); the `deployment.yaml` comments about the overlap and the preStop drain are rewritten in the same slice | Two pods must never open the file at once |
| Checkpoints | The layer keeps one long-lived idle connection open, so closing the last request connection does not checkpoint and delete the WAL each time (repeated fsyncs on DRBD under `synchronous=FULL`) | — |
| Admin commands | `artifacts-admin` runs inside the pod (`kubectl exec`) while it runs. With the pod stopped (migration, rollback, restore drill), it runs as a one-off Job mounting the PVC | The file is reachable only through the PVC |
| `DATABASE_URL` | Set in this repo's `deploy/artifacts-config.env` as `sqlite:////data/artifacts.db`. The Postgres URI stays available as a second variable (from the CNPG secret `artifacts-db-app/uri`) through the rollback week, for the reverse copy | It comes from a CNPG-made secret today, not a sops file; no homelab change |
| Tests | One temporary SQLite file per test worker, emptied between tests. Lock-wait tests use thread events instead of `pg_stat_activity` | No server to start: faster runs |

## 3. Surfaces

None change for agents or readers. Operators see:

- `DATABASE_URL` accepts `sqlite:///<path>` as well as `postgresql://…`;
- the nightly snapshots in `/data/backups` on the PVC, and their copies on the backup share;
- `artifacts-admin migrate-from-postgres` (§5).

## 4. Error handling

- **Busy past `busy_timeout`:** the request fails with 503 and a retry hint. This should not happen
  at today's write rate; it is logged with the statement.
- **A failed backup:** logged, and retried at the next start-up or the next 04:00 UTC. A backup older
  than 48 h raises an artifacts health warning on the status board. Homelab's copy CronJob reports
  its own failures.
- **A corrupt file:** restore from the newest snapshot. The cutover runbook includes a restore drill.

## 5. Migration and cutover

- **`artifacts-admin migrate-from-postgres --from <pg DSN> --to <sqlite path>`** copies every table
  in dependency order, converting types, then verifies, refusing to finish on any mismatch:
  - row counts per table;
  - a checksum per table, defined as follows. Each row becomes canonical JSON: timestamps in the
    codec's format, JSON values with `sort_keys` and fixed separators, bytes as hex, booleans as
    `true`/`false`, NULL as `null`, and the `seat_read` NULL→`''` session mapping applied on the
    Postgres side. Each row is hashed with SHA-256; the row hashes are sorted in Python, then hashed
    together. No dependence on either database's collation or ordering.
- **`artifacts-admin migrate-to-postgres`** is the same copier in reverse, with the same verifier,
  for a rollback that keeps writes made on SQLite.
- **Rehearsal:** run against a copy of last night's pg_dump before the real window.
- **Window, an evening announced in #agents:**
  1. `flux suspend ks artifacts-app`, so Flux doesn't undo the next step within 5 minutes;
  2. scale the Deployment to 0;
  3. run `migrate-from-postgres` as a one-off Job mounting the PVC, and check its verification;
  4. apply the SQLite `DATABASE_URL` and the PVC mount (the change already merged, waiting behind
     the suspension);
  5. `flux resume ks artifacts-app`, and the pod starts on SQLite;
  6. smoke-test publish, fetch, a decision and a comment end to end, and watch an agent's wait wake.
- **Rollback, for one week:** suspend Flux, scale to 0, run `migrate-to-postgres` into CNPG as a
  one-off Job (CNPG stays running, backed up by its pg_dump CronJob, and the 5432 egress NetworkPolicy
  stays), verify, point `DATABASE_URL` back, resume.
- **Restore drill:** the same one-off Job pattern, restoring the newest snapshot into a scratch file
  and running the verifier's row counts against it.
- **After the week:** remove CNPG, its pg_dump CronJob and netpol egress, and the Postgres code
  paths, with Jeremy's yes. The homelab priority test changes in the same commit.

## 6. Docs (part of done, per slice)

- `AGENTS.md` (HOW: the test commands, `PG_TEST_DSN`, `DATABASE_URL`), `README.md`, the
  `tests/conftest.py` header, and `docs/agent/design.md` (the database decision) are updated in the
  slice that changes them.
- `deploy/deployment.yaml` and `deploy/purge-cronjob.yaml` comments change with slice 6.
- A runbook, `docs/agent/runbooks/sqlite.md`, covers the backup, a restore, the restore drill, the
  cutover and the rollback, and lands with slice 7.
- Homelab's own docs (the priority table, the changelog) change in homelab's commit.

## 7. Tests

- Every slice keeps the full suite green on Postgres.
- From slice 5 the suite runs on SQLite in CI. Postgres keeps a CI job until its removal, so the
  rollback path stays tested. The race tests run on both databases from slice 5.
- New tests:
  - the `%`-only-`%s` pin;
  - timestamp round trip and ordering;
  - JSON and boolean round trip;
  - the `BEGIN IMMEDIATE` serialisation for the race tests (two threads answering one decision:
    exactly one wins);
  - the patch race maps to `RevisionMismatch`;
  - NUL and surrogate refusals, in a session label and a comment anchor as well as a page body;
  - typed results for every computed expression listed in §2;
  - the purge never running before that night's backup, and the catch-up at start-up;
  - the migration verifier catching a changed row, in both directions.

## 8. Slices

Each lands on main by itself. Slices 1 to 4 change behaviour on Postgres not at all.

| # | Slice | Covers |
|---|---|---|
| 1 | Time in Python: no `now()`, `interval` or `extract(epoch)` in SQL | 2, 3 |
| 2 | Portable SQL: arrays → expanded `in`, `array(select)`, `lateral`, `delete … using`, `greatest`, `is distinct from`, casts; `seat_read` `''` sessions | 2, 3 |
| 3 | The connection layer owns transactions (on Postgres still plain transactions and savepoints; the `BEGIN IMMEDIATE` branch arrives with slice 5), its own `Rollback`, eager fetching and error mapping; `for update` and advisory locks behind the helper; `ctid` → primary key, `octet_length` → `length(cast(… as blob))`, `%s::jsonb` → the adapter | 3 |
| 4 | NUL and surrogate refusals in the layer, for every str and JSON parameter (instead of relying on Postgres `DataError`) | 3 |
| 5 | SQLite backend and `schema_sqlite.sql`, selected by `DATABASE_URL`; tests on SQLite in CI | 1 |
| 6 | Purge and nightly backup in-process by wall clock; `Recreate`, `fsGroup`, the PVC mount and `SQLITE_TMPDIR` in this repo's `deploy/`; remove `deploy/purge-cronjob.yaml`. Deploys only after homelab's PVC exists, and stays on Postgres until the cutover | 4 |
| 7 | `migrate-from-postgres` and `migrate-to-postgres` with the verifier; the runbook; a rehearsal on a pg_dump copy | 2, 5 |
| 8 | The cutover, using homelab's side: the 5Gi PVC (it must exist before slice 6 deploys, or the pod sits Pending) and the root copy CronJob, both built by homelab | 2 |
| 9 | After the rollback week: remove the Postgres paths and CNPG (Jeremy's yes) | — |

## 9. Risks

| Risk | Mitigation |
|---|---|
| A lock-dependent path behaves differently | `BEGIN IMMEDIATE` covers every path that reads inside its transaction; paths that read before it are unchanged from today. The race tests run on both databases from slice 5 |
| A computed value comes back untyped | Explicit column-type casts at each expression site, with a test each |
| Downtime on every deploy (`Recreate`) | Accepted; clients retry on 5xx; the gap is seconds |
| The writer queue under bursty writes (thumbnails, events, long-poll reads) | WAL means readers never block. Writes are small and short. `busy_timeout` 5 s, and a 503 with a retry hint beyond it |
| Timestamp text mis-ordering | One fixed format, written only by the codec, and a round-trip test |
| Migration silently drops or alters data | Per-table counts and checksums, refusing on mismatch; a rehearsal first |
| Data loss since the last snapshot | RPO 24 h as today, then near zero once Litestream lands. The backup runs by wall clock, so restarts cannot starve it |
| Losing CNPG's replication and failover | A drain or a deploy is seconds. **A dead node is minutes**: the RWO volume stays attached until piraeus's ha-controller fences the node, where CNPG fails over in seconds today. Accepted for one reader and agents that retry. No PodDisruptionBudget: `minAvailable: 1` on one replica would block Talos drains |
| The NAS being down | The app pod mounts no NFS, so the gallery is unaffected; only the off-site copy waits |
