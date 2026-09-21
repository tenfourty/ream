r"""Comments: an agent's page can be talked to, not just looked at.

A comment's `body` is content exactly like a page's `body` -- an agent writes it, it
is displayed verbatim, and it can just as easily carry a pasted secret as any page
can -- so it passes through the SAME two guards `store.publish`/`store.patch` use:
a size cap (`MAX_COMMENT_BYTES`) and the secret scanner, in that order, cheapest
first. `quote` is the same kind of content (it is, after all, a verbatim slice of a
page body Jeremy selected) and gets its own, smaller cap (`MAX_QUOTE_BYTES`) and the
same scan. This module does not invent its own guard machinery for any of that: it
calls straight into `store`'s private `_check_size`/`_check_secrets` helpers and
raises `store`'s own `BodyTooLarge`, `InvalidText`, `SecretFound`, `ScanFailed` and
`NotFound` -- a caller (the API layer, a future UI) that already knows how to handle
those from publishing a page handles a comment refusal identically, for free.

Ownership is checked before the scanner ever runs, exactly as `store.py`'s module
docstring lays out for `patch`: the scanner holds the GIL while it reads two views
of the text, and that cost is never spent on behalf of a caller with no right to the
artifact in the first place. Concretely, in `add()`: the cheap in-memory checks
(size, and anchor shape) run first and touch no database; ownership of the artifact,
and -- for a reply -- existence of the thread it names, run next, one query each;
the scanner runs last, on whatever body and quote are actually about to be stored.
An unknown artifact and someone else's look identical either way (both `NotFound`),
matching `store.publish`/`store.patch`.
"""

from dataclasses import dataclass

from psycopg.types.json import Jsonb

from artifacts import events, ids, nudge, store
from artifacts.db import Database

# A comment is a remark about a page, not a page in itself -- 8 KB is generous for
# prose and small enough that a thread of hundreds never becomes its own storage
# problem. Well below MAX_BODY_BYTES (512 KiB): nobody pastes a page into a comment.
MAX_COMMENT_BYTES = 8_000

# A quote is a short excerpt Jeremy selected, not the passage it came from -- 2 KB
# is room for several paragraphs, comfortably more than any real selection, while
# still refusing someone pasting the whole page back in as its own "quote".
MAX_QUOTE_BYTES = 2_000

# Every read of a comment row, in the fixed column order `_row_to_comment` expects.
# Qualified with `comment.` throughout -- `list_for_agent` joins `artifact`, which
# also has an `id` and a `created_at`, and an unqualified name there is ambiguous to
# Postgres. Qualifying it here too, even where nothing else is joined, means one
# constant serves every query in this file rather than two near-identical ones.
_COLUMNS = (
    "comment.id, comment.artifact_id, comment.version_n, comment.thread_root,"
    " comment.anchor, comment.quote, comment.author_kind, comment.author,"
    " comment.body, comment.created_at, comment.resolved_at, comment.resolved_by"
)

# Correlated subquery, not a join: for the comment a given row names -- itself, if it
# is a root (thread_root is null), else the root it points at -- this is that root's
# resolved_at. A reply's OWN resolved_at is not good enough to test here even though
# resolve() always resolves a whole thread in one statement (see resolve() below):
# a reply added to an ALREADY-resolved thread is born with resolved_at still null
# (nothing re-stamps it retroactively), so checking the reply's own column would
# show it -- and only it -- as unresolved while the rest of its thread is hidden.
# Rule: "include_resolved=False hides threads whose ROOT is resolved", always the
# root, which is exactly what this clause tests.
_ROOT_RESOLVED_AT = (
    "(select root.resolved_at from comment as root"
    " where root.id = coalesce(comment.thread_root, comment.id))"
)


@dataclass(frozen=True)
class CommentRow:
    id: str
    artifact_id: str
    version_n: int
    thread_root: str | None
    anchor: dict
    quote: str
    author_kind: str
    author: str
    body: str
    created_at: str  # isoformat
    resolved_at: str | None
    resolved_by: str | None


class InvalidAnchor(Exception):
    def __init__(self) -> None:
        super().__init__("anchor")
        self.field = "anchor"


class NoSuchThread(Exception): ...


def _is_number(value: object) -> bool:
    # bool is an int subclass in Python -- True/False must never pass a numeric
    # check meant for a fractional page-position, or {"kind": "point", "x": True,
    # "y": False} would validate as (1.0, 0.0).
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_anchor(anchor: dict | None) -> dict:
    """Normalise and validate an anchor. `None` -- the default for a caller who
    doesn't mention where a comment belongs -- means a whole-page comment. Anything
    else must match exactly one of the three shapes the design allows; anything that
    doesn't, for any reason (an unknown `kind`, an extra or missing key, a value of
    the wrong type, a number out of range), raises `InvalidAnchor`.

    The exception never says which of those it was, and never carries the anchor
    itself: `InvalidAnchor.__init__` takes no arguments and names only the field
    `anchor`. An anchor is caller-supplied and, unlike `body`, was never going to be
    scanned for secrets -- echoing its keys or values back (`f"unexpected key
    {key!r}"`, `f"got {anchor!r}"`) would hand a hostile caller a free oracle for
    probing the validator one field at a time, which is exactly the mistake already
    open as homelab-ij8.4 elsewhere in this repo. Every branch below raises the same
    bare `InvalidAnchor()` so there is only one way to get this wrong, not one per
    branch.
    """
    if not isinstance(anchor, dict):
        if anchor is None:
            return {"kind": "page"}
        raise InvalidAnchor()

    kind = anchor.get("kind")

    if kind == "page":
        if anchor.keys() != {"kind"}:
            raise InvalidAnchor()
        return {"kind": "page"}

    if kind == "point":
        if anchor.keys() != {"kind", "x", "y"}:
            raise InvalidAnchor()
        x, y = anchor["x"], anchor["y"]
        if not (_is_number(x) and _is_number(y)):
            raise InvalidAnchor()
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise InvalidAnchor()
        return {"kind": "point", "x": float(x), "y": float(y)}

    if kind == "range":
        if anchor.keys() != {"kind", "path", "start", "end"}:
            raise InvalidAnchor()
        path, start, end = anchor["path"], anchor["start"], anchor["end"]
        if not isinstance(path, str) or len(path) > 512:
            raise InvalidAnchor()
        if not (_is_int(start) and _is_int(end)):
            raise InvalidAnchor()
        if not (0 <= start < end):
            raise InvalidAnchor()
        return {"kind": "range", "path": path, "start": start, "end": end}

    raise InvalidAnchor()


def _row_to_comment(row) -> CommentRow:
    return CommentRow(
        id=row[0],
        artifact_id=row[1],
        version_n=row[2],
        thread_root=row[3],
        anchor=row[4],
        quote=row[5],
        author_kind=row[6],
        author=row[7],
        body=row[8],
        created_at=row[9].isoformat(),
        resolved_at=row[10].isoformat() if row[10] is not None else None,
        resolved_by=row[11],
    )


def add(
    db: Database,
    *,
    artifact_id: str,
    author_kind: str,
    author: str,
    body: str,
    anchor: dict | None = None,
    quote: str = "",
    thread_root: str | None = None,
    agent: str | None = None,
) -> CommentRow:
    """Write a comment. `agent`, when given, is the identity the artifact must be
    owned by -- an agent answering comments on its OWN pages, via the keyed API.
    `agent=None` means the caller is the gallery, acting for Jeremy, who owns
    everything by definition: only the artifact's existence is checked, never an
    `agent` column match. Either way `author`/`author_kind` are what gets stored and
    displayed -- they are never used for the ownership check itself.

    `author` (not `agent`, which can be None) is what's passed to `store`'s guards
    as their `agent=` logging identity, so a refusal line always names who actually
    wrote the content, human or agent alike.
    """
    is_reply = thread_root is not None

    # Cheapest first, no database touched yet. A reply's own `anchor`/`quote`
    # arguments are about to be thrown away entirely in favour of the root's (see
    # below), so there is nothing to gain by validating or size-guarding values
    # that will never be looked at again -- only `body`, which is always the
    # caller's own and always stored, is guarded unconditionally.
    store._check_size(
        body, MAX_COMMENT_BYTES, "body", agent=author, artifact_id=artifact_id
    )
    if is_reply:
        effective_anchor = None  # resolved from the root below
        effective_quote = None
    else:
        store._check_size(
            quote, MAX_QUOTE_BYTES, "quote", agent=author, artifact_id=artifact_id
        )
        effective_anchor = validate_anchor(anchor)
        effective_quote = quote

    with db.connect() as conn:
        if agent is not None:
            owned = store._owned(conn, artifact_id, agent)
        else:
            owned = conn.execute(
                "select id, title, agent, visibility, current_version"
                " from artifact where id = %s",
                (artifact_id,),
            ).fetchone()
        if owned is None:
            raise store.NotFound()
        version_n = owned[4]

        if is_reply:
            root = conn.execute(
                "select comment.anchor, comment.quote from comment"
                " where comment.id = %s and comment.artifact_id = %s"
                " and comment.thread_root is null",
                (thread_root, artifact_id),
            ).fetchone()
            if root is None:
                raise NoSuchThread()
            effective_anchor, effective_quote = root[0], root[1]

        # The scanner last, on whatever will actually be stored -- for a reply
        # that's the root's own quote, inherited rather than the caller's, and
        # scanning it again here is cheap and keeps one invariant true without
        # exception: every quote that lands in a row passed a scan on THIS call.
        store._check_secrets(body, "body", agent=author, artifact_id=artifact_id)
        store._check_secrets(
            effective_quote, "quote", agent=author, artifact_id=artifact_id
        )

        owning_agent = owned[2]
        title = owned[1]
        # Which SESSION published the page, so the seat that wrote it can pick
        # its own work out of a shared feed (homelab-ij8.32). A label on the
        # event, never a scope.
        page_session = conn.execute(
            "select session from artifact where id = %s", (artifact_id,)
        ).fetchone()[0]
        with conn.transaction():
            comment_id = ids.new_id()
            conn.execute(
                "insert into comment (id, artifact_id, version_n, thread_root, anchor,"
                " quote, author_kind, author, body)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    comment_id,
                    artifact_id,
                    version_n,
                    thread_root,
                    Jsonb(effective_anchor),
                    effective_quote,
                    author_kind,
                    author,
                    body,
                ),
            )
            # THE HALF THAT WAS MISSING. The comment table, the anchor
            # validator and the agent API have existed since 19 Sep; what did
            # not exist was anything that TOLD the agent. Jeremy ruled (a) on
            # 19 Sep: the agent that owns the page hears while it is still
            # working, and anything unanswered turns up in his digest.
            #
            # Not when the owner comments on its own page: an event means
            # "something happened on your page that you did not do", and a
            # seat woken by its own reply is a watcher crying wolf.
            event_seq = None
            if author != owning_agent:
                event_seq = events.append(
                    conn,
                    artifact_id=artifact_id,
                    agent=owning_agent,
                    actor=author,
                    kind="comment_added",
                    summary=body,
                    # The comment itself, so the agent can read the thread, the
                    # quote and where on the page it was anchored.
                    ref_id=comment_id,
                    # His words in full. The summary is one line in a list; the
                    # comment is the thing he actually said.
                    note=body,
                    session=page_session,
                )
        created = conn.execute(
            f"select {_COLUMNS} from comment where comment.id = %s",  # noqa: S608 -- _COLUMNS is a fixed module constant, never caller input
            (comment_id,),
        ).fetchone()
    # Fire-and-forget, and only once the transaction above has committed: a
    # nudge for a row that then rolled back would send an agent to read
    # something that never happened. `emit` never raises and never blocks --
    # commenting is what Jeremy came to do, and telling a seat about it is a
    # courtesy on top of a journal that already has the comment durably.
    if event_seq is not None:
        nudge.emit(
            kind="comment_added",
            artifact_id=artifact_id,
            title=title,
            agent=owning_agent,
            summary=body,
            event_id=str(event_seq),
        )
    return _row_to_comment(created)


def list_for_artifact(
    db: Database, artifact_id: str, *, include_resolved: bool = False
) -> list[CommentRow]:
    """Roots and replies together, oldest first (`id` breaks a tie on `created_at`,
    which two comments can share). Ownership is the caller's job -- this function
    trusts `artifact_id` outright, exactly as `store`'s own listing helpers do."""
    # Every interpolated piece here (`_COLUMNS`, `_ROOT_RESOLVED_AT`) is a fixed
    # module constant, never caller input -- `artifact_id` travels as a bound
    # parameter below, not string-built.
    sql = f"select {_COLUMNS} from comment where comment.artifact_id = %s"  # noqa: S608
    if not include_resolved:
        sql += f" and {_ROOT_RESOLVED_AT} is null"
    sql += " order by comment.created_at asc, comment.id asc"
    with db.connect() as conn:
        rows = conn.execute(sql, (artifact_id,)).fetchall()
    return [_row_to_comment(row) for row in rows]


def list_for_agent(
    db: Database, agent: str, *, include_resolved: bool = False, limit: int = 500
) -> list[CommentRow]:
    """Every comment across every artifact `agent` owns, newest first -- the
    polling surface an agent uses to find what Jeremy said, so newest matters
    more than oldest and a hard `limit` protects an agent with a very long history
    from pulling its entire back catalogue on every poll."""
    # As in list_for_artifact: every interpolated piece is a fixed module constant.
    sql = (
        f"select {_COLUMNS} from comment"  # noqa: S608
        " join artifact on artifact.id = comment.artifact_id"
        " where artifact.agent = %s"
    )
    if not include_resolved:
        sql += f" and {_ROOT_RESOLVED_AT} is null"
    sql += " order by comment.created_at desc, comment.id desc limit %s"
    with db.connect() as conn:
        rows = conn.execute(sql, (agent, limit)).fetchall()
    return [_row_to_comment(row) for row in rows]


def _settled_summary(quote: str, body: str) -> str:
    """Which conversation was settled, in words.

    The quote first when the thread was anchored to something on the page --
    that is how the reader remembers it ("the bit about the backups"), not by
    the reply it opened with. The body when there is no quote. Bounded here
    only to keep the join short; `events.append` does the real flattening and
    truncation, on every field, for every caller.
    """
    about = (quote or "").strip() or (body or "").strip()
    return f"settled: {about[:160]}" if about else "settled"


def resolve(
    db: Database, comment_id: str, *, by: str, agent: str | None = None
) -> CommentRow:
    """Resolve the thread `comment_id` belongs to (its root, if `comment_id` names a
    reply, and every reply alongside it -- one thread is one conversation, resolved
    or not as a whole). `by` is who resolved it and is what gets stored. `agent` is the
    identity the artifact must be owned by, and defaults to `by` -- right for
    the agent API, where the caller and the resolver are the same identity, and
    giving the usual `NotFound` for "no such comment" and "someone else's
    comment" alike.

    `agent=None` is the GALLERY, acting for Jeremy, exactly as `add()`'s
    `agent=None` already means for writing: only the comment's existence is
    checked. This docstring used to predict that such a path "will need its own
    route in -- not this one, unchanged". It needed a parameter, not a route,
    and it needed it the moment the gallery grew a Mark settled button -- every
    press 404'd, because no artifact is owned by "jeremy". Found by rendering
    the page, not by a test, which is its own small lesson.

    Idempotent: if the thread is already resolved, the row named by `comment_id` is
    returned exactly as it stands, and `resolved_at` does not move -- a second
    resolve is a no-op, not a fresh timestamp."""
    with db.connect() as conn:
        if agent is None:
            row = conn.execute(
                f"select {_COLUMNS} from comment where comment.id = %s",  # noqa: S608 -- fixed constant, not caller input
                (comment_id,),
            ).fetchone()
        else:
            row = conn.execute(
                f"select {_COLUMNS} from comment"  # noqa: S608 -- fixed constant, not caller input
                " join artifact on artifact.id = comment.artifact_id"
                " where comment.id = %s and artifact.agent = %s",
                (comment_id, agent),
            ).fetchone()
        if row is None:
            raise store.NotFound()

        root_id = row[3] if row[3] is not None else row[0]
        root_resolved_at = conn.execute(
            "select resolved_at from comment where id = %s", (root_id,)
        ).fetchone()[0]

        if root_resolved_at is None:
            # The owner of the page, and what the thread was about -- read
            # before the update so the event can say which conversation was
            # settled, in words, rather than only by id.
            owner = conn.execute(
                "select artifact.agent, artifact.title, artifact.session,"
                " root.body, root.quote"
                " from comment root join artifact on artifact.id = root.artifact_id"
                " where root.id = %s",
                (root_id,),
            ).fetchone()
            owning_agent, title, page_session, root_body, root_quote = owner
            with conn.transaction():
                conn.execute(
                    "update comment set resolved_at = now(), resolved_by = %s"
                    " where id = %s or thread_root = %s",
                    (by, root_id, root_id),
                )
                # Settling a thread is one of the interactions Jeremy widened
                # this to on 19 Sep: EVERY interaction he has with a page
                # nudges the agent that owns it. A seat that is mid-reply
                # needs to know the conversation is over as much as it needed
                # to know it had started -- and it must not be woken by its
                # own tidying up, so an owner settling its own thread is
                # silent, exactly as its own comment is.
                event_seq = None
                if by != owning_agent:
                    event_seq = events.append(
                        conn,
                        artifact_id=row[1],
                        agent=owning_agent,
                        actor=by,
                        kind="thread_resolved",
                        summary=_settled_summary(root_quote, root_body),
                        ref_id=root_id,
                        session=page_session,
                    )
            row = conn.execute(
                f"select {_COLUMNS} from comment where comment.id = %s",  # noqa: S608 -- fixed constant, not caller input
                (comment_id,),
            ).fetchone()
            if event_seq is not None:
                nudge.emit(
                    kind="thread_resolved",
                    artifact_id=row[1],
                    title=title,
                    agent=owning_agent,
                    summary=_settled_summary(root_quote, root_body),
                    event_id=str(event_seq),
                )

    return _row_to_comment(row)
