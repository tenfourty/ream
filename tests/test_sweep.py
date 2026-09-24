"""The small-fix sweep of 24 Sep 2026 (Jeremy's order: sweep first). One test
per fix, named for the bead it closes."""

import pytest
from fastapi.testclient import TestClient
from test_client import key_file, live  # noqa: F401

from artifacts import client, comments, decisions, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>t</title></head><body><p>x</p></body></html>"
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def auth(db):
    token, _ = keys.mint(db, agent="seat")
    return {"Authorization": f"Bearer {token}"}


def test_ij8_97_an_answer_on_a_deleted_page_is_not_owed_to_anyone(db, api, auth):
    page = store.publish(db, "seat", "Gone", PAGE)
    asked = decisions.ask(db, agent="seat", artifact_id=page.id, question="q?", options=OPTIONS)
    decisions.answer(db, asked.id, letter="a", by="jeremy")
    assert [d.decision.id for d in decisions.uncollected(db, agent="seat", stuck_after_seconds=0)] == [asked.id]
    store.set_deleted(db, page.id, True, agent="seat")
    assert decisions.uncollected(db, agent="seat", stuck_after_seconds=0) == []
    store.set_deleted(db, page.id, False, agent="seat")
    assert len(decisions.uncollected(db, agent="seat", stuck_after_seconds=0)) == 1, "restoring brings it back"


def test_ij8_45_an_open_only_view_says_how_many_answers_it_left_out(db, api, auth):
    page = store.publish(db, "seat", "Board", PAGE)
    for q in ("one?", "two?"):
        asked = decisions.ask(db, agent="seat", artifact_id=page.id, question=q, options=OPTIONS)
        decisions.answer(db, asked.id, letter="a", by="jeremy")
    decisions.ask(db, agent="seat", artifact_id=page.id, question="open?", options=OPTIONS)
    only_open = api.get(f"/api/v1/artifacts/{page.id}/decisions?open_only=true", headers=auth)
    assert len(only_open.json()) == 1
    assert only_open.headers["x-artifacts-answered-omitted"] == "2"
    everything = api.get(f"/api/v1/artifacts/{page.id}/decisions", headers=auth)
    assert everything.headers["x-artifacts-answered-omitted"] == "0"


def test_ij8_55_the_event_feed_carries_no_terminal_control_characters(db, api, auth):
    page = store.publish(db, "seat", "Board", PAGE)
    asked = decisions.ask(db, agent="seat", artifact_id=page.id, question="q?", options=OPTIONS)
    decisions.answer(db, asked.id, letter="a", note="line one\nline two\x1b[2Jcleared", by="jeremy")
    rows = api.get("/api/v1/events?since=0", headers=auth).json()
    note = next(r["note"] for r in rows if r["kind"] == "decision_answered")
    assert "\x1b" not in note
    assert "line one" in note and "line two[2Jcleared" in note, "his words survive"
    assert comments  # imported for the next producer of free text


def test_ij8_94_the_gallery_scripts_are_revalidated(api):
    for path in ("/gallery.js", "/connect.js"):
        assert api.get(path).headers["cache-control"] == "no-cache", path


def test_ij8_93_an_agent_can_bin_list_and_restore_with_the_tool(
    db, live, key_file, capsys  # noqa: F811
):
    path, _ = key_file
    page = store.publish(db, "ops-seat", "Binnable", PAGE)

    def run(*a):
        return client.main(["--key-file", str(path), *a])

    assert run("--delete", page.id) == 0
    assert "recoverable for thirty days" in capsys.readouterr().err
    assert run("--deleted") == 0
    assert page.id in capsys.readouterr().out
    assert run("--restore", page.id) == 0
    capsys.readouterr()
    assert run("--deleted") == 0
    assert "nothing in the bin" in capsys.readouterr().out


@pytest.mark.parametrize("encoded", ["&#10;", "&#x0a;", "&#X0A", "&NewLine;", "&#0010;"])
def test_ij8_8_an_encoded_line_break_does_not_shift_line_numbers(encoded):
    from artifacts import scan

    body = f"<p>first{encoded}still the first line</p>\n<p>DB_PASSWORD=hunter2fake</p>\n"
    found = scan.scan(body)
    assert [f.line for f in found] == [2], found


def test_ij8_8_a_longer_number_is_not_mistaken_for_a_line_break():
    from artifacts import scan

    # &#100; is "d": the fix must not eat the start of a longer reference.
    assert scan._text_view("&#100;&#10;x") == "d x"
