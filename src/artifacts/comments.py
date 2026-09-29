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
(size, `kind`, and anchor shape) run first and touch no database; ownership of the artifact,
and -- for a reply -- existence of the thread it names, run next, one query each;
the scanner runs last, on whatever body and quote are actually about to be stored.
An unknown artifact and someone else's look identical either way (both `NotFound`),
matching `store.publish`/`store.patch`.
"""

import logging
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace

import psycopg
from psycopg.types.json import Jsonb

from artifacts import events, ggscan, holds, ids, nudge, store, text
from artifacts.db import Database

# A comment is a remark about a page, not a page in itself -- 8 KB is generous for
# prose and small enough that a thread of hundreds never becomes its own storage
# problem. Well below MAX_BODY_BYTES (3 MiB): nobody pastes a page into a comment.
MAX_COMMENT_BYTES = 8_000

# A quote is a short excerpt Jeremy selected, not the passage it came from -- 2 KB
# is room for several paragraphs, comfortably more than any real selection, while
# still refusing someone pasting the whole page back in as its own "quote".
MAX_QUOTE_BYTES = 2_000

# A comment can be a remark, or one of two marks that carry no note of their own:
# "remove this" and "looks good" (page review step 1, homelab-ij8.108). A reply is
# always 'comment' -- see `add()` -- so a thread's own back-and-forth is never
# mistaken for a fresh mark on the page.
KINDS = ("comment", "remove", "looks_good")

# Every read of a comment row, in the fixed column order `_row_to_comment` expects.
# Qualified with `comment.` throughout -- `list_for_agent` joins `artifact`, which
# also has an `id` and a `created_at`, and an unqualified name there is ambiguous to
# Postgres. Qualifying it here too, even where nothing else is joined, means one
# constant serves every query in this file rather than two near-identical ones.
_COLUMNS = (
    "comment.id, comment.artifact_id, comment.version_n, comment.thread_root,"
    " comment.anchor, comment.quote, comment.author_kind, comment.author,"
    " comment.body, comment.created_at, comment.resolved_at, comment.resolved_by,"
    " comment.kind, comment.held, comment.review_round"
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

# A comment GitGuardian flagged after it was saved is withdrawn: hidden from every
# read, and so are the replies under a withdrawn ROOT (Jeremy may have answered it
# a second before it went). Same root-subquery shape as `_ROOT_RESOLVED_AT`.
_VISIBLE = (
    "comment.withdrawn_at is null and (select root.withdrawn_at from comment as root"
    " where root.id = coalesce(comment.thread_root, comment.id)) is null"
)

_log = logging.getLogger("artifacts.comments")


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
    # A remark, "remove this" or "looks good" -- see `KINDS`. Defaulted, like
    # `quote_state` below it, so a caller that only ever wrote plain comments
    # before this existed is unaffected.
    kind: str = "comment"
    # "" when the comment quotes nothing, else "current" or "changed" -- see
    # `quote_state`. Not stored: it is a fact about the page as it is NOW, and
    # storing it would make it a fact about the page as it was when somebody
    # last looked.
    quote_state: str = ""
    # Written by Jeremy while his page's review was held: the agent cannot see it yet.
    held: bool = False
    # The review round open when he made it (page review step 4); None outside
    # an open round, and for an agent's write.
    review_round: int | None = None


class InvalidAnchor(Exception):
    def __init__(self) -> None:
        super().__init__("anchor")
        self.field = "anchor"


class InvalidKind(Exception):
    def __init__(self) -> None:
        super().__init__("kind")
        self.field = "kind"


class NoSuchThread(Exception): ...


def _is_number(value: object) -> bool:
    # bool is an int subclass in Python -- True/False must never pass a numeric
    # check meant for a fractional page-position, or {"kind": "point", "x": True,
    # "y": False} would validate as (1.0, 0.0).
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# How much text either side of a quote a range anchor may carry. Short: it exists
# to tell two identical quotes on the same page apart, not to store a paragraph.
ANCHOR_CONTEXT_MAX = 64


def _reject_lone_surrogate(value: str) -> None:
    """Raise `InvalidAnchor` if `value` cannot round-trip through UTF-8.

    A `str` can hold a lone surrogate (half of a UTF-16 pair) -- the page
    shim's own `slice` can produce exactly one, cutting an astral character in
    half at a boundary it slices at -- and Python is happy to carry it this
    far. Postgres's `json` column is not: it raises on the way in, uncaught,
    and turns one mark into a 500 (final review, Item 1). UTF-8 can never
    encode a lone surrogate, so trying is the cheapest possible test for "will
    this actually store", and never echoes what was rejected. Shared by every
    `range`-anchor field that is caller-supplied text -- `path`, `prefix` and
    `suffix` alike -- so there is one place that knows this check, not one
    copy per field (follow-up to the final review, homelab-ij8.111).
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidAnchor() from None


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
        keys = set(anchor.keys())
        if not (
            {"kind", "path", "start", "end"}
            <= keys
            <= {"kind", "path", "start", "end", "prefix", "suffix"}
        ):
            raise InvalidAnchor()
        path, start, end = anchor["path"], anchor["start"], anchor["end"]
        if not isinstance(path, str) or len(path) > 512:
            raise InvalidAnchor()
        _reject_lone_surrogate(path)
        if not (_is_int(start) and _is_int(end)):
            raise InvalidAnchor()
        if not (0 <= start < end):
            raise InvalidAnchor()
        out = {"kind": "range", "path": path, "start": start, "end": end}
        # Text either side of the quote, so a quote that appears twice on the
        # page is placed on the right one (the text-quote-selector idea; see
        # the plannotator study, section A).
        for side in ("prefix", "suffix"):
            if side in anchor:
                value = anchor[side]
                if not isinstance(value, str) or len(value) > ANCHOR_CONTEXT_MAX:
                    raise InvalidAnchor()
                _reject_lone_surrogate(value)
                out[side] = value
        return out

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
        kind=row[12],
        held=row[13],
        review_round=row[14],
    )


def _event_summary(kind: str, quote: str, body: str) -> str:
    """What a `comment_added` event says: the body, or for a mark the kind
    label and the quote (the note after it)."""
    label = {"remove": "Remove this", "looks_good": "Looks good"}.get(kind)
    if not label:
        return body
    short = " ".join((quote or "").split())[:120]
    return f'{label}: "{short}"' + (f" — {body}" if body.strip() else "")


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
    kind: str = "comment",
    second_opinion: bool | None = None,
    conn: psycopg.Connection | None = None,
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

    `kind` is one of `KINDS` -- `"comment"` (the default, a free-form remark),
    `"remove"` or `"looks_good"` (a mark that says something by being pinned to an
    exact quote, not by its own words). `remove` and `looks_good` both require a
    `range` anchor and a non-empty `quote`; without one there is nothing they are
    marking, and `InvalidKind` is raised instead of storing a mark that points at
    the whole page. A reply (`thread_root` given) is always stored as `"comment"`,
    whatever `kind` was passed -- a thread is one conversation, and a reply cannot
    put a second, independent mark on the spot its root already marked.

    `second_opinion` is whether GitGuardian is asked BEFORE saving. It defaults
    to False for everyone: Jeremy's comments never wait on the relay, and an
    agent's are checked by `second_scan` after they are saved.

    `conn`, when given, is the caller's open transaction (an
    `Idempotency-Key`'d request): the comment is written inside it and commits
    with whatever the caller stores next, or not at all. Its own transaction
    becomes a savepoint. The owner-event nudge would then fire before the
    caller commits, so that combination is refused: only the agent API passes
    `conn`, and an agent writing on its own page raises no event.
    """
    if second_opinion is None:
        second_opinion = False
    is_reply = thread_root is not None

    # Cheapest first, no database touched yet. A reply's own `anchor`/`quote`
    # arguments are about to be thrown away entirely in favour of the root's (see
    # below), so there is nothing to gain by validating or size-guarding values
    # that will never be looked at again -- only `body`, which is always the
    # caller's own and always stored, is guarded unconditionally.
    store._check_size(
        body, MAX_COMMENT_BYTES, "body", agent=author, artifact_id=artifact_id
    )
    if kind not in KINDS:
        raise InvalidKind()
    if is_reply:
        # A thread is one conversation; letting a reply carry its own mark
        # would put a second, independent "remove this" or "looks good" on
        # the same spot the root already marked. A reply is always talk.
        kind = "comment"
        effective_anchor = None  # resolved from the root below
        effective_quote = None
    else:
        store._check_size(
            quote, MAX_QUOTE_BYTES, "quote", agent=author, artifact_id=artifact_id
        )
        effective_anchor = validate_anchor(anchor)
        effective_quote = quote
        # A mark says something by ITSELF -- "remove this", "looks good" -- only
        # when it is pinned to an exact quote: a whole-page "remove this" with no
        # quote does not say what to remove. A plain comment carries no such
        # requirement; it is free-form, exactly as before.
        if kind != "comment" and (
            effective_anchor.get("kind") != "range" or not quote.strip()
        ):
            raise InvalidKind()

    callers_txn = conn is not None
    # The caller's connection, or one of our own: the same name below either way.
    with nullcontext(conn) if callers_txn else db.connect() as conn:  # noqa: PLR1704
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
                " and comment.thread_root is null and comment.withdrawn_at is null"
                # A held thread does not exist as far as the agent can tell.
                " and (not comment.held or %s)",
                (thread_root, artifact_id, agent is None),
            ).fetchone()
            if root is None:
                raise NoSuchThread()
            effective_anchor, effective_quote = root[0], root[1]

        # The scanner last, on whatever will actually be stored -- for a reply
        # that's the root's own quote, inherited rather than the caller's, and
        # scanning it again here is cheap and keeps one invariant true without
        # exception: every quote that lands in a row passed a scan on THIS call.
        store._check_secrets(
            body,
            "body",
            agent=author,
            artifact_id=artifact_id,
            second_opinion=second_opinion,
        )
        store._check_secrets(
            effective_quote,
            "quote",
            agent=author,
            artifact_id=artifact_id,
            second_opinion=second_opinion,
        )

        owning_agent = owned[2]
        title = owned[1]
        # Which SESSION published the page, so the seat that wrote it can pick
        # its own work out of a shared feed (homelab-ij8.32). A label on the
        # event, never a scope.
        page_session = conn.execute(
            "select session from artifact where id = %s", (artifact_id,)
        ).fetchone()[0]
        # What the event should say. A mark carries no note of its own, so
        # "body" -- empty for a mark, per the test above -- would tell the
        # agent nothing happened at all. Naming the mark and quoting what it
        # is about says the same thing a human reading the page sees.
        said = _event_summary(kind, effective_quote, body)
        with conn.transaction():
            comment_id = ids.new_id()
            # Read INSIDE the insert transaction, and locking the open review
            # rows it reads: a verdict's update of the decision then waits for
            # this insert (so its release sees the mark), and a verdict that
            # committed first is seen here as no open review at all.
            held = (
                author_kind == "human"
                and holds.page_mode(conn, artifact_id, lock=True) == "held"
            )
            # The open round this mark belongs to, read after the review rows
            # were locked (decision, then round): a republish opening the next
            # round waits for this insert, and one that committed first is
            # seen. None between rounds, on a page with no open review, and
            # for an agent's write (a round is his review of the page).
            review_round = None
            if author_kind == "human":
                review_round = conn.execute(
                    "select r.n from review_round r"
                    " join decision d on d.id = r.review_id"
                    " where d.artifact_id = %s and d.kind = 'review'"
                    " and d.answer is null and d.closed_at is null"
                    " and r.closed_at is null"
                    " order by d.created_at desc, r.n desc limit 1",
                    (artifact_id,),
                ).fetchone()
            # The decision rows were locked first, as a verdict does; the root
            # comes second so the two can never wait on each other.
            if is_reply:
                # Locked until the insert commits (the connection is autocommit,
                # so a lock taken by the lookup above would be gone already): a
                # withdrawal of the root now waits for this reply, and one that
                # got in first is seen here.
                still = conn.execute(
                    "select 1 from comment where id = %s"
                    " and withdrawn_at is null for share",
                    (thread_root,),
                ).fetchone()
                if still is None:
                    raise NoSuchThread()
            conn.execute(
                "insert into comment (id, artifact_id, version_n, thread_root, anchor,"
                " quote, author_kind, author, body, kind, held, review_round)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
                    kind,
                    held,
                    review_round[0] if review_round is not None else None,
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
            if author != owning_agent and not held:
                if callers_txn:
                    raise ValueError(
                        "a comment that raises an owner event cannot join a"
                        " caller's transaction: its nudge would precede the commit"
                    )
                event_seq = events.append(
                    conn,
                    artifact_id=artifact_id,
                    agent=owning_agent,
                    actor=author,
                    kind="comment_added",
                    summary=said,
                    # The comment itself, so the agent can read the thread, the
                    # quote and where on the page it was anchored.
                    ref_id=comment_id,
                    # His words in full. The summary is one line in a list; the
                    # comment is the thing he actually said.
                    note=said,
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
            summary=said,
            event_id=str(event_seq),
        )
    return _row_to_comment(created)


def quote_state(quote: str, current_body: str) -> str:
    """Is the text this comment quotes still on the page? "", "current" or
    "changed".

    homelab-ij8.18's re-anchoring rule, and the reason it is a rule: a comment
    is words about OTHER words. Republish the page and the words it was about
    can be gone, while the comment sits there looking like it is about
    whatever is in that spot now. The design says it plainly -- never silently
    move a comment onto text it was not about -- so when the quote no longer
    matches, the comment stays attached to the version it was made on and says
    so.

    Compared on the page's READABLE WORDS, via `text.searchable`, not on its
    HTML: a quote is what a reader selected, so it never carries the tags that
    may sit inside it, and an untouched sentence that gained an <em> must not
    read as changed. Whitespace collapsed and case folded on both sides for
    the same reason -- neither is something a reader chose.

    "" for a comment that quotes nothing: most of them, today. A whole-page
    comment cannot go stale in this sense, and claiming otherwise would put a
    warning on a comment that is perfectly fine.
    """
    words = text.searchable(current_body)
    return _state_against(quote, words, _tight(words))


def _tight(value: str) -> str:
    """Lowercased with every space removed.

    Tag stripping turns `<em>midnight</em>.` into `midnight .`, so a sentence
    that only gained emphasis would read as changed against a quote taken
    before it -- the exact false alarm this must not raise, since the words
    the reader selected have not moved at all. Neither side's spacing is
    something a reader chose, so neither side keeps it.
    """
    return "".join(value.split()).lower()


def _state_against(quote: str, words: str, tight_words: str) -> str:
    """`quote_state`'s comparison, against words already extracted.

    Separate because `text.searchable` walks the whole page: a thread of
    twenty comments would walk it twenty times if each row called
    `quote_state` itself. Same rule, one reading of the page.
    """
    needle = " ".join(quote.split()).lower()
    if not needle:
        return ""
    if needle in words or _tight(quote) in tight_words:
        return "current"
    return "changed"


STILL = "still on page"
TEXT_CHANGED = "text changed"
ADDRESSED = "addressed/removed"

# How much of a quote's surroundings, or of the quote itself, must still be on
# the page (spaces removed) for its absence to read as an edit in place rather
# than a removal. Short enough to survive an edit next to it, long enough that
# a few common letters do not count.
_CONTEXT_CHARS = 32
_CONTEXT_MIN = 8


def carried_state(row: CommentRow, current_body: str) -> str:
    """What became of a mark from an earlier review round, read off the page as
    it is now (page review step 4). Inferred, never a flag:

    - `addressed/removed`: the thread is resolved, or its quote is gone;
    - `text changed`: its quote is not there as it was, but the text around it
      (the anchor's prefix or suffix) or half of the quote itself still is --
      the words were edited in place;
    - `still on page`: otherwise, including a mark that quotes nothing.

    Compared the way `quote_state` compares -- tags stripped, whitespace and
    case ignored -- so a sentence that only gained emphasis is still on page.
    """
    words = text.searchable(current_body)
    return _carried_against(row, words, _tight(words))


def _carried_against(row: CommentRow, words: str, tight_words: str) -> str:
    if row.resolved_at is not None:
        return ADDRESSED
    if _state_against(row.quote, words, tight_words) != "changed":
        return STILL
    anchor = row.anchor or {}
    hints = [
        _tight(anchor.get("prefix") or "")[-_CONTEXT_CHARS:],
        _tight(anchor.get("suffix") or "")[:_CONTEXT_CHARS],
    ]
    quote = _tight(row.quote)
    half = len(quote) // 2
    hints += [quote[:half], quote[half:]]
    if any(len(h) >= _CONTEXT_MIN and h in tight_words for h in hints):
        return TEXT_CHANGED
    return ADDRESSED


def carried_states(rows: list[CommentRow], current_body: str) -> dict[str, str]:
    """`carried_state` for many rows, reading the page once."""
    words = text.searchable(current_body)
    tight_words = _tight(words)
    return {row.id: _carried_against(row, words, tight_words) for row in rows}


def carried_marks(
    rows: list[CommentRow], round_n: int, since: str, current_body: str
) -> list[tuple[CommentRow, str]]:
    """His marks from earlier rounds of a review in round `round_n`, each with
    its `carried_state`, oldest first.

    Earlier means a human root stamped with a lower round, or with none: a
    mark made between a verdict and the republish that opened this round
    belongs to no round, and he made it about the version he sent back. A
    mark with no round, and a resolved one, is carried only if it was made
    since the review was asked (`since`): a mark from before this review
    began is not part of its rounds, open or settled."""
    earlier = [
        c
        for c in rows
        if c.thread_root is None
        and c.author_kind == "human"
        and (c.review_round is None or c.review_round < round_n)
        and (
            (c.review_round is not None and c.resolved_at is None)
            or c.created_at >= since
        )
    ]
    states = carried_states(earlier, current_body)
    return [(c, states[c.id]) for c in earlier]


def _with_quote_state(conn, rows: list[CommentRow]) -> list[CommentRow]:
    """Every quoting row told whether its quote is still on its page.

    ONE query for every page that has a quoting comment, never one per comment
    and never one per page: a body here is tens of kilobytes, most comments
    quote nothing, and this runs on a listing that can span the whole wall.
    Pages with no quoting comment are not read at all.

    `replace`, not assignment: CommentRow is frozen, which is right -- a row
    read out of the database is a record, and code that can edit one in place
    is code that can silently disagree with it.
    """
    quoting = sorted({row.artifact_id for row in rows if row.quote})
    if not quoting:
        return rows
    words_by_page: dict[str, tuple[str, str]] = {}
    for artifact_id, body in conn.execute(
        "select a.id, v.body from artifact a"
        " join version v on v.artifact_id = a.id and v.n = a.current_version"
        " where a.id = any(%s)",
        (quoting,),
    ).fetchall():
        words = text.searchable(body)
        words_by_page[artifact_id] = (words, _tight(words))
    return [
        replace(
            row, quote_state=_state_against(row.quote, *words_by_page[row.artifact_id])
        )
        if row.quote and row.artifact_id in words_by_page
        else row
        for row in rows
    ]


def list_for_artifact(
    db: Database,
    artifact_id: str,
    *,
    include_resolved: bool = False,
    include_held: bool = False,
) -> list[CommentRow]:
    """Roots and replies together, oldest first (`id` breaks a tie on `created_at`,
    which two comments can share). Ownership is the caller's job -- this function
    trusts `artifact_id` outright, exactly as `store`'s own listing helpers do."""
    # Every interpolated piece here (`_COLUMNS`, `_ROOT_RESOLVED_AT`) is a fixed
    # module constant, never caller input -- `artifact_id` travels as a bound
    # parameter below, not string-built.
    sql = (
        f"select {_COLUMNS} from comment where comment.artifact_id = %s"  # noqa: S608
        f" and {_VISIBLE}"
    )
    if not include_held:
        sql += " and not comment.held"
    if not include_resolved:
        sql += f" and {_ROOT_RESOLVED_AT} is null"
    sql += " order by comment.created_at asc, comment.id asc"
    with db.connect() as conn:
        rows = conn.execute(sql, (artifact_id,)).fetchall()
        # The page as it is NOW, read only when something actually quotes it --
        # see `_with_quote_state`, which every listing in this module shares so
        # that an agent is never told "this quote is gone" on one route and
        # nothing at all on another.
        return _with_quote_state(conn, [_row_to_comment(row) for row in rows])


def marks_for_page(db: Database, artifact_id: str) -> list[dict]:
    """The open marks the page shim paints: id, kind, quote and anchor only,
    NEVER the note -- the page is agent HTML on its own origin and must not read
    what Jeremy wrote about it (page review design, Security).

    Open ROOT comments with a `range` anchor, oldest first. A reply carries its
    root's anchor and quote, so it is the same place on the page, not a second
    mark; a settled thread has nothing left to point at; and a page-level or
    point anchor has no text to paint.

    A held mark's quote is still visible to page scripts, because the dotted
    underline needs it; its note and its kind are not (kind is sent as "")."""
    with db.connect() as conn:
        rows = conn.execute(
            "select id, kind, quote, anchor, held from comment"
            " where artifact_id = %s and thread_root is null"
            " and resolved_at is null and withdrawn_at is null"
            " and anchor->>'kind' = 'range'"
            " order by created_at asc, id asc",
            (artifact_id,),
        ).fetchall()
    return [
        {
            "id": r[0],
            "kind": "" if r[4] else r[1],
            "quote": r[2],
            "anchor": r[3],
            "held": r[4],
        }
        for r in rows
    ]


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
        # A deleted page's threads leave the agent's list too (ij8.110): they
        # kept listing as NEEDS YOUR REPLY and waking its waiter.
        f" where artifact.agent = %s and artifact.deleted_at is null"
        f" and not comment.held and {_VISIBLE}"
    )
    if not include_resolved:
        sql += f" and {_ROOT_RESOLVED_AT} is null"
    sql += " order by comment.created_at desc, comment.id desc limit %s"
    with db.connect() as conn:
        rows = conn.execute(sql, (agent, limit)).fetchall()
        # THE SAME FIELD THE PER-PAGE ROUTE GIVES IT. This one returned "" for
        # every row until 21 Sep, so an agent polling its whole feed was told
        # nothing about quotes that had been edited away, while the same
        # comment read through /artifacts/<id>/comments said "changed". One of
        # those two answers was always wrong.
        return _with_quote_state(conn, [_row_to_comment(row) for row in rows])


@dataclass(frozen=True)
class PageComments:
    """One page's conversations, for the view that spans every page."""

    artifact_id: str
    artifact_title: str
    archived: bool
    rows: list[CommentRow]


def across_pages(
    db: Database, *, include_resolved: bool = False, limit: int = 500
) -> list[PageComments]:
    """Every page's conversations, grouped by page, most recently active page
    first (homelab-ij8.18's "all pages" filter).

    TWO ORDERINGS, on purpose, and both are already argued elsewhere in this
    service: the PAGES come newest-active first, because this view answers
    "where is something being said", and the comments WITHIN a page come oldest
    first, because a conversation is read forwards.

    Unscoped, unlike `list_for_agent`. This serves Jeremy's own gallery, which
    is his whole wall behind Authelia -- the scoping argument that applies to a
    key does not apply to him. No route hands this to a key.

    A DELETED page's conversations are not here: deleting is how he tidies the
    wall, and a view that kept talking about a page he has deleted would undo
    that. An ARCHIVED one's are, flagged, because archiving is "not now", not
    "gone" -- and a question waiting in an archived page's thread is exactly
    the kind of thing that gets lost.
    """
    sql = (
        f"select {_COLUMNS}, artifact.title, artifact.archived_at from comment"  # noqa: S608
        " join artifact on artifact.id = comment.artifact_id"
        f" where artifact.deleted_at is null and {_VISIBLE}"
    )
    if not include_resolved:
        sql += f" and {_ROOT_RESOLVED_AT} is null"
    sql += " order by comment.created_at asc, comment.id asc limit %s"

    with db.connect() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
        stated = _with_quote_state(conn, [_row_to_comment(row) for row in rows])
        found = [
            (comment, row[-2], row[-1])
            for comment, row in zip(stated, rows, strict=True)
        ]

    grouped: dict[str, PageComments] = {}
    for row, title, archived_at in found:
        group = grouped.get(row.artifact_id)
        if group is None:
            group = PageComments(
                artifact_id=row.artifact_id,
                artifact_title=title,
                archived=archived_at is not None,
                rows=[],
            )
            grouped[row.artifact_id] = group
        group.rows.append(row)

    # A PAGE WITH SOMETHING STILL OPEN COMES FIRST, whatever the clock says.
    # Sorting on recency alone put a page whose every thread was settled -- an
    # archived one, showing nothing but a closed fold -- above two live
    # conversations, which is the opposite of what this view is for. Seen in a
    # render; the tests were happy.
    #
    # Within each half, most recently talked about first. `created_at` is an
    # ISO string and every one of them was stamped by the same database in the
    # same zone, so a lexical max is a chronological one.
    def _rank(page: PageComments) -> tuple[bool, str]:
        has_open = any(
            row.thread_root is None and row.resolved_at is None for row in page.rows
        )
        return (has_open, max(row.created_at for row in page.rows))

    return sorted(grouped.values(), key=_rank, reverse=True)


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
                f"select {_COLUMNS} from comment where comment.id = %s"  # noqa: S608 -- fixed constant, not caller input
                f" and {_VISIBLE}",
                (comment_id,),
            ).fetchone()
        else:
            row = conn.execute(
                f"select {_COLUMNS} from comment"  # noqa: S608 -- fixed constant, not caller input
                " join artifact on artifact.id = comment.artifact_id"
                " where comment.id = %s and artifact.agent = %s"
                f" and not comment.held and {_VISIBLE}",
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
                " root.body, root.quote, root.held"
                " from comment root join artifact on artifact.id = root.artifact_id"
                " where root.id = %s",
                (root_id,),
            ).fetchone()
            owning_agent, title, page_session, root_body, root_quote, root_held = owner
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
                if by != owning_agent and not root_held:
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


def withdraw(db: Database, comment_id: str, *, kind: str) -> bool:
    """Take a comment GitGuardian flagged out of every read, and blank its text so
    the secret does not sit in the database. `kind` names the shape (e.g.
    `ggshield_slack_bot_token`), never the value. Row, event and log line are one
    transaction. False if the comment is missing or already withdrawn."""
    with db.connect() as conn, conn.transaction():
        row = conn.execute(
            "update comment set withdrawn_at = now(), withdrawn_kind = %s,"
            " body = '', quote = ''"
            " where id = %s and withdrawn_at is null returning artifact_id",
            (kind, comment_id),
        ).fetchone()
        if row is None:
            return False
        artifact_id = row[0]
        # A reply stores its root's quote as its own.
        conn.execute(
            "update comment set quote = '' where thread_root = %s", (comment_id,)
        )
        # The text may also sit in events that quoted it (a resolved thread).
        conn.execute(
            "update event set summary = %s, note = ''"
            " where kind <> 'comment_withdrawn' and ref_id in"
            " (select id from comment where id = %s or thread_root = %s)",
            (f"Withdrawn: it looked like a {kind}.", comment_id, comment_id),
        )
        owner = conn.execute(
            "select agent, session from artifact where id = %s", (artifact_id,)
        ).fetchone()
        agent = owner[0]
        events.append(
            conn,
            artifact_id=artifact_id,
            agent=agent,
            actor="artifacts",
            kind="comment_withdrawn",
            summary=(
                f"Your comment was withdrawn: it looked like a {kind}. "
                "Nothing of it was kept visible."
            ),
            ref_id=comment_id,
            session=owner[1],
        )
        _log.warning(
            "comment withdrawn: agent=%s artifact=%s comment=%s reason=%s",
            agent,
            artifact_id,
            comment_id,
            kind,
        )
    return True


def second_scan(db: Database, comment_id: str) -> str:
    """The background job after an agent's comment is saved: GitGuardian on the
    saved body. A finding withdraws the comment; an unreachable relay leaves it
    up on the built-in scan and says so. Never raises -- a background task's
    exception is lost, so it is logged and swallowed.

    Returns "scanned", "withdrawn", "unavailable" (no answer from the relay)
    or "gone" (nothing to scan, or the scan itself failed)."""
    try:
        with db.connect() as conn:
            row = conn.execute(
                "select body, artifact_id, author, quote from comment"
                " where id = %s and withdrawn_at is null and author_kind = 'agent'",
                (comment_id,),
            ).fetchone()
        if row is None:
            return "gone"
        body, artifact_id, author, quote = row
        with db.connect() as conn:
            conn.execute(
                "update comment set gg_attempted_at = now() where id = %s",
                (comment_id,),
            )
        # The quote was only ever built-in scanned; the relay sees it too.
        found = ggscan.scan(body + "\n" + quote)
        if found is None:
            if ggscan.configured():
                _log.warning(
                    "comment kept on the built-in scan only: agent=%s artifact=%s "
                    "comment=%s reason=ggshield_unavailable",
                    author,
                    artifact_id,
                    comment_id,
                )
            return "unavailable"
        if found:
            withdraw(db, comment_id, kind=found[0].shape)
        with db.connect() as conn:
            conn.execute(
                "update comment set gg_scanned_at = now() where id = %s", (comment_id,)
            )
        return "withdrawn" if found else "scanned"
    except Exception:  # noqa: BLE001 -- a background task's exception is lost
        _log.error("comment second scan failed: comment=%s", comment_id, exc_info=True)
        return "gone"


def sweep_unscanned(db: Database, *, limit: int = 200) -> int:
    """Scan agent comments the background task never got to (a restart killed it),
    oldest first, at most `limit` per pass. Stops after two consecutive comments the
    relay could not answer for (it is down).
    Returns how many were actually checked."""
    with db.connect() as conn:
        ids = [
            r[0]
            for r in conn.execute(
                "select id from comment where author_kind = 'agent'"
                " and withdrawn_at is null and gg_scanned_at is null"
                " and created_at < now() - interval '30 seconds'"
                " order by gg_attempted_at asc nulls first, created_at asc, id asc"
                " limit %s",
                (limit,),
            ).fetchall()
        ]
    checked = 0
    unavailable = 0
    for comment_id in ids:
        status = second_scan(db, comment_id)
        # One refusal may be this comment's alone; two in a row mean the relay
        # is down and the rest would each wait out a timeout.
        unavailable = unavailable + 1 if status == "unavailable" else 0
        if unavailable >= 2:
            break
        if status in ("scanned", "withdrawn"):
            checked += 1
    return checked


SWEEP_EVERY_SECONDS = 600


def start_sweeper(db: Database) -> threading.Thread:
    """One daemon thread: a pass at startup, then one every ten minutes. It never
    blocks startup and never dies of an exception."""

    def loop() -> None:
        while True:
            try:
                sweep_unscanned(db)
            except Exception:  # noqa: BLE001 -- the loop must outlive one bad pass
                _log.error("comment sweep failed", exc_info=True)
            time.sleep(SWEEP_EVERY_SECONDS)

    thread = threading.Thread(target=loop, name="comment-sweep", daemon=True)
    thread.start()
    return thread
