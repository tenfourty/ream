# Runbook: artifacts on SQLite: cutover, rollback, backups and the restore drill

The design and the reasons for each step are in `docs/agent/specs/2026-10-09-sqlite-design.md`
(§2 for backups, §5 for the migration). This file holds the commands. The deployment's own names
(the cluster, the Flux kustomization, the backup share) live in its own deployment repo; the names
below are the ones its manifests used: namespace `artifacts`, Deployment `artifacts`, the CNPG
secret `artifacts-db-app` (key `uri`), and the PVC `artifacts-data` mounted at `/data` (30Gi since
20 Oct 2026; see "Sizing" below).

## The commands

| Command | What it does |
|---|---|
| `artifacts-admin migrate-from-postgres --from <pg DSN> --to <sqlite path>` | The cutover copy |
| `artifacts-admin migrate-to-postgres --from <sqlite path> --to <pg DSN>` | The rollback copy |
| `artifacts-admin restore-drill --snapshot <file>` | Integrity check, restore into a scratch file, verify, print row counts |

Both migrate commands do the same thing in opposite directions (`src/ream/migrate.py`):

- bring the destination's schema up to date, then **refuse unless every table is empty**;
- refuse unless both schemas have the same tables and columns (so a new table cannot be skipped);
- copy every table in foreign-key order, carry the event counter over, then **verify inside the
  destination's transaction**: per table the row count and a SHA-256 over each row's canonical
  JSON (spec §5). Any difference exits non-zero with `REFUSED: table <name>: …`, naming the rows
  and columns that differ (never their values), and the destination is rolled back, empty.

A Postgres DSN may be given as `env:NAME`, which reads it from that environment variable. Use that
form: a DSN carries a password, and a password must never be a process argument. The commands
print DSNs with the password replaced by `***`.

The source must not change during a copy (it is read twice, to copy and to verify): the app is
always scaled to 0 first.

## The one-off Job

With the pod stopped, `artifacts-admin` runs as a one-off Job mounting the PVC (spec §2, "Admin
commands"). Take the image the Deployment runs now:

```bash
IMAGE=$(kubectl -n artifacts get deploy artifacts -o jsonpath='{.spec.template.spec.containers[0].image}')
```

and a Job like this one, changing only `args` per step:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: artifacts-migrate          # a new name per run, or delete the finished one first
  namespace: artifacts
spec:
  backoffLimit: 0                  # a refusal must stay a refusal, never a retry
  template:
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: admin
          image: IMAGE             # the value of $IMAGE above
          command: ["artifacts-admin"]
          args: ["migrate-from-postgres", "--from", "env:REAM_PG_URI",
                 "--to", "/data/artifacts.db"]
          env:
            - name: REAM_PG_URI
              valueFrom:
                secretKeyRef: {name: artifacts-db-app, key: uri}
            - name: SQLITE_TMPDIR
              value: /data/tmp
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
          volumeMounts:
            - {name: data, mountPath: /data}
      volumes:
        - name: data
          persistentVolumeClaim: {claimName: artifacts-data}
```

Run and read it:

```bash
kubectl apply -f artifacts-migrate.yaml
kubectl -n artifacts wait --for=condition=complete --timeout=30m job/artifacts-migrate
kubectl -n artifacts logs job/artifacts-migrate      # read it whether or not the wait succeeded
```

Success ends with `verified <N> tables, <M> rows`, one `verified <table>: <rows> rows, sha256 …`
line per table before it, and the Job `Complete`. Anything else (`REFUSED: …`, a traceback, the
Job `Failed`) means stop: the destination holds nothing, and the old database is untouched.

## Rehearsal (before the window)

Restore last night's pg_dump into a scratch Postgres database, then run the same copy into a
scratch file, from any machine with the repo installed:

```bash
createdb artifacts_rehearsal && pg_restore -d artifacts_rehearsal --no-owner <artifacts-db-*.dump>
export REHEARSAL_PG_URI=postgresql://…/artifacts_rehearsal   # set it without echoing a password
artifacts-admin migrate-from-postgres --from env:REHEARSAL_PG_URI --to /tmp/rehearsal.db
artifacts-admin restore-drill --snapshot /tmp/rehearsal.db   # and it restores, as a snapshot would
```

It must end in `verified …`. Note how long it took: that is the window's copy time.

**Measured, 16 Oct 2026** (a deployment's one-off Job, run 8, on a dump of the live database: 1.3 GB,
22 tables, 4,206 rows, `version` 2,654 rows): Postgres to SQLite, the restore-drill, and SQLite
back to Postgres took **285 s** in all, and every table's hash after the round trip equalled the
first copy's. The dump took 2 m 42 s and its restore 1 m 21 s. So plan a cutover copy of a few
minutes, not an hour. The copier fetches 8 rows at a time (`migrate._BATCH`): at 500 rows the
`version` table (bodies up to 8 MiB) was OOM-killed at 768 Mi, so give the Job at least 1 Gi and
a work volume of 10 Gi or more (the copy, the drill's own copy and its journal).

## Cutover (spec §5): done 20 Oct 2026, through the deployment repo

On that deployment, changes reach the cluster only through its manifests repo, never by a hand-run
`kubectl` or a Flux suspend. So the cutover was three merges, each checked before the next.
Announce the window to every agent about 30 minutes ahead.

1. **Scale to 0** (a manifest PR: `replicas: 0`). Wait until
   `kubectl -n artifacts get pods -l app.kubernetes.io/name=artifacts` shows none. The copy must
   not start while the app can still write: a write mid-copy makes the verify refuse (safe), but
   one after the verify and before the pod stops is lost silently. RWO does not prevent a second
   mount on the same node, so the Job goes in its own PR, merged only once no pod is left.
2. **The copy Job** (a manifest PR adding the Job above, the Deployment's image digest, a 1Gi
   memory limit, `SQLITE_TMPDIR=/data`, `backoffLimit: 0`). Its log must end
   `verified 22 tables, <M> rows`. **Save the log**: the next PR removes the Job, and Flux's prune
   deletes it with its log.
3. **The switch** (a manifest PR: `DATABASE_URL: sqlite:////data/artifacts.db`, `replicas: 1`, the
   Job removed). If step 2 refused, this PR is only `replicas: 1` and the Job removed: the app
   comes back on Postgres as before.
4. Smoke test: the page list matches the one taken before step 1 (same ids); publish a page, fetch
   it, update it, list its versions; ask a decision on it and have a person tap it while an agent's
   `artifacts-decide wait --page <id>` waits.
5. Force one off-site sync, then restore the newest off-site snapshot and run `restore-drill` on it
   beside a `VACUUM INTO` copy of the live file (the deployment's own off-site restore-test script).
   Removing Postgres waits on this passing.

The 20 Oct off-site restore test passed: the newest off-site snapshot (07:44 UTC) restored,
passed the integrity check, and held 22 tables and 4,664 rows, every table at or below the live
count. Two things to know when running it: compare against the copy Job's counts as a floor, not
exactly (the app writes between the copy and the snapshot), and point `TMPDIR` at disk, because
`/tmp` on the management machine lives in memory and a 3.2 GB restore plus its copy exceeds it.

**Measured, 20 Oct 2026**: the window took 10 minutes from the first merge to the app ready on
SQLite (09:29 to 09:39 Paris); the copy itself 3.5 minutes, `verified 22 tables, 4646 rows`
(`version` 3,017 rows).

## Sizing

The SQLite file is much larger than Postgres reports, because Postgres stores the page bodies
TOAST-compressed and SQLite stores them raw. On 20 Oct 2026 Postgres reported `version` at
1.5 GB; the SQLite file was **3.23 GB**. The 16 Oct rehearsal's "1.3 GB" was the compressed dump,
not the database. On the volume: the live file, its WAL, a `VACUUM INTO` temporary during each
nightly snapshot, and the kept snapshots (`maintenance.KEEP`, uncompressed), so about
(KEEP + 2) times the file. With seven kept that came to 23 GB of snapshots beside a 4.7 GB file,
and the volume reached 95% on 25 Oct 2026. Since 26 Oct 2026 the volume keeps two (the owner's
choice); the off-site copy and the on-site backup copy hold the history. Snapshots stay uncompressed so the
off-site restic copy deduplicates them night to night.

**Versions stored as deltas (26 Oct 2026, spec `2026-10-26-delta-versions-design.md`).** Only the
current version of a page keeps its plain text in `version.body`; every older one lives in
`version.packed` as zstd, a full copy at version 1 and every hundredth after, a delta against the
previous version in between (about 2.6 KB for an hourly board against 3.4 MB plain). New versions
pack as they are written. Versions written earlier are packed by `artifacts-admin pack-versions`
(or at start-up with `REAM_PACK_VERSIONS=startup`): it verifies every version by rebuilding
it before blanking its old body, is safe to re-run, and prints counts and bytes saved. The file
shrinks only on `VACUUM`, which rewrites the whole file and needs free space about the size of
the new database; run it once after the backfill, not before a snapshot has been taken. Run the
backfill first on a copy of a snapshot (`DATABASE_URL=sqlite:////tmp/copy.db`).

## Rollback (for one week after the cutover)

CNPG keeps running through the week, backed up by its pg_dump CronJob, with its egress
NetworkPolicy. The rollback copies SQLite back, so writes made on SQLite are kept.

1. `flux suspend ks <the app's kustomization>`; scale the Deployment to 0 and wait for no pod (cutover step 2).
2. Empty the Postgres database without destroying it: the copy refuses a non-empty destination,
   and the pre-cutover rows are stale by now. Rename the old schema aside, in `psql` on the app
   database as its owner (the user in the `uri`):

   ```sql
   alter schema public rename to pre_rollback_20261015;   -- today's date
   create schema public;
   ```

   (Its rows stay readable under the old name; dropping that schema later is a separate, asked-for
   step.)
3. Run the Job with `args: ["migrate-to-postgres", "--from", "/data/artifacts.db", "--to",
   "env:REAM_PG_URI"]`; check it ends in `verified …`.
4. Point `DATABASE_URL` back at the CNPG secret (revert the step-4 change), and remove the PVC
   mount only if the change says so; the SQLite file stays on the PVC.
5. `flux resume ks <the app's kustomization>`, and smoke test as in cutover step 6.

## Backups

The app writes one snapshot a night at 04:00 UTC (`VACUUM INTO`), then purges; at start-up it takes
one at once if the newest is over 24 h old. Snapshots are `/data/backups/artifacts-sqlite-<UTC
date>.db`, uncompressed, two kept. The deployment's CronJob copies new ones to its backup share. List
them from the running pod:

```bash
kubectl -n artifacts exec deploy/artifacts -- ls -l /data/backups
```

## Restore drill

Proves the newest snapshot is a database that restores, without touching the live file. It needs
no downtime: run it in the running pod,

```bash
NEWEST=$(kubectl -n artifacts exec deploy/artifacts -- sh -c 'ls -1 /data/backups/artifacts-sqlite-*.db | tail -1')
kubectl -n artifacts exec deploy/artifacts -- artifacts-admin restore-drill --snapshot "$NEWEST"
```

or, with the pod stopped, as the one-off Job with `args: ["restore-drill", "--snapshot",
"/data/backups/artifacts-sqlite-<date>.db"]`. It prints `integrity_check ok`, one `<table>
<rows>` line per table, and `restored and verified <N> tables, <M> rows`; the scratch copy (under
`SQLITE_TMPDIR`, which must be set: the root filesystem is read-only) is removed afterwards. Compare the counts with the live ones you expect (pages,
decisions, comments): a snapshot from last night holds roughly today's numbers.

## A real restore (a corrupt or lost database)

1. `flux suspend ks <the app's kustomization>`; scale to 0 and wait for no pod.
2. Run the restore drill on the chosen snapshot first (Job form); it must pass.
3. In a Job (or a debug pod) mounting the PVC: move `/data/artifacts.db`, `-wal` and `-shm` aside
   to `/data/broken-<date>/` (never delete them), then copy the snapshot to `/data/artifacts.db`.
4. `flux resume ks <the app's kustomization>`; smoke test. Writes since the snapshot's night are lost: say so
   to the agents, with the snapshot's date.
