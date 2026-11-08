"""An arc's real numbers, from the tracker, as JSON.

homelab-ij8.69.2. Jeremy, 20 Sep 2026, overruling a proposal to leave the metric
tiles off a status page: "agents here could have velocity and other stuff
because we track our work in beads and we could calculate that here and have
metrics etc." He was right and the argument against was wrong: the numbers exist
and are auditable, so the only real question is whether they are computed or
typed.

    A number an agent TYPES is a number an agent can get wrong.

That is the whole reason this exists. A wrong number on a status page is the
worst failure this service has, because the page reads as current either way --
the same reasoning that made the service stamp its own dates rather than let an
agent write one (homelab-ij8.70).

THE HONESTY RULES LIVE HERE, IN THE DATA, NOT IN WHOEVER DRAWS IT. A renderer
that has to remember them is a renderer that will forget:

  * A field that cannot be computed is ABSENT from the output, never zero.
    Zero is a measurement; "I could not tell" is not, and a tile reading 0 is
    indistinguishable from a tile reading nothing-happened.
  * `velocity` is null until the arc is old enough to have a shape. A two-bar
    chart implies a trend that is not there, and the caller cannot accidentally
    draw one because there is nothing to draw.
  * Every count says what window it covers, so a consumer cannot relabel it.

`bd` and the beads database are homelab-side while this package is reachable
from anywhere, so the repo is an argument (`--repo`, like `bd -C`) and a missing
workspace is an ERROR rather than an empty result. An empty result would render
as an arc where nothing has happened.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta

VELOCITY_DAYS = 14
VELOCITY_MIN_AGE_DAYS = 7
URGENT_MAX_PRIORITY = 1  # P0 and P1


class TrackerUnavailable(Exception):
    """`bd` could not be asked. Never silently an arc with no work in it."""


def _bd(repo: str, status: str) -> list[dict]:
    try:
        done = subprocess.run(  # noqa: S603
            # Resolved from PATH rather than pinned to a path: bd is installed
            # per-user (uv tool / ~/.local/bin) and an absolute path here would
            # be wrong on the next machine. A caller that cannot see bd gets a
            # loud TrackerUnavailable, which is the correct outcome anyway.
            ["bd", "-C", repo, "list", f"--status={status}", "--json"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TrackerUnavailable(f"could not run bd: {exc}") from exc
    if done.returncode != 0:
        raise TrackerUnavailable(
            f"bd -C {repo} list --status={status} exited {done.returncode}: "
            f"{done.stderr.strip()[:200]}"
        )
    try:
        return json.loads(done.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise TrackerUnavailable(f"bd did not return JSON: {exc}") from exc


def _day(stamp: str | None) -> date | None:
    """The calendar day of an ISO stamp in the local zone, or None.

    None rather than a guess: a row with an unparseable timestamp must not be
    counted into a window it may not belong to.
    """
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).astimezone().date()
    except ValueError:
        return None


def collect(repo: str, prefix: str, today: date | None = None) -> dict:
    """Every number a status page can honestly show for one arc.

    `today` is injectable so a test can pin the clock: a function that reads the
    wall clock internally can only be tested on the day it was written.
    """
    rows = [
        row
        for status in ("open", "in_progress", "closed")
        for row in _bd(repo, status)
        if row.get("id", "").startswith(prefix)
    ]
    if not rows:
        # A prefix that matches nothing is far more likely a typo than an arc
        # with no work in it, and reporting zeroes would render as the latter.
        raise TrackerUnavailable(f"no beads matched the prefix {prefix!r} in {repo}")

    today = today or datetime.now().astimezone().date()
    by_status = Counter(row.get("status", "") for row in rows)
    closed = [r for r in rows if r.get("status") == "closed"]
    live = [r for r in rows if r.get("status") != "closed"]

    out: dict = {
        "prefix": prefix,
        "repo": repo,
        "counted_for": today.isoformat(),
        "totals": {
            "all": len(rows),
            "done": len(closed),
            "open": by_status.get("open", 0),
            "in_progress": by_status.get("in_progress", 0),
            "urgent_open": sum(
                1 for r in live if r.get("priority", 2) <= URGENT_MAX_PRIORITY
            ),
        },
        "windows": {
            "closed_this_week": {
                "value": sum(
                    1
                    for r in closed
                    if (d := _day(r.get("closed_at"))) and d > today - timedelta(days=7)
                ),
                "window": "last 7 days",
            }
        },
    }

    # Only present when something actually carries a timestamp we trust.
    moved = [d for r in rows if (d := _day(r.get("updated_at")))]
    if moved:
        out["last_moved_days_ago"] = (today - max(moved)).days

    starts = [d for r in rows if (d := _day(r.get("created_at")))]
    age = (today - min(starts)).days if starts else None
    if age is not None:
        out["arc_age_days"] = age

    out["velocity"] = _velocity(closed, today, age)
    return out


def _velocity(closed: list[dict], today: date, age: int | None) -> dict | None:
    """Closed-per-day, or None with a reason a caller can print.

    None is the interesting case and it is deliberate: a caller cannot draw a
    misleading chart from data it does not have, which is a stronger guarantee
    than asking every caller to check an age field first.
    """
    if age is None:
        return None
    if age < VELOCITY_MIN_AGE_DAYS:
        return {
            "drawable": False,
            "why": (
                f"the project is {age} day{'' if age == 1 else 's'} old; a chart "
                f"needs {VELOCITY_MIN_AGE_DAYS} days behind it before a shape "
                "means anything"
            ),
        }
    days = [today - timedelta(days=n) for n in range(VELOCITY_DAYS, 0, -1)]
    per_day = dict.fromkeys(days, 0)
    for row in closed:
        day = _day(row.get("closed_at"))
        if day in per_day:
            per_day[day] += 1
    counts = [per_day[d] for d in days]
    if not any(counts):
        return {
            "drawable": False,
            "why": f"nothing has finished in the last {VELOCITY_DAYS} days",
        }
    return {
        "drawable": True,
        "window_days": VELOCITY_DAYS,
        "peak": max(counts),
        "quiet_days": sum(1 for n in counts if n == 0),
        "days": [
            {"date": d.isoformat(), "closed": n}
            for d, n in zip(days, counts, strict=True)
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="An arc's counts and velocity from the beads tracker, as JSON, for a page on Ream (formerly artifacts)."
    )
    parser.add_argument(
        "--prefix", required=True, help="bead id prefix, e.g. homelab-ij8."
    )
    parser.add_argument(
        "--repo", required=True, help="the repo holding the beads workspace"
    )
    args = parser.parse_args(argv)
    try:
        print(json.dumps(collect(args.repo, args.prefix), indent=2))
    except TrackerUnavailable as exc:
        # Loud and non-zero. A caller that cannot reach the tracker must publish
        # nothing rather than a page of zeroes.
        print(f"artifacts-bead-metrics: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
