"""Comments pinned to a spot, slice 2 (docs/agent/specs/2026-10-29-pinned-comments.md,
section 8): the `element` anchor, what resolves it after a republish, what an
agent may send for it, and how it reads in the API, the CLI and the panel.

The pure rules come first (no database), then the wiring, on whichever
database the run uses. The shim and the gallery script are run for real in
tests/js/marks.test.mjs and tests/js/comments.test.mjs.
"""

import pytest
from fastapi.testclient import TestClient
from test_client import key_file, live  # noqa: F401

from artifacts import anchors, comments, content, decide, gallery, keys, store
from artifacts.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>Costs</title></head><body>"
    '<h2 id="costs">Costs</h2>'
    "<p>Cost per seat is flat across plans.</p>"
    '<table id="plans"><tr><th>Plan</th><th>Price</th></tr>'
    "<tr><td>Pro</td><td>$12 a month</td></tr></table>"
    '<figure><img id="chart" src="c.png" alt="Revenue by quarter">'
    "<figcaption>Revenue, 2026</figcaption></figure>"
    "<h2>Hosting</h2>"
    "<p>Hosting is billed monthly in advance.</p>"
    "</body></html>"
)


NO_TABLE = PAGE[: PAGE.index("<table")] + PAGE[PAGE.index("</table>") + 8 :]


def _el(**extra):
    return {"kind": "element", **extra}


# --- the pure rules: what an element is, in words ------------------------------


def test_an_elements_quote_is_its_visible_text_capped():
    outer = '<p id="x">Cost  per <em>seat</em>\n is flat.</p>'
    assert anchors.element_quote(outer) == "Cost per seat is flat."
    assert len(anchors.element_quote("<p>" + "word " * 100 + "</p>")) <= 200


def test_an_image_or_chart_is_named_by_its_alt_text_or_label():
    assert anchors.element_quote('<img src="c.png" alt="Revenue by quarter">') == (
        "Revenue by quarter"
    )
    assert anchors.element_quote('<canvas aria-label="Signups"></canvas>') == "Signups"
    assert anchors.element_quote('<div title="Map">&nbsp;</div>') == "Map"
    svg = '<svg viewBox="0 0 1 1"><title>Latency</title><rect/></svg>'
    assert anchors.element_quote(svg) == "Latency"


def test_an_svg_title_is_found_in_one_pass_and_an_unclosed_one_names_nothing():
    import time

    assert anchors.element_label(
        '<svg><TITLE lang="en">Rate &amp; <b>p95</b></TITLE></svg>'
    ) == ("Rate & p95")
    assert anchors.element_label("<svg><title>never closed</svg>") == ""
    hostile = "<svg>" + "<title>x" * 200_000 + "</svg>"
    began = time.monotonic()
    assert anchors.element_label(hostile) == ""
    assert time.monotonic() - began < 1.0


def test_the_gallery_moves_focus_only_for_a_pin_mode_turned_on_in_the_gallery():
    from pathlib import Path

    source = Path(gallery.__file__).read_text(encoding="utf-8")
    assert "if(hand)i.focus()" in source
    assert 'd.type==="artifact-pin-toggle")pinMode(!pinOn)' in source
    assert "pinMode(!pinOn,true)" in source


def test_an_element_with_text_is_named_by_the_text_not_its_label():
    assert anchors.element_quote('<p title="tip">Hello there</p>') == "Hello there"


def test_an_element_with_no_text_and_no_label_has_no_quote():
    assert anchors.element_quote('<img src="c.png">') == ""


def test_the_nearest_heading_before_a_position_and_its_id():
    pos = PAGE.index("<table")
    assert anchors.heading_before(PAGE, pos) == ("Costs", "costs")
    assert anchors.heading_before(PAGE, PAGE.index("<body>")) == ("", "")
    pos = PAGE.index("<p>Hosting")
    assert anchors.heading_before(PAGE, pos) == ("Hosting", "")


# --- the pure rules: where an element pin is now -------------------------------


def test_an_element_found_by_id_with_its_text_is_current():
    anchor = _el(id="plans", tag="table")
    assert anchors.resolve(anchor, "Plan Price Pro $12 a month", PAGE) == (
        anchors.CURRENT,
        "",
    )


def test_an_element_found_by_id_with_new_text_is_edited():
    body = PAGE.replace("$12 a month", "$15 a month")
    anchor = _el(id="plans", tag="table")
    assert anchors.resolve(anchor, "Plan Price Pro $12 a month", body)[0] == (
        anchors.EDITED
    )


def test_an_element_whose_id_is_gone_and_which_had_no_quote_is_outdated():
    body = PAGE.replace('id="plans"', 'id="prices"')
    assert anchors.resolve(_el(id="plans"), "", body) == (anchors.OUTDATED, "")


def test_an_element_whose_id_is_repeated_is_not_trusted():
    body = PAGE.replace("<h2>Hosting</h2>", '<h2 id="plans">Hosting</h2>')
    assert anchors.resolve(_el(id="plans"), "", body) == (anchors.OUTDATED, "")


def test_an_element_that_is_only_an_id_is_current_while_the_id_is_there():
    assert anchors.resolve(_el(id="chart", tag="img"), "", PAGE) == (
        anchors.CURRENT,
        "",
    )


def test_an_image_pin_is_current_while_its_alt_text_is_what_it_was():
    anchor = _el(id="chart", tag="img", label="Revenue by quarter")
    assert anchors.resolve(anchor, "Revenue by quarter", PAGE)[0] == anchors.CURRENT


def test_an_image_pin_is_edited_when_its_alt_text_changes():
    body = PAGE.replace("Revenue by quarter", "Revenue by month")
    anchor = _el(id="chart", tag="img", label="Revenue by quarter")
    assert anchors.resolve(anchor, "Revenue by quarter", body)[0] == anchors.EDITED


def test_an_image_with_no_id_is_found_by_its_label_anywhere_on_the_page():
    anchor = _el(tag="img", ordinal=1, heading="Costs")
    assert anchors.resolve(anchor, "Revenue by quarter", PAGE)[0] == anchors.CURRENT
    gone = PAGE.replace("Revenue by quarter", "Something else entirely")
    assert anchors.resolve(anchor, "Revenue by quarter", gone)[0] == anchors.OUTDATED


def test_an_element_with_only_a_tag_and_an_ordinal_is_unknown():
    anchor = _el(tag="img", ordinal=2, heading="Costs")
    assert anchors.resolve(anchor, "", PAGE) == ("", "")


def test_an_element_without_an_id_is_found_by_its_text_under_its_heading():
    anchor = _el(tag="p", heading="Costs", heading_id="costs")
    assert anchors.resolve(anchor, "Cost per seat is flat across plans.", PAGE) == (
        anchors.CURRENT,
        "",
    )
    moved = PAGE.replace(
        "<h2>Hosting</h2>",
        "<h2>Hosting</h2>" + "<p>Cost per seat is flat across plans.</p>",
    ).replace("<p>Cost per seat is flat across plans.</p><table", "<table", 1)
    state, note = anchors.resolve(anchor, "Cost per seat is flat across plans.", moved)
    assert state == anchors.CURRENT and "moved" in note


def test_an_element_removed_while_its_neighbours_stay_is_outdated_not_edited():
    anchor = _el(
        id="plans",
        tag="table",
        prefix="Cost per seat is flat across plans.",
        suffix="Revenue, 2026 Hosting",
    )
    body = PAGE.replace("<tr><td>Pro</td><td>$12 a month</td></tr>", "").replace(
        'id="plans"', 'id="gone"'
    )
    assert anchors.resolve(anchor, "Plan Price Pro $12 a month", body)[0] == (
        anchors.OUTDATED
    )


# --- the pure rules: what stands there now -------------------------------------


def test_an_edited_element_shows_what_is_there_now():
    body = PAGE.replace("$12 a month", "$15 a month")
    anchor = _el(id="plans", tag="table")
    assert anchors.current_quote(anchor, "Plan Price Pro $12 a month", body) == (
        "Plan Price Pro $15 a month"
    )


def test_an_edited_image_shows_its_new_alt_text():
    body = PAGE.replace("Revenue by quarter", "Revenue by month")
    anchor = _el(id="chart", tag="img")
    assert anchors.current_quote(anchor, "Revenue by quarter", body) == (
        "Revenue by month"
    )


def test_an_edited_quote_shows_the_words_between_its_prefix_and_suffix():
    body = PAGE.replace("is flat across plans", "is flat for every plan")
    anchor = {
        "kind": "range",
        "prefix": "Cost per seat ",
        "suffix": ". Plan Price",
    }
    got = anchors.current_quote(anchor, "is flat across plans", body)
    assert got == "is flat for every plan"


def test_nothing_is_shown_as_current_when_the_surroundings_are_gone_too():
    anchor = {"kind": "range", "prefix": "no such words", "suffix": "nor these"}
    assert anchors.current_quote(anchor, "Cost per seat", PAGE) == ""


def test_the_current_quote_is_capped():
    body = PAGE.replace("$12 a month", "word " * 400)
    assert len(anchors.current_quote(_el(id="plans"), "x", body)) <= 500


# --- the anchor an element carries ----------------------------------------------


def test_an_element_anchor_keeps_its_keys_and_nothing_else():
    full = _el(
        id="plans",
        tag="table",
        path="body>table:nth-of-type(1)",
        ordinal=1,
        heading="Costs",
        heading_id="costs",
        label="Plans",
        row="Pro",
        column="Price",
        prefix="x" * 32,
        suffix="y" * 32,
    )
    assert comments.validate_anchor(full) == full


@pytest.mark.parametrize(
    "bad",
    [
        _el(id="has space"),
        _el(id="x" * 201),
        _el(id=""),
        _el(id=5),
        _el(tag="TABLE"),
        _el(tag="a b"),
        _el(tag="x" * 17),
        _el(tag=""),
        _el(ordinal=0),
        _el(ordinal=True),
        _el(ordinal="1"),
        _el(ordinal=10_001),
        _el(path="p" * 513),
        _el(path=7),
        _el(label=""),
        _el(label="x" * 201),
        _el(row="x" * 201),
        _el(column=3),
        _el(prefix="x" * 65),
        _el(suffix=1),
        _el(path="\ud800"),
        _el(label="\ud800"),
        _el(start=0),
        _el(quote="smuggled"),
        {"kind": "element", "x": 1},
    ],
)
def test_a_bad_element_anchor_is_refused_without_saying_which(bad):
    with pytest.raises(comments.InvalidAnchor) as raised:
        comments.validate_anchor(bad)
    assert str(raised.value) == "anchor"


def test_an_element_anchor_may_be_bare_the_quote_is_checked_by_add():
    assert comments.validate_anchor({"kind": "element"}) == {"kind": "element"}


# --- the wiring ------------------------------------------------------------------


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
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


def _pin(db, page, anchor, quote="", **extra):
    return comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="Check this.",
        quote=quote,
        anchor=anchor,
        **extra,
    )


def _only(db, page):
    [row] = comments.list_for_artifact(db, page.id)
    return row


def _post(client, seats, page_id, who="owner", **body):
    return client.post(
        f"/api/v1/artifacts/{page_id}/comments",
        json={"body": "A thought.", **body},
        headers=seats[who],
    )


@pytest.mark.parametrize(
    "anchor,quote",
    [
        (_el(id="plans"), ""),
        (_el(heading="Costs"), ""),
        (_el(tag="p"), "Cost per seat is flat across plans."),
    ],
)
def test_a_human_element_pin_needs_an_id_a_heading_or_a_quote_and_gets_one(
    db, page, anchor, quote
):
    row = _pin(db, page, anchor, quote)
    assert row.anchor["kind"] == "element"


def test_an_element_with_no_id_no_heading_and_no_quote_is_refused(db, page):
    with pytest.raises(comments.InvalidAnchor):
        _pin(db, page, _el(tag="img", ordinal=1))


def test_remove_and_looks_good_stay_range_only(db, page):
    for kind in ("remove", "looks_good"):
        with pytest.raises(comments.InvalidKind):
            _pin(db, page, _el(id="plans"), "Plan Price", kind=kind)


def test_a_reply_inherits_the_element_anchor_of_its_root(db, page):
    root = _pin(db, page, _el(id="plans", tag="table"), "Plan Price")
    reply = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="Also",
        thread_root=root.id,
    )
    assert reply.anchor == root.anchor and reply.quote == root.quote


def test_an_element_pin_with_no_text_is_still_painted_and_a_note_never_is(db, page):
    _pin(db, page, _el(id="chart", tag="img"))
    [mark] = comments.marks_for_page(db, page.id)
    assert mark["anchor"]["kind"] == "element" and mark["quote"] == ""
    assert "Check this" not in str(mark)


def test_a_listing_reads_an_element_pin_against_the_page(db, page):
    _pin(db, page, _el(id="plans", tag="table"), "Plan Price Pro $12 a month")
    assert _only(db, page).anchor_state == "current"
    _republish(db, page, PAGE.replace("$12 a month", "$15 a month"))
    row = _only(db, page)
    assert row.anchor_state == "edited"
    assert row.anchor_current == "Plan Price Pro $15 a month"
    # The id and its words both go: the quote elsewhere would still hold it.
    _republish(db, page, NO_TABLE)
    assert _only(db, page).anchor_state == "outdated"


def test_a_vanished_id_falls_back_to_the_quote_still_on_the_page(db, page):
    _pin(db, page, _el(id="plans", tag="table"), "Plan Price Pro $12 a month")
    _republish(db, page, PAGE.replace('id="plans"', 'id="other"'))
    assert _only(db, page).anchor_state == "current"


def test_an_edited_text_pin_lists_the_words_now_between_its_surroundings(db, page):
    quote = "is flat across plans"
    anchor = {
        "kind": "range",
        "path": "body>p:nth-of-type(1)",
        "start": 14,
        "end": 34,
        "prefix": "Cost per seat ",
        "suffix": ". Plan Price",
    }
    _pin(db, page, anchor, quote)
    _republish(db, page, PAGE.replace(quote, "is flat for every plan"))
    row = _only(db, page)
    assert (row.anchor_state, row.anchor_current) == (
        "edited",
        "is flat for every plan",
    )


def test_a_current_pin_has_no_current_quote_to_show(db, page):
    _pin(db, page, _el(id="plans"), "Plan Price Pro $12 a month")
    assert _only(db, page).anchor_current == ""


# --- an agent's element anchor ----------------------------------------------------


def test_an_agent_pins_an_element_by_its_id_and_the_server_fills_the_rest(
    client, db, seats, page
):
    got = _post(client, seats, page.id, anchor={"kind": "element", "id": "plans"})
    assert got.status_code == 201
    row = got.json()
    anchor = row["anchor"]
    assert (anchor["kind"], anchor["id"], anchor["tag"]) == (
        "element",
        "plans",
        "table",
    )
    assert (anchor["heading"], anchor["heading_id"]) == ("Costs", "costs")
    assert row["quote"] == "Plan Price Pro $12 a month"
    assert anchor["prefix"].endswith("is flat across plans.")
    assert anchor["suffix"].startswith("Revenue, 2026")
    assert len(anchor["prefix"]) <= 32 and len(anchor["suffix"]) <= 32


def test_an_agent_pinning_an_image_gets_its_alt_text_as_the_quote_and_label(
    client, db, seats, page
):
    row = _post(
        client, seats, page.id, anchor={"kind": "element", "id": "chart"}
    ).json()
    assert row["quote"] == "Revenue by quarter"
    assert (row["anchor"]["tag"], row["anchor"]["label"]) == (
        "img",
        "Revenue by quarter",
    )


def test_the_state_comes_back_when_the_comment_is_created(client, db, seats, page):
    row = _post(
        client, seats, page.id, anchor={"kind": "element", "id": "plans"}
    ).json()
    assert row["anchor_state"] == "current"
    text = _post(
        client,
        seats,
        page.id,
        anchor={"kind": "text", "quote": "Hosting is billed monthly"},
    ).json()
    assert (text["anchor_state"], text["anchor_note"]) == ("current", "")
    plain = _post(client, seats, page.id).json()
    assert plain["anchor_state"] == ""


def test_an_id_that_is_not_on_the_page_is_a_422_that_says_so(client, db, seats, page):
    got = _post(client, seats, page.id, anchor={"kind": "element", "id": "nope"})
    assert got.status_code == 422
    detail = got.json()["detail"]
    assert detail["field"] == "anchor" and "nope" in detail["detail"]


def test_an_id_used_twice_is_a_422_not_a_guess(client, db, seats):
    body = PAGE.replace("<h2>Hosting</h2>", '<h2 id="plans">Hosting</h2>')
    page = store.publish(db, "owner", "Twice", body)
    got = _post(client, seats, page.id, anchor={"kind": "element", "id": "plans"})
    assert got.status_code == 422
    assert got.json()["detail"]["field"] == "anchor"


@pytest.mark.parametrize(
    "anchor",
    [
        {"kind": "element"},
        {"kind": "element", "id": 5},
        {"kind": "element", "id": "plans", "tag": "p"},
        {"kind": "element", "id": "a b"},
        {"kind": "element", "id": "x" * 129},
    ],
)
def test_an_agent_element_anchor_is_an_id_and_nothing_else(
    client, db, seats, page, anchor
):
    got = _post(client, seats, page.id, anchor=anchor)
    assert got.status_code == 422
    assert got.json()["detail"]["field"] == "anchor"


def test_an_agent_cannot_pin_a_page_that_is_not_its_own_or_open_to_it(
    client, db, seats, page
):
    store.set_agents_can_read(db, page.id, False, agent="owner")
    mine = _post(
        client, seats, page.id, who="other", anchor={"kind": "element", "id": "plans"}
    )
    missing = _post(
        client,
        seats,
        "00000000000000000000000000",
        who="other",
        anchor={"kind": "element", "id": "plans"},
    )
    assert (mine.status_code, mine.json()) == (missing.status_code, missing.json())


def test_an_agents_element_pin_goes_outdated_with_the_element(client, db, seats, page):
    _post(client, seats, page.id, anchor={"kind": "element", "id": "plans"})
    _republish(db, page, NO_TABLE)
    rows = client.get(
        f"/api/v1/artifacts/{page.id}/comments?anchor_state=outdated",
        headers=seats["owner"],
    ).json()
    assert len(rows) == 1 and rows[0]["anchor"]["id"] == "plans"


# --- the shim and the gallery script carry the pin messages -----------------------


def test_the_shim_speaks_the_pin_messages_to_the_gallery_only(monkeypatch):
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    source = content._viewer_report()
    for name in ("artifact-pin-mode", "artifact-pin-hover", "artifact-pin"):
        assert f'"{name}"' in source
    assert '"*"' not in source
    assert "outerHTML" not in source and "innerHTML" not in source


def test_the_gallery_script_has_the_pin_mode_and_draws_with_text_content_only():
    js = gallery._COMMENTS_JS
    for name in ("artifact-pin-mode", "artifact-pin-hover", "artifact-pin"):
        assert f'"{name}"' in js
    block = js[js.index("// ELEMENT PINS") : js.index("// Full screen:")]
    assert "innerHTML" not in block and "insertAdjacentHTML" not in block
    assert "textContent" in block


def test_the_page_bar_offers_a_pin_button_beside_comment():
    bar = gallery._comments_button("p1", 0)
    assert 'class="pinmodebtn"' in bar and "hidden" in bar and "Pin" in bar
    assert bar.index("cbtn") < bar.index("pinmodebtn")
    for rule in (".pinmodebtn", ".pinout", ".pinbadge"):
        assert rule in gallery._CSS


def test_the_page_bar_has_a_comment_on_page_button_that_works_without_scripts():
    bar = gallery._comments_button("p1", 0)
    assert 'class="pagecbtn"' in bar and "Comment on page" in bar
    assert 'href="#cwhole"' in bar and bar.index("pagecbtn") < bar.index('class="cbtn')
    assert ".pagecbtn" in gallery._CSS


# --- the CLI -------------------------------------------------------------------------


def _row(**over):
    return {
        "id": "c1",
        "artifact_id": "p1",
        "version_n": 3,
        "quote": "Plan Price Pro $12 a month",
        "kind": "comment",
        "author_kind": "human",
        "author": "jeremy",
        "body": "Why?",
        "created_at": "2026-10-29T10:00:00Z",
        "thread_root": None,
        "anchor": {
            "kind": "element",
            "id": "plans",
            "tag": "table",
            "heading": "Costs",
        },
        "anchor_state": "current",
        "anchor_note": "",
        "anchor_current": "",
        **over,
    }


def test_the_cli_prints_the_element_a_pin_is_on():
    [thread] = decide._threads([_row()])
    shown = decide._fmt_thread(thread)
    assert '  element: <table id="plans"> under "Costs"' in shown
    assert '"Plan Price Pro $12 a month"' in shown
    assert "v3" in shown


def test_the_cli_names_a_table_cell_by_its_row_and_column():
    anchor = {"kind": "element", "tag": "td", "row": "Pro", "column": "Price"}
    [thread] = decide._threads([_row(anchor=anchor)])
    assert 'row "Pro", column "Price"' in decide._fmt_thread(thread)


def test_the_cli_prints_an_image_by_its_label():
    anchor = {"kind": "element", "tag": "img", "label": "Revenue by quarter"}
    [thread] = decide._threads([_row(anchor=anchor, quote="Revenue by quarter")])
    assert '<img> "Revenue by quarter"' in decide._fmt_thread(thread)


def test_the_cli_shows_both_the_original_and_the_current_quote_when_edited():
    [thread] = decide._threads(
        [_row(anchor_state="edited", anchor_current="Plan Price Pro $15 a month")]
    )
    shown = decide._fmt_thread(thread)
    assert "text edited since" in shown
    assert 'now: "Plan Price Pro $15 a month"' in shown
    assert '"Plan Price Pro $12 a month"' in shown


def test_the_cli_prints_no_now_line_for_a_current_pin():
    [thread] = decide._threads([_row()])
    assert "now:" not in decide._fmt_thread(thread)


def test_element_is_a_flag_on_comment():
    args = decide.build_parser().parse_args(
        ["comment", "P", "hi", "--element", "plans"]
    )
    assert args.element == "plans"
    assert decide.build_parser().parse_args(["comment", "P", "hi"]).element is None


def test_the_comment_help_explains_element():
    parser = decide.build_parser()
    help_text = parser._subparsers._group_actions[0].choices["comment"].format_help()
    assert "--element" in help_text and "id" in help_text


PAGE_ID_PAGE = PAGE


def _run(args, path):
    return decide.main(["--key-file", str(path), "--session", "t/1", *args])


def test_comment_element_pins_another_offices_page_end_to_end(
    db, live, key_file, capsys
):
    path, _ = key_file
    theirs = store.publish(db, "cove", "Costs", PAGE)
    assert (
        _run(["comment", theirs.id, "is $12 right?", "--element", "plans"], path) == 0
    )
    [row] = comments.list_for_artifact(db, theirs.id)
    assert row.anchor["kind"] == "element" and row.anchor["id"] == "plans"
    assert row.anchor_state == "current"
    capsys.readouterr()
    assert _run(["comment", theirs.id, "x", "--element", "nope"], path) == 1
    assert "nope" in capsys.readouterr().err
    assert (
        _run(["comment", theirs.id, "x", "--element", "plans", "--quote", "y"], path)
        == 2
    )


# --- the panel --------------------------------------------------------------------------


def _panel(db, page):
    rows = comments.list_for_artifact(db, page.id, include_resolved=True)
    return gallery._render_comments(rows, page.id)


def test_the_panel_says_what_is_pinned_where_and_on_which_version(db, page):
    _pin(db, page, _el(id="plans", tag="table", heading="Costs"), "Plan Price")
    html = _panel(db, page)
    assert "Pinned on version 1" in html
    assert "under Costs" in html
    assert "&lt;table id=&quot;plans&quot;&gt;" in html or "table#plans" in html


def test_the_panel_shows_the_original_and_the_current_quote_of_an_edited_spot(db, page):
    _pin(db, page, _el(id="plans", tag="table"), "Plan Price Pro $12 a month")
    _republish(db, page, PAGE.replace("$12 a month", "$15 a month"))
    html = _panel(db, page)
    assert "Text edited since version 1" in html
    assert "Plan Price Pro $12 a month" in html
    assert "Now: " in html and "Plan Price Pro $15 a month" in html
    assert "Outdated pins" not in html


def test_an_outdated_element_pin_keeps_its_quote_in_the_list_and_the_thread(db, page):
    _pin(db, page, _el(id="plans", tag="table"), "Plan Price Pro $12 a month")
    _republish(db, page, NO_TABLE)
    html = _panel(db, page)
    assert "Outdated pins (1)" in html
    assert html.count("Plan Price Pro $12 a month") >= 2


def test_a_table_cell_and_an_image_are_labelled_in_the_panel(db, page):
    _pin(
        db,
        page,
        _el(tag="td", row="Pro", column="Price", heading="Costs"),
        "$12 a month",
    )
    _pin(
        db,
        page,
        _el(id="chart", tag="img", label="Revenue by quarter"),
        "Revenue by quarter",
    )
    html = _panel(db, page)
    assert "Row Pro, column Price" in html
    assert "Image: Revenue by quarter" in html


def test_everything_a_page_sends_is_escaped_in_the_panel(db, page):
    evil = "<img src=x onerror=alert(1)>"
    _pin(
        db,
        page,
        _el(
            id="plans",
            tag="table",
            heading="<b>h</b>",
            row=evil,
            column=evil,
            label=evil,
        ),
        evil,
    )
    html = _panel(db, page)
    assert "<img src=x" not in html and "<b>h</b>" not in html
    assert "&lt;img src=x" in html
