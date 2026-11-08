"""An arc's numbers come from the tracker, and refuse to mislead.

homelab-ij8.69.2. The rules being tested are honesty rules, and they live in the
data rather than in whoever draws it — a renderer that has to remember them is a
renderer that will forget.
"""

from datetime import date

import pytest

from ream import beadmetrics


def row(rid, status, *, created, closed=None, updated=None, priority=2):
    return {
        "id": rid,
        "status": status,
        "priority": priority,
        "created_at": f"{created}T09:00:00+00:00",
        "closed_at": f"{closed}T09:00:00+00:00" if closed else None,
        "updated_at": f"{updated or closed or created}T09:00:00+00:00",
    }


@pytest.fixture()
def fake_bd(monkeypatch):
    """Replace the subprocess call, keyed by status the way bd actually behaves."""

    def install(rows):
        def _bd(repo, status):
            return [r for r in rows if r["status"] == status]

        monkeypatch.setattr(beadmetrics, "_bd", _bd)

    return install


TODAY = date(2026, 9, 20)


def test_in_progress_is_counted_and_is_not_open(fake_bd):
    """`bd list --status=open` does not include in_progress. A caller that asks
    only for open sees neither, which is how a status page ended up unable to
    show what was being worked on at all."""
    fake_bd(
        [
            row("a.1", "open", created="2026-09-01"),
            row("a.2", "in_progress", created="2026-09-01"),
            row("a.3", "closed", created="2026-09-01", closed="2026-09-19"),
        ]
    )
    out = beadmetrics.collect("/repo", "a.", today=TODAY)
    assert out["totals"] == {
        "all": 3,
        "done": 1,
        "open": 1,
        "in_progress": 1,
        "urgent_open": 0,
    }


def test_urgent_counts_live_work_only_and_includes_in_progress(fake_bd):
    fake_bd(
        [
            row("a.1", "open", created="2026-09-01", priority=1),
            row("a.2", "in_progress", created="2026-09-01", priority=0),
            row("a.3", "open", created="2026-09-01", priority=2),
            row("a.4", "closed", created="2026-09-01", closed="2026-09-19", priority=1),
        ]
    )
    out = beadmetrics.collect("/repo", "a.", today=TODAY)
    assert out["totals"]["urgent_open"] == 2, (
        "P0 and P1 that are still live, not the closed one"
    )


def test_a_young_arc_gets_no_chart_and_says_why(fake_bd):
    """The caller cannot draw a misleading chart because there is nothing to
    draw — a stronger guarantee than asking every caller to check an age first."""
    fake_bd(
        [
            row("a.1", "closed", created="2026-09-19", closed="2026-09-19"),
            row("a.2", "closed", created="2026-09-19", closed="2026-09-20"),
        ]
    )
    out = beadmetrics.collect("/repo", "a.", today=TODAY)
    assert out["arc_age_days"] == 1
    assert out["velocity"]["drawable"] is False
    assert "1 day old" in out["velocity"]["why"]
    assert "days" not in out["velocity"], "no series to accidentally plot"


def test_an_old_enough_arc_gets_a_real_series(fake_bd):
    rows = [row("a.0", "open", created="2026-08-01")]
    for n, day in enumerate(("2026-09-18", "2026-09-19", "2026-09-19", "2026-09-10")):
        rows.append(row(f"a.{n + 1}", "closed", created="2026-08-01", closed=day))
    fake_bd(rows)
    out = beadmetrics.collect("/repo", "a.", today=TODAY)
    v = out["velocity"]
    assert v["drawable"] is True
    assert v["window_days"] == 14
    assert len(v["days"]) == 14
    assert v["peak"] == 2, "two closed on the 19th"
    assert sum(d["closed"] for d in v["days"]) == 4
    assert v["days"][-1]["date"] == "2026-09-19", "the window ends yesterday, not today"


def test_an_old_arc_with_nothing_finished_still_refuses(fake_bd):
    """Fourteen empty bars is a chart that says 'this project is dead' by
    accident. Say it in words or not at all."""
    fake_bd(
        [
            row("a.1", "open", created="2026-01-01"),
            row("a.2", "closed", created="2026-01-01", closed="2026-02-01"),
        ]
    )
    out = beadmetrics.collect("/repo", "a.", today=TODAY)
    assert out["velocity"]["drawable"] is False
    assert "nothing has finished" in out["velocity"]["why"]


def test_a_field_with_no_source_is_absent_never_zero(fake_bd):
    """Zero is a measurement. "I could not tell" is not, and a tile reading 0 is
    indistinguishable from one reading nothing-happened."""
    fake_bd([{"id": "a.1", "status": "open", "priority": 2}])
    out = beadmetrics.collect("/repo", "a.", today=TODAY)
    assert "last_moved_days_ago" not in out
    assert "arc_age_days" not in out
    assert out["velocity"] is None


def test_an_unparseable_timestamp_is_not_counted_into_a_window(fake_bd):
    bad = row("a.1", "closed", created="2026-01-01", closed="2026-09-19")
    bad["closed_at"] = "not a date"
    fake_bd([bad])
    out = beadmetrics.collect("/repo", "a.", today=TODAY)
    assert out["windows"]["closed_this_week"]["value"] == 0


def test_only_the_prefix_is_counted(fake_bd):
    fake_bd(
        [
            row("homelab-ij8.1", "open", created="2026-09-01"),
            row("homelab-ij8.2", "open", created="2026-09-01"),
            row("homelab-tay.1", "open", created="2026-09-01"),
            row("homelab-ij8", "open", created="2026-09-01"),
        ]
    )
    out = beadmetrics.collect("/repo", "homelab-ij8.", today=TODAY)
    assert out["totals"]["all"] == 2, (
        "the epic itself has no trailing dot and is not a child"
    )


def test_a_prefix_that_matches_nothing_is_an_error_not_an_empty_arc(fake_bd):
    """Reporting zeroes would render as a project where nothing has happened,
    which is a claim. A typo must not be able to make that claim."""
    fake_bd([row("other.1", "open", created="2026-09-01")])
    with pytest.raises(beadmetrics.TrackerUnavailable, match="no beads matched"):
        beadmetrics.collect("/repo", "a.", today=TODAY)


def test_a_tracker_that_cannot_be_reached_raises(monkeypatch):
    def boom(repo, status):
        raise beadmetrics.TrackerUnavailable("bd is not installed")

    monkeypatch.setattr(beadmetrics, "_bd", boom)
    with pytest.raises(beadmetrics.TrackerUnavailable):
        beadmetrics.collect("/repo", "a.", today=TODAY)


def test_the_command_exits_non_zero_when_the_tracker_is_unavailable(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        beadmetrics,
        "_bd",
        lambda repo, status: (_ for _ in ()).throw(
            beadmetrics.TrackerUnavailable("nope")
        ),
    )
    code = beadmetrics.main(["--prefix", "a.", "--repo", "/repo"])
    assert code == 1
    assert "nope" in capsys.readouterr().err
