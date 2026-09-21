"""The conversation on a page, in the gallery — Jeremy's half of it.

homelab-ij8.18. He wanted to talk to the agent on the page rather than in a
terminal. The storage and the agent-facing API have existed since 19 Sep; what
did not exist was any way for HIM to write one, which made "agents read the
comments and reply" a loop with no beginning.

SHAPE TAKEN FROM THE REFERENCE, not invented: the saved claude.ai HTML he gave
us puts a ButtonGroup labelled "Comments" in the page bar, holding a Comment
toggle and a "More comment options" menu, with the conversation in a panel
rather than in the document flow. So: a control in the bar, and a `:target`
drawer — the same scriptless mechanism the rename and link dialogs use.

NOT BUILT HERE, and it is the half he actually described: selecting text and
anchoring a comment to it. That needs the content shim to postMessage an anchor
across the origin boundary, and the bead says to ask him for the claude.ai
comment screenshots before building that UI.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import comments, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db=db), base_url="https://testserver")


@pytest.fixture()
def page(db):
    return store.publish(db, "seat", "Board", PAGE)


def _same_origin(extra=None):
    headers = {"Sec-Fetch-Site": "same-origin"}
    headers.update(extra or {})
    return headers


# --- he can start one ---------------------------------------------------------


def test_he_can_comment_on_a_page_and_it_is_his(client, db, page):
    got = client.post(
        f"/c/{page.id}",
        data={"body": "Can the chart go first?"},
        headers=_same_origin(),
        follow_redirects=False,
    )
    assert got.status_code == 303
    rows = comments.list_for_artifact(db, page.id)
    assert [r.body for r in rows] == ["Can the chart go first?"]
    assert rows[0].author_kind == "human"
    assert rows[0].author == "jeremy"


def test_the_author_is_hardcoded_and_not_taken_from_a_header(client, db, page):
    """Authelia admits exactly one subject to this host, and nothing here proves
    the ingress strips an inbound copy of an identity header. Same reasoning as
    the answer form, and worth a test because the failure is a forged author on
    a record this service exists to keep honest."""
    client.post(
        f"/c/{page.id}",
        data={"body": "x", "author": "someone-else", "author_kind": "agent"},
        headers=_same_origin({"X-Forwarded-User": "mallory"}),
        follow_redirects=False,
    )
    row = comments.list_for_artifact(db, page.id)[0]
    assert row.author == "jeremy"
    assert row.author_kind == "human"


def test_a_write_without_the_same_origin_header_is_refused(client, db, page):
    got = client.post(
        f"/c/{page.id}", data={"body": "x"}, follow_redirects=False
    )
    assert got.status_code in (403, 400)
    assert comments.list_for_artifact(db, page.id) == []


def test_he_can_reply_into_an_existing_thread(client, db, page):
    root = comments.add(
        db, artifact_id=page.id, author_kind="agent", author="seat", body="Done."
    )
    client.post(
        f"/c/{page.id}",
        data={"body": "Thanks", "thread_root": root.id},
        headers=_same_origin(),
        follow_redirects=False,
    )
    rows = comments.list_for_artifact(db, page.id)
    reply = next(r for r in rows if r.body == "Thanks")
    assert reply.thread_root == root.id


def test_marking_it_settled_works_for_him(client, db, page):
    """It could not, until today. `resolve` matched the artifact's owner against
    the resolver, and no artifact is owned by "jeremy", so every press 404'd.
    Found by rendering the page, not by a test."""
    root = comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="q"
    )
    got = client.post(
        f"/c/{root.id}/resolve", headers=_same_origin(), follow_redirects=False
    )
    assert got.status_code == 303
    settled = comments.list_for_artifact(db, page.id, include_resolved=True)[0]
    assert settled.resolved_at is not None
    assert settled.resolved_by == "jeremy"


# --- what he sees -------------------------------------------------------------


def test_the_bar_carries_a_comments_control_with_a_count(client, db, page):
    """The reference puts the control in the page bar. The count is there
    because a control that does not say whether there is anything behind it
    gets pressed once and then never again."""
    plain = client.get(f"/a/{page.id}").text
    assert 'href="#comments"' in plain
    assert 'class="cn"' not in plain, "a count with nothing behind it is a number"

    comments.add(
        db, artifact_id=page.id, author_kind="agent", author="seat", body="hello"
    )
    with_one = client.get(f"/a/{page.id}").text
    assert '<span class="cn">1</span>' in with_one


def test_a_settled_thread_is_folded_not_hidden(client, db, page):
    """Out of the way must never become gone -- the count is on the summary,
    exactly as the archived wall works."""
    root = comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="old thing"
    )
    comments.resolve(db, root.id, by="jeremy", agent=None)
    body = client.get(f"/a/{page.id}").text
    assert "old thing" in body, "a settled conversation was thrown away"
    assert 'class="settled-cmts"' in body
    assert "1 conversation" in body
    assert '<span class="cn">' not in body, "a settled thread is not waiting on anyone"


def test_the_fold_looks_like_something_you_can_open(client, db, page):
    """Without a marker the summary reads as a caption and nobody presses it.
    Rendered and looked at: "Settled 1 conversation" sat there looking inert."""
    root = comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="x"
    )
    comments.resolve(db, root.id, by="jeremy", agent=None)
    body = client.get(f"/a/{page.id}").text
    fold = body[body.index('class="settled-cmts"') :][:400]
    assert 'class="chev"' in fold


def test_agent_text_is_escaped_into_his_signed_in_origin(client, db, page):
    comments.add(
        db, artifact_id=page.id, author_kind="agent", author="seat",
        body="<script>alert(1)</script>",
    )
    body = client.get(f"/a/{page.id}").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_an_empty_page_still_offers_the_box(client, db, page):
    """An empty section with a box is an invitation; an empty section with no
    box is a fault."""
    body = client.get(f"/a/{page.id}").text
    assert f'action="/c/{page.id}"' in body
    assert "Nothing yet" in body


def test_the_panel_needs_no_script(client, db, page):
    """Everything else in this gallery works with scripts off and a
    conversation is a poor place to start making exceptions."""
    comments.add(
        db, artifact_id=page.id, author_kind="agent", author="seat", body="hi"
    )
    body = client.get(f"/a/{page.id}").text
    # Scoped to the drawer element, not "everything after it" -- the page's own
    # <script src="/gallery.js"> lives further down the shell and a slice to the
    # end of the document catches it. The claim is about the panel's own markup.
    start = body.index('<div class="cdrawer"')
    # Bounded by the element the detail page renders next, the rename dialog.
    # My first two attempts at this slice both ran to the end of the document
    # and caught the page's own <script src="/gallery.js">, which is a test
    # failing on markup it was never about.
    panel = body[start : body.index('<div class="dlg" id="rename"', start)]
    assert "<script" not in panel, "the panel grew a script"
    assert "onclick" not in panel and "javascript:" not in panel
    assert 'method="post"' in panel, "its controls must be real forms"
    # And the toggle is a link to a fragment, which is what makes it work with
    # scripts off at all.
    assert 'href="#comments"' in body
