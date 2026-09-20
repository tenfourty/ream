#!/usr/bin/env python3
"""Splice the shared stylesheet into every template, then fan them out.

Templates are self-contained files on purpose: an agent opens one in a browser
with nothing installed, which is the property that makes the reference repo this
set is modelled on useful at all. That means the shared token block is
physically repeated in every template, and repeated blocks drift.

So `_shared.css` is the one authoritative copy and this script writes it into
the region each template marks off:

    /* ==== shared: begin ... ==== */
    ...
    /* ==== shared: end ==== */

Run it after editing `_shared.css`, or after adding a template. Then run
`pytest tests/test_templates.py`, which fails if any checked-in file has drifted
from what this script would write — so a template edited by hand and not synced
is caught, not shipped.

`--check` writes nothing and exits 1 if anything is out of date.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BEGIN = "/* ==== shared: begin — do not edit here; edit _shared.css and re-sync ==== */"
END = "/* ==== shared: end ==== */"

REPO = Path(__file__).resolve().parent.parent
CANON = REPO / "src" / "artifacts" / "templates"

# Every place a copy of the templates has to appear. The homelab tree is listed
# because its own sessions read their skill from there; it is skipped rather
# than failed when the tree is not on this machine, so a clone of this repo
# alone still syncs.
SKILL_DIRS = [
    REPO / ".claude" / "skills" / "lab-artifacts" / "templates",
    Path.home() / "repos" / "homelab" / ".claude" / "skills" / "lab-artifacts" / "templates",
]


def templates() -> list[Path]:
    found = sorted(p for p in CANON.glob("*.html"))
    if not found:
        raise SystemExit(f"no templates found in {CANON} — refusing to sync nothing")
    return found


def splice(text: str, shared: str, where: Path) -> str:
    """Return `text` with the region between the sentinels replaced by `shared`.

    Both sentinels must be present and in order. A file that has lost one fails
    loudly here rather than being silently written with an empty shared block —
    which would pass a naive "are they all identical?" check on every file at
    once.
    """
    start = text.find(BEGIN)
    end = text.find(END)
    if start < 0:
        raise SystemExit(f"{where}: missing the shared-begin sentinel")
    if end < 0:
        raise SystemExit(f"{where}: missing the shared-end sentinel")
    if end < start:
        raise SystemExit(f"{where}: shared-end comes before shared-begin")
    head = text[: start + len(BEGIN)]
    tail = text[end:]
    return f"{head}\n{shared.rstrip(chr(10))}\n{tail}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 if stale")
    args = ap.parse_args()

    shared = (CANON / "_shared.css").read_text(encoding="utf-8")
    stale: list[str] = []
    written = 0

    for path in templates():
        current = path.read_text(encoding="utf-8")
        wanted = splice(current, shared, path)
        if current != wanted:
            if args.check:
                stale.append(f"{path.relative_to(REPO)}: shared block differs from _shared.css")
            else:
                path.write_text(wanted, encoding="utf-8")
                written += 1

        for skill_dir in SKILL_DIRS:
            if not skill_dir.parent.exists():
                continue  # that tree is not on this machine
            copy = skill_dir / path.name
            existing = copy.read_text(encoding="utf-8") if copy.exists() else None
            if existing == wanted:
                continue
            if args.check:
                stale.append(f"{copy}: copy differs from canonical")
            else:
                skill_dir.mkdir(parents=True, exist_ok=True)
                copy.write_text(wanted, encoding="utf-8")
                written += 1

    if args.check:
        for line in stale:
            print(line, file=sys.stderr)
        print(f"checked {len(templates())} templates, {len(stale)} stale", file=sys.stderr)
        return 1 if stale else 0

    print(f"synced {len(templates())} templates, wrote {written} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
