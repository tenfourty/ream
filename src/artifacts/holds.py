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


def release(conn, artifact_id: str, *, emit: bool) -> list[str]:
    """Make every held comment of the page visible to the agent; returns the ids.

    With `emit`, one `comment_added` event per released ROOT comment, built as
    `comments.add` builds it. Never sends a nudge. Runs in the caller's
    transaction."""
    from artifacts import comments

    released = conn.execute(
        "update comment set held = false where artifact_id = %s and held"
        " returning id, thread_root, kind, quote, body, author, created_at",
        (artifact_id,),
    ).fetchall()
    if emit and released:
        agent, page_session = conn.execute(
            "select agent, session from artifact where id = %s", (artifact_id,)
        ).fetchone()
        for cid, root, kind, quote, body, author, _ in sorted(
            released, key=lambda r: (r[6], r[0])
        ):
            if root is not None:
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
