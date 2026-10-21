"""His inbox: everything that is waiting on Jeremy, in one list, in one order.

Three kinds of item today, all built by `items`, which returns typed `Item`s:

- `question`: an open lettered question on any page, unless it is held behind an
  earlier unanswered one (a held question is not for him yet);
- `review`: an open review request awaiting his verdict;
- `reply`: an office's reply under one of his comments or notes that he has not
  seen (the same rule the wall's unread mark uses, `comments.UNSEEN_REPLY`).

A further source (the Slack bridge, later) is one more function that returns
`Item`s, appended in `items`; `order` and the views take it from there.

THE ORDER (Jeremy's, 21 Oct 2026): overdue first, then urgency (blocking, by,
when_you_can), then a question before a review or reply of the same band, then
soonest needed-by, then topic, then age (oldest first). A review and a reply
carry no urgency, so they sit in the `when_you_can` band, behind the questions of
that band: a question he was asked with a deadline or a topic is a stronger
claim on his afternoon than a verdict that has no date. Nothing in the data
offers something better, and a review is already flagged on the wall by its page.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from artifacts import comments, decisions
from artifacts.db import Database

URGENCY_RANK = {"blocking": 0, "by": 1, "when_you_can": 2}

# A reply is shown as its first part, enough to know what it says.
REPLY_EXCERPT = 600
_LIMIT = 500  # per source


def moment(value: datetime | str | None) -> datetime | None:
    """A stored timestamp as an aware datetime, or None if there is none or it
    will not parse. Drivers hand back datetimes or ISO strings."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def is_overdue(row: decisions.DecisionRow, now: datetime) -> bool:
    needed = moment(row.needed_by)
    return needed is not None and needed < now


def key(row: decisions.DecisionRow, now: datetime) -> tuple:
    """Order for a question: overdue first, then urgency, then soonest needed-by
    (none last), then topic title (none last), then when it was asked."""
    needed = moment(row.needed_by)
    title = (row.topic or {}).get("title")
    return (
        not is_overdue(row, now),
        URGENCY_RANK.get(row.urgency, 2),
        needed is None,
        needed or now,
        title is None,
        (title or "").lower(),
        moment(row.created_at) or now,
        row.id,
    )


@dataclass(frozen=True)
class Item:
    kind: str  # 'question' | 'review' | 'reply'
    id: str  # the decision's id, or the reply comment's
    artifact_id: str
    page_title: str
    # The asking office: the page's agent, or for a reply the session label's
    # office (what the replying session said it is).
    office: str
    created_at: datetime
    decision: decisions.DecisionRow | None = None
    body: str = ""


def item_key(item: Item, now: datetime) -> tuple:
    """The one order for every kind. A question keeps its own key; a review or a
    reply is the not-overdue, `when_you_can`, no-needed-by, no-topic item it is,
    after the questions of that band, oldest first."""
    if item.kind == "question" and item.decision is not None:
        k = key(item.decision, now)
        return (*k[:2], 0, *k[2:])
    return (
        True,
        URGENCY_RANK["when_you_can"],
        1,
        True,
        now,
        True,
        "",
        item.created_at,
        item.id,
    )


def order(rows: list[Item], now: datetime) -> list[Item]:
    return sorted(rows, key=lambda i: item_key(i, now))


# Shared by the list and the count, so they cannot disagree about which replies
# are waiting: a reply he has not seen, on a page that is neither archived nor
# deleted.
_REPLY_FROM_WHERE = (
    " from comment c join artifact a on a.id = c.artifact_id"
    " where a.deleted_at is null and a.archived_at is null"
    f"   and {comments.UNSEEN_REPLY}"
)


def _decision_items(db: Database, limit: int) -> tuple[list[Item], bool]:
    out = []
    got = decisions.across_seats(db, limit=limit)
    for r in got:
        d = r.decision
        if d.kind == "question" and d.held_by:
            continue  # not for him yet
        out.append(
            Item(
                kind="review" if d.kind == "review" else "question",
                id=d.id,
                artifact_id=d.artifact_id,
                page_title=r.artifact_title,
                office=r.agent,
                created_at=moment(d.created_at) or datetime.now(UTC),
                decision=d,
            )
        )
    return out, len(got) >= limit


def _reply_items(db: Database, limit: int) -> tuple[list[Item], bool]:
    with db.connect() as conn:
        found = conn.execute(
            "select c.id, c.artifact_id, a.title, a.agent,"
            "       coalesce(nullif(c.session, ''), c.author), c.body, c.created_at"
            f"{_REPLY_FROM_WHERE}"
            " order by c.created_at asc, c.id asc limit %s",
            (limit,),
        ).fetchall()
    return [
        Item(
            kind="reply",
            id=cid,
            artifact_id=aid,
            page_title=title,
            office=comments.office_of(label) or label or agent,
            created_at=moment(created) or datetime.now(UTC),
            body=body,
        )
        for cid, aid, title, agent, label, body, created in found
    ], len(found) >= limit


def gather(
    db: Database, now: datetime | None = None, limit: int | None = None
) -> tuple[list[Item], bool]:
    """Everything waiting on him in inbox order, and whether a source hit its
    `limit` (the oldest `limit` of each source are read; a newer one is not
    shown, and the caller must say so)."""
    now = now or datetime.now(UTC)
    limit = limit or _LIMIT
    decided, capped_d = _decision_items(db, limit)
    replied, capped_r = _reply_items(db, limit)
    return order(decided + replied, now), capped_d or capped_r


def items(
    db: Database, now: datetime | None = None, limit: int | None = None
) -> list[Item]:
    """Everything waiting on him, in inbox order."""
    return gather(db, now, limit)[0]


def count(db: Database, limit: int | None = None) -> tuple[int, bool]:
    """How many items `items` would hold, and whether a source is over `limit`,
    from COUNT queries and ids only: no question text and no reply bodies."""
    limit = limit or _LIMIT
    reviews, questions, open_rows = decisions.open_counts(db)
    with db.connect() as conn:
        replies = conn.execute(f"select count(*){_REPLY_FROM_WHERE}").fetchone()[0]
    capped = open_rows >= limit or replies >= limit
    return reviews + questions + replies, capped


def label(total: int, capped: bool, limit: int | None = None) -> str:
    """The number as he reads it: "500+" when the list is cut short."""
    return f"{limit or _LIMIT}+" if capped else str(total)
