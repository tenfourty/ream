# Reading a CI run's log without a browser

Through the forge API, as yourself (`FORGE_AS=<you> forge tea api …`):

1. `/repos/runcove/artifacts/actions/runs?limit=N&page=1` gives the run's `id`. Always send
   `page=1`: Forgejo ignores `limit` without it and loads every run, a heavy query on the forge's
   database.
2. `/repos/runcove/artifacts/actions/runs/<id>/jobs` gives the job's `id`.
3. `/repos/runcove/artifacts/actions/jobs/<job id>/logs` gives the log.

Grep out only the lines you need. The tests mint throwaway keys, and a log carries their
`Bearer art_…` lines.

A superseded run still holds the runner. Cancel it before pushing again to the same branch:
`forge tea api -X POST /repos/runcove/artifacts/actions/runs/<id>/cancel`.

## Proving the browser tests ran

Green alone does not say so: they skip without a browser. CI installs chromium (step "install
chromium") and sets `REAM_REQUIRE_BROWSER=1`, so a missing browser fails the run at start.
The "browser tests" step, in the `test-sqlite` job (the Postgres suite runs in `test`), runs with `-v`, so each test has its own line: grep the log for `chromium --version`,
then count `test_panel_browser.*PASSED` (and `test_shim_marks`, `test_thumbs`,
`test_thumbnail_wall`); none of them may appear in the `SKIPPED` summary.
