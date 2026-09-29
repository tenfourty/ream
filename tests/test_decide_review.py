# tests/test_decide_review.py
"""`artifacts-decide ask-review` and `wait --review` (page review step 2)."""

import json
import time

import pytest
from test_client import key_file, live  # noqa: F401 -- fixtures

from artifacts import decide, decisions, store

PAGE = "<!doctype html><title>Plan</title><p>Rotate the keys every 90 days.</p>"


def _setup(db, key_file, capsys):
    path, _ = key_file
    page = store.publish(db, "ops-seat", "Plan", PAGE)
    assert decide.main(["--key-file", str(path), "ask-review", page.id]) == 0
    rid = capsys.readouterr().out.splitlines()[0].split()[0]
    return path, page, rid


@pytest.mark.parametrize("letter,code", [("a", 0), ("b", 4), ("c", 5), ("d", 6)])
def test_each_verdict_has_its_exit_code(live, key_file, db, capsys, letter, code):
    path, _page, rid = _setup(db, key_file, capsys)
    decisions.answer(db, rid, letter=letter, by="jeremy")
    assert (
        decide.main(
            ["--key-file", str(path), "wait", "--review", rid, "--timeout", "5s"]
        )
        == code
    )
    assert capsys.readouterr().out.startswith("Artifacts review:")


def test_a_verdict_given_before_the_wait_is_seen_at_once(live, key_file, db, capsys):
    path, _page, rid = _setup(db, key_file, capsys)
    decisions.answer(db, rid, letter="c", by="jeremy")

    t = time.monotonic()
    assert (
        decide.main(
            ["--key-file", str(path), "wait", "--review", rid, "--timeout", "30s"]
        )
        == 5
    )
    assert time.monotonic() - t < 10


def test_require_approval_collapses_to_zero_or_one(live, key_file, db, capsys):
    path, _page, rid = _setup(db, key_file, capsys)
    decisions.answer(db, rid, letter="b", by="jeremy")
    assert (
        decide.main(
            [
                "--key-file",
                str(path),
                "wait",
                "--review",
                rid,
                "--timeout",
                "5s",
                "--require-approval",
            ]
        )
        == 0
    )


def test_json_prints_the_data_on_one_line(live, key_file, db, capsys):
    path, _page, rid = _setup(db, key_file, capsys)
    decisions.answer(db, rid, letter="c", by="jeremy")
    assert (
        decide.main(
            [
                "--key-file",
                str(path),
                "wait",
                "--review",
                rid,
                "--timeout",
                "5s",
                "--json",
            ]
        )
        == 5
    )
    out = capsys.readouterr().out.strip()
    assert "\n" not in out and json.loads(out)["verdict"] == "changes_requested"


def test_result_file_gets_the_same_bytes_and_is_never_clobbered(
    live, key_file, db, capsys, tmp_path
):
    path, _page, rid = _setup(db, key_file, capsys)
    decisions.answer(db, rid, letter="a", by="jeremy")
    target = tmp_path / "verdict.txt"
    assert (
        decide.main(
            [
                "--key-file",
                str(path),
                "wait",
                "--review",
                rid,
                "--timeout",
                "5s",
                "--result-file",
                str(target),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert target.read_text() == out
    target.write_text("keep me")
    assert (
        decide.main(
            [
                "--key-file",
                str(path),
                "wait",
                "--review",
                rid,
                "--timeout",
                "5s",
                "--result-file",
                str(target),
            ]
        )
        == 2
    )
    assert target.read_text() == "keep me"


def test_new_marks_before_a_verdict_wake_with_seven(live, key_file, db, capsys):
    from artifacts import comments

    path, page, rid = _setup(db, key_file, capsys)
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="hm",
        kind="comment",
    )
    assert (
        decide.main(
            ["--key-file", str(path), "wait", "--review", rid, "--timeout", "5s"]
        )
        == 7
    )


def test_verdict_only_ignores_marks_and_times_out(live, key_file, db, capsys):
    from artifacts import comments

    path, page, rid = _setup(db, key_file, capsys)
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="hm",
        kind="comment",
    )
    assert (
        decide.main(
            [
                "--key-file",
                str(path),
                "wait",
                "--review",
                rid,
                "--timeout",
                "3s",
                "--verdict-only",
            ]
        )
        == 3
    )


def test_a_review_that_is_not_yours_is_refused(live, key_file, db, capsys):
    path, _ = key_file
    other = store.publish(db, "other", "Theirs", PAGE)
    rid = decisions.ask_review(db, agent="other", artifact_id=other.id).id
    assert (
        decide.main(
            ["--key-file", str(path), "wait", "--review", rid, "--timeout", "3s"]
        )
        == 1
    )


def test_result_flags_without_review_are_a_usage_error(live, key_file, capsys):
    path, _ = key_file
    for flag in (
        ["--json"],
        ["--require-approval"],
        ["--result-file", "x"],
        ["--verdict-only"],
    ):
        assert decide.main(["--key-file", str(path), "wait", *flag]) == 2
    assert "--review" in capsys.readouterr().err


def test_ask_review_prints_the_id_first_and_the_wait_hint(live, key_file, db, capsys):
    path, _ = key_file
    page = store.publish(db, "ops-seat", "Plan", PAGE)
    assert decide.main(["--key-file", str(path), "ask-review", page.id]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert f"review requested on {page.id}" in lines[0]
    assert (
        lines[1].strip()
        == f"wait: artifacts-decide wait --review {lines[0].split()[0]}"
    )


def test_ask_review_on_a_page_that_is_not_yours_is_refused(live, key_file, db):
    path, _ = key_file
    other = store.publish(db, "other", "Theirs", PAGE)
    assert decide.main(["--key-file", str(path), "ask-review", other.id]) == 1
