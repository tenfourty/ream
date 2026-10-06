"""Updating one section of a page (6 Oct 2026).

The failure this fixes: a whole-page republish from a local copy that had
drifted from live silently dropped content outside the part being changed.
A section update applies one operation to one element of the LIVE page on the
server, and stores the whole result as a new version through the ordinary
path -- so every check a whole-page update runs, runs on the whole result.
"""

import json

import pytest
from fastapi.testclient import TestClient
from test_client import key_file, live  # noqa: F401 -- fixtures

from artifacts import client as cli
from artifacts import decisions, keys, sections, store
from artifacts.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>CI</title></head><body>"
    '<section id="running"><h2>Running now</h2>'
    '<ul id="rows"><li>build 1</li><li>build 2</li></ul></section>'
    '<p id="status" data-section-title="Status line">green</p>'
    '<div id="log">first</div>'
    "</body></html>"
)


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_DISPLAY_TZ", "Europe/Paris")
    return TestClient(create_gallery_app(db=db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def page(client, db):
    token, _ = keys.mint(db, agent="ci-seat")
    made = client.post(
        "/api/v1/artifacts", json={"title": "CI", "body": PAGE}, headers=auth(token)
    ).json()
    return made["id"], token


def body_of(db, artifact_id: str) -> str:
    return store.get_body(db, "ci-seat", artifact_id).body


def section(client, artifact_id, token, el, **payload):
    return client.patch(
        f"/api/v1/artifacts/{artifact_id}/sections/{el}",
        json=payload,
        headers=auth(token),
    )


# --- each operation ---------------------------------------------------------


def test_replace_swaps_the_content_and_keeps_the_tag_and_attributes(client, db, page):
    aid, token = page
    r = section(client, aid, token, "status", op="replace", html="amber")
    assert r.status_code == 200, r.text
    assert '<p id="status" data-section-title="Status line">amber</p>' in body_of(
        db, aid
    )


def test_append_adds_a_last_child(client, db, page):
    aid, token = page
    assert section(
        client, aid, token, "rows", op="append", html="<li>b3</li>"
    ).is_success
    assert "<li>build 2</li><li>b3</li></ul>" in body_of(db, aid)


def test_prepend_adds_a_first_child(client, db, page):
    aid, token = page
    assert section(
        client, aid, token, "rows", op="prepend", html="<li>b0</li>"
    ).is_success
    assert '<ul id="rows"><li>b0</li><li>build 1</li>' in body_of(db, aid)


def test_remove_removes_the_element_and_nothing_else(client, db, page):
    aid, token = page
    assert section(client, aid, token, "log", op="remove").is_success
    assert body_of(db, aid) == PAGE.replace('<div id="log">first</div>', "")


# --- addressing -------------------------------------------------------------


def test_an_unknown_id_is_refused_and_nothing_is_applied(client, db, page):
    aid, token = page
    r = section(client, aid, token, "nope", op="append", html="<li>x</li>")
    assert r.status_code == 422
    assert "no element with id 'nope'" in r.json()["detail"]["detail"]
    assert body_of(db, aid) == PAGE


def test_a_duplicated_id_is_refused(client, db):
    token, _ = keys.mint(db, agent="ci-seat")
    twice = PAGE.replace('<div id="log">', '<div id="rows">')
    aid = client.post(
        "/api/v1/artifacts", json={"title": "CI", "body": twice}, headers=auth(token)
    ).json()["id"]
    r = section(client, aid, token, "rows", op="replace", html="x")
    assert r.status_code == 422
    assert "2 elements have id 'rows'" in r.json()["detail"]["detail"]


def test_an_id_inside_a_comment_is_not_an_element():
    body = '<!-- <div id="x">old</div> --><div id="x">live</div>'
    assert sections.apply(body, "x", "replace", "new").body == (
        '<!-- <div id="x">old</div> --><div id="x">new</div>'
    )


@pytest.mark.parametrize("fragment", ["<div>open", "</ul><li>x", "<li>x<!-- cut"])
def test_an_unbalanced_fragment_is_refused(client, db, page, fragment):
    aid, token = page
    r = section(client, aid, token, "rows", op="append", html=fragment)
    assert r.status_code == 422, r.text
    assert body_of(db, aid) == PAGE


def test_a_self_closing_div_is_open_as_a_browser_reads_it():
    """`<div id="a"/>` is an OPEN div in HTML: a browser puts what follows
    inside it, so the section is everything up to its real end tag."""
    body = '<body><div id="a"/><p>inside</p></div><p id="b">after</p></body>'
    where = sections.locate(body, "a")
    assert where.inner(body) == "<p>inside</p>"
    assert not where.void
    assert sections.apply(body, "a", "replace", "new").body == (
        '<body><div id="a"/>new</div><p id="b">after</p></body>'
    )


def test_a_self_closing_div_in_a_fragment_is_left_open():
    with pytest.raises(sections.UnbalancedError):
        sections.check_balanced("<div/>")


def test_only_void_and_svg_elements_self_close():
    sections.check_balanced('<br/><img src="x"/><svg><path d="M0 0"/></svg>')
    body = '<svg id="s"><circle r="1"/><g id="g"/></svg><p id="p">x</p>'
    assert sections.locate(body, "g").void
    assert sections.locate(body, "s").inner(body) == '<circle r="1"/><g id="g"/>'


def test_a_row_whose_end_tag_html_allows_omitting_is_balanced():
    sections.check_balanced("<li>new row")
    sections.check_balanced('<svg><path d="M0 0"/></svg><br>')


# --- the whole-page checks ----------------------------------------------------


def test_a_secret_in_the_fragment_is_refused_by_the_page_check(client, db, page):
    aid, token = page
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    r = section(client, aid, token, "log", op="append", html=f"<p>key {secret}</p>")
    assert r.status_code == 422
    assert r.json()["detail"]["refused"][0]["field"] == "body"
    assert secret not in r.text
    assert body_of(db, aid) == PAGE


def _ask(db, aid):
    return decisions.ask(
        db,
        agent="ci-seat",
        artifact_id=aid,
        question="Ship it?",
        options=[{"letter": "a", "label": "Yes"}, {"letter": "b", "label": "No"}],
        recommended="a",
    )


def test_an_open_questions_slot_cannot_be_dropped(client, db, page):
    aid, token = page
    asked = _ask(db, aid)
    slot = f'<div data-artifact-decision="{asked.id}"></div>'
    assert section(client, aid, token, "log", op="append", html=slot).is_success
    r = section(client, aid, token, "log", op="replace", html="gone")
    assert r.status_code == 422
    assert r.json()["detail"]["decision"] == asked.id
    assert asked.id in r.json()["detail"]["detail"]
    assert slot in body_of(db, aid)


def test_a_closed_questions_slot_may_go(client, db, page):
    aid, token = page
    asked = _ask(db, aid)
    slot = f'<div data-artifact-decision="{asked.id}"></div>'
    assert section(client, aid, token, "log", op="append", html=slot).is_success
    decisions.close(db, "ci-seat", asked.id)
    assert section(client, aid, token, "log", op="remove").is_success
    assert slot not in body_of(db, aid)


# --- preconditions ------------------------------------------------------------


def test_if_revision_lands_when_current_and_refuses_when_stale(client, db, page):
    aid, token = page
    rev = client.get(f"/api/v1/artifacts/{aid}", headers=auth(token)).json()["revision"]
    first = section(client, aid, token, "log", op="append", html="a", if_revision=rev)
    assert first.status_code == 200
    stale = section(client, aid, token, "log", op="append", html="b", if_revision=rev)
    assert stale.status_code == 409
    assert stale.json()["detail"] == f"page changed since revision {rev}, re-fetch"
    # The version number is accepted too.
    by_n = section(client, aid, token, "log", op="append", html="c", if_revision="2")
    assert by_n.status_code == 200


def test_if_section_lands_when_unchanged_and_refuses_when_changed(client, db, page):
    aid, token = page
    got = client.get(f"/api/v1/artifacts/{aid}/sections/log", headers=auth(token))
    h = got.json()["section_revision"]
    # A change to ANOTHER section does not trip it.
    assert section(client, aid, token, "status", op="replace", html="red").is_success
    ok = section(client, aid, token, "log", op="replace", html="x", if_section=h)
    assert ok.status_code == 200
    stale = section(client, aid, token, "log", op="replace", html="y", if_section=h)
    assert stale.status_code == 409
    assert stale.json()["detail"] == "section changed, re-fetch"


def test_two_updates_to_different_sections_both_land(client, db, page):
    aid, token = page
    assert section(
        client, aid, token, "rows", op="prepend", html="<li>new</li>"
    ).is_success
    assert section(client, aid, token, "status", op="replace", html="red").is_success
    body = body_of(db, aid)
    assert "<li>new</li><li>build 1</li>" in body
    assert ">red</p>" in body


def test_a_section_update_that_loses_a_race_reapplies_to_the_new_page(db, page):
    """No row lock is held; a write that loses the race re-reads and
    re-applies, so neither change is lost."""
    aid, _ = page
    real_patch = store.patch
    raced = []

    def patch_after_a_rival(*args, **kwargs):
        if not raced:
            raced.append(1)
            live_body = store.get_body(db, "ci-seat", aid)
            real_patch(
                db,
                "ci-seat",
                aid,
                live_body.body.replace("green", "red"),
                live_body.revision,
            )
        return real_patch(*args, **kwargs)

    store.patch, saved = patch_after_a_rival, store.patch
    try:
        store.patch_section(db, "ci-seat", aid, "log", "append", "<b>x</b>")
    finally:
        store.patch = saved
    body = body_of(db, aid)
    assert ">red</p>" in body and "first<b>x</b>" in body


# --- the record -----------------------------------------------------------------


def test_it_is_a_new_version_with_the_section_note(client, db, page):
    aid, token = page
    r = section(client, aid, token, "rows", op="prepend", html="<li>n</li>", note="b9")
    out = r.json()
    assert out["version"] == 2
    assert set(out) >= {"page_revision", "section_revision", "version"}
    history = client.get(f"/api/v1/artifacts/{aid}/versions", headers=auth(token))
    assert history.json()[0]["note"] == "#rows: prepend -- b9"
    assert history.json()[1]["note"] is None


def test_get_returns_the_section_and_both_revisions(client, db, page):
    aid, token = page
    got = client.get(f"/api/v1/artifacts/{aid}/sections/status", headers=auth(token))
    assert got.status_code == 200
    data = got.json()
    assert data["html"] == '<p id="status" data-section-title="Status line">green</p>'
    assert data["section_revision"] == sections.section_revision(data["html"])
    read = client.get(f"/api/v1/artifacts/{aid}", headers=auth(token)).json()
    assert data["page_revision"] == read["revision"]


def test_another_agents_key_gets_the_no_such_page_answer(client, db, page):
    aid, _ = page
    other, _ = keys.mint(db, agent="someone-else")
    missing = section(client, "doesnotexist", other, "log", op="remove")
    theirs = section(client, aid, other, "log", op="remove")
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json() == missing.json()
    got = client.get(f"/api/v1/artifacts/{aid}/sections/log", headers=auth(other))
    nope = client.get(
        "/api/v1/artifacts/doesnotexist/sections/log", headers=auth(other)
    )
    assert got.status_code == nope.status_code == 404
    assert got.json() == nope.json()


# --- his wall -------------------------------------------------------------------


def test_the_wall_tile_names_the_part_that_changed(client, db, page):
    """Jeremy's ruling 1a: the part's heading, else its data-section-title,
    else its id."""
    aid, token = page
    row = '<h2>Running now</h2><ul id="rows"><li>build 3</li></ul>'
    section(client, aid, token, "running", op="replace", html=row)
    part = '<span class="part" title="Changed part">{}</span>'
    assert part.format("Running now: updated") in client.get("/").text
    section(client, aid, token, "rows", op="append", html="<li>b4</li>")
    assert part.format("rows: added") in client.get("/").text
    section(client, aid, token, "status", op="replace", html="red", notify=True)
    wall = client.get("/").text
    assert "Status line: updated</span>" in wall
    assert 'class="part flagged"' in wall
    assert "Look at this" in wall


def test_the_part_line_goes_once_he_has_read_it(client, db, page):
    aid, token = page
    section(client, aid, token, "log", op="append", html="x")
    store.mark_read(db, aid)
    assert 'class="part' not in client.get("/").text


def test_a_whole_page_update_names_no_part(client, db, page):
    aid, token = page
    section(client, aid, token, "rows", op="prepend", html="<li>n</li>")
    rev = client.get(f"/api/v1/artifacts/{aid}", headers=auth(token)).json()
    client.patch(
        f"/api/v1/artifacts/{aid}",
        json={"body": PAGE, "expected_revision": rev["revision"]},
        headers=auth(token),
    )
    assert 'class="part' not in client.get("/").text


# --- the CLI --------------------------------------------------------------------


def test_cli_fetch_then_update_a_section_round_trip(live, key_file, tmp_path, capsys):
    path, _ = key_file
    whole = tmp_path / "page.html"
    whole.write_text(PAGE)
    assert cli.main(["--key-file", str(path), str(whole), "--title", "CI"]) == 0
    capsys.readouterr()
    listing = cli._list(path.read_text())
    aid = listing[0]["id"]
    part = tmp_path / "part.html"
    assert (
        cli.main(
            ["--key-file", str(path), "--fetch", aid, "--section", "rows", str(part)]
        )
        == 0
    )
    err = capsys.readouterr().err
    assert part.read_text().startswith('<ul id="rows">')
    rev = err.split("section_revision ")[1].split()[0]
    row = tmp_path / "row.html"
    row.write_text("<li>build 3</li>")
    rc = cli.main(
        [
            "--key-file",
            str(path),
            "--update",
            aid,
            "--section",
            "rows",
            "--append",
            str(row),
            "--if-section",
            rev,
        ]
    )
    out = capsys.readouterr()
    assert rc == 0, out.err
    echoed = json.loads(out.out)
    assert echoed["version"] == 2 and echoed["section_revision"]
    # Same revision again: the section has changed, so it is refused.
    rc = cli.main(
        [
            "--key-file",
            str(path),
            "--update",
            aid,
            "--section",
            "rows",
            "--append",
            str(row),
            "--if-section",
            rev,
        ]
    )
    assert rc == 1
    assert "section changed, re-fetch" in capsys.readouterr().err


def test_cli_section_needs_exactly_one_operation(live, key_file, capsys):
    path, _ = key_file
    rc = cli.main(["--key-file", str(path), "--update", "x", "--section", "rows"])
    assert rc == 2
    assert "exactly one of" in capsys.readouterr().err


# --- what is stored beside the page faces the same checks ---------------------


def test_a_secret_in_the_note_is_refused_as_a_page_secret_is(client, db, page):
    aid, token = page
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    r = section(client, aid, token, "log", op="append", html="x", note=f"k {secret}")
    assert r.status_code == 422
    assert r.json()["detail"]["refused"][0]["field"] == "note"
    assert secret not in r.text
    assert body_of(db, aid) == PAGE


def test_a_secret_in_a_heading_that_becomes_the_label_is_refused(client, db, page):
    """The label is scanned in its own right, before the page is: the refusal
    names the note field (line 2 is the label), not the body."""
    aid, token = page
    split = "<h2>AKIA<b></b>" + "IOSFODNN7EXAMPLE</h2>"
    r = section(client, aid, token, "running", op="replace", html=split)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["refused"][0] == {
        "field": "note",
        "shape": "aws_access_key_id",
        "line": 2,
    }
    assert "IOSFODNN7EXAMPLE" not in r.text
    assert body_of(db, aid) == PAGE


@pytest.mark.parametrize("bad", ["has space", "esc%1B%5B31m", "a" * 129, "é"])
def test_an_id_outside_the_safe_charset_is_refused(client, page, bad):
    aid, token = page
    patched = section(client, aid, token, bad, op="remove")
    got = client.get(f"/api/v1/artifacts/{aid}/sections/{bad}", headers=auth(token))
    assert patched.status_code == got.status_code == 422
    assert "letters, digits" in patched.json()["detail"]["detail"]


def test_the_label_is_stripped_of_control_characters():
    body = '<div id="d"><h2>Run\x1b[31mning\x07 now</h2></div>'
    assert sections.locate(body, "d").label == "Run[31mning now"


def test_versions_printing_strips_escape_characters(capsys):
    cli._print_versions(
        [
            {
                "n": 2,
                "revision": "abc\x1b]0;x\x07",
                "bytes": 10,
                "created_at": "2026-10-06T10:00:00",
                "session": "s\x1b[2J",
                "live": True,
                "note": "#rows: append -- \x1b[31mred\u202e",
            }
        ]
    )
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "\u202e" not in out
    assert "#rows: append -- [31mred" in out


def test_a_label_is_clean_where_it_is_derived_and_everywhere_it_goes(client, db, page):
    """Sanitised in sections.py, so the version row, the wall, the API echo
    and the CLI all get the same clean value."""
    aid, token = page
    heading = "<h2>Running\x1b[31m\nnow</h2>"
    r = section(client, aid, token, "running", op="replace", html=heading)
    assert r.status_code == 200, r.text
    assert r.json()["section"] == "Running[31m now"
    with db.connect() as conn:
        stored = conn.execute(
            "select section from version where artifact_id = %s order by n desc",
            (aid,),
        ).fetchone()[0]
    assert stored == "Running[31m now: updated"
    assert "Running[31m now: updated</span>" in client.get("/").text


# --- the gate: the page as a browser reads it (html5lib) ------------------------

GATE_PAGE = (
    '<!doctype html><html><body><div id="a"><h2>A</h2>x</div>'
    '<div id="q">Q</div></body></html>'
)


@pytest.mark.parametrize(
    "fragment",
    [
        "<![CDATA[><textarea>]]>",
        "<![CDATA[><div>]]>",
        "<math><mi><textarea/></mi></math>",
        "<svg><desc><textarea/></desc></svg>",
        '<math><annotation-xml encoding="text/html"><div/></annotation-xml></math>',
        "<svg><foreignObject><div/></foreignObject></svg>",
        "<svg><div/><textarea/></svg>",
        "<svg><b/></svg>",
        "<p>a<table>",
        "<template><div/></template>",
        "<select><textarea/></select>",
        "<noscript/>",
    ],
)
def test_a_fragment_that_escapes_its_section_as_a_browser_reads_it_is_refused(
    fragment,
):
    with pytest.raises(sections.SectionError):
        sections.apply(GATE_PAGE, "a", "append", fragment)


@pytest.mark.parametrize(
    ("body", "el"),
    [
        # A duplicate id attribute: a browser keeps the FIRST.
        ('<html><body><div id="b" id="a">X</div></body></html>', "a"),
        # A browser reads the CDATA as a bogus comment ending at the first ">".
        ('<html><body><![CDATA[><div id="x">]]>X</div></body></html>', "x"),
        # Never seen by a reader.
        ('<html><body><template><div id="t">X</div></template></body></html>', "t"),
    ],
)
def test_a_target_a_browser_would_not_see_as_addressed_is_refused(body, el):
    with pytest.raises(sections.SectionError):
        sections.apply(body, el, "replace", "Z")


@pytest.mark.parametrize(
    ("body", "el", "fragment", "expect"),
    [
        (
            '<html><body><svg id="s"><g id="g"><circle r="1"/></g></svg></body></html>',
            "g",
            '<rect width="2"/>',
            '<g id="g"><circle r="1"/><rect width="2"/></g>',
        ),
        (
            "<html><body><table><tr><td id=c>1</td><td>2</td></tr></table></body></html>",
            "c",
            "<b>9</b>",
            "<td id=c><b>9</b></td>",
        ),
        (
            "<html><body><ul id=l><li>a<li>b</ul><p>after</p></body></html>",
            "l",
            "<li>c",
            "<li>b<li>c</ul>",
        ),
    ],
)
def test_legitimate_edits_in_svg_a_table_cell_and_a_list_are_accepted(
    body, el, fragment, expect
):
    op = "replace" if el == "c" else "append"
    assert expect in sections.apply(body, el, op, fragment).body


@pytest.mark.parametrize(
    "fake",
    [
        '<![CDATA[ data-artifact-decision="{d}" ]]>',
        "<span title='data-artifact-decision=\"{d}\"'></span>",
    ],
)
def test_slot_lookalike_text_does_not_count_as_the_slot(client, db, page, fake):
    aid, token = page
    asked = _ask(db, aid)
    slot = f'<span data-artifact-decision="{asked.id}">Q</span>'
    assert section(client, aid, token, "log", op="append", html=slot).is_success
    r = section(client, aid, token, "log", op="replace", html=fake.format(d=asked.id))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["decision"] == asked.id


def test_a_question_opened_during_the_update_keeps_its_slot(db, page):
    """The slot check runs again inside the write's transaction, under the
    artifact row lock ask() also takes."""
    aid, _ = page
    asked = _ask(db, aid)
    slot = f'<div data-artifact-decision="{asked.id}"></div>'
    store.patch_section(db, "ci-seat", aid, "log", "append", slot)
    decisions.close(db, "ci-seat", asked.id)
    real_patch = store.patch

    def reopen_then_patch(*args, **kwargs):
        with db.connect() as conn:
            conn.execute(
                "update decision set closed_at = null where id = %s", (asked.id,)
            )
        return real_patch(*args, **kwargs)

    store.patch = reopen_then_patch
    try:
        with pytest.raises(store.OpenDecisionDropped):
            store.patch_section(db, "ci-seat", aid, "log", "replace", "gone")
    finally:
        store.patch = real_patch
    assert slot in body_of(db, aid)


def test_a_retry_does_not_rescan_what_was_already_scanned(db, page, monkeypatch):
    from artifacts import ggscan

    aid, _ = page
    asked_about = []

    def scan(value):
        asked_about.append(value)
        return []

    monkeypatch.setattr(ggscan, "scan", scan)
    real_patch = store.patch
    raced = []

    def patch_after_a_rival(*args, **kwargs):
        if not raced:
            raced.append(1)
            live_body = store.get_body(db, "ci-seat", aid)
            real_patch(
                db,
                "ci-seat",
                aid,
                live_body.body.replace("green", "red"),
                live_body.revision,
            )
        return real_patch(*args, **kwargs)

    monkeypatch.setattr(store, "patch", patch_after_a_rival)
    store.patch_section(db, "ci-seat", aid, "log", "append", "<b>x</b>")
    notes = [v for v in asked_about if v.startswith("#log: append")]
    assert len(notes) == 1


def test_a_page_too_large_for_section_updates_says_republish_whole(client, db, page):
    """The browser-faithful check is pure Python; past 512 KB it would take
    too long, so the update is refused with the way forward."""
    from artifacts import sections

    big = (
        "<!doctype html><html><body><div id='log'>x</div>"
        + ("<p>" + "filler " * 100 + "</p>") * 800
        + "</body></html>"
    )
    assert len(big.encode()) > sections.SECTION_PAGE_MAX
    with pytest.raises(sections.TooLargeError, match="republish the whole page"):
        sections.apply(big, "log", "append", "<p>new</p>")
