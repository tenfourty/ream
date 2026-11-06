"""Draw a status page from a seat's beads, so keeping it current is one command.

homelab-ij8.40. Jeremy, 19 Sep 2026: agents should update their page whenever
they update a bead -- and hand-editing HTML is the friction that makes them
stop. `artifacts-republish` already redraws a seat's pages after every bead
change; what a seat lacked was something to redraw FROM. This is it:

    artifacts-beads-page --parent homelab-08 --title "Warpgate"

prints a page on stdout, so it drops straight into a seat's
`~/.config/artifacts/pages.toml` as a `render` line:

    [[page]]
    id     = "<the page's id>"
    render = "artifacts-beads-page --parent homelab-08 --title Warpgate -C ~/repos/homelab"

WHAT IT SHOWS, and what it deliberately does not. Titles, states and counts --
never a description, a note or a design field, which can hold anything an
agent ever pasted into a tracker. And never a bead id: to Jeremy those are plan
coordinates ("ij8.71"), and the rule on every page he reads is outcomes. The
page says in its own footer that it was drawn from the tracker and when.

WHAT IT REFUSES TO DO: print a page it could not source. If `bd` fails or
answers with something that is not a list, this exits non-zero with nothing on
stdout, and `artifacts-republish` publishes nothing -- an empty board reads as
"the work stopped", which is worse than a stale one.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from artifacts import settings, templates, textvars

BD_ENV = "ARTIFACTS_BD"
RECENT_DAYS = 7
MAX_ROWS = 40

# bd's stored statuses, in the order a reader cares about, with the chip each
# one wears on the page (the templates' own done/now/next/held vocabulary).
_GROUPS = (
    ("in_progress", "now", "In progress"),
    ("blocked", "held", "Blocked"),
    ("open", "next", "Next"),
    ("deferred", "held", "Deferred"),
)


class Unsourced(Exception):
    """The tracker could not be read; nothing may be printed."""


def _bd(parent: str, directory: str | None) -> list[dict]:
    argv = [settings.current().bd_command]
    if directory:
        argv += ["-C", os.path.expanduser(directory)]
    argv += ["list", "--json", "--all", "--limit", "0", "--brief", "--parent", parent]
    try:
        done = subprocess.run(  # noqa: S603 -- argv list, no shell
            argv, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unsourced(f"bd could not run: {type(exc).__name__}") from exc
    if done.returncode != 0:
        raise Unsourced(f"bd exited {done.returncode}")
    try:
        rows = json.loads(done.stdout)
    except ValueError as exc:
        raise Unsourced("bd printed no JSON") from exc
    if not isinstance(rows, list):
        raise Unsourced("bd answered with something that is not a list")
    return [r for r in rows if isinstance(r, dict)]


def _when(row: dict) -> datetime | None:
    # When it closed, for a closed bead; otherwise when it last moved.
    raw = row.get("closed_at") or row.get("updated_at") or ""
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _title(row: dict) -> str:
    return html.escape(" ".join(str(row.get("title") or "untitled").split())[:200])


def _table(rows: list[dict], chip: str) -> str:
    out = []
    for row in rows[:MAX_ROWS]:
        out.append(
            f'    <tr><td class="s"><span class="state {chip}">{chip}</span></td>'
            f"<td>{_title(row)}</td></tr>"
        )
    if len(rows) > MAX_ROWS:
        out.append(
            f'    <tr><td class="s"></td><td>and {len(rows) - MAX_ROWS} more</td></tr>'
        )
    return "  <table>\n" + "\n".join(out) + "\n  </table>\n"


def render(rows: list[dict], title: str, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    by_status: dict[str, list[dict]] = {}
    for row in rows:
        by_status.setdefault(str(row.get("status") or "open"), []).append(row)
    for group in by_status.values():
        group.sort(key=lambda r: (r.get("priority", 9), str(r.get("title"))))
    closed = by_status.get("closed", [])
    recent = [
        r for r in closed if (_when(r) or now) >= now - timedelta(days=RECENT_DAYS)
    ]
    recent.sort(key=lambda r: _when(r) or now, reverse=True)
    still_open = len(rows) - len(closed)

    css, _etag = templates.shared_css()
    gallery_origin = textvars.gallery_origin(client=True)
    name = html.escape(title)
    tiles = [
        ("Done", len(closed), f"of {len(rows)}, all time"),
        ("Still open", still_open, "not yet closed"),
        ("In progress", len(by_status.get("in_progress", [])), "being worked on now"),
        ("Done this week", len(recent), f"closed in the last {RECENT_DAYS} days"),
    ]
    tile_html = "".join(
        f'    <div class="tile"><p class="k">{k}</p><p class="v">{v}</p>'
        f'<p class="w">{w}</p></div>\n'
        for k, v, w in tiles
    )
    sections = []
    for status, chip, heading in _GROUPS:
        group = by_status.get(status, [])
        if group:
            sections.append(f"  <h2>{heading}</h2>\n" + _table(group, chip))
    if recent:
        sections.append(
            f"  <h2>Done in the last {RECENT_DAYS} days</h2>\n" + _table(recent, "done")
        )
    if not rows:
        sections.append(
            '  <div class="card"><p>Nothing is tracked here yet.</p></div>\n'
        )
    drawn = now.strftime("%d %b %Y, %H:%M UTC")
    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>
{css}
</style>
</head>
<body>
<header class="masthead">
  <p class="topline"><a href="{gallery_origin}/" target="_blank" rel="noopener">Gallery</a></p>
  <p class="eyebrow">STATUS &middot; <!--artifacts:updated--></p>
  <h1>{name}</h1>
  <p class="standfirst">Where this work stands, drawn from the tracker every time a piece of it moves.</p>
  <p class="chips"><span class="chip">Updated <!--artifacts:updated--></span></p>
  <hr>
</header>
<main>
  <div class="tiles">
{tile_html}  </div>
{"".join(sections)}  <p class="w">Drawn from the tracker at {drawn}: titles and states only, nothing typed by hand.</p>
</main>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="artifacts-beads-page",
        description=(
            "Print a status page drawn from a seat's beads (the children of one "
            "parent). Put it in ~/.config/artifacts/pages.toml as a `render` "
            "line and artifacts-republish keeps the page current."
        ),
    )
    parser.add_argument(
        "--parent", required=True, help="the epic whose children the page reports"
    )
    parser.add_argument("--title", required=True, help="the page's name")
    parser.add_argument(
        "-C", dest="directory", metavar="DIR", help="the beads workspace (bd -C DIR)"
    )
    args = parser.parse_args(argv)
    try:
        rows = _bd(args.parent, args.directory)
    except Unsourced as exc:
        print(f"artifacts-beads-page: {exc}; printing nothing", file=sys.stderr)
        return 1
    sys.stdout.write(render(rows, args.title))
    return 0


def run() -> None:
    sys.exit(main())
