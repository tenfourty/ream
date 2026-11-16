"""The publish finding for a page that carries its own fonts as data: addresses.

The page policy allows `font-src 'self' data:` (decisions.md, 16 Nov 2026), so a page
may embed a font. A custom font can make the text a person sees differ from the text in
the page's source (a glyph map can draw other letters than the ones written), so the
publish answer names them, as a warning: the publish is never refused for it.

The warning is advisory, made for an honest author. It reads `@font-face` blocks written
in the page's text; it does not see a font added by script (`new FontFace(...)`) or one
whose address or rule is written with CSS escapes. Its silence is not proof of no fonts.

Only `@font-face` blocks are read (a block cannot sit in a style attribute: it is an
at-rule, which an attribute cannot hold). The reading is plain string search, never a
regular expression run from every opening: from each block's start, one walk looks for
the next `}`, comment opener, quote or `</style` with a pattern of single characters, a
comment or a quoted string is skipped with `str.find` to its end (one that is never
closed runs to the end of the text), so a `}` inside a comment or a string does not end
the block and one pass over the page is the whole cost. A block never closed ends at the
`</style` or the end of the text. At most `MAX_BLOCKS` blocks are read, and then the
count says "at least". No HTML is parsed, so `htmlbound` has no part in it. A page over
the scan limit, or one that spends the time budget the other publish readers share,
answers `embedded_fonts_not_run`, never a partial list or an empty one, which would read as
a clean page (as `csp.check` and `traps.check` do); so does a page with more than
`MAX_BLOCKS` blocks and no font among those read. A comment or string still open at the
next `</style` or `<style` ends there, with the block (a browser ends the element at
`</style`; a `<style` means what came before was prose).
"""

from __future__ import annotations

import re

from ream.htmlbound import TooComplex
from ream.renderfind import MAX_SCAN_CHARS, Budget

SHAPE = "embedded_fonts"
NOT_RUN_SHAPE = "embedded_fonts_not_run"
MAX_BLOCKS = 500

NOT_RUN = (
    "this page is too large or complex for the embedded-font check to read in the time "
    "allowed: it did not run, so its silence on this page means nothing"
)

_FACE = re.compile(r"@font-face", re.IGNORECASE)
# What changes the walk inside a block: its end, a comment, a string, or the end of the
# style element. Single characters and two fixed ones: nothing here backtracks. ASCII, so
# that `</ſtyle` (whose `ſ` the flag would match to an `s`) is not an end tag.
_STOP = re.compile(r"[}\"']|/\*|</?style", re.IGNORECASE | re.ASCII)
_STYLE = re.compile(r"</?style", re.IGNORECASE | re.ASCII)
# The end of a string opened by that quote, a backslash taking the character after it.
_STRING = {
    '"': re.compile(r'[^"\\]*(?:\\.[^"\\]*)*"', re.DOTALL),
    "'": re.compile(r"[^'\\]*(?:\\.[^'\\]*)*'", re.DOTALL),
}
# url( data:<address>: a quoted address runs to its closing quote (a `)` inside it is
# part of it), an unquoted one to the `)`. Size is of the address as written, in the
# page's bytes (base64 text, not the decoded font).
_DATA_URL = re.compile(
    r"url\(\s*(?:\"\s*(data:[^\"]*)|'\s*(data:[^']*)|(data:[^)\"'\s]*))",
    re.IGNORECASE,
)


class _EndTag:
    """Where the next `</style` is, searched for again only once the one found is behind
    the reading: a page with none is searched once, however many blocks it has."""

    def __init__(self) -> None:
        self.at: int | None = None

    def next(self, body: str, pos: int) -> int:
        if self.at is None or (self.at >= 0 and self.at < pos):
            found = _STYLE.search(body, pos)
            self.at = -1 if found is None else found.start()
        return self.at


def _block(body: str, at: int, budget: Budget, tag: _EndTag) -> tuple[str, int]:
    """(the block's text with its comments blanked, where the block ends) for the
    `@font-face` at `at`. Newlines of a comment are kept so lines still count. A
    comment or a string still open at the next `</style` ends there, and so does the
    block, as the browser ends the element (a `<style` opening ends them too: the text
    after it is a stylesheet again, and what came before it, prose)."""
    parts: list[str] = []
    done = pos = at + len("@font-face")
    while True:
        budget.step()
        stop = _STOP.search(body, pos)
        if stop is None:
            parts.append(body[done:])
            return "".join(parts), len(body)
        found, where = stop.group(), stop.start()
        if found == "}" or found.lower().endswith("style"):
            parts.append(body[done:where])
            return "".join(parts), where
        limit = tag.next(body, where)
        if found == "/*":
            parts.append(body[done:where])
            close = body.find("*/", where + 2)
            end = len(body) if close < 0 else close + 2
            if limit >= 0 and limit < end:
                parts.append("\n" * body.count("\n", where, limit))
                return "".join(parts), limit
            parts.append("\n" * body.count("\n", where, end))
            done = pos = end
        else:
            closed = _STRING[found].match(body, where + 1)
            end = len(body) if closed is None else closed.end()
            if limit >= 0 and limit < end:
                parts.append(body[done:limit])
                return "".join(parts), limit
            pos = end


def check(body: str, budget: Budget | None = None) -> list[dict]:
    """One finding when a `@font-face` block holds a data: address, else none. A page
    too large to read, or one that spends the budget, answers `embedded_fonts_not_run`."""
    if len(body) > MAX_SCAN_CHARS:
        return [{"shape": NOT_RUN_SHAPE, "line": 1, "hint": NOT_RUN}]
    budget = budget or Budget()
    faces = size = 0
    first_at = -1
    capped = False
    tag = _EndTag()
    try:
        budget.check()
        found = _FACE.search(body)
        for read in range(MAX_BLOCKS + 1):
            if found is None:
                break
            if read == MAX_BLOCKS:
                capped = True
                break
            budget.step()
            text, end = _block(body, found.start(), budget, tag)
            addresses = 0
            for match in _DATA_URL.finditer(text):
                budget.step()
                address = next(g for g in match.groups() if g is not None)
                addresses += 1
                size += len(address.encode("utf-8"))
            if addresses:
                faces += 1
                if first_at < 0:
                    first_at = found.start()
            found = _FACE.search(body, end)
    except TooComplex:
        return [{"shape": NOT_RUN_SHAPE, "line": 1, "hint": NOT_RUN}]
    if capped and not faces:
        # blocks went unread, and none of those read held a font: not a clean page
        return [{"shape": NOT_RUN_SHAPE, "line": 1, "hint": NOT_RUN}]
    if not faces:
        return []
    noun = "font" if faces == 1 else "fonts"
    count = f"at least {faces}" if capped else str(faces)
    return [
        {
            "shape": SHAPE,
            "line": body.count("\n", 0, first_at) + 1,
            "hint": (
                f"this page embeds {count} {noun} as data: addresses, {size} bytes in all "
                "(they count inside the page size limit). A custom font can make the text "
                "a person sees differ from the page's source text, so say so if the "
                "page's wording matters. Fonts are allowed (font-src 'self' data:); "
                "background workers are still blocked. This warning is advisory: it does "
                "not see fonts added by script or written with CSS escapes."
            ),
        }
    ]
