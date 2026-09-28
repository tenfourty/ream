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


def test_a_range_anchor_may_carry_context_either_side():
    got = comments.validate_anchor({**RANGE, "prefix": "the keys ", "suffix": " and"})
    assert got == {**RANGE, "prefix": "the keys ", "suffix": " and"}
    assert comments.validate_anchor(RANGE) == RANGE, "context stays optional"


@pytest.mark.parametrize(
    "bad",
    [
        {"prefix": 5},
        {"suffix": None},
        {"prefix": "x" * 65},
        {"extra": "no"},
    ],
)
def test_context_is_validated_like_the_rest_of_the_anchor(bad):
    with pytest.raises(comments.InvalidAnchor):
        comments.validate_anchor({**RANGE, **bad})
