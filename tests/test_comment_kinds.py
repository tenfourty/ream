"""Marks on every page: a comment can be a remark, a 'remove this' or a 'looks
good' (page review step 1, homelab-ij8.108)."""

import pytest

from artifacts import comments, events, store

PAGE = "<!doctype html><title>t</title><p>Rotate the keys every 90 days.</p>"
RANGE = {"kind": "range", "path": "body>p:nth-of-type(1)", "start": 0, "end": 29}


def _page(db):
    return store.publish(db, "seat", "Board", PAGE)


def test_a_plain_comment_is_kind_comment(db):
    page = _page(db)
    row = comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="why 90?"
    )
    assert row.kind == "comment"


@pytest.mark.parametrize("kind", ["remove", "looks_good"])
def test_a_mark_needs_no_note_but_needs_a_quote(db, kind):
    page = _page(db)
    row = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="",
        kind=kind,
        quote="Rotate the keys every 90 days.",
        anchor=RANGE,
    )
    assert row.kind == kind and row.body == ""
    with pytest.raises(comments.InvalidKind):
        comments.add(
            db,
            artifact_id=page.id,
            author_kind="human",
            author="jeremy",
            body="",
            kind=kind,
        )  # page anchor, no quote


def test_an_unknown_kind_is_refused_without_echoing_it(db):
    page = _page(db)
    with pytest.raises(comments.InvalidKind) as err:
        comments.add(
            db,
            artifact_id=page.id,
            author_kind="human",
            author="jeremy",
            body="x",
            kind="<script>",
        )
    assert "<script>" not in str(err.value)


def test_a_reply_is_always_a_comment(db):
    page = _page(db)
    root = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="",
        kind="remove",
        quote="Rotate the keys every 90 days.",
        anchor=RANGE,
    )
    reply = comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        body="done",
        thread_root=root.id,
        kind="looks_good",
    )
    assert reply.kind == "comment"


def test_a_mark_with_no_note_still_says_something_to_the_agent(db):
    page = _page(db)
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="",
        kind="remove",
        quote="Rotate the keys every 90 days.",
        anchor=RANGE,
    )
    [event] = events.list_for_agent(db, "seat")
    assert event.summary.startswith('Remove this: "Rotate the keys')
