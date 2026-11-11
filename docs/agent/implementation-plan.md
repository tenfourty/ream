# Artifacts publisher — implementation plan (part 1: it publishes, it serves, the decisions page moves)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any agent with an API key can publish an HTML page and get back a stable URL that only
Jeremy can open, and `scripts/ops-decisions-page.sh` publishes there instead of claude.ai.

**Architecture:** One Python service, two ASGI apps routed by `Host`. The **gallery**
(`artifacts.tenfourty.site`) owns every write route, the human UI and the agent API. The **content
server** (`<id>.tenfourty.com`) serves page bodies framed-only, with the security header set, and
injects the runtime shim on the current version alone. Postgres holds artifacts, versions, scoped
agent keys and page state; a PVC holds bodies and files. No agent-authored byte is ever rendered as
markup by the gallery.

**Tech Stack:** Python 3.12 (`uv`), FastAPI + uvicorn, psycopg 3 (plain SQL, no ORM), pytest with
`pgserver` (a real ephemeral Postgres from a wheel — no container, no root; verified working on
the management machine, 19 Sep), ruff, httpx.

**Spec:** `docs/plans/2026-09-19-agent-artifacts-design.md` (v3.1). Executors read both.

**Repository:** this code does **not** live in the homelab repo, which owns only wiring. Work in a
fresh local git repo at `~/repos/artifacts`. Pushing it to the forge and building an image are
gated on Jeremy and are not in this plan. The homelab repo gains only
`k8s/apps/artifacts/` in a later part.

## Global Constraints

- **Python 3.12** exactly (`uv python install 3.12`); `pgserver` has no wheels for 3.13+.
- **No containers, no podman, no root** in any test or dev step.
- **Never read or print a credential.** Tests use fixture values that are obvious fakes.
- **Artifact ids are one DNS label**, lowercase base32, 26 chars, so `<id>.tenfourty.com` stays one
  level deep and inside Cloudflare's free certificate.
- **The gallery renders agent-authored values as escaped text only** — never as markup, never as
  bytes served from its origin.
- **Every agent API route is scoped** to artifacts created by the calling key's agent.
- **Visibility changes and share tokens are human-only** — they exist in no agent route.
- **Header set on every content response** (spec §5.5), including `default-src 'none'`,
  `worker-src 'none'`, `form-action 'none'`, `base-uri 'none'`, `frame-ancestors
  https://artifacts.tenfourty.site`.
- **Commit after every task** with a conventional-commit subject naming the outcome.

---

### Task 1: Project skeleton that runs and is tested

**Files:**
- Create: `~/repos/artifacts/pyproject.toml`, `src/artifacts/__init__.py`,
  `src/artifacts/app.py`, `tests/test_health.py`, `.gitignore`, `README.md`
- Test: `tests/test_health.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `artifacts.app.create_gallery_app() -> FastAPI` with `GET /healthz` returning
  `{"status": "ok"}`.

- [ ] **Step 1: Create the repo and pin the toolchain**

```bash
mkdir -p ~/repos/artifacts && cd ~/repos/artifacts && git init -q
uv python install 3.12
uv init --python 3.12 --no-workspace --name artifacts --lib >/dev/null 2>&1 || true
uv add fastapi "psycopg[binary]" uvicorn
uv add --dev pytest pytest-asyncio httpx ruff pgserver
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_health.py
from fastapi.testclient import TestClient
from artifacts.app import create_gallery_app


def test_healthz_reports_ok():
    client = TestClient(create_gallery_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/test_health.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'artifacts.app'`

- [ ] **Step 4: Write the minimal implementation**

```python
# src/artifacts/app.py
from fastapi import FastAPI


def create_gallery_app() -> FastAPI:
    app = FastAPI(title="artifacts gallery", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
```

- [ ] **Step 5: Run it and watch it pass**

Run: `uv run pytest -v` · Expected: 1 passed.
Run: `uv run ruff check src tests` · Expected: no findings.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: the service starts and answers a health check"
```

---

### Task 2: Storage that survives a restart

**Files:**
- Create: `src/artifacts/db.py`, `src/artifacts/schema.sql`, `tests/conftest.py`,
  `tests/test_schema.py`
- Test: `tests/test_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `artifacts.db.Database(dsn: str)` with `.connect()` (context manager yielding a
  `psycopg.Connection`), `.migrate()` (idempotent), `.reset()` (drop and recreate the schema; the
  test fixture uses it), and module constant `SCHEMA_VERSION: int = 1`.
  Tables: `agent_key`, `artifact`, `version`, `file`, `state`.

- [ ] **Step 1: Write the shared Postgres fixture**

```python
# tests/conftest.py
import os
import tempfile
import pytest
import pgserver
from artifacts.db import Database


@pytest.fixture(scope="session")
def dsn() -> str:
    """A real Postgres for the test session: an existing one via PG_TEST_DSN, else ephemeral."""
    existing = os.environ.get("PG_TEST_DSN")
    if existing:
        yield existing
        return
    server = pgserver.get_server(tempfile.mkdtemp(prefix="artifacts-pgtest-"))
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture()
def db(dsn: str) -> Database:
    database = Database(dsn)
    database.reset()          # drops and recreates the schema; tests never share state
    return database
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_schema.py
def test_migrate_creates_every_table(db):
    with db.connect() as conn:
        rows = conn.execute(
            "select table_name from information_schema.tables where table_schema = 'public'"
        ).fetchall()
    names = {row[0] for row in rows}
    assert {"agent_key", "artifact", "version", "file", "state"} <= names


def test_migrate_is_idempotent(db):
    db.migrate()
    db.migrate()
    with db.connect() as conn:
        count = conn.execute("select count(*) from artifact").fetchone()[0]
    assert count == 0
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/test_schema.py -v` · Expected: FAIL, no module `artifacts.db`.

- [ ] **Step 4: Write the schema**

```sql
-- src/artifacts/schema.sql
create table if not exists agent_key (
    id           text primary key,
    agent        text not null,
    token_sha256 text not null unique,
    created_at   timestamptz not null default now(),
    revoked_at   timestamptz
);

create table if not exists artifact (
    id              text primary key,
    title           text not null,
    agent           text not null,  -- the owning agent's SEAT name. No foreign key to agent_key,
                                    -- deliberately: a key can be revoked and reissued while its
                                    -- artifacts remain, so re-minting for the same seat keeps the
                                    -- whole history. Do not "tidy" this into a real FK, and mint
                                    -- a replacement key for the same name -- a new seat name is a
                                    -- new world with an empty listing, which is how the pages we
                                    -- are replacing got duplicated after an account rotation.
    visibility      text not null default 'private'
        check (visibility in ('private', 'link', 'public')),
    current_version integer,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create table if not exists version (
    artifact_id text not null references artifact(id) on delete cascade,
    n           integer not null,
    body        text not null,
    revision    text not null,
    status      text not null default 'live' check (status in ('live', 'pending')),
    created_by  text not null,
    created_at  timestamptz not null default now(),
    primary key (artifact_id, n)
);

create table if not exists file (
    artifact_id  text not null,
    n            integer not null,
    path         text not null,
    content_type text not null,
    bytes        bytea not null,
    primary key (artifact_id, n, path),
    -- a file belongs to a VERSION, not merely to an artifact: without this, a file row can name
    -- a version number that has never existed. Deleting the artifact still cascades, through
    -- version.
    foreign key (artifact_id, n) references version(artifact_id, n) on delete cascade
);

create table if not exists state (
    artifact_id text not null references artifact(id) on delete cascade,
    path        text not null,
    doc         jsonb not null,
    updated_at  timestamptz not null default now(),
    primary key (artifact_id, path)
);

-- every agent route filters by the calling key's agent, so that filter gets an index.
create index if not exists artifact_agent_idx on artifact (agent);

-- artifact.current_version should point at a real version row, but the FK can only be added
-- after `version` exists (created below `artifact` above), and Postgres has no
-- ADD CONSTRAINT IF NOT EXISTS, so guard it explicitly for migrate()'s idempotency. MATCH SIMPLE
-- (the default) leaves current_version unchecked while it is NULL, which is required before the
-- first version is written -- see publish() in Task 5, which inserts the artifact with a null
-- current_version, writes version 1, and only then points the artifact at it.
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'artifact_current_version_fkey'
    ) then
        alter table artifact
            add constraint artifact_current_version_fkey
            foreign key (id, current_version) references version(artifact_id, n)
            on delete set null;
    end if;
end $$;
```

- [ ] **Step 5: Write the database module**

```python
# src/artifacts/db.py
from contextlib import contextmanager
from importlib import resources
from typing import Iterator
import psycopg

SCHEMA_VERSION = 1


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            yield conn

    def migrate(self) -> None:
        sql = resources.files("artifacts").joinpath("schema.sql").read_text()
        with self.connect() as conn:
            conn.execute(sql)

    def reset(self) -> None:
        with self.connect() as conn:
            conn.execute("drop schema public cascade; create schema public;")
        self.migrate()
```

Add to `pyproject.toml` so the SQL ships with the package:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/artifacts"]
[tool.hatch.build]
include = ["src/artifacts/*.sql"]
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_schema.py -v` · Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "feat: artifacts, versions, keys and page state have a schema"
```

---

### Task 3: Agent keys that are scoped and never stored in the clear

**Files:**
- Create: `src/artifacts/keys.py`, `tests/test_keys.py`
- Test: `tests/test_keys.py`

**Interfaces:**
- Consumes: `artifacts.db.Database`.
- Produces: `artifacts.keys.mint(db, agent: str) -> tuple[str, str]` returning
  `(token, key_id)`; `artifacts.keys.verify(db, token: str) -> AgentKey | None`;
  dataclass `AgentKey(id: str, agent: str)`; `artifacts.keys.revoke(db, key_id: str) -> None`.
  Token format: `art_` + 43 chars of `secrets.token_urlsafe(32)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_keys.py
from artifacts import keys


def test_minted_token_verifies_and_carries_its_agent(db):
    token, _ = keys.mint(db, agent="ops-seat")
    assert token.startswith("art_")
    verified = keys.verify(db, token)
    assert verified is not None and verified.agent == "ops-seat"


def test_plaintext_token_is_never_stored(db):
    token, _ = keys.mint(db, agent="ops-seat")
    with db.connect() as conn:
        stored = conn.execute("select token_sha256 from agent_key").fetchall()
    assert all(token not in row[0] for row in stored)


def test_revoked_token_stops_verifying(db):
    token, key_id = keys.mint(db, agent="ops-seat")
    keys.revoke(db, key_id)
    assert keys.verify(db, token) is None


def test_unknown_and_malformed_tokens_return_none(db):
    assert keys.verify(db, "art_not-a-real-token") is None
    assert keys.verify(db, "") is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_keys.py -v` · Expected: FAIL, no module `artifacts.keys`.

- [ ] **Step 3: Implement**

```python
# src/artifacts/keys.py
import hashlib
import secrets
from dataclasses import dataclass
from artifacts.db import Database


@dataclass(frozen=True)
class AgentKey:
    id: str
    agent: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint(db: Database, agent: str) -> tuple[str, str]:
    token = "art_" + secrets.token_urlsafe(32)
    key_id = secrets.token_hex(8)
    with db.connect() as conn:
        conn.execute(
            "insert into agent_key (id, agent, token_sha256) values (%s, %s, %s)",
            (key_id, agent, _hash(token)),
        )
    return token, key_id


def verify(db: Database, token: str) -> AgentKey | None:
    if not token.startswith("art_"):
        return None
    with db.connect() as conn:
        row = conn.execute(
            "select id, agent from agent_key where token_sha256 = %s and revoked_at is null",
            (_hash(token),),
        ).fetchone()
    return AgentKey(id=row[0], agent=row[1]) if row else None


def revoke(db: Database, key_id: str) -> None:
    with db.connect() as conn:
        conn.execute("update agent_key set revoked_at = now() where id = %s", (key_id,))
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_keys.py -v` · Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: agent keys are hashed at rest and can be revoked"
```

---

### Task 4: The publish gate refuses a body carrying a secret

**Files:**
- Create: `src/artifacts/scan.py`, `src/artifacts/secret_shapes.txt`, `tests/test_scan.py`
- Test: `tests/test_scan.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `artifacts.scan.scan(text: str) -> list[Finding]`, dataclass
  `Finding(shape: str, line: int)`; `artifacts.scan.SHAPES: list[tuple[str, re.Pattern]]`.

**Context:** the lab's own shapes live in `~/repos/homelab/scripts/log-secret-shapes.txt`. Copy
that file into this repo (it is patterns, not secrets) and keep the copy honest with a test that
asserts a known set of shape names is present. ggshield is the authoritative gate but runs in the
deploy pipeline with Jeremy's token; this local scanner is the one that always runs and the only
one used on feed responses and state reads (spec D12, D15).

- [ ] **Step 1: Copy the shapes file**

```bash
cp ~/repos/homelab/scripts/log-secret-shapes.txt src/artifacts/secret_shapes.txt
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_scan.py
from artifacts import scan

FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"
FAKE_PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----"


def test_clean_body_has_no_findings():
    assert scan.scan("<!doctype html><title>Board</title><p>42 issues open</p>") == []


def test_aws_key_shape_is_found_with_its_line():
    findings = scan.scan(f"line one\nconst k = '{FAKE_AWS}'\n")
    assert findings and findings[0].line == 2


def test_private_key_header_is_found():
    assert scan.scan(FAKE_PRIVATE_KEY) != []


def test_shapes_file_still_carries_the_shapes_we_rely_on():
    names = {name for name, _ in scan.SHAPES}
    assert {"aws_access_key_id", "private_key_header"} <= names
```

- [ ] **Step 3: Run them and watch them fail**

Run: `uv run pytest tests/test_scan.py -v` · Expected: FAIL, no module `artifacts.scan`.

- [ ] **Step 4: Implement**

```python
# src/artifacts/scan.py
import re
from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class Finding:
    shape: str
    line: int


def _load_shapes() -> list[tuple[str, re.Pattern]]:
    text = resources.files("artifacts").joinpath("secret_shapes.txt").read_text()
    shapes: list[tuple[str, re.Pattern]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, pattern = line.partition("\t")
        if not pattern:
            name, _, pattern = line.partition(" ")
        shapes.append((name.strip(), re.compile(pattern.strip())))
    return shapes


SHAPES = _load_shapes()


def scan(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in SHAPES:
            if pattern.search(line):
                findings.append(Finding(shape=name, line=number))
    return findings
```

If the copied shapes file does not carry `aws_access_key_id` or `private_key_header`, add those two
lines to it (name, a tab, then the pattern) rather than weakening the test:

```
aws_access_key_id	AKIA[0-9A-Z]{16}
private_key_header	-----BEGIN [A-Z ]*PRIVATE KEY-----
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_scan.py -v` · Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: a body carrying a secret shape is caught before it is published"
```

---

### Task 5: Publishing a page returns a URL that keeps working

**Files:**
- Create: `src/artifacts/ids.py`, `src/artifacts/store.py`, `src/artifacts/api.py`,
  `tests/test_publish.py`
- Modify: `src/artifacts/app.py` (mount the agent API)
- Test: `tests/test_publish.py`

**Interfaces:**
- Consumes: `Database`, `keys.verify`, `scan.scan`.
- Produces: `artifacts.ids.new_id() -> str` (26 lowercase base32 chars);
  `artifacts.store.publish(db, agent, title, body) -> Published`;
  `artifacts.store.patch(db, agent, artifact_id, body, expected_revision) -> Published`;
  dataclass `Published(id: str, version: int, revision: str, url: str)`;
  `artifacts.store.get_owned(db, agent, artifact_id) -> ArtifactRow` (raises `NotFound`);
  `artifacts.store.list_for_agent(db, agent) -> list[ArtifactRow]`;
  exceptions `store.SecretFound`, `store.RevisionMismatch`, `store.NotFound`,
  `store.BodyTooLarge` (the size caps; mapped to 413). There is deliberately no `Forbidden` and no
  403: another agent's artifact must be indistinguishable from one that never existed, so cross-agent
  access is `NotFound` and 404.
  Content base host comes from `ARTIFACTS_CONTENT_DOMAIN` (default `tenfourty.com`).

**Additions from the scanner's review (binding; they override the code below where they differ):**

- **Body size cap, before scanning.** `MAX_BODY_BYTES = 512 * 1024`. A `body` whose UTF-8
  encoding exceeds it is refused with **413** before `scan.scan` runs, on both create and patch.
  The scanner reads two views of the body and holds the GIL while it does, so an uncapped body is a
  way to stall the process. Measured worst clean body at 1 MiB: 484-524 ms at load 2-3, over the
  0.5 s bar under load — and tuning only moves the worst case to the next shape — so 512 KiB, not
  the 4 MiB or 1 MiB first proposed. The largest real page today is ~41 KB, so this is 12x
  headroom; raise the constant only when a real page needs it. Test the exact limit (accepted) and
  the limit plus one byte (413).

  Measured again after the character-reference work (19 Sep, fix round 3), because that rewrite fires
  its callback on EVERY decimal reference rather than only the pathological ones: a `&#65;`-dense
  512 KiB body went 92.5 -> **141.4 ms** (+53 %), a padded-dense one 52.4 -> 76.6 ms, clean prose
  unchanged at 9.9 ms, and a long zero run got faster. Still comfortably inside the 0.5 s bar, but
  the number moved — do not quote the older figure. If a future change touches this rewrite, measure
  again rather than assuming the profile carried over; that assumption was wrong once here already.
- **A scanner crash refuses — with 503, not 422.** A `scan()` exception must never fall through to a
  clean publish and must never be caught into an empty finding list. But the status code must not
  tell the caller its content is at fault: 422 sends a well-behaved agent off to rewrite a perfectly
  good page and resubmit forever while the defect is ours. Answer **503** saying the scanner failed,
  the page was not published, and not to rewrite it. Log it on `artifacts.refusals` at **ERROR** —
  not WARNING, which is the level of a routine refusal on a channel that is supposed to have rows in
  it, so a scanner regression would otherwise hide inside normal refusal traffic. Log the exception's
  TYPE only: never the content, and never the exception's message, since a ValueError's message can
  quote what it choked on.
- **Normalise absurd numeric character references before unescaping — by SIGNIFICANT digits.**
  `html.unescape` hits CPython's decimal integer-to-string limit and raises on `&#` followed by more
  than 4300 digits, and the digits need not be adjacent in the source, because
  `&#1<b></b>1<b></b>1...` only becomes a long run after tag-stripping. So the run must be dealt with
  after `_detag` and before `html.unescape`. Split it into leading zeros and significant digits:
  - **≤7 significant digits** — a legitimate code point wearing padding (`&#00000065;` is `A`, and a
    browser renders it as one). Drop the leading zeros so `html.unescape` decodes it exactly as it
    would unpadded.
  - **>7 significant digits** — never a real code point at any padding. Replace the WHOLE reference,
    `&#` and any trailing `;` included, with **`\ufffd`** — which is precisely what `html.unescape`
    returns for an out-of-range reference when the conversion limit is not in the way. Do not invent
    an inert filler: emulating `unescape` is the thing that makes the scanner behave exactly as if
    CPython had no limit, which is what everyone assumed it did. A filler that keeps the leading `&`
    silently breaks any shape whose match begins at the character after a `=` — that cost
    `url_query_secret` on `?key=&#12345678MYREALTOKEN99`, which scanned clean and published while the
    page rendered the real token. An all-`x` filler additionally risks fullmatching the placeholder
    valve `[*xX]{3,}` and suppressing a neighbouring finding. `\ufffd` has neither problem, adds no
    newline, and was verified against `html.unescape` with the limit lifted: 0 mismatches over 6,000
    random bodies plus a hand-picked set.

  **Do not gate this on the raw digit COUNT.** A threshold of "more than 7 digits" treats
  `&#00000065;` as illegitimate, so the text view stops decoding it — and the text view is the only
  place an entity-hidden credential is ever found. That cost 24 detections across 8 secret families
  when it shipped (fix round 2), turning a fail-CLOSED crash into a fail-OPEN miss:
  `AKI&#00000065;IOSFODNN7EXAMPLE` published 201 with the real key recoverable from the stored body.
  Note also that stripping zeros alone is not sufficient, because CPython's limit counts digit
  CHARACTERS: `int("0"*4301 + "7")` raises too. In the scheme above nothing long reaches `int()`.

  **The invariant here is newline preservation, NOT length preservation.** `_detag` deletes tags
  wholesale and `html.unescape` shortens every ordinary entity; line numbers are computed from a
  view's own newline offsets, so a substitution may change length freely and must only never add or
  remove a newline. Imposing length preservation instead is what forced a digit-destroying
  substitution rather than a digit-fixing one.

  Note `&#x` with 17,200 hex digits does NOT raise (base 16 is exempt) and exactly 4300 digits does
  not either — test the boundary, not a round number. And test the ENCODED form of every shape the
  scanner loads, not one hand-picked entity: a unit test on `&#65;` alone passes against the broken
  version, because `&#65;` is the one form a digit-count threshold cannot touch.
- **Make the differential a standing property, not a one-off harness.** Three separate security bugs
  lived in this one rewrite — all the same shape, a rewriting step changing a character a later stage
  depended on — and each was found by a reviewer rather than a test, because each needed someone to
  think of the right example first. A property needs no example. The control looks impossible because
  it crashes on exactly the inputs the rewrite exists for, but `sys.set_int_max_str_digits(0)` lifts
  the limit for the control alone (measured: with it lifted, `html.unescape("&#" + "9"*50000)`
  returns `'\ufffd'` and does not raise). So assert, over the fixture cases and over random bodies,
  that the findings of a scan with this rewrite stubbed to identity are a **subset** of the real
  scanner's — an **equality** once the `\ufffd` substitution is in, since the scanner then behaves
  exactly as if the conversion limit did not exist. That single assertion catches all three bugs and
  the next one. If the equality fails somewhere, report where; do not weaken it to make it pass.
- **A refusal says where, never what — and WHICH FIELD.** The 422 detail is structured:
  `{"refused": [{"field": "title"|"body", "shape": "<name>", "line": <n>}, ...]}`, using the
  scanner's already de-duplicated and capped findings. FastAPI wraps this in its own envelope, so
  the wire shape is `{"detail": {"refused": [...]}}`; that is fine, leave it.
  Line numbers are what let an agent fix the line instead of learning to split a string to get past
  the gate — but a line number is worse than useless if it names the wrong field. Both guards take
  the field they are checking and report it: without that, a secret in the TITLE answers byte-for-byte
  what the same secret on line 1 of the body answers, and points the agent at line 1 of a clean page,
  while an oversized title answers "body exceeds the size limit" and logs the title's size as a body
  size — so an agent shrinks its page, retries, and is refused identically forever. Test that the 422
  carries the line number, that the same secret in the title and in the body produce distinguishable
  answers, and that the fake secret value appears nowhere in the response.
- **Text that cannot be encoded is refused, not a 500.** A lone surrogate (`"\ud800"`) survives JSON
  decoding and then makes `value.encode("utf-8")` raise `UnicodeEncodeError`. Postgres cannot store
  one either, so the answer is a 422 naming the field, never echoing the text, on all three paths
  (title, body-on-create, body-on-patch). Note for whoever writes this test: httpx's
  `TestClient.post(json=…)` CANNOT transmit a lone surrogate — it encodes client-side and raises
  before a request exists — so the obvious test passes against unfixed code. Build the JSON with
  `ensure_ascii=True` and send raw bytes via `content=`, so the surrogate first becomes a Python str
  inside the server's own `json.loads`, which is where the defect lives.
- **Every refusal is logged, without content.** Log on the `artifacts.refusals` logger at WARNING —
  except a scanner crash, which is ERROR (see above): the agent seat, the artifact id when patching,
  the field, the shape names and the finding count. Never the
  body or a matched value. These logs are the data the shapes get tuned on during adoption. Test with
  `caplog` that the fake secret value is absent from every record.
- **The auth scheme is case-insensitive** (RFC 9110 §11.1): `caller` accepts `Bearer`, `bearer` and
  any other casing of the scheme, then strips it.
- **The title goes through the same gate as the body, wherever a title is accepted.** It is
  agent-authored text that the gallery displays, so `MAX_TITLE_BYTES = 512` (UTF-8, a page name in a
  list, not prose) is checked before scanning, and the title is scanned with the same scanner; a
  finding refuses the publish exactly as a body finding does. Today that is create only: `PatchBody`
  carries `body` and `expected_revision` and no title, and an edit does not rename a page. Do not add
  a title field to PATCH to give this rule somewhere else to apply — if renaming ever becomes a
  feature, the gate comes with it.
- **A validation error never echoes the request.** Pydantic's default 422 returns `input` and `ctx`
  verbatim, and the caller here is an agent, so whatever comes back lands in that agent's transcript.
  A `RequestValidationError` handler returns the field location and the error type only.
- **`publish()` and `patch()` each run in ONE transaction.** Connections autocommit per statement, so
  a failure between the artifact insert and the version insert otherwise leaves a row with a null
  `current_version` that lists forever, answers 409 to every patch, and has no delete route.
- **The lost-patch race answers 409, not 500.** Two concurrent patches with the same expected revision
  both pass the revision check; the `(artifact_id, n)` primary key is what prevents the lost update.
  Catch that unique violation and raise `RevisionMismatch`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_publish.py
import pytest
from fastapi.testclient import TestClient
from artifacts import keys
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_publishing_returns_a_one_label_url(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    response = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    )
    assert response.status_code == 201
    body = response.json()
    assert body["url"] == f"https://{body['id']}.tenfourty.com/"
    assert body["id"].isalnum() and body["id"].islower() and len(body["id"]) == 26


def test_patching_keeps_the_same_url_and_advances_the_version(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    second = client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={"body": PAGE.replace("hi", "hello"), "expected_revision": first["revision"]},
        headers=auth(token),
    )
    assert second.status_code == 200
    assert second.json()["url"] == first["url"]
    assert second.json()["version"] == first["version"] + 1


def test_a_stale_revision_is_refused(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={"body": PAGE, "expected_revision": first["revision"]},
        headers=auth(token),
    )
    stale = client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={"body": PAGE, "expected_revision": first["revision"]},
        headers=auth(token),
    )
    assert stale.status_code == 409


def test_a_body_with_a_secret_is_refused_and_says_why(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    response = client.post(
        "/api/v1/artifacts",
        json={"title": "Leaky", "body": PAGE + "AKIA" + "IOSFODNN7EXAMPLE"},
        headers=auth(token),
    )
    assert response.status_code == 422
    assert "aws_access_key_id" in response.text


def test_another_agents_key_cannot_read_or_patch(client, db):
    token_a, _ = keys.mint(db, agent="seat-a")
    token_b, _ = keys.mint(db, agent="seat-b")
    mine = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token_a)
    ).json()
    assert client.get(f"/api/v1/artifacts/{mine['id']}", headers=auth(token_b)).status_code == 404
    assert client.patch(
        f"/api/v1/artifacts/{mine['id']}",
        json={"body": PAGE, "expected_revision": mine["revision"]},
        headers=auth(token_b),
    ).status_code == 404
    assert mine["id"] not in client.get("/api/v1/artifacts", headers=auth(token_b)).text


def test_no_key_and_a_forged_identity_header_are_both_refused(client, db):
    assert client.post("/api/v1/artifacts", json={"title": "x", "body": PAGE}).status_code == 401
    forged = client.post(
        "/api/v1/artifacts",
        json={"title": "x", "body": PAGE},
        headers={"Remote-User": "jeremy", "Remote-Groups": "admins"},
    )
    assert forged.status_code == 401


def test_a_key_in_a_query_parameter_is_refused(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    response = client.post(
        f"/api/v1/artifacts?key={token}", json={"title": "x", "body": PAGE}
    )
    assert response.status_code == 401
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_publish.py -v` · Expected: FAIL, `create_gallery_app()` takes no
`db` argument.

- [ ] **Step 3: Implement ids**

```python
# src/artifacts/ids.py
import secrets

_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"  # base32, lowercase, DNS-safe


def new_id() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(26))
```

- [ ] **Step 4: Implement the store**

```python
# src/artifacts/store.py
import hashlib
import os
from dataclasses import dataclass
from artifacts import ids, scan
from artifacts.db import Database


class SecretFound(Exception):
    def __init__(self, findings): self.findings = findings


class RevisionMismatch(Exception): ...
class NotFound(Exception): ...


@dataclass(frozen=True)
class Published:
    id: str
    version: int
    revision: str
    url: str


@dataclass(frozen=True)
class ArtifactRow:
    id: str
    title: str
    agent: str
    visibility: str
    current_version: int


def content_domain() -> str:
    return os.environ.get("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")


def url_for(artifact_id: str) -> str:
    return f"https://{artifact_id}.{content_domain()}/"


def _revision(body: str, n: int) -> str:
    return hashlib.sha256(f"{n}:{body}".encode()).hexdigest()[:16]


def _guard(body: str) -> None:
    findings = scan.scan(body)
    if findings:
        raise SecretFound(findings)


def publish(db: Database, agent: str, title: str, body: str) -> Published:
    _guard(body)
    artifact_id, revision = ids.new_id(), _revision(body, 1)
    with db.connect() as conn:
        # Three steps, not two: `version` references `artifact`, and `artifact.current_version`
        # references `version`, so the two tables point at each other. The artifact row is born
        # with no current version, the first version is written, and only then does the artifact
        # point at it -- the same shape `patch()` uses for every later version.
        conn.execute(
            "insert into artifact (id, title, agent, current_version) values (%s, %s, %s, null)",
            (artifact_id, title, agent),
        )
        conn.execute(
            "insert into version (artifact_id, n, body, revision, created_by)"
            " values (%s, 1, %s, %s, %s)",
            (artifact_id, body, revision, agent),
        )
        conn.execute(
            "update artifact set current_version = 1, updated_at = now() where id = %s",
            (artifact_id,),
        )
    return Published(id=artifact_id, version=1, revision=revision, url=url_for(artifact_id))


def _owned(conn, artifact_id: str, agent: str):
    return conn.execute(
        "select id, title, agent, visibility, current_version from artifact"
        " where id = %s and agent = %s",
        (artifact_id, agent),
    ).fetchone()


def patch(db: Database, agent: str, artifact_id: str, body: str, expected_revision: str) -> Published:
    _guard(body)
    with db.connect() as conn:
        row = _owned(conn, artifact_id, agent)
        if row is None:
            raise NotFound()
        current = row[4]
        live = conn.execute(
            "select revision from version where artifact_id = %s and n = %s",
            (artifact_id, current),
        ).fetchone()
        if live is None or live[0] != expected_revision:
            raise RevisionMismatch()
        n = current + 1
        revision = _revision(body, n)
        conn.execute(
            "insert into version (artifact_id, n, body, revision, created_by)"
            " values (%s, %s, %s, %s, %s)",
            (artifact_id, n, body, revision, agent),
        )
        conn.execute(
            "update artifact set current_version = %s, updated_at = now() where id = %s",
            (n, artifact_id),
        )
    return Published(id=artifact_id, version=n, revision=revision, url=url_for(artifact_id))


def get_owned(db: Database, agent: str, artifact_id: str) -> ArtifactRow:
    with db.connect() as conn:
        row = _owned(conn, artifact_id, agent)
    if row is None:
        raise NotFound()
    return ArtifactRow(*row)


def list_for_agent(db: Database, agent: str) -> list[ArtifactRow]:
    with db.connect() as conn:
        rows = conn.execute(
            "select id, title, agent, visibility, current_version from artifact"
            " where agent = %s order by updated_at desc",
            (agent,),
        ).fetchall()
    return [ArtifactRow(*row) for row in rows]
```

- [ ] **Step 5: Implement the agent API**

```python
# src/artifacts/api.py
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from artifacts import keys, store
from artifacts.db import Database


class PublishBody(BaseModel):
    title: str
    body: str


class PatchBody(BaseModel):
    body: str
    expected_revision: str


def agent_router(db: Database) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    def caller(request: Request) -> str:
        if "key" in request.query_params:
            raise HTTPException(401, "a key must not be sent in a query parameter")
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(401, "an agent key is required")
        key = keys.verify(db, header.removeprefix("Bearer ").strip())
        if key is None:
            raise HTTPException(401, "unknown or revoked key")
        return key.agent

    @router.post("/artifacts", status_code=201)
    def create(payload: PublishBody, agent: str = Depends(caller)):
        try:
            return store.publish(db, agent, payload.title, payload.body).__dict__
        except store.SecretFound as found:
            raise HTTPException(422, f"refused: {[f.shape for f in found.findings]}")

    @router.patch("/artifacts/{artifact_id}")
    def update(artifact_id: str, payload: PatchBody, agent: str = Depends(caller)):
        try:
            return store.patch(
                db, agent, artifact_id, payload.body, payload.expected_revision
            ).__dict__
        except store.SecretFound as found:
            raise HTTPException(422, f"refused: {[f.shape for f in found.findings]}")
        except store.RevisionMismatch:
            raise HTTPException(409, "the artifact moved on; read it and patch again")
        except store.NotFound:
            raise HTTPException(404, "no such artifact")

    @router.get("/artifacts/{artifact_id}")
    def read(artifact_id: str, agent: str = Depends(caller)):
        try:
            return store.get_owned(db, agent, artifact_id).__dict__
        except store.NotFound:
            raise HTTPException(404, "no such artifact")

    @router.get("/artifacts")
    def index(agent: str = Depends(caller)):
        return [row.__dict__ for row in store.list_for_agent(db, agent)]

    return router
```

- [ ] **Step 6: Wire it into the app**

```python
# src/artifacts/app.py
from fastapi import FastAPI
from artifacts.api import agent_router
from artifacts.db import Database


def create_gallery_app(db: Database | None = None) -> FastAPI:
    app = FastAPI(title="artifacts gallery", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if db is not None:
        app.include_router(agent_router(db))
    return app
```

- [ ] **Step 7: Run every test**

Run: `uv run pytest -v` · Expected: all pass, including the seven in `test_publish.py`.
Run: `uv run ruff check src tests` · Expected: no findings.

- [ ] **Step 8: Commit**

```bash
git add -A && git commit -m "feat: an agent publishes a page and gets a URL that survives editing"
```

---

### Task 6: The page is served framed-only, with the header set

**Files:**
- Create: `src/artifacts/content.py`, `tests/test_content.py`
- Modify: `src/artifacts/app.py` (host-based routing)
- Test: `tests/test_content.py`

**Interfaces:**
- Consumes: `Database`, `store`.
- Produces: `artifacts.content.create_content_app(db) -> FastAPI` serving
  `GET /` (current version), `GET /v/{n}` (inert older version), and
  `artifacts.content.headers_for(live: bool) -> dict[str, str]`; `artifacts.app.create_app(db)`
  routing by `Host`. (As built after its review, the router fails closed through
  `artifacts.hosts`; Task 6b then moves both page routes under `/t/{token}/`.)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_content.py
import pytest
from fastapi.testclient import TestClient
from artifacts import keys, store
from artifacts.content import create_content_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"
GALLERY = "https://artifacts.tenfourty.site"
FRAMED = {"sec-fetch-dest": "iframe"}


@pytest.fixture()
def published(db):
    """Two versions, so v1 is genuinely an older one. A fixture that publishes
    once makes `/v/1` the CURRENT version, and a test called "an older version
    is inert" then proves nothing about older versions."""
    keys.mint(db, agent="ops-seat")
    artifact = store.publish(db, "ops-seat", "Board", PAGE)
    store.patch(db, "ops-seat", artifact.id, PAGE.replace("hi", "hi again"), artifact.revision)
    return artifact


@pytest.fixture()
def content(db, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{published.id}.tenfourty.com"})
    return client


def test_a_framed_request_gets_the_page(content):
    response = content.get("/", headers=FRAMED)
    assert response.status_code == 200
    assert "Board" in response.text


def test_a_top_level_request_is_sent_to_the_gallery_instead(content, published):
    response = content.get("/", headers={"sec-fetch-dest": "document"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"{GALLERY}/a/{published.id}"


def test_the_header_set_is_on_every_page_response(content):
    headers = content.get("/", headers=FRAMED).headers
    csp = headers["content-security-policy"]
    for directive in [
        "default-src 'none'",
        "connect-src 'self'",
        "form-action 'none'",
        "base-uri 'none'",
        "worker-src 'none'",
        f"frame-ancestors {GALLERY}",
    ]:
        assert directive in csp
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-dns-prefetch-control"] == "off"
    assert headers["cross-origin-opener-policy"] == "same-origin"
    assert headers["cross-origin-resource-policy"] == "same-origin"


def test_an_older_version_is_inert_but_still_answers_the_contract(content):
    """Inert is not the same as absent. A page written to the contract does
    `await window.claude.use("db")` and branches on null; strip window.claude
    and that page throws instead of degrading."""
    response = content.get("/v/1", headers=FRAMED)
    assert response.status_code == 200
    assert "hi</body>" in response.text and "hi again" not in response.text
    assert "connect-src 'none'" in response.headers["content-security-policy"]
    assert "window.claude" in response.text
    assert "use:async(n)=>null" in response.text


def test_the_current_version_by_number_is_inert_too(content):
    """Liveness comes from the route, not from the number. Asking for version 2
    explicitly asks for a snapshot, and a snapshot gets the inert runtime --
    otherwise the number the gallery happens to link becomes a privilege."""
    response = content.get("/v/2", headers=FRAMED)
    assert response.status_code == 200
    assert "hi again" in response.text
    assert "use:async(n)=>null" in response.text


def test_the_current_version_carries_the_runtime_shim(content):
    assert "window.claude" in content.get("/", headers=FRAMED).text


@pytest.mark.parametrize("sent", [{}, {"sec-fetch-dest": "empty"}, {"sec-fetch-dest": ""}])
def test_a_request_that_does_not_say_it_is_framed_never_gets_the_page(content, sent):
    """curl, a script's fetch and an old browser all send no Sec-Fetch-Dest.
    Denying only "document" would serve every one of them the page."""
    response = content.get("/", headers=sent, follow_redirects=False)
    assert response.status_code == 303
    assert "window.claude" not in response.text


def test_a_page_with_no_head_still_gets_the_runtime(db, monkeypatch):
    """`<!doctype html><body>...` is ordinary agent-written HTML. Injecting only
    at `</head>` leaves that page with no runtime and reports nothing."""
    keys.mint(db, agent="headless-seat")
    artifact = store.publish(db, "headless-seat", "Bare", "<!doctype html><body>hi</body>")
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{artifact.id}.tenfourty.com"})
    text = client.get("/", headers=FRAMED).text
    assert "window.claude" in text
    assert text.index("window.claude") < text.index("hi")


def test_the_gallery_origin_is_read_at_call_time(content, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://elsewhere.example")
    response = content.get("/", headers={"sec-fetch-dest": "document"}, follow_redirects=False)
    assert response.headers["location"] == f"https://elsewhere.example/a/{published.id}"


def test_an_unknown_artifact_host_is_not_found(content):
    content.headers.update({"host": "aaaaaaaaaaaaaaaaaaaaaaaaaa.tenfourty.com"})
    assert content.get("/", headers=FRAMED).status_code == 404


def test_a_pending_version_is_never_served(db, content, published):
    """Nothing in this arc writes a 'pending' version row, so this test writes one by
    hand. That is the point: the `status = 'live'` filter in _load has no other way to be
    exercised, and an unexercised filter is indistinguishable from an absent one. Without
    the assertion, the day pending-on-widening (design D11) lands, the content server
    starts serving bodies whose whole reason for existing is that they are NOT yet cleared
    to be served -- and every test here would still be green."""
    with db.connect() as conn:
        conn.execute(
            "insert into version (artifact_id, n, body, revision, status, created_by) "
            "values (%s, %s, %s, %s, 'pending', %s)",
            (published.id, 99, "<!doctype html><body>not cleared</body>", "r99", "ops-seat"),
        )
    assert content.get("/v/99", headers=FRAMED).status_code == 404
    # and the live current version is still served, so the filter has not just broken reads
    assert content.get("/", headers=FRAMED).status_code == 200
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_content.py -v` · Expected: FAIL, no module `artifacts.content`.

- [ ] **Step 3: Implement the content server**

```python
# src/artifacts/content.py
import os
import re
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from artifacts.db import Database
from artifacts.store import content_domain

def gallery_origin() -> str:
    """Read at call time, exactly like store.content_domain(). A module-level
    os.environ.get() is fixed at import, so no test can override it and the
    deployment has to set it before the first import of this module."""
    return os.environ.get("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")

_BASE_CSP = (
    "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; font-src 'self'; form-action 'none'; "
    "base-uri 'none'; worker-src 'none'; "
)


def headers_for(live: bool) -> dict[str, str]:
    connect = "connect-src 'self'; " if live else "connect-src 'none'; "
    return {
        "content-security-policy": _BASE_CSP + connect + f"frame-ancestors {gallery_origin()}",
        "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
        "x-dns-prefetch-control": "off",
        "cross-origin-opener-policy": "same-origin",
        "cross-origin-resource-policy": "same-origin",
        "permissions-policy": "geolocation=(), camera=(), microphone=(), payment=()",
        "cache-control": "no-store",
    }


SHIM = "<script>window.claude={use:async(n)=>null};</script>"  # replaced in Task 7

# An older version is inert, which is NOT the same as having no runtime. The
# design says every name RESOLVES null on a version path; a page written to the
# contract does `const db = await window.claude.use("db")` and branches on null,
# so removing window.claude entirely makes that page throw a TypeError instead
# of degrading. Serve the null shim there and let the page render read-only.
INERT_SHIM = "<script>window.claude={use:async(n)=>null};window.storage=null;</script>"


def inject(body: str, shim: str) -> str:
    """Put the shim in the document, wherever the document lets us.

    A plain `body.replace("</head>", ...)` silently does nothing on a page with
    no head -- `<!doctype html><body>...` is ordinary agent-written HTML -- and
    the page then loads with no runtime at all and no error anywhere. Try the
    head close, then the body open, then the front of the document; the page
    always ends up with a runtime. Matching is case-insensitive: `</HEAD>` is
    the same tag.
    """
    for pattern, where in ((r"</head\s*>", "before"), (r"<body\b[^>]*>", "after")):
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            at = match.start() if where == "before" else match.end()
            return body[:at] + shim + body[at:]
    return shim + body


def _artifact_id(request: Request) -> str | None:
    host = request.headers.get("host", "").split(":")[0]
    suffix = "." + content_domain()
    return host[: -len(suffix)] if host.endswith(suffix) else None


def create_content_app(db: Database) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)

    def _load(artifact_id: str, n: int | None) -> tuple[str, int] | None:
        with db.connect() as conn:
            row = conn.execute(
                "select current_version from artifact where id = %s", (artifact_id,)
            ).fetchone()
            if row is None:
                return None
            wanted = n if n is not None else row[0]
            # `and status = 'live'` is a no-op today -- nothing in this arc writes a
            # 'pending' version row, because pending-on-widening (design D11) is deferred.
            # It is here so that the day D11 lands, the content server does not silently
            # begin serving bodies that were written precisely because they had NOT yet
            # been cleared to be served. A filter that costs nothing now and closes a hole
            # later beats a comment in the deferral list that a future task has to notice.
            body = conn.execute(
                "select body from version where artifact_id = %s and n = %s "
                "and status = 'live'",
                (artifact_id, wanted),
            ).fetchone()
        return (body[0], wanted) if body else None

    def _serve(request: Request, n: int | None) -> Response:
        artifact_id = _artifact_id(request)
        if artifact_id is None:
            return Response("not found", status_code=404)
        # Framed-only is asserted POSITIVELY. Denying just "document" serves the page to
        # any client that sends no Sec-Fetch-Dest at all -- curl, a script's fetch, an old
        # browser -- which is every client that is not a browser honouring the frame.
        # `frame` is the legacy <frame> value; nested iframes also send `iframe`.
        if request.headers.get("sec-fetch-dest") not in ("iframe", "frame"):
            return RedirectResponse(f"{gallery_origin()}/a/{artifact_id}", status_code=303)
        loaded = _load(artifact_id, n)
        if loaded is None:
            return Response("not found", status_code=404)
        body, served = loaded
        live = n is None
        html = inject(body, SHIM if live else INERT_SHIM)
        return HTMLResponse(html, headers=headers_for(live))

    @app.get("/")
    def current(request: Request) -> Response:
        return _serve(request, None)

    @app.get("/v/{n}")
    def version(request: Request, n: int) -> Response:
        return _serve(request, n)

    return app
```

- [ ] **Step 4: Route by host in the app factory**

```python
# add to src/artifacts/app.py
from artifacts.content import create_content_app
from artifacts.store import content_domain


def create_app(db: Database):
    gallery, content = create_gallery_app(db), create_content_app(db)

    async def router(scope, receive, send):
        headers = dict(scope.get("headers") or [])
        host = headers.get(b"host", b"").decode().split(":")[0]
        chosen = content if host.endswith("." + content_domain()) else gallery
        await chosen(scope, receive, send)

    return router
```

- [ ] **Step 5: Run every test**

Run: `uv run pytest -v` · Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/artifacts/content.py src/artifacts/app.py tests/test_content.py
git commit -m "feat: pages are served only inside the gallery's frame, with the header set"
```

---

### Task 6b: A private page is readable only with its capability token

Added 2026-09-19 by the controller, after Task 6's review: the design's access control for a page
on the content origin — **a capability token as a path segment** (design D2, D8, §4's hostname
table, §5 rule 7) — was in neither this plan's tasks nor its "does not cover" list. Without it a
page is readable by anyone who knows its id and sends `Sec-Fetch-Dest: iframe`, and **the id is not
a secret**: it is the hostname, so it travels in DNS queries and in plaintext TLS SNI. That is why the
design puts the token in the path, which TLS encrypts. Until this task, "private" is a label, not a
control.

Rulings this task carries (the spec is silent or this part cannot do what it says):

- **Every page needs its token in this part, whatever its visibility.** The design lets a public page
  answer without one, but a public page has no public viewer yet — the page is framed-only and the
  gallery is Authelia-gated, and the ungated frame host (`links.tenfourty.com`) is deferred with
  link-sharing. A token-free path with no legitimate consumer is only surface, so it is not built.
  The part that builds the public viewer decides how public pages are addressed.
- **`link` visibility uses the same read token here.** Its expiring, revocable share token (D8) is
  deferred with the gallery's visibility screens; visibility cannot be changed by anyone yet.
- **The URL handed to an agent does not change.** `Published.url` stays the token-free content root
  (`https://<id>.tenfourty.com/`). It keeps working as a link, because an unframed request is sent to
  the gallery, which frames the tokenised address. The token appears only in the gallery's `<iframe
  src>`, which only Jeremy's Authelia session can load. **No agent-facing response ever carries it.**
- **A wrong or missing token is a 404, never a 403**: the answer must not confirm that an id exists.

**Files:**
- Modify: `src/artifacts/schema.sql` (the `read_token` column), `src/artifacts/store.py` (generate it,
  `content_url`, `read_token`), `src/artifacts/content.py` (the token-prefixed routes),
  `tests/test_content.py` and `tests/test_router.py` (request paths gain the token prefix)
- Create: `tests/test_capability.py`

**Interfaces:**
- Consumes: Task 6 as amended by its fix round — `artifacts.hosts`, `store.gallery_origin()`,
  `content._artifact_id(request)`, the content app's base-header middleware.
- Produces: `store.content_url(artifact_id: str, read_token: str) -> str` (the address the gallery
  frames); `store.read_token(db, artifact_id: str) -> str | None`; content routes
  `GET /t/{token}/` (current version) and `GET /t/{token}/v/{n}` (inert version). `GET /` remains
  only so an unframed visit to the address an agent was given is sent to the gallery; framed, it is
  a 404. Task 7 mounts its state routes under `/t/{token}/_state/`; Task 8 frames
  `store.content_url(...)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_capability.py
import pytest
from fastapi.testclient import TestClient

from artifacts import keys, store
from artifacts.app import create_gallery_app
from artifacts.content import create_content_app

PAGE = "<!doctype html><html><head></head><body>hi</body></html>"
FRAMED = {"sec-fetch-dest": "iframe"}


@pytest.fixture()
def published(db):
    keys.mint(db, agent="ops-seat")
    return store.publish(db, "ops-seat", "Board", PAGE)


@pytest.fixture()
def content(db, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{published.id}.tenfourty.com"})
    return client


def test_the_page_is_served_only_with_its_own_token(db, content, published):
    token = store.read_token(db, published.id)
    assert content.get(f"/t/{token}/", headers=FRAMED).status_code == 200
    assert content.get("/t/not-the-token/", headers=FRAMED).status_code == 404
    assert content.get("/", headers=FRAMED).status_code == 404
    # another artifact's token, presented on this artifact's host, opens nothing
    other = store.publish(db, "ops-seat", "Other", PAGE)
    assert content.get(f"/t/{store.read_token(db, other.id)}/", headers=FRAMED).status_code == 404


def test_a_version_needs_the_token_too(db, content, published):
    token = store.read_token(db, published.id)
    assert content.get(f"/t/{token}/v/1", headers=FRAMED).status_code == 200
    assert content.get("/t/not-the-token/v/1", headers=FRAMED).status_code == 404


def test_a_non_ascii_token_is_a_mismatch_not_a_crash(content):
    # hmac.compare_digest raises TypeError on a non-ASCII str; the comparison must be on bytes.
    assert content.get("/t/%C3%A9t%C3%A9/", headers=FRAMED).status_code == 404


def test_the_redirect_never_carries_the_token(db, content, published):
    token = store.read_token(db, published.id)
    response = content.get(f"/t/{token}/", follow_redirects=False)
    assert response.status_code == 303
    assert token not in response.headers["location"]
    assert response.headers["location"].endswith(f"/a/{published.id}")


def test_the_address_an_agent_is_given_still_works_as_a_link(content, published):
    response = content.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/a/{published.id}")


def test_the_agent_api_never_returns_the_token(db):
    token, _ = keys.mint(db, agent="ops-seat")
    client = TestClient(create_gallery_app(db))
    headers = {"authorization": f"Bearer {token}"}
    created = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=headers
    )
    artifact_id = created.json()["id"]
    read = store.read_token(db, artifact_id)
    patched = client.patch(
        f"/api/v1/artifacts/{artifact_id}",
        json={"body": PAGE.replace("hi", "hello"), "expected_revision": created.json()["revision"]},
        headers=headers,
    )
    listed = client.get("/api/v1/artifacts", headers=headers)
    fetched = client.get(f"/api/v1/artifacts/{artifact_id}", headers=headers)
    responses = (created, patched, listed, fetched)
    assert [r.status_code for r in responses] == [201, 200, 200, 200]
    for response in responses:
        assert read not in response.text


def test_the_token_survives_a_patch(db, published):
    before = store.read_token(db, published.id)
    store.patch(db, "ops-seat", published.id, PAGE.replace("hi", "hello"), published.revision)
    assert store.read_token(db, published.id) == before


def test_every_artifact_gets_its_own_unguessable_token(db):
    tokens = {store.read_token(db, store.publish(db, "ops-seat", "t", PAGE).id) for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(token) >= 32 for token in tokens)


def test_a_public_page_still_needs_its_token_in_this_part(db, content, published):
    """Fail-closed by ruling: a public page has no public viewer yet (see the task text)."""
    with db.connect() as conn:
        conn.execute("update artifact set visibility = 'public' where id = %s", (published.id,))
    assert content.get("/", headers=FRAMED).status_code == 404
    token = store.read_token(db, published.id)
    assert content.get(f"/t/{token}/", headers=FRAMED).status_code == 200


def test_content_url_is_what_the_gallery_frames(db, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    token = store.read_token(db, published.id)
    assert store.content_url(published.id, token) == (
        f"https://{published.id}.tenfourty.com/t/{token}/"
    )
```

Also, in `tests/test_content.py` and `tests/test_router.py`: every request that expects the PAGE
now goes to `/t/<token>/` (or `/t/<token>/v/<n>`), with the token from `store.read_token(db, id)`.
Change the paths only — every assertion stays as it is. A test that expects a redirect or a 404
keeps its current path when that path still means the same thing. **One that does not:** Task 6's
fix-round test that expects a 422 from `/v/abc` must move to `/t/<token>/v/abc` — the bare
`/v/{n}` route is removed below, so the old path would answer 404 and the test would be asserting
the wrong thing about the wrong route.

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_capability.py -v` · Expected: FAIL — `store.read_token` does not
exist.

- [ ] **Step 3: The column**

In `src/artifacts/schema.sql`, inside `create table if not exists artifact`, after `visibility`:

```sql
    read_token      text not null,  -- the capability for reading this page on the content origin
                                    -- (design D2). A PATH segment, because the id is the hostname
                                    -- and so travels in DNS and in plaintext TLS SNI; the path is
                                    -- inside TLS. Never returned by the agent API.
```

No `alter table` is needed: no database outside the test suite exists yet, and the suite resets the
schema per test.

- [ ] **Step 4: Generate, expose to the gallery, never to an agent**

In `src/artifacts/store.py`, add `import secrets` to the imports and:

```python
def content_url(artifact_id: str, read_token: str) -> str:
    """The address the GALLERY frames. Never handed to an agent: the token in its path is the
    capability for reading the page (design D2), and `Published.url` stays token-free."""
    return f"https://{artifact_id}.{content_domain()}/t/{read_token}/"


def read_token(db: Database, artifact_id: str) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            "select read_token from artifact where id = %s", (artifact_id,)
        ).fetchone()
    return row[0] if row else None
```

In `publish`, generate the token alongside the id and write it with the artifact row:

```python
    artifact_id, revision = ids.new_id(), _revision(body, 1)
    token = secrets.token_urlsafe(24)  # 192 bits; URL-safe alphabet, so it is one path segment
    ...
        conn.execute(
            "insert into artifact (id, title, agent, read_token, current_version)"
            " values (%s, %s, %s, %s, null)",
            (artifact_id, title, agent, token),
        )
```

`patch` does not touch the token. `_owned`, `get_owned` and `list_for_agent` keep their explicit
column lists, which do not include it — that is what keeps it out of every agent response.

- [ ] **Step 5: The token-prefixed routes**

In `src/artifacts/content.py` (as amended by Task 6's fix round), add `import hmac` and:

```python
def _token_matches(expected: str, given: str | None) -> bool:
    """hmac.compare_digest raises TypeError on a non-ASCII str, and `given` is whatever the request
    path carried, so compare bytes: a crafted path is a mismatch, never a 500."""
    return given is not None and hmac.compare_digest(expected.encode(), given.encode())
```

`_load` takes the presented token and refuses a mismatch exactly as it refuses a missing artifact:

```python
    def _load(artifact_id: str, token: str | None, n: int | None) -> tuple[str, int] | None:
        with db.connect() as conn:
            row = conn.execute(
                "select current_version, read_token from artifact where id = %s", (artifact_id,)
            ).fetchone()
            # A wrong token and a missing artifact are the same answer: nothing here may confirm
            # that an id exists.
            if row is None or not _token_matches(row[1], token):
                return None
            wanted = n if n is not None else row[0]
            body = conn.execute(
                "select body from version where artifact_id = %s and n = %s "
                "and status = 'live'",
                (artifact_id, wanted),
            ).fetchone()
        return (body[0], wanted) if body else None
```

`_serve(request, token, n)` passes `token` through to `_load`; its order is unchanged — host check,
then the framed-only redirect (which never carries the token: its Location is
`{gallery_origin()}/a/{artifact_id}`), then the load. The routes become:

```python
    @app.get("/")
    def bare(request: Request) -> Response:
        # The address an agent is handed. Unframed, it is sent to the gallery like any other
        # unframed request; framed, it carries no token and so opens nothing.
        return _serve(request, None, None)

    @app.get("/t/{token}/")
    def current(request: Request, token: str) -> Response:
        return _serve(request, token, None)

    @app.get("/t/{token}/v/{n}")
    def version(request: Request, token: str, n: int) -> Response:
        return _serve(request, token, n)
```

Remove the bare `GET /v/{n}` route: a version is only ever reached with its token.

- [ ] **Step 6: Run every test**

Run: `uv run pytest -q && uv run ruff check .` · Expected: all pass, including the ten in
`test_capability.py`, with `test_content.py` and `test_router.py` passing on their token-prefixed
paths.

- [ ] **Step 7: Commit**

```bash
git add src/artifacts/schema.sql src/artifacts/store.py src/artifacts/content.py \
        tests/test_capability.py tests/test_content.py tests/test_router.py
git commit -m "feat: a page is readable only with its capability token, which no agent is ever given"
```

---

### Task 7: The page keeps its own state, in the decisions page's shape

**Files:**
- Create: `src/artifacts/state.py`, `src/artifacts/shim.js`, `tests/test_state.py`
- Modify: `src/artifacts/content.py` (serve the real shim and `/_state` routes),
  `src/artifacts/api.py` (agent read-back with the scanner)
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: `Database`, `scan`.
- Produces: `artifacts.state.put(db, artifact_id, path, doc) -> None`;
  `artifacts.state.get(db, artifact_id, path) -> dict | None`;
  `artifacts.state.collection(db, artifact_id, prefix) -> dict[str, dict]`, reached over HTTP by a
  `/t/{token}/_state/` path ending in a slash, which returns the map instead of one document;
  exception `state.SecretInState`; content routes `GET|PUT /t/{token}/_state/{path:path}` —
  under the page's capability token like every other content path (Task 6b);
  agent route `GET /api/v1/artifacts/{id}/state/{path:path}`;
  `window.claude.use("db")` resolving `{doc(path), collection(prefix)}` with
  `get/set/update` on a doc.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_state.py
import pytest
from fastapi.testclient import TestClient
from artifacts import keys, state, store
from artifacts.app import create_gallery_app
from artifacts.content import create_content_app

PAGE = "<!doctype html><html><head></head><body>hi</body></html>"
ANSWER = {"choice": "a", "note": "go ahead", "answeredAt": "2026-09-19T01:00:00Z"}
FRAMED = {"sec-fetch-dest": "iframe"}


@pytest.fixture()
def published(db):
    return store.publish(db, "ops-seat", "Decisions", PAGE)


def base(db, published) -> str:
    """Every content path is under the page's capability token (Task 6b)."""
    return f"/t/{store.read_token(db, published.id)}"


def test_a_page_writes_and_reads_its_own_state(db, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{published.id}.tenfourty.com"})
    put = client.put(f"{base(db, published)}/_state/answers/homelab-ij8", json=ANSWER,
                     headers={**FRAMED, "sec-fetch-site": "same-origin"})
    assert put.status_code == 204
    got = client.get(f"{base(db, published)}/_state/answers/homelab-ij8",
                     headers={**FRAMED, "sec-fetch-site": "same-origin"})
    assert got.json() == ANSWER


def test_state_calls_from_another_origin_are_refused(db, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{published.id}.tenfourty.com"})
    for site in ("cross-site", "same-site", "none"):
        response = client.get(f"{base(db, published)}/_state/answers/homelab-ij8",
                              headers={**FRAMED, "sec-fetch-site": site})
        assert response.status_code == 403
    assert client.get(f"{base(db, published)}/_state/answers/x", headers=FRAMED).status_code == 403  # header absent


def test_the_operator_reads_answers_back_by_bead_id(db, published):
    token, _ = keys.mint(db, agent="ops-seat")
    state.put(db, published.id, "answers/homelab-ij8", ANSWER)
    client = TestClient(create_gallery_app(db))
    response = client.get(
        f"/api/v1/artifacts/{published.id}/state/answers/homelab-ij8",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200 and response.json() == ANSWER


def test_state_carrying_a_secret_refuses_the_read_and_names_the_artifact(db, published):
    token, _ = keys.mint(db, agent="ops-seat")
    state.put(db, published.id, "answers/x", {"note": "AKIA" + "IOSFODNN7EXAMPLE"})
    client = TestClient(create_gallery_app(db))
    response = client.get(
        f"/api/v1/artifacts/{published.id}/state/answers/x",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409
    assert published.id in response.text and "aws_access_key_id" in response.text


def test_a_collection_comes_back_keyed_by_path(db, published):
    state.put(db, published.id, "answers/one", {"choice": "a"})
    state.put(db, published.id, "answers/two", {"choice": "b"})
    assert set(state.collection(db, published.id, "answers/")) == {"answers/one", "answers/two"}


def test_a_collection_is_reachable_over_http_too(db, published, monkeypatch):
    """The Python function existing is not the same as the shim being able to
    call it. A trailing slash addresses the collection; without this route the
    shim's collection(prefix) silently answers {} forever."""
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    state.put(db, published.id, "answers/one", {"choice": "a"})
    state.put(db, published.id, "answers/two", {"choice": "b"})
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{published.id}.tenfourty.com"})
    body = client.get(f"{base(db, published)}/_state/answers/", headers={**FRAMED, "sec-fetch-site": "same-origin"}).json()
    assert set(body) == {"answers/one", "answers/two"}


def test_state_carrying_a_secret_is_refused_on_the_way_in(db, published, monkeypatch):
    """Scanning only at agent read-back leaves the value in the database and
    hands it back to the page's own unscanned GET."""
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{published.id}.tenfourty.com"})
    leak = "AKIA" + "IOSFODNN7EXAMPLE"
    response = client.put(
        f"{base(db, published)}/_state/answers/x", json={"note": leak},
        headers={**FRAMED, "sec-fetch-site": "same-origin"},
    )
    assert response.status_code == 422
    assert leak not in response.text
    assert response.json()["refused"][0]["shape"] == "aws_access_key_id"
    assert state.get(db, published.id, "answers/x") is None


def test_a_state_document_has_a_ceiling(db, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{published.id}.tenfourty.com"})
    response = client.put(
        f"{base(db, published)}/_state/answers/big", json={"note": "x" * (state.MAX_STATE_BYTES + 1)},
        headers={**FRAMED, "sec-fetch-site": "same-origin"},
    )
    assert response.status_code == 413


def test_a_public_page_reads_its_state_but_cannot_write_it(db, published, monkeypatch):
    """A public artifact's origin is reachable without Authelia. Sec-Fetch-Site
    is a browser's word, not an identity: curl sends it just as happily, and
    every visitor to a public page is same-origin anyway."""
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    state.put(db, published.id, "answers/seeded", {"choice": "a"})
    with db.connect() as conn:
        conn.execute("update artifact set visibility = 'public' where id = %s", (published.id,))
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{published.id}.tenfourty.com"})
    same_origin = {**FRAMED, "sec-fetch-site": "same-origin"}
    assert client.get(f"{base(db, published)}/_state/answers/seeded", headers=same_origin).json() == {"choice": "a"}
    assert client.put(f"{base(db, published)}/_state/answers/x", json={"a": 1}, headers=same_origin).status_code == 403
    assert state.get(db, published.id, "answers/x") is None


def test_a_public_page_is_told_it_cannot_edit(db, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    with db.connect() as conn:
        conn.execute("update artifact set visibility = 'public' where id = %s", (published.id,))
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{published.id}.tenfourty.com"})
    text = client.get(f"{base(db, published)}/", headers=FRAMED).text
    assert "canEdit: () => false" in text.replace('"', "'") or "=> false" in text
    assert "__OWNER__" not in text and "__STATE_BASE__" not in text
    assert "__" not in text.split("window.claude")[0][-400:]


def test_the_shim_calls_state_under_the_token_it_was_served_with(db, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{published.id}.tenfourty.com"})
    text = client.get(f"{base(db, published)}/", headers=FRAMED).text
    assert f"{base(db, published)}/_state/" in text


def test_state_needs_the_token_too(db, published, monkeypatch):
    """The page's answers are exactly what the token protects: a wrong token reads nothing and
    writes nothing, and the answer is the same 404 as a missing artifact."""
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    state.put(db, published.id, "answers/seeded", {"choice": "a"})
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{published.id}.tenfourty.com"})
    same_origin = {**FRAMED, "sec-fetch-site": "same-origin"}
    assert client.get("/t/wrong/_state/answers/seeded", headers=same_origin).status_code == 404
    assert client.get("/t/wrong/_state/answers/", headers=same_origin).status_code == 404
    put = client.put("/t/wrong/_state/answers/x", json={"a": 1}, headers=same_origin)
    assert put.status_code == 404
    assert state.get(db, published.id, "answers/x") is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_state.py -v` · Expected: FAIL, no module `artifacts.state`.

- [ ] **Step 3: Implement the state module**

```python
# src/artifacts/state.py
import json
from artifacts import scan
from artifacts.db import Database


class SecretInState(Exception):
    def __init__(self, artifact_id: str, findings):
        self.artifact_id, self.findings = artifact_id, findings


class StateTooLarge(Exception):
    def __init__(self, size: int):
        self.size = size


class StatePathTooLong(Exception):
    def __init__(self, length: int):
        self.length = length


MAX_STATE_BYTES = 64 * 1024
MAX_STATE_PATH = 256


def put(db: Database, artifact_id: str, path: str, doc: dict) -> None:
    """Capped and scanned on the way IN, not only on the way out.

    Scanning only at agent read-back leaves a matched secret sitting in the
    database until someone happens to read it, and hands it straight back to
    the page itself, whose own GET does not scan. And with no cap a page in a
    retry loop writes until the disk is full: the 512 KiB body cap governs
    publishing, nothing governed this.
    """
    encoded = json.dumps(doc)
    if len(path.encode("utf-8")) > MAX_STATE_PATH:
        raise StatePathTooLong(len(path))
    if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
        raise StateTooLarge(len(encoded.encode("utf-8")))
    findings = scan.scan(encoded)
    if findings:
        raise SecretInState(artifact_id, findings)
    with db.connect() as conn:
        conn.execute(
            "insert into state (artifact_id, path, doc) values (%s, %s, %s)"
            " on conflict (artifact_id, path) do update set doc = excluded.doc,"
            " updated_at = now()",
            (artifact_id, path, encoded),
        )


def get(db: Database, artifact_id: str, path: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "select doc from state where artifact_id = %s and path = %s", (artifact_id, path)
        ).fetchone()
    return row[0] if row else None


def get_scanned(db: Database, artifact_id: str, path: str) -> dict | None:
    doc = get(db, artifact_id, path)
    if doc is None:
        return None
    findings = scan.scan(json.dumps(doc))
    if findings:
        raise SecretInState(artifact_id, findings)
    return doc


def collection(db: Database, artifact_id: str, prefix: str) -> dict[str, dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "select path, doc from state where artifact_id = %s and path like %s",
            (artifact_id, prefix + "%"),
        ).fetchall()
    return {row[0]: row[1] for row in rows}
```

- [ ] **Step 4: Add the content routes with the same-origin guard**

```python
# add inside create_content_app in src/artifacts/content.py
from fastapi import Body
from artifacts import state

    def _same_origin_or_403(request: Request) -> Response | None:
        """CSRF defence, NOT authentication. Sec-Fetch-* is set by the browser
        and cannot be forged by page script, which is exactly what makes it
        useful against another site's page -- and exactly nothing against curl,
        which simply sends the header. Never let this be the only thing between
        a writer and the data; the visibility check below is the real gate.
        """
        if request.headers.get("sec-fetch-site") != "same-origin":
            return Response("same-origin only", status_code=403)
        return None

    def _visibility(artifact_id: str, token: str) -> str | None:
        """The artifact's visibility, for a caller presenting its token (Task 6b). A wrong token
        and a missing artifact are the same None, and every state route answers both with 404."""
        with db.connect() as conn:
            row = conn.execute(
                "select visibility, read_token from artifact where id = %s", (artifact_id,)
            ).fetchone()
        return row[0] if row and _token_matches(row[1], token) else None

    @app.get("/t/{token}/_state/{path:path}")
    def read_state(request: Request, token: str, path: str) -> Response:
        refusal = _same_origin_or_403(request)
        if refusal:
            return refusal
        artifact_id = _artifact_id(request)
        if artifact_id is None or _visibility(artifact_id, token) is None:
            return Response("not found", status_code=404, media_type="text/plain")
        # A path ending in a slash addresses the collection under it, which is
        # what the shim's collection(prefix) calls. Without this the whole
        # collection API is unreachable over HTTP and always answers {}.
        if path.endswith("/"):
            body = state.collection(db, artifact_id, path)
        else:
            doc = state.get(db, artifact_id, path)
            body = doc if doc is not None else {}
        return JSONResponse(body, headers=headers_for(True))

    @app.put("/t/{token}/_state/{path:path}", status_code=204)
    def write_state(request: Request, token: str, path: str, doc: dict = Body(...)) -> Response:
        refusal = _same_origin_or_403(request)
        if refusal:
            return refusal
        artifact_id = _artifact_id(request)
        visibility = _visibility(artifact_id, token) if artifact_id else None
        if visibility is None:
            return Response("not found", status_code=404, media_type="text/plain")
        # A PUBLIC artifact's origin is reachable without Authelia -- that is
        # what public means -- so any visitor, and anyone who can send two
        # headers, would otherwise be able to overwrite the page's answers.
        # Public pages read their seeded state and render it; they do not write.
        if visibility != "private":
            return Response("this page is public and cannot write state", status_code=403)
        if path.endswith("/"):
            return Response("a collection is not a document", status_code=400)
        try:
            state.put(db, artifact_id, path, doc)
        except state.SecretInState as found:
            return JSONResponse(
                {"refused": [{"shape": f.shape, "line": f.line} for f in found.findings]},
                status_code=422,
            )
        except (state.StateTooLarge, state.StatePathTooLong):
            return Response("too large", status_code=413)
        return Response(status_code=204)
```

Add `from fastapi.responses import JSONResponse` at the top of the module.

- [ ] **Step 5: Write the real shim**

```javascript
// src/artifacts/shim.js
(() => {
  const call = async (method, path, body) => {
    const response = await fetch(`__STATE_BASE__${path}`, {
      method,
      headers: body ? { "content-type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) throw new Error(`state ${method} failed: ${response.status}`);
    return response.status === 204 ? undefined : response.json();
  };

  const doc = (path) => ({
    get: () => call("GET", path),
    set: (value) => call("PUT", path, value),
    update: async (patch) => call("PUT", path, { ...(await call("GET", path)), ...patch }),
  });

  const capabilities = {
    db: { doc, collection: (prefix) => ({ get: () => call("GET", prefix.endsWith("/") ? prefix : prefix + "/") }) },
    // Templated from the artifact's real visibility by content.py. Telling a
    // public page's visitor they are the owner makes every page render an
    // editing UI whose writes the server then refuses -- the page should know
    // it is read-only and degrade, which is what canEdit() is for.
    user: { isOwner: () => __OWNER__, canEdit: () => __OWNER__, id: () => "owner" },
    downloads: {
      save: async ({ filename, data }) => {
        const url = URL.createObjectURL(new Blob([data]));
        const anchor = Object.assign(document.createElement("a"), { href: url, download: filename });
        anchor.click();
        URL.revokeObjectURL(url);
      },
    },
  };

  window.claude = { use: async (name) => capabilities[name] ?? null };
  window.storage = capabilities.db;
})();
```

Replace the `SHIM` constant in `content.py` with the file's contents wrapped in `<script>` tags,
read once at import via `importlib.resources`, and add `"src/artifacts/*.js"` to the wheel include
list in `pyproject.toml`. `__OWNER__` is substituted per request with `true` for a private artifact
and `false` for a public one; `__STATE_BASE__` with `/t/<token>/_state/`, using the token the request
presented, which `_load` has just verified (Task 6b) — a relative URL would follow the page if it
rewrote its own path with `history.pushState`. Substitute each literal token, never string-format
the whole script, and assert in a test that no `__` token survives into a served page. Both
substitutions happen only on the live shim; the inert shim carries neither.

- [ ] **Step 6: Add the agent read-back route**

```python
# add inside agent_router in src/artifacts/api.py
from artifacts import state

    @router.get("/artifacts/{artifact_id}/state/{path:path}")
    def read_state(artifact_id: str, path: str, agent: str = Depends(caller)):
        try:
            store.get_owned(db, agent, artifact_id)
            doc = state.get_scanned(db, artifact_id, path)
        except store.NotFound:
            raise HTTPException(404, "no such artifact")
        except state.SecretInState as found:
            raise HTTPException(
                409,
                f"refused: state of {found.artifact_id} matches "
                f"{[f.shape for f in found.findings]}; look at it in the gallery",
            )
        if doc is None:
            raise HTTPException(404, "no such document")
        return doc
```

- [ ] **Step 7: Run every test**

Run: `uv run pytest -v` · Expected: all pass, including every test in `test_state.py`.

- [ ] **Step 8: Commit**

```bash
git add src/artifacts/state.py src/artifacts/shim.js src/artifacts/content.py \
        src/artifacts/api.py pyproject.toml tests/test_state.py
git commit -m "feat: a page keeps its own answers, and an agent can read them back safely"
```

---

### Task 8: The gallery shows the list without ever rendering an agent's markup

**Files:**
- Create: `src/artifacts/gallery.py`, `tests/test_gallery.py`
- Modify: `src/artifacts/app.py`
- Test: `tests/test_gallery.py`

**Interfaces:**
- Consumes: `Database`, `store`.
- Produces: `artifacts.gallery.gallery_router(db) -> APIRouter` serving `GET /` (list) and
  `GET /a/{artifact_id}` (detail: an iframe pointing at `store.content_url(id, read_token)`, the
  only page anywhere that carries the capability token — Task 6b);
  no in-app identity check: Authelia gates this host at the ingress, and `/api/v1` already
  rejects a `Remote-User` header outright (Task 5). The `Remote-User` header in these tests is
  only there to mimic a gated request; nothing reads it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gallery.py
from fastapi.testclient import TestClient
from artifacts import store
from artifacts.app import create_gallery_app

XSS = '"><img src=x onerror=alert(1)>'


def test_the_list_escapes_a_hostile_title(db):
    store.publish(db, "ops-seat", XSS, "<!doctype html><html><head></head><body>x</body></html>")
    page = TestClient(create_gallery_app(db)).get("/", headers={"Remote-User": "jeremy"}).text
    assert "<img src=x" not in page
    assert "&lt;img src=x" in page or "&#" in page


def test_the_detail_page_frames_the_content_origin_and_inlines_nothing(db):
    published = store.publish(
        db, "ops-seat", "Board", "<!doctype html><html><head></head><body>secret-body</body></html>"
    )
    page = TestClient(create_gallery_app(db)).get(
        f"/a/{published.id}", headers={"Remote-User": "jeremy"}
    ).text
    token = store.read_token(db, published.id)
    assert f'src="{store.content_url(published.id, token)}"' in page
    assert "secret-body" not in page


def test_the_gallery_sets_its_own_strict_headers(db):
    response = TestClient(create_gallery_app(db)).get("/", headers={"Remote-User": "jeremy"})
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "frame-src https://*.tenfourty.com" in csp
    assert "frame-src https:;" not in csp


def test_an_unknown_artifact_gets_a_404_not_a_frame(db):
    """An unchecked id renders a frame pointing at an invented subdomain."""
    response = TestClient(create_gallery_app(db)).get(
        "/a/aaaaaaaaaaaaaaaaaaaaaaaaaa", headers={"Remote-User": "jeremy"}
    )
    assert response.status_code == 404
    assert "<iframe" not in response.text


def test_the_detail_page_escapes_the_title_it_now_shows(db):
    published = store.publish(
        db, "ops-seat", XSS, "<!doctype html><html><head></head><body>x</body></html>"
    )
    page = TestClient(create_gallery_app(db)).get(
        f"/a/{published.id}", headers={"Remote-User": "jeremy"}
    ).text
    assert "<img src=x" not in page
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_gallery.py -v` · Expected: FAIL, 404 on `/`.

- [ ] **Step 3: Implement, escaping every interpolation**

Ruling: no template engine. Two pages of a handful of fields do not need Jinja, and an earlier draft
of this task listed template files it then never used. Every interpolation goes through `escape()`
explicitly and a test proves a hostile title comes back inert. If the gallery grows past a few fields,
introducing Jinja with `autoescape=True` is a contained change — do that rather than adding a sixth
hand-escaped field.

```python
# src/artifacts/gallery.py
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from markupsafe import escape
from artifacts.db import Database
from artifacts.store import content_domain, content_url

def gallery_headers() -> dict[str, str]:
    """Built per call, because the content domain is read from the environment
    at call time (store.content_domain())."""
    return {
        "content-security-policy": (
            "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
            # frame-src is the content domain only. `https:` would let any bug
            # that reaches this attribute frame anything on the internet,
            # inside the one page that IS logged in.
            f"frame-src https://*.{content_domain()}; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        ),
        "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
    }


def gallery_router(db: Database) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        with db.connect() as conn:
            rows = conn.execute(
                # Bounded: an unpaginated list is fine at ten artifacts and a
                # page nobody can load at ten thousand.
                "select id, title, updated_at from artifact"
                " order by updated_at desc limit 200"
            ).fetchall()
        items = "".join(
            f'<li><a href="/a/{escape(row[0])}">{escape(row[1])}</a>'
            f"<time>{escape(str(row[2]))}</time></li>"
            for row in rows
        )
        return HTMLResponse(
            f"<!doctype html><title>Artifacts</title><ul>{items}</ul>",
            headers=gallery_headers(),
        )

    @router.get("/a/{artifact_id}", response_class=HTMLResponse)
    def detail(artifact_id: str) -> HTMLResponse:
        # Look the artifact up rather than framing whatever the URL said: an
        # unchecked id renders a frame pointing at an invented subdomain, and
        # the title is wanted on the page anyway.
        with db.connect() as conn:
            row = conn.execute(
                "select title, read_token from artifact where id = %s", (artifact_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(404, "no such artifact")
        # The one place the capability token is rendered: a page only Jeremy's Authelia session
        # can load, with Referrer-Policy: no-referrer so it never leaves in a Referer.
        src = escape(content_url(artifact_id, row[1]))
        return HTMLResponse(
            f"<!doctype html><title>{escape(row[0])}</title>"
            f"<h1>{escape(row[0])}</h1>"
            f'<iframe src="{src}" style="width:100%;height:100vh;border:0"></iframe>',
            headers=gallery_headers(),
        )

    return router
```

Add `markupsafe` with `uv add markupsafe`, and include the router in `create_gallery_app`.

- [ ] **Step 4: Run every test**

Run: `uv run pytest -v` · Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/artifacts/gallery.py src/artifacts/app.py tests/test_gallery.py pyproject.toml
git commit -m "feat: the gallery lists pages as text and frames them from the content domain"
```

---

### Task 9: The decisions page publishes here

**Files:**
- Create: `~/repos/artifacts/clients/artifacts-publish` (Python 3 stdlib only, no dependencies)
- Modify (in the **homelab** repo): `scripts/ops-decisions-page.sh`,
  `scripts/tests/ops-decisions-page-selftest.sh`, `scripts/README.md`
- Test: `scripts/tests/ops-decisions-page-selftest.sh` (extended)

**Interfaces:**
- Consumes: the agent API from Tasks 5 and 7.
- Produces: `artifacts-publish --title T --file F [--artifact-id ID] [--state-dir D]` printing the
  URL on stdout and, with `--state-dir`, recording `artifact.id`, `artifact.revision` and
  `artifact.url` there so the next edit can send the revision it is actually editing;
  `scripts/ops-decisions-page.sh --publisher` publishing `index.html` and recording the returned
  URL in `~/.local/state/homelab-ops/decisions-page/artifact.url`.

**Constraint:** the key is read from a file path given by `ARTIFACTS_KEY_FILE`, never from a
command line, never printed. The script must refuse to run if the variable is unset.

- [ ] **Step 1: Write the client**

Ruling: Python, not POSIX shell. The shell draft of this client put the key on a command line —
`curl -H "Authorization: Bearer $(cat "$ARTIFACTS_KEY_FILE")"` — which every process running as this
user can read out of `/proc/<pid>/cmdline` while the request is in flight, and which this task's own
Constraint forbids. Shell has no good way to hand curl a header without argv, and the draft already
shelled out to `python3` twice, so "no dependencies" was never buying shell anything. Python reads the
key into a variable and puts it in a header; it never becomes an argument, and `cat` is never pointed
at a credential file.

```python
#!/usr/bin/env python3
"""clients/artifacts-publish -- publish or update a page; prints the URL.

The key is read from the file named by ARTIFACTS_KEY_FILE, into a variable,
and goes out in a header. It is never an argument, never printed, and never
part of an error message: a traceback from here lands in an agent transcript.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title")
    parser.add_argument("--file", required=True)
    parser.add_argument("--artifact-id")
    parser.add_argument("--state-dir", help="where to record the id and revision for the next edit")
    args = parser.parse_args()

    key_file = os.environ.get("ARTIFACTS_KEY_FILE")
    if not key_file:
        print("artifacts-publish: set ARTIFACTS_KEY_FILE to a file holding the agent key",
              file=sys.stderr)
        return 64
    key = open(key_file).read().strip()
    api = os.environ.get("ARTIFACTS_API", "https://artifacts.tenfourty.site")
    body = open(args.file, encoding="utf-8").read()

    # The revision comes from the state directory, never from a flag: the caller
    # holding a stale one in its shell is exactly the lost update the server's
    # 409 exists to prevent, and there is no reason to let it pass one in.
    revision = ""
    if args.state_dir and args.artifact_id:
        try:
            revision = open(os.path.join(args.state_dir, "artifact.revision")).read().strip()
        except FileNotFoundError:
            revision = ""

    if args.artifact_id:
        url = f"{api}/api/v1/artifacts/{args.artifact_id}"
        payload, method = {"body": body, "expected_revision": revision}, "PATCH"
    else:
        url = f"{api}/api/v1/artifacts"
        payload, method = {"title": args.title or "Untitled", "body": body}, "POST"

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method=method,
        headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:2000]
        print(f"artifacts-publish: {error.code} {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print(f"artifacts-publish: cannot reach {api}: {error.reason}", file=sys.stderr)
        return 1

    if args.state_dir:
        # Without this the next run sends an empty expected_revision and the
        # server answers 409 forever. An earlier draft read these two files and
        # nothing ever wrote them.
        os.makedirs(args.state_dir, exist_ok=True)
        for name, value in (("artifact.id", answer["id"]),
                            ("artifact.revision", answer["revision"]),
                            ("artifact.url", answer["url"])):
            with open(os.path.join(args.state_dir, name), "w") as handle:
                handle.write(value + "\n")

    print(answer["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Make it executable (`chmod +x clients/artifacts-publish`).

- [ ] **Step 2: Write the failing selftest case (homelab repo)**

Add to `scripts/tests/ops-decisions-page-selftest.sh`, following the file's existing fixture style:

```sh
# --publisher builds the page and calls the publisher with the file, never the key
test_publisher_flag_passes_a_file_and_never_the_key() {
  fake_bin="$(mktemp -d)"
  cat > "$fake_bin/artifacts-publish" <<'EOF'
#!/bin/sh
echo "$@" > "$FAKE_ARGS_FILE"
echo "https://abcdefghijklmnopqrstuvwxyz.tenfourty.com/"
EOF
  chmod +x "$fake_bin/artifacts-publish"
  FAKE_ARGS_FILE="$(mktemp)"; export FAKE_ARGS_FILE
  PATH="$fake_bin:$PATH" OPS_DECISIONS_PAGE_DIR="$(mktemp -d)" \
    ARTIFACTS_KEY_FILE=/dev/null scripts/ops-decisions-page.sh --publisher
  grep -q -- "--file" "$FAKE_ARGS_FILE" || fail "publisher was not given a file"
  # NOT `grep -qv`: that succeeds when ANY line lacks the pattern, so it passes
  # happily on a file whose first line carries the key. Negate the match itself.
  if grep -q -- "art_" "$FAKE_ARGS_FILE"; then fail "a key reached the command line"; fi
  [ -s "$OPS_DECISIONS_PAGE_DIR/artifact.url" ] || fail "the returned URL was not recorded"
}
```

- [ ] **Step 3: Run it and watch it fail**

Run: `cd ~/repos/homelab && scripts/tests/ops-decisions-page-selftest.sh`
Expected: FAIL, `--publisher` is an unknown flag (exit 64).

- [ ] **Step 4: Add the flag to `scripts/ops-decisions-page.sh`**

In the flag loop add `--publisher) mode=publish ;;`, and after the page is built:

```sh
if [ "$mode" = publish ]; then
  : "${ARTIFACTS_KEY_FILE:?ops-decisions-page: set ARTIFACTS_KEY_FILE to the agent key file}"
  id_file="$STATE/artifact.id"
  if [ -s "$id_file" ]; then
    url="$(artifacts-publish --file "$STATE/index.html" \
            --artifact-id "$(cat "$id_file")" --state-dir "$STATE")"
  else
    url="$(artifacts-publish --title 'Decisions' --file "$STATE/index.html" --state-dir "$STATE")"
  fi
  echo "ops-decisions-page: published to $url"
fi
```

- [ ] **Step 5: Run the selftest**

Run: `scripts/tests/ops-decisions-page-selftest.sh` · Expected: every case passes.

- [ ] **Step 6: Migration check — answers move by bead id, never by wording**

Create `scripts/tests/artifacts-answer-migration-check.sh` in the homelab repo. It asserts a
**positive count** and refuses a zero-match run, per `agent_docs/verification-and-gating.md`:

```sh
#!/usr/bin/env bash
# Every answer that existed before the move is readable from the new host, matched by bead id.
# Usage: ARTIFACTS_KEY_FILE=… scripts/tests/artifacts-answer-migration-check.sh <artifact-id> <expected-answers.json>
set -euo pipefail
: "${ARTIFACTS_KEY_FILE:?set ARTIFACTS_KEY_FILE to the agent key file}"
: "${ARTIFACTS_API:=https://artifacts.tenfourty.site}"
artifact_id="${1:?artifact id}"; expected_file="${2:?a JSON object of bead id -> choice}"
expected_count="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$expected_file")"
[ "$expected_count" -gt 0 ] || { echo "migration-check: nothing expected; refusing a vacuous pass" >&2; exit 2; }
found=0; mismatched=0
while IFS=$'\t' read -r bead choice; do
  # The key goes into a variable inside python and out in a header. `curl -H
  # "Bearer $(cat …)"` would put it in argv, readable from /proc by every
  # process of this user for as long as the request is in flight.
  actual="$(python3 -c '
import json, os, sys, urllib.request
key = open(os.environ["ARTIFACTS_KEY_FILE"]).read().strip()
request = urllib.request.Request(sys.argv[1], headers={"authorization": f"Bearer {key}"})
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        print(json.load(response).get("choice", ""))
except Exception:
    print("")
' "$ARTIFACTS_API/api/v1/artifacts/$artifact_id/state/answers/$bead")" || actual=""
  if [ "$actual" = "$choice" ]; then found=$((found + 1))
  else mismatched=$((mismatched + 1)); echo "migration-check: $bead expected '$choice', got '$actual'" >&2; fi
done < <(python3 -c 'import json,sys
for bead, choice in json.load(open(sys.argv[1])).items(): print(f"{bead}\t{choice}")' "$expected_file")
echo "migration-check: $found/$expected_count answers matched, $mismatched mismatched"
[ "$found" -eq "$expected_count" ] && [ "$mismatched" -eq 0 ]
```

Build `expected-answers.json` from the old page's exported state before the cutover, keyed by bead
id only — never by the wording of a question, which changes between rebuilds.

- [ ] **Step 7: Commit (two repos, separately)**

```bash
cd ~/repos/artifacts && git add clients/artifacts-publish &&
  git commit -m "feat: a client publishes a page without the key ever reaching a command line"
cd ~/repos/homelab && git add scripts/ops-decisions-page.sh scripts/tests/ scripts/README.md &&
  git commit -m "feat(ops): the decisions page can publish to our own host" -- scripts/
```

---

## What this plan does not cover

Feeds and version-bound grants; the twelve-case browser escape suite; the Kubernetes manifests,
ingress class, wildcard certificate and Cloudflare route; VolSync and the restore drill; the
gallery's key-management and visibility screens; ggshield in the deploy pipeline. Each is a
following part, written once this one is green.

Two of those deferrals have a consequence worth stating rather than discovering:

- **No `Clear-Site-Data` on a version change (design B4).** The design has the first load after a
  version change carry `Clear-Site-Data: "storage","cache","executionContexts"`, so nothing a
  previous version registered outlives it. Knowing a viewer's *previous* version needs per-viewer
  state this part does not have, so it is deferred with version-bound grants — and until then, what
  a page wrote to `localStorage` on one version is readable by the next version at the same address.
  That is the same origin either way, so it is not a cross-artifact leak; it is a
  "this page's old code left something behind" leak. Do not describe versions as isolated from each
  other until this lands.
- **No pending-on-widening (design D11).** `artifact.publish()` from page JavaScript is not in this
  part at all, so the live/pending distinction has nothing to gate yet. When page-driven publishing
  arrives, it arrives with D11, not after it.
- **No public viewer, so "public" does not yet mean viewable by the public.** A page is framed-only
  and the gallery is Authelia-gated; the ungated frame host (`links.tenfourty.com`) and the
  expiring link-share token (D8) are deferred with link-sharing and the visibility screens. In this
  part, `public` means exactly one thing: the page cannot write its state. Every page, whatever its
  visibility, is read with its capability token (Task 6b). The part that builds the public viewer
  decides how a public page is addressed.

And one consequence of this part that the deployment part must carry:

- **Health probes must name the gallery host.** The host router fails closed (Task 6), so a
  Kubernetes `httpGet` probe — which sends the pod's IP as `Host` — gets the router's 404 and the pod
  crash-loops. Probes send `httpHeaders: [{name: Host, value: artifacts.tenfourty.site}]`.
- **No access log may record a content path.** Every framed load is `GET /t/<token>/…`, so uvicorn's
  access log (on by default) and the content ingress controller's access log would each write the
  capability token for every page view, and operator agents read pod logs — that is a route from the
  token to an agent's transcript. Run uvicorn with `--no-access-log` (or a filter that rewrites
  `/t/[^/]+/` to `/t/[REDACTED]/`), and configure the content ingress's log format the same way,
  before the content hostname resolves. Verify it by loading a page and grepping both logs for the
  token: zero lines.
- **uvicorn must trust the ingress for the scheme.** The content app refuses trailing-slash
  redirects (Task 6b), so no Location it emits carries a token; but any future redirect built from
  the request URL inherits the scope's scheme, which is `http` behind a TLS-terminating ingress
  unless uvicorn runs with `--proxy-headers --forwarded-allow-ips=<the ingress>`. Set both.

## Self-review

- **Spec coverage:** D3 (Tasks 5, 9), D5 (Task 7), D11 old-versions-inert (Task 6), D12 local
  scanner (Tasks 4, 7), D14 scoped keys and no agent-callable sharing (Tasks 3, 5 — sharing routes
  simply do not exist), D15 state scanning (Task 7), §5.4 framed-only (Task 6), §5.5 header set
  (Task 6), §5.6 `/feeds` same-origin — deferred with feeds, but the same guard is proven on
  `/_state` in Task 7. §4.3 push endpoint, §4.5 feeds, §6.2 escape suite: next part.
- **Placeholders:** none. Two were found on review and removed — a broken SQL line in Task 2 and
  an unfinished migration snippet in Task 9, both now written out in full.
- **Type consistency:** `Published` and `ArtifactRow` are used with the same field names in Tasks
  5–8; `store.get_owned`, `store.list_for_agent`, `state.get`, `state.get_scanned` and
  `state.collection` are named identically wherever they appear.

## MCP sign-in for people's agents: the build in slices (planned 8 Nov 2026)

**Goal:** a person adds Ream to Claude.ai (or any MCP app) as a remote connector, signs in once
through the deployment's OIDC provider, and their Claude then reads, publishes, comments, asks and
passes on answers as "Jeremy, via claude.ai", with no key. Keys stay for machines, offices and CI.

**Spec:** `docs/agent/specs/2026-11-08-mcp-oauth-design.md` (sections cited as §n). Decisions:
`docs/decisions.md`, "Ream in Claude.ai: the six sign-in choices (8 Nov 2026)".

**Rules for every slice.** Each lands on its own, on a `ci/` branch first, with no change for a
deployment that sets nothing: `/mcp` needs `REAM_MCP_PUBLIC_URL`, the sign-in needs
`REAM_MCP_SIGN_IN=on`, both off by default. Tests run in CI on both databases (never on the management machine). New
tables go in `schema.sql` and one new `db._SQLITE_STEPS` step, and join the copier test's list.
Settings are `Settings` fields with `REAM_*` names in `_ENV`; a half-set group refuses to start.
Docs land with the code (the agent guide and `agents.txt` from slice 1; the runbook
`docs/agent/runbooks/mcp-sign-in.md` with slice 5). Sizes are S, M or L, never a time.

### Slice 1 (M): `/mcp` speaks MCP, with today's keys, and reads

- **Builds:** `ream/mcp.py`, stateless Streamable HTTP (§6): `initialize` for 2025-03-26 to
  2025-11-25, the 2026-07-28 header rules, JSON responses only, `GET`/`DELETE` 405, the 12 MiB cap,
  the `Origin` check (403). `ream/mcp_tools.py` with the read tools (`whoami`, `list_pages`,
  `read_page`, `get_template`, `comments`, `questions`, `whats_new`), two resources, two prompts,
  annotations, the labelled envelope for page text (§15). Keys only, with a key's reach.
  `REAM_MCP_PUBLIC_URL`, `REAM_MCP_ACCEPT_KEYS`.
- **Tests:** a recorded request from each revision; version mismatch (400, -32020); foreign
  `Origin` 403, none passes; `GET` 405; each read tool against its route for the same key; another
  agent's closed page answers as "no such page"; `/mcp` 404 with the setting unset.

### Slice 2 (M): the writing tools, with today's keys

- **Builds:** `publish_page`, `update_page`, `organise_page`, `delete_page`, `comment`, `ask`,
  `close_question`, `record_answer` (a key's existing relay sources), each calling its route's
  module functions (§11.3), with the session label rule (§11.1).
- **Tests:** each tool against its route (same rows, events and refusal words); a secret refused;
  `delete_page` is `destructiveHint` and restorable; a test fails on any tool not in §11.3's table.

### Slice 3 (M): Ream says how to sign in, and knows which app is asking

- **Builds:** the two `/.well-known` documents and the 401 challenge (§4); `mcp_client`; the client
  metadata fetch with its fences on `webhook.py`'s address guard; open registration with its limits;
  the trusted and allowed host lists; keys refused at `/mcp` while sign-in is on (§7).
- **Tests:** both documents field by field (Claude's needs); fetch refusals with literal private,
  loopback, link-local and redirecting answers, oversized or non-JSON documents, a `client_id` one
  byte off; the 11th registration in an hour is 429; the allowed list refuses before fetching.

### Slice 4 (M): a person-and-app agent and its reach

- **Builds:** `mcp_agent`, `mcp_app` (§8); the agent name rule and the display "<owner name>, via
  <host>"; the `Reach` value (§11.2) threaded through the functions the tools call, keys passing
  today's reach unchanged; permissions gating tools (§11.3).
- **Tests:** apps made directly in the database; an app reads a page closed to agents, cannot
  change an office's page, can change one by the person's other app, comments on an office's page
  as "Jeremy, via claude.ai"; a read-only app lists no write tools; every API test unchanged.

### Slice 5 (L): signing in, consent and tokens

- **Builds:** `ream/oidc.py` (code flow with PKCE, state, nonce, secret from file, claim checks,
  the single-reader check, §13); `ream/oauth.py` (authorize, callback, the consent screen with three
  boxes and remembered consent, the 200 interstitial with `iss`, `mcp_signin`, `mcp_token`, code
  exchange, refresh with strict rotation and reuse detection, `/oauth/revoke`, audience, idle lapse
  and cap, §9-10); `rmo_`/`rmr_` in `secret_shapes.txt`; clean-up; `REAM_OIDC_*`; the runbook.
- **Tests:** a fake OIDC provider behind the fetch seam; the whole flow with the literal Claude.ai
  callback; bad `nonce`/`aud`/`iss`/expiry refused; a non-owner refused, nothing stored; a code or
  refresh token used twice ends the app; a wrong verifier, another client or another resource
  refused; `rmo_` refused under `/api/v1`; remembered consent skipped only for the same or narrower
  request and never for a registered app; cross-site consent refused; the consent page in the
  browser tests; lapse and cap on a moved clock.

### Slice 6 (S): connected apps in "Machines and keys"

- **Builds:** the "Connected apps" section with Revoke (§10), `POST /machines/apps/{id}/revoke`,
  `artifacts-admin list-apps` and `revoke-app`.
- **Tests:** Revoke stops the next call and forgets consent; lapsed apps listed as lapsed; no token
  or hash on the page.
- **Ledger (9 Nov 2026):** built on `ci/mcp-slice6`: `mcp_apps.listed`, `_apps_section` and
  `POST /machines/apps/{id}/revoke` (gallery), `artifacts-admin list-apps` / `revoke-app`; both end
  an app through `oauth.end_app` (reasons `owner`, `admin`). No `/api/v1` twin: §10 gives agents
  only `POST /oauth/revoke` (the app ending itself) and says no tool lists or revokes apps.

- **Ledger, slice 7 (9 Nov 2026):** built on `ci/mcp-slice7`: `decisions.MCP_SOURCE` (`mcp`, a
  core stored source, in `CORE_SOURCES` and reserved against plug-ins, not in `ANSWER_SOURCES`);
  `record_elsewhere(..., by_app=True)` is the only writer and takes `mcp` and nothing else, a key's
  path refuses `mcp`. `record_answer` for an app with `relay` goes through `api.record_relay(...,
  by_app=True)`: any open question on any page the app reads (`decisions.get_any`), forms per
  field via `formanswer.relay`; the review's first-verdict rule is untouched. Gallery:
  "Jeremy said (b) in claude.ai" with "Recorded by Jeremy, via claude.ai" (host from the app).

### Slice 7 (S): his Claude passes on his answers

- **Builds:** the core stored source `mcp`; `record_answer` for an app with `relay` on any open
  question put to the person (§11.5); the gallery wording "Jeremy said (b) in claude.ai".
- **Tests:** the asking office's wait wakes with the relay; a key cannot claim `mcp`; empty words
  refused; no `relay`, no tool; review verdict rules hold.

### Slice 8 (M): asking on any page he can read

- **Builds:** nullable `decision.asked_by` (null is the page's agent); `ask` from an app on a page
  it does not own; the answer goes to the asker; the page's agent is told (§11.4).
- **Tests:** every decision test passes with `asked_by` null; the app's `whats_new` wakes, not the
  office's wait; only the asker closes it.
- **Built (9 Nov 2026):** `tests/test_ask_anywhere.py`; the design is §11.4 (status note there). The
  page's agent is told by a `question_asked` event, not woken by `ream wait`.

### Slice 9 (M): the homelab turns it on, and the owner tries it from Claude.ai

- **Needs the deployment first:** the setup in §16.
- **Trial, by hand:** the `/.well-known` documents answer from outside (bypass and Cloudflare);
  connect read-only and ask "what is waiting on me"; reconnect with all three boxes, publish from a
  chat, pass on an answer; revoke and see the next call fail. The report says what it checked end
  to end. The company build (§18) follows as its own plan.

## Ledger

Work built after the tasks above, newest last. Each line says what a reader can now do.

- **Where an answer goes (29 Oct 2026; 9a5cbed, f485a33).** An open question says whether a
  session of the asking office is waiting now, or where the answer goes otherwise (Slack, delayed,
  or nowhere). `GET /api/v1/listening` and `artifacts-decide listening` say the same for the
  calling key's agent, and a fresh session can check before arming a second wait. Liveness is
  `seat_read.held_until`, read before the hold starts. Spec `specs/2026-10-29-where-an-answer-goes.md`.
- **The pickup line (29 Oct 2026; e768d14, 1f60eb8).** An agent may say in one line when it acts
  on answers (`artifacts-decide pickup`), shown as "<agent> says: ..." on its cards. A rename that
  merges two agents keeps the newer line and counts the merge.
- **Pin a chart, an image or a table (29 Oct 2026, slice 2 of pinned comments).** He can press Pin
  (or C) on a page, outline an element and comment on it; an agent can send
  `{"kind":"element","id":...}` or `artifacts-decide comment PAGE "..." --element ID`. A pinned
  element shows a numbered badge, says which version it was pinned on, names a table cell by its
  row and column and an image by its alt text, shows the original and the current text when it was
  edited, and goes outdated if a republish removes it. Creating a comment returns where the pin
  stands. Spec `specs/2026-10-29-pinned-comments.md` (slice 2).
- **A slightly reworded pinned quote is `edited` (29 Oct 2026).** Under its own heading, a quote
  gone from the page is found approximately (similarity 0.8, at least 24 characters, bounded
  work) and reported `edited` with the new words as `anchor_current`; never re-attached from
  another section. Server-side only; the browser shim is unchanged. Spec as above, "As built:
  approximate re-find".
- **Pictures on a comment, server half (29 Oct 2026, slice 3 of pinned comments).** A comment takes
  up to 4 PNG, JPEG or WebP pictures (5 MB, 4000 px a side), uploaded first and named in `images`;
  the service sniffs the bytes, reads the size from the header, and stores a rewrite without EXIF or
  text, never decoding pixels. Agents: `artifacts-decide comment ... --image PATH`, and `comments`
  caches pictures locally and prints the path. A reader's browser gets one only from the page's own
  content origin, with an hour-long signed link, `nosniff` and a sandbox CSP. The gallery's attach
  control and thumbnails are not built. Spec `specs/2026-10-29-pinned-comments.md` (slice 3, server).
- **Small fixes (29 Oct 2026).** A pickup line of only invisible filler is refused like an empty one; `artifacts-decide pickup` takes a line starting with "-" (after `--`); an element pin with under 4 visible characters of evidence no longer reads current from a stray match. Specs `where-an-answer-goes` section 6 and `pinned-comments` slice 2.
  content origin, with an hour-long signed link, `nosniff` and a sandbox CSP. Spec
  `specs/2026-10-29-pinned-comments.md` (slice 3, server).
- **Pictures on a comment, gallery half (29 Oct 2026, slice 3 of pinned comments).** He can paste,
  drop or choose up to four pictures on a comment or a reply, see a removable thumbnail for each,
  and send them with his words; a picture over 4000 px a side, 16 megapixels or 5 MB is shrunk in
  the browser first when it can be. Pictures show as thumbnails in the comments panel (his own held
  comments too) and open larger in place; they load only from the page's content origin, which the
  gallery CSP now names (`img-src 'self' https://*.<content domain>`). Under the control: "Images
  are not checked for secrets." Without scripts the form is the plain one. Spec as above, "As
  built, slice 3 (gallery)".
- **One emit point, part 1 (6 Nov 2026, slice 4 of core-and-plugins).** `events.emit(conn, Event)`
  is the single call that records "this happened": the journal row (journal kinds), then per
  registered sink either a row in the new core `outbox` table or the sink's own staging, in the
  action's transaction, and an `Emission.fire()` for after-commit sinks (never raises). The nudge
  and the partner bridge are the two in-core sinks (`sinks.py`); `events.BRIDGE_KINDS` is the
  partner's kind list. `partner_outbox` is left in place and its sender unchanged, so old rows
  keep delivering; a core sender thread drains `outbox`. Two call sites converted
  (`formanswer.py`, the set-aside loop in `store.py`); the other ~11 are part 2. Spec
  `specs/2026-11-04-core-and-plugins-design.md`, slice 4.
- **One emit point, part 2 (6 Nov 2026, slice 4).** Every fan-out site now calls `events.emit` and
  `.fire()`s the Emission after commit: `decisions.py` (ask incl. born-set-aside, record_elsewhere,
  answer, and `_close_row` for replaced/withdrawn), `store.py` (set-aside and reopen loops, which
  hand their Emissions to the callers to fire after the commit; `purge_expired`), `comments.py`
  (comment_added to owner and repliers, thread_resolved, thread_reopened). Journal rows, nudge
  payloads and partner rows are as before; the `partner.enabled()` gates at call sites are gone
  (`partner.enqueue` returns early when off). Left: `partner_in.answer_here` calls
  `partner.enqueue_foreign`, which has no artifact or agent (a foreign copy), its own version
  space and is never gated, so it does not fit an `Event`; it stays a direct partner-module call.
  `receipts` calls `decisions._close_row` under `DONE_CLOSES = False` and drops the returned
  Emission (bridge kinds have no after-commit sink). Behaviour change: a raising nudge no longer
  propagates out of `comments.add` (the nudge is an after-commit sink, so it is logged and
  counted). Review fixes: built-in sinks register under the lock and the flag is set last; the
  sender's completion and failure updates touch only a row it still leases; `sinks.start` is
  idempotent and `sinks.stop` runs at lifespan shutdown; `deliver` is bounded by
  `DELIVER_TIMEOUT_SECONDS` (30 s, under the 60 s lease) via a per-call daemon thread, a timeout
  being an ordinary failure with backoff. `artifacts-admin outbox` prints pending, parked and
  delivered counts by sink. Still later: the per-sink circuit breaker, the generic webhook sink.
- **Drop the CHECKs, part 1 (6 Nov 2026, slice 5 of core-and-plugins).** The CHECKs on
  `decision.answer_source` and `event.kind` are gone. Postgres: `schema.sql` no longer creates them
  or re-adds them, and a `do $$` block drops any check on those two columns found in `pg_constraint`
  by what it tests (so the inline-created and the re-added constraint both go), idempotent.
  SQLite: step 21 (`_drop_house_checks`) strips both clauses from the stored table text in one
  `writable_schema` pass (`event` is rewritten once; step 16's `receipt_set` and step 18's
  `thread_reopened` steps already shipped, so there is no ordering issue), then raises and rolls
  back, leaving `user_version`, if any `<column> in` is left in either table's SQL. No rebuild
  fallback: an unknown shape fails start-up loudly instead. Code validates: `events.append` raises
  `ValueError` for a kind not in `events.KINDS`; `decisions.require_stored_source` raises
  `InvalidSource` for a value outside `STORED_SOURCES` (the relay and answer paths already refused
  such values before the SQL, with the same 422 as before; the CHECK was only a backstop that
  surfaced as a 500); `formanswer.relay` refuses it with `SubmitRefused` (422). Old step tests that
  expected the CHECK text after a full migration were updated. Part 2: the
  `decisions.stored_sources()` registry.
- **MCP's writing tools, with today's keys (9 Nov 2026; slice 2 of MCP sign-in).** An agent
  speaking MCP at `/mcp` can publish a page, update it whole or one section, rename, pin, archive,
  file, delete and restore it, comment, resolve and reopen a thread, ask and withdraw a question,
  and pass on an answer the person gave elsewhere (`record_answer`, a key's relay sources only,
  his words required). Each tool builds its route's body and calls the route's module function,
  so refusals, secret scanning and events match the route's; a write is labelled with its
  `session` argument, the `X-Ream-Session` header, or `mcp/<date>`. `docs/mcp.md`.
- **Ream says how to sign in, and knows which app is asking (9 Nov 2026; slice 3 of MCP
  sign-in).** With `REAM_MCP_SIGN_IN=on` beside `REAM_MCP_PUBLIC_URL`, the public host serves the
  protected-resource document (both paths) and the authorization-server document (§4), and `/mcp`
  answers a missing or unknown credential with 401 and the `resource_metadata` challenge. A key
  there gets the same 401 with the "Agent keys are not accepted" words and a WARN
  `reason=key_on_mcp` naming its public id, unless `REAM_MCP_ACCEPT_KEYS=1`. `ream/mcp_clients.py`
  reads a client metadata document through `webhook.fenced_request` (the address guard shared with
  the webhook sink: public addresses only, pinned, no redirect, `application/json`, 64 KiB, 5 s,
  kept 60 s in the new `mcp_client` table, SQLite step 24) and checks its `client_id` byte for byte
  and its return addresses; `POST /oauth/register` registers public loopback clients, 10 an hour
  per address (IPv6 by /64), 1000 stored, an unused one gone after 24 hours.
  `REAM_MCP_ALLOWED_CLIENT_HOSTS` refuses before anything is fetched or stored;
  `REAM_MCP_TRUSTED_CLIENT_HOSTS` only labels an app Verified. Until slice 5 builds
  `/oauth/authorize` and `/oauth/token`, `REAM_MCP_SIGN_IN=on` refuses to start, so no deployment
  advertises pages that are not there; with the settings unset nothing served changes.
- **A person-and-app agent and the apps a person lets in, stored (9 Nov 2026; first half of slice 4
  of MCP sign-in).** The `mcp_agent` and `mcp_app` tables (§8; SQLite step 25) and
  `ream/mcp_apps.py`: the first consent from an app makes `<person>--<host slug>` (`-2`, `-3` for
  another app on the same host; the slug cut, never the person, to fit `keys.AGENT_NAME`), never
  the owner id and never a name a key, a rename's old name or a page already answers to, and the
  same pair always gets its agent back. A consent stores its permissions (read, write, relay), a
  30-day idle lapse and a 90-day cap, one live app per person and app. `display_name` shows a
  person-and-app agent as "<owner name>, via <host>" (`settings.owner_name()`). Nothing calls them
  yet: `Reach` and permission gating are the second half.
- **A signed-in app's reach and permissions (9 Nov 2026; second half of slice 4 of MCP sign-in).**
  `ream/reach.py`: `Reach(agent, readable, writable_agents, can_relay, permissions)` (§11.2), one
  value per MCP message, a key's from `reach.of_key` (today's, widening nothing) and an app's from
  `mcp_apps.reach_of`. `/mcp` acts with it for the length of a message (`reach.acting`), and the
  store's one page check (`store._owned`, and `list_readable`) widens only for the agent acting
  with it: an app reads every page the owner can read, closed to agents included, never the
  trash, and changes the pages of any of the person's app agents; asking stays on its own pages
  (`widen=False`, until §11.4). `mcp.app_call` makes an app's call (slice 5's token check will);
  its permissions gate `tools/list` and `tools/call` as a key's do. An app's comment says
  "Jeremy, via claude.ai" (`author_name`; stored as its agent), its `whoami` says who it is shown
  as and its reach, its session defaults to `<host>/<date>`, its `whats_new` cursor is
  `mcp_app.events_seen`, and `record_answer` refuses it until source `mcp` (§11.5) exists.
  `rename_agent` refuses to rename a person-and-app agent or to rename a key's agent onto one's
  name. `/api/v1` is unchanged. (The checks outside `_owned` were widened afterwards; see the
  next entry.)
- **A signed-in app organises pages and reads questions (9 Nov 2026; closes the leftover of slice 4).**
  `store._scoped` and `store.move_many` now widen for the acting app as `_owned` does for writes:
  rename, pin, archive, file in a folder, delete and restore work on the pages of any of the
  person's app agents (`reach.writes()`), never an office's or a key agent's, and "not yours"
  still answers as "no such page". A filed page goes in a folder of its own agent (found or made
  per owner), since a folder shows only on its owner's pages. Destroy, give away and who may read
  (`purge`, `transfer`, `set_agents_can_read`) pass `widen=False`: never for an app. The
  `questions` tool lists, for an app, every open question on a page it can read (any live page:
  `decisions.list_for_agent(any_agent=True)`), which includes those it asked; a question on a
  binned page is not listed. A key's `questions` and organising are unchanged. Tests:
  `tests/test_mcp_organise.py`.
- **Signing in: the provider and the owner check (9 Nov 2026, slice 5a; b5156b5, 610f4c3, 55e11e7).**
  The server can sign the one reader in at an upstream OpenID Connect provider (`REAM_OIDC_*`,
  `src/ream/oidc.py`): code flow with PKCE, state and nonce, the secret read from a file, claims
  checked, the owner needs a verified email and a pinned subject, and the provider's endpoints are
  pinned to its origin with bounded fetches. Start-up refuses a half-set sign-in.
- **Signing in: consent and tokens (9 Nov 2026, slice 5b; 9abbfd6, e765b10, 740b050).** An app
  sends the person to `/oauth/authorize`; after the provider he sees a consent page with three
  boxes (read, write, relay), remembered consent skips it for the same or narrower request, and
  the app gets a one-time code and exchanges it at `/oauth/token` for `rmo_` and `rmr_` tokens,
  stored hashed (`mcp_token`, `mcp_signin`). `/mcp` takes the `rmo_` token; `/api/v1` refuses it;
  a code used twice ends the app.
- **Signing in: refresh, revoke, lapse and clean-up (9 Nov 2026, slice 5c).** An app refreshes at
  `/oauth/token` (one conditional update spends the old `rmr_`; a spent one presented again ends
  the app, reason `refresh_reuse`), can end itself at `/oauth/revoke`, lapses after 30 idle days
  (every `/mcp` call and refresh slides it) and never outlives the 90-day cap; the nightly
  clean-up drops finished sign-ins and spent or expired tokens and ends lapsed apps. Runbook
  `docs/agent/runbooks/mcp-sign-in.md`. Not built: Connected apps page and admin commands (slice 6).
- **Signing in: a back channel to the provider (9 Nov 2026; branch `ci/oidc-backchannel`).**
  `REAM_OIDC_BACKCHANNEL_URL` sends Ream's own discovery, token and key-set calls to another
  address (the provider's in-cluster service) with `Host` and `X-Forwarded-*` of the public
  issuer; issuer, endpoint-origin and `iss` checks are unchanged and the authorize address is
  never rewritten. Start-up refuses it without the issuer or with more than scheme, host and port.
- **Asking on any page he can read (9 Nov 2026, slice 8).** A connected app can ask a question on
  any page the person can read, not only its own. The card says "Asked by Jeremy, via claude.ai";
  the answer wakes the app's `whats_new`, not the page's agent, which gets a `question_asked`
  event (nudged as a comment is); only the app can withdraw it. A key still asks on its own pages
  only. New column `decision.asked_by` (null: the page's agent). Spec §11.4.
- **The GitGuardian second scan leaves the core (11 Nov 2026, step 9c of core-and-plugins).** Core no
  longer carries its own ggshield relay scan (`ggscan.py` and the `ggshield_*` settings are deleted):
  the house plug-in's scanner `gitguardian` is the second opinion on pages and agent comments, and a
  comment it gave no answer for is left unscanned for the sweeper. A deployment that still sets
  `REAM_GGSHIELD_*` starts as before. Ledger: `specs/2026-11-04-core-and-plugins-design.md`, step 9c.
- **Mailgun leaves the core (11 Nov 2026, step 9b of core-and-plugins).** Core no longer carries its
  own Mailgun sender (`mail._mailgun_send` and the `mailgun_*` and `mail_from` settings are deleted):
  the house plug-in's mail backend sends all mail, and with none registered mail is off, as on a fresh
  install. A deployment that still sets `REAM_MAILGUN_*` or `REAM_MAIL_FROM` starts, with one warning
  if no backend is loaded. Ledger: `specs/2026-11-04-core-and-plugins-design.md`, step 9b.
