"""Comments pinned to a spot, slice 1 (docs/agent/specs/2026-10-29-pinned-comments.md):
the evidence a pin carries, where it is after a republish, and how that reads
in the API, the CLI and the comments panel.

`tests/test_anchors.py` covers the resolution rules on their own; this file
covers the wiring around them, on whichever database the run uses.
"""

import pytest
from fastapi.testclient import TestClient

from ream import anchors, comments, decide, gallery, keys, store
from ream.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>Costs</title></head><body>"
    '<h2 id="costs">Costs</h2><p>Cost per seat is flat across plans.</p>'
    "<h2>Hosting</h2><p>Hosting is billed monthly in advance.</p>"
    "</body></html>"
)
QUOTE = "Cost per seat is flat across plans"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db), base_url="https://testserver")


@pytest.fixture()
def seats(db):
    owner, _ = keys.mint(db, agent="owner")
    other, _ = keys.mint(db, agent="other")
    return {
        "owner": {"Authorization": f"Bearer {owner}"},
        "other": {"Authorization": f"Bearer {other}"},
    }


@pytest.fixture()
def page(db):
    return store.publish(db, "owner", "Costs", PAGE)


def _republish(db, page, body):
    store.patch(
        db, "owner", page.id, body, store.get_owned(db, "owner", page.id).revision
    )


def _pin(db, page, quote=QUOTE, anchor=None, **extra):
    return comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="Why flat?",
        quote=quote,
        anchor=anchor
        or {
            "kind": "range",
            "path": "body>p:nth-of-type(1)",
            "start": 0,
            "end": len(quote),
            "heading": "Costs",
            "heading_id": "costs",
        },
        **extra,
    )


def _only(db, page):
    [row] = comments.list_for_artifact(db, page.id)
    return row


def _pin_many(db, page, n):
    """`n` pins at distinct spots (a different element id each), all on `page`."""
    for i in range(n):
        _pin(
            db,
            page,
            anchor={
                "kind": "range",
                "path": "p",
                "start": 0,
                "end": len(QUOTE),
                "heading": "Costs",
                "el_id": f"spot{i}",
            },
        )


def test_a_listing_parses_a_page_once_however_many_pins_it_has(db, page, monkeypatch):
    _pin_many(db, page, 8)
    calls = {"headings": 0, "locate": 0}
    real_headings, real_locate = anchors._headings, anchors.sections.locate_many

    def headings(body):
        calls["headings"] += 1
        return real_headings(body)

    def locate_many(body, ids):
        calls["locate"] += 1
        return real_locate(body, ids)

    monkeypatch.setattr(anchors, "_headings", headings)
    monkeypatch.setattr(anchors.sections, "locate_many", locate_many)
    rows = comments.list_for_artifact(db, page.id)
    assert len(rows) == 8
    assert {r.anchor_state for r in rows} == {"current"}
    assert calls["headings"] <= 1 and calls["locate"] == 1


def test_pins_past_the_html_cap_are_unknown_not_slow(db, page, monkeypatch):
    monkeypatch.setattr(comments, "HTML_PINS_MAX", 3)
    _pin_many(db, page, 5)
    rows = comments.list_for_artifact(db, page.id)
    states = sorted(r.anchor_state for r in rows)
    assert states == ["", "", "current", "current", "current"]


# --- what an anchor may carry -------------------------------------------------


def test_a_range_anchor_keeps_the_heading_and_ids_it_carries():
    got = comments.validate_anchor(
        {
            "kind": "range",
            "path": "p",
            "start": 0,
            "end": 5,
            "heading": "Costs",
            "heading_id": "costs",
            "el_id": "plans.v2",
        }
    )
    assert got["heading"] == "Costs" and got["heading_id"] == "costs"
    assert got["el_id"] == "plans.v2"


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"heading": ""}, id="empty-heading"),
        pytest.param({"heading": "h" * 201}, id="long-heading"),
        pytest.param({"heading": 5}, id="heading-not-text"),
        pytest.param({"heading_id": "has space"}, id="id-outside-the-charset"),
        pytest.param({"el_id": "x" * 201}, id="long-id"),
        pytest.param({"el_id": ""}, id="empty-id"),
        pytest.param({"heading": "\u200d\u00ad\ud800"}, id="nothing-printable"),
        pytest.param({"el_id": "x" * 129}, id="id-longer-than-sections-address"),
        pytest.param({"colour": "red"}, id="unknown-key"),
    ],
)
def test_a_bad_heading_or_id_is_refused_without_saying_which(extra):
    anchor = {"kind": "range", "path": "p", "start": 0, "end": 5, **extra}
    with pytest.raises(comments.InvalidAnchor) as raised:
        comments.validate_anchor(anchor)
    assert str(raised.value) == str(comments.InvalidAnchor())


def test_a_heading_with_unprintable_characters_is_cleaned_not_refused():
    heading = "Team \U0001f469\u200d\U0001f4bb no\u00adtes\ud800\x1b!"
    got = comments.validate_anchor(
        {"kind": "range", "path": "p", "start": 0, "end": 5, "heading": heading}
    )
    assert got["heading"] == "Team \U0001f469\U0001f4bb notes!"
    assert got["heading"].isprintable()


def test_an_agent_quote_under_a_heading_with_a_joiner_still_pins(client, db, seats):
    body = PAGE.replace(
        "<h2>Hosting</h2>", "<h2>Team \U0001f469\u200d\U0001f4bb no\u00adtes</h2>"
    )
    page = store.publish(db, "owner", "Costs", body)
    got = _post(
        client,
        seats,
        page.id,
        anchor={"kind": "text", "quote": "Hosting is billed monthly"},
    )
    assert got.status_code == 201, got.text
    assert got.json()["anchor"]["heading"] == "Team \U0001f469\U0001f4bb notes"


def test_a_quote_anchor_takes_context_and_nothing_else():
    got = comments.validate_anchor({"kind": "quote", "heading": "Costs", "prefix": "a"})
    assert got == {"kind": "quote", "prefix": "a", "heading": "Costs"}
    for bad in ({"kind": "quote", "path": "p"}, {"kind": "quote", "start": 1}):
        with pytest.raises(comments.InvalidAnchor):
            comments.validate_anchor(bad)


def test_a_quote_anchor_without_a_quote_is_refused(db, page):
    with pytest.raises(comments.InvalidAnchor):
        comments.add(
            db,
            artifact_id=page.id,
            author_kind="human",
            author="jeremy",
            body="x",
            anchor={"kind": "quote", "heading": "Costs"},
        )


def test_remove_and_looks_good_stay_range_only(db, page):
    with pytest.raises(comments.InvalidKind):
        comments.add(
            db,
            artifact_id=page.id,
            author_kind="human",
            author="jeremy",
            body="",
            kind="remove",
            quote=QUOTE,
            anchor={"kind": "quote", "heading": "Costs"},
        )


# --- anchor_state across a republish -----------------------------------------


def test_a_pin_on_an_untouched_page_is_current(db, page):
    _pin(db, page)
    row = _only(db, page)
    assert (row.anchor_state, row.anchor_note) == ("current", "")
    assert row.quote_state == "current"


def test_a_comment_that_pins_nothing_has_no_state(db, page):
    comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="Hi"
    )
    assert _only(db, page).anchor_state == ""


def test_current_to_edited_when_the_words_change_in_place(db, page):
    _pin(db, page, anchor={**_range(), "prefix": "Cost per seat is "})
    _republish(db, page, PAGE.replace("is flat across plans", "is flat for every plan"))
    row = _only(db, page)
    assert row.anchor_state == "edited"
    assert row.quote_state == "changed", "the older reading is unchanged"
    assert row.version_n == 1, "and the pin stays on the version it was made on"


def test_edited_to_outdated_when_the_spot_goes(db, page):
    _pin(db, page)
    _republish(db, page, PAGE.replace("<p>Cost per seat is flat across plans.</p>", ""))
    assert _only(db, page).anchor_state == "outdated"


def test_outdated_comes_back_to_current_only_when_the_words_return(db, page):
    _pin(db, page)
    gone = PAGE.replace("<p>Cost per seat is flat across plans.</p>", "")
    _republish(db, page, gone)
    assert _only(db, page).anchor_state == "outdated"
    _republish(db, page, PAGE)
    assert _only(db, page).anchor_state == "current"


def test_a_quote_moved_under_another_heading_is_current_with_a_note(db, page):
    _pin(db, page)
    moved = PAGE.replace("<p>Cost per seat is flat across plans.</p>", "").replace(
        "</body>", "<p>Cost per seat is flat across plans.</p></body>"
    )
    _republish(db, page, moved)
    row = _only(db, page)
    assert row.anchor_state == "current"
    assert (
        row.anchor_note == 'moved: no longer under "Hosting"'
        or "moved" in row.anchor_note
    )


def test_the_state_follows_the_current_version_only(db, page):
    _pin(db, page)
    _republish(db, page, PAGE.replace("Cost per seat is flat across plans.", "Gone."))
    assert _only(db, page).anchor_state == "outdated"
    _republish(db, page, PAGE)
    assert _only(db, page).anchor_state == "current"


def test_a_reply_carries_the_state_of_its_root(db, page):
    root = _pin(db, page)
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="owner",
        body="Because.",
        thread_root=root.id,
    )
    _republish(db, page, PAGE.replace("Cost per seat is flat across plans.", "Gone."))
    states = [r.anchor_state for r in comments.list_for_artifact(db, page.id)]
    assert states == ["outdated", "outdated"]


def test_an_id_on_the_pin_is_read_against_the_page_html(db, page):
    _pin(
        db, page, quote="Hosting is billed monthly", anchor={**_range(), "el_id": "h1"}
    )
    _republish(
        db,
        page,
        PAGE.replace("<p>Hosting is billed", '<p id="h1">Hosting is billed'),
    )
    assert _only(db, page).anchor_state == "current"
    _republish(
        db,
        page,
        PAGE.replace(
            "<h2>Hosting</h2><p>Hosting is billed monthly in advance.</p>",
            '<h2>Hosting</h2><p id="h1">Hosting is now billed yearly.</p>',
        ),
    )
    assert _only(db, page).anchor_state == "edited"


def _range():
    return {"kind": "range", "path": "body>p", "start": 0, "end": 5}


# --- the marks the page paints ------------------------------------------------


def test_the_page_is_sent_range_and_quote_marks_but_never_a_note(db, page):
    _pin(db, page)
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="other",
        body="private note",
        quote="Hosting is billed monthly",
        anchor={"kind": "quote", "heading": "Hosting"},
    )
    marks = comments.marks_for_page(db, page.id)
    assert [m["anchor"]["kind"] for m in marks] == ["range", "quote"]
    assert "private note" not in repr(marks)


# --- the agent's quote --------------------------------------------------------


def _post(client, seats, page_id, who="other", **body):
    return client.post(
        f"/api/v1/artifacts/{page_id}/comments",
        json={"body": "A thought.", **body},
        headers=seats[who],
    )


def test_an_agent_quote_is_stored_as_a_quote_anchor_with_its_heading(
    client, db, seats, page
):
    got = _post(client, seats, page.id, anchor={"kind": "text", "quote": QUOTE.upper()})
    assert got.status_code == 201
    row = got.json()
    assert row["anchor"] == {"kind": "quote", "heading": "Costs", "heading_id": "costs"}
    assert row["quote"] == QUOTE.upper()


def test_a_section_the_quote_is_not_under_is_still_refused(client, db, seats):
    body = PAGE + "<h2>Costs</h2><p>Cost per seat is flat across plans.</p>"
    page = store.publish(db, "owner", "Costs", body)
    got = _post(
        client,
        seats,
        page.id,
        anchor={"kind": "text", "quote": QUOTE, "section": "Hosting"},
    )
    assert got.status_code == 422, "the quote is not under that section"


def test_an_agents_quote_comment_is_outdated_after_a_republish(client, db, seats, page):
    _post(client, seats, page.id, anchor={"kind": "text", "quote": QUOTE})
    _republish(db, page, PAGE.replace("Cost per seat is flat across plans.", "Gone."))
    [row] = client.get(
        f"/api/v1/artifacts/{page.id}/comments", headers=seats["owner"]
    ).json()
    assert row["anchor_state"] == "outdated"
    assert row["version_n"] == 1


def test_older_agent_comments_stored_as_page_plus_quote_still_get_a_state(
    client, db, seats, page
):
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="agent",
        author="other",
        body="Old style.",
        quote=QUOTE,
        anchor={"kind": "page"},
    )
    assert _only(db, page).anchor_state == "current"
    _republish(db, page, PAGE.replace("Cost per seat is flat across plans.", "Gone."))
    assert _only(db, page).anchor_state == "outdated"


# --- the API filter -----------------------------------------------------------


def test_the_comments_list_can_be_cut_to_the_outdated_spots(client, db, seats, page):
    _post(client, seats, page.id, anchor={"kind": "text", "quote": QUOTE})
    _post(
        client,
        seats,
        page.id,
        anchor={"kind": "text", "quote": "Hosting is billed monthly"},
    )
    _republish(db, page, PAGE.replace("Cost per seat is flat across plans.", "Gone."))
    url = f"/api/v1/artifacts/{page.id}/comments"
    everything = client.get(url, headers=seats["owner"]).json()
    gone = client.get(url + "?anchor_state=outdated", headers=seats["owner"]).json()
    current = client.get(url + "?anchor_state=current", headers=seats["owner"]).json()
    assert len(everything) == 2
    assert [r["quote"] for r in gone] == [QUOTE]
    assert [r["quote"] for r in current] == ["Hosting is billed monthly"]


def test_the_account_wide_list_filters_the_same_way(client, db, seats, page):
    _post(client, seats, page.id, anchor={"kind": "text", "quote": QUOTE})
    _republish(db, page, PAGE.replace("Cost per seat is flat across plans.", "Gone."))
    got = client.get("/api/v1/comments?anchor_state=outdated", headers=seats["owner"])
    assert [r["anchor_state"] for r in got.json()] == ["outdated"]
    none = client.get("/api/v1/comments?anchor_state=current", headers=seats["owner"])
    assert none.json() == []


def test_an_unknown_state_filter_is_refused(client, seats, page):
    got = client.get(
        f"/api/v1/artifacts/{page.id}/comments?anchor_state=moved",
        headers=seats["owner"],
    )
    assert got.status_code == 422
    assert got.json()["detail"]["field"] == "anchor_state"


def test_a_page_that_is_not_yours_answers_the_filter_like_no_such_page(
    client, db, seats
):
    closed = store.publish(db, "owner", "Closed", PAGE)
    store.set_agents_can_read(db, closed.id, False, agent="owner")
    got = client.get(
        f"/api/v1/artifacts/{closed.id}/comments?anchor_state=outdated",
        headers=seats["other"],
    )
    missing = client.get(
        "/api/v1/artifacts/00000000000000000000000000/comments?anchor_state=outdated",
        headers=seats["other"],
    )
    assert (got.status_code, got.json()) == (missing.status_code, missing.json())


# --- the CLI ------------------------------------------------------------------


def _row(**over):
    return {
        "id": "c1",
        "artifact_id": "p1",
        "version_n": 3,
        "quote": QUOTE,
        "kind": "comment",
        "author_kind": "human",
        "author": "jeremy",
        "body": "Why flat?",
        "created_at": "2026-10-29T10:00:00Z",
        "thread_root": None,
        "anchor": {"kind": "range", "heading": "Costs"},
        "anchor_state": "outdated",
        "anchor_note": "",
        **over,
    }


def test_the_cli_prints_where_the_pin_was_and_that_it_is_outdated():
    [thread] = decide._threads([_row()])
    shown = decide._fmt_thread(thread)
    assert (
        f'  on: "{QUOTE}"  (under "Costs" · v3 · OUTDATED: this spot is gone from'
        " the current version)"
    ) in shown


def test_the_cli_says_edited_and_says_moved():
    [edited] = decide._threads([_row(anchor_state="edited")])
    assert "text edited since" in decide._fmt_thread(edited)
    [moved] = decide._threads(
        [_row(anchor_state="current", anchor_note='moved: no longer under "Costs"')]
    )
    assert 'moved: no longer under "Costs"' in decide._fmt_thread(moved)


def test_the_cli_prints_no_parenthesis_for_a_row_with_no_state():
    [thread] = decide._threads([_row(anchor_state="", anchor={"kind": "page"})])
    assert f'  on: "{QUOTE}"\n' in decide._fmt_thread(thread) + "\n"


def test_outdated_is_a_flag_on_comments():
    args = decide.build_parser().parse_args(["comments", "--outdated"])
    assert args.outdated is True
    assert decide.build_parser().parse_args(["comments"]).outdated is False


# --- the comments panel -------------------------------------------------------


def _panel(db, page):
    rows = comments.list_for_artifact(db, page.id, include_resolved=True)
    return gallery._render_comments(rows, page.id)


def test_the_panel_lists_outdated_pins_at_the_top_and_keeps_them_in_place(db, page):
    _pin(db, page)
    _republish(db, page, PAGE.replace("Cost per seat is flat across plans.", "Gone."))
    html = _panel(db, page)
    assert "Outdated pins (1)" in html
    assert html.index("Outdated pins") < html.index('class="threads"')
    assert html.count(QUOTE) >= 2, "listed at page level and still in its thread"
    assert "Outdated: this spot is gone from the current version" in html
    assert "it was there in version 1" in html
    assert "under Costs" in html


def test_the_panel_has_no_outdated_list_when_nothing_is_outdated(db, page):
    _pin(db, page)
    html = _panel(db, page)
    assert "Outdated pins" not in html
    assert "Outdated:" not in html


def test_the_panel_says_text_was_edited_and_that_a_pin_moved(db, page):
    _pin(db, page, anchor={**_range(), "prefix": "Cost per seat is "})
    _republish(db, page, PAGE.replace("is flat across plans", "is flat for every plan"))
    html = _panel(db, page)
    assert "Text edited since version 1" in html
    assert "Outdated pins" not in html


def test_a_settled_outdated_thread_is_not_in_the_page_level_list(db, page):
    root = _pin(db, page)
    _republish(db, page, PAGE.replace("Cost per seat is flat across plans.", "Gone."))
    comments.resolve(db, root.id, by="jeremy")
    assert "Outdated pins" not in _panel(db, page)


def test_a_quote_is_escaped_in_the_list_and_the_thread(db, page):
    hostile = "<img src=x onerror=alert(1)>"
    body = PAGE.replace("Cost per seat is flat across plans.", hostile)
    other = store.publish(db, "owner", "Hostile", body)
    _pin(db, other, quote=hostile)
    _republish(db, other, body.replace(hostile, "Gone."))
    html = _panel(db, other)
    assert "<img src=x" not in html and "&lt;img src=x" in html
