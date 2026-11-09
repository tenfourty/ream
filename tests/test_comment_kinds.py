"""Marks on every page: a comment can be a remark, a 'remove this' or a 'looks
good' (page review step 1, homelab-ij8.108)."""

import pytest
from fastapi.testclient import TestClient

from ream import comments, decide, events, keys, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><title>t</title><p>Rotate the keys every 90 days.</p>"
RANGE = {"kind": "range", "path": "body>p:nth-of-type(1)", "start": 0, "end": 29}
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


def _page(db):
    return store.publish(db, "seat", "Board", PAGE)


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
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


def test_a_surrogate_split_prefix_or_suffix_is_refused_not_stored():
    """JS `slice` cuts UTF-16 code units, not characters: an astral emoji (eg.
    U+1F7E2) straddling the 32-char boundary the shim slices at leaves
    prefix/suffix holding a lone surrogate -- a valid Python `str`, but not
    valid UTF-8, and Postgres's `json` column raises on it once it reaches
    `Jsonb` (final review, Item 1: uncaught `DataError` -> 500, mark lost).
    `validate_anchor` must refuse it outright, the same `InvalidAnchor` as any
    other anchor it cannot use, so a bad client gets a named 422, never a 500.
    """
    with pytest.raises(comments.InvalidAnchor):
        comments.validate_anchor({**RANGE, "prefix": "\udfe2 all"})
    with pytest.raises(comments.InvalidAnchor):
        comments.validate_anchor({**RANGE, "suffix": "all \ud800"})


def test_a_surrogate_split_path_is_refused_not_stored():
    """`path` gets only a `str`-and-length check today, not the UTF-8 check
    `prefix`/`suffix` already have -- the same lone-surrogate DataError (final
    review, Item 1) reaches Postgres through `path` exactly as it does through
    `prefix`/`suffix`, so `validate_anchor` must refuse it the same way (follow-up
    to the final review, homelab-ij8.111)."""
    with pytest.raises(comments.InvalidAnchor):
        comments.validate_anchor({**RANGE, "path": "p\udfe2"})


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


def test_the_gallery_form_refuses_a_surrogate_split_path_not_500(client, db):
    """Follow-up to the final review, homelab-ij8.111: `path` reaches this route
    the same way `prefix`/`suffix` already do -- a JSON-escaped string in the
    form body that the server's own `json.loads` turns back into a real lone
    surrogate. It must come back 422, never a 500."""
    page = _page(db)
    r = client.post(
        f"/c/{page.id}",
        data={
            "body": "hello",
            "quote": "Rotate the keys every 90 days.",
            "anchor": '{"kind":"range","path":"p\\udfe2","start":0,"end":2}',
        },
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 422


def test_the_gallery_form_refuses_a_surrogate_split_prefix_not_500(client, db):
    """The same route, for `prefix` -- already guarded server-side, but
    untested on the gallery form until now (homelab-ij8.111)."""
    page = _page(db)
    r = client.post(
        f"/c/{page.id}",
        data={
            "body": "hello",
            "quote": "Rotate the keys every 90 days.",
            "anchor": (
                '{"kind":"range","path":"body>p:nth-of-type(1)","start":0,'
                '"end":2,"prefix":"\\udfe2 all"}'
            ),
        },
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 422


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
    assert set(marks[0]) == {"id", "kind", "quote", "anchor", "held"}


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


def test_the_agent_sees_what_kind_of_mark_it_was():
    thread = decide._threads(
        [
            {
                "id": "c1",
                "artifact_id": "p1",
                "quote": "Rotate the keys",
                "kind": "remove",
                "author_kind": "human",
                "author": "jeremy",
                "body": "",
                "created_at": "2026-09-28T10:00:00Z",
                "thread_root": None,
            }
        ]
    )[0]
    assert '  REMOVE THIS: "Rotate the keys"' in decide._fmt_thread(thread)


def _mark_row(**over):
    row = {
        "id": "c1",
        "artifact_id": "p1",
        "quote": "Rotate the keys",
        "kind": "looks_good",
        "author_kind": "human",
        "author": "jeremy",
        "body": "",
        "created_at": "2026-09-28T10:00:00Z",
        "thread_root": None,
    }
    row.update(over)
    return row


def test_an_empty_mark_says_no_note_not_nothing():
    """Final review, Item 4b: `HE SAID: ` with nothing after the colon reads
    as a rendering bug, not as an acknowledgement -- say plainly there was no
    note."""
    thread = decide._threads([_mark_row()])[0]
    lines = decide._fmt_thread(thread).splitlines()
    assert "  HE SAID: (no note)" in lines
    assert not any(line.strip() == "HE SAID:" for line in lines)


def test_a_bare_looks_good_needs_no_reply():
    """Final review, Item 4c: a lone 'looks good' with no note is an
    acknowledgement, not a question -- it must not be flagged NEEDS YOUR
    REPLY. A lone 'remove this' still is: it asks for an action."""
    looks_good = decide._threads([_mark_row(kind="looks_good")])[0]
    assert looks_good["needs_reply"] is False
    assert "NEEDS YOUR REPLY" not in decide._fmt_thread(looks_good)

    remove = decide._threads([_mark_row(kind="remove")])[0]
    assert remove["needs_reply"] is True

    # A looks_good with an actual note is still a live thread: the ack rule is
    # for a bare "looks good", not for one that says something.
    with_note = decide._threads([_mark_row(kind="looks_good", body="ship it")])[0]
    assert with_note["needs_reply"] is True

    # A reply from the agent still resets needs_reply the ordinary way -- the
    # single-bare-message rule does not swallow that case.
    replied = decide._threads(
        [
            _mark_row(),
            {
                "id": "c2",
                "artifact_id": "p1",
                "thread_root": "c1",
                "author_kind": "agent",
                "author": "seat",
                "body": "thanks",
                "created_at": "2026-09-28T10:01:00Z",
            },
        ]
    )[0]
    assert replied["needs_reply"] is False


@pytest.mark.parametrize(
    ("kind", "label"), [("remove", "Remove this"), ("looks_good", "Looks good")]
)
def test_an_existing_remove_or_looks_good_thread_still_shows_its_label(
    client, db, kind, label
):
    """The reader's screen no longer offers these kinds, but threads made earlier
    (or by the API, which still accepts them) display as they always did."""
    page = _page(db)
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="",
        kind=kind,
        quote="Rotate the keys every 90 days.",
        anchor=RANGE,
    )
    html = client.get(f"/a/{page.id}").text
    assert f'class="ckind ckind-{kind}">{label}</span>' in html


def test_the_page_view_has_the_how_to_box_and_the_select_text_switch(client, db):
    page = _page(db)
    html = client.get(f"/a/{page.id}").text
    assert 'id="chow"' in html and "How to comment" in html
    assert 'id="coffer" checked' in html, "the switch is on by default"
    assert "Offer comments when I select text" in html
    assert "Comment on page" not in html and "pagecbtn" not in html
