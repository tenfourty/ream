"""The programme board renders from the tracker, and refuses to draw without it."""

import importlib.util
import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "board_tracker.json").read_text())


def _load():
    spec = importlib.util.spec_from_file_location(
        "board_render", ROOT / "scripts/board/render.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def board(monkeypatch):
    mod = _load()
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[0] == "artifacts-decide":
            return subprocess.CompletedProcess(argv, 0, FIXTURE["decide"], "")
        assert argv[:3] == ["bd", "-C", mod.BEADS_REPO] and argv[-1] == "--json"
        key = " ".join(argv[3:-1])
        if key not in FIXTURE:
            return subprocess.CompletedProcess(argv, 1, "", f"no fixture for {key}")
        return subprocess.CompletedProcess(argv, 0, json.dumps(FIXTURE[key]), "")

    monkeypatch.setattr(mod, "_run", fake_run)
    mod.calls = calls
    return mod


def _page(mod):
    now = datetime(2026, 10, 6, 13, 5, tzinfo=mod.PARIS)
    return mod.render(mod.load_tracker(), mod.load_decisions(), now)


def test_header_summary_and_stat_strip(board):
    page = _page(board)
    assert "RUNCOVE-5T3OJ · Programme board" in page and "<h1>Artifacts</h1>" in page
    assert "Pages any agent can publish for Jeremy to read and answer on." in page
    assert "two lanes moving, nothing stuck." in page and "OLD HEALTH LINE" not in page
    # Blocks .1 .2 .3.1-.3.3 .4: 6 items, 13 pts; done is .1 (3) and .3.1 (2).
    assert "2 <small>of 6 items · 5 of 13 pts · 38%</small>" in page
    assert 'Ready</span><span class="sv ready">0' in page  # .3.3 is blocked by .3.2
    assert 'Needs you</span><span class="sv needs">2' in page
    assert "2026-10-06 13:05 CEST" in page
    assert 'class="pbar"' in page and "f-blocked" in page and "points, of 13" in page


def test_needs_you_and_in_flight(board):
    page = _page(board)
    assert "Review Template — implementation plan" in page
    assert "on Template plan" in page and "Which colour for blocked lanes?" in page
    assert "the stat strip and lanes render from the tracker" in page
    assert "OLDER UPDATE" not in page.split('id="flight-h"')[1].split("</section>")[0]


def test_lanes_and_tables(board):
    page = _page(board)
    for phase in ("Brainstorm", "Spec", "Plan", "Implement", "Wrap"):
        assert f">{phase}<span" in page
    assert page.count('<article class="lane') == 4
    # Outcome language: the conventional-commit prefix is gone from every title.
    assert "feat:" not in page and "chore:" not in page
    assert "<h3>Page formats</h3>" in page
    # The lane with children draws one block per child, sized by points.
    lane3 = page.split("<h3>Page formats</h3>")[1].split("</article>")[0]
    assert lane3.count('class="blk ') == 3
    assert "b-done" in lane3 and "b-blocked" in lane3 and "b-queued" in lane3
    assert "flex-grow:2.00" in lane3
    assert "<b>Now</b>the stat strip and lanes render" in page
    assert page.count('<section class="lsec">') == 4
    assert '<span class="bid">runcove-5t3oj.3.2</span>' in page
    assert "What works today" in page and "Latest" in page


def test_unknown_decisions_never_read_as_nothing_waiting(board, monkeypatch):
    real = board._run

    def failing(argv, **kw):
        if argv[0] == "artifacts-decide":
            return subprocess.CompletedProcess(argv, 1, "", "no key")
        return real(argv, **kw)

    monkeypatch.setattr(board, "_run", failing)
    page = _page(board)
    assert "Nothing is waiting on you" not in page
    assert "could not check" in page


def test_tracker_failure_fails_closed(board, monkeypatch, capsys):
    real = board._run

    def broken(argv, **kw):
        if argv[:4] == ["bd", "-C", board.BEADS_REPO, "list"]:
            return subprocess.CompletedProcess(argv, 1, "", "database locked")
        return real(argv, **kw)

    monkeypatch.setattr(board, "_run", broken)
    assert board.main(["render.py", "-"]) == 2
    out = capsys.readouterr()
    assert out.out == "" and "database locked" in out.err


def test_no_lanes_is_a_failure_not_an_empty_board(board, monkeypatch, capsys):
    monkeypatch.setitem(FIXTURE, "list --all --parent runcove-5t3oj", [])
    assert board.main(["render.py", "-"]) == 2
    assert capsys.readouterr().out == ""


def test_points_and_titles(board):
    assert board.points({"estimated_minutes": 90}) == 1.5
    assert board.points({}) == 1.0
    assert board.outcome("feat(board): lanes for the flow") == "Lanes for the flow"
