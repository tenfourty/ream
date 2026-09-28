"""Marks on every page: a comment can be a remark, a 'remove this' or a 'looks
good' (page review step 1, homelab-ij8.108)."""

import pytest
from fastapi.testclient import TestClient

from artifacts import comments, events, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><title>t</title><p>Rotate the keys every 90 days.</p>"
RANGE = {"kind": "range", "path": "body>p:nth-of-type(1)", "start": 0, "end": 29}
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


def _page(db):
    return store.publish(db, "seat", "Board", PAGE)


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


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


def test_the_gallery_form_takes_a_mark_with_no_note(client, db):
    page = _page(db)
    r = client.post(
        f"/c/{page.id}",
        data={
            "kind": "remove",
            "body": "",
            "quote": "Rotate the keys every 90 days.",
            "anchor": '{"kind":"range","path":"body>p:nth-of-type(1)","start":0,"end":29}',
        },
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 303
    token, _ = keys.mint(db, agent="seat")
    rows = client.get(
        f"/api/v1/artifacts/{page.id}/comments",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert [row["kind"] for row in rows] == ["remove"]


def test_an_empty_plain_comment_is_still_refused(client, db):
    page = _page(db)
    r = client.post(
        f"/c/{page.id}",
        data={"body": "  "},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 422


def test_an_agent_can_mark_its_own_page_and_a_bad_kind_names_the_field(client, db):
    token, _ = keys.mint(db, agent="seat")
    page = _page(db)
    h = {"Authorization": f"Bearer {token}"}
    ok = client.post(
        f"/api/v1/artifacts/{page.id}/comments",
        headers=h,
        json={
            "kind": "looks_good",
            "body": "",
            "quote": "Rotate the keys every 90 days.",
            "anchor": RANGE,
        },
    )
    assert ok.status_code == 201 and ok.json()["kind"] == "looks_good"
    bad = client.post(
        f"/api/v1/artifacts/{page.id}/comments",
        headers=h,
        json={"kind": "nope", "body": "x"},
    )
    assert bad.status_code == 422 and bad.json()["detail"]["field"] == "kind"


# ---------------------------------------------------------------------------
# Task 5: the marks the page shim paints. The page is agent HTML on its own
# origin, so what reaches it is the quote and the anchor -- never the note.
# ---------------------------------------------------------------------------


def test_marks_for_page_carries_no_note_text(db):
    page = _page(db)
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="secret thought",
        kind="comment",
        quote="Rotate the keys every 90 days.",
        anchor=RANGE,
    )
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="page-level",
        kind="comment",
    )
    marks = comments.marks_for_page(db, page.id)
    assert [m["kind"] for m in marks] == ["comment"], "range-anchored roots only"
    assert "secret thought" not in str(marks)
    assert set(marks[0]) == {"id", "kind", "quote", "anchor"}


def test_marks_for_page_are_open_roots_oldest_first(db):
    page = _page(db)

    def mark(kind, body=""):
        return comments.add(
            db,
            artifact_id=page.id,
            author_kind="human",
            author="jeremy",
            body=body,
            kind=kind,
            quote="Rotate the keys every 90 days.",
            anchor=RANGE,
        )

    first = mark("remove")
    settled = mark("looks_good")
    last = mark("comment", body="why?")
    # A reply inherits its root's anchor and quote; it is not a second mark.
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        body="reply text",
        thread_root=first.id,
    )
    comments.resolve(db, settled.id, by="jeremy", agent="seat")
    marks = comments.marks_for_page(db, page.id)
    assert [m["id"] for m in marks] == [first.id, last.id]
    assert [m["kind"] for m in marks] == ["remove", "comment"]
    assert marks[0]["anchor"]["kind"] == "range"
    assert "reply text" not in str(marks) and "why?" not in str(marks)


def test_the_page_view_embeds_the_marks_and_never_the_note(client, db):
    page = _page(db)
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="private note",
        kind="remove",
        # A quote that tries to end the script it is embedded in.
        quote="Rotate the keys every 90 days.</script><b>",
        anchor=RANGE,
    )
    html = client.get(f"/a/{page.id}").text
    start = html.index('<script type="application/json" id="artifact-marks">')
    block = html[start : html.index("</script>", start)]
    assert '"kind": "remove"' in block
    assert "</script><b>" not in block and "\\u003c/script\\u003e" in block
    assert "private note" not in block
    assert 'id="marks-unplaced"' in html, "the line the shim's report fills"
