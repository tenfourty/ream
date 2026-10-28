"""Where a pinned comment is now: its evidence read against the page as it is.

A pin carries evidence -- the quote, the nearest heading, an element id -- not
only a position (docs/agent/specs/2026-10-29-pinned-comments.md). After a
republish this module says, from that evidence alone, whether the spot is
`current`, `edited` (still there, its words changed) or `outdated` (gone), and
never moves a pin onto text it was not about. The answer is read, not stored:
it is a fact about the page as it is NOW.

Pure functions over a page body. The lower layer of `comments`, which imports
from here, so nothing in this file may import `comments`.
"""

import re
from html.parser import HTMLParser

from artifacts import sections, text

CURRENT = "current"
EDITED = "edited"
OUTDATED = "outdated"
STATES = (CURRENT, EDITED, OUTDATED)

# An id the anchor may record: the charset and length `sections` addresses (`sections.ELEMENT_ID`).
ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
HEADING_MAX = 200

# How much of a quote's surroundings, or of the quote itself, must still be on
# the page (spaces removed) for its absence to read as an edit in place rather
# than a removal. Short enough to survive an edit next to it, long enough that
# a few common letters do not count.
CONTEXT_CHARS = 32
CONTEXT_MIN = 8
# Headings searched when finding the one a quote sits under.
_HEADINGS_MAX = 400


class _Headings(HTMLParser):
    """Every CLOSED heading as (level, start, end, name): `start` is where its
    opening tag begins, `end` where its closing tag ends, both offsets into the
    body. One linear pass, so a page of unclosed `<h2` openers costs no more than
    any other page; an opener never closed before the next heading is dropped."""

    def __init__(self, body: str) -> None:
        super().__init__()
        self._body = body
        # HTMLParser counts a line at each "\n" and nothing else.
        self._lines = [0]
        at = body.find("\n")
        while at >= 0:
            self._lines.append(at + 1)
            at = body.find("\n", at + 1)
        self.found: list[tuple[int, int, int, str]] = []
        # Aligned with `found`: the heading's text as written (case kept) and its id.
        self.found_text: list[str] = []
        self.found_ids: list[str] = []
        self._open: tuple[int, int] | None = None
        self._open_id = ""
        self._text: list[str] = []

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._lines[line - 1] + col

    def handle_starttag(self, tag, attrs):
        if len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
            self._open = (int(tag[1]), self._offset())
            self._text = []
            self._open_id = dict(attrs).get("id") or ""

    def handle_data(self, data):
        if self._open is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if self._open is not None and tag == f"h{self._open[0]}":
            at = self._offset()
            stop = self._body.find(">", at)
            end = len(self._body) if stop < 0 else stop + 1
            raw = " ".join("".join(self._text).split())
            self.found.append((self._open[0], self._open[1], end, raw.lower()))
            self.found_text.append(raw)
            self.found_ids.append(self._open_id)
            self._open = None


SECTION_MAX = 200
# A page this large is not searched by heading: the parse is linear but not free.
SECTION_PAGE_MAX = 2_000_000


def _region(body: str, heads: list, i: int) -> str:
    """The HTML of heading `i` and what follows it, to the next heading of the
    same or a higher rank."""
    level, start = heads[i][0], heads[i][1]
    stop = len(body)
    for later_level, later_start, _, _ in heads[i + 1 :]:
        if later_level <= level:
            stop = later_start
            break
    return body[start:stop]


def _headings(body: str) -> _Headings:
    parser = _Headings(body)
    parser.feed(body)
    parser.close()
    return parser


def _section_slices(body: str, section: str) -> list[str]:
    """The HTML of every heading named `section` (case and spacing folded) and
    what follows it, to the next heading of the same or a higher rank. The
    heading itself is included, so a quote that is the heading's own words is
    found under its own section."""
    want = " ".join(section.split()).lower()
    heads = _headings(body).found
    return [_region(body, heads, i) for i, h in enumerate(heads) if h[3] == want]


def _tight(value: str) -> str:
    """Lowercased with every space removed.

    Tag stripping turns `<em>midnight</em>.` into `midnight .`, so a sentence
    that only gained emphasis would read as changed against a quote taken
    before it -- the exact false alarm this must not raise, since the words
    the reader selected have not moved at all. Neither side's spacing is
    something a reader chose, so neither side keeps it.
    """
    return "".join(value.split()).lower()


def _state_against(quote: str, words: str, tight_words: str) -> str:
    """`quote_state`'s comparison, against words already extracted.

    Separate because `text.searchable` walks the whole page: a thread of
    twenty comments would walk it twenty times if each row called
    `quote_state` itself. Same rule, one reading of the page.
    """
    needle = " ".join(quote.split()).lower()
    if not needle:
        return ""
    if needle in words or _tight(quote) in tight_words:
        return "current"
    return "changed"


def edited_in_place(anchor: dict, quote: str, tight_words: str) -> bool:
    """Is a quote that is no longer on the page still recognisable in an edit:
    the anchor's prefix or suffix, or half the quote itself, survives?"""
    hints = [
        _tight(anchor.get("prefix") or "")[-CONTEXT_CHARS:],
        _tight(anchor.get("suffix") or "")[:CONTEXT_CHARS],
    ]
    folded = _tight(quote)
    half = len(folded) // 2
    hints += [folded[:half], folded[half:]]
    return any(len(h) >= CONTEXT_MIN and h in tight_words for h in hints)


def _clean(value: str) -> str:
    """Collapsed, capped and printable: every character `str.isprintable` refuses
    (format characters such as a zero-width joiner or soft hyphen, private use,
    unassigned, lone surrogates) is dropped, so the stored heading is safe on a
    terminal and a page that uses such characters still pins."""
    kept = "".join(ch for ch in value if ch.isprintable() or ch.isspace())
    return text.sanitize(kept, HEADING_MAX).strip()


class PageIndex:
    """A page body read ONCE for everything `resolve` asks of its HTML: the
    headings (parsed on first use), the regions of the ids it was told to
    expect (one pass), and the visible words of each region (memoised). A
    listing builds one per (page, version) and hands it to every pin on it."""

    def __init__(self, body: str, ids: set[str] | frozenset[str] = frozenset()):
        self.body = body
        self.small = len(body) <= SECTION_PAGE_MAX
        self._ids = frozenset(ids)
        self._located: dict | None = None
        self._heads: _Headings | None = None
        self._words: dict[tuple, tuple[str, bool]] = {}

    @property
    def heads(self) -> _Headings:
        if self._heads is None:
            self._heads = _headings(self.body)
        return self._heads

    def id_span(self, element_id: str) -> tuple[int, int] | None:
        """(start, end) of the one element with this id, or None if the id is
        absent, repeated, unclosed or not an id this service addresses."""
        if self._located is None:
            self._located = sections.locate_many(self.body, set(self._ids))
        if element_id not in self._located and element_id not in self._ids:
            # An id the caller did not announce: found by itself, still correct.
            try:
                found = sections.locate(self.body, element_id)
            except sections.SectionError:
                return None
            return found.start, found.end
        found = self._located.get(element_id)
        return None if found is None else (found.start, found.end)

    def heading_spans(self, heading: str, heading_id: str) -> list[tuple]:
        """Where the pin's heading is now, as (key, start, stop): by id first,
        then by its text."""
        heads = self.heads.found
        if heading_id:
            for i, hid in enumerate(self.heads.found_ids):
                if hid == heading_id:
                    return [self._heading_span(i)]
            span = self.id_span(heading_id)
            if span is not None:
                return [(("id", heading_id), *span)]
        if not heading:
            return []
        want = " ".join(heading.split()).lower()
        return [self._heading_span(i) for i, h in enumerate(heads) if h[3] == want]

    def _heading_span(self, i: int) -> tuple:
        """Heading `i` and what follows it, to the next heading of the same or a
        higher rank."""
        heads = self.heads.found
        level, start = heads[i][0], heads[i][1]
        stop = len(self.body)
        for later_level, later_start, _, _ in heads[i + 1 :]:
            if later_level <= level:
                stop = later_start
                break
        return ("h", i), start, stop

    def holds(self, quote: str, key: tuple, start: int, stop: int) -> bool | None:
        """Does this stretch of page hold the quote? None when it has too much
        text. The stretch's words are read once per index."""
        if key not in self._words:
            self._words[key] = text.visible(self.body[start:stop])
        words, cut = self._words[key]
        if cut:
            return None
        return _state_against(quote, words, _tight(words)) == CURRENT


def resolve(
    anchor: dict,
    quote: str,
    body: "str | PageIndex",
    facts: tuple[str, str] | None = None,
) -> tuple[str, str]:
    """(state, note) for a pin against the current `body`.

    Order of evidence, first hit wins, each step checked:
    1. the element id, present exactly once: `current` if its text still holds
       the quote, else `edited` (the id says it is the same section);
    2. the quote under its heading (by id, then by text): `current`; found only
       elsewhere on the page: `current` with a note saying it moved;
    3. the quote anywhere: `current`;
    4. the quote edited in place (prefix, suffix or half of it survives): `edited`;
    5. otherwise `outdated`.

    ("", "") when nothing is known: no evidence on the pin, or more page text
    than is searched. `note` is "" unless a pin moved.

    `body` may be a `PageIndex` already built for the page, so many pins on one
    page parse it once; a plain string gets an index of its own.

    `facts` is the page's (visible words, the same with spaces removed), when
    the caller has them already; `body` may then be "" for a pin that names no
    heading and no id, which are the only evidence read from the HTML."""
    anchor = anchor if isinstance(anchor, dict) else {}
    element_id = anchor.get("el_id") or anchor.get("id") or ""
    needle = " ".join(quote.split()).lower()
    if not needle and not element_id:
        return "", ""
    index = body if isinstance(body, PageIndex) else PageIndex(body, {element_id})
    if facts is None:
        words, cut = text.visible(index.body)
        if cut:
            return "", ""
        tight_words = _tight(words)
    else:
        words, tight_words = facts
    small = index.small
    if element_id and small:
        span = index.id_span(element_id)
        if span is not None:
            held = index.holds(quote, ("id", element_id), *span) if needle else True
            if held is None:
                return "", ""
            return (CURRENT if held else EDITED), ""
    if not needle:
        return OUTDATED, ""
    if _state_against(quote, words, tight_words) == CURRENT:
        heading = anchor.get("heading") or ""
        heading_id = anchor.get("heading_id") or ""
        if (heading or heading_id) and small:
            for key, start, stop in index.heading_spans(heading, heading_id):
                held = index.holds(quote, key, start, stop)
                if held is None:
                    return "", ""
                if held:
                    return CURRENT, ""
            return CURRENT, f'moved: no longer under "{heading or heading_id}"'
        return CURRENT, ""
    if edited_in_place(anchor, quote, tight_words):
        return EDITED, ""
    return OUTDATED, ""


def heading_for_quote(
    body: str, quote: str, section: str | None = None
) -> tuple[str, str]:
    """(heading, heading_id) a quote sits under: the nearest heading above it,
    found as the one stretch of page between two headings that holds it. "" for
    both when no single stretch does (the quote spans a heading, sits above the
    first one, or is repeated) -- and then `section`, the heading the caller
    named, is the heading when one was given."""
    named = _clean(section or "")
    if len(body) > SECTION_PAGE_MAX:
        return named, ""
    index = PageIndex(body)
    heads = index.heads
    found = heads.found[:_HEADINGS_MAX]
    hits = []
    for i, head in enumerate(found):
        stop = found[i + 1][1] if i + 1 < len(found) else len(body)
        if index.holds(quote, ("s", i), head[1], stop):
            hits.append(i)
    if len(hits) == 1:
        i = hits[0]
        hid = heads.found_ids[i]
        return _clean(heads.found_text[i]), hid if ID_RE.fullmatch(hid) else ""
    return named, ""
