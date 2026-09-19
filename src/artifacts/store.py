"""Publishing, patching, reading and listing artifacts.

`publish` and `patch` check the cheapest guard first, and the scanner last, so
CPU is never spent on behalf of a caller who has no right to it. `patch` in
particular runs: the size cap (no database round trip) -- ownership (one
indexed query, so an unknown artifact and someone else's look identical, both
404) -- the secret scan (the scanner holds the GIL while it reads two views of
the body, so it always runs last and only once ownership is confirmed). The
title is agent-authored and displayed exactly as the body is, so it passes
through the same two guards -- capped at `MAX_TITLE_BYTES`, well below the
body's cap, because a title is a page name in a list, not prose. Every guard
takes a `field` ("title" or "body") and carries it into the exception, the log
line and (for a scan finding) the refusal entry -- a title refusal must never
be reported, logged or measured as a body refusal, which is what happened
before `field` existed: same message, same log line, wrong field named in
both. A lone surrogate (`"\ud800"`) survives JSON decoding but neither `utf-8`
nor Postgres `text` can hold one, so encoding it for the size check is also a
guard, refusing with `InvalidText` rather than crashing. Every refusal is
logged on `artifacts.refusals` at WARNING with the agent seat, the artifact id
(when patching) and the field -- a size refusal adds the value's size, a scan
refusal adds the shape names and finding count -- never the value itself or a
matched value, which `scan.Finding` never carries in the first place. A 404 or
a stale-revision 409 is routine, not a refusal, and is never logged.

Every write is one transaction: `publish`'s three statements and `patch`'s two
run inside `conn.transaction()`, so a failure partway through -- a NUL byte
Postgres refuses in `text`, or anything else -- leaves no row behind rather
than an artifact with no version an agent can never patch or delete. Two
patches racing the same `expected_revision` both pass the revision check
(nothing here takes a lock), but only one can insert `version(artifact_id,
n)` -- the primary key catches the loser, and its `UniqueViolation` is turned
into the same `RevisionMismatch` a plain stale read would have given it, so
the loser sees 409, not a raw database error.
"""

import hashlib
import logging
import os
from dataclasses import dataclass

from psycopg.errors import UniqueViolation

from artifacts import ids, scan
from artifacts.db import Database

# Measured worst clean body at 1 MiB: 484-524 ms under load, over the 0.5 s
# bar, and tuning only moves the worst case to the next shape -- so 512 KiB,
# not the 4 MiB or 1 MiB first proposed. The largest real page today is
# ~41 KB: 12x headroom. Raise this only when a real page needs it.
MAX_BODY_BYTES = 512 * 1024

# A title is a page name shown in a gallery list, not prose -- 512 bytes, not
# the body's 512 KiB.
MAX_TITLE_BYTES = 512

_refusals = logging.getLogger("artifacts.refusals")


class SecretFound(Exception):
    def __init__(self, field: str, findings):
        self.field = field
        self.findings = findings


class BodyTooLarge(Exception):
    def __init__(self, field: str, size: int):
        self.field = field
        self.size = size


class InvalidText(Exception):
    def __init__(self, field: str):
        self.field = field


class RevisionMismatch(Exception): ...


class NotFound(Exception): ...


class ScanFailed(Exception):
    def __init__(self, field: str):
        self.field = field


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


def _check_size(
    value: str, limit: int, field: str, *, agent: str, artifact_id: str | None
) -> None:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        # A lone surrogate (`"\ud800"`) survives JSON decoding -- Python's str
        # happily holds one -- but neither `utf-8` nor Postgres `text` can, so
        # this is a refusal, not a bug to repair around. Never echo the text;
        # only the field name says anything about what was sent.
        _refusals.warning(
            "publish refused: agent=%s artifact=%s field=%s reason=invalid_text",
            agent,
            artifact_id or "-",
            field,
        )
        raise InvalidText(field) from exc
    if size > limit:
        _refusals.warning(
            "publish refused: agent=%s artifact=%s field=%s reason=body_too_large size=%d",
            agent,
            artifact_id or "-",
            field,
            size,
        )
        raise BodyTooLarge(field, size)


def _check_secrets(
    value: str, field: str, *, agent: str, artifact_id: str | None
) -> None:
    try:
        findings = scan.scan(value)
    except Exception as exc:
        # scan() can raise (e.g. CPython's integer-to-string conversion limit,
        # hit inside html.unescape on a body holding thousands of digits after
        # `&#`) -- a scanner exception is not "no findings", so it must refuse
        # exactly as a real finding would, never fall through to a clean
        # publish. Only the exception's type is logged, never its message or
        # the value that triggered it.
        _refusals.warning(
            "publish refused: agent=%s artifact=%s field=%s reason=scan_failed error=%s",
            agent,
            artifact_id or "-",
            field,
            type(exc).__name__,
        )
        raise ScanFailed(field) from exc
    if findings:
        shapes = sorted({finding.shape for finding in findings})
        _refusals.warning(
            "publish refused: agent=%s artifact=%s field=%s shapes=%s findings=%d",
            agent,
            artifact_id or "-",
            field,
            shapes,
            len(findings),
        )
        raise SecretFound(field, findings)


def _guard(
    value: str, limit: int, field: str, *, agent: str, artifact_id: str | None
) -> None:
    """Both checks, in order, for one field -- used for the title and, on
    create, the body. `patch` guards the body itself (see below), since there
    the scan must wait until after ownership is confirmed. `field` ("title" or
    "body") travels with every refusal -- the log line, the size exception,
    and each scan finding -- so a title refusal is never reported, logged, or
    even measured as a body refusal."""
    _check_size(value, limit, field, agent=agent, artifact_id=artifact_id)
    _check_secrets(value, field, agent=agent, artifact_id=artifact_id)


def publish(db: Database, agent: str, title: str, body: str) -> Published:
    _guard(title, MAX_TITLE_BYTES, "title", agent=agent, artifact_id=None)
    _guard(body, MAX_BODY_BYTES, "body", agent=agent, artifact_id=None)
    artifact_id, revision = ids.new_id(), _revision(body, 1)
    with db.connect() as conn, conn.transaction():
        # Three steps, not two: `version` references `artifact`, and `artifact.current_version`
        # references `version`, so the two tables point at each other. The artifact row is born
        # with no current version, the first version is written, and only then does the artifact
        # point at it -- the same shape `patch()` uses for every later version. All three run in
        # one transaction: a failure partway through (a NUL byte Postgres refuses, or anything
        # else) must leave no row behind, not an artifact with no version.
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
    _check_size(body, MAX_BODY_BYTES, "body", agent=agent, artifact_id=artifact_id)
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
        _check_secrets(body, "body", agent=agent, artifact_id=artifact_id)
        n = current + 1
        revision = _revision(body, n)
        try:
            # One transaction: a failure between the two writes must leave the
            # artifact exactly as it was, not pointing at a version that was
            # never written.
            with conn.transaction():
                conn.execute(
                    "insert into version (artifact_id, n, body, revision, created_by)"
                    " values (%s, %s, %s, %s, %s)",
                    (artifact_id, n, body, revision, agent),
                )
                conn.execute(
                    "update artifact set current_version = %s, updated_at = now() where id = %s",
                    (n, artifact_id),
                )
        except UniqueViolation as exc:
            # Two patches raced the same `expected_revision`: both passed the
            # check above (nothing here takes a lock), but only one of them
            # can occupy `version(artifact_id, n)` -- the primary key is what
            # actually prevents the lost update. The loser hits this, not a
            # lost write, and sees the same 409 a plain stale read would have
            # given it a moment later.
            #
            # Reading ANY UniqueViolation here as "you raced" -- rather than
            # some other collision -- holds only on two premises, both true
            # today: `version(artifact_id, n)` is the only unique or exclusion
            # constraint this insert can hit (`artifact_pkey` is `id`, never
            # written by this statement), and nothing writes a `version` row
            # outside this function (including publish()'s first one).
            # `version.status` already distinguishes 'pending' from 'live' in
            # the schema -- a later task that promotes a pending version,
            # restores an old one, or writes versions for uploaded files
            # breaks the second premise, and this branch would then mislabel a
            # real collision as a stale revision. Re-check both premises
            # before adding any such write path.
            raise RevisionMismatch() from exc
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
