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

Memory is not the only cost: a page of 8 MiB of tags of 4000 attributes each, or of
end tags, took `forms`, `sections`, `text` and `anchors` 3 to 20 seconds with every tag
read as it is. So each of those four gives its parser a `Budget` as its `meter`
(`PARSE_SECONDS`, made per call, started at its first token), which `BoundedParser`
asks about every token it steps over and about each long tag, and `TooComplex` is
raised when it is spent.

Every `HTMLParser` in `src/` is a `BoundedParser`, and a subclass overrides only what
it handles (`tests/test_htmlbound.py` walks the source and refuses anything else), so
a new reader of pages cannot forget this. What a reader does when it raises
`TooComplex` is its own to decide, and is written where it is caught: a request must
never get a 500 from it.
"""

from __future__ import annotations

import re
import time
from html import parser as html_parser
from html import unescape as _unescape
from html.parser import HTMLParser
from typing import Protocol


class TooComplex(Exception):
    """A tag is too long or has too many attributes for the parser's match to be run
    on it, or (for a reader with a budget, `renderfind.Budget` or `Budget`) the time is spent."""


# What reading one page may cost, in time: memory is bounded by the tag rules below,
# and this bounds the rest (a page of 8 MiB of tags of 4000 attributes each, or of
# end tags, takes the readers 5 to 20 seconds with nothing else wrong with it).
PARSE_SECONDS = 2.0


class _Budget(Protocol):
    def check(self) -> None: ...


class Budget:
    """The time one reading of a page may take. It starts at its first use, so the
    time a request waited for a scan slot is not spent. `check()` raises TooComplex
    when time is up and is cheap to call between tags; `step()` is for each token, and
    reads the clock only now and then. (`renderfind.Budget`, shared by the readings of
    a request and started when it is made, is its own: its readers count their own
    tokens.)"""

    def __init__(self, seconds: float = PARSE_SECONDS) -> None:
        self._seconds = seconds
        self._deadline: float | None = None
        self._steps = 0
        self.spent = False

    def check(self) -> None:
        if self.spent:
            raise TooComplex
        now = time.monotonic()
        if self._deadline is None:
            self._deadline = now + self._seconds
        if now >= self._deadline:
            self.spent = True
            raise TooComplex

    def step(self) -> None:
        """One more token read. Looks at the clock on the first call, then on every
        64th, and on every call once the budget is spent."""
        self._steps += 1
        if self.spent or self._steps == 1 or not self._steps & 63:
            self.check()


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
REPLACED = ("check_for_whole_start_tag", "goahead", "parse_endtag", "updatepos")

_UNESCAPE_CODE = _unescape.__code__


def _came_from_unescape(exc: BaseException) -> bool:
    """Whether `exc` was raised under `html.unescape`, which the parser calls on text and
    on attribute values when `convert_charrefs` is on. It reads a decimal reference
    (`&#` and digits) with `int()`, which refuses more digits than
    `sys.get_int_max_str_digits()` (4300) with a ValueError. The traceback says where it
    came from, so a ValueError from a subclass's own handler is not taken for it."""
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code is _UNESCAPE_CODE:
            return True
        tb = tb.tb_next
    return False


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
    `feed` or `close` for a tag too long or too busy to read, where the standard
    parser would run one match over the rest of the page, and for a decimal character
    reference of more digits than `int` reads (`&#` and over 4300 of them). `budget`
    (anything with `check()`, which raises TooComplex when time is up) is asked about
    long tags: for a reader that counts its own tokens. `meter` (a `Budget`) is asked about long
    tags too, and about every token the parser steps over (`updatepos`, which the
    standard parser calls after each one, an end tag or a stray "<" too)."""

    def __init__(
        self,
        *,
        convert_charrefs: bool = True,
        budget: _Budget | None = None,
        meter: Budget | None = None,
    ) -> None:
        self._meter = meter
        self._tag_budget = budget if budget is not None else meter
        super().__init__(convert_charrefs=convert_charrefs)

    def goahead(self, end):
        # `feed` and `close` both read through here. A reference of too many digits is
        # a page the parser cannot read, as a tag of too many attributes is: TooComplex.
        try:
            super().goahead(end)
        except ValueError as exc:
            if not _came_from_unescape(exc):
                raise
            raise TooComplex from exc

    def updatepos(self, i, j):
        if self._meter is not None:
            self._meter.step()
        return super().updatepos(i, j)

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
