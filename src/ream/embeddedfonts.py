"""The publish finding for a page that carries its own fonts as data: addresses.

The page policy allows `font-src 'self' data:` (decisions.md, 16 Nov 2026), so a page
may embed a font. A custom font can make the text a person sees differ from the text in
the page's source (a glyph map can draw other letters than the ones written), so the
publish answer names them, as a warning: the publish is never refused for it.

Only `@font-face` blocks are read (a block cannot sit in a style attribute: it is an
at-rule, which an attribute cannot hold). The reading is plain string search, never a
regular expression run from every opening: each block is found with `str.find` and ends
at the next "}", so one pass over the page is the whole cost, a block that is never
closed ends the reading, and the count of blocks read is capped. No HTML is parsed, so
`htmlbound` has no part in it; the time budget is the one the other publish readers share.
"""

from __future__ import annotations

import re

from ream.htmlbound import TooComplex
from ream.renderfind import MAX_SCAN_CHARS, Budget

SHAPE = "embedded_fonts"
MAX_BLOCKS = 500

# url( data:<anything up to the closing quote or parenthesis>. The size counted is the
# whole address, in the page's own bytes (base64 text, not the decoded font).
_DATA_URL = re.compile(r"url\(\s*[\"']?(data:[^\"')]*)", re.IGNORECASE)
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def check(body: str, budget: Budget | None = None) -> list[dict]:
    """One finding when a `@font-face` block holds a data: address, else none. A page too
    large to read, or one that spends the budget, reports nothing here (the other checks
    already say they did not run)."""
    lowered = body.lower()
    if "@font-face" not in lowered or len(body) > MAX_SCAN_CHARS:
        return []
    budget = budget or Budget()
    count = size = 0
    first_at = -1
    start = 0
    try:
        for _ in range(MAX_BLOCKS):
            budget.check()
            at = lowered.find("@font-face", start)
            if at < 0:
                break
            end = body.find("}", at)
            if end < 0:
                break
            start = end + 1
            block = _COMMENT.sub("", body[at:end])
            for found in _DATA_URL.finditer(block):
                count += 1
                size += len(found.group(1).encode("utf-8"))
                if first_at < 0:
                    first_at = at
    except TooComplex:
        return []
    if not count:
        return []
    noun = "font" if count == 1 else "fonts"
    return [
        {
            "shape": SHAPE,
            "line": body.count("\n", 0, first_at) + 1,
            "hint": (
                f"this page embeds {count} {noun} as data: addresses, {size} bytes in all "
                "(they count inside the page size limit). A custom font can make the text "
                "a person sees differ from the page's source text, so say so if the "
                "page's wording matters. Fonts are allowed (font-src 'self' data:); "
                "background workers are still blocked."
            ),
        }
    ]
