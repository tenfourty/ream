"""`artifacts-decide wait` hears only its own office's answers (feedback from
all five maintainers, 6 Oct 2026). One key is shared by every office on a
machine; a waiter that woke for any unacknowledged answer under it looped on
another office's answer for as long as nobody acknowledged it."""

import json
import os
import time

import pytest
from test_client import key_file, live  # noqa: F401

from artifacts import decide, decisions, store

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture()
def page(db, key_file):
    return store.publish(db, "ops-seat", "Board", PAGE)


def _ask(db, page, session, question="ship?"):
    return decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=page.id,
        question=question,
        options=OPTIONS,
        session=session,
    )


def _answer(db, asked, note):
    decisions.answer(db, asked.id, letter="a", note=note, by="jeremy")


def _run(args, key_path, monkeypatch, session="cove/1") -> int:
    if session is None:
        monkeypatch.delenv("ARTIFACTS_SESSION", raising=False)
    else:
        monkeypatch.setenv("ARTIFACTS_SESSION", session)
    return decide.main(["--key-file", str(key_path), *args])


def test_another_offices_answer_does_not_wake_you_and_your_own_does(
    db, live, key_file, page, capsys, monkeypatch, on_first_listen
):
    path, _ = key_file
    theirs = _ask(db, page, "homelab/2", "theirs?")
    mine = _ask(db, page, "cove/1", "mine?")

    def both():
        _answer(db, theirs, "homelab note")
        time.sleep(0.5)
        _answer(db, mine, "cove note")

    listener = on_first_listen(both)
    assert _run(["wait", "--timeout", "40s"], path, monkeypatch) == 0
    listener.thread.join(10)
    # Theirs ended the first hold (the feed is the key's) and was dropped; the
    # waiter held again, and its own answer ended the second.
    assert listener.held == 2, listener.held
    out = capsys.readouterr().out
    assert "cove note" in out
    assert "homelab note" not in out, "someone else's answer never wakes you"


def test_the_first_look_skips_another_offices_unacknowledged_answer(
    db, live, key_file, page, capsys, monkeypatch, brief_holds
):
    """The bug: `wait` returned at once on ANY unacknowledged answer under the
    key, so a re-armed waiter looped on another office's."""
    path, _ = key_file
    _answer(db, _ask(db, page, "homelab/2"), "homelab note")
    assert _run(["wait", "--timeout", "0.1s"], path, monkeypatch) == 3
    assert "homelab note" not in capsys.readouterr().out


def test_an_unlabelled_question_goes_to_the_keys_own_office(
    db, live, key_file, page, capsys, monkeypatch, brief_holds
):
    path, _ = key_file
    _answer(db, _ask(db, page, None), "unlabelled note")
    # The key's own office (named like its agent) hears it...
    assert _run(["wait", "--timeout", "5s"], path, monkeypatch, "ops-seat/9") == 0
    assert "unlabelled note" in capsys.readouterr().out
    # ...and another office sharing the key does not.
    assert _run(["wait", "--timeout", "0.1s"], path, monkeypatch, "cove/1") == 3
    assert "unlabelled note" not in capsys.readouterr().out


def test_your_own_unacknowledged_answer_comes_back_only_after_the_hour(
    db, live, key_file, page, capsys, monkeypatch, brief_holds
):
    path, _ = key_file
    asked = _ask(db, page, "cove/1")
    _answer(db, asked, "cove note")
    assert _run(["wait", "--timeout", "5s"], path, monkeypatch) == 0
    assert "cove note" in capsys.readouterr().out

    # Re-armed straight away: shown already, so not again.
    assert _run(["wait", "--timeout", "0.1s"], path, monkeypatch) == 3
    assert "cove note" not in capsys.readouterr().out

    # An hour on (the state file says it was shown an hour and a bit ago).
    state = os.path.join(os.environ["XDG_STATE_HOME"], "artifacts", "wait-cove.json")
    with open(state) as fh:
        shown = json.load(fh)
    assert len(shown) == 1
    with open(state, "w") as fh:
        json.dump({k: v - 3601 for k, v in shown.items()}, fh)
    assert _run(["wait", "--timeout", "5s"], path, monkeypatch) == 0
    assert "cove note" in capsys.readouterr().out

    # `list` keeps showing it regardless, until it is acknowledged.
    assert _run(["list"], path, monkeypatch) == 0
    assert "cove note" in capsys.readouterr().out


def test_office_and_page_narrow_together(
    db, live, key_file, page, capsys, monkeypatch, brief_holds
):
    path, _ = key_file
    other = store.publish(db, "ops-seat", "Other", PAGE)
    _answer(db, _ask(db, page, "cove/1"), "first page note")
    _answer(db, _ask(db, other, "homelab/2"), "their other note")
    assert (
        _run(
            ["wait", "--office", "cove", "--page", other.id, "--timeout", "0.1s"],
            path,
            monkeypatch,
            None,
        )
        == 3
    )
    _answer(db, _ask(db, other, "cove/3"), "other page note")
    assert (
        _run(
            ["wait", "--office", "cove", "--page", other.id, "--timeout", "5s"],
            path,
            monkeypatch,
            None,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "other page note" in out
    assert "first page note" not in out and "their other note" not in out


def test_all_offices_hears_everything(
    db, live, key_file, page, capsys, monkeypatch, brief_holds
):
    path, _ = key_file
    _answer(db, _ask(db, page, "homelab/2"), "homelab note")
    assert _run(["wait", "--all-offices", "--timeout", "5s"], path, monkeypatch) == 0
    assert "homelab note" in capsys.readouterr().out
    assert _run(["wait", "--office", "", "--timeout", "5s"], path, monkeypatch) == 0
    assert "homelab note" in capsys.readouterr().out


def test_no_label_and_no_office_hears_everything_and_says_so(
    db, live, key_file, page, capsys, monkeypatch, brief_holds
):
    path, _ = key_file
    _answer(db, _ask(db, page, "homelab/2"), "homelab note")
    assert _run(["wait", "--timeout", "5s"], path, monkeypatch, None) == 0
    captured = capsys.readouterr()
    assert "homelab note" in captured.out
    assert "ARTIFACTS_SESSION" in captured.err and "--office" in captured.err


def test_timeout_zero_keeps_holding_and_resumes_from_its_cursor(
    db, live, key_file, page, capsys, monkeypatch
):
    path, _ = key_file
    monkeypatch.setattr(decide, "_HOLD", 1)
    asked = _ask(db, page, "cove/1")
    real = decide._request
    sinces: list[int] = []

    def counting(method, url, *args, **kwargs):
        if "&wait=" in url:
            sinces.append(int(url.split("since=")[1].split("&")[0]))
            if len(sinces) == 3:
                _answer(db, asked, "third hold")
        return real(method, url, *args, **kwargs)

    monkeypatch.setattr(decide, "_request", counting)
    assert _run(["wait", "--timeout", "0"], path, monkeypatch) == 0
    captured = capsys.readouterr()
    assert "third hold" in captured.out
    assert "no limit" in captured.err
    assert len(sinces) == 3, "it held again after each empty hold"
    assert sinces == sorted(sinces), "each hold resumes from the cursor"


def test_forever_is_the_same_as_zero():
    assert decide._duration("forever") == 0


def test_json_prints_one_line_per_plain_wake(
    db, live, key_file, page, capsys, monkeypatch, brief_holds
):
    path, _ = key_file
    asked = _ask(db, page, "cove/1")
    _answer(db, asked, "cove note")
    assert _run(["wait", "--json", "--timeout", "5s"], path, monkeypatch) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    woke = json.loads(lines[0])
    assert woke["office"] == "cove"
    assert [a["id"] for a in woke["answers"]] == [asked.id]
    assert woke["answers"][0]["answer_note"] == "cove note"
    assert woke["threads"] == []


def test_an_endless_waiter_shows_its_own_held_back_answer_again_when_due(
    db, live, key_file, page, capsys, monkeypatch
):
    """`--timeout 0` never re-arms, so the hourly re-showing has to happen
    inside the wait: no hold may run past the moment it falls due."""
    path, _ = key_file
    monkeypatch.setattr(decide, "_RESURFACE_EVERY", 1.5)
    _answer(db, _ask(db, page, "cove/1"), "cove note")
    assert _run(["wait", "--timeout", "5s"], path, monkeypatch) == 0
    capsys.readouterr()
    started = time.monotonic()
    assert _run(["wait", "--timeout", "0"], path, monkeypatch) == 0
    assert 1 < time.monotonic() - started < 10
    assert "cove note" in capsys.readouterr().out
