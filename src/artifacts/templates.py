"""The templates an agent starts a page from, served with its key.

homelab-ij8.69. The templates are files in this package, so the wheel carries
them and the same bytes reach an agent three ways: the skill directory in this
repo, the skill directory in the homelab, and these two routes. The skill
directories are copies kept in step by `scripts/sync-templates.py` and guarded by
a test; THESE are the canonical bytes.

The routes exist because a skill directory is a Claude Code notion. Jeremy's
brief is that this has to work for "agents anywhere that I give access to", and
an agent that is not Claude, or is on another machine, has no skill directory to
read -- but it does have a key, because it needs one to publish at all. So the
key is the only thing a template costs.

    akey | curl -sS -H @- "$BASE/templates/project-status" -o page.html

A template body is served as text/plain with nosniff, not text/html, and that is
a security choice rather than pedantry: the API origin is the one origin in this
service that must never render markup. Page content lives on its own throwaway
origin precisely so a published page cannot reach anything; handing back the
same markup as a renderable document from the API origin would quietly undo
that. It is source, the caller is saving it to a file, and text/plain says so.
"""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path

from artifacts import textvars

DIR = Path(__file__).resolve().parent / "templates"

# What each one is for, in the words an agent needs to pick between them. Keyed
# by file stem; a template on disk with no entry here is still served, but it is
# listed with an empty `use_when`, which is a visible gap rather than a silent
# one -- see `index`.
USE_WHEN = {
    "project-status": "Where an arc stands. Update it in place as work moves.",
    "decision-request": "The page exists to ask {{owner_name}} something and get an answer back.",
    "incident": "Something broke and is now fixed: impact, timeline, what was ruled out, the lesson.",
    "implementation-plan": "The order of a piece of work, with each step named by its outcome.",
    "comparison": "Two or three approaches argued side by side, before a decision is asked.",
    "explainer": "How a piece of the house works, for someone who has forgotten.",
    "intent": "A design still being worked out. Mostly questions, several open at once.",
    "design": "The design written down: modules, flow, failure, tests, risks.",
}

TITLES = {
    "project-status": "Project status",
    "decision-request": "Decision request",
    "incident": "Incident",
    "implementation-plan": "Implementation plan",
    "comparison": "Comparison",
    "explainer": "Explainer",
    "intent": "Intent",
    "design": "Design",
}


class UnknownTemplate(Exception):
    """No template by that name."""


def names() -> list[str]:
    """Every template on disk, sorted. Files beginning `_` are not templates."""
    return sorted(p.stem for p in DIR.glob("*.html") if not p.name.startswith("_"))


@functools.cache
def raw(name: str) -> str:
    """One template's file, as it is in the package: its `{{variables}}` still in it. The name
    is matched against the directory listing rather than joined onto a path, so nothing a
    caller sends can walk out of it. A traversal attempt is simply a name that is not in the
    list, which is the same 404 as a typo -- there is no separate rejection path to get wrong.
    """
    if name not in names():
        raise UnknownTemplate(name)
    return (DIR / f"{name}.html").read_text(encoding="utf-8")


def body(name: str) -> str:
    """One template as it is served: the file with the deployment's words filled in (owner,
    gallery link, footer; `textvars`). Rendered per call, because the settings are read at call
    time; the cost is one regex pass over 20 KB."""
    return textvars.render(raw(name))


def shared_css() -> tuple[str, str]:
    """The shared stylesheet, and an ETag for it: (css, etag).

    homelab-ij8.21, Jeremy 19 Sep 2026: serve the shared look as a real
    stylesheet instead of 150 lines pasted into every page, while agents stay
    free to design a page however they like. It is 7.9 KB, which is 42% of the
    project-status template.

    The SAME FILE the templates are built from, not a copy: `sync-templates.py`
    splices this text between each template's `shared:` sentinels, and a test
    fails if a checked-in template has drifted from it. So a page that links
    the stylesheet and a page that inlines the block are looking at one source
    -- which is the only version of this worth having.

    Rendered per call (the owner's name is in its comments); the file ships inside the image
    and cannot change under a running pod. The ETag is content-derived, so a pod serving new CSS
    answers a stale revalidation with 200 and the rest with 304.
    """
    css = textvars.render((DIR / "_shared.css").read_text(encoding="utf-8"))
    digest = hashlib.sha256(css.encode("utf-8")).hexdigest()[:16]
    return css, f'"{digest}"'


def index() -> list[dict]:
    """The catalogue: enough for an agent to choose without fetching all eight."""
    out = []
    for name in names():
        text = body(name)
        out.append(
            {
                "name": name,
                "title": TITLES.get(name, name.replace("-", " ").capitalize()),
                "use_when": textvars.render(USE_WHEN.get(name, "")),
                "bytes": len(text.encode("utf-8")),
            }
        )
    return out
