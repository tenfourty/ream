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

import difflib
import hashlib
import html
import re

from ream import scanslots, sections, text
from ream.htmlbound import BoundedParser, TooComplex

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

# An element pin's evidence text, and the most of "what is there now" shown for
# an edited one.
QUOTE_MAX = 200
CURRENT_MAX = 500
# An element pin's evidence must be at least this many visible characters
# (spaces removed) before it is searched for in the page's text or labels: one
# or two letters are found almost anywhere and would read current.
ELEMENT_QUOTE_MIN = 4
# A tag name an anchor may record (lower case; a custom element past this is
# simply not named).
TAG_RE = re.compile(r"[a-z][a-z0-9-]{0,15}")
# An element's first start tag, and the attributes that name what has no text:
# an image, a chart (canvas, svg) or a labelled control.
_START_TAG = re.compile(r"""<[a-zA-Z](?:"[^"]*"|'[^']*'|[^>"'])*>""")
_LABEL_ATTRS = ("alt", "aria-label", "title")
_ATTR = {
    name: re.compile(
        r"(?<![\w-])" + name + r"""\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE
    )
    for name in _LABEL_ATTRS
}
_ANY_LABEL = re.compile(
    r"""(?<![\w-])(?:alt|aria-label|title)\s*=\s*(?:"([^"]*)"|'([^']*)')""",
    re.IGNORECASE,
)
# Labels looked at when an element with no id is found by its alt text.
_LABELS_MAX = 2000


class _Headings(BoundedParser):
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
    with scanslots.slot(len(body)):  # ScanBusy goes up: a busy server, not this page
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


# Approximate re-finding of an edited quote, inside the pin's own section only.
# SequenceMatcher.ratio() >= 0.8: roughly one word in five changed. Looser
# starts matching a different sentence of the same shape; tighter misses a
# reworded clause. Standard library only, and every input is capped so the cost
# is a fixed ceiling whatever the page holds.
FUZZY_RATIO = 0.8
FUZZY_QUOTE_MIN = 24  # characters, spaces folded: a shorter quote matches too much
FUZZY_WORDS_MIN = 4
FUZZY_SECTION_HTML = 200_000  # of a section's HTML read
FUZZY_SECTION_TEXT = 4_000  # of its words compared
FUZZY_CANDIDATES = 1_500  # word windows scored per search
FUZZY_WORK = (
    1_500_000  # per listing: every window's size, plus the scored ones' size x quote's
)
FUZZY_SEARCHES = 20  # per listing


class FuzzyBudget:
    """What one listing may spend on approximate searches, shared by every
    PageIndex it builds: a hostile wall cannot multiply the per-search bound by
    its page count. Worst case about 0.15 s of matching for a whole listing."""

    def __init__(self, searches: int = FUZZY_SEARCHES, work: int = FUZZY_WORK) -> None:
        self.searches = searches
        self.work = work


# Finished searches, kept across requests: a page version never changes, so the
# same section and quote always give the same answer. Keyed by a hash of the
# words searched, not the words; cleared when full.
FUZZY_CACHE_MAX = 4_000
_FUZZY_CACHE: dict[tuple[bytes, str], str] = {}


def _fuzzy_text(section: str, needle: str, budget: FuzzyBudget) -> tuple[str, bool]:
    """(the stretch of `section` most like `needle` at or over FUZZY_RATIO or "",
    whether the search ran to its end). Windows of as many words as the quote
    has, one fewer and one more; every window is charged to `budget`, the cheap
    upper bounds go first, and a scored one is charged by its size too."""
    tokens = section[:FUZZY_SECTION_TEXT].split()
    want = len(needle.split())
    best, best_ratio, seen = "", FUZZY_RATIO, 0
    matcher = difflib.SequenceMatcher(None, autojunk=False)
    matcher.set_seq2(needle)
    for size in (want, want - 1, want + 1):
        for i in range(len(tokens) - size + 1):
            seen += 1
            window = " ".join(tokens[i : i + size])
            budget.work -= len(window) + len(needle)
            if seen > FUZZY_CANDIDATES or budget.work < 0:
                return best, False
            matcher.set_seq1(window.lower())
            if (
                matcher.real_quick_ratio() >= best_ratio
                and matcher.quick_ratio() >= best_ratio
            ):
                budget.work -= len(window) * len(needle)
                if budget.work < 0:
                    return best, False
                ratio = matcher.ratio()
                if ratio >= best_ratio and (ratio > best_ratio or not best):
                    best, best_ratio = window, ratio
    return best, True


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

    def __init__(
        self,
        body: str,
        ids: set[str] | frozenset[str] = frozenset(),
        budget: FuzzyBudget | None = None,
    ):
        self.body = body
        self.budget = budget or FuzzyBudget()
        self.small = len(body) <= SECTION_PAGE_MAX
        self._ids = frozenset(ids)
        self._located: dict | None = None
        self._heads: _Headings | None = None
        self._words: dict[tuple, tuple[str, bool]] = {}
        self._labels: list[str] | None = None
        self._plain: tuple[str, list[int], str] | None = None
        self._fuzzy: dict[tuple, str] = {}

    @property
    def labels(self) -> list[str]:
        """Every alt text, aria-label and title on the page, spaces removed and
        lowered (`_tight`), so an image pin with no id can be found by its words."""
        if self._labels is None:
            found = []
            for m in _ANY_LABEL.finditer(self.body):
                found.append(_tight(html.unescape(m.group(1) or m.group(2) or "")))
                if len(found) >= _LABELS_MAX:
                    break
            self._labels = found
        return self._labels

    def has_label(self, quote: str) -> bool:
        needle = _tight(quote)
        return bool(self.body and needle) and any(needle in t for t in self.labels)

    def plain(self) -> tuple[str, list[int], str]:
        """The page's readable text with its own capitals, and the same with
        every space removed and lowered, with where each kept character came
        from. Built once; ("", [], "") for a page with too much text."""
        if self._plain is None:
            words, cut = text.readable(self.body)
            if cut:
                self._plain = ("", [], "")
            else:
                at, kept = [], []
                for i, ch in enumerate(words):
                    if ch.isspace():
                        continue
                    low = ch.lower()
                    kept.append(low if len(low) == 1 else ch)
                    at.append(i)
                self._plain = (words, at, "".join(kept))
        return self._plain

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
            except TooComplex:
                raise
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

    def fuzzy(self, anchor: dict, quote: str) -> str:
        """The words under the pin's own heading that are approximately the
        quote, or "". Only that section is read, never the rest of the page, so
        a pin is never re-attached elsewhere; a quote too short to be told from
        other text is not searched; what it may spend is the listing's `budget`."""
        needle = " ".join(quote.split()).lower()[:QUOTE_MAX]
        heading = anchor.get("heading") or ""
        heading_id = anchor.get("heading_id") or ""
        if (
            not self.small
            or not (heading or heading_id)
            or len(needle) < FUZZY_QUOTE_MIN
            or len(needle.split()) < FUZZY_WORDS_MIN
        ):
            return ""
        key = (needle, heading, heading_id)
        if key not in self._fuzzy:
            found, done = "", True
            for _, start, stop in self.heading_spans(heading, heading_id)[:3]:
                words, _ = text.readable(
                    self.body[start : min(stop, start + FUZZY_SECTION_HTML)]
                )
                words = words[:FUZZY_SECTION_TEXT]
                ck = (hashlib.sha256(words.encode()).digest(), needle)
                if ck in _FUZZY_CACHE:
                    found = _FUZZY_CACHE[ck]
                else:
                    if self.budget.searches <= 0:
                        return ""
                    self.budget.searches -= 1
                    found, whole = _fuzzy_text(words, needle, self.budget)
                    if whole:
                        if len(_FUZZY_CACHE) >= FUZZY_CACHE_MAX:
                            _FUZZY_CACHE.clear()
                        _FUZZY_CACHE[ck] = found
                    else:
                        done = False
                if found:
                    break
            if not done and not found:
                return ""  # out of budget: not remembered as "no match"
            self._fuzzy[key] = found
        return self._fuzzy[key]

    def holds(self, quote: str, key: tuple, start: int, stop: int) -> bool | None:
        """Does this stretch of page hold the quote? None when it has too much
        text. The stretch's words are read once per index."""
        if key not in self._words:
            outer = self.body[start:stop]
            words, cut = text.visible(outer)
            if key[0] == "id":
                # An image or chart has no words: its alt text or label is what
                # the pin quoted.
                words = f"{words} {element_label(outer).lower()}".strip()
            self._words[key] = (words, cut)
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
    5. the quote approximately (ratio 0.8) within its own heading's section:
       `edited`, never `current`, never from elsewhere on the page;
    6. otherwise `outdated`.

    ("", "") when nothing is known: no evidence on the pin, more page text
    than is searched, or markup too long or too busy to read (`TooComplex`, from
    the parser of `htmlbound`). `note` is "" unless a pin moved.

    `body` may be a `PageIndex` already built for the page, so many pins on one
    page parse it once; a plain string gets an index of its own.

    `facts` is the page's (visible words, the same with spaces removed), when
    the caller has them already; `body` may then be "" for a pin that names no
    heading and no id, which are the only evidence read from the HTML."""
    try:
        return _resolve(anchor, quote, body, facts)
    except TooComplex:
        return "", ""


def _resolve(
    anchor: dict,
    quote: str,
    body: "str | PageIndex",
    facts: tuple[str, str] | None,
) -> tuple[str, str]:
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
    if anchor.get("kind") == "element" and len(_tight(quote)) < ELEMENT_QUOTE_MIN:
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
    if anchor.get("kind") == "element" and small and index.has_label(quote):
        return CURRENT, ""
    # An element's prefix and suffix are the words AROUND it, so their surviving
    # says the neighbours are there, not that the element was edited rather than
    # removed: only the element's own words count.
    hints = {} if anchor.get("kind") == "element" else anchor
    if edited_in_place(hints, quote, tight_words):
        return EDITED, ""
    if anchor.get("kind") != "element" and small and index.fuzzy(anchor, quote):
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
    try:
        heads = index.heads
    except TooComplex:
        return named, ""  # markup too tangled to read: as for a page too large
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


def _svg_title(outer: str) -> str:
    """The inner HTML of an SVG's first `<title>`, "" when it has none or never
    closes it. Three finds, so a page of unclosed `<title` openers costs one
    pass, where a lazy regex would rescan to the end from each of them."""
    low = outer.lower()
    at = low.find("<title")
    if at < 0:
        return ""
    gt = low.find(">", at)
    if gt < 0:
        return ""
    end = low.find("</title", gt)
    return outer[gt + 1 : end] if end >= 0 else ""


def element_label(outer: str) -> str:
    """What names an element that has no words of its own: the alt text,
    aria-label or title on its start tag, else an inline SVG's `<title>`, else
    the first such label inside it (a chart wrapper around an image). "" when
    there is none. `outer` is the element's HTML, starting at its `<`."""
    tag = _START_TAG.match(outer)
    start = tag.group(0) if tag else ""
    for name in _LABEL_ATTRS:
        m = _ATTR[name].search(start)
        if m:
            label = _clean(html.unescape(m.group(1) or m.group(2) or ""))
            if label:
                return label[:QUOTE_MAX]
    if start[:4].lower() == "<svg":
        inner = _svg_title(outer)
        if inner:
            label = _clean(html.unescape(re.sub(r"<[^>]*>", "", inner)))
            if label:
                return label[:QUOTE_MAX]
    for m in _ANY_LABEL.finditer(outer, len(start)):
        label = _clean(html.unescape(m.group(1) or m.group(2) or ""))
        if label:
            return label[:QUOTE_MAX]
    return ""


def element_quote(outer: str) -> str:
    """The evidence text of an element: its visible words (the first 200
    characters), or with none its alt text or label. "" when it has neither."""
    words, _ = text.readable(outer, QUOTE_MAX * 4)
    return _clean(words)[:QUOTE_MAX] or element_label(outer)


def heading_before(body: str, pos: int) -> tuple[str, str]:
    """(heading, heading_id) of the nearest heading that starts at or before
    offset `pos`: the one a pinned element sits under. ("", "") above the first
    heading, or on a page too large to search by heading."""
    if len(body) > SECTION_PAGE_MAX:
        return "", ""
    try:
        heads = _headings(body)
    except TooComplex:
        return "", ""  # markup too tangled to read: as for a page too large
    best = -1
    for i, head in enumerate(heads.found[:_HEADINGS_MAX]):
        if head[1] > pos:
            break
        best = i
    if best < 0:
        return "", ""
    hid = heads.found_ids[best]
    return _clean(heads.found_text[best]), hid if ID_RE.fullmatch(hid) else ""


def element_anchor(body: str, element_id: str, found) -> tuple[dict, str]:
    """(anchor, quote) for an agent pinning the element `found`
    (`sections.locate`) by its id: the server fills what a browser would have
    measured -- the tag, the heading it sits under, 32 characters of the page's
    words either side, and the quote (its text, or the label of an image or
    chart)."""
    outer = body[found.start : found.end]
    quote = element_quote(outer)
    anchor: dict = {"kind": "element", "id": element_id}
    tag = found.tag.lower()
    if TAG_RE.fullmatch(tag):
        anchor["tag"] = tag
    heading, heading_id = heading_before(body, found.start)
    if heading:
        anchor["heading"] = heading
    if heading_id:
        anchor["heading_id"] = heading_id
    label = element_label(outer)
    if label and label == quote:
        anchor["label"] = label
    near = 2000
    before, _ = text.readable(body[max(0, found.start - near) : found.start])
    after, _ = text.readable(body[found.end : found.end + near])
    if before[-CONTEXT_CHARS:].strip():
        anchor["prefix"] = before[-CONTEXT_CHARS:].lstrip()
    if after[:CONTEXT_CHARS].strip():
        anchor["suffix"] = after[:CONTEXT_CHARS].rstrip()
    return anchor, quote


def current_quote(anchor: dict, quote: str, body: "str | PageIndex") -> str:
    """What stands where an EDITED pin's words were, so a reader sees both the
    original and the current: an element's text now, or for a quote the words
    now between its prefix and its suffix. "" when neither can be told -- never
    a guess from elsewhere on the page, so it cannot show words the pin was not
    about. Capped at 500 characters."""
    try:
        return _current_quote(anchor, quote, body)
    except TooComplex:
        return ""  # markup too tangled to read: neither can be told


def _current_quote(anchor: dict, quote: str, body: "str | PageIndex") -> str:
    anchor = anchor if isinstance(anchor, dict) else {}
    element_id = anchor.get("el_id") or anchor.get("id") or ""
    index = body if isinstance(body, PageIndex) else PageIndex(body, {element_id})
    if not index.small or not index.body:
        return ""
    if element_id and anchor.get("kind") == "element":
        span = index.id_span(element_id)
        if span is not None:
            return element_quote(index.body[span[0] : span[1]])
    pre = _tight(anchor.get("prefix") or "")[-CONTEXT_CHARS:]
    suf = _tight(anchor.get("suffix") or "")[:CONTEXT_CHARS]
    if len(pre) < CONTEXT_MIN or len(suf) < CONTEXT_MIN:
        return _fuzzy_current(anchor, quote, index)
    words, at, tight = index.plain()
    if tight.count(pre) != 1:
        return _fuzzy_current(anchor, quote, index)
    i = tight.index(pre) + len(pre)
    j = tight.find(suf, i)
    if j < 0:
        return _fuzzy_current(anchor, quote, index)
    between = words[at[i - 1] + 1 : at[j]].strip()
    if not between or len(between) > 2 * len(quote) + 120:
        return _fuzzy_current(anchor, quote, index)
    return text.sanitize(between, CURRENT_MAX).strip()


def _fuzzy_current(anchor: dict, quote: str, index: "PageIndex") -> str:
    """The approximate match under the pin's heading, as `anchor_current`, when
    the words between its prefix and suffix cannot be told."""
    if anchor.get("kind") == "element":
        return ""
    return text.sanitize(index.fuzzy(anchor, quote), CURRENT_MAX).strip()
