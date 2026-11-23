"""Small edits to a page by quoted text (23 Nov 2026).

An agent changes one sentence by sending the exact words it wants replaced and the words that
replace them, instead of resending the whole page or needing an element id. The edits apply in
order, each to the text the edits before it produced, and every `old_str` must occur exactly
once there -- overlapping occurrences count too, so "aa" in "aaa" is two. All or nothing: the
whole result is computed first and `store.patch_edits` stores it in one `store.patch` call, so
the size limit, the is-a-page check, both secret scans and the review rounds run on the whole
resulting page as they do for a whole-page update.

Pure functions only -- no database. A refusal names the edit's index and how many matches
there were, never the page's text or the quote.

Left out on purpose: regular expressions, fuzzy matching and whitespace normalisation.
"""

from __future__ import annotations

MAX_EDITS = 50
MAX_STR_BYTES = 64 * 1024


class EditError(Exception):
    """An edit was refused. `index` is the edit's place in the list (None when the list as a
    whole is), `matches` how many times its old_str occurs when that is why."""

    def __init__(
        self, detail: str, index: int | None = None, matches: int | None = None
    ):
        super().__init__(detail)
        self.detail = detail
        self.index = index
        self.matches = matches

    def refusal(self) -> dict:
        """The 422 body: the field, then only what is present."""
        out: dict = {"field": "edits"}
        if self.index is not None:
            out["index"] = self.index
        if self.matches is not None:
            out["matches"] = self.matches
        out["detail"] = self.detail
        return out


def check(edits: list[tuple[str, str]]) -> None:
    """The list's own limits, before any page is read: 1 to MAX_EDITS edits, each old_str
    non-empty, each string at most MAX_STR_BYTES."""
    if not edits:
        raise EditError("edits must hold at least one edit")
    if len(edits) > MAX_EDITS:
        raise EditError(f"at most {MAX_EDITS} edits in one update")
    for index, (old, new) in enumerate(edits):
        if not old:
            raise EditError("old_str is empty", index)
        for name, value in (("old_str", old), ("new_str", new)):
            try:
                size = len(value.encode())
            except UnicodeEncodeError:  # a lone surrogate from JSON's \ud800
                raise EditError(f"{name} is not valid UTF-8", index) from None
            if size > MAX_STR_BYTES:
                raise EditError(f"{name} is over {MAX_STR_BYTES // 1024} KiB", index)


def occurrences(text: str, needle: str) -> int:
    """How many times `needle` occurs in `text`, overlapping ones included."""
    count = 0
    at = text.find(needle)
    while at != -1:
        count += 1
        at = text.find(needle, at + 1)
    return count


def apply(body: str, edits: list[tuple[str, str]]) -> str:
    """`body` with each (old_str, new_str) applied in order to the text so far. Raises
    EditError, naming the first failing index, when an old_str is empty or does not occur
    exactly once."""
    check(edits)
    for index, (old, new) in enumerate(edits):
        first = body.find(old)
        if first == -1:
            raise EditError("old_str occurs 0 times", index, 0)
        if body.find(old, first + 1) != -1:
            found = occurrences(body, old)
            raise EditError(f"old_str occurs {found} times", index, found)
        body = body[:first] + new + body[first + len(old) :]
    return body
