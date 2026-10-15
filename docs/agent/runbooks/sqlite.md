# Runbook: artifacts on SQLite: cutover, rollback, backups and the restore drill

The design and the reasons for each step are in `docs/agent/specs/2026-10-09-sqlite-design.md`
(§2 for backups, §5 for the migration). This file holds the commands. The deployment's own names
(the cluster, the Flux kustomization, the backup share) live in the homelab repo; the names below
are the ones this repo's `deploy/` uses: namespace `artifacts`, Deployment `artifacts`, the CNPG
secret `artifacts-db-app` (key `uri`), and the 5Gi PVC `artifacts-data` mounted at `/data`.

## The commands

| Command | What it does |
|---|---|
| `artifacts-admin migrate-from-postgres --from <pg DSN> --to <sqlite path>` | The cutover copy |
| `artifacts-admin migrate-to-postgres --from <sqlite path> --to <pg DSN>` | The rollback copy |
| `artifacts-admin restore-drill --snapshot <file>` | Integrity check, restore into a scratch file, verify, print row counts |

Both migrate commands do the same thing in opposite directions (`src/artifacts/migrate.py`):

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
          args: ["migrate-from-postgres", "--from", "env:ARTIFACTS_PG_URI",
                 "--to", "/data/artifacts.db"]
          env:
            - name: ARTIFACTS_PG_URI
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

## Cutover (spec §5, an evening announced in #agents)

1. `flux suspend ks artifacts-app`, so Flux does not undo step 2 within five minutes.
2. `kubectl -n artifacts scale deploy/artifacts --replicas=0`, and wait until no pod is left:
   `kubectl -n artifacts get pods -l app.kubernetes.io/name=artifacts` shows none.
3. Run the Job above with `migrate-from-postgres --from env:ARTIFACTS_PG_URI --to
   /data/artifacts.db`, and check its log ends in `verified …`. Keep the log.
4. Apply the SQLite `DATABASE_URL` (`sqlite:////data/artifacts.db`, in `deploy/artifacts-config.env`)
   and the PVC mount: the change already merged, waiting behind the suspension.
5. `flux resume ks artifacts-app`; the pod starts on SQLite.
6. Smoke test end to end: publish a page, fetch it, ask a decision and answer it in the gallery,
   leave a comment, and watch an agent's `artifacts-decide wait` wake on the answer.

If step 3 refuses: resume Flux without step 4, and the app comes back on Postgres as before.

## Rollback (for one week after the cutover)

CNPG keeps running through the week, backed up by its pg_dump CronJob, with its egress
NetworkPolicy. The rollback copies SQLite back, so writes made on SQLite are kept.

1. `flux suspend ks artifacts-app`; scale the Deployment to 0 and wait for no pod (cutover step 2).
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
   "env:ARTIFACTS_PG_URI"]`; check it ends in `verified …`.
4. Point `DATABASE_URL` back at the CNPG secret (revert the step-4 change), and remove the PVC
   mount only if the change says so; the SQLite file stays on the PVC.
5. `flux resume ks artifacts-app`, and smoke test as in cutover step 6.

## Backups

The app writes one snapshot a night at 04:00 UTC (`VACUUM INTO`), then purges; at start-up it takes
one at once if the newest is over 24 h old. Snapshots are `/data/backups/artifacts-sqlite-<UTC
date>.db`, uncompressed, seven kept. Homelab's CronJob copies new ones to the backup share. List
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

1. `flux suspend ks artifacts-app`; scale to 0 and wait for no pod.
2. Run the restore drill on the chosen snapshot first (Job form); it must pass.
3. In a Job (or a debug pod) mounting the PVC: move `/data/artifacts.db`, `-wal` and `-shm` aside
   to `/data/broken-<date>/` (never delete them), then copy the snapshot to `/data/artifacts.db`.
4. `flux resume ks artifacts-app`; smoke test. Writes since the snapshot's night are lost: say so
   in #agents, with the snapshot's date.
