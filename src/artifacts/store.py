"""Publishing, patching, reading and listing artifacts.

Every write goes through `_guard`: a body over `MAX_BODY_BYTES` is refused
before it ever reaches the scanner (the scanner holds the GIL while it reads
two views of the body, so an uncapped body is a way to stall the process),
then `scan.scan` runs and any finding refuses the write. A refusal is logged
on `artifacts.refusals` at WARNING with the agent seat, the artifact id (when
patching), the shape names and the finding count -- never the body or a
matched value, which `scan.Finding` never carries in the first place.
"""

import hashlib
import logging
import os
from dataclasses import dataclass

from artifacts import ids, scan
from artifacts.db import Database

# Measured worst clean body at 1 MiB: 484-524 ms under load, over the 0.5 s
# bar, and tuning only moves the worst case to the next shape -- so 512 KiB,
# not the 4 MiB or 1 MiB first proposed. The largest real page today is
# ~41 KB: 12x headroom. Raise this only when a real page needs it.
MAX_BODY_BYTES = 512 * 1024

_refusals = logging.getLogger("artifacts.refusals")


class SecretFound(Exception):
    def __init__(self, findings):
        self.findings = findings


class BodyTooLarge(Exception):
    def __init__(self, size: int):
        self.size = size


class RevisionMismatch(Exception): ...


class NotFound(Exception): ...


class Forbidden(Exception): ...


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


def _guard(body: str, *, agent: str, artifact_id: str | None) -> None:
    size = len(body.encode("utf-8"))
    if size > MAX_BODY_BYTES:
        raise BodyTooLarge(size)
    findings = scan.scan(body)
    if findings:
        shapes = sorted({finding.shape for finding in findings})
        _refusals.warning(
            "publish refused: agent=%s artifact=%s shapes=%s findings=%d",
            agent,
            artifact_id or "-",
            shapes,
            len(findings),
        )
        raise SecretFound(findings)


def publish(db: Database, agent: str, title: str, body: str) -> Published:
    _guard(body, agent=agent, artifact_id=None)
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
    return Published(
        id=artifact_id, version=1, revision=revision, url=url_for(artifact_id)
    )


def _owned(conn, artifact_id: str, agent: str):
    return conn.execute(
        "select id, title, agent, visibility, current_version from artifact"
        " where id = %s and agent = %s",
        (artifact_id, agent),
    ).fetchone()


def patch(
    db: Database, agent: str, artifact_id: str, body: str, expected_revision: str
) -> Published:
    _guard(body, agent=agent, artifact_id=artifact_id)
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
    return Published(
        id=artifact_id, version=n, revision=revision, url=url_for(artifact_id)
    )


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
