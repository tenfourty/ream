r"""Publishing, patching, reading and listing artifacts.

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
logged on `artifacts.refusals` -- a size refusal adds the value's size, a scan
refusal adds the shape names and finding count -- never the value itself or a
matched value, which `scan.Finding` never carries in the first place. A 404 or
a stale-revision 409 is routine, not a refusal, and is never logged.

Almost every refusal is logged at WARNING and answered as 422: the caller's
own content tripped a guard working as designed, and rewriting the content is
the right response. `ScanFailed` is the one exception in both directions: the
scanner itself broke, the content is not the problem, and telling the caller
422 would send a well-behaved agent into an infinite loop rewriting a page
that was never at fault while the actual defect goes unfixed. It is logged at
ERROR -- distinguishable from a routine `shapes=` refusal line at a glance,
not just by grepping `reason=scan_failed` -- and answered 503, saying plainly
that the fault is ours, nothing published, and to tell the operator rather
than rewrite and retry.

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
import re
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from artifacts import events, folders, forms, ggscan, ids, packing, partner, scan
from artifacts import rename as agent_rename
from artifacts.db import DUPLICATE_KEY, Database, in_list, jsonb
from artifacts.text import LEADING_DOCTYPE

# 3 MiB since 28 Sep 2026: an agent's real page hit the old 512 KiB, and
# Jeremy chose 3 MiB, the most that fits under the ingresses' 4m
# proxy-body-size with room for JSON escaping (the tools send UTF-8, not
# \u-escapes, for that reason). Measured on baba at 3 MiB: ordinary HTML
# scans in about 0.3 s, and the worst bodies found (dense entity references,
# dense hits) take about 1.2 s. ggshield was shown to find a token at the end
# of a 3 MiB page. It was 512 KiB from 19 Sep, when the bar was 0.5 s at 1 MiB.
# 8 MiB since 15 Oct 2026: the tracker board reached 2.7 MB, and Jeremy chose a
# higher limit over trimming it. The ingresses allow 12m and the ggshield relay
# 12 MiB for it (homelab); ggshield found a token at the end of an 8 MiB page in
# 2.5 s. The first try OOM-killed the pod through the thumbnail browser; pages over
# thumbs.RENDER_MAX_BYTES now get no browser at all.
MAX_BODY_BYTES = 8 * 1024 * 1024

# A title is a page name shown in a gallery list, not prose -- 512 bytes, not
# the body's megabytes.
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


class NotAPage(Exception):
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
    # True when a PATCH changed nothing: the body was byte-identical to the
    # live version, so no version was minted (homelab-ij8.41).
    #
    # SAID BY THE SERVICE rather than inferred by each client (homelab-ij8.53).
    # `artifacts-republish` worked this out by holding the revision from before
    # and comparing, which is correct and is also a thing every future client
    # has to reimplement -- and the one that does not reimplement it is the one
    # that reports an hourly timer as having updated a page every hour. The
    # same argument `patch` already makes for enforcing the no-op server-side
    # applies to reporting it.
    #
    # Always False from `publish` and `duplicate`: a page that did not exist a
    # moment ago has not failed to change.
    unchanged: bool = False
    # THE ADDRESS A HUMAN CAN ACTUALLY FOLLOW (homelab-ij8.42). `url` is the
    # page's own origin, which 303s to the gallery by design -- so the one
    # field that looks like the thing to paste into a message is the one field
    # that does not take the reader to the page. An agent nearly pasted it into
    # a report to another session, and agents that cannot find the right shape
    # invent one.
    page_url: str = ""
    # WHAT WAS ACTUALLY STORED (homelab-ij8.43). The publisher cannot see its
    # own page -- the content origin only answers a framed request holding a
    # capability token no agent is given -- so a substitution that silently did
    # nothing looks exactly like one that worked. An agent that had just
    # replaced two timestamp placeholders with sed pointed this out: had the
    # sed failed, the response would have been identical and the page would
    # have shown the raw placeholder to Jeremy. A title echo and a byte count
    # catch a whole class of that for the price of two fields.
    title: str = ""
    bytes: int = 0
    # THE FOLDER THIS CALL FILED THE PAGE IN, and whether it made it: `{"slug", "created"}`,
    # or None when the call named none (or took the page out). `created` is how a typo
    # shows: `--folder raodmap` prints "created new folder", which no one meant (all eight
    # publishing offices asked for exactly this).
    folder: dict | None = None


@dataclass(frozen=True)
class StoredBody:
    id: str
    version: int
    revision: str
    body: str


@dataclass(frozen=True)
class ArtifactRow:
    id: str
    title: str
    agent: str
    visibility: str
    current_version: int
    revision: str
    # When this page last changed. The query has always ORDERED BY it and never
    # returned it, so every client saw null and `artifacts-publish --list` could
    # not say when a page last moved -- the service knew and told nobody
    # (homelab-ij8.70).
    updated_at: datetime
    url: str
    # The gallery address, for the same reason `Published` carries one: `url`
    # is the page's own origin and 303s, so a listing printed for a person was
    # a column of links none of which went anywhere useful (homelab-ij8.42).
    page_url: str = ""
    # The two pieces of shelf state. They are on the row rather than behind a
    # separate call because an agent that can archive a page has to be able to
    # tell whether it already is -- otherwise "put it back" is a guess, and the
    # only way to find an archived page again would be to remember its id.
    # None means not archived / not pinned; a time means since when.
    archived_at: datetime | None = None
    pinned_at: datetime | None = None
    # Set only on a row from `list_deleted`: every other listing here excludes
    # deleted pages outright, so on those it is always None and says so.
    deleted_at: datetime | None = None
    # Only filled by `list_deleted`: how many questions on this binned page are
    # SET ASIDE (open, unanswered). Shown as such in the bin, never as waiting.
    open_questions: int = 0
    # WHICH SESSION published the version that is live right now.
    #
    # A SEAT IS NOT A SESSION, and on 22 Sep 2026 that cost a wrong attribution:
    # a key is shared by every session on its machine, so `agent` above answers
    # "which key published this" and nothing at all about who was behind it. The
    # operator's seat listed ten pages under `baba_operator` and could not tell
    # which were its own; the cove seat's listing grew a page published by a
    # different session under the same key, and Jeremy attributed it to the
    # wrong one. Worse, four of his answers sat uncollected for days because the
    # seat name made a departed session and a live one look identical.
    #
    # The service has recorded this on every version since 20 Sep and surfaced
    # it nowhere, which is the whole gap: what a listing cannot say, nobody
    # checks. None means a version published before sessions existed -- unknown,
    # which is not the same as "nobody" and must never be printed as one.
    last_session: str | None = None
    # WHETHER JEREMY HAS SEEN IT (homelab-ij8.103). The version he last opened
    # in the gallery and when; None for a page he has never opened. His ruling:
    # the agent that published a page can see this, for its own pages only --
    # the agent's twin of the wall's unread marker. `seen_version` below
    # `current_version` means he has not opened the latest change.
    seen_version: int | None = None
    seen_at: datetime | None = None
    # Whether other agents in his space may read it (homelab-ij8.105): True by
    # default, False when he or the publisher has closed it to the two of them.
    agents_can_read: bool = True
    # The slug of the folder the page is in, None for a loose page. Only on the caller's own
    # rows: another agent's folder is not this agent's to see.
    folder: str | None = None


def content_domain() -> str:
    return os.environ.get("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")


def gallery_origin() -> str:
    """Read at call time, exactly like content_domain(). A module-level
    os.environ.get() is fixed at import, so no test can override it and the
    deployment has to set it before the first import of this module."""
    return os.environ.get(
        "ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site"
    )


def url_for(artifact_id: str) -> str:
    return f"https://{artifact_id}.{content_domain()}/"


def page_url_for(artifact_id: str) -> str:
    """Where a person opens this page: the gallery, behind Jeremy's login.

    `url_for` is the page's own origin and 303s here anyway, so this is the
    address to put in a message, a report or a bead (homelab-ij8.42)."""
    return f"{gallery_origin().rstrip('/')}/a/{artifact_id}"


def content_url(artifact_id: str, frame_pass: str) -> str:
    """The address the GALLERY frames, carrying a pass from `frame_pass.mint`: good
    once, for minutes (homelab-ij8.101). Never handed to an agent, and
    `Published.url` stays pass-free. Until 22 Sep 2026 this carried the page's
    permanent `read_token`, which made any leaked frame URL a read for ever."""
    return f"https://{artifact_id}.{content_domain()}/p/{frame_pass}/"


def read_token(db: Database, artifact_id: str) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            # A deleted page's address stops working. Leaving it live would
            # mean "deleted" was only a change to a listing, and anyone holding
            # the link would never know.
            "select read_token from artifact where id = %s and deleted_at is null",
            (artifact_id,),
        ).fetchone()
    return row[0] if row else None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


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


def check_body(body: str, *, agent: str) -> None:
    """Both scanners on a body that is not being stored -- a preview. Raises
    `SecretFound` exactly as a publish would (Jeremy, 24 Sep 2026: previews
    are scanned too)."""
    _check_secrets(body, "body", agent=agent, artifact_id=None)


def _check_secrets(
    value: str,
    field: str,
    *,
    agent: str,
    artifact_id: str | None,
    second_opinion: bool = True,
    gitguardian: bool = False,
) -> None:
    try:
        findings = scan.scan(value)
    except Exception as exc:
        # scan() can raise (the known case was CPython's integer-to-string
        # conversion limit inside html.unescape, since fixed at the source --
        # this guard is now the net for whatever the next one turns out to
        # be) -- a scanner exception is not "no findings", so it must refuse
        # exactly as a real finding would, never fall through to a clean
        # publish. Only the exception's type is logged, never its message or
        # the value that triggered it.
        #
        # Logged at ERROR, not WARNING: a `shapes=` line below is the gate
        # doing its job, but a `scan_failed` line is a defect in the scanner
        # itself, and it needs to look different at a glance, not just under
        # a `reason=scan_failed` grep -- a run of these hiding inside a
        # weekly refusal count is invisible until every publish and patch,
        # for every agent, is silently failing closed.
        _refusals.error(
            "publish refused: agent=%s artifact=%s field=%s reason=scan_failed error=%s",
            agent,
            artifact_id or "-",
            field,
            type(exc).__name__,
        )
        raise ScanFailed(field) from exc
    # GitGuardian runs on a body, and on any other field whose caller asks for
    # it (`gitguardian=True`: a section update's version note and part label,
    # which are stored and shown like page text).
    if not findings and (field == "body" or gitguardian) and second_opinion:
        # The second opinion, on the ORIGINAL body (homelab-ij8.2; see ggscan).
        # Only once the built-in scan is clean: an obvious secret is refused
        # here without ever leaving the house.
        second = ggscan.scan(value)
        if second is None:
            if ggscan.configured():
                _refusals.warning(
                    "published on the built-in scan only: agent=%s artifact=%s "
                    "reason=ggshield_unavailable",
                    agent,
                    artifact_id or "-",
                )
        else:
            findings = second
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


# Is this a page at all? Anything that declares itself HTML counts: a doctype
# (after an optional BOM, whitespace and whole leading comments -- the shim
# injector already knows how to find one, so this reuses its regex rather than
# growing a second opinion about where a doctype may sit), or an <html>,
# <head> or <body> tag anywhere in the body.
#
# WHY IT EXISTS (homelab-ij8.28, 19 Sep 2026). A builder script printed
# "wrote <path>" to stdout, that LOG LINE was piped into a publish, and a
# 145-byte body with no doctype, no html and no head was accepted, stored as a
# new version, and served at an address Jeremy has. The publish path guarded
# size and scanned for secrets and never asked the first question.
#
# DELIBERATELY GENEROUS, and it must never become a validator: a page that is
# odd, or minimal, or a fragment somebody meant, is the author's business and
# gets published. It refuses exactly one thing -- a body with NO HTML IN IT AT
# ALL -- because that is the shape the mistake takes.
#
# WIDENED BEFORE IT EVER REFUSED ANYTHING, 21 Sep. The first version asked for
# a doctype or an <html>/<head>/<body>, which is what the bead asked for, and
# then I swept the live wall: the operator's "Homelab decisions and status" --
# 83 KB, published daily, perfectly good -- begins `<title>` and has none of
# them. A guard that refuses a working page on somebody else's daily schedule
# is not a guard, it is an outage I would have caused by reading an acceptance
# line more carefully than the wall it was about.
#
# So: any element-looking tag counts. The shapes this exists to catch -- a log
# line, a JSON blob, a markdown file, an empty string -- contain none.
_DECLARES_HTML = re.compile(r"<[a-z][a-z0-9-]*(?:[\s/>]|$)", re.IGNORECASE)


def _check_is_a_page(
    body: str, field: str, *, agent: str, artifact_id: str | None
) -> None:
    if LEADING_DOCTYPE.match(body) or _DECLARES_HTML.search(body):
        return
    _refusals.warning(
        "publish refused: agent=%s artifact=%s field=%s reason=not_a_page bytes=%d",
        agent,
        artifact_id or "-",
        field,
        len(body.encode("utf-8", "replace")),
    )
    raise NotAPage(field)


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
    if field == "body":
        _check_is_a_page(value, field, agent=agent, artifact_id=artifact_id)
    _check_secrets(value, field, agent=agent, artifact_id=artifact_id)


def _check_folder(
    folder: str | None, folder_title: str | None, *, agent: str, artifact_id: str | None
) -> None:
    """The folder slug and its display name, checked before any write. `""` is a slug only on
    an update (it takes the page out); `folders.BadSlug` for anything else that is not one."""
    if folder:
        folders.clean_slug(folder)
    if folder_title is not None:
        _guard(
            folder_title,
            MAX_TITLE_BYTES,
            "folder_title",
            agent=agent,
            artifact_id=artifact_id,
        )


def publish(
    db: Database,
    agent: str,
    title: str,
    body: str,
    *,
    session: str | None = None,
    folder: str | None = None,
    folder_title: str | None = None,
) -> Published:
    """`folder` files the new page in the agent's own folder of that slug, creating it on first
    use (`folder_title` names a NEW folder, once; it defaults to the slug). A bad slug is
    refused before anything is stored (`folders.BadSlug`).

    `session` is a LABEL the caller declared about itself, not a credential --
    see `api.session`. It is stored on the artifact (who first published it) and
    on the version (who published THAT one), because a page three sessions have
    edited has three answers to "who did this" and only the per-version one is
    ever right. Keyword-only and defaulted so every existing caller -- the
    gallery, the tests, the republisher -- keeps working and simply stores NULL,
    which is the honest value for "did not say".
    """
    _guard(title, MAX_TITLE_BYTES, "title", agent=agent, artifact_id=None)
    _guard(body, MAX_BODY_BYTES, "body", agent=agent, artifact_id=None)
    _check_folder(folder, folder_title, agent=agent, artifact_id=None)
    # A bad form is refused before anything is stored (FormRefused -> 422).
    parsed_forms = forms.parse(body)
    artifact_id, revision = ids.new_id(), _revision(body, 1)
    # Version 1 is a full copy, encoded before the transaction opens so the write lock is
    # not held while zstd runs.
    packed = packing.pack(1, body, None)
    token = secrets.token_urlsafe(
        24
    )  # 192 bits; URL-safe alphabet, so it is one path segment
    # One instant for the whole publish, as Postgres's now() was: a new page's
    # created_at and updated_at are equal.
    now = datetime.now(UTC)
    with db.connect() as conn, conn.transaction():
        # Three steps, not two: `version` references `artifact`, and `artifact.current_version`
        # references `version`, so the two tables point at each other. The artifact row is born
        # with no current version, the first version is written, and only then does the artifact
        # point at it -- the same shape `patch()` uses for every later version. All three run in
        # one transaction: a failure partway through (a NUL byte Postgres refuses, or anything
        # else) must leave no row behind, not an artifact with no version.
        conn.execute(
            "insert into artifact"
            " (id, title, agent, read_token, current_version, session,"
            "  created_at, updated_at)"
            " values (%s, %s, %s, %s, null, %s, %s, %s)",
            (artifact_id, title, agent, token, session, now, now),
        )
        conn.execute(
            "insert into version (artifact_id, n, body, revision, created_by, session,"
            "  packed, codec, base_n, body_bytes, body_sha256)"
            " values (%s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                artifact_id,
                body,
                revision,
                agent,
                session,
                packed.data,
                packed.codec,
                packed.base_n,
                len(body.encode()),
                packing.sha256(body),
            ),
        )
        conn.execute(
            "update artifact set current_version = 1, updated_at = %s where id = %s",
            (now, artifact_id),
        )
        forms.sync(conn, artifact_id, 1, parsed_forms, session)
        filed = (
            folders.file_page(conn, artifact_id, agent, folder, folder_title)
            if folder
            else None
        )
    return Published(
        id=artifact_id,
        version=1,
        revision=revision,
        url=url_for(artifact_id),
        page_url=page_url_for(artifact_id),
        title=title,
        bytes=len(body.encode()),
        folder=filed,
    )


def _owned(conn, artifact_id: str, agent: str, as_reader: bool = False):
    """The page, if `agent` may act on it. `as_reader` widens that to READING
    a page another agent published, when the page is open to agents
    (homelab-ij8.105). Only read routes may pass it; every write stays scoped
    to the publisher. A closed page and a missing one still look the same."""
    if as_reader:
        return conn.execute(
            "select id, title, agent, visibility, current_version, updated_at,"
            " archived_at, pinned_at, agents_can_read"
            " from artifact where id = %s and deleted_at is null"
            " and (agent = %s or agents_can_read)",
            (artifact_id, agent),
        ).fetchone()
    return conn.execute(
        # `deleted_at is null` is here rather than at each caller BECAUSE
        # every caller is here: get_owned, get_body, patch and the
        # housekeeping verbs all resolve a page through this one function. A
        # deleted page must be invisible to all of them, and the way to be
        # sure of that is for there to be one place it can be forgotten.
        "select id, title, agent, visibility, current_version, updated_at,"
        " archived_at, pinned_at, agents_can_read"
        " from artifact where id = %s and agent = %s and deleted_at is null",
        (artifact_id, agent),
    ).fetchone()


def exists(db: Database, artifact_id: str) -> bool:
    """Whether this page exists at all, whoever owns it.

    The ONE read in this module that ignores ownership, and it exists for the
    one caller allowed to: a key holding `decisions:read-all` reading another
    seat's questions (homelab-9gj). Everywhere else "not yours" answers
    identically to "no such page", so that a key cannot be walked to learn
    which ids are real -- this function does not weaken that, because a caller
    without the grant never reaches it.

    Says nothing about the page but yes or no: a granted key is entitled to
    that page's DECISIONS, which is not the same authority as reading its body,
    its title or its version history.

    `deleted_at is null` for the same reason `_owned` carries it: a deleted
    page is invisible to every read in this module, and the way to be sure of
    that is for there to be one place per shape it can be forgotten.
    """
    with db.connect() as conn:
        row = conn.execute(
            "select 1 from artifact where id = %s and deleted_at is null",
            (artifact_id,),
        ).fetchone()
    return row is not None


def patch(
    db: Database,
    agent: str,
    artifact_id: str,
    body: str,
    expected_revision: str,
    title: str | None = None,
    *,
    session: str | None = None,
    note: str | None = None,
    section: str | None = None,
    notify: bool = False,
    guard=None,
    scanned: set[str] | None = None,
    folder: str | None = None,
    folder_title: str | None = None,
) -> Published:
    """`title=None` leaves the page's name alone; a string renames it.

    `folder=None` leaves the page's folder alone, a slug files it in the agent's own folder of
    that name (created on first use), `""` takes it out. A shelf fact: it makes no version, and
    it rides in the same transaction as the write, so a refused one (an archived folder, 409)
    changes nothing else.

    `note`, `section` and `notify` are written on the new version by a section
    update (`patch_section`); a whole-page update leaves them NULL/false.
    `guard(conn)`, when given, runs inside the write's transaction after the
    artifact row is locked `for update`, and may raise to refuse the write.
    `scanned` is a per-request set of body digests already scanned clean, so
    a retry of an identical body skips the scanners.

    `session` labels the VERSION this call writes -- see `publish`.

    A page's name outlives its first version -- what a page is FOR is often only
    clear after a few revisions, and until this existed the only way to correct a
    name was to publish a second artifact at a second address and abandon the
    first. The rename rides in this call rather than in one of its own so that a
    body and the name that describes it can never disagree: they commit together
    or not at all."""
    _check_size(body, MAX_BODY_BYTES, "body", agent=agent, artifact_id=artifact_id)
    _check_is_a_page(body, "body", agent=agent, artifact_id=artifact_id)
    if title is not None:
        _check_size(
            title, MAX_TITLE_BYTES, "title", agent=agent, artifact_id=artifact_id
        )
    _check_folder(folder, folder_title, agent=agent, artifact_id=artifact_id)
    # A bad form is refused before anything is stored (FormRefused -> 422).
    parsed_forms = forms.parse(body)
    with db.connect() as conn:
        row = _owned(conn, artifact_id, agent)
        if row is None:
            raise NotFound()
        current = row[4]
        live = conn.execute(
            "select revision, body from version where artifact_id = %s and n = %s",
            (artifact_id, current),
        ).fetchone()
        if live is None or live[0] != expected_revision:
            raise RevisionMismatch()

        # An identical body is not a new version (homelab-ij8.41). Before this,
        # every republish inserted a row and bumped `updated_at`, so a page
        # redrawn on a schedule LOOKED freshly updated to a reader every time
        # and its history filled with identical copies. That matters now rather
        # than in principle: a bd hook fires a republish after every tracker
        # change, and most tracker changes do not change the page.
        #
        # Enforced HERE rather than in each client on purpose. A client-side
        # check is one every future client has to reimplement, and the one that
        # does not is the one that quietly fills the history.
        #
        # A rename with an unchanged body still renames: the name is not the
        # body, and refusing it would make "correct the title" impossible
        # without editing the page. It bumps `updated_at`, because to a reader
        # the page HAS changed.
        if live[1] == body:
            # The TITLE is still new even when the body is not, so it still
            # faces the secret scan. Skipping it here was a real regression
            # caught by an existing test: a rename carrying an AWS key in the
            # title sailed through 200 while the same title on a changed body
            # was refused 422. The body itself needs no re-scan — it is the
            # bytes already stored, already checked when they were.
            filed = None
            with conn.transaction():
                if title is not None:
                    _check_secrets(title, "title", agent=agent, artifact_id=artifact_id)
                    conn.execute(
                        "update artifact set title = %s, updated_at = %s where id = %s",
                        (title, datetime.now(UTC), artifact_id),
                    )
                if folder is not None:
                    filed = folders.file_page(
                        conn, artifact_id, agent, folder, folder_title
                    )
            return Published(
                folder=filed,
                id=artifact_id,
                url=url_for(artifact_id),
                page_url=page_url_for(artifact_id),
                revision=expected_revision,
                version=current,
                unchanged=True,
                # The title AFTER this call, which is the new one on a rename
                # and the stored one otherwise -- the echo is only worth having
                # if it reflects what a reader will now see.
                title=title if title is not None else row[1],
                bytes=len(body.encode()),
            )

        if scanned is None or _digest(body) not in scanned:
            _check_secrets(body, "body", agent=agent, artifact_id=artifact_id)
            if scanned is not None:
                scanned.add(_digest(body))
        if title is not None:
            _check_secrets(title, "title", agent=agent, artifact_id=artifact_id)
        n = current + 1
        revision = _revision(body, n)
        # The new version is packed now, while its base (the current version) is still plain
        # text in `body`: a write never has to rebuild anything. Encoded before the
        # transaction, so a slow encode holds no lock.
        packed = packing.pack(n, body, live[1])
        try:
            # One transaction: a failure between the two writes must leave the
            # artifact exactly as it was, not pointing at a version that was
            # never written.
            with conn.transaction():
                if guard is not None:
                    # The same lock `decisions.ask` takes, so the guard's
                    # check and a new question cannot interleave.
                    conn.execute(
                        "select 1 from artifact where id = %s" + conn.for_update(),  # noqa: S608 -- clause from the db layer, no caller input
                        (artifact_id,),
                    )
                    guard(conn)
                # The version carries the session that wrote IT. The artifact's
                # own `session` is deliberately NOT updated here: it records who
                # first published the page, and overwriting it on every edit
                # would turn "who made this" into "who touched it last", which
                # the version rows already say and say better.
                conn.execute(
                    "insert into version"
                    " (artifact_id, n, body, revision, created_by, session,"
                    "  note, section, notify,"
                    "  packed, codec, base_n, body_bytes, body_sha256)"
                    " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        artifact_id,
                        n,
                        body,
                        revision,
                        agent,
                        session,
                        note,
                        section,
                        notify,
                        packed.data,
                        packed.codec,
                        packed.base_n,
                        len(body.encode()),
                        packing.sha256(body),
                    ),
                )
                # The version that just stopped being current keeps its text only in
                # `packed`. An UPDATE of an existing row, not an insert, so a duplicate
                # key below still means a race. Only a version that IS packed is blanked:
                # one written before this change keeps its text until the backfill
                # (artifacts-admin pack-versions) has packed and checked it.
                conn.execute(
                    "update version set body = '' where artifact_id = %s and n = %s"
                    " and packed is not null",
                    (artifact_id, current),
                )
                if title is None:
                    conn.execute(
                        "update artifact set current_version = %s, updated_at = %s"
                        " where id = %s",
                        (n, datetime.now(UTC), artifact_id),
                    )
                else:
                    conn.execute(
                        "update artifact set current_version = %s, title = %s,"
                        " updated_at = %s where id = %s",
                        (n, title, datetime.now(UTC), artifact_id),
                    )
                # A review he sent back opens its next round on this version,
                # in this transaction, so the round and the version it reviews
                # commit together. Only here: an identical body returned above
                # without a version, and `publish` writes version 1, which no
                # review can precede. Looked up at call time (not imported at
                # the top) because decisions imports this module.
                from artifacts import decisions

                decisions._advance_rounds(conn, artifact_id, n)
                forms.sync(conn, artifact_id, n, parsed_forms, session)
                filed = (
                    folders.file_page(conn, artifact_id, agent, folder, folder_title)
                    if folder is not None
                    else None
                )
        except DUPLICATE_KEY as exc:
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
        id=artifact_id,
        version=n,
        revision=revision,
        url=url_for(artifact_id),
        page_url=page_url_for(artifact_id),
        title=title if title is not None else row[1],
        bytes=len(body.encode()),
        folder=filed,
    )


class PageChanged(Exception):
    """`if_revision` named a revision the page has moved on from."""

    def __init__(self, given: str):
        self.given = given


class OpenDecisionDropped(Exception):
    """A section update would remove the slot of a question still open."""

    def __init__(self, decision_id: str, question: str):
        self.decision_id = decision_id
        self.question = question


@dataclass(frozen=True)
class SectionResult:
    published: Published
    section_revision: str | None
    label: str
    body: str


# A caller's version note is a line in a history listing, not a document.
SECTION_NOTE_MAX = 300


def page_note(note: str | None, *, agent: str, artifact_id: str | None) -> str | None:
    """A whole-page update's history note, ready to store: whitespace and
    control characters cleaned and capped as a section note is, scanned as page
    text is (it is shown in history like the page), and None when blank.

    A generator that republishes whole -- a programme board on a timer -- had no
    way to say in the history what moved (7 Oct 2026)."""
    from artifacts import text as text_module

    if note is None or not note.strip():
        return None
    cleaned = text_module.sanitize(note, SECTION_NOTE_MAX)
    _check_secrets(
        cleaned, "note", agent=agent, artifact_id=artifact_id, gitguardian=True
    )
    return cleaned


# How many times a section update re-reads and re-applies when ANOTHER update
# landed between its read and its write. Each retry is against the new live
# page, so two sessions changing different sections both land.
_SECTION_RETRIES = 5


def _open_among(conn, artifact_id: str, slots) -> tuple[str, str] | None:
    """The first still-open question whose slot is in `slots`, if any."""
    if not slots:
        return None
    ordered = sorted(slots)
    return conn.execute(
        "select id, question from decision"  # noqa: S608 -- placeholders only
        f" where artifact_id = %s and id in {in_list(ordered)}"
        "   and answer is null and closed_at is null"
        " order by id limit 1",
        (artifact_id, *ordered),
    ).fetchone()


def _matches_revision(given: str, version_n: int, revision: str) -> bool:
    """`if_revision` may be the revision hash every update returns, or the
    version number -- the spec says "revision N", and a script that chains on
    `version` should not have to learn a second field to do it."""
    given = given.strip()
    if given.isdigit():
        return int(given) == version_n
    return given == revision


def get_section(
    db: Database,
    agent: str,
    artifact_id: str,
    element_id: str,
    *,
    as_reader: bool = False,
) -> tuple[str, str, StoredBody]:
    """(outer html, section revision, live body) of one element of the live
    page. `as_reader` lets any agent read it when the page is open to agents,
    exactly as it may read the whole page; updating it stays the publisher's.
    Raises NotFound or a sections.SectionError."""
    from artifacts import sections

    live = get_body(db, agent, artifact_id, as_reader=as_reader)
    where = sections.locate(live.body, element_id)
    # The walker found it; a browser must find the same single element too.
    sections.check_target(live.body, element_id)
    outer = where.outer(live.body)
    return outer, sections.section_revision(outer), live


def patch_section(
    db: Database,
    agent: str,
    artifact_id: str,
    element_id: str,
    op: str,
    fragment: str | None,
    *,
    if_revision: str | None = None,
    if_section: str | None = None,
    note: str | None = None,
    notify: bool = False,
    session: str | None = None,
) -> SectionResult:
    """Apply one operation to one element of the LIVE page and store the
    whole result as a new version, through `patch` -- so the size limit, the
    is-a-page check, both secret scanners and the review rounds all run on the
    whole resulting page exactly as they do for a whole-page update.

    Optimistic rather than locking: read, apply, then write with the live
    revision as the precondition. If another update landed in between, the
    write loses the race (`RevisionMismatch`) and this re-reads and re-applies
    against the new page -- unless the caller pinned `if_revision`, in which
    case the page moving IS the refusal it asked for. No row lock is held
    across the GitGuardian call.
    """
    from artifacts import sections
    from artifacts import text as text_module

    if fragment is not None:
        # Before anything else touches it: the same refusal a body gets.
        _check_size(
            fragment, MAX_BODY_BYTES, "body", agent=agent, artifact_id=artifact_id
        )
    label_note = f"#{element_id}: {op}"
    if note and note.strip():
        label_note += " -- " + text_module.sanitize(note, SECTION_NOTE_MAX)
    # Digests of what has passed the scanners during THIS request.
    scanned: set[str] = set()
    for attempt in range(_SECTION_RETRIES):
        live = get_body(db, agent, artifact_id)
        if if_revision is not None and not _matches_revision(
            if_revision, live.version, live.revision
        ):
            raise PageChanged(if_revision)
        applied = sections.apply(
            live.body, element_id, op, fragment, if_section=if_section
        )
        # Slots as a browser reads the page (html5lib attributes, never a text
        # match). Checked here to fail fast, and again INSIDE the write's
        # transaction under the artifact row lock (`guard`), which `ask` and
        # `ask_review` also take -- so a question created after this read
        # cannot lose its slot to this write.
        dropped = applied.slots_before - applied.slots_after
        with db.connect() as conn:
            still_open = _open_among(conn, artifact_id, dropped)
        if still_open:
            raise OpenDecisionDropped(*still_open)

        def guard(conn, dropped=dropped):
            found = _open_among(conn, artifact_id, dropped)
            if found:
                raise OpenDecisionDropped(*found)

        part = f"{applied.label}: {sections.OP_WORDS[op]}"
        # The note and the part label are stored on the version and shown in
        # history and on his wall, so they face both scanners as page text
        # does. One call (field "note") so GitGuardian is asked once: line 1
        # is the note, line 2 the label. Cached by content for this request,
        # so an optimistic retry does not ask GitGuardian the same thing twice.
        stored_text = f"{label_note}\n{part}"
        digest = _digest(stored_text)
        if digest not in scanned:
            _check_secrets(
                stored_text,
                "note",
                agent=agent,
                artifact_id=artifact_id,
                gitguardian=True,
            )
            scanned.add(digest)
        try:
            published = patch(
                db,
                agent,
                artifact_id,
                applied.body,
                live.revision,
                session=session,
                note=label_note,
                section=part,
                notify=notify,
                guard=guard,
                scanned=scanned,
            )
        except RevisionMismatch:
            if if_revision is not None or attempt == _SECTION_RETRIES - 1:
                raise
            continue
        return SectionResult(
            published=published,
            section_revision=applied.section_revision,
            label=applied.label,
            body=applied.body,
        )
    raise RevisionMismatch()  # unreachable: the loop returns or raises


def get_owned(
    db: Database, agent: str, artifact_id: str, *, as_reader: bool = False
) -> ArtifactRow:
    with db.connect() as conn:
        row = _owned(conn, artifact_id, agent, as_reader)
        if row is None:
            raise NotFound()
        # The client's only way to learn the expected_revision for a PATCH: this
        # route returns the body's current hash, never the body. `get_body()`
        # below is the route that hands the body itself back to its owner.
        revision = conn.execute(
            "select revision, session from version where artifact_id = %s and n = %s",
            (row[0], row[4]),
        ).fetchone()
        # Whether HE has read it is the publisher's to know, not every reader's.
        seen = (
            conn.execute(
                "select read_version, read_at from page_read where artifact_id = %s",
                (row[0],),
            ).fetchone()
            if row[2] == agent
            else None
        ) or (None, None)
        folder = (
            conn.execute(
                "select f.slug from artifact a join folder f on f.id = a.folder_id"
                " where a.id = %s and f.owner = a.agent",
                (row[0],),
            ).fetchone()
            if row[2] == agent
            else None
        )
    return ArtifactRow(
        id=row[0],
        title=row[1],
        agent=row[2],
        visibility=row[3],
        current_version=row[4],
        revision=revision[0],
        last_session=revision[1],
        updated_at=row[5],
        url=url_for(row[0]),
        page_url=page_url_for(row[0]),
        archived_at=row[6],
        pinned_at=row[7],
        seen_version=seen[0],
        seen_at=seen[1],
        agents_can_read=row[8],
        folder=folder[0] if folder else None,
    )


def get_body(
    db: Database, agent: str, artifact_id: str, *, as_reader: bool = False
) -> StoredBody:
    """The current version's stored body, to its own agent.

    An author cannot otherwise see what it published. The rendered page is
    served only to a framed request carrying a capability token no agent is
    given, and the gallery is behind Jeremy's login, so until this existed the
    only thing a publisher could verify about its own page was the title --
    which is exactly why the first board here carried a PAGE TITLE placeholder
    in <title> for two hours.

    This is strictly less exposure than the content origin already has: the
    same bytes, unrendered, to the one agent that wrote them and already holds
    them. Ownership is checked the same way every other owning route checks
    it, and a body belonging to another agent is a plain 404, never a 403 --
    an agent learns nothing about what it does not own.
    """
    with db.connect() as conn:
        row = _owned(conn, artifact_id, agent, as_reader)
        if row is None:
            raise NotFound()
        version = conn.execute(
            "select n, revision, body from version where artifact_id = %s and n = %s",
            (row[0], row[4]),
        ).fetchone()
    return StoredBody(
        id=row[0], version=version[0], revision=version[1], body=version[2]
    )


@dataclass(frozen=True)
class VersionRow:
    """One entry in a page's history. NEVER the body.

    A history listing is read to decide which version to look at; carrying
    every body would make it the largest response this service produces and
    would hand a caller bytes it did not ask for. `bytes` is the size, which
    is what a reader actually compares.
    """

    n: int
    revision: str
    created_at: str
    created_by: str
    session: str | None
    bytes: int
    live: bool
    # What a section update says moved ("#running-now: prepend"); None on a
    # whole-page version.
    note: str | None = None


def list_versions(
    db: Database, agent: str, artifact_id: str, *, as_reader: bool = False
) -> list[VersionRow]:
    """A page's history, newest first, to its own agent.

    homelab-ij8.30, first-user feedback from the cove seat on 19 Sep: publish
    returns a revision hash and the skill says a page keeps its history, but
    nothing listed one, showed an old one or rolled back -- so the hash was a
    number the agent was told and could do nothing with.

    NEWEST FIRST, unlike a conversation: the question asked of a history is
    almost always "what happened recently", and the version somebody wants is
    usually the last one or the one before it.

    Ownership checked exactly as every other owning read checks it, and
    another agent's page is a plain 404 rather than a 403.
    """
    with db.connect() as conn:
        row = _owned(conn, artifact_id, agent, as_reader)
        if row is None:
            raise NotFound()
        rows = conn.execute(
            # Bytes, not length: `length` counts CHARACTERS, and a page with
            # any non-ASCII in it would report fewer bytes than it has --
            # which is the one number this row exists to give honestly.
            "select n, revision, created_at, created_by, session,"  # noqa: S608 -- clause from the db layer, no caller input
            # `body_bytes` is the plain text's size whatever is stored; a version not packed
            # yet has none, and its text is still here to measure.
            f"       coalesce(body_bytes, {conn.byte_length('body')}), note from version"
            " where artifact_id = %s order by n desc",
            (artifact_id,),
        ).fetchall()
    return [
        VersionRow(
            n=r[0],
            revision=r[1],
            created_at=r[2].isoformat(),
            created_by=r[3],
            session=r[4],
            bytes=r[5],
            live=r[0] == row[4],
            note=r[6],
        )
        for r in rows
    ]


def get_version_body(
    db: Database, agent: str, artifact_id: str, n: int, *, as_reader: bool = False
) -> StoredBody:
    """The body of ONE version, to its own agent (homelab-50ze, homelab-ij8.30).

    Strictly less exposure than `get_body` already grants: the same page, to
    the same agent, at a version it wrote itself. Rolling back is then this
    plus an ordinary update -- no new verb, and the roll-back lands as a NEW
    version rather than rewriting history, which is the only honest way for a
    service whose whole argument is that the record is not edited.
    """
    with db.connect() as conn:
        row = _owned(conn, artifact_id, agent, as_reader)
        if row is None:
            raise NotFound()
        revision = conn.execute(
            "select revision from version where artifact_id = %s and n = %s",
            (artifact_id, n),
        ).fetchone()
        if revision is None:
            raise NotFound()
        body = version_bodies(conn, artifact_id, [n])[n]
    return StoredBody(id=artifact_id, version=n, revision=revision[0], body=body)


class _Rebuilt:
    """A small least-recently-used store of rebuilt versions, bounded by bytes, so browsing
    a page's history does not decode the same chain again for each click. Keyed by the
    plain text's own hash, so an entry can never be the wrong text for its version."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._size = 0
        self._items: OrderedDict[tuple[str, int, str], str] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, int, str]) -> str | None:
        with self._lock:
            text = self._items.get(key)
            if text is not None:
                self._items.move_to_end(key)
            return text

    def put(self, key: tuple[str, int, str], text: str) -> None:
        with self._lock:
            size = len(text.encode())
            if key in self._items or size > self._limit:
                return
            self._items[key] = text
            self._size += size
            while self._size > self._limit:
                _, old = self._items.popitem(last=False)
                self._size -= len(old.encode())

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._size = 0


_rebuilt = _Rebuilt(48 * 1024 * 1024)


def forget_rebuilt() -> None:
    """Empty the rebuilt-version store (tests, and anything that must re-read the rows)."""
    _rebuilt.clear()


def version_bodies(conn, artifact_id: str, ns) -> dict[int, str]:
    """The plain text of each version in `ns` that exists (spec 2026-10-26, section 4).

    A version whose `body` holds its text (the current one, or one not yet packed) is
    returned as stored. Any other is rebuilt from `packed`: from the nearest full copy at or
    below it (at most 99 deltas), each step checked against the stored hash, so a damaged row
    raises `packing.PackError` and never returns wrong text. Versions in one chain share one
    walk. A version that does not exist is absent from the result."""
    wanted = sorted({int(n) for n in ns})
    if not wanted:
        return {}
    out: dict[int, str] = {}
    chains: dict[int, list[int]] = {}
    for n, body, is_packed in conn.execute(
        "select n, body, packed is not null from version"  # noqa: S608 -- placeholders only
        f" where artifact_id = %s and n in {in_list(wanted)}",
        (artifact_id, *wanted),
    ).fetchall():
        if body != "" or not is_packed:
            out[n] = body
        else:
            chains.setdefault(packing.keyframe_at_or_below(n), []).append(n)
    for start, group in chains.items():
        out.update(_rebuild(conn, artifact_id, start, group))
    return out


def _rebuild(conn, artifact_id: str, start: int, group: list[int]) -> dict[int, str]:
    top = max(group)
    # Hashes first (small), so a cache hit means the packed blobs before it are never read.
    shas = dict(
        conn.execute(
            "select n, body_sha256 from version"
            " where artifact_id = %s and n between %s and %s order by n",
            (artifact_id, start, top),
        ).fetchall()
    )
    # Begin at the newest version in the range that is already rebuilt.
    begin, prev = start, None
    for n in range(top, start - 1, -1):
        sha = shas.get(n)
        cached = _rebuilt.get((artifact_id, n, sha)) if sha else None
        if cached is not None:
            begin, prev = n, cached
            break
    rows = conn.execute(
        "select n, codec, packed, body_sha256 from version"
        " where artifact_id = %s and n between %s and %s order by n",
        (artifact_id, begin, top),
    ).fetchall()
    found: dict[int, str] = {}
    expected = begin
    for n, codec, packed, sha in rows:
        if n != expected:
            raise packing.PackError(f"version {expected} is missing from its chain")
        expected += 1
        if n == begin and prev is not None:
            text = prev
        elif packed is None:
            # Not packed yet, so its text is still in `body` (a page the backfill has
            # not reached).
            text = conn.execute(
                "select body from version where artifact_id = %s and n = %s",
                (artifact_id, n),
            ).fetchone()[0]
        else:
            text = packing.unpack(codec, packed, prev, sha)
        if n in group:
            found[n] = text
            if sha:
                _rebuilt.put((artifact_id, n, sha), text)
        prev = text
    if expected != top + 1:
        raise packing.PackError(f"version {expected} is missing from its chain")
    return found


def list_for_agent(db: Database, agent: str) -> list[ArtifactRow]:
    with db.connect() as conn:
        rows = conn.execute(
            "select artifact.id, artifact.title, artifact.agent, artifact.visibility,"
            " artifact.current_version, version.revision, artifact.updated_at,"
            " artifact.archived_at, artifact.pinned_at, version.session,"
            " page_read.read_version, page_read.read_at, artifact.agents_can_read,"
            " case when folder.owner = artifact.agent then folder.slug end"
            " from artifact"
            " join version on version.artifact_id = artifact.id"
            " and version.n = artifact.current_version"
            " left join page_read on page_read.artifact_id = artifact.id"
            " left join folder on folder.id = artifact.folder_id"
            " where artifact.agent = %s and artifact.deleted_at is null"
            " order by artifact.updated_at desc",
            (agent,),
        ).fetchall()
    return [
        ArtifactRow(
            id=row[0],
            title=row[1],
            agent=row[2],
            visibility=row[3],
            current_version=row[4],
            revision=row[5],
            updated_at=row[6],
            url=url_for(row[0]),
            page_url=page_url_for(row[0]),
            archived_at=row[7],
            pinned_at=row[8],
            last_session=row[9],
            seen_version=row[10],
            seen_at=row[11],
            agents_can_read=row[12],
            folder=row[13],
        )
        for row in rows
    ]


def list_in_folder(db: Database, agent: str, slug: str) -> list[ArtifactRow] | None:
    """The agent's own pages in its folder `slug` (an old slug resolves too), newest change
    first; None when the agent has no such folder. 'Not yours' and 'no such folder' are one
    answer: a folder is found by owner and slug together."""
    with db.connect() as conn:
        found = folders.find(conn, agent, slug)
    if found is None:
        return None
    wanted = set(folders.page_ids(db, agent, found[1]) or ())
    return [row for row in list_for_agent(db, agent) if row.id in wanted]


def list_readable(db: Database, agent: str) -> list[ArtifactRow]:
    """Every page `agent` may READ: its own, and every other agent's page that
    is open to agents (homelab-ij8.105; Jeremy chose that agents can browse
    their colleagues' open work, 5a). Whether he has read a page is reported
    only on the caller's own rows."""
    with db.connect() as conn:
        rows = conn.execute(
            "select artifact.id, artifact.title, artifact.agent, artifact.visibility,"
            " artifact.current_version, version.revision, artifact.updated_at,"
            " artifact.archived_at, artifact.pinned_at, version.session,"
            " page_read.read_version, page_read.read_at, artifact.agents_can_read,"
            " case when folder.owner = artifact.agent then folder.slug end"
            " from artifact"
            " join version on version.artifact_id = artifact.id"
            " and version.n = artifact.current_version"
            " left join page_read on page_read.artifact_id = artifact.id"
            " left join folder on folder.id = artifact.folder_id"
            " where artifact.deleted_at is null"
            " and (artifact.agent = %s or artifact.agents_can_read)"
            " order by artifact.updated_at desc",
            (agent,),
        ).fetchall()
    return [
        ArtifactRow(
            id=row[0],
            title=row[1],
            agent=row[2],
            visibility=row[3],
            current_version=row[4],
            revision=row[5],
            updated_at=row[6],
            url=url_for(row[0]),
            page_url=page_url_for(row[0]),
            archived_at=row[7],
            pinned_at=row[8],
            last_session=row[9],
            seen_version=row[10] if row[2] == agent else None,
            seen_at=row[11] if row[2] == agent else None,
            agents_can_read=row[12],
            folder=row[13] if row[2] == agent else None,
        )
        for row in rows
    ]


def set_agents_can_read(
    db: Database, artifact_id: str, can_read: bool, *, agent: str | None
) -> bool:
    """Open a page to every agent in his space, or close it to him and its
    publisher (homelab-ij8.105). `agent` None is the gallery (his wall, any
    page); an agent may only change its own. False if nothing matched."""
    where, params = _scoped(artifact_id, agent)
    with db.connect() as conn:
        done = conn.execute(
            f"update artifact set agents_can_read = %s {where}",  # noqa: S608 -- _scoped emits a fixed clause with placeholders, never caller text
            (can_read, *params),
        )
        return done.rowcount > 0


def save_thumbnail(db: Database, artifact_id: str, version_n: int, png: bytes) -> bool:
    """Attach a rendered picture to the version it depicts.

    Returns False rather than raising when the version has gone -- a render runs
    after the publish that triggered it has already returned, so the page may
    have moved on or been deleted in between. That is an ordinary race with an
    obvious answer: the picture is of a version nobody will serve, so drop it.
    """
    with db.connect() as conn:
        done = conn.execute(
            "update version set thumbnail = %s where artifact_id = %s and n = %s",
            (png, artifact_id, version_n),
        )
        return done.rowcount > 0


def live_thumbnail(db: Database, artifact_id: str) -> bytes | None:
    """The picture of the version currently being served, or None.

    Joined through `artifact.current_version` rather than taking the highest `n`:
    those are the same today, and if they ever diverge the one that matters is
    the version a reader is actually looking at.
    """
    with db.connect() as conn:
        row = conn.execute(
            "select v.thumbnail from artifact a"
            " join version v on v.artifact_id = a.id and v.n = a.current_version"
            " where a.id = %s and a.deleted_at is null",
            (artifact_id,),
        ).fetchone()
    return bytes(row[0]) if row and row[0] is not None else None


def _lock_open_decisions(conn, artifact_id: str) -> None:
    """Row-lock the page's open decisions, in id order, BEFORE anything touches the
    artifact row. A mark takes a share lock on an open review (`holds.page_mode`) and
    then inserts a comment, which key-shares the artifact; an archive that locked the
    artifact first and the review second would deadlock with it. Taking the decisions
    first makes the archive wait for the mark to commit, so `holds.release` then sees
    it. Same id order as `decisions._advance_rounds` and `holds.page_mode`."""
    conn.execute(
        "select id from decision where artifact_id = %s"  # noqa: S608 -- clause from the db layer, no caller input
        " and answered_at is null and closed_at is null order by id"
        + conn.for_update(),
        (artifact_id,),
    ).fetchall()


def _set_aside_state(conn, artifact_id: str) -> tuple[bool, object] | None:
    """(is the page archived or binned right now, its archived_at), read inside the
    caller's transaction BEFORE the write that moves it, so the caller can tell a
    page that is going away from one that already had. None if there is no such page.
    Archiving an archived page, or binning a binned one, must not tell the agent
    twice."""
    row = conn.execute(
        "select archived_at, deleted_at from artifact where id = %s"  # noqa: S608 -- clause from the db layer, no caller input
        + conn.for_update(),
        (artifact_id,),
    ).fetchone()
    if row is None:
        return None
    return (row[0] is not None or row[1] is not None), row[0]


def _set_aside_open_questions(conn, artifact_id: str, what: str, actor: str) -> int:
    """Tell the world that the page's open questions are SET ASIDE, and close nothing.

    `what` is "archived" or "binned". Called from inside the caller's transaction,
    on the same connection as the archive or bin it describes, so the events and
    their cause commit together. An event for a write that then rolls back would
    tell an agent about something that never happened.

    THE RULE (Jeremy, 2 Nov 2026, replacing his 21 Sep 2026 one that closed them):
    archiving or binning a page SETS ASIDE every question on it that is still open,
    of every kind. Nothing that shows him what waits on him shows them while the
    page is archived or binned, and unarchiving or restoring brings them back open
    and unanswered, with the same id and the original asked-at
    (`_reopen_set_aside_questions`). "Set aside" is DERIVED -- an open question whose
    page is archived or binned (docs/agent/specs/2026-11-02-archive-sets-aside-design.md
    section 2) -- so this writes no state on the decision: it releases a review's held
    marks, tells the partner hub, and tells the asking agent.

    WHY AN EVENT AT ALL: before the first such event (homelab-ij8.84.10) an agent
    that armed a waiter on a decision whose page was then archived waited until its
    own timeout, every time, unable to tell "not answered yet" from "he is not being
    shown this". A seat can lose a shift to that. Found by walking the gallery.

    Returns how many questions it set aside, so a caller can report a number.
    """
    now = datetime.now(UTC)
    rows = conn.execute(
        "select id, question, session, kind from decision"  # noqa: S608 -- clause from the db layer, no caller input
        " where artifact_id = %s and answered_at is null and closed_at is null"
        " order by created_at, id" + conn.for_update(),
        (artifact_id,),
    ).fetchall()
    if not rows:
        return 0
    owner = conn.execute(
        "select agent from artifact where id = %s", (artifact_id,)
    ).fetchone()[0]
    if any(kind == "review" for *_, kind in rows):
        # A set-aside review gets no verdict while the page is away; his held
        # marks go to the agent now rather than stay invisible. A reopened review
        # starts with none held.
        from artifacts import holds

        holds.release(conn, artifact_id, emit=True)
    for decision_id, question, asked_by_session, kind in rows:
        if kind == "question" and partner.enabled():
            partner.enqueue(
                conn,
                decision_id,
                "set_aside",
                {"reason": f"The page holding this question was {what}."},
                at=now,
            )
        events.append(
            conn,
            artifact_id=artifact_id,
            agent=owner,
            actor=actor,
            kind="decision_set_aside",
            summary=f"{what}: {question}",
            ref_id=decision_id,
            # Said in full, because the agent reading this has to decide what to do and
            # "set aside" alone does not say whether to give up. It does not: the
            # question is parked, not dropped.
            note=(
                f"The page holding this question was {what}, so the question is SET "
                "ASIDE -- Jeremy is not shown it while the page is away, and it cannot "
                "be answered. It is NOT closed: if the page is unarchived or restored "
                "it comes back open and unanswered with the same id, and you will get "
                "a decision_reopened event. Stop waiting on it for now; re-arm your "
                "waiter when it reopens. To withdraw it for good, close it."
            ),
            session=asked_by_session,
        )
    return len(rows)


def _reopen_set_aside_questions(conn, artifact_id: str, what: str, actor: str) -> int:
    """Bring back the questions that were set aside: stamp `reopened_at`, tell the
    partner hub and the asking agent. `what` is "unarchived" or "restored".

    Called only when the page was archived or binned and now is neither (a page
    still binned, or still archived, reopens nothing). Every open question on the
    page was set aside, including one asked while it was away (born set aside).
    The id, the asked-at and the order he sees them in are untouched: the wall
    orders by asked-at, so a parked question does not jump the queue.
    """
    now = datetime.now(UTC)
    rows = conn.execute(
        "update decision set reopened_at = %s"
        " where artifact_id = %s and answered_at is null and closed_at is null"
        " returning id, question, session, kind",
        (now, artifact_id),
    ).fetchall()
    if not rows:
        return 0
    owner = conn.execute(
        "select agent from artifact where id = %s", (artifact_id,)
    ).fetchone()[0]
    for decision_id, question, asked_by_session, kind in rows:
        if kind == "question" and partner.enabled():
            partner.enqueue(
                conn,
                decision_id,
                "reopened",
                {"reason": f"The page holding this question was {what}."},
                at=now,
            )
        events.append(
            conn,
            artifact_id=artifact_id,
            agent=owner,
            actor=actor,
            kind="decision_reopened",
            summary=f"{what}: {question}",
            ref_id=decision_id,
            note=(
                f"The page holding this question was {what}, so the question is OPEN "
                "again, unanswered, with the same id and the original asked-at. "
                "Re-arm your waiter on it if you are still waiting for an answer."
            ),
            session=asked_by_session,
        )
    return len(rows)


def _scoped(
    artifact_id: str, agent: str | None, *, live: bool = True
) -> tuple[str, tuple]:
    """The WHERE clause for a housekeeping write, and its parameters.

    `agent` is the owner the write is scoped to, or None for no scope. It has
    no default anywhere it is used: every caller has to say which of the two it
    is, because the failure mode of getting this wrong is silent and one-way --
    a route that omitted a scope would work perfectly, on everybody's pages.
    Two callers, two answers. The gallery passes None: it sits behind Jeremy's
    login, it is HIS wall, and he may tidy any page on it. The API always passes
    the calling key's agent, so a seat can only manage its own pages -- the same
    rule `patch` has always had.

    The scope is a clause in the UPDATE, never a lookup before it. A read-then-
    write would be two statements with a gap in the middle, and the gap is where
    the check stops meaning anything; this way a request that does not own the
    page changes no rows and is answered 404, which is also what it gets for a
    page that does not exist. Those two are deliberately indistinguishable: a
    different answer for "exists but is not yours" would let any key holder
    enumerate every id on the service.
    """
    # `live` False is for the two operations that act ON a deleted page --
    # recovering it and destroying it. Everything else must not see one, and
    # the default is therefore the safe direction: a new caller that forgets to
    # think about deletion gets the behaviour that cannot resurrect a page.
    clause = "where id = %s" if agent is None else "where id = %s and agent = %s"
    params = (artifact_id,) if agent is None else (artifact_id, agent)
    if live:
        clause += " and deleted_at is null"
    return clause, params


def move_many(
    db: Database,
    agent: str,
    artifact_ids: list[str],
    folder: str | None,
    folder_title: str | None = None,
) -> tuple[list[str], list[str], dict | None]:
    """File several of the agent's own pages in its folder `folder` (None takes them out), in
    one transaction. Returns `(moved, missing, folder)`; `missing` is both "no such page" and
    "not yours" (and a binned page), never told apart. An unknown slug is created, but only
    when at least one page is going into it: a batch of ids that are all missing leaves nothing
    behind. `folders.BadSlug` / `folders.FolderArchived` as for publish; nothing is written then.
    """
    _check_folder(folder, folder_title, agent=agent, artifact_id=None)
    wanted = list(dict.fromkeys(artifact_ids))
    with db.connect() as conn, conn.transaction():
        mine = {
            r[0]
            for r in conn.execute(
                "select id from artifact"  # noqa: S608 -- placeholders only
                f" where agent = %s and deleted_at is null and id in {in_list(wanted)}",
                (agent, *wanted),
            ).fetchall()
        }
        moved = [i for i in wanted if i in mine]
        missing = [i for i in wanted if i not in mine]
        filed = None
        if folder is None:
            target = None
        elif moved:
            target, canonical, created = folders.resolve_or_create(
                conn, agent, folder, folder_title
            )
            filed = {"slug": canonical, "created": created}
        if moved:
            conn.execute(
                "update artifact set folder_id = %s, archived_via_folder = null"  # noqa: S608 -- placeholders only
                f" where agent = %s and deleted_at is null and id in {in_list(moved)}",
                (target, agent, *moved),
            )
        return moved, missing, filed


MAX_COPY_TITLE_SUFFIX = " (copy)"


def duplicate(
    db: Database, artifact_id: str, *, agent: str | None
) -> tuple[Published, str]:
    """Copy a page's live body to a NEW page with its own id, address and
    history. Raises NotFound when there is no such page for this caller.

    Returns the new page AND the body it copied. The body comes back because
    the caller's next move is to render a thumbnail of it, and the alternative
    -- reading it again through `get_body` -- is scoped to the owning agent,
    which the gallery is not. Handing back what was already read is cheaper
    than the read and cannot disagree with it.

    WHAT IS COPIED IS THE BODY AND NOTHING ELSE -- no versions, no decisions, no
    answers, no thumbnail. The decisions are the part that had to be decided
    rather than assumed, and the answer is not "it would be tidy": an answered
    decision is a record of something JEREMY SAID about the original. Copying
    one would manufacture a record of him answering a question he has never
    seen, on a page he has never read. This service exists to make that class
    of fabrication impossible, so a copy starts with none of it.

    An UNANSWERED decision is not copied either, for a quieter reason: it would
    put the same question in front of him twice, and answering one would leave
    the other sitting there looking unanswered for ever.

    The copy is owned by the ORIGINAL'S agent, not by whoever pressed the
    button. Jeremy duplicating a page in his gallery does not make him its
    publisher -- the seat that wrote it is still the seat that maintains it,
    and the alternative is a page no agent can update.
    """
    scope = "" if agent is None else " and a.agent = %s"
    params = (artifact_id,) if agent is None else (artifact_id, agent)
    with db.connect() as conn:
        row = conn.execute(
            "select a.title, a.agent, v.body from artifact a"  # noqa: S608 -- `scope` is a fixed clause with a placeholder, never caller text
            " join version v on v.artifact_id = a.id and v.n = a.current_version"
            f" where a.id = %s and a.deleted_at is null{scope}",
            params,
        ).fetchone()
    if row is None:
        raise NotFound()
    title, owner, body = row[0], row[1], row[2]
    # Trimmed so the suffix always fits, rather than the publish being refused
    # for a title the caller never typed.
    room = MAX_TITLE_BYTES - len(MAX_COPY_TITLE_SUFFIX.encode())
    while len(title.encode()) > room:
        title = title[:-1]
    return publish(db, owner, f"{title}{MAX_COPY_TITLE_SUFFIX}", body), body


# How long a deleted page can be recovered. Jeremy chose thirty days, and chose
# it partly because it is honest about eventually being gone -- so the number
# living here, where the sweep reads it, matters: a window nothing enforces is
# not a window, it is a hidden row for ever.
DELETE_WINDOW = timedelta(days=30)


def set_deleted(
    db: Database, artifact_id: str, deleted: bool, *, agent: str | None
) -> bool:
    """Delete a page, or put it back. False if nothing changed.

    A DELETE HERE DESTROYS NOTHING. The body, every version, and every decision
    with his answers and his notes are all still exactly where they were -- one
    nullable column moves. Deleting is the cheap way to tidy a gallery and it
    must not be the way the record of what he decided is destroyed; that is
    homelab-ij8.44's lesson and it bit twice in one day.

    `deleted` False is the recovery, and it is deliberately the same call with
    the flag flipped rather than a separate `restore`: two functions would be
    two places for the scoping rule to drift apart, and recovery has to be
    available to exactly whoever could delete it.
    """
    # `live=False`: recovery has to be able to FIND a deleted page, and so does
    # deleting an already-deleted one (a no-op that should report False rather
    # than 404). The default clause excludes exactly the rows this call is for.
    where, params = _scoped(artifact_id, agent, live=False)
    with db.connect() as conn, conn.transaction():
        if deleted:
            _lock_open_decisions(conn, artifact_id)
        before = _set_aside_state(conn, artifact_id)
        done = conn.execute(
            f"update artifact set deleted_at = %s {where}",  # noqa: S608 -- see _scoped
            (datetime.now(UTC) if deleted else None, *params),
        )
        if done.rowcount > 0 and before is not None:
            was, archived_at = before
            actor = agent or "jeremy"
            if deleted and not was:
                _set_aside_open_questions(conn, artifact_id, "binned", actor)
            elif not deleted and was and archived_at is None:
                _reopen_set_aside_questions(conn, artifact_id, "restored", actor)
        return done.rowcount > 0


def destroy(db: Database, artifact_id: str, *, agent: str | None) -> bool:
    """Remove a page and everything under it, for good. No window, no undo.

    The rare one. Separate from `set_deleted` on purpose and not reachable by
    passing a flag to it: "tidy this away" and "this must not exist anywhere"
    are different intentions, and an interface where the second is a parameter
    of the first is an interface where it happens by accident.

    The versions, decisions, comments and events go with the row via their
    foreign keys. That is the point of it, and it is why this is the only
    operation in this module that can lose something he said.
    """
    # `live=False`: the usual thing being destroyed is a page already in the
    # window. Destroying a live page is allowed too -- "this must not exist
    # anywhere" does not require deleting it first.
    where, params = _scoped(artifact_id, agent, live=False)
    with db.connect() as conn:
        done = conn.execute(f"delete from artifact {where}", params)  # noqa: S608 -- see _scoped
        return done.rowcount > 0


def list_deleted(db: Database, agent: str | None = None) -> list[ArtifactRow]:
    """What is in the window, soonest to go first.

    Ordered by when it was deleted, OLDEST first, so the one about to expire is
    the one at the top -- the opposite of every other listing here, and right
    for the same reason they are wrong for this: the useful question about a
    recycle bin is not "what did I just delete" but "what am I about to lose".

    `agent` None is his gallery, which sees every seat's. An agent sees only
    its own.
    """
    scope = "" if agent is None else " and artifact.agent = %s"
    params = () if agent is None else (agent,)
    with db.connect() as conn:
        rows = conn.execute(
            "select artifact.id, artifact.title, artifact.agent,"  # noqa: S608 -- `scope` is a fixed clause with a placeholder
            " artifact.visibility, artifact.current_version, version.revision,"
            " artifact.updated_at, artifact.archived_at, artifact.pinned_at,"
            " artifact.deleted_at, version.session,"
            # Questions set aside while the page sits in the bin: shown to him as such,
            # never counted as waiting.
            " (select count(*) from decision d where d.artifact_id = artifact.id"
            "    and d.answer is null and d.closed_at is null and d.kind <> 'form')"
            " from artifact"
            " join version on version.artifact_id = artifact.id"
            " and version.n = artifact.current_version"
            f" where artifact.deleted_at is not null{scope}"
            " order by artifact.deleted_at asc, artifact.id asc",
            params,
        ).fetchall()
    return [
        ArtifactRow(
            id=row[0],
            title=row[1],
            agent=row[2],
            visibility=row[3],
            current_version=row[4],
            revision=row[5],
            updated_at=row[6],
            url=url_for(row[0]),
            page_url=page_url_for(row[0]),
            archived_at=row[7],
            pinned_at=row[8],
            deleted_at=row[9],
            last_session=row[10],
            open_questions=row[11],
        )
        for row in rows
    ]


def purge_expired(db: Database, *, window: timedelta = DELETE_WINDOW) -> list[str]:
    """Destroy everything whose window has run out. Returns the ids it removed.

    Returns the IDS, not a count, and that is not decoration: the caller logs
    them, and a destroy that reports "3" gives whoever reads that log no way to
    find out afterwards what went. This is the one operation here that cannot
    be undone, so it says exactly what it did.
    """
    now = datetime.now(UTC)
    cutoff = now - window
    with db.connect() as conn, conn.transaction():
        # A question still open at the purge was set aside while its page sat in the
        # bin; the purge closes it for good, so the partner hub is told `withdrawn`
        # BEFORE the delete takes the decision rows (the outbox keeps no foreign key
        # to them, but `enqueue` bumps the decision's own counter).
        if partner.enabled():
            for (decision_id,) in conn.execute(
                "select d.id from decision d join artifact a on a.id = d.artifact_id"
                " where a.deleted_at is not null and a.deleted_at < %s"
                " and d.kind = 'question' and d.answer is null and d.closed_at is null",
                (cutoff,),
            ).fetchall():
                partner.enqueue(
                    conn,
                    decision_id,
                    "withdrawn",
                    {"reason": "The page holding this question was deleted for good."},
                    at=now,
                )
        rows = conn.execute(
            "delete from artifact where deleted_at is not null and deleted_at < %s"
            " returning id",
            (cutoff,),
        ).fetchall()
    return [row[0] for row in rows]


def set_archived(
    db: Database, artifact_id: str, archived: bool, *, agent: str | None
) -> bool:
    """Take a page off the wall, or put it back. False if nothing was changed.

    Reversible by construction: one nullable column, and nothing else in the
    page is touched. Its versions, its decisions and the answers he gave on it
    are all exactly where they were.
    """
    where, params = _scoped(artifact_id, agent)
    with db.connect() as conn, conn.transaction():
        if archived:
            _lock_open_decisions(conn, artifact_id)
        before = _set_aside_state(conn, artifact_id)
        done = conn.execute(
            # A page archived or unarchived by itself is no longer the folder's doing.
            f"update artifact set archived_at = %s, archived_via_folder = null {where}",  # noqa: S608 -- _scoped emits a fixed clause with placeholders, never caller text
            (datetime.now(UTC) if archived else None, *params),
        )
        if done.rowcount > 0 and before is not None:
            was, _ = before
            deleted_at = conn.execute(
                "select deleted_at from artifact where id = %s", (artifact_id,)
            ).fetchone()[0]
            actor = agent or "jeremy"
            if archived and not was:
                _set_aside_open_questions(conn, artifact_id, "archived", actor)
            elif not archived and was and deleted_at is None:
                _reopen_set_aside_questions(conn, artifact_id, "unarchived", actor)
        return done.rowcount > 0


def folder_by_id(db: Database, folder_id: str):
    """`(id, owner, slug, title, archived_at)` of a folder, found by its opaque id (the wall
    names folders that way), or None."""
    with db.connect() as conn:
        return folders.get(conn, folder_id)


def folders_for_page(db: Database, artifact_id: str):
    """For the wall's "Move to folder": `(agent, current folder id, choices)` for a page, or
    None for no such page. `choices` is `[(id, title, mine)]`: his own folders (any agent's page
    may go in) and the page's own agent's folders, live ones only, his first."""
    with db.connect() as conn:
        page = conn.execute(
            "select agent, folder_id from artifact where id = %s and deleted_at is null",
            (artifact_id,),
        ).fetchone()
        if page is None:
            return None
        rows = conn.execute(
            "select id, title, owner from folder where archived_at is null"
            " and owner in (%s, %s) order by (owner = %s) desc, lower(title), slug",
            (folders.HIS, page[0], folders.HIS),
        ).fetchall()
    return page[0], page[1], [(r[0], r[1], r[2] == folders.HIS) for r in rows]


def file_page_for_reader(
    db: Database,
    artifact_id: str,
    folder_id: str | None,
    new_title: str | None = None,
) -> bool:
    """He files a page, unscoped (his wall, behind the same-origin guard). `folder_id` None
    takes it out; `new_title` (with no id) makes a NEW folder of his, named so, and files it
    there. A folder must be his or the page's own agent's, else NotFound (agents' folders stay
    one agent each); an archived one is `folders.FolderArchived`. False for no such page.
    Filing is a shelf fact: no version, and the archived-via-folder marker is cleared."""
    if new_title is not None:
        _guard(new_title, MAX_TITLE_BYTES, "title", agent=folders.HIS, artifact_id=None)
    with db.connect() as conn, conn.transaction():
        page = conn.execute(
            "select agent from artifact where id = %s and deleted_at is null",
            (artifact_id,),
        ).fetchone()
        if page is None:
            return False
        if new_title is not None:
            target, _slug = folders.create_his(conn, new_title)
        elif folder_id is None:
            target = None
        else:
            row = folders.get(conn, folder_id)
            if row is None or row[1] not in (folders.HIS, page[0]):
                raise NotFound()
            # Lock the folder row, then read archived_at again: a folder archive that
            # commits between the first read and the update must not be raced past.
            locked = conn.execute(
                "select archived_at from folder where id = %s" + conn.for_update(),  # noqa: S608 -- for_update() is a fixed clause
                (folder_id,),
            ).fetchone()
            if locked is None:
                raise NotFound()
            if locked[0] is not None:
                raise folders.FolderArchived(row[2])
            target = row[0]
        conn.execute(
            "update artifact set folder_id = %s, archived_via_folder = null"
            " where id = %s",
            (target, artifact_id),
        )
    return True


def rename_folder(
    db: Database, agent: str, slug: str, new_slug: str | None, title: str | None
) -> dict | None:
    """`folders.rename_folder` in one transaction, with the title guarded like any text that
    reaches storage. None for no such folder of this agent's; `folders.SlugTaken` on a clash."""
    _check_folder(new_slug, title, agent=agent, artifact_id=None)
    with db.connect() as conn, conn.transaction():
        return folders.rename_folder(conn, agent, slug, new_slug, title)


def set_folder_archived(
    db: Database,
    agent: str,
    slug: str,
    archived: bool,
    *,
    confirm: bool,
    actor: str | None = None,
) -> dict | None:
    """Archive or unarchive one of the agent's own folders (an old slug resolves), in one
    transaction (docs/agent/specs/2026-11-02-folders-design.md 3.4). None for no such folder.

    Archiving without `confirm` changes nothing and returns `{"would_archive": {"pages",
    "questions_set_aside"}}`. With it, every live, unarchived page of the agent's in the folder
    is archived through the same mechanism as a single-page archive (questions set aside, the
    asking agents told) and marked `archived_via_folder`; pages already archived on their own
    are left as they were, unmarked. Unarchiving restores only the pages carrying the marker and
    brings their questions back; a page that is binned is restored as a page restore does it
    (its questions stay set aside until the bin is emptied the other way).

    `agent` is the folder's OWNER. For his own folder (`folders.HIS`) the pages are any
    agent's, so the page filter drops the publisher clause; the gallery passes `actor="jeremy"`
    so the set-aside and the told agents name who archived it."""
    actor = actor or agent
    unscoped = agent == folders.HIS
    who = "" if unscoped else " and agent = %s"
    who_params: tuple = () if unscoped else (agent,)
    with db.connect() as conn, conn.transaction():
        found = folders.find(conn, agent, slug)
        if found is None:
            return None
        folder_id, canonical = found[0], found[1]
        if archived:
            page_rows = conn.execute(
                "select id from artifact where folder_id = %s"  # noqa: S608 -- clause from the db layer, no caller input
                f"{who} and deleted_at is null and archived_at is null order by id"
                + (conn.for_update() if confirm else ""),
                (folder_id, *who_params),
            ).fetchall()
            pages = [r[0] for r in page_rows]
            if not confirm:
                waiting = 0
                for pid in pages:
                    waiting += conn.execute(
                        "select count(*) from decision where artifact_id = %s"
                        " and answered_at is null and closed_at is null",
                        (pid,),
                    ).fetchone()[0]
                return {
                    "slug": canonical,
                    "would_archive": {
                        "pages": len(pages),
                        "questions_set_aside": waiting,
                    },
                }
            now = datetime.now(UTC)
            conn.execute(
                "update folder set archived_at = %s where id = %s and archived_at is null",
                (now, folder_id),
            )
            aside = 0
            done_pages = 0
            for pid in pages:
                # Decisions first, then the artifact row, as every archive does
                # (`_lock_open_decisions`: the reverse order can deadlock with a mark). The
                # update re-checks everything the select saw, so a page a concurrent move-out,
                # bin or page-level archive got to in between is left alone, not given a
                # stale marker.
                _lock_open_decisions(conn, pid)
                changed = conn.execute(
                    "update artifact set archived_at = %s, archived_via_folder = %s"  # noqa: S608 -- `who` is a fixed clause
                    f" where id = %s and folder_id = %s{who}"
                    " and deleted_at is null and archived_at is null",
                    (now, folder_id, pid, folder_id, *who_params),
                ).rowcount
                if changed:
                    done_pages += 1
                    aside += _set_aside_open_questions(conn, pid, "archived", actor)
            return {
                "slug": canonical,
                "archived": True,
                "pages": done_pages,
                "questions_set_aside": aside,
            }
        conn.execute("update folder set archived_at = null where id = %s", (folder_id,))
        restored = conn.execute(
            "select id, deleted_at from artifact where archived_via_folder = %s"  # noqa: S608 -- clause from the db layer, no caller input
            " and folder_id = %s order by id" + conn.for_update(),
            (folder_id, folder_id),
        ).fetchall()
        reopened = 0
        restored_pages = 0
        for pid, deleted_at in restored:
            # Re-checked in the update: a page a concurrent move-out or page-level action
            # already detached (marker cleared) is not touched.
            changed = conn.execute(
                "update artifact set archived_at = null, archived_via_folder = null"
                " where id = %s and archived_via_folder = %s and folder_id = %s",
                (pid, folder_id, folder_id),
            ).rowcount
            if changed:
                restored_pages += 1
                if deleted_at is None:
                    reopened += _reopen_set_aside_questions(
                        conn, pid, "unarchived", actor
                    )
        return {
            "slug": canonical,
            "archived": False,
            "pages_restored": restored_pages,
            "questions_reopened": reopened,
        }


def rename(db: Database, artifact_id: str, title: str, *, agent: str | None) -> bool:
    """Give a page a different name, without republishing it.

    Renaming used to mean sending the whole body again with `--title`, which is
    fine for an agent that has the file and impossible for Jeremy in a browser.
    What a page is FOR is often only clear after a few revisions, and a name
    that turned out wrong should never be a reason to abandon an address people
    already have.

    Guarded like any other text reaching storage -- size and the secret scan --
    because "it is only a title" is how a field skips its checks. The guards run
    BEFORE the ownership clause does any work, so a refusal is logged against
    whoever actually sent the title rather than against the page's owner.
    """
    actor = agent or "jeremy"
    _guard(title, MAX_TITLE_BYTES, "title", agent=actor, artifact_id=artifact_id)
    _check_secrets(title, "title", agent=actor, artifact_id=artifact_id)
    where, params = _scoped(artifact_id, agent)
    with db.connect() as conn:
        done = conn.execute(
            f"update artifact set title = %s, updated_at = %s {where}",  # noqa: S608 -- see _scoped
            (title, datetime.now(UTC), *params),
        )
        return done.rowcount > 0


def set_pinned(
    db: Database, artifact_id: str, pinned: bool, *, agent: str | None
) -> bool:
    """Pin a page to the front of the wall, or unpin it.

    Its own column rather than a flag on `updated_at`, because the two orderings
    answer different questions: "what moved last" is what the wall has always
    shown, and "what I care about" is what pinning adds. A pinned page that has
    not changed in a month still leads.
    """
    where, params = _scoped(artifact_id, agent)
    with db.connect() as conn:
        done = conn.execute(
            f"update artifact set pinned_at = %s {where}",  # noqa: S608 -- see _scoped
            (datetime.now(UTC) if pinned else None, *params),
        )
        return done.rowcount > 0


def mark_read(db: Database, artifact_id: str) -> None:
    """Jeremy opened this page in the gallery: record the version he saw
    (homelab-ij8.103). Only ever called from the gallery, behind his login."""
    with db.connect() as conn:
        conn.execute(
            "insert into page_read (artifact_id, read_version, read_at)"
            " select id, current_version, %s from artifact"
            " where id = %s and current_version is not null"
            " on conflict (artifact_id) do update"
            " set read_version = excluded.read_version, read_at = excluded.read_at",
            (datetime.now(UTC), artifact_id),
        )


def share_url(artifact_id: str, token: str) -> str:
    """The address a shared page is read at: its own origin, top level, no
    login (homelab-ij8.101)."""
    return f"https://{artifact_id}.{content_domain()}/s/{token}/"


def share_token(db: Database, artifact_id: str) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            "select token from artifact_share where artifact_id = %s", (artifact_id,)
        ).fetchone()
    return row[0] if row else None


def start_sharing(db: Database, artifact_id: str) -> str | None:
    """His deliberate act: the page's link, created if it has none. Returns the
    token, or None for a page that does not exist or is deleted. `visibility`
    becomes 'link', the switch public and email sharing will extend."""
    token = secrets.token_urlsafe(24)
    with db.connect() as conn, conn.transaction():
        exists = conn.execute(
            "update artifact set visibility = 'link' where id = %s and deleted_at is null"
            " returning id",
            (artifact_id,),
        ).fetchone()
        if exists is None:
            return None
        conn.execute(
            "insert into artifact_share (artifact_id, token) values (%s, %s)"
            " on conflict (artifact_id) do nothing",
            (artifact_id, token),
        )
        row = conn.execute(
            "select token from artifact_share where artifact_id = %s", (artifact_id,)
        ).fetchone()
    return row[0]


def stop_sharing(db: Database, artifact_id: str) -> bool:
    """Withdraw the link. It stops working at once: the row is gone, and the
    content origin looks it up on every request. Sharing again mints a NEW
    link, so the old one can never come back to life."""
    with db.connect() as conn, conn.transaction():
        gone = conn.execute(
            "delete from artifact_share where artifact_id = %s", (artifact_id,)
        ).rowcount
        conn.execute(
            "update artifact set visibility = 'private' where id = %s", (artifact_id,)
        )
    return gone > 0


def shared_page(
    db: Database, artifact_id: str, token: str
) -> tuple[str, datetime] | None:
    """The live body and its publication time, if `token` is this page's live
    share link and the page is not deleted; otherwise None."""
    if not token or len(token) > 128:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "select v.body, v.created_at from artifact_share s"
            " join artifact a on a.id = s.artifact_id"
            " join version v on v.artifact_id = a.id and v.n = a.current_version"
            " where s.artifact_id = %s and s.token = %s and a.deleted_at is null"
            " and v.status = 'live'",
            (artifact_id, token),
        ).fetchone()
    return (row[0], row[1]) if row else None


def shared_page_body(db: Database, artifact_id: str) -> tuple[str, datetime] | None:
    """The live body and its time, for a reader already let in by an email
    share (homelab-ij8.16); the caller has checked the share and the device."""
    with db.connect() as conn:
        row = conn.execute(
            "select v.body, v.created_at from artifact a"
            " join version v on v.artifact_id = a.id and v.n = a.current_version"
            " where a.id = %s and a.deleted_at is null and v.status = 'live'",
            (artifact_id,),
        ).fetchone()
    return (row[0], row[1]) if row else None


def email_share_url(artifact_id: str, token: str) -> str:
    """One person's own link to a page (homelab-ij8.16)."""
    return f"https://{artifact_id}.{content_domain()}/e/{token}/"


class NoSuchAgent(Exception):
    """The destination is not a machine with a working key."""


def owner(db: Database, artifact_id: str) -> str | None:
    """The agent a page belongs to now, whatever spelling it was given as."""
    with db.connect() as conn:
        row = conn.execute(
            "select agent from artifact where id = %s", (artifact_id,)
        ).fetchone()
    return row[0] if row else None


def _spelling(name: str) -> str:
    return name.lower().replace("_", "-")


def _live_agent(conn, name: str) -> str:
    """The live agent `name` means. Exact first; failing that, the one live
    agent whose name differs only in case or `_`/`-`. The page list shows
    `homelab-ops` and the vault's seat is `homelab_ops`, and the homelab ops
    seat's first give (24 Sep 2026) was refused as though the agent did not
    exist. Two candidates is ambiguous, and nothing is guessed. A name an
    agent had before it was renamed means the agent it became."""
    rows = conn.execute(
        "select distinct agent from agent_key where revoked_at is null"
    ).fetchall()
    live = [r[0] for r in rows]
    # A renamed agent's old name still means it (6 Oct 2026), in either spelling.
    name = agent_rename._resolve(conn, name)
    if name in live:
        return name
    renamed = agent_rename._resolve(conn, _spelling(name))
    if renamed != _spelling(name) and renamed in live:
        return renamed
    near = [a for a in live if _spelling(a) == _spelling(name)]
    if len(near) == 1:
        return near[0]
    raise NoSuchAgent()


def transfer(
    db: Database, artifact_id: str, to_agent: str, *, agent: str | None
) -> str | None:
    """Give a page to another agent without changing its address
    (homelab-ij8.87). `agent` is the owner giving it away -- an agent can only
    give what it owns -- or None for the gallery (his wall, any page). The
    page's own events move with it, so the new owner sees its history and the
    old one stops being woken; decisions and comments follow through the page;
    who wrote each version is history and is NOT rewritten. Returns the old
    owner, or None when nothing matched."""
    with db.connect() as conn, conn.transaction():
        to_agent = _live_agent(conn, to_agent)
        where, params = _scoped(artifact_id, agent)
        row = conn.execute(
            f"select agent from artifact {where}"  # noqa: S608 -- fixed clause from _scoped
            + conn.for_update(),
            params,
        ).fetchone()
        if row is None:
            return None
        old = row[0]
        if old == to_agent:
            return old
        conn.execute(
            "update artifact set agent = %s where id = %s", (to_agent, artifact_id)
        )
        conn.execute(
            "update event set agent = %s where artifact_id = %s and agent = %s",
            (to_agent, artifact_id, old),
        )
        # An agent's folder holds that agent's pages: the new owner sorts it itself. A folder
        # of his (owner 'jeremy') may hold anyone's, so the page stays in it.
        conn.execute(
            "update artifact set folder_id = null, archived_via_folder = null"
            " where id = %s and folder_id in"
            " (select id from folder where owner <> 'jeremy')",
            (artifact_id,),
        )
    return old


def save_render_errors(
    db: Database, artifact_id: str, version_n: int, errors: list[str]
) -> None:
    """The page's script errors from its render, kept with that version
    (homelab-ij8.72). An empty list means it rendered cleanly."""
    with db.connect() as conn:
        conn.execute(
            "update version set render_errors = %s, rendered_at = %s"
            " where artifact_id = %s and n = %s",
            (jsonb(errors), datetime.now(UTC), artifact_id, version_n),
        )


def render_report(db: Database, artifact_id: str) -> dict:
    """For the live version: {"rendered": bool, "errors": [...]}. Not rendered
    means the render has not finished, or this deployment has no browser."""
    with db.connect() as conn:
        row = conn.execute(
            "select v.render_errors, v.rendered_at from artifact a join version v"
            " on v.artifact_id = a.id and v.n = a.current_version where a.id = %s",
            (artifact_id,),
        ).fetchone()
    if row is None or row[1] is None:
        return {"rendered": False, "errors": []}
    return {"rendered": True, "errors": list(row[0] or [])}
