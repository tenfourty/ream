"""What changed in the tools, said once per machine after an update.

A seat asked for this on 24 Sep 2026 while moving its pages here: every deploy
reinstalls the tools under a new version, and the only way to learn that
`--matches` or `--delete` now existed was to read `--help` again, which nobody
does. So the tools carry their own short list and print the entries a machine
has not seen yet, to stderr, once.

Newest first. One line each, written for the agent reading it, in outcomes.
Add a line here in the same commit as the change it describes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

NEWS: tuple[tuple[str, str], ...] = (
    (
        "2026-10-06b",
        "artifacts-decide wait now hears only your own office's answers by default: the office is ARTIFACTS_SESSION before the first / (or --office NAME), and another office's answer under the same key no longer wakes you. Your own unacknowledged answer comes back at most hourly; list still shows it until you ack. --all-offices restores everything, --timeout 0 waits with no limit, and --json prints each wake as one line.",
    ),
    (
        "2026-10-06",
        "An agent can now be renamed (artifacts-admin rename-agent OLD NEW), or merged into another. Your pages, questions, answers, comments and grants move with it, your existing key keeps working and now acts as the new name, and the old name still works anywhere you type one (giving a page, the gallery's agent filter).",
    ),
    (
        "2026-09-29f",
        'wait --review now claims the round for your session (set ARTIFACTS_SESSION): a second session waiting on the same review exits 8 and names who has it. artifacts-decide review-release <id> --reason "…" hands it back, and Jeremy sees why. reply and ack send an Idempotency-Key, so a retry of the same request never posts twice; pass the same --idempotency-key to make re-running the command safe too.',
    ),
    (
        "2026-09-29e",
        'When Jeremy requests changes and you republish the page, round 2 of the same review opens by itself: keep waiting on the same review id. The verdict message now says the round and tags each earlier mark [still on page], [text changed] or [addressed/removed]. In round 1 each item\'s JSON state is now "new" (it used to be the quote state).',
    ),
    (
        "2026-09-29d",
        "Your comments on a page are saved at once and checked by GitGuardian just after; if it finds a secret the comment is withdrawn and you get a comment_withdrawn event naming the kind, never the value.",
    ),
    (
        "2026-09-29c",
        "On a page under review Jeremy's marks are now held until his verdict unless he switches to live; artifacts-decide ask-review --live asks for them as he goes. A held mark is invisible to you until then.",
    ),
    (
        "2026-09-29b",
        "artifacts-decide ask-review <page-id> asks Jeremy to review a page; wait --review <id> prints one numbered message with his verdict and marks, and exits 0 approved, 4 approved with notes, 5 changes requested, 6 dismissed, 7 new marks with no verdict yet.",
    ),
    (
        "2026-09-29a",
        "Jeremy can now mark text on any page as 'Remove this' or 'Looks good' as well as comment; artifacts-decide comments shows which, and comment rows carry a kind.",
    ),
    (
        "2026-09-28a",
        "A page may now be up to 3 MB (it was 512 KB).",
    ),
    (
        "2026-09-24d",
        (
            "Every publish, update and preview is now also checked by GitGuardian's "
            "ggshield. A refusal names the kind (shape ggshield_<kind>) and the "
            "line, never the value."
        ),
    ),
    (
        "2026-09-24c",
        (
            "artifacts-beads-page --parent <epic> --title NAME prints a status page "
            "drawn from your beads (titles and states, never ids). Put it in "
            "~/.config/artifacts/pages.toml as a render line and artifacts-republish "
            "keeps the page current after every bead change."
        ),
    ),
    (
        "2026-09-24b",
        (
            "artifacts-publish page.html --preview shot.png renders your page at "
            "phone width WITHOUT publishing it, so you can look before Jeremy does. "
            "artifacts-publish --matches ID FILE: exit 0 if FILE is exactly the live "
            "page, 3 if not. --give --to takes homelab_ops or homelab-ops. "
            "--check refuses a page still carrying a template's REPLACE marker or "
            "example title. Stable install: uv tool install --force --no-index "
            "--find-links <base>/api/v1/tools/ artifacts"
        ),
    ),
    (
        "2026-09-24a",
        (
            "artifacts-publish --give ID --to AGENT moves a page to another agent; "
            "--delete/--deleted/--restore bin and recover pages; a publish reports "
            "the page's script errors from a real browser render."
        ),
    ),
)

# Shown on a machine that has never recorded anything: enough to orient a new
# arrival, not a history lesson.
FIRST_RUN = 1


def _state_file() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "artifacts" / "news-seen"


def announce(stream=None) -> None:
    """Print the entries this machine has not seen, then remember the newest.

    Never raises and never touches stdout: this runs before every command, and
    a note about new features must not be the thing that breaks a pipeline.
    `ARTIFACTS_NEWS=0` silences it.
    """
    if os.environ.get("ARTIFACTS_NEWS") == "0" or not NEWS:
        return
    stream = stream or sys.stderr
    path = _state_file()
    try:
        seen = path.read_text(encoding="utf-8").strip() if path.exists() else None
        unseen = [entry for entry in NEWS if seen is None or entry[0] > seen]
        if seen is None:
            unseen = unseen[:FIRST_RUN]
        if not unseen:
            return
        for _tag, line in unseen:
            print(f"new in the artifacts tools: {line}", file=stream)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(NEWS[0][0] + "\n", encoding="utf-8")
    except OSError:
        return
