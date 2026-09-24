"""A status page drawn from a seat's beads (homelab-ij8.40)."""

import json
import stat
from datetime import UTC, datetime

from artifacts import beadpage, traps

NOW = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)
ROWS = [
    {
        "id": "x-1.1",
        "title": "The relay scans pages",
        "status": "in_progress",
        "priority": 1,
    },
    {"id": "x-1.2", "title": "Rotate keys <weekly>", "status": "open", "priority": 2},
    {"id": "x-1.3", "title": "Held on a question", "status": "blocked", "priority": 1},
    {
        "id": "x-1.4",
        "title": "Shipped yesterday",
        "status": "closed",
        "closed_at": "2026-09-23T10:00:00Z",
    },
    {
        "id": "x-1.5",
        "title": "Shipped last month",
        "status": "closed",
        "closed_at": "2026-08-01T10:00:00Z",
    },
]


def test_the_page_groups_by_state_and_counts_from_the_rows():
    page = beadpage.render(ROWS, "Relay", now=NOW)
    assert "<title>Relay</title>" in page
    values = [int(v) for v in __import__("re").findall(r'<p class="v">(\d+)', page)]
    assert values == [2, 3, 1, 1], "done, still open, in progress, done this week"
    assert page.index("In progress") < page.index("Blocked") < page.index("Next")
    assert "Shipped yesterday" in page and "Shipped last month" not in page


def test_no_bead_id_and_no_unescaped_title_reaches_the_page():
    page = beadpage.render(ROWS, "Relay", now=NOW)
    assert "x-1." not in page, "ids are plan coordinates to Jeremy"
    assert "&lt;weekly&gt;" in page and "<weekly>" not in page


def test_the_page_passes_the_check_it_will_face():
    assert traps.check(beadpage.render(ROWS, "Relay", now=NOW)) == []
    assert "<!--artifacts:updated-->" in beadpage.render(ROWS, "Relay", now=NOW)


def _fake_bd(tmp_path, monkeypatch, script):
    fake = tmp_path / "bd"
    fake.write_text("#!/bin/sh\n" + script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(beadpage.BD_ENV, str(fake))


def test_a_tracker_it_cannot_read_prints_nothing(tmp_path, monkeypatch, capsys):
    """An empty board reads as 'the work stopped'."""
    _fake_bd(tmp_path, monkeypatch, "exit 2\n")
    assert beadpage.main(["--parent", "x-1", "--title", "Relay"]) == 1
    out, err = capsys.readouterr()
    assert out == "" and "printing nothing" in err


def test_a_tracker_that_answers_prints_the_page(tmp_path, monkeypatch, capsys):
    _fake_bd(tmp_path, monkeypatch, f"cat <<'J'\n{json.dumps(ROWS)}\nJ\n")
    assert beadpage.main(["--parent", "x-1", "--title", "Relay"]) == 0
    assert capsys.readouterr().out.startswith("<!doctype html>")
