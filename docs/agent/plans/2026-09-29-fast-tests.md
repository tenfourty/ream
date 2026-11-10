# Fast tests and a fast deploy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The full test suite runs in under a minute on the management machine, and a push reaches the cluster in about five minutes. Today it takes 9m44s on the management machine, and 10 to 35 minutes from push to live.

**Architecture:**
- **Tests run in parallel** under pytest-xdist, and each worker gets its own Postgres database.
- **The schema is built once per worker.** Each test starts from emptied tables rather than a rebuilt schema.
- **The per-test web servers stop without idling.**
- **Tests stop sleeping on real clocks.**
- **In CI, the image is built alongside the tests**, and the deploy waits for both.

**Tech Stack:** pytest 9 plus pytest-xdist, psycopg 3, pgserver locally, a Postgres 18 service in CI, uvicorn, and Forgejo Actions.

**Spec:** homelab-ij8.114, Jeremy's confirmed decision "Aa Ba", 29 Sep 2026. It includes his SQLite question and the reason for staying on Postgres.

## Global Constraints

- **Stay on Postgres.** SQLite would test a different database from the one the service runs on: row locks, JSON and the waiting that wakes agents all behave differently.
- **Targets.**
  - `uv run pytest -q -n auto` on the management machine finishes in under 60 s wall-clock, measured with `time` on an otherwise quiet machine.
  - No single test takes more than 1.0 s, except those marked `@pytest.mark.slow` with a one-line reason. At most 10 tests may carry that mark.
- **Every test still gets a clean database.** It starts with every table empty and every sequence restarted, exactly as the old full reset gave it.
- **A test that changes the schema itself** rebuilds for the next test. That covers anything that calls `Database.reset()` or `migrate()` on a non-standard schema, or drops or alters objects. Grep `tests/test_schema.py`, `tests/test_agents_can_read.py` and `tests/walk_gallery.py`.
- **Run the same way everywhere.**
  - `uv run pytest -q` without `-n` must still work, serially, for a single file or test.
  - Parallel is opt-in on the command line (`-n auto`).
  - CI and the documented full-suite command use `-n auto`.
- **Locally,** each worker starts its own pgserver, or all workers share one server with a database per worker. Choose whichever measures faster.
- **In CI,** `PG_TEST_DSN` points at one Postgres service. Each worker creates its own database (`artifacts_test_<worker_id>`) from that DSN, and drops it first if it exists.
- **The CI deploy chain keeps its guarantees.**
  - `deploy` runs only when `test` has passed AND `image` has succeeded, on the same commit.
  - The `[skip ci]` guards stay in place.
  - The deploy job's tag bump and stand-down logic are unchanged.
  - An image built from a commit whose tests fail is never deployed. It may sit in the registry, which is acceptable because the registry is internal.
- **Commit rules:** conventional commits, and no Co-Authored-By or "Generated with" lines, because the repo guard refuses them. Stage explicit paths. Never use amend, stash or reset --hard.

## Review Focus

1. **Leftover state between tests.** A row, a sequence or a `human_pref` row survives from one test into the next and makes an order-dependent pass or fail. *Test (Task 1):* two tests in one module. The first writes rows into every table family (page, decision, comment, event, key, pref); the second asserts every table is empty and that the next id or sequence value is back at its start. Run them with `-p no:randomly` in both orders, via `-k`.
2. **A test that alters the schema poisons later tests on the same worker.** *Test (Task 1):* a schema-altering test followed in the same worker by an ordinary test that uses the altered table. It still passes.
3. **Parallel exposes shared global state.** Examples: module-level caches, `gallery._watchers`, `nudge` state, environment variables that monkeypatch fails to restore, or files under a fixed path. *Check (Task 1):* the full suite passes three runs in a row under `-n auto`, including one with `-p randomly` if that plugin is available. Otherwise use `--dist loadgroup` or `loadfile` only where needed.
4. **A wall-clock test that passes quietly and flips under CI load,** like the watcher test fixed on 29 Sep. *Check (Task 2):* the suite passes under `-n auto` while 12 busy-loop processes load the CPU.
5. **The CI change deploys an image whose tests failed, or never deploys.** *Check (Task 3):* the workflow's `needs:` graph, read by the reviewer, plus a live observation on the first push. Show that `deploy` waited for both jobs.

---

### Task 1: parallel workers, one database each, emptied tables between tests

**Files:**
- Modify: `pyproject.toml`. Add `pytest-xdist` to the test or dev extra, the same place pytest is declared. Run `uv lock`.
- Modify: `tests/conftest.py`, in the `dsn`, `db` and any schema fixtures.
- Modify: the tests that alter the schema, so they rebuild afterwards. Use a fixture or marker, for example `fresh_schema`, that performs a full `reset()` after the test.
- Modify: `.forgejo/workflows/ci.yml`. The tests step runs `uv run pytest -q -n auto`.
- Modify: `CLAUDE.md`, the HOW block. The full-suite command becomes `uv run pytest -q -n auto`.
- Test: create `tests/test_isolation.py`.

**Interfaces:**
- Produces:
  - `dsn` is per worker. It uses the `worker_id` fixture from xdist, which is `"master"` when run serially.
  - A session-scoped schema build for each worker.
  - The `db` fixture empties every table with one `TRUNCATE … RESTART IDENTITY CASCADE`. It builds the table list once per session from `pg_tables` in the current schema, and rebuilds that list after any `fresh_schema` rebuild.
  - The `fresh_schema` fixture or marker.

- [ ] **Step 1: Failing tests** in `tests/test_isolation.py`, for Review Focus 1 and 2 as written above. Also add `test_every_table_is_empty_at_the_start`, which counts rows in every table and expects 0.
- [ ] **Step 2: Run them** under the current fixtures and record the result. They may already pass with the full reset; that is fine, because they guard the new path.
- [ ] **Step 3: Implement.** Measure `time uv run pytest -q` before the change and `time uv run pytest -q -n auto` after it, and put both numbers in the report.
- [ ] **Step 4:** Run `uv run pytest -q -n auto` three times in a row, and once serially (`uv run pytest -q`). Every run must pass. Then `ruff format` and `ruff check`.
- [ ] **Step 5: Commit** `git commit -m "test: run the suite in parallel, one database per worker, emptied rather than rebuilt between tests"`.

### Task 2: no idling servers, no real waits

**Files:**
- Modify: `tests/test_client.py`, in `_LiveServer`. Its `stop()` must return in well under 100 ms. Start with uvicorn `Config(timeout_graceful_shutdown=0, timeout_keep_alive=…)`, or force-close connections; measure first to find where the ~2 s goes.
- Modify: the tests listed by `uv run pytest -q -n auto --durations=60` as taking more than 1.0 s. For each, remove real waiting:
  - shorten CLI `--timeout` values in tests to the minimum the code accepts;
  - monkeypatch module constants such as `decide._HOLD`, retry sleeps or ggscan timeouts to small values;
  - replace `time.sleep(n)` with an Event or a poll on the real condition. The watcher-test fix of 29 Sep is the model.
  - Production defaults do not change. Only tests shorten them.
- Modify: `pyproject.toml`, to register a `slow` marker.

**Interfaces:** none.

- [ ] **Step 1:** Record `--durations=60` before the change.
- [ ] **Step 2: Fix** the servers, then each slow test in turn. Where a test genuinely needs time, mark it `@pytest.mark.slow` with a reason. Stay within the budget in the Global Constraints.
- [ ] **Step 3: Measure.**
  - `time uv run pytest -q -n auto` must come in under 60 s.
  - `--durations=15` must show nothing over 1.0 s except tests marked slow.
  - Run the suite once under load, with 12 `python3 -c "while True: pass"` processes. Kill them by PID afterwards and never with `pkill -f`. It must pass.
- [ ] **Step 4:** Run `ruff format` and `ruff check`.
- [ ] **Step 5: Commit** `git commit -m "test: no test idles on a server shutdown or a real clock, so the suite fits in a minute"`.

### Task 3: build the image while the tests run

**Files:**
- Modify: `.forgejo/workflows/ci.yml`.
  - Remove `needs: test` from `image`, so it starts at once.
  - Change `deploy` to `needs: [test, image]`.
  - Keep every `if:` guard as it is.
  - Add a uv cache to the test job if Forgejo's `actions/cache` or a `setup-uv` equivalent works on this runner. Check `docs/services/forge/readme.md` in `~/repos/homelab` for what the runner supports. If there is no reliable cache, skip it and say so.
- Modify: `docs/agent/implementation-plan.md` or the repo docs if they describe the pipeline order. Grep for `needs: test` or "image job".

**Interfaces:** none.

- [ ] **Step 1:** Make the change and validate the YAML: `python3 -c "import yaml,sys; yaml.safe_load(open('.forgejo/workflows/ci.yml'))"`, or `uv run python` with PyYAML if it is available.
- [ ] **Step 2:** Run the guard selftests (`scripts/guards/run-selftests.sh --no-lefthook`), because CI runs them.
- [ ] **Step 3: Commit** `git commit -m "ci: build the image while the tests run; deploy waits for both"`.

The controller merges and pushes. The live proof is the first push after merge:
- read the CI run times from the forge;
- `test` and `image` overlap in time;
- `deploy` starts only after both have finished;
- the new image reaches the Deployment;
- push-to-live time is recorded on ij8.114.

## Self-review

- **Coverage of ij8.114:**
  - Postgres, built once and emptied per test, with one database per worker: Task 1.
  - No idling servers and no real waits: Task 2.
  - The image built alongside the tests: Task 3.
  - Targets: measured in Tasks 1 and 2 and by the live proof.
  - The runner's size belongs to ops. If CI stays slow once the suite is fast, the numbers go to them.
- **Placeholders:** the conftest and server code is specified by behaviour and tests rather than written out, because the implementer must measure first: pgserver per worker versus shared, and where uvicorn's shutdown time goes.
- **Names:** the `fresh_schema` fixture, the `slow` marker, `artifacts_test_<worker_id>` and `-n auto` are used consistently across the tasks.
