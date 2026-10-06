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
