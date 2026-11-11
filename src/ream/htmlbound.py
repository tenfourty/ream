"""The one HTML parser base this service uses: a page cannot make it run the standard
parser's long match on the whole page.

Python's `html.parser` decides whether a start tag, or an end tag, is whole with one
regular expression that reads attribute after attribute to the end of the tag, which on
a page of `<img <img <img ... >` (each "<img" is taken for an attribute) is the end of
the page. That one match keeps about 320 bytes of its state for each attribute: 540 MB
for a page of 8 MiB (1.4 GB when an attribute is two characters), 3 to 11 seconds, and
nothing can interrupt a regular expression part-way. So `BoundedParser` runs it only on
a tag that a ">" ends with no quote before it (the ">" cannot be inside a quoted value)
and that is under TAG_WINDOW characters long, where its state is a few megabytes. Any
other tag is read one attribute at a time (`end_of_tag`), a quoted value, however long,
being one step, and one of more than MAX_TAG_ATTRIBUTES attributes is not read:
`TooComplex`. Neither the next ">" nor the tag's length can be the measure: a ">" inside
quotes does not end the tag, so a page can put one at its start and a long tag after it;
and the advice of `csp.py` is to inline an image as a data: address, one tag of a
megabyte. (`_END_TAG_USES_IT`: an older parser reads an end tag another way.)

`_TAG_END_NEEDS_A_GT` is for the start tag with no ">" after it, where the answer is
the one the match gives (this parser says a tag is whole only if the match ends on a
">", and with none after the "<" it cannot) and need not be asked. Parsers of an older
Python test tags another way, so they keep their own.

Every `HTMLParser` in `src/` is a `BoundedParser`, and a subclass overrides only what
it handles (`tests/test_htmlbound.py` walks the source and refuses anything else), so
a new reader of pages cannot forget this. What a reader does when it raises
`TooComplex` is its own to decide, and is written where it is caught: a request must
never get a 500 from it.
"""

from __future__ import annotations

import re
from html import parser as html_parser
from html.parser import HTMLParser
from typing import Protocol


class TooComplex(Exception):
    """A tag is too long or has too many attributes for the parser's match to be run
    on it, or (for a reader with a budget, `renderfind.Budget`) the budget is spent."""


class _Budget(Protocol):
    def check(self) -> None: ...


_TAG_END_NEEDS_A_GT = hasattr(html_parser, "locatetagend")
_END_TAG_USES_IT = "locatetagend" in HTMLParser.parse_endtag.__code__.co_names
TAG_WINDOW = 64 * 1024
MAX_TAG_ATTRIBUTES = 4096
_CLOCK_AFTER = 1024  # characters: a tag this long is timed on its own
_SHORT_TAG = 256

# `html.parser.locatetagend` with its repeated group taken apart: the tag's name, and
# one attribute. tests/test_render_findings.py holds them to the parser's own match.
_TAG_NAME = re.compile(r"[a-zA-Z][^\t\n\r\f />]*[\t\n\r\f /]*")
_ATTRIBUTE = re.compile(
    r"""
    (?<=['"\t\n\r\f /])[^\t\n\r\f />][^\t\n\r\f /=>]*
    (?:[\t\n\r\f ]*=[\t\n\r\f ]*
      (?:'[^']*'|"[^"]*"|(?!['"])[^>\t\n\r\f ]*)
    )?
    [\t\n\r\f /]*
    """,
    re.VERBOSE,
)

# The methods `BoundedParser` replaces. A subclass defines none of them
# (tests/test_htmlbound.py).
REPLACED = ("check_for_whole_start_tag", "parse_endtag")


def end_of_tag(rawdata: str, name: int, budget: _Budget | None = None) -> int:
    """Where the parser's own match would end the tag whose name begins at `name`
    (after its ">"), or -1 when it is not whole; or TooComplex for a tag it should not
    be asked about. The same answers as the parser's, on any page: each way of reading
    the tag below is one of them.

    With no quote before the first ">", that ">" ends the tag, and the parser's match
    is asked only as far as it (so that no quote can be left open at the end of what it
    is shown), and only when that is inside the window. Any other tag is read one
    attribute at a time, where a quoted value, however long, is one step."""
    closing = rawdata.find(">", name)
    if closing < 0:
        return -1
    if (
        closing - name < TAG_WINDOW
        and rawdata.find('"', name, closing) < 0
        and rawdata.find("'", name, closing) < 0
    ):
        end = html_parser.locatetagend.match(rawdata, name, closing + 1).end()
        if budget is not None and closing - name > _CLOCK_AFTER:
            budget.check()  # a tag this long took long enough that each one is timed
        return end if rawdata[end - 1] == ">" else -1
    named = _TAG_NAME.match(rawdata, name)
    if named is None:
        return -1  # not a tag name: the caller has seen a letter, so not reached
    at = named.end()
    for count in range(MAX_TAG_ATTRIBUTES):
        attribute = _ATTRIBUTE.match(rawdata, at)
        if attribute is None:
            if budget is not None and count >= 64:
                budget.check()
            return at + 1 if rawdata.startswith(">", at) else -1
        at = attribute.end()
    raise TooComplex


class BoundedParser(HTMLParser):
    """An HTMLParser whose tags are read by `end_of_tag`: it raises `TooComplex` from
    `feed` or `close` for a tag too long or too busy to read, where the standard parser
    would run one match over the rest of the page. `budget` (anything with `check()`,
    which raises TooComplex when time is up) is asked about long tags too."""

    def __init__(
        self, *, convert_charrefs: bool = True, budget: _Budget | None = None
    ) -> None:
        super().__init__(convert_charrefs=convert_charrefs)
        self._tag_budget = budget

    def check_for_whole_start_tag(self, i):
        if not _TAG_END_NEEDS_A_GT:
            return super().check_for_whole_start_tag(i)
        if self.rawdata.find(">", i + 1) < 0:
            return -1  # incomplete: what the parser answers, without its long match
        return end_of_tag(self.rawdata, i + 1, self._tag_budget)

    def parse_endtag(self, i):
        rawdata = self.rawdata
        if _END_TAG_USES_IT and html_parser.endtagopen.match(rawdata, i):
            closing = rawdata.find(">", i + 2)
            if closing < 0:
                return -1
            # The parser reads an end tag with the same match as a start tag. One
            # that a ">" ends in a few characters, with no quote before it to hide a
            # ">" in, cannot be long: the parser reads it as it is.
            if (
                closing - i > _SHORT_TAG
                or rawdata.find('"', i, closing) >= 0
                or rawdata.find("'", i, closing) >= 0
            ) and end_of_tag(rawdata, i + 2, self._tag_budget) < 0:
                return -1
        return super().parse_endtag(i)
