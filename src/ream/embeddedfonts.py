"""The publish finding for a page that carries its own fonts as data: addresses.

The page policy allows `font-src 'self' data:` (decisions.md, 16 Nov 2026), so a page
may embed a font. A custom font can make the text a person sees differ from the text in
the page's source (a glyph map can draw other letters than the ones written), so the
publish answer names them, as a warning: the publish is never refused for it.

The warning is advisory, made for an honest author. It counts the font files embedded as
data: addresses, wherever they appear in the page's text (a comment or a template
included). It does not see a font added by script, one whose address is written with CSS
escapes, or one embedded without a font media type (`data:application/octet-stream`).
Its silence is not proof of no fonts.

The reading is one pass over the page's text with one regular expression that has no
nested quantifier (so nothing in it backtracks), and no parsing of HTML, CSS, comments or
strings: each address is read from its start to the first `)`, quote, whitespace or `<`,
and the next search starts where that one ended, so each character is read once. A page
over the scan limit, or one that spends the time budget the other publish readers share,
answers `embedded_fonts_not_run`, never an empty list, which would read as a clean page
(as `csp.check` and `traps.check` do).
"""

from __future__ import annotations

import re

from ream.htmlbound import TooComplex
from ream.renderfind import MAX_SCAN_CHARS, Budget

SHAPE = "embedded_fonts"
NOT_RUN_SHAPE = "embedded_fonts_not_run"

NOT_RUN = (
    "this page is too large or complex for the embedded-font check to read in the time "
    "allowed: it did not run, so its silence on this page means nothing"
)

# `data:`, optional whitespace, a font media type, then `;` or `,`. Possessive, so a type
# that is not followed by either is dropped at once rather than given back a character at
# a time.
_FONT = re.compile(
    r"data:[ \t\r\n\f]*+"
    r"(?:font/[a-z0-9.+-]++|application/(?:x-)?font-[a-z0-9.+-]++|application/vnd\.ms-fontobject)"
    r"(?=[;,])",
    re.IGNORECASE | re.ASCII,
)
# The rest of an address, from where its media type ended: to a `)`, quote, space or `<`.
_REST = re.compile(r"[^)\"'\s<]*+")


def check(body: str, budget: Budget | None = None) -> list[dict]:
    """One finding when the page holds a font as a data: address, else none. A page too
    large to read, or one that spends the budget, answers `embedded_fonts_not_run`."""
    if len(body) > MAX_SCAN_CHARS:
        return [{"shape": NOT_RUN_SHAPE, "line": 1, "hint": NOT_RUN}]
    budget = budget or Budget()
    count = size = 0
    first_at = -1
    try:
        budget.check()
        pos = 0
        while (found := _FONT.search(body, pos)) is not None:
            budget.step()
            end = _REST.match(body, found.end()).end()  # type: ignore[union-attr]
            count += 1
            size += len(body[found.start() : end].encode("utf-8", "surrogatepass"))
            if first_at < 0:
                first_at = found.start()
            pos = end
    except TooComplex:
        return [{"shape": NOT_RUN_SHAPE, "line": 1, "hint": NOT_RUN}]
    if not count:
        return []
    noun = "font file" if count == 1 else "font files"
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
                "not see fonts added by script, written with CSS escapes or embedded "
                "without a font type."
            ),
        }
    ]
