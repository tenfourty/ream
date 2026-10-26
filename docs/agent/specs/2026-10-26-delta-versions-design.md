# Every version kept, stored as deltas: design (26 Oct 2026)

## 1. Intent

Ruled by Jeremy on 26 Oct 2026 (runcove-5t3oj.45; mirrored back and confirmed "1a 2a",
m_01M5VYCKPFEV00W6GTHVBRJ5HW):

- **Goal.** Every version of every page is kept for ever, in a format that makes history cost
  almost nothing. The current version opens exactly as fast as today. Nothing is ever thinned.
- **Read cost.** A full copy every 100 versions, deltas in between, so any old version is
  rebuilt in under a second.
- **Deleting still deletes.** A deleted page keeps today's 30-day bin, then is destroyed with
  all its history (unchanged: the cascade from `artifact`).
- **Non-goals.** Moving bodies to object storage (company-wide design, later). Any change an
  agent or reader can see: the API, the CLI, the gallery and `revision` all stay the same.

**Why.** Every version is stored in full today. One page (cove's tracker board, 3.4 MB, hourly)
holds 4.3 of the database's 4.7 GB. Measured on 24 of its versions on 26 Oct with zstd at level
19:

| | Size |
|---|---|
| a version, as stored | 3,403 KB |
| compressed on its own | 825 KB |
| delta against the previous version | 2.6 KB average, 4.9 KB worst |
| delta against a version 23 h older | 13.9 KB average, 30.1 KB worst |
| rebuilding a version from its delta | 7 ms |

Expected: that page's 1,440 versions go from 4.3 GB to about 16 MB.

**Prior art** (sources in the runcove-5t3oj.45 notes):
- git packs and Mercurial revlogs bound their delta chains with periodic full copies.
- Wikipedia's lesson is to name the stored format, so it can change later.
- Products (Notion, Confluence, Grafana, BookStack, S3) thin by age or count instead. That is
  what Jeremy ruled out.

## 2. Storage

New columns on `version` (Postgres `schema.sql`, plus a SQLite step):

| Column | Meaning |
|---|---|
| `packed` (bytea / blob) | the version, encoded per `codec` |
| `codec` (text) | `zstd1-full`, or `zstd1-delta` against `base_n`. The name carries the format version. |
| `base_n` (integer) | the version a delta is against: always `n - 1`. Null for a full copy. |
| `body_bytes` (integer) | the plain body's length in bytes, for `list_versions` |
| `body_sha256` (text) | the plain body's hash; every rebuild is checked against it |

`body` stays, and holds the plain text **for the current version only**. A non-current version
has `body = ''` and is read from `packed`. The current version is served from `body` exactly as
today: no decoding on the hot path, and the wall, shared links and the identical-republish check
are unchanged.

**Encoding**, with python-`zstandard` (a new `server` dependency):
- **Full copy:** zstd level 19, for version 1 and every `n` where `(n - 1) % 100 == 0`.
- **Delta:** zstd level 19 with the previous version's plain text as a raw-content prefix
  dictionary, and `window_log` large enough to cover the base (a body is at most 8 MiB, so 24).
  This is the mechanism behind `zstd --patch-from`.
- **Postgres:** `packed` is set to `storage external`, so TOAST doesn't compress compressed bytes
  again.

## 3. Writes

Every version's `packed` is computed **when that version is written**, not when it stops being
current. At that moment its base (the current version) is still plain text in `body`, so a write
never has to rebuild anything.

- **`publish`** (version 1): `packed` is a full copy, and `body` is plain.
- **`patch`** (version n+1): in the same transaction that inserts n+1 and moves
  `current_version`:
  - pack n+1: a delta against n's plain `body`, or a full copy on a keyframe;
  - set n's `body = ''`.

  That is an UPDATE of an existing row, not a new insert, so `patch`'s rule that a duplicate key
  means a race (store.py:714) still holds. The identical-republish no-op returns before any of
  this.
- **Write cost:** one zstd encode per publish, ~50–150 ms at level 19 for 3.4 MB (to measure). If
  that is too slow on the hot path, drop to level 9: deltas barely change size, because their
  size comes from the match, not the level.

## 4. Reads

A single accessor, `store.version_bodies(conn, artifact_id, ns) -> dict[int, str]`:
- returns the plain `body` for the current version;
- for an older version, walks back to the nearest full copy at or below it (at most 99 deltas),
  decodes forward, and checks each result against `body_sha256`. A mismatch raises, never returns
  wrong text;
- shares a walk when it is asked for several versions (pagediff asks for two).

**Callers that move to it:**
- `get_version_body`;
- the content server's `_load`, for `/v/N` and its diff base;
- the gallery's `_bodies`, for review-round diffs.

**Callers that only change their SQL:**
- `list_versions` reads `body_bytes` instead of `octet_length(body)`;
- the wall's correlated subquery joins on `current_version` instead of "highest live `n`". The
  two are the same today, because nothing writes `pending`.

**Callers that already read only the current version stay as they are:** `get_body`,
`duplicate`, the shared-link reads and the comment quote check.

**Worst case:** a version 99 steps from its full copy is ~99 × 7 ms ≈ 0.7 s. A small in-process
LRU of rebuilt full copies keeps repeated history browsing fast.

## 5. Moving existing versions

`artifacts-admin pack-versions` runs at start-up in a background thread, and on demand:
- page by page, in `n` order;
- packs every version that has no `packed` yet;
- verifies each one by rebuilding it and comparing to the original;
- only then blanks `body` on the non-current ones.

It is idempotent and resumable: it commits per page, and a crash leaves only rows whose `packed`
is either complete or absent. It logs a count and the bytes saved.

Space comes back only on `VACUUM`. SQLite's `VACUUM` rewrites the whole file, so it needs free
space about the size of the new database (small). It runs once, after the backfill, at the next
04:00 maintenance pass, and leaves the night's snapshot to run after it. The volume has 26 GB
free.

**The copier** (`migrate.py`) carries the new columns as it carries any column. A body of `''`
is valid. The per-row checksum covers `packed` as hex.

## 6. Tests

- **Round trip:** write 250 versions with small random edits, and read every one back exactly.
  Full copies land at 1, 101 and 201.
- **Corruption:** flip a byte in a delta, and reading that version or any later one in its chain
  raises; it never returns altered text.
- **Unchanged behaviour:**
  - `revision`, the identical-republish no-op and the wall's body all match the current
    behaviour;
  - `list_versions` reports plain sizes;
  - `/v/N`, diffs and review-round diffs serve the right text.
- **Backfill:** it converts a database written by today's code; run twice, the second run changes
  nothing; killed mid-page, the next run finishes it.
- **Both engines:** the suite runs on Postgres and SQLite, and the copier round-trips a packed
  database.
- **One measured check against production**, before blanking anything, on a copy of last night's
  snapshot: every version of every page rebuilds byte-for-byte.

## 7. Rollout

1. Ship the columns and writes, which pack new versions. Bodies stay as they are: reads fall
   back to `body` when it is non-empty, so this step loses nothing even if it is reverted.
2. Ship the backfill, run it first against a snapshot copy, then live; then `VACUUM`.
3. Check the result: database size, snapshot size, and a sample of old versions opened in the
   gallery.

A sizeable change to stored data, so it gets one fresh reviewer round before the push.

## 8. As built, steps 1 and 2 (26 Oct 2026)

Deviations from the sections above, and why:

- **Level 9, not 19** (`packing.LEVEL`). Measured on a 3.2 MB page of mixed text with ten small
  edits: a delta took 2.1 s at level 19 and 0.12 s at level 9, for 414 and 453 bytes; a full copy
  3.2 s against 0.25 s, for 807 KB against 859 KB. Section 3 allowed this fallback; 2 s on the
  publish path was too slow, and the encode runs before the write transaction opens, so no lock is
  held while it runs.
- **Backfill is not started automatically at start-up.** `artifacts-admin pack-versions` runs it on
  demand; `ARTIFACTS_PACK_VERSIONS=startup` runs it in a background thread. Section 7 wants the
  first run watched, on a snapshot copy, before it is live.
- **Commits.** The pack phase commits every 10 versions (`repack.BATCH`); the verify-and-blank
  phase is one transaction per page holding the page's row lock, so a patch cannot land between
  the check and the blanking.
- **A patch blanks its predecessor only if that predecessor is packed**, so a page the backfill
  has not reached keeps every body until it has.
- **Not built yet:** the nightly `VACUUM` after the backfill, and the production check against
  last night's snapshot (both belong to step 2's run, not its code).
- The in-process cache of rebuilt versions (48 MiB, by text length) is keyed by the plain text's
  hash, so an entry can never be the wrong text for its version.
