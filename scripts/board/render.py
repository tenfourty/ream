#!/usr/bin/env python3
"""Render the artifacts programme board from the runcove tracker, not from memory.

Jeremy asked on 6 Oct 2026 for the artifacts status board to be redrawn as a
programme board, like the Agent SDK and "cove on a Mac" boards: headline numbers,
one progress bar, what needs him beside what is in flight, one lane per child of
the epic through the feature flow, and a table per lane.

Sources, read at render time:
  * the epic and its children (the lanes), and each lane's children (its blocks),
    from `bd -C ~/repos/runcove` -- READ ONLY;
  * `update:` comments on claimed issues (the NOW lines and "In flight now") and
    `health:` comments on the epic (the summary's second line);
  * the questions and reviews waiting on Jeremy, from
    `artifacts-decide list --open-only`, run with whatever key the caller holds
    (`artifacts-republish` hands each render one on $REAM_KEY_FD).

FAIL CLOSED. If the tracker cannot be read the render exits non-zero and writes
nothing to stdout: a board that prints "nothing in flight" because `bd` failed is
the same lie as a wait loop reporting silence it never waited for. The decisions
read is softer, as on the old board: if it fails, "Needs you" says it could not
check, never "nothing is waiting".

Usage:  render.py -          the page to stdout and nothing else (for republish)
        render.py <file>     the page to a file
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
CARRIED = HERE / "carried.html"
EPIC = os.environ.get("BOARD_EPIC", "runcove-5t3oj")
BEADS_REPO = os.environ.get("BOARD_BEADS_REPO", str(Path.home() / "repos" / "runcove"))
GALLERY = (
    os.environ.get("REAM_GALLERY")
    or os.environ.get("ARTIFACTS_GALLERY")
    or "https://artifacts.tenfourty.site"
)
PARIS = ZoneInfo("Europe/Paris")

PHASES = ["brainstorm", "spec", "plan", "implement", "wrap"]

# board class -> (legend word, lane pill word, pill class)
KINDS = {
    "done": ("Done", "done", "t-landed"),
    "progress": ("In progress", "building", "t-review"),
    "ready": ("Ready to pick up", "ready", "t-ready"),
    "queued": ("Queued", "queued", "t-queued"),
    "blocked": ("Blocked", "blocked", "t-blocked"),
    "backlog": ("Backlog", "backlog", "t-queued"),
}
ORDER = ["done", "progress", "ready", "queued", "blocked", "backlog"]


class TrackerError(RuntimeError):
    """The tracker could not be read; the render must not continue."""


# --- reading -----------------------------------------------------------------


def _run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    """The one place a process is started, so a test can stand in for it."""
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell
        argv, capture_output=True, text=True, timeout=60, check=False, **kw
    )


def _bd(*args: str):
    argv = ["bd", "-C", BEADS_REPO, *args, "--json"]
    try:
        proc = _run(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TrackerError(f"`bd {' '.join(args)}` could not run: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no stderr"]
        raise TrackerError(f"`bd {' '.join(args)}` exited {proc.returncode}: {tail[0]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise TrackerError(f"`bd {' '.join(args)}` did not return JSON") from exc


def _comments(row: dict) -> list[dict]:
    if not row.get("comment_count"):
        return []
    got = _bd("comments", row["id"])
    if not isinstance(got, list):
        raise TrackerError(f"comments for {row['id']} were not a list")
    return got


def load_tracker() -> dict:
    shown = _bd("show", EPIC)
    epic = shown[0] if isinstance(shown, list) and shown else shown
    if not isinstance(epic, dict) or epic.get("id") != EPIC:
        raise TrackerError(f"`bd show {EPIC}` did not return the epic")
    epic["comments"] = _bd("comments", EPIC) or []
    lanes = _bd("list", "--all", "--parent", EPIC)
    if not isinstance(lanes, list) or not lanes:
        # An epic with no lanes is not an empty programme; it is a bad read.
        raise TrackerError(f"{EPIC} has no children -- refusing to draw an empty board")
    for lane in lanes:
        lane["comments"] = _comments(lane)
        kids = _bd("list", "--all", "--parent", lane["id"]) or []
        for kid in kids:
            kid["comments"] = _comments(kid)
        lane["kids"] = sorted(kids, key=_id_key)
    return {"epic": epic, "lanes": sorted(lanes, key=_id_key)}


_DECISION_HEAD = re.compile(r"^([a-z0-9]{20,32})  (.+)$")
_ON_PAGE = re.compile(r'^\s+on "(.*?)"')


def load_decisions() -> list[dict] | None:
    """Open questions and reviews waiting on Jeremy, or None if unknown.

    None and [] differ on purpose: "nothing is waiting on you" is a claim, and a
    page that prints it because the call failed is lying.
    """
    kw = {}
    fd = os.environ.get("REAM_KEY_FD") or os.environ.get("ARTIFACTS_KEY_FD")
    if fd and fd.isdigit():
        kw["pass_fds"] = (int(fd),)
    try:
        proc = _run(["artifacts-decide", "list", "--open-only", "--json"], **kw)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        rows = json.loads(proc.stdout)
    except ValueError:
        return None  # output we could not read at all is unknown, not empty
    if not isinstance(rows, list):
        return None
    return [
        {
            "id": r.get("id", ""),
            "question": r.get("question", ""),
            "page": r.get("artifact_title"),
            "artifact_id": r.get("artifact_id"),
        }
        for r in rows
    ]


# --- shaping -----------------------------------------------------------------


def _id_key(row: dict) -> list:
    return [int(p) if p.isdigit() else p for p in re.split(r"[.-]", row.get("id", ""))]


def points(row: dict) -> float:
    minutes = row.get("estimated_minutes")
    return minutes / 60 if isinstance(minutes, (int, float)) and minutes > 0 else 1.0


def _fmt_pts(value: float) -> str:
    return f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"


def _open_blockers(row: dict) -> bool:
    for dep in row.get("dependencies") or []:
        if not isinstance(dep, dict):
            continue
        kind = dep.get("dependency_type") or dep.get("type")
        if kind == "blocks" and dep.get("status") != "closed":
            return True
    return False


def kind_of(row: dict) -> str:
    status = row.get("status")
    if status == "closed":
        return "done"
    if status == "in_progress":
        return "progress"
    if status == "blocked":
        return "blocked"
    if status in ("backlog", "deferred"):
        return "backlog"
    return "queued" if _open_blockers(row) else "ready"


_PREFIX = re.compile(
    r"^(feat|chore|fix|docs|refactor|test|ci|perf|build)(\([^)]*\))?!?:\s*",
    re.IGNORECASE,
)


def outcome(title: str) -> str:
    text = _PREFIX.sub("", title or "").strip()
    text = re.sub(r"^EPIC:\s*", "", text)
    return text[:1].upper() + text[1:]


def _tagged(comments: list[dict], tag: str) -> list[dict]:
    hits = [
        c
        for c in comments
        if isinstance(c, dict)
        and (c.get("text") or "").lstrip().lower().startswith(tag + ":")
    ]
    return sorted(hits, key=lambda c: c.get("created_at") or "")


def newest(rows: list[dict], tag: str = "update") -> dict | None:
    hits = [dict(c, _on=r) for r in rows for c in _tagged(r.get("comments") or [], tag)]
    return max(hits, key=lambda c: c.get("created_at") or "") if hits else None


def _body(comment: dict, tag: str = "update") -> str:
    return re.sub(
        rf"^\s*{tag}:\s*", "", comment.get("text") or "", flags=re.IGNORECASE
    ).strip()


def _when(stamp: str | None) -> str:
    if not stamp:
        return ""
    try:
        moment = datetime.fromisoformat(stamp).astimezone(PARIS)
    except ValueError:
        return ""
    return f"{moment.day} {moment:%b %H:%M}"


def lane_blocks(lane: dict) -> list[dict]:
    return lane["kids"] or [lane]


def lane_kind(lane: dict) -> str:
    if not lane["kids"]:
        return kind_of(lane)
    kinds = {kind_of(k) for k in lane["kids"]}
    if kinds == {"done"}:
        return "done"
    for k in ("blocked", "progress", "ready", "queued"):
        if k in kinds:
            return k
    return "backlog"


def lane_phase(lane: dict) -> str | None:
    """The column a lane is in: a `phase:<name>` label wins, else its state."""
    for label in lane.get("labels") or []:
        if label.startswith("phase:") and label[6:] in PHASES:
            return label[6:]
    kind = lane_kind(lane)
    if kind == "done":
        return "done"
    return "implement" if kind == "progress" else None


def summarise(tracker: dict) -> dict:
    blocks = [b for lane in tracker["lanes"] for b in lane_blocks(lane)]
    pts = {k: 0.0 for k in ORDER}
    count = {k: 0 for k in ORDER}
    for b in blocks:
        pts[kind_of(b)] += points(b)
        count[kind_of(b)] += 1
    return {
        "blocks": blocks,
        "pts": pts,
        "count": count,
        "total_pts": sum(pts.values()),
        "total": len(blocks),
    }


# --- drawing -----------------------------------------------------------------

e = html.escape


def _pct(part: float, whole: float) -> int:
    return round(100 * part / whole) if whole else 0


def _summary_lines(tracker: dict, s: dict) -> tuple[str, str]:
    desc = (tracker["epic"].get("description") or "").strip().splitlines()
    first = desc[0] if desc else outcome(tracker["epic"].get("title", ""))
    first = re.split(r"(?<=[.;])\s", first, maxsplit=1)[0].rstrip(";")
    if not first.endswith("."):
        first += "."
    health = newest([tracker["epic"]], "health")
    if health:
        second = _body(health, "health")
    else:
        c = s["count"]
        second = (
            f"{c['done']} of {s['total']} pieces of work are done, {c['progress']} in progress"
            f" and {c['ready']} ready to pick up."
        )
    return first, second


def _stat_strip(s: dict, decisions: list[dict] | None, now: datetime) -> str:
    c, p = s["count"], s["pts"]
    needs = "?" if decisions is None else str(len(decisions))
    needs_note = "could not check" if decisions is None else "open"
    return (
        '<div class="summary" role="group" aria-label="Programme summary">'
        f'<div><span class="sk">Done</span><span class="sv">{c["done"]} <small>of {s["total"]} items'
        f" · {_fmt_pts(p['done'])} of {_fmt_pts(s['total_pts'])} pts"
        f" · {_pct(p['done'], s['total_pts'])}%</small></span></div>"
        f'<div><span class="sk">In progress</span><span class="sv review">{c["progress"]}'
        " <small>items</small></span></div>"
        f'<div><span class="sk">Ready</span><span class="sv ready">{c["ready"]}'
        " <small>items</small></span></div>"
        f'<div><span class="sk">Needs you</span><span class="sv needs">{needs}'
        f" <small>{needs_note}</small></span></div>"
        f'<div><span class="sk">Updated</span><span class="sv time">{now:%Y-%m-%d %H:%M %Z}'
        "</span></div></div>"
    )


def _progress(s: dict) -> str:
    bar = "".join(
        f'<i class="f-{k}" style="flex:{s["pts"][k]:.2f}" title="{KINDS[k][0]}: '
        f'{_fmt_pts(s["pts"][k])} pts"></i>'
        for k in ORDER
        if s["pts"][k]
    )
    legend = "".join(
        f'<span><b class="f-{k}"></b>{KINDS[k][0]} {_fmt_pts(s["pts"][k])}</span>'
        for k in ORDER
    )
    return (
        f'<div class="pwrap"><div class="pbar" role="img" aria-label="Progress by points">{bar}</div>'
        f'<div class="plegend">{legend}<span class="pts">points, of '
        f"{_fmt_pts(s['total_pts'])}</span></div></div>"
    )


def _plain_question(q: str) -> str:
    m = re.match(r"Review requested:\s*(.+)", q)
    return f"Review {m.group(1)}" if m else q


def _link(d: dict) -> str:
    """The question's own page in the gallery, or the gallery when unknown."""
    aid = d.get("artifact_id") or ""
    return f"{GALLERY}/a/{aid}" if re.fullmatch(r"[a-z0-9]{26}", aid) else f"{GALLERY}/"


def _needs_box(decisions: list[dict] | None) -> str:
    if decisions is None:
        sub, items = (
            "unknown",
            (
                '<li class="empty">I could not check what is waiting on you when this page'
                " was built, so this is missing rather than empty: treat it as unknown.</li>"
            ),
        )
    elif not decisions:
        sub, items = "0 open", '<li class="empty">Nothing is waiting on you.</li>'
    else:
        sub = f"{len(decisions)} open"
        items = "".join(
            f'<li><a href="{e(_link(d))}" target="_blank" rel="noopener">'
            f"{e(_plain_question(d['question']))}</a>"
            + (f'<span class="when">on {e(d["page"])}</span>' if d.get("page") else "")
            + f'<span class="bid">{e(d["id"])}</span></li>'
            for d in decisions
        )
    return (
        '<section class="box needs" aria-labelledby="needs-h"><h2 id="needs-h">Needs you'
        f'<span class="sub">{sub}</span></h2><ol>{items}</ol></section>'
    )


def _flight_box(tracker: dict) -> str:
    rows = [
        b
        for lane in tracker["lanes"]
        for b in lane_blocks(lane)
        if kind_of(b) == "progress"
    ]
    items = []
    for row in rows:
        latest = newest([row])
        text = _body(latest) if latest else "Claimed; no update posted yet."
        who = row.get("assignee") or "unclaimed"
        when = _when(latest.get("created_at")) if latest else ""
        items.append(
            f"<li><b>{e(outcome(row.get('title', '')))}</b> {e(text)}"
            f'<span class="when">{e(who)}{" · " + e(when) if when else ""}'
            f' · <span class="bid">{e(row["id"])}</span></span></li>'
        )
    body = (
        "".join(items) or '<li class="empty">Nothing is being worked on right now.</li>'
    )
    return (
        '<section class="box flight" aria-labelledby="flight-h"><h2 id="flight-h">In flight now'
        f'<span class="sub">{len(rows)} claimed</span></h2><ul>{body}</ul></section>'
    )


def _block(b: dict, lane: dict) -> str:
    kind = kind_of(b)
    if b is lane:
        label = KINDS[kind][1] if kind != "progress" else "in progress"
    else:
        label = b["id"].removeprefix(lane["id"])
    tip = f"{outcome(b.get('title', ''))} · {_fmt_pts(points(b))} pts · {KINDS[kind][0]} · {b['id']}"
    return (
        f'<span class="blk b-{kind}" style="flex-grow:{points(b):.2f}" title="{e(tip)}">'
        f"<span>{e(label)}</span></span>"
    )


def _lane(lane: dict) -> str:
    blocks = lane_blocks(lane)
    kind, phase = lane_kind(lane), lane_phase(lane)
    total = sum(points(b) for b in blocks)
    done = sum(points(b) for b in blocks if kind_of(b) == "done")
    moving = sum(1 for b in blocks if kind_of(b) == "progress")
    title = outcome(lane.get("title", ""))
    cells = []
    current = (
        PHASES.index(phase)
        if phase in PHASES
        else (len(PHASES) if phase == "done" else -1)
    )
    for i, name in enumerate(PHASES):
        state = "done" if i < current else ("now" if i == current else "todo")
        mark = {"done": "done", "now": "now", "todo": ""}[state]
        extra = ""
        if name == "implement":
            n_done = sum(1 for b in blocks if kind_of(b) == "done")
            extra = f'<span class="phc">{n_done}/{len(blocks)}</span>'
        cells.append(
            f'<div class="ph ph-{name} {state}" title="{name.title()}: {state}">'
            f'<span class="phl">{name.title()}</span>{extra}'
            f'<span class="phm" aria-hidden="true">{mark}</span></div>'
        )
    latest = newest([lane, *lane["kids"]])
    now_line = e(_body(latest)) if latest else "&mdash;"
    who = lane.get("assignee")
    meta = f"{_fmt_pts(done)} of {_fmt_pts(total)} pts done · {moving} in progress" + (
        f" · {e(who)}" if who else ""
    )
    strip = "".join(_block(b, lane) for b in blocks)
    return (
        f'<article class="lane k-{kind}"><div class="lhead"><h3>{e(title)}</h3>'
        f'<div class="lstat"><span class="lpct">{_pct(done, total)}<small>%</small></span>'
        f'<span class="tag {KINDS[kind][2]}">{KINDS[kind][1]}</span></div>'
        f'<p class="lmeta">{meta} <span class="bid">{e(lane["id"])}</span></p></div>'
        + "".join(cells)
        + f'<div class="strip" aria-label="{e(title)}, by points">{strip}</div>'
        f'<p class="lnow"><b>Now</b>{now_line}</p></article>'
    )


def _lanes(tracker: dict, s: dict) -> str:
    per_phase = {p: 0 for p in PHASES}
    for lane in tracker["lanes"]:
        ph = lane_phase(lane)
        ph = "wrap" if ph == "done" else ph
        if ph in per_phase:
            per_phase[ph] += 1
    head = '<div class="grid-head" aria-hidden="true"><div>Lane</div>' + "".join(
        f'<div class="{"cur" if p == "implement" else ""}">{p.title()}'
        f'<span class="arrow">{per_phase[p]} lane{"" if per_phase[p] == 1 else "s"}</span></div>'
        for p in PHASES
    )
    legend = "".join(
        f'<li><span class="sw b-{k}" aria-hidden="true"></span>{KINDS[k][0]} '
        f"<b>{s['count'][k]}</b></li>"
        for k in ORDER
    )
    return (
        '<section class="prog" aria-labelledby="prog-h"><div class="prog-head">'
        f'<h2 id="prog-h">Lanes through the feature flow</h2><ul class="legend">{legend}</ul></div>'
        + head
        + "</div>"
        + "".join(_lane(lane) for lane in tracker["lanes"])
        + '<p class="note">One lane per piece of the programme. Blocks are sized by points'
        " (an hour of estimate is a point; an unsized item counts as one). Built from the"
        " runcove tracker each time the page is drawn, by scripts/board/render.py in"
        " runcove/artifacts (the homelab's old scripts/artifacts-status.py is retired).</p>"
        "</section>"
    )


def _tables(tracker: dict) -> str:
    out = []
    for lane in tracker["lanes"]:
        blocks = lane_blocks(lane)
        n_done = sum(1 for b in blocks if kind_of(b) == "done")
        rows = []
        for b in blocks:
            latest = newest([b])
            note = e(_body(latest)) if latest else '<span class="none">&mdash;</span>'
            st = b.get("status", "")
            rows.append(
                f'<tr class="r-{kind_of(b)}"><td class="title">{e(outcome(b.get("title", "")))}'
                f'<span class="bid">{e(b["id"])}</span></td>'
                f'<td class="stc"><span class="st {e(st)}">{e(st.replace("_", " "))}</span></td>'
                f'<td class="notec">{note}</td></tr>'
            )
        out.append(
            f'<section class="lsec"><h2>{e(outcome(lane.get("title", "")))}'
            f' <span class="sub">{e(lane["id"])} · {n_done}/{len(blocks)} done</span></h2>'
            '<div class="twrap"><table><thead><tr><th>What</th><th>Status</th><th>Note</th>'
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>"
        )
    return '<div class="lanes">' + "".join(out) + "</div>"


def render(
    tracker: dict,
    decisions: list[dict] | None,
    now: datetime,
    carried: str | None = None,
) -> str:
    s = summarise(tracker)
    first, second = _summary_lines(tracker, s)
    if carried is None:
        carried = CARRIED.read_text()
    body = (
        '<header class="top"><div>'
        f'<div class="eyebrow">{e(EPIC.upper())} · Programme board</div><h1>Artifacts</h1>'
        f'<p class="lede">{e(first)}<br>{e(second)}</p></div>'
        + _stat_strip(s, decisions, now)
        + "</header>"
        + _progress(s)
        + f'<div class="duo">{_needs_box(decisions)}{_flight_box(tracker)}</div>'
        + _lanes(tracker, s)
        + _tables(tracker)
        + f'<div class="duo carried">{carried}</div>'
    )
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Artifacts programme board</title><style>{CSS}</style></head>"
        f"<body><main>{body}</main></body></html>\n"
    )


def main(argv: list[str]) -> int:
    try:
        tracker = load_tracker()
    except TrackerError as exc:
        print(
            f"{Path(argv[0]).name}: cannot read the tracker, no page drawn: {exc}",
            file=sys.stderr,
        )
        return 2
    page = render(tracker, load_decisions(), datetime.now(PARIS))
    if len(argv) > 1 and argv[1] != "-":
        Path(argv[1]).write_text(page)
        print(f"wrote {argv[1]} ({len(page)} bytes)", file=sys.stderr)
    else:
        # The page and NOTHING else on stdout: artifacts-republish publishes it.
        sys.stdout.write(page)
    return 0


CSS = """
:root{
  --bg:#edf0f1;--panel:#fbfcfc;--sunk:#e3e8ea;--ink:#13202a;--muted:#566571;--line:#d3dade;
  --accent:#12626d;--accent-soft:#d5e9eb;
  --landed:#2b8650;--ready:#c7860a;--review:#3b72c4;--queued:#8e9ca6;--blocked:#c1433a;
  --on-solid:#fff;color-scheme:light;
  --f-display:"Bricolage Grotesque","IBM Plex Sans",system-ui,sans-serif;
  --f-body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --f-mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#0d1316;--panel:#141c20;--sunk:#1b252a;--ink:#e3e9ec;--muted:#94a2ac;--line:#26333a;
  --accent:#5cc0cc;--accent-soft:#163b41;
  --landed:#55c07f;--ready:#efb23a;--review:#6c9ff0;--queued:#61707a;--blocked:#ee6a5e;
  --on-solid:#0d1316;color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0d1316;--panel:#141c20;--sunk:#1b252a;--ink:#e3e9ec;--muted:#94a2ac;--line:#26333a;
  --accent:#5cc0cc;--accent-soft:#163b41;
  --landed:#55c07f;--ready:#efb23a;--review:#6c9ff0;--queued:#61707a;--blocked:#ee6a5e;
  --on-solid:#0d1316;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--f-body)}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px;display:flex;flex-direction:column;gap:22px}
a{color:inherit}
h1,h2,h3{font-family:var(--f-display);margin:0}
.eyebrow{font:500 11px/1 var(--f-mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.bid{display:block;font:400 10.5px var(--f-mono);color:var(--muted);opacity:.8;margin-top:1px}
.lmeta .bid{display:inline;margin-left:4px}
.top{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:14px 28px}
.top h1{font-size:28px;font-weight:700;letter-spacing:-.01em;margin-top:6px}
.top .lede{margin:6px 0 0;color:var(--muted);font-size:13.5px;max-width:84ch}
.summary{display:flex;flex-wrap:wrap;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.summary>div{padding:9px 16px;display:flex;flex-direction:column;gap:3px;min-width:0;border-left:1px solid var(--line)}
.summary>div:first-child{border-left:0}
.sk{font:500 10.5px/1 var(--f-mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.sv{font:600 18px/1.15 var(--f-display);font-variant-numeric:tabular-nums}
.sv small{font:400 12px var(--f-body);color:var(--muted)}
.sv.ready,.sv.needs{color:var(--ready)}.sv.review{color:var(--review)}
.sv.time{font:500 13px/1.6 var(--f-mono)}
.pbar{display:flex;height:14px;border-radius:4px;overflow:hidden;background:var(--sunk);gap:1px;margin-bottom:6px}
.pbar i{display:block;min-width:0}
.f-done,.b-done{background:var(--landed)}.f-ready{background:var(--ready)}
.f-progress{background:repeating-linear-gradient(135deg,var(--review) 0 4px,color-mix(in srgb,var(--review) 55%,var(--panel)) 4px 8px)}
.f-queued{background:var(--panel);box-shadow:inset 0 0 0 1.5px var(--queued)}
.f-blocked{background:repeating-linear-gradient(135deg,var(--blocked) 0 4px,var(--panel) 4px 7px)}
.f-backlog{background:color-mix(in srgb,var(--queued) 45%,var(--panel))}
.plegend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.plegend b{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.plegend .pts{margin-left:auto}
.duo{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;align-items:start}
.box{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.box h2{font-size:16px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.box h2 .sub,.lsec h2 .sub{font:400 11.5px var(--f-mono);color:var(--muted)}
.box ol,.box ul{margin:0;padding:0;list-style:none;display:grid;gap:8px}
.box li{font-size:13.5px;line-height:1.45;overflow-wrap:anywhere}
.box li a{font-weight:600}
.box li .when{display:block;font:500 10.5px var(--f-mono);color:var(--muted);letter-spacing:.03em;margin-top:1px}
.box li .when .bid{display:inline}
.box .empty{color:var(--muted)}
.box.needs{border-left:4px solid var(--ready)}.box.flight{border-left:4px solid var(--review)}
.box.carry{border-left:4px solid var(--landed)}
.box.carry ul{list-style:disc;padding-left:18px;gap:6px}
.box.carry li{font-size:13px}
.prog{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 16px 10px}
.prog-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:8px 20px;margin-bottom:12px}
.prog-head h2{font-size:17px}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin:0;padding:0;list-style:none;font-size:12px;color:var(--muted)}
.legend li{display:flex;align-items:center;gap:6px}
.legend b{color:var(--ink);font-weight:600}
.sw{width:14px;height:10px;border-radius:2px;display:inline-block}
.grid-head,.lane{display:grid;grid-template-columns:minmax(200px,1.4fr) 64px 64px 64px minmax(0,4fr) 64px;
  grid-template-areas:"name b s p i w" "name now now now now now";column-gap:4px}
.grid-head{grid-template-areas:"name b s p i w";padding-bottom:6px;border-bottom:1px solid var(--line)}
.grid-head>div{font:500 10.5px/1.2 var(--f-mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted);text-align:center;padding:4px 2px;border-radius:5px}
.grid-head>div:first-child{text-align:left}
.grid-head .cur{color:var(--accent);background:var(--accent-soft);font-weight:600}
.grid-head .arrow{display:block;font:400 10px var(--f-body);letter-spacing:0;text-transform:none;opacity:.8}
.lane{padding:10px 0;border-bottom:1px solid var(--line);row-gap:4px}
.lane:last-of-type{border-bottom:0}
.lhead{grid-area:name;min-width:0;padding-right:10px;display:flex;flex-direction:column;gap:3px}
.lhead h3{font-size:14.5px;font-weight:600;line-height:1.25}
.lane.k-progress .lhead h3::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--review);margin-right:7px;vertical-align:2px}
.lstat{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.lpct{font:700 20px/1 var(--f-display);font-variant-numeric:tabular-nums}
.lpct small{font-size:12px;font-weight:500;color:var(--muted)}
.tag{font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;white-space:nowrap}
.t-ready{background:var(--ready);color:var(--on-solid)}
.t-review{background:color-mix(in srgb,var(--review) 18%,transparent);color:var(--review)}
.t-queued{background:var(--sunk);color:var(--muted)}
.t-blocked{background:color-mix(in srgb,var(--blocked) 18%,transparent);color:var(--blocked)}
.t-landed{background:color-mix(in srgb,var(--landed) 18%,transparent);color:var(--landed)}
.lmeta{margin:0;font-size:11.5px;color:var(--muted)}
.ph{position:relative;min-height:54px;border-radius:6px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;font:500 10px var(--f-mono);text-transform:uppercase;letter-spacing:.05em;color:var(--muted);min-width:0}
.ph-brainstorm{grid-area:b}.ph-spec{grid-area:s}.ph-plan{grid-area:p}.ph-implement{grid-area:i}.ph-wrap{grid-area:w}
.ph .phl{display:none}
.ph.done{background:color-mix(in srgb,var(--landed) 12%,transparent);color:var(--landed)}
.ph.todo{border:1px dashed var(--line)}
.ph.now{background:var(--accent-soft);box-shadow:inset 0 0 0 1.5px var(--accent);color:var(--accent)}
.ph-implement{flex-direction:row;align-items:flex-start;justify-content:flex-start;gap:8px;padding:6px 8px 7px}
.ph-implement.todo,.ph-implement.done{background:color-mix(in srgb,var(--sunk) 60%,transparent)}
.ph-implement .phl,.ph-implement .phc{display:inline;font:500 10.5px/1.2 var(--f-mono);letter-spacing:.04em}
.ph-implement .phm{display:none}
.strip{grid-area:i;align-self:end;display:flex;gap:2px;margin:0 8px 7px;min-width:0;height:28px;position:relative;z-index:1}
.blk{flex:1 1 0;min-width:5px;height:100%;border-radius:3px;display:flex;align-items:center;justify-content:center;overflow:hidden;font:500 9.5px/1 var(--f-mono);white-space:nowrap}
.blk span{padding:0 4px;overflow:hidden}
.b-done{color:var(--on-solid)}
.b-ready{background:var(--ready);color:var(--on-solid);font-weight:700}
.b-progress{color:var(--on-solid);background:repeating-linear-gradient(135deg,var(--review) 0 6px,color-mix(in srgb,var(--review) 78%,var(--panel)) 6px 12px)}
.b-queued{background:var(--panel);box-shadow:inset 0 0 0 1.5px var(--queued);color:var(--muted)}
.b-blocked{background:repeating-linear-gradient(135deg,var(--blocked) 0 4px,var(--panel) 4px 7px);color:var(--ink)}
.b-blocked span{background:var(--panel);border-radius:2px}
.b-backlog{background:color-mix(in srgb,var(--queued) 45%,var(--panel));color:var(--ink)}
.lnow{grid-area:now;margin:0;font-size:12.5px;color:var(--muted);padding-left:2px;min-width:0;overflow-wrap:anywhere}
.lnow b{font:500 10.5px var(--f-mono);letter-spacing:.06em;text-transform:uppercase;color:var(--accent);margin-right:6px}
.note{margin:8px 0 0;font-size:12px;color:var(--muted);max-width:75ch}
.lanes{display:grid;gap:18px}
.lsec h2{font-size:16px;display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px;margin-bottom:8px}
.twrap{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font:500 10.5px/1.2 var(--f-mono);text-transform:uppercase;letter-spacing:.06em;color:var(--muted);background:var(--sunk)}
tbody tr:last-child td{border-bottom:0}
tbody tr{box-shadow:inset 3px 0 0 var(--rs,transparent)}
tr.r-done{--rs:var(--landed)}tr.r-ready{--rs:var(--ready)}tr.r-progress{--rs:var(--review)}
tr.r-queued,tr.r-backlog{--rs:var(--line)}tr.r-blocked{--rs:var(--blocked)}
td.title{width:42%}td.stc{width:8em}
.st{font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;white-space:nowrap;background:var(--sunk);color:var(--muted)}
.st.closed{background:color-mix(in srgb,var(--landed) 16%,transparent);color:var(--landed)}
.st.in_progress{background:color-mix(in srgb,var(--review) 16%,transparent);color:var(--review)}
.st.blocked{background:color-mix(in srgb,var(--blocked) 16%,transparent);color:var(--blocked)}
.st.open{background:color-mix(in srgb,var(--ready) 16%,transparent);color:var(--ready)}
.none{color:var(--muted)}
@media (max-width:760px){
  .duo{grid-template-columns:minmax(0,1fr)}
  .grid-head{display:none}
  .lane{grid-template-columns:repeat(5,minmax(0,1fr));grid-template-areas:"name name name name name" "b s p i w" "strip strip strip strip strip" "now now now now now";row-gap:6px}
  .ph{min-height:30px;padding:3px 2px}
  .ph .phl{display:inline;font-size:9px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ph .phm,.ph-implement .phc{display:none}
  .ph-implement{align-items:center;justify-content:center;padding:3px 2px}
  .strip{grid-area:strip;margin:0}
  .summary{width:100%}
  .summary>div{flex:1 1 40%;border-left:0;border-top:1px solid var(--line)}
  .plegend .pts{margin-left:0}
}
"""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
