"""`artifacts-decide` and a set-aside question (2 Nov 2026; runcove-5t3oj.64):
`_fmt` says SET ASIDE rather than "unanswered", `list` has a section for them, and
`wait` exits 9 when everything it waits on is parked."""

import json

import pytest
from test_client import key_file, live  # noqa: F401

from ream import decide, decisions, store

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture()
def page(db, key_file):
    return store.publish(db, "ops-seat", "Board", PAGE)


def _ask(db, page, session="cove/1", question="ship?"):
    return decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=page.id,
        question=question,
        options=OPTIONS,
        session=session,
    )


def _run(args, key_path, monkeypatch, session="cove/1") -> int:
    monkeypatch.setenv("REAM_SESSION", session)
    return decide.main(["--key-file", str(key_path), *args])


def _row(**over):
    row = {
        "id": "d1",
        "question": "ship?",
        "options": OPTIONS,
        "kind": "question",
        "answer": None,
        "closed_at": None,
        "created_at": "2026-11-02T10:00:00+00:00",
    }
    row.update(over)
    return row


def test_fmt_says_set_aside_not_unanswered():
    text = decide._fmt(
        _row(
            set_aside=True,
            set_aside_since="2026-11-02T13:40:00+00:00",
            set_aside_why="archived",
        )
    )
    assert "SET ASIDE since 2026-11-02T13:40, page archived" in text
    assert "unanswered" not in text
    assert "WITHDRAWN" not in text


def test_fmt_says_binned_and_mentions_a_reopening():
    binned = decide._fmt(
        _row(
            set_aside=True,
            set_aside_since="2026-11-02T13:40:00+00:00",
            set_aside_why="binned",
        )
    )
    assert "page binned" in binned
    back = decide._fmt(_row(reopened_at="2026-11-03T09:00:00+00:00"))
    assert "REOPENED 2026-11-03T09:00" in back
    assert "unanswered" in back


def test_a_withdrawn_question_still_says_withdrawn():
    text = decide._fmt(_row(closed_at="2026-11-02T11:00:00+00:00"))
    assert "WITHDRAWN" in text and "SET ASIDE" not in text


def test_wait_on_a_page_whose_questions_are_parked_exits_9(
    db, live, key_file, page, capsys, monkeypatch
):
    path, _ = key_file
    asked = _ask(db, page)
    store.set_archived(db, page.id, True, agent=None)
    assert _run(["wait", "--page", page.id, "--timeout", "5s"], path, monkeypatch) == 9
    out = capsys.readouterr().out
    assert "SET ASIDE" in out and asked.id in out


def test_wait_on_a_binned_page_exits_9_instead_of_waiting_out_the_timeout(
    db, live, key_file, page, capsys, monkeypatch
):
    path, _ = key_file
    _ask(db, page)
    store.set_deleted(db, page.id, True, agent=None)
    assert _run(["wait", "--page", page.id, "--timeout", "5s"], path, monkeypatch) == 9
    assert "SET ASIDE" in capsys.readouterr().out


def test_wait_on_a_page_exits_9_when_it_is_set_aside_while_waiting(
    db, live, key_file, page, capsys, monkeypatch, on_first_listen
):
    path, _ = key_file
    asked = _ask(db, page)
    listener = on_first_listen(lambda: store.set_deleted(db, page.id, True, agent=None))
    assert _run(["wait", "--page", page.id, "--timeout", "40s"], path, monkeypatch) == 9
    listener.thread.join()
    assert asked.id in capsys.readouterr().out


def test_the_office_wide_wait_prints_set_aside_and_reopened_lines(
    db, live, key_file, page, capsys, monkeypatch, on_first_listen
):
    path, _ = key_file
    mine = _ask(db, page, question="mine?")
    other = store.publish(db, "ops-seat", "Other", PAGE)
    both = _ask(db, other, question="other?")

    def park_and_answer():
        store.set_archived(db, page.id, True, agent=None)
        decisions.answer(db, both.id, letter="a", note="n", by="jeremy")

    listener = on_first_listen(park_and_answer)
    code = _run(["wait", "--timeout", "40s", "--json"], path, monkeypatch)
    listener.thread.join()
    out = capsys.readouterr().out
    assert code == 0, "an answer on the other page still wakes it with exit 0"
    assert "set_aside" in out and mine.id in out
    json_lines = [line for line in out.splitlines() if line.startswith("{")]
    assert any(json.loads(line).get("event") == "set_aside" for line in json_lines)


def test_list_has_a_set_aside_section(db, live, key_file, page, capsys, monkeypatch):
    path, _ = key_file
    asked = _ask(db, page, question="parked?")
    store.set_deleted(db, page.id, True, agent=None)
    assert _run(["list"], path, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "SET ASIDE (not waiting on him" in out
    assert asked.id in out and "page binned" in out
