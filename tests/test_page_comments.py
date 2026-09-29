"""The conversation on a page, in the gallery — Jeremy's half of it.

homelab-ij8.18. He wanted to talk to the agent on the page rather than in a
terminal. The storage and the agent-facing API have existed since 19 Sep; what
did not exist was any way for HIM to write one, which made "agents read the
comments and reply" a loop with no beginning.

SHAPE TAKEN FROM THE REFERENCE, not invented: the saved claude.ai HTML he gave
us puts a ButtonGroup labelled "Comments" in the page bar, holding a Comment
toggle and a "More comment options" menu, with the conversation in a panel
rather than in the document flow. So: a control in the bar, and a panel docked
beside the page, which narrows to make room. With scripts a class opens it and
nothing navigates; without them `:target` on #comments shows the same docked
panel, so it never depends on script.

Selecting text and anchoring a comment to it is tested beside the shim
(test_shim_marks.py, tests/js/comments.test.mjs); the panel in a real browser
is test_panel_browser.py.
"""

import re

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
    got = client.post(f"/c/{page.id}", data={"body": "x"}, follow_redirects=False)
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
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        body="<script>alert(1)</script>",
    )
    body = client.get(f"/a/{page.id}").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_a_mark_with_no_note_renders_no_empty_paragraph(client, db, page):
    """Final review, Item 4a: a 'remove this'/'looks good' mark carries no note
    of its own (comments.py's `add()` allows an empty `body` for those two
    kinds), and the bubble used to render `<p></p>` for it -- an empty tag
    with nothing in it, not a rendering of anything Jeremy or the agent said.
    """
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="",
        kind="looks_good",
        quote="x",
        anchor={"kind": "range", "path": "body>p:nth-of-type(1)", "start": 0, "end": 1},
    )
    body = client.get(f"/a/{page.id}").text
    assert "<p></p>" not in body


def test_an_empty_page_still_offers_the_box(client, db, page):
    """An empty section with a box is an invitation; an empty section with no
    box is a fault."""
    body = client.get(f"/a/{page.id}").text
    assert f'action="/c/{page.id}"' in body
    assert "Nothing yet" in body


def test_the_panel_needs_no_script(client, db, page):
    """Everything else in this gallery works with scripts off and a
    conversation is a poor place to start making exceptions."""
    comments.add(db, artifact_id=page.id, author_kind="agent", author="seat", body="hi")
    body = client.get(f"/a/{page.id}").text
    # Scoped to the drawer element, not "everything after it" -- the page's own
    # <script src="/gallery.js"> lives further down the shell and a slice to the
    # end of the document catches it. The claim is about the panel's own markup.
    start = body.index('<aside class="cpanel"')
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


# --- docked beside the page, never an overlay ---------------------------------
#
# The drawer used to be a `:target` overlay: opening it was a navigation, and it
# laid a blurred, dimmed backdrop over a page that can be tens of thousands of
# pixels tall. Both were slow. The panel now docks beside the page, which
# narrows to make room, and a script toggles it by class without navigating.


def _rules(css: str, needle: str) -> list[str]:
    """Every rule block in `css` whose selector mentions `needle`, comments
    stripped -- the prose above a rule may say "blur"; the rule may not."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    out, pos = [], 0
    while (at := css.find(needle, pos)) != -1:
        start = css.rfind("}", 0, at) + 1
        end = css.find("}", at) + 1
        out.append(css[start:end])
        pos = end
    return out


def test_the_panel_has_no_backdrop_and_keeps_its_id(client, db, page):
    body = client.get(f"/a/{page.id}").text
    start = body.index('<aside class="cpanel" id="comments"')
    panel = body[start : body.index('<div class="dlg" id="rename"', start)]
    assert "dlg-back" not in panel, "a backdrop is the dimming and the blur"
    assert "cdrawer" not in body
    # With scripts off the close control is a link that drops the fragment.
    assert '<a class="cclose" href="#_" aria-label="Close comments">' in panel
    assert 'aria-controls="comments"' in body


def test_scripts_off_still_shows_the_panel_docked_and_unblurred():
    from artifacts import gallery

    css = gallery._CSS
    target = [r for r in _rules(css, ".cpanel:target") if "display:block" in r]
    assert target, "with scripts off, #comments must still open the panel"
    for rule in _rules(css, "cpanel"):
        assert "backdrop-filter" not in rule and "blur" not in rule, rule
    # Docked, so the page beside it narrows rather than sitting underneath.
    assert any(
        ":has(.cpanel:target)" in r and "margin-right:min(24rem,100%)" in r
        for r in _rules(css, ".wrap.full")
    )


def test_an_open_panel_narrows_the_page_and_is_a_sheet_on_a_phone():
    from artifacts import gallery

    css = gallery._CSS
    assert any(
        "body.cpanel-open .wrap.full" in r and "margin-right:min(24rem,100%)" in r
        for r in _rules(css, "cpanel-open")
    )
    docked = next(r for r in _rules(css, ".cpanel{") if "position:fixed" in r)
    assert "top:48px" in docked and "right:0" in docked and "bottom:0" in docked
    phone = css[css.index("/* THE PANEL ON A PHONE") :]
    phone = phone[: phone.index("/* END THE PANEL ON A PHONE")]
    assert "@media (max-width:34rem)" in phone and "height:70vh" in phone
    assert "margin-right:0" in phone, "on a phone the sheet slides over the page"


def test_the_script_never_navigates_to_open_the_panel():
    from artifacts import gallery

    js = gallery._COMMENTS_JS
    assert not re.search(r"location\.hash\s*=(?!=)", js), "opening must not navigate"
    assert '"artifacts.comments.open"' in js
    assert "cpanel-open" in js


# --- this page / all pages ----------------------------------------------------
#
# The reference's comments panel carries a filter menu reading "This page /
# All pages" and a "Show resolved" checkbox. The fold above has always been the
# second one; this is the first. Until now a conversation could only be found
# by remembering which page it was on, which is exactly the shape that let a
# question sit unseen for seventeen hours on the decisions side.


@pytest.fixture()
def other(db):
    return store.publish(db, "other-seat", "Forge migration", PAGE)


def test_the_drawer_offers_both_scopes_and_says_which_one_is_on(client, db, page):
    body = client.get(f"/a/{page.id}").text
    assert f'href="/a/{page.id}#comments">This page' in body
    assert f'href="/a/{page.id}?comments=all#comments">All pages' in body
    # The one you are looking at is marked, the same way a wall tab is.
    drawer = body[body.index('id="comments"') :][:900]
    assert '<a class="tab on" href="/a/' in drawer

    everywhere = client.get(f"/a/{page.id}?comments=all").text
    drawer = everywhere[everywhere.index('id="comments"') :][:900]
    assert f'class="tab on" href="/a/{page.id}?comments=all#comments"' in drawer


def test_all_pages_shows_a_conversation_from_another_page(client, db, page, other):
    comments.add(
        db,
        artifact_id=other.id,
        author_kind="agent",
        author="other-seat",
        body="the runner token expired",
    )

    here = client.get(f"/a/{page.id}").text
    everywhere = client.get(f"/a/{page.id}?comments=all").text

    # The pin that matters: "this page" must actually mean this page, or the
    # filter is decoration.
    assert "the runner token expired" not in here
    assert "the runner token expired" in everywhere
    # And it says which page it is about, with a way to get there.
    assert "Forge migration" in everywhere
    assert f'href="/a/{other.id}#comments"' in everywhere


def test_a_reply_in_the_all_pages_view_goes_to_its_own_page(client, db, page, other):
    """The bug this shape invites: every reply form posting to the page you
    happen to be looking at, so a remark about the forge lands on the board."""
    root = comments.add(
        db, artifact_id=other.id, author_kind="agent", author="other-seat", body="hi"
    )

    everywhere = client.get(f"/a/{page.id}?comments=all").text

    # Sliced around the root id rather than forward from it: the id first
    # appears in the reply form's hidden thread_root value, which is AFTER the
    # action attribute this test is about.
    at = everywhere.index(root.id)
    around = everywhere[max(0, at - 600) : at + 600]
    assert f'action="/c/{other.id}"' in around
    # And nothing in this view posts a comment at the page being looked at:
    # the composer belongs to a page, so the all-pages view does not carry one.
    assert f'action="/c/{page.id}"' not in everywhere


def test_a_deleted_pages_conversations_are_not_dragged_back(client, db, page, other):
    """Deleting is how he tidies the wall. A view that kept talking about a
    page he deleted would undo that."""
    comments.add(
        db,
        artifact_id=other.id,
        author_kind="agent",
        author="other-seat",
        body="about the deleted one",
    )
    store.set_deleted(db, other.id, True, agent="other-seat")

    everywhere = client.get(f"/a/{page.id}?comments=all").text

    assert "about the deleted one" not in everywhere


def test_an_archived_pages_conversations_are_here_and_flagged(client, db, page, other):
    """Archiving is "not now", not "gone" -- and a question waiting in an
    archived page's thread is exactly the kind that gets lost."""
    comments.add(
        db,
        artifact_id=other.id,
        author_kind="agent",
        author="other-seat",
        body="still waiting on you",
    )
    store.set_archived(db, other.id, True, agent="other-seat")

    everywhere = client.get(f"/a/{page.id}?comments=all").text

    assert "still waiting on you" in everywhere
    assert 'class="carch"' in everywhere


def test_settled_conversations_stay_folded_in_the_all_pages_view(
    client, db, page, other
):
    root = comments.add(
        db,
        artifact_id=other.id,
        author_kind="human",
        author="jeremy",
        body="settled elsewhere",
    )
    comments.resolve(db, root.id, by="jeremy", agent=None)

    everywhere = client.get(f"/a/{page.id}?comments=all").text

    assert "settled elsewhere" in everywhere
    assert 'class="settled-cmts"' in everywhere
    assert "1 conversation" in everywhere


def test_the_all_pages_view_says_so_when_there_is_nothing_anywhere(client, db, page):
    everywhere = client.get(f"/a/{page.id}?comments=all").text

    assert "No conversations on any page yet" in everywhere


def test_a_scope_this_drawer_does_not_know_is_refused_at_the_edge(client, db, page):
    assert client.get(f"/a/{page.id}?comments=everything").status_code == 422


def test_the_page_you_are_looking_at_is_not_shown_twice_over(client, db, page):
    """FOUND IN A RENDER, not by the tests, which were perfectly happy. The
    page being viewed is also in the all-pages list, so a replies map built for
    it and then appended to again showed every reply twice -- a conversation
    that read as if the agent had said the same thing twice."""
    root = comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="ask"
    )
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="seat",
        thread_root=root.id,
        body="the one and only reply",
    )

    everywhere = client.get(f"/a/{page.id}?comments=all").text

    assert everywhere.count("the one and only reply") == 1


def test_a_page_with_something_open_comes_before_one_that_is_all_settled(
    client, db, page, other
):
    """Also found in the render: an archived page whose every thread was
    settled sat at the top showing nothing but a closed fold, pushing two live
    conversations down. Recency alone is the wrong sort for a view whose
    question is "where is something being said"."""
    live = comments.add(
        db, artifact_id=page.id, author_kind="agent", author="seat", body="still open"
    )
    assert live.resolved_at is None
    done = comments.add(
        db, artifact_id=other.id, author_kind="human", author="jeremy", body="finished"
    )
    comments.resolve(db, done.id, by="jeremy", agent=None)

    everywhere = client.get(f"/a/{page.id}?comments=all").text

    # Compared on the GROUP HEADINGS, not on the titles: "Board" is also the
    # page's own title in the bar above the drawer, so a looser search would
    # pass whatever the order was.
    live_at = everywhere.index(f'class="cpage"><a href="/a/{page.id}#comments">')
    done_at = everywhere.index(f'class="cpage"><a href="/a/{other.id}#comments">')
    assert live_at < done_at, "the page with an open conversation must come first"
