# tests/test_decide_review.py
"""`artifacts-decide ask-review` and `wait --review` (page review step 2)."""

import itertools
import json
import time
from datetime import UTC, datetime

import pytest
from test_client import key_file, live  # noqa: F401 -- fixtures

from ream import comments, decide, decisions, holds, store

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
    from ream import comments

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
    from ream import comments

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
    # --json is not here: a plain wait prints each wake as a JSON line too
    # (feedback, 6 Oct 2026), so a unit can append it to a mailbox.
    path, _ = key_file
    for flag in (
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
    from ream import comments

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
    monkeypatch.setattr(decide, "_UNAVAILABLE_PAUSE", 0.01)
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
    from ream import comments

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


@brief
def test_round_one_marks_do_not_wake_a_round_two_wait(live, key_file, db, capsys):
    """After changes requested and a republish, the marks he made in round 1
    are not new to round 2: the wait times out instead of exiting 7."""
    from ream import comments

    path, page, rid = _setup(db, key_file, capsys)
    comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="hm",
        kind="comment",
    )
    decisions.answer(db, rid, letter="c", by="jeremy")
    revision = store.get_owned(db, "ops-seat", page.id).revision
    store.patch(db, "ops-seat", page.id, PAGE.replace("90", "30"), revision)
    capsys.readouterr()
    assert (
        decide.main(
            ["--key-file", str(path), "wait", "--review", rid, "--timeout", "3s"]
        )
        == 3
    )


# ---- claims (page review step 5) ----


def _claim_state(db, rid):
    with db.connect() as conn:
        return conn.execute(
            "select claimed_by, claim_expires_at, released_reason from review_round"
            " where review_id = %s order by n desc limit 1",
            (rid,),
        ).fetchone()


def _held_by_other(db, rid, session="ops-seat/other"):
    claim = decisions.claim_round(db, "ops-seat", rid, session)
    until = (
        datetime.fromisoformat(claim.claim_expires_at)
        .astimezone(UTC)
        .strftime("%H:%M UTC")
    )
    return (
        f"CLAIMED by {session} until {until}: another session is acting on this round"
    )


def test_wait_claims_the_round_for_its_session(live, key_file, db, capsys):
    path, _page, rid = _setup(db, key_file, capsys)
    decisions.answer(db, rid, letter="c", by="jeremy")
    args = ["--key-file", str(path), "--session", "ops-seat/me"]
    assert decide.main([*args, "wait", "--review", rid, "--timeout", "5s"]) == 5
    assert _claim_state(db, rid)[0] == "ops-seat/me"


def test_wait_exits_eight_when_another_session_has_the_round(
    live, key_file, db, capsys
):
    path, _page, rid = _setup(db, key_file, capsys)
    # Claimed after the verdict: a verdict stretches a live lease, so the
    # line's time is the one the closed round's claim names.
    decisions.answer(db, rid, letter="c", by="jeremy")
    line = _held_by_other(db, rid)
    args = ["--key-file", str(path), "--session", "ops-seat/me"]
    started = time.monotonic()
    assert decide.main([*args, "wait", "--review", rid, "--timeout", "30s"]) == 8
    assert time.monotonic() - started < 10
    captured = capsys.readouterr()
    assert line in captured.err.splitlines()
    assert "Artifacts review:" not in captured.out, "the verdict is not this one's"
    assert _claim_state(db, rid)[0] == "ops-seat/other"


def test_require_approval_maps_claimed_to_one(live, key_file, db, capsys):
    path, _page, rid = _setup(db, key_file, capsys)
    _held_by_other(db, rid)
    args = ["--key-file", str(path), "--session", "ops-seat/me"]
    wait = ["wait", "--review", rid, "--timeout", "5s", "--require-approval"]
    assert decide.main([*args, *wait]) == 1


def test_the_same_session_waiting_again_reclaims_rather_than_exit_eight(
    live, key_file, db, capsys, monkeypatch
):
    path, _page, rid = _setup(db, key_file, capsys)
    monkeypatch.setenv("REAM_SESSION", "ops-seat/me")
    decisions.claim_round(db, "ops-seat", rid, "ops-seat/me")
    decisions.answer(db, rid, letter="a", by="jeremy")
    wait = ["--key-file", str(path), "wait", "--review", rid, "--timeout", "5s"]
    assert decide.main(wait) == 0
    assert _claim_state(db, rid)[0] == "ops-seat/me"


def test_wait_without_a_session_label_does_not_claim_and_says_so(
    live, key_file, db, capsys, monkeypatch
):
    monkeypatch.delenv("REAM_SESSION", raising=False)
    path, _page, rid = _setup(db, key_file, capsys)
    decisions.answer(db, rid, letter="c", by="jeremy")
    wait = ["--key-file", str(path), "wait", "--review", rid, "--timeout", "5s"]
    assert decide.main(wait) == 5
    err = capsys.readouterr().err.splitlines()
    assert "no session label: not claiming the round (set REAM_SESSION)" in err
    assert _claim_state(db, rid)[0] is None


@brief
@pytest.mark.slow(
    reason="a real 9 s wait: the lease must lapse twice over without renewal"
)
def test_wait_renews_its_lease_while_it_runs(live, key_file, db, capsys, monkeypatch):
    """A 4 s lease renewed when under 3.5 s remain, for a 9 s wait: without
    renewal it would lapse twice over, and another session could take it."""
    monkeypatch.setattr(decisions, "CLAIM_SECONDS", 4)
    monkeypatch.setattr(decide, "_RENEW_BELOW", 3.5)
    path, _page, rid = _setup(db, key_file, capsys)
    claims = []
    real = decide._request

    def counting(method, url, *a, **kw):
        if url.endswith(f"/reviews/{rid}/claim"):
            claims.append(time.monotonic())
        return real(method, url, *a, **kw)

    monkeypatch.setattr(decide, "_request", counting)
    args = ["--key-file", str(path), "--session", "ops-seat/me"]
    assert decide.main([*args, "wait", "--review", rid, "--timeout", "9s"]) == 3
    assert len(claims) >= 3, "claimed once and renewed at least twice"
    assert max(b - a for a, b in itertools.pairwise(claims)) < 3.5
    # Still held when the wait ends, though the first claim is 9 s old.
    with pytest.raises(decisions.Claimed):
        decisions.claim_round(db, "ops-seat", rid, "ops-seat/other")


def test_review_release_hands_the_round_back(live, key_file, db, capsys):
    path, _page, rid = _setup(db, key_file, capsys)
    decisions.claim_round(db, "ops-seat", rid, "ops-seat/me")
    args = ["--key-file", str(path), "--session", "ops-seat/me"]
    reason = "cannot deploy from this machine"
    assert decide.main([*args, "review-release", rid, "--reason", reason]) == 0
    assert capsys.readouterr().out == f"released {rid}: {reason}\n"
    assert _claim_state(db, rid) == (None, None, reason)


def test_review_release_of_another_sessions_round_exits_eight(
    live, key_file, db, capsys
):
    path, _page, rid = _setup(db, key_file, capsys)
    line = _held_by_other(db, rid)
    args = ["--key-file", str(path), "--session", "ops-seat/me"]
    assert decide.main([*args, "review-release", rid, "--reason", "mine now"]) == 8
    assert line in capsys.readouterr().err.splitlines()
    assert _claim_state(db, rid)[0] == "ops-seat/other"


def test_review_release_needs_a_session_label(live, key_file, db, capsys, monkeypatch):
    monkeypatch.delenv("REAM_SESSION", raising=False)
    path, _page, rid = _setup(db, key_file, capsys)
    assert (
        decide.main(["--key-file", str(path), "review-release", rid, "--reason", "x"])
        == 2
    )
    assert "REAM_SESSION" in capsys.readouterr().err


def test_a_lease_with_no_date_header_is_timed_on_the_cli_clock(monkeypatch):
    """No Date header: renew at send time + the lease the server named - the
    margin, on the CLI's own monotonic clock; no wall-clock comparison."""
    now = [1000.0]
    monkeypatch.setattr(decide, "_mono", lambda: now[0])
    body = {
        "claimed_by": "s",
        "claim_expires_at": "2000-01-01T00:00:00+00:00",
        "round": 1,
        "lease_seconds": 3600,
    }

    def answer(*a, **kw):
        now[0] += 2  # the request takes 2 s
        return dict(body)

    monkeypatch.setattr(decide, "_request", answer)
    lease = decide._Lease("http://x", "k", "r", "s")
    assert lease.claim() is None
    assert lease.renew_at == 1000.0 + 3600 - decide._RENEW_BELOW
    del body["lease_seconds"]
    now[0] = 5000.0
    assert lease.claim() is None
    assert lease.renew_at == 5000.0 + decide._LEASE_SECONDS - decide._RENEW_BELOW


@brief
def test_a_suspend_renews_before_the_next_poll_and_moves_the_deadline(
    live, key_file, db, capsys, monkeypatch
):
    """The wall clock jumps two minutes past the monotonic one (a laptop lid):
    the wait renews its claim at once and measures its deadline in wall time,
    so a 20 s wait that slept for two minutes ends now."""
    path, _page, rid = _setup(db, key_file, capsys)
    offset = [0.0]
    monkeypatch.setattr(decide, "_wall", lambda: time.time() + offset[0])
    claims = []
    real = decide._request

    def jumping(method, url, *a, **kw):
        if url.endswith(f"/reviews/{rid}/claim"):
            claims.append(offset[0])
        out = real(method, url, *a, **kw)
        if "wait=" in url:
            offset[0] = 120.0
        return out

    monkeypatch.setattr(decide, "_request", jumping)
    args = ["--key-file", str(path), "--session", "ops-seat/me"]
    started = time.monotonic()
    assert decide.main([*args, "wait", "--review", rid, "--timeout", "20s"]) == 3
    assert time.monotonic() - started < 10, "the deadline moved with the wall clock"
    assert claims == [0.0, 120.0], "claimed, then renewed after the suspend"


def test_exit_eight_with_json_prints_one_line_naming_the_holder(
    live, key_file, db, capsys
):
    path, _page, rid = _setup(db, key_file, capsys)
    _held_by_other(db, rid)
    args = ["--key-file", str(path), "--session", "ops-seat/me"]
    wait = ["wait", "--review", rid, "--timeout", "5s", "--json"]
    assert decide.main([*args, *wait]) == 8
    out = capsys.readouterr().out.strip()
    assert "\n" not in out
    data = json.loads(out)
    assert data["claimed_by"] == "ops-seat/other"
    assert data["round"] == 1
    assert datetime.fromisoformat(data["claim_expires_at"]) > datetime.now(UTC)


def test_reply_and_ack_reuse_a_given_idempotency_key(live, key_file, db, capsys):
    """A re-run after a timeout that DID commit, with the same key, writes
    nothing twice."""
    path, _ = key_file
    page = store.publish(db, "ops-seat", "Mine", PAGE)
    root = comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="hi"
    )
    row = decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=page.id,
        question="q?",
        options=[{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}],
    )
    args = ["--key-file", str(path)]
    for _ in range(2):
        assert (
            decide.main(
                [*args, "reply", root.id, "one", "--idempotency-key", "rerun-1"]
            )
            == 0
        )
        assert decide.main([*args, "ack", row.id, "--idempotency-key", "rerun-2"]) == 0
    with db.connect() as conn:
        replies = conn.execute(
            "select count(*) from comment where thread_root = %s", (root.id,)
        ).fetchone()[0]
    assert replies == 1


def test_a_review_wait_with_no_limit_waits_for_the_verdict(
    live, key_file, db, capsys, on_first_listen
):
    """`--timeout forever` used to end a review wait at once: its deadline was
    "now + 0". It now waits until the verdict comes."""
    path, _page, rid = _setup(db, key_file, capsys)
    listener = on_first_listen(
        lambda: decisions.answer(db, rid, letter="a", by="jeremy")
    )
    code = decide.main(
        ["--key-file", str(path), "wait", "--review", rid, "--timeout", "forever"]
    )
    listener.woke_the_first_hold()
    assert code == 0


def test_reply_on_another_offices_page_needs_page_and_lands(
    live, key_file, db, tmp_path, capsys
):
    """Another office's page: his thread is listed with a reply hint that names
    the page, `reply` without --page says to add it, and with it the reply lands
    as the agent's own (Jeremy, 21 Oct 2026)."""
    path, _token = key_file
    page = store.publish(
        db, "elsewhere", "Their page", "<!doctype html><body>hi</body></html>"
    )
    root = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="what about retries?",
    )
    base = ["--key-file", str(path)]
    assert decide.main([*base, "comments", "--page", page.id]) == 0
    assert f"--page {page.id}" in capsys.readouterr().out
    assert decide.main([*base, "reply", root.id, "three, capped"]) == 2
    assert "--page" in capsys.readouterr().err
    assert (
        decide.main([*base, "reply", root.id, "three, capped", "--page", page.id]) == 0
    )
    rows = comments.list_for_artifact(db, page.id)
    reply = [r for r in rows if r.thread_root == root.id]
    assert [(r.author_kind, r.author, r.body) for r in reply] == [
        ("agent", "ops-seat", "three, capped")
    ]
