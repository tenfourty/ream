"""The message an agent gets when Jeremy gives a verdict on a page it asked
him to review (page review step 2, homelab-ij8.108).

One message for the whole review, the verdict first, then every open mark on
the page as a numbered item in page order. The wording is plannotator's
lesson (docs/agent/research/2026-09-28-plannotator-study.md, section B):
agents ignore soft wording on "changes requested", and over-revise on
"approved with notes", so each verdict says exactly what to do. Ideas only;
no plannotator code (Jeremy, 28 Sep 2026).
"""

from __future__ import annotations

import re

from artifacts import comments, decisions, store, text
from artifacts.db import Database

# Bidi controls, zero-width characters and the BOM: a note that renders one
# way to Jeremy and another way to an agent is how an instruction hides.
_INVISIBLE = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200b-\u200f\u2060\ufeff]")
QUOTE_MAX = 300

_HEAD = {
    "approved": "Approved.",
    "approved_with_notes": (
        "These notes are non-blocking guidance. Do not revise solely because of them."
    ),
    "dismissed": "Closed without a verdict. This is not approval.",
}
_LABEL = {"comment": "Feedback on", "remove": "Remove this", "looks_good": "Looks good"}


def sanitize(s: str) -> str:
    return _INVISIBLE.sub("", s or "")


def _quote(s: str) -> str:
    return " ".join(sanitize(s).split())[:QUOTE_MAX]


def _load(db: Database, review_id: str, agent: str):
    """The answered review decision, only to the agent that owns its page.
    Every refusal is the same NotFound, so a key learns nothing about ids."""
    decision = decisions.get_owned(db, agent, review_id)
    if decision is None:
        raise store.NotFound()
    if decision.kind != "review" or decision.answer is None:
        raise store.NotFound()
    return decision


def _page_order(threads: list, body: str) -> list:
    haystack = text.searchable(body).lower()

    def key(item: tuple[int, comments.CommentRow]):
        i, root = item
        if root.anchor.get("kind") == "page":
            return (2, 0, i)
        at = haystack.find(" ".join(root.quote.split()).lower()) if root.quote else -1
        return (0, at, i) if at >= 0 else (1, 0, i)

    return [root for _, root in sorted(enumerate(threads), key=key)]


def _round_of(db: Database, review_id: str):
    with db.connect() as conn:
        return decisions.current_round(conn, review_id)


def render(db: Database, review_id: str, *, agent: str) -> tuple[str, dict]:
    decision = _load(db, review_id, agent)
    verdict = decisions.VERDICTS[decision.answer]
    page = store.get_owned(db, agent, decision.artifact_id)
    stored = store.get_body(db, agent, decision.artifact_id)

    # The round this verdict closed (page review step 4). The version is the
    # round's -- the one his page view showed when he gave it, when known --
    # not `decision.version_n`, which is the version first asked about.
    rnd = _round_of(db, review_id)
    round_n = rnd.n if rnd is not None else 1
    version = stored.version
    base_version = None
    if rnd is not None:
        version = rnd.reviewed_version or rnd.version_n
        base_version = rnd.base_version
    carried = round_n >= 2
    body_now = stored.body
    if carried and rnd.version_n != stored.version:
        body_now = store.get_version_body(
            db, agent, decision.artifact_id, rnd.version_n
        ).body

    everything = comments.list_for_artifact(
        db, decision.artifact_id, include_resolved=carried
    )
    replies: dict[str, list[comments.CommentRow]] = {}
    for c in everything:
        if c.thread_root:
            replies.setdefault(c.thread_root, []).append(c)
    states: dict[str, str] = {}
    if carried:
        states = {
            c.id: state
            for c, state in comments.carried_marks(
                everything, round_n, decision.created_at, body_now
            )
        }
    roots = [
        c
        for c in everything
        if c.thread_root is None
        and c.author_kind == "human"
        and (c.resolved_at is None or c.id in states)
    ]
    ordered = _page_order(roots, body_now)

    note = sanitize(decision.answer_note).strip()
    n = len(ordered)
    # Round 1 keeps the step-2 line exactly, so its callers are unchanged.
    where = f"{page.id}, v{version}" + (f", round {round_n}" if carried else "")
    lines = [
        (
            f"Artifacts review: {verdict.upper().replace('_', ' ')}"
            f' — "{_quote(page.title)}" ({where})'
        ),
        "",
    ]
    if verdict == "changes_requested":
        republish = (
            f" republish it with artifacts-publish <file> --update {page.id}."
            " Do not republish it unchanged."
        )
        if n:
            what = f"You MUST revise the page to address ALL {n} items below, then"
        elif note:
            what = "You MUST revise the page as his note asks, then"
        else:
            # No marks and no note: nothing here says what to change, and
            # guessing is how a page gets republished unchanged in spirit.
            what = (
                "He did not say what to change: ask him"
                f" (artifacts-decide ask {page.id} ...), then revise the page and"
            )
        lines.append(what + republish)
    else:
        lines.append(_HEAD[verdict])
    if note:
        lines.append("His note:")
        lines.extend(f"   > {line}" for line in note.splitlines())
    lines.append("")
    lines.append(
        "He left no marks." if n == 0 else f"He left {n} mark{'' if n == 1 else 's'}:"
    )

    items = []
    for number, root in enumerate(ordered, 1):
        body = sanitize(root.body).strip()
        if root.anchor.get("kind") == "page":
            head = f"{number}. General feedback about the page (mark {root.id})"
        else:
            label = _LABEL.get(root.kind, _LABEL["comment"])
            head = f'{number}. {label}: "{_quote(root.quote)}" (mark {root.id})'
        state = states.get(root.id)
        lines.append(head + (f" [{state}]" if state else ""))
        for line in body.splitlines():
            lines.append(f"   > {line}")
        thread = []
        for r in replies.get(root.id, []):
            said = " ".join(sanitize(r.body).split())
            author = _quote(r.author)
            lines.append(f"   - {author}: {said}")
            thread.append({"author": author, "text": said})
        items.append(
            {
                "n": number,
                "id": root.id,
                "kind": root.kind,
                "quote": _quote(root.quote),
                "note": body,
                "state": states.get(root.id, "new"),
                "replies": thread,
            }
        )

    data = {
        "verdict": verdict,
        "round": round_n,
        "version": version,
        "base_version": base_version,
        "note": note,
        "items": items,
    }
    return "\n".join(lines) + "\n", data
