"""Mark up what changed between two versions of an HTML page.

Pure and dependency-free. ``render_changes`` returns the NEW page with inserted
text in ``<ins class="ar-diff-ins">``, deleted text in ``<del class="ar-diff-del">``
at the place it was removed, and a small chip where an element with no text
disappeared or a tag's attributes changed. Tags are never wrapped and deleted
tags are dropped, so the tree is always the new document's own.

The output is served into a browser, so two properties matter more than the
marks themselves:

* **No markup that neither page had.** The tokenizer follows the browser's own
  tokenizer for tags, comments and raw-text elements, so a mark is only ever
  placed where the browser sees a boundary between text and tags. Deleted text
  is escaped, and a ``<`` that starts no tag is written as ``&lt;``.
* **Bounded time.** The tokenizer is linear (an unclosed construct runs to the
  end of the page, as it does in a browser, and a failed search is never
  repeated), the input is capped, and the matcher charges its work against a
  budget. Past any bound the page falls back to the plain new document.
"""

import re
import time
from bisect import bisect_left
from collections import Counter
from difflib import Match, SequenceMatcher

MAX_TOKENS = 60_000
MAX_BYTES = 3 * 1024 * 1024  # the page cap
MAX_SCRIPT_SHARE = 0.7
# Inner-loop steps the matcher may take across all its recursive calls, and a
# wall-clock bound as belt and braces. About 0.1 µs a step.
MAX_MATCH_WORK = 5_000_000
MAX_MATCH_SECONDS = 1.5

REASON_LARGE = "too large to mark up changes"
REASON_SCRIPT = "this page draws itself with script"

_INS = '<ins class="ar-diff-ins">'
_DEL = '<del class="ar-diff-del">'
_CHIP = '<span class="ar-diff-chip">removed</span>'
_CHANGED = '<span class="ar-diff-chip">changed</span>'

_WS, _WORD, _TAG = 0, 1, 2

# --- tokenizer ------------------------------------------------------------------

# A start or end tag exactly as the HTML tokenizer reads it: a quote opens a value
# only after ``=``, a quote anywhere else is part of a name, and a tag with no
# closing ``>`` runs to the end of the input (the browser drops it). Atomic
# groups and possessive quantifiers keep the match linear.
_TAG_RE = re.compile(
    r"""
    </?[A-Za-z][^\t\n\f\r />]*+
    (?>
        [\t\n\f\r /]++
      | [^\t\n\f\r />][^\t\n\f\r />=]*+
        (?>
            [\t\n\f\r ]*+=[\t\n\f\r ]*+
            (?>"[^"]*+(?:"|\Z)|'[^']*+(?:'|\Z)|[^\t\n\f\r >"'][^\t\n\f\r >]*+)?
        )?
    )*+
    (?:(?P<gt>>)|\Z)
    """,
    re.VERBOSE,
)
_TEXT = re.compile(r"\s+|\S+")  # applied only to text between markup
_NAME = re.compile(r"</?([A-Za-z][^\t\n\f\r />]*)")
_SPACES = re.compile(r"[\t\n\f\r ]+")

# Elements whose content the tokenizer reads as text up to their end tag.
_RAWTEXT = frozenset(
    [
        "script",
        "style",
        "title",
        "textarea",
        "xmp",
        "iframe",
        "noembed",
        "noframes",
        "noscript",
        "plaintext",
    ]
)
_FOREIGN = frozenset({"svg", "math"})
_END_RE = {
    name: re.compile(rf"</{name}[\t\n\f\r />]", re.IGNORECASE) for name in _RAWTEXT
}
_SCRIPT_OPEN = re.compile(r"<script[\t\n\f\r />]", re.IGNORECASE)


class _Unsafe(Exception):
    """The page's tokens cannot be read the way every browser would read them."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Finder:
    """``str.find``/``re.search`` that never repeats a search it already did.

    Scans move forward, so remembering the last answer for each needle means a
    failed search (no closer after position p) is done once, not once per ``<``.
    """

    def __init__(self, s: str):
        self.s = s
        self.memo: dict[object, tuple[int, int]] = {}

    def _cached(self, key, pos):
        hit = self.memo.get(key)
        if hit is not None:
            start, res = hit
            if pos >= start and (res < 0 or pos <= res):
                return res
        return None

    def find(self, needle: str, pos: int) -> int:
        res = self._cached(needle, pos)
        if res is None:
            res = self.s.find(needle, pos)
            self.memo[needle] = (pos, res)
        return res

    def search(self, pattern: re.Pattern, pos: int) -> int:
        res = self._cached(pattern, pos)
        if res is None:
            m = pattern.search(self.s, pos)
            res = m.start() if m else -1
            self.memo[pattern] = (pos, res)
        return res


def _tokenize(s: str, strict: bool = True) -> tuple[list[str], list[int], bool]:
    """Split ``s`` into whole tags, whitespace runs and words, in linear time.

    Returns the tokens and, for each, the index of the ``<svg>``/``<math>`` token
    that opened the foreign region it sits in (or -1). Inside such a region the
    browser may read raw-text elements and CDATA either way, so they are only
    accepted where both readings agree; otherwise ``_Unsafe`` is raised. The
    last value says the final token is unterminated (it ran to the end of the
    input without its closer), so nothing may be written after it.

    ``strict=False`` reads an ambiguous construct the HTML way instead of
    raising: right for the old page, whose text is only ever written escaped.
    """
    toks: list[str] = []
    region: list[int] = []
    finder = _Finder(s)
    n = len(s)
    i = 0
    foreign: list[str] = []  # open <svg>/<math>, innermost last
    # Inside SVG/MathML, a <select> (whose parsing browsers now disagree on) or
    # a frameset, a raw-text start tag may or may not switch the tokenizer.
    in_select = frameset = False
    region_start = -1
    open_end = False
    while i < n:
        this_region = region_start
        open_end = False
        zone = bool(foreign) or in_select or frameset
        if s[i] != "<":
            k = finder.find("<", i)
            j = n if k < 0 else k
            run = _TEXT.findall(s, i, j)
            toks.extend(run)
            region.extend([region_start] * len(run))
            i = j
            continue
        elif s.startswith("<!--", i):
            if s.startswith("<!-->", i):
                j = i + 5
            elif s.startswith("<!--->", i):
                j = i + 6
            else:
                ends = [
                    e + len(c)
                    for c in ("-->", "--!>")
                    if (e := finder.find(c, i + 4)) >= 0
                ]
                j = min(ends) if ends else n
                open_end = not ends
        elif s.startswith("<![CDATA[", i):
            gt = finder.find(">", i + 9)
            cd = finder.find("]]>", i + 9)
            html_end = gt + 1 if gt >= 0 else n
            foreign_end = cd + 3 if cd >= 0 else n
            if html_end != foreign_end and zone and strict:
                raise _Unsafe(REASON_LARGE)
            j = html_end
            open_end = gt < 0
        else:
            c1 = s[i + 1] if i + 1 < n else ""
            c2 = s[i + 2] if i + 2 < n else ""
            if c1 in ("!", "?") or (c1 == "/" and c2 and c2 != ">" and not _alpha(c2)):
                gt = finder.find(">", i + 2)  # a bogus comment ends at the first ">"
                j = gt + 1 if gt >= 0 else n
                open_end = gt < 0
            elif c1 == "/" and c2 == ">":
                j = i + 3
            elif _alpha(c1) or (c1 == "/" and _alpha(c2)):
                m = _TAG_RE.match(s, i)
                j = m.end()
                open_end = m.group("gt") is None
                name = _NAME.match(s, i).group(1).lower()
                if name in _RAWTEXT and not open_end:
                    j, open_end = _raw_text_end(s, i, j, name, finder, zone, strict)
                elif name in _FOREIGN:
                    if c1 == "/":
                        if name in foreign:
                            del foreign[len(foreign) - 1 - foreign[::-1].index(name) :]
                            if not foreign:
                                region_start = -1
                    elif not _self_closing(s[i:j]):
                        if not foreign:
                            region_start = this_region = len(toks)
                        foreign.append(name)
                elif name == "select":
                    in_select = c1 != "/"
                elif name == "frameset" and c1 != "/":
                    frameset = True
            else:
                j = i + 1  # a "<" that starts nothing is text
        toks.append(s[i:j])
        region.append(this_region)
        i = j
    return toks, region, open_end


def _alpha(c: str) -> bool:
    return c.isascii() and c.isalpha()


# A "/>" whose "/" belongs to an unquoted attribute value does not self-close.
_SLASH_IN_VALUE = re.compile(r"""=[\t\n\f\r ]*(?:[^\t\n\f\r >"'][^\t\n\f\r >]*)?/>$""")


def _self_closing(tag: str) -> bool:
    return tag.endswith("/>") and not _SLASH_IN_VALUE.search(tag)


def _raw_text_end(s, i, j, name, finder, zone, strict) -> tuple[int, bool]:
    """End of a raw-text element whose start tag spans ``s[i:j]``, and whether
    it ran to the end of the input unterminated."""
    if s[i + 1] == "/":
        return j, False  # a stray end tag is just a tag
    n = len(s)
    if not strict:
        zone = False
    if name == "plaintext":
        if zone:
            raise _Unsafe(REASON_LARGE)
        return n, True
    e = finder.search(_END_RE[name], j)
    body = s[j:] if e < 0 else s[j:e]
    if zone and "<" in body:
        # Read as markup there by some parsers and as text by others.
        raise _Unsafe(REASON_SCRIPT if name == "script" else REASON_LARGE)
    if strict and name == "script" and "<!--" in body:
        k = body.find("<!--")
        if _SCRIPT_OPEN.search(body, k):
            # "<!-- <script>" makes the first "</script>" not end the script.
            raise _Unsafe(REASON_SCRIPT)
    if e < 0:
        return n, True
    m = _TAG_RE.match(s, e)
    return m.end(), m.group("gt") is None


def _kind(tok: str) -> int:
    if tok[0] == "<" and len(tok) > 1:
        return _TAG
    return _WS if tok.isspace() else _WORD


def _tag_name(tok: str) -> str | None:
    """Lower-case name of a start tag, or None for anything else."""
    if len(tok) > 1 and tok[0] == "<" and tok[1].isascii() and tok[1].isalpha():
        return _NAME.match(tok).group(1).lower()
    return None


def _end_name(tok: str) -> str | None:
    if tok.startswith("</") and len(tok) > 2 and tok[2].isascii() and tok[2].isalpha():
        return _NAME.match(tok).group(1).lower()
    return None


def _is_script(tok: str) -> bool:
    return _tag_name(tok) == "script"


def _is_opaque(tok: str) -> bool:
    return tok.startswith(("<!", "<?")) or _tag_name(tok) in _RAWTEXT


def _key(tok: str) -> str:
    """What a token is compared by: tags ignore how their attributes are spaced."""
    if tok[0] == "<" and len(tok) > 1 and not _is_opaque(tok):
        return _SPACES.sub(" ", tok)
    return tok


def _text(tok: str) -> str:
    return "&lt;" if tok == "<" else tok


def _escape_text(tok: str) -> str:
    """Escape deleted text. ``&`` is kept: an entity is inside one token, and an
    ``&`` can never join with the ``<`` of the markup that follows it."""
    return (
        tok.replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# --- fallback -------------------------------------------------------------------


def _fallback(new_html: str, reason: str) -> tuple[str, str]:
    banner = (
        f'<div class="ar-diff-banner">Showing the page as it now is: {reason}</div>'
    )
    at = 0
    if len(new_html) <= MAX_BYTES:
        try:
            toks, region, _ = _tokenize(new_html)
        except _Unsafe:
            toks, region = [], []
        pos = 0
        for tok, r in zip(toks, region, strict=True):
            pos += len(tok)
            if r < 0 and _tag_name(tok) == "body":
                at = pos
                break
    return new_html[:at] + banner + new_html[at:], reason


# --- matcher --------------------------------------------------------------------


class _OverBudget(Exception):
    pass


class _Matcher(SequenceMatcher):
    """``SequenceMatcher`` that charges every inner-loop step against a budget.

    ``find_longest_match`` is difflib's own loop (no junk, so its extension
    passes are no-ops), except that it bisects straight to the ``[blo, bhi)``
    window instead of stepping over the entries outside it.
    """

    def __init__(self, a, b):
        self._work = 0
        self._deadline = time.monotonic() + MAX_MATCH_SECONDS
        super().__init__(None, a, b, autojunk=False)

    def find_longest_match(self, alo=0, ahi=None, blo=0, bhi=None):
        a, b2j = self.a, self.b2j
        if ahi is None:
            ahi = len(a)
        if bhi is None:
            bhi = len(self.b)
        besti, bestj, bestsize = alo, blo, 0
        j2len: dict[int, int] = {}
        work = self._work
        checkpoint = work + 100_000
        for i in range(alo, ahi):
            js = b2j.get(a[i])
            newj2len: dict[int, int] = {}
            if js:
                lo_k = bisect_left(js, blo)
                hi_k = bisect_left(js, bhi, lo_k)
                work += hi_k - lo_k + 1
                if work > MAX_MATCH_WORK:
                    raise _OverBudget
                if work > checkpoint:
                    if time.monotonic() > self._deadline:
                        raise _OverBudget
                    checkpoint = work + 100_000
                get = j2len.get
                for j in js[lo_k:hi_k]:
                    k = newj2len[j] = get(j - 1, 0) + 1
                    if k > bestsize:
                        besti, bestj, bestsize = i - k + 1, j - k + 1, k
            else:
                work += 1
            j2len = newj2len
        self._work = work
        return Match(besti, bestj, bestsize)


# --- where marks may go ---------------------------------------------------------

_VOID = frozenset(
    [
        "area",
        "base",
        "basefont",
        "bgsound",
        "br",
        "col",
        "embed",
        "frame",
        "hr",
        "img",
        "input",
        "keygen",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    ]
)
_TABLE = frozenset({"table", "tbody", "thead", "tfoot", "tr", "colgroup"})
_SELECT = frozenset({"select", "option", "optgroup", "datalist"})
_LISTS = frozenset({"ul", "ol"})
_NO_TEXT_MARKS = _TABLE | _SELECT
_NO_SPAN_INSIDE = _TABLE | _SELECT | _LISTS | {"html", "head", "frameset"}
_NO_CHIP_AFTER_VOID = frozenset(
    [
        "area",
        "base",
        "basefont",
        "bgsound",
        "col",
        "frame",
        "keygen",
        "link",
        "meta",
        "param",
        "source",
        "track",
    ]
)


_HEAD = frozenset(
    [
        "html",
        "head",
        "title",
        "meta",
        "link",
        "base",
        "basefont",
        "bgsound",
        "style",
        "script",
        "noscript",
        "noframes",
        "template",
    ]
)


def _starts_body(tok: str) -> bool:
    """Whether the browser starts the body at this token (from the head)."""
    kind = _kind(tok)
    if kind != _TAG:
        return kind == _WORD
    if tok.startswith(("<!", "<?")):
        return False
    name = _tag_name(tok)
    if name is not None:
        return name not in _HEAD
    return _end_name(tok) in ("body", "html", "br")


class _Frame:
    __slots__ = ("last_close", "name", "start")

    def __init__(self, name: str, start: int):
        self.name = name
        self.start = start
        self.last_close: int | None = None  # tables/lists: end of the last cell


class _Places:
    """Walks the new page's element stack to decide where each mark may go.

    Browsers move anything that is not a cell out of table structure, and drop
    or move phrasing markup inside a ``<select>``, so a deletion that would land
    there is moved to the end of the nearest earlier cell or list item, or
    before the table or select.
    """

    def __init__(self, toks: list[str], region: list[int]):
        self.toks = toks
        self.region = region
        self.stack: list[_Frame] = []
        self.region_target: dict[int, tuple[int, bool]] = {}

    # stack upkeep ----------------------------------------------------------
    def _pop_to(self, k: int, at: int) -> None:
        while len(self.stack) > k:
            f = self.stack.pop()
            if f.name in ("td", "th", "li"):
                want = ("table",) if f.name != "li" else ("ul", "ol")
                for g in reversed(self.stack):
                    if g.name in want:
                        g.last_close = at
                        break

    def _close(self, names, stop, at: int) -> None:
        for k in range(len(self.stack) - 1, -1, -1):
            nm = self.stack[k].name
            if nm in names:
                self._pop_to(k, at)
                return
            if nm in stop:
                return

    def step(self, i: int) -> bool:
        """Apply token ``i``; True when it opened an element that stays open."""
        tok = self.toks[i]
        if self.region[i] >= 0 or _kind(tok) != _TAG or _is_opaque(tok):
            return False
        name = _tag_name(tok)
        if name is None:
            end = _end_name(tok)
            if end:
                for k in range(len(self.stack) - 1, -1, -1):
                    if self.stack[k].name == end:
                        self._pop_to(k, i)
                        break
            return False
        sections = {"table", "tbody", "thead", "tfoot"}
        if name in ("td", "th"):
            self._close({"td", "th"}, sections | {"tr"}, i)
        elif name == "tr":
            self._close({"td", "th"}, sections | {"tr"}, i)
            self._close({"tr"}, sections, i)
        elif name in ("tbody", "thead", "tfoot"):
            self._close({"td", "th"}, sections | {"tr"}, i)
            self._close({"tr"}, sections, i)
            self._close({"tbody", "thead", "tfoot"}, {"table"}, i)
        elif name == "li":
            self._close({"li"}, _LISTS, i)
        elif name == "option":
            self._close({"option"}, {"select", "optgroup", "datalist"}, i)
        elif name == "optgroup":
            self._close({"option"}, {"select", "optgroup", "datalist"}, i)
            self._close({"optgroup"}, {"select"}, i)
        if name in _VOID:
            return False
        self.stack.append(_Frame(name, i))
        return True

    # decisions -------------------------------------------------------------
    def top(self) -> str | None:
        return self.stack[-1].name if self.stack else None

    def text_marks_ok(self, i: int) -> bool:
        return not self._inside_region(i) and self.top() not in _NO_TEXT_MARKS

    def _inside_region(self, i: int) -> bool:
        return i < len(self.region) and self.region[i] >= 0 and self.region[i] != i

    def target(self, i: int) -> tuple[int, bool]:
        """Where a deletion meant for position ``i`` goes, and whether its text
        may stay (False: one chip instead)."""
        if self._inside_region(i):
            return self.region_target[self.region[i]]
        return self._resolve(i, len(self.stack))

    def note_region_start(self, i: int) -> None:
        if i < len(self.region) and self.region[i] == i:
            self.region_target[i] = self._resolve(i, len(self.stack))

    def _resolve(self, pos: int, k: int) -> tuple[int, bool]:
        if k == 0:
            return pos, True
        f = self.stack[k - 1]
        if f.name in _SELECT:
            while k > 1 and self.stack[k - 2].name in _SELECT:
                k -= 1
            return self._resolve(self.stack[k - 1].start, k - 1)
        if f.name in _LISTS:
            if f.last_close is not None:
                return f.last_close, True
            return self._resolve(f.start, k - 1)
        if f.name in _TABLE:
            outer = k
            table = None
            while outer > 0 and self.stack[outer - 1].name in _TABLE:
                outer -= 1
                if table is None and self.stack[outer].name == "table":
                    table = self.stack[outer]
            if table is not None and table.last_close is not None:
                return table.last_close, True
            spot, _ = self._resolve(self.stack[outer].start, outer)
            return spot, False
        return pos, True

    def chip_after_ok(self, name: str, pushed: bool, i: int) -> bool:
        if self._inside_region(i) or self.region[i] == i:
            return False
        if pushed:
            return name not in _NO_SPAN_INSIDE and name not in _RAWTEXT
        return (
            name in _VOID
            and name not in _NO_CHIP_AFTER_VOID
            and self.top() not in _NO_SPAN_INSIDE
        )


# --- the diff -------------------------------------------------------------------


def render_changes(old_html: str, new_html: str) -> tuple[str, str | None]:
    """Return ``(document, fallback_reason)``: the new page with changes marked."""
    for html in (old_html, new_html):
        if len(html) > MAX_BYTES or len(html.encode()) > MAX_BYTES:
            return _fallback(new_html, REASON_LARGE)
    try:
        old_toks, _, _ = _tokenize(old_html, strict=False)
        new_toks, region, open_end = _tokenize(new_html)
    except _Unsafe as e:
        return _fallback(new_html, e.reason)
    if len(old_toks) > MAX_TOKENS or len(new_toks) > MAX_TOKENS:
        return _fallback(new_html, REASON_LARGE)
    script_bytes = sum(
        len(t.encode()) for t in new_toks if t[:2].lower() == "<s" and _is_script(t)
    )
    if script_bytes and script_bytes > MAX_SCRIPT_SHARE * len(new_html.encode()):
        return _fallback(new_html, REASON_SCRIPT)
    try:
        return _mark(old_toks, new_toks, region, open_end), None
    except _OverBudget:
        return _fallback(new_html, REASON_LARGE)


def _mark(
    old_toks: list[str], new_toks: list[str], region: list[int], open_end: bool
) -> str:
    # Diff over non-whitespace tokens only, so whitespace changes never mark.
    a_idx = [i for i, t in enumerate(old_toks) if _kind(t) != _WS]
    b_idx = [i for i, t in enumerate(new_toks) if _kind(t) != _WS]
    a_seq = [_key(old_toks[i]) for i in a_idx]
    b_seq = [_key(new_toks[i]) for i in b_idx]

    # Trim the common ends: keeps a one-word edit on a huge page linear.
    lo = 0
    n = min(len(a_seq), len(b_seq))
    while lo < n and a_seq[lo] == b_seq[lo]:
        lo += 1
    hi = 0
    while hi < n - lo and a_seq[-1 - hi] == b_seq[-1 - hi]:
        hi += 1
    a_mid = a_seq[lo : len(a_seq) - hi]
    b_mid = b_seq[lo : len(b_seq) - hi]

    ins_at: set[int] = set()
    changed_at: set[int] = set()
    deleted_at: dict[int, list[str]] = {}
    if a_mid and b_mid:
        # The matcher's first call alone scans this many entries: when that is
        # over budget, say so now rather than after spending the budget.
        freq = Counter(b_mid)
        if sum(freq[t] for t in a_mid) > MAX_MATCH_WORK:
            raise _OverBudget
    if a_mid or b_mid:
        sm = _Matcher(a_mid, b_mid)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                continue
            for j in range(lo + j1, lo + j2):
                if _kind(b_seq[j]) == _WORD:
                    ins_at.add(b_idx[j])
            if op == "replace":
                changed_at.update(
                    _changed_tags(a_mid, i1, i2, b_mid, j1, j2, lo, b_idx)
                )
            if i2 > i1:
                b_all_text = (
                    all(_kind(t) != _TAG for t in b_mid[j1:j2])
                    if op == "replace"
                    else True
                )
                rendered = _render_deleted(
                    a_idx[lo + i1 : lo + i2], old_toks, b_all_text
                )
                if rendered:
                    pos = b_idx[lo + j1] if lo + j1 < len(b_idx) else len(new_toks)
                    deleted_at.setdefault(pos, []).append(rendered)

    # First pass: decide where every mark may legally sit.
    places = _Places(new_toks, region)
    insert_before: dict[int, list[str]] = {}
    after_tok: dict[int, list[str]] = {}
    ins_ok: set[int] = set()
    # Until the body starts, a deletion would start it early and change how the
    # page's own head tags are read, so deletions wait for the body.
    in_head = True
    held: list[str] = []

    def place(pos: int) -> None:
        if in_head:
            held.extend(deleted_at[pos])
            return
        spot, keep = places.target(pos)
        parts = deleted_at[pos] if keep else [_CHIP]
        insert_before.setdefault(spot, []).extend(parts)

    for i, tok in enumerate(new_toks):
        if tok[0] != "<" and tok.isspace():
            continue
        if region[i] == i:
            places.note_region_start(i)
        if i in deleted_at:
            place(i)
        if in_head and _starts_body(tok):
            in_head = False
            if held:
                where = after_tok if _tag_name(tok) == "body" else insert_before
                where.setdefault(i, []).extend(held)
                held = []
        if tok[0] != "<" or len(tok) == 1:
            if i in ins_at and places.text_marks_ok(i):
                ins_ok.add(i)
            continue
        pushed = places.step(i)
        if i in changed_at and places.chip_after_ok(_tag_name(tok), pushed, i):
            after_tok.setdefault(i, []).insert(0, _CHANGED)
    if len(new_toks) in deleted_at:
        place(len(new_toks))
    if held:
        insert_before.setdefault(len(new_toks), []).extend(held)
    if open_end:
        # The last token runs to the end unterminated: anything after it would
        # be read as part of it, so it all goes before it instead.
        last = len(new_toks) - 1
        after_tok.pop(last, None)
        tail = insert_before.pop(len(new_toks), [])
        if tail:
            insert_before.setdefault(last, []).extend(tail)

    # Second pass: write the new page with the marks in.
    out: list[str] = []
    pending_ws: list[str] = []
    ins_open = False

    def close_ins():
        nonlocal ins_open
        if ins_open:
            out.append("</ins>")
            ins_open = False

    def flush_ws():
        out.extend(pending_ws)
        pending_ws.clear()

    for i, tok in enumerate(new_toks):
        kind = _kind(tok)
        if kind == _WS:
            pending_ws.append(tok)
            continue
        if i in insert_before:
            close_ins()
            flush_ws()
            out.extend(insert_before[i])
        if kind == _WORD and i in ins_ok:
            flush_ws()
            if not ins_open:
                out.append(_INS)
                ins_open = True
            out.append(_text(tok))
        else:
            close_ins()
            flush_ws()
            out.append(_text(tok) if kind == _WORD else tok)
        if i in after_tok:
            out.extend(after_tok[i])
    close_ins()
    flush_ws()
    out.extend(insert_before.get(len(new_toks), ()))
    return "".join(out)


def _changed_tags(a_mid, i1, i2, b_mid, j1, j2, lo, b_idx) -> list[int]:
    """New start tags in a replaced run whose old twin had other attributes."""
    olds = [(_tag_name(t), t) for t in a_mid[i1:i2] if not _is_opaque(t)]
    olds = [(nm, t) for nm, t in olds if nm]
    found = []
    p = 0
    for j in range(j1, j2):
        tok = b_mid[j]
        name = _tag_name(tok)
        if not name or _is_opaque(tok):
            continue
        for q in range(p, len(olds)):
            if olds[q][0] == name:
                if olds[q][1] != tok:
                    found.append(b_idx[lo + j])
                p = q + 1
                break
    return found


def _render_deleted(idxs: list[int], old_toks: list[str], b_all_text: bool) -> str:
    """Deleted text runs in one ``<del>`` each; a chip if only tags went.

    Text is escaped, so nothing deleted can become markup again, and the page's
    own whitespace between deleted words is kept (it matters inside ``<pre>``).
    """
    parts: list[str] = []
    run: list[str] = []
    has_text = False
    prev = -1

    def end_run():
        if run:
            parts.append(_DEL + "".join(run) + "</del>")
            run.clear()

    for i in idxs:
        tok = old_toks[i]
        if _kind(tok) == _TAG:
            end_run()
            prev = -1
            continue
        has_text = True
        if run and prev >= 0:
            run.extend(old_toks[prev + 1 : i])  # whitespace only, between words
        run.append(_escape_text(tok))
        prev = i
    end_run()
    if (
        not has_text
        and b_all_text
        and any(_tag_name(old_toks[i]) and not _is_opaque(old_toks[i]) for i in idxs)
    ):
        return _CHIP
    return "".join(parts)
