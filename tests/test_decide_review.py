# tests/test_decide_review.py
"""`artifacts-decide ask-review` and `wait --review` (page review step 2)."""

import json
import time

import pytest
from test_client import key_file, live  # noqa: F401 -- fixtures

from artifacts import decide, decisions, holds, store

PAGE = "<!doctype html><title>Plan</title><p>Rotate the keys every 90 days.</p>"

# A test that expects "nothing before the timeout" caps the server's holds at
# 0.2 s (conftest.brief_holds). The wake tests must not: see on_first_listen.
brief = pytest.mark.usefixtures("brief_holds")


def _setup(db, key_file, capsys):
    path, _ = key_file
    page = store.publish(db, "ops-seat", "Plan", PAGE)
    # These tests are about marks that arrive as he makes them; a held review
    # (the default) keeps them from the waiter until the verdict.
    with db.connect() as conn:
        holds.set_mode(conn, "live")
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


@brief
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
                "1.1s",
                "--verdict-only",
            ]
        )
        == 3
    )


@brief
def test_a_review_that_is_not_yours_is_refused(live, key_file, db, capsys):
    path, _ = key_file
    other = store.publish(db, "other", "Theirs", PAGE)
    rid = decisions.ask_review(db, agent="other", artifact_id=other.id).id
    assert (
        decide.main(
            ["--key-file", str(path), "wait", "--review", rid, "--timeout", "1.1s"]
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


def _mark(db, page):
    from artifacts import comments

    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="hm",
        kind="comment",
    )


def test_a_verdict_landing_between_the_look_and_the_listen_is_caught(
    live, key_file, db, capsys, monkeypatch
):
    path, _page, rid = _setup(db, key_file, capsys)
    real = decide._request
    fired = []

    def racing(method, url, *a, **kw):
        out = real(method, url, *a, **kw)
        if url.endswith(f"/reviews/{rid}") and not fired:
            fired.append(1)  # he answers just after the look returned "no verdict"
            decisions.answer(db, rid, letter="c", by="jeremy")
        return out

    monkeypatch.setattr(decide, "_request", racing)
    t = time.monotonic()
    code = decide.main(
        ["--key-file", str(path), "wait", "--review", rid, "--timeout", "20s"]
    )
    assert code == 5 and time.monotonic() - t < 10


def test_a_verdict_during_the_long_poll_is_caught(
    live, key_file, db, capsys, on_first_listen
):
    path, _page, rid = _setup(db, key_file, capsys)
    listener = on_first_listen(
        lambda: decisions.answer(db, rid, letter="d", by="jeremy")
    )
    t = time.monotonic()
    code = decide.main(
        ["--key-file", str(path), "wait", "--review", rid, "--timeout", "30s"]
    )
    listener.woke_the_first_hold()
    assert code == 6 and time.monotonic() - t < 10


def test_a_transient_failure_in_a_relook_does_not_end_the_wait(
    live, key_file, db, capsys, monkeypatch, on_first_listen
):
    path, _page, rid = _setup(db, key_file, capsys)
    monkeypatch.setattr(decide, "_RETRY_PAUSE", 0.01)
    listener = on_first_listen(
        lambda: decisions.answer(db, rid, letter="c", by="jeremy")
    )
    real = decide._request
    seen = []

    def flaky(method, url, *a, **kw):
        if url.endswith(f"/reviews/{rid}"):
            seen.append(1)
            if len(seen) == 2:
                raise decide.HTTPFailure(503, "blip")
        return real(method, url, *a, **kw)

    monkeypatch.setattr(decide, "_request", flaky)
    started = time.monotonic()
    code = decide.main(
        ["--key-file", str(path), "wait", "--review", rid, "--timeout", "30s"]
    )
    listener.woke_the_first_hold()
    assert code == 5 and len(seen) >= 3
    assert time.monotonic() - started < 10, "woken by the answer, not the timeout"


@pytest.mark.parametrize("letter", ["c", "d"])
def test_require_approval_maps_refusals_to_one(live, key_file, db, capsys, letter):
    path, _page, rid = _setup(db, key_file, capsys)
    decisions.answer(db, rid, letter=letter, by="jeremy")
    args = ["--key-file", str(path), "wait", "--review", rid, "--timeout", "5s"]
    assert decide.main([*args, "--require-approval"]) == 1


def test_require_approval_maps_new_marks_to_one(live, key_file, db, capsys):
    path, page, rid = _setup(db, key_file, capsys)
    _mark(db, page)
    args = ["--key-file", str(path), "wait", "--review", rid, "--timeout", "5s"]
    assert decide.main([*args, "--require-approval"]) == 1


def test_verdict_only_still_reports_a_verdict(live, key_file, db, capsys):
    path, page, rid = _setup(db, key_file, capsys)
    _mark(db, page)
    decisions.answer(db, rid, letter="b", by="jeremy")
    args = ["--key-file", str(path), "wait", "--review", rid, "--timeout", "5s"]
    assert decide.main([*args, "--verdict-only"]) == 4


@brief
def test_a_closed_review_does_not_count_older_marks_as_new(live, key_file, db, capsys):
    path, _ = key_file
    page = store.publish(db, "ops-seat", "Plan", PAGE)
    _mark(db, page)  # before the review was asked
    assert decide.main(["--key-file", str(path), "ask-review", page.id]) == 0
    rid = capsys.readouterr().out.splitlines()[0].split()[0]
    decisions.close(db, "ops-seat", rid)
    args = ["--key-file", str(path), "wait", "--review", rid, "--timeout", "1.1s"]
    assert decide.main(args) == 3


def test_a_result_file_in_a_missing_directory_is_refused_before_waiting(
    live, key_file, db, capsys, tmp_path
):
    path, _page, rid = _setup(db, key_file, capsys)
    target = tmp_path / "nope" / "v.txt"
    args = [
        "--key-file",
        str(path),
        "wait",
        "--review",
        rid,
        "--result-file",
        str(target),
    ]
    assert decide.main(args) == 2


@brief
def test_a_short_timeout_is_told_in_seconds(live, key_file, db, capsys):
    path, _page, rid = _setup(db, key_file, capsys)
    args = ["--key-file", str(path), "wait", "--review", rid, "--timeout", "1.1s"]
    assert decide.main(args) == 3
    err = capsys.readouterr().err
    assert "up to 1s" in err and "0m" not in err


def _reply_as_agent(live, path, page, rid_thread):
    assert (
        decide.main(
            ["--key-file", str(path), "reply", rid_thread, "seen"],
        )
        == 0
    )


@brief
def test_a_reply_clears_the_marks_and_the_verdict_still_comes(
    live, key_file, db, capsys
):
    from artifacts import comments

    path, page, rid = _setup(db, key_file, capsys)
    root = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="hm",
        kind="comment",
    )
    args = ["--key-file", str(path), "wait", "--review", rid, "--timeout", "1.1s"]
    assert decide.main(args) == 7
    assert decide.main(args) == 7  # no reply yet: still his word last
    capsys.readouterr()
    _reply_as_agent(live, path, page, root.id)
    assert decide.main(args) == 3  # the agent has the last word: nothing new
    decisions.answer(db, rid, letter="c", by="jeremy")
    capsys.readouterr()
    assert decide.main(args) == 5
    assert capsys.readouterr().out.startswith("Artifacts review:")


def _asked(path, page, *flags):
    return decide.main(["--key-file", str(path), "ask-review", page.id, *flags])


def test_ask_review_says_which_mode_it_got(live, key_file, db, capsys):
    path, _ = key_file
    with db.connect() as conn:
        holds.set_mode(conn, "held")
    p1 = store.publish(db, "ops-seat", "One", PAGE)
    assert _asked(path, p1, "--live") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith(f"review requested on {p1.id} (marks: live)")
    assert out[1].startswith("  wait: artifacts-decide wait --review ")
    p2 = store.publish(db, "ops-seat", "Two", PAGE)
    assert _asked(path, p2, "--hold") == 0
    assert "(marks: held)" in capsys.readouterr().out
    p3 = store.publish(db, "ops-seat", "Three", PAGE)
    assert _asked(path, p3) == 0
    assert "(marks: held)" in capsys.readouterr().out  # his preference


def test_live_and_hold_together_is_a_usage_error(live, key_file, db, capsys):
    path, _ = key_file
    page = store.publish(db, "ops-seat", "One", PAGE)
    with pytest.raises(SystemExit) as exc:
        _asked(path, page, "--live", "--hold")
    assert exc.value.code == 2


@brief
def test_a_held_mark_does_not_wake_the_wait_until_it_is_released(
    live, key_file, db, capsys
):
    path, _ = key_file
    page = store.publish(db, "ops-seat", "Held", PAGE)
    assert _asked(path, page, "--hold") == 0
    rid = capsys.readouterr().out.splitlines()[0].split()[0]
    _mark(db, page)
    wait = ["--key-file", str(path), "wait", "--review", rid, "--timeout", "1.1s"]
    assert decide.main(wait) == 3
    assert "hm" not in capsys.readouterr().out
    holds.set_review_mode(db, rid, "live")
    assert decide.main(wait) == 7
    assert "hm" in capsys.readouterr().out


def test_an_older_server_that_names_no_mode_is_said_plainly(
    live, key_file, monkeypatch, capsys
):
    """A server from before hold/live ignores --live and returns no mode: say
    so, never print "None" (step 3 task review, 29 Sep 2026)."""
    path, _ = key_file
    monkeypatch.setattr(
        decide, "_request", lambda *a, **k: {"id": "r" * 26, "kind": "review"}
    )
    assert decide.main(["--key-file", str(path), "ask-review", "p" * 26, "--live"]) == 0
    out = capsys.readouterr().out
    assert "None" not in out
    assert "(marks: unknown, the server predates hold/live)" in out
