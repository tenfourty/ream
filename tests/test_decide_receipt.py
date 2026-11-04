"""`artifacts-decide receipt`, over real HTTP (receipts design 4.2)."""

import pytest
from test_client import key_file, live  # noqa: F401

from artifacts import decide, decisions, store

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"


@pytest.fixture()
def asked(db, key_file):
    art = store.publish(db, "ops-seat", "Board", PAGE)
    return decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=art.id,
        question="Which?",
        options=[{"letter": "a", "label": "First"}, {"letter": "b", "label": "Second"}],
    )


def _run(args, key_path) -> int:
    return decide.main(["--key-file", str(key_path), *args])


def test_receipt_working_with_a_note(live, key_file, asked, db, capsys):
    path, _ = key_file
    assert _run(["receipt", asked.id, "working", "--note", "bead x"], path) == 0
    assert "working" in capsys.readouterr().out
    got = decisions.get_owned(db, "ops-seat", asked.id).receipt
    assert got["state"] == "working" and got["note"] == "bead x"


def test_receipt_done_with_a_link_and_wont_do_spelled_with_a_dash(
    live,
    key_file,
    asked,
    db,
):
    path, _ = key_file
    decisions.answer(db, asked.id, letter="a", note="", by="jeremy")
    assert _run(["receipt", asked.id, "done", "--link", "https://x.test/1"], path) == 0
    assert _run(["receipt", asked.id, "wont-do", "--note", "no"], path) == 0
    assert decisions.get_owned(db, "ops-seat", asked.id).receipt["state"] == "wont_do"


def test_receipt_blocked_without_a_note_is_refused_with_the_reason(
    live,
    key_file,
    asked,
    capsys,
):
    path, _ = key_file
    assert _run(["receipt", asked.id, "blocked"], path) != 0
    assert "note" in capsys.readouterr().err


def test_receipt_shows_on_list_and_ack_is_still_seen(
    live,
    key_file,
    asked,
    db,
    capsys,
):
    path, _ = key_file
    assert _run(["ack", asked.id], path) == 0
    assert decisions.get_owned(db, "ops-seat", asked.id).receipt["state"] == "seen"
    assert _run(["receipt", asked.id, "working", "--note", "on it"], path) == 0
    capsys.readouterr()
    _run(["list"], path)
    assert "working: on it" in capsys.readouterr().out
