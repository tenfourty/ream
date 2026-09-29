"""Hold or live: whether Jeremy's marks on a page under review reach the agent
as he makes them (`live`) or wait for his verdict (`held`).

The mode is a server-side preference (`human_pref`), never a cookie, so every
device he uses agrees. Each review stores the mode it was opened under
(`decision.review_mode`); a mark is held only while an open review on its page
is held. Every function here takes the caller's connection, so a release runs
inside the caller's own transaction.
"""

from artifacts import events

MODES = ("held", "live")
_KEY = "review_mode"


def get_mode(conn) -> str:
    """His remembered mode; `held` before he has ever chosen."""
    row = conn.execute(
        "select value from human_pref where key = %s", (_KEY,)
    ).fetchone()
    return row[0] if row is not None and row[0] in MODES else "held"


def set_mode(conn, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(mode)
    conn.execute(
        "insert into human_pref (key, value) values (%s, %s)"
        " on conflict (key) do update set value = excluded.value, updated_at = now()",
        (_KEY, mode),
    )


def page_mode(conn, artifact_id: str, *, lock: bool = False) -> str | None:
    """`held` if ANY open review on the page is held, `live` if open reviews
    exist and none is held, None if the page has no open review.

    `lock` takes a share lock on the open review rows it reads, so a verdict's
    update of them waits for the caller's transaction to finish."""
    modes = [
        r[0]
        for r in conn.execute(
            "select coalesce(review_mode, 'live') from decision"  # noqa: S608 -- fixed strings
            " where artifact_id = %s and kind = 'review'"
            " and answer is null and closed_at is null"
            + (" for share" if lock else ""),
            (artifact_id,),
        ).fetchall()
    ]
    if not modes:
        return None
    return "held" if "held" in modes else "live"


def held_count(conn_or_db, artifact_id: str) -> int:
    """Open held root marks on the page, for the review card. Takes a connection
    or a Database."""
    sql = (
        "select count(*) from comment where artifact_id = %s and held"
        " and thread_root is null and resolved_at is null"
    )
    if hasattr(conn_or_db, "connect"):
        with conn_or_db.connect() as conn:
            return conn.execute(sql, (artifact_id,)).fetchone()[0]
    return conn_or_db.execute(sql, (artifact_id,)).fetchone()[0]


def release(conn, artifact_id: str, *, emit: bool) -> list[str]:
    """Make every held comment of the page visible to the agent; returns the ids.

    With `emit`, one `comment_added` event per released ROOT comment, built as
    `comments.add` builds it. Never sends a nudge. Runs in the caller's
    transaction."""
    from artifacts import comments

    released = conn.execute(
        "update comment set held = false where artifact_id = %s and held"
        " returning id, thread_root, kind, quote, body, author, created_at, resolved_at",
        (artifact_id,),
    ).fetchall()
    if emit and released:
        agent, page_session = conn.execute(
            "select agent, session from artifact where id = %s", (artifact_id,)
        ).fetchone()
        released_ids = {r[0] for r in released}
        for cid, root, kind, quote, body, author, _, resolved_at in sorted(
            released, key=lambda r: (r[6], r[0])
        ):
            # A root gets an event unless it is resolved (the agent's list
            # hides resolved threads). A reply gets one only when its root was
            # not released with it -- the root's event covers a whole thread,
            # but a waiter never saw a thread that was already visible.
            if root is None and resolved_at is not None:
                continue
            if root is not None and root in released_ids:
                continue
            said = comments._event_summary(kind, quote, body)
            events.append(
                conn,
                artifact_id=artifact_id,
                agent=agent,
                actor=author,
                kind="comment_added",
                summary=said,
                ref_id=cid,
                note=said,
                session=page_session,
            )
    return [r[0] for r in released]


def approved_letter(conn, artifact_id: str, letter: str) -> str:
    """`a` (Approved) becomes `b` (Approved with notes) while an open held ROOT
    mark of his exists on the page; every other letter passes through. Call it
    inside the verdict's transaction, after locking the decision row."""
    if letter != "a":
        return letter
    held = conn.execute(
        "select 1 from comment where artifact_id = %s and held"
        " and author_kind = 'human' and thread_root is null and resolved_at is null"
        " limit 1",
        (artifact_id,),
    ).fetchone()
    return "b" if held is not None else letter


def set_review_mode(db, decision_id: str, mode: str) -> tuple[str, int]:
    """Jeremy's switch on an open review: remember `mode`, and on `live` release
    what is held, with events. Returns (artifact_id, released_count). Takes the
    decision row `for update`, the same lock a verdict takes, so a flip and a
    verdict serialise."""
    from artifacts import decisions, store

    if mode not in MODES:
        raise ValueError(mode)
    with db.connect() as conn, conn.transaction():
        found = conn.execute(
            f"select {decisions._COLUMNS} from decision"  # noqa: S608 -- fixed constant
            " where decision.id = %s and decision.kind = 'review' for update",
            (decision_id,),
        ).fetchone()
        if found is None:
            raise store.NotFound()
        if found[6] is not None or found[11] is not None:
            raise decisions.AlreadyDecided(decisions._row_to_decision(found))
        artifact_id = found[1]
        conn.execute(
            "update decision set review_mode = %s where id = %s", (mode, decision_id)
        )
        set_mode(conn, mode)
        # Another open review on the page may still hold his marks.
        released = (
            release(conn, artifact_id, emit=True)
            if mode == "live" and page_mode(conn, artifact_id) != "held"
            else []
        )
    return artifact_id, len(released)
