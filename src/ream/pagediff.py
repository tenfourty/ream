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

import hashlib
import os
import re
import subprocess
import sys
import threading
import time
from bisect import bisect_left
from collections import Counter, OrderedDict
from difflib import Match, SequenceMatcher
from html.parser import HTMLParser

MAX_TOKENS = 60_000
# The diff's own cap, below the page cap since that rose to 8 MiB (15 Oct 2026):
# a bigger page shows "too large to mark up changes" instead of a marked-up diff.
MAX_BYTES = 3 * 1024 * 1024
MAX_SCRIPT_SHARE = 0.7
# Inner-loop steps the matcher may take across all its recursive calls, and a
# wall-clock bound as belt and braces. About 0.1 µs a step.
MAX_MATCH_WORK = 5_000_000
MAX_MATCH_SECONDS = 1.5

REASON_LARGE = "too large to mark up changes"
REASON_SCRIPT = "this page draws itself with script"
REASON_MIXED = "this page uses markup browsers read differently"
REASON_FAILED = "could not mark up changes"

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
                raise _Unsafe(REASON_MIXED)
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


_LAST_SPACE = re.compile(r"[\t\n\f\r ][^\t\n\f\r ]*\Z")
_UNQUOTED_EQ = re.compile(r"""=(?!["'])""")


def _self_closing(tag: str) -> bool:
    """A trailing "/>" self-closes unless its "/" ends an unquoted value.

    Only the last attribute can own that "/", and an unquoted value holds no
    whitespace, so only the text after the last space is examined (linear).
    Unsure cases answer "not self-closing", which keeps the foreign region open:
    the cautious reading.
    """
    if not tag.endswith("/>"):
        return False
    body = tag[:-2]
    m = _LAST_SPACE.search(body)
    if m is None:
        seg, before = body, ""
    else:
        seg, before = body[m.start() + 1 :], body[: m.start()]
    if _UNQUOTED_EQ.search(seg + "/"):
        return False  # "a=b/>", "a=/>", "a=b=c/>"
    tail = before.rstrip("\t\n\f\r ")
    return not (tail.endswith("=") and (not seg or seg[0] not in "\"'"))


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
            raise _Unsafe(REASON_MIXED)
        return n, True
    e = finder.search(_END_RE[name], j)
    body = s[j:] if e < 0 else s[j:e]
    if zone and "<" in body:
        # Read as markup there by some parsers and as text by others.
        raise _Unsafe(REASON_MIXED)
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
    """The new page exactly as it is; the reason is shown beside it, not in it."""
    return new_html, reason


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
        # Text before a frameset would start a body and void the frameset.
        return name not in _HEAD and name not in ("frameset", "frame")
    return _end_name(tok) in ("body", "html", "br")


_FORMATTING = frozenset(
    "a b big code em font i nobr s small strike strong tt u".split()  # noqa: SIM905
)
_MARKER = frozenset("td th caption applet object marquee template".split())  # noqa: SIM905
MAX_PLACE_STEPS = 2_000_000
# Start tags before which the browser does not clone formatting elements back.
_NO_REOPEN = frozenset(
    "address article aside blockquote center details dialog dir div dl fieldset "  # noqa: SIM905
    "figcaption figure footer header hgroup main menu nav ol p search section "
    "summary ul h1 h2 h3 h4 h5 h6 pre listing form li dd dt plaintext table hr "
    "param source track textarea caption col colgroup frame frameset head tbody "
    "td tfoot th thead tr html body base basefont bgsound link meta noframes "
    "script style template title".split()
)


class _Frame:
    __slots__ = ("depth", "last_close", "listed", "name", "start")

    def __init__(self, name: str, start: int, depth: int):
        self.name = name
        self.start = start
        self.depth = depth  # index in the stack
        self.last_close: int | None = None  # tables/lists: end of the last cell
        self.listed = False  # on the active formatting list


class _Places:
    """Walks the new page's element stack to decide where each mark may go.

    Browsers move anything that is not a cell out of table structure, and drop
    or move phrasing markup inside a ``<select>``, so a deletion that would land
    there is moved to the end of the nearest earlier cell or list item, or
    before the table or select. Frames are also indexed by name, so no lookup
    walks the stack, and every walk that remains is charged to a budget.
    """

    def __init__(self, toks: list[str], region: list[int]):
        self.toks = toks
        self.region = region
        self.stack: list[_Frame] = []
        self.by_name: dict[str, list[_Frame]] = {}
        self.region_target: dict[int, tuple[int, bool]] = {}
        self.steps = 0
        # The active formatting list: frames, with None for a marker. A listed
        # frame that is no longer open is one the browser clones back in at the
        # next text or inline start tag.
        self.afe: list[_Frame | None] = []

    def _charge(self, n: int = 1) -> None:
        self.steps += n
        if self.steps > MAX_PLACE_STEPS:
            raise _OverBudget

    # stack upkeep ----------------------------------------------------------
    def _innermost(self, names) -> _Frame | None:
        best = None
        for nm in names:
            frames = self.by_name.get(nm)
            if frames and (best is None or frames[-1].depth > best.depth):
                best = frames[-1]
        return best

    def _push(self, name: str, i: int) -> None:
        f = _Frame(name, i, len(self.stack))
        self.stack.append(f)
        self.by_name.setdefault(name, []).append(f)
        if name in _MARKER:
            self.afe.append(None)
        elif name in _FORMATTING:
            self.afe.append(f)
            f.listed = True

    def _pop_to(self, k: int, at: int) -> None:
        while len(self.stack) > k:
            self._charge()
            f = self.stack.pop()
            self.by_name[f.name].pop()
            if f.name in _MARKER:
                self._clear_to_marker()
            if f.name in ("td", "th", "li"):
                g = self._innermost(("table",) if f.name != "li" else _LISTS)
                if g is not None:
                    g.last_close = at

    def _clear_to_marker(self) -> None:
        while self.afe:
            self._charge()
            e = self.afe.pop()
            if e is None:
                return
            self._unlist(e)

    def _unlist(self, e: _Frame) -> None:
        e.listed = False

    def _open(self, f: _Frame) -> bool:
        return f.depth < len(self.stack) and self.stack[f.depth] is f

    def needs_reopen(self) -> bool:
        """The browser would clone formatting elements in at the next insert."""
        last = self.afe[-1] if self.afe else None
        return last is not None and not self._open(last)

    def reopen(self, i: int) -> None:
        """Clone closed formatting elements back in, as the browser does."""
        if not self.needs_reopen():
            return
        k = len(self.afe) - 1
        while k > 0 and self.afe[k - 1] is not None and not self._open(self.afe[k - 1]):
            self._charge()
            k -= 1
        for idx in range(k, len(self.afe)):
            self._charge()
            old = self.afe[idx]
            clone = _Frame(old.name, i, len(self.stack))
            clone.listed = True
            self.stack.append(clone)
            self.by_name.setdefault(old.name, []).append(clone)
            self.afe[idx] = clone

    def _close(self, names, stop, at: int) -> None:
        f = self._innermost(names)
        if f is None:
            return
        g = self._innermost(stop)
        if g is None or g.depth < f.depth:
            self._pop_to(f.depth, at)

    def _end_formatting(self, name: str) -> None:
        for k in range(len(self.afe) - 1, -1, -1):
            self._charge()
            e = self.afe[k]
            if e is None:
                return
            if e.name == name:
                del self.afe[k]
                self._unlist(e)
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
                if end in _FORMATTING:
                    self._end_formatting(end)
                frames = self.by_name.get(end)
                if frames:
                    self._pop_to(frames[-1].depth, i)
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
        if name not in _NO_REOPEN:
            self.reopen(i)
        if name in _VOID:
            return False
        self._push(name, i)
        return True

    # decisions -------------------------------------------------------------
    def top(self) -> str | None:
        return self.stack[-1].name if self.stack else None

    def _outer_select(self) -> _Frame | None:
        best = None
        for nm in _SELECT:
            frames = self.by_name.get(nm)
            if frames and (best is None or frames[0].depth < best.depth):
                best = frames[0]
        return best

    def text_marks_ok(self, i: int) -> bool:
        return (
            not self._inside_region(i)
            and self.top() not in _NO_TEXT_MARKS
            and self._outer_select() is None
        )

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
        keep = True
        sel = self._outer_select()
        if sel is not None and sel.depth < k:
            pos, k = sel.start, sel.depth  # never inside a select, at any depth
        while k > 0:
            self._charge()
            f = self.stack[k - 1]
            if f.name in _LISTS:
                if f.last_close is not None:
                    return f.last_close, keep
                pos, k = f.start, k - 1
                continue
            if f.name in _TABLE:
                outer = k
                table = None
                while outer > 0 and self.stack[outer - 1].name in _TABLE:
                    self._charge()
                    outer -= 1
                    if table is None and self.stack[outer].name == "table":
                        table = self.stack[outer]
                if table is not None and table.last_close is not None:
                    return table.last_close, keep
                keep = False
                pos, k = self.stack[outer].start, outer
                continue
            break
        return pos, keep

    def chip_after_ok(self, name: str, pushed: bool, i: int) -> bool:
        if self._inside_region(i) or self.region[i] == i:
            return False
        if self._outer_select() is not None:
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
    # When the browser would clone closed formatting elements (say an
    # <i onclick> that a </p> closed early) in at the next insert, a mark put
    # there would pull the clone around the blocks that follow; marks wait for
    # the page's own next text instead.
    fmt_held: list[str] = []

    def place(pos: int) -> None:
        if in_head:
            held.extend(deleted_at[pos])
            return
        spot, keep = places.target(pos)
        parts = deleted_at[pos] if keep else [_CHIP]
        if spot == pos and places.needs_reopen():
            fmt_held.extend(parts)
            return
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
        is_text = tok[0] != "<" or len(tok) == 1
        if fmt_held and (is_text or _tag_name(tok) not in _NO_REOPEN | {None}):
            insert_before[i] = fmt_held + insert_before.get(i, [])
            fmt_held = []
        if is_text:
            places.reopen(i)
            if i in ins_at and places.text_marks_ok(i):
                ins_ok.add(i)
            continue
        pushed = places.step(i)
        if i in changed_at and places.chip_after_ok(_tag_name(tok), pushed, i):
            if places.needs_reopen():
                fmt_held.insert(0, _CHANGED)
            else:
                after_tok.setdefault(i, []).insert(0, _CHANGED)
    if len(new_toks) in deleted_at:
        place(len(new_toks))
    if held or fmt_held:
        insert_before.setdefault(len(new_toks), []).extend(held + fmt_held)
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
    """New start tags in a replaced run whose old twin had other attributes.

    Twins pair in order: each new tag takes the first unused old tag of its
    name that comes after the previous pairing. Old tags are indexed by name
    with a pointer per name, so the pairing is linear.
    """
    by_name: dict[str, list[tuple[int, str]]] = {}
    for q in range(i1, i2):
        t = a_mid[q]
        nm = _tag_name(t)
        if nm and not _is_opaque(t):
            by_name.setdefault(nm, []).append((q, t))
    ptr: dict[str, int] = {}
    found = []
    last = -1
    for j in range(j1, j2):
        tok = b_mid[j]
        name = _tag_name(tok)
        if not name or _is_opaque(tok):
            continue
        olds = by_name.get(name)
        if not olds:
            continue
        k = ptr.get(name, 0)
        while k < len(olds) and olds[k][0] <= last:
            k += 1
        if k < len(olds):
            if olds[k][1] != tok:
                found.append(b_idx[lo + j])
            last = olds[k][0]
            k += 1
        ptr[name] = k
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


# --- the safe entry point -------------------------------------------------------


class _StartTags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, tuple]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, tuple(attrs)))

    handle_startendtag = handle_starttag


_OURS = {
    ("ins", (("class", "ar-diff-ins"),)),
    ("del", (("class", "ar-diff-del"),)),
    ("span", (("class", "ar-diff-chip"),)),
}


def _start_tags(doc: str) -> list[tuple[str, tuple]]:
    p = _StartTags()
    p.feed(doc)
    p.close()
    return p.tags


def _checked_render(old_html: str, new_html: str) -> tuple[str, str | None]:
    """``render_changes``, then prove the output added no tag of its own.

    The output's start tags, minus our marks, must be the new page's start tags
    in order with the same attributes. Any mismatch is a bug somewhere above,
    and it becomes the plain new page rather than a page with markup in it
    that neither version had.
    """
    doc, reason = render_changes(old_html, new_html)
    if reason is not None:
        return new_html, reason
    ours = [t for t in _start_tags(doc) if t not in _OURS]
    if ours != _start_tags(new_html):
        return new_html, REASON_FAILED
    return doc, None


_render = _checked_render  # what the worker runs; a module global so tests can swap it


def _serve(conn_in, conn_out) -> None:
    """Worker loop: render each request; never let an exception escape."""
    while True:
        try:
            func, old_html, new_html = conn_in.recv()
        except (EOFError, OSError):
            return
        try:
            result = func(old_html, new_html)
        except BaseException:  # noqa: BLE001 - any failure is a fallback
            result = (new_html, REASON_FAILED)
        try:
            conn_out.send(result)
        except (EOFError, OSError):
            return


def _worker_main() -> None:
    """Entry point of ``python -c``: the channel is stdin/stdout, so the
    parent's ``__main__`` is never re-imported (as spawn/forkserver would)."""
    from multiprocessing.connection import Connection

    out_fd = os.dup(1)
    os.dup2(2, 1)  # stray prints go to stderr, not into the channel
    conn_in = Connection(0, writable=False)
    conn_out = Connection(out_fd, readable=False)
    sys.path[:0] = [x for x in conn_in.recv() if x not in sys.path]
    _serve(conn_in, conn_out)


class _Worker:
    def __init__(self):
        from multiprocessing.connection import Connection

        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from ream.pagediff import _worker_main; _worker_main()",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            close_fds=True,
        )
        self.conn_out = Connection(os.dup(self.proc.stdin.fileno()), readable=False)
        self.conn_in = Connection(os.dup(self.proc.stdout.fileno()), writable=False)
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.conn_out.send(list(sys.path))
        self._exchange: threading.Thread | None = None

    def alive(self) -> bool:
        return self.proc.poll() is None

    def run(self, old_html: str, new_html: str, timeout: float, func=None):
        """The worker's answer within `timeout`, or TimeoutError.

        The whole exchange is bounded, the SEND included: a page larger than
        the pipe's buffer blocks `send` until the worker reads it, and a worker
        that is alive but not reading (stopped, wedged, starved) would hold
        the request -- and its slot -- for ever, with every process idle. That
        is the shape of a CI job seen idle for 35 minutes on 6 Oct 2026. The
        exchange runs in a thread; on timeout the caller kills the worker,
        which breaks the pipe and ends the thread."""
        outcome: dict = {}

        def exchange() -> None:
            try:
                self.conn_out.send((func or _render, old_html, new_html))
                if self.conn_in.poll(timeout):
                    outcome["result"] = self.conn_in.recv()
            except BaseException as exc:  # noqa: BLE001 -- reported below
                outcome["error"] = exc

        thread = threading.Thread(target=exchange, daemon=True)
        self._exchange = thread
        thread.start()
        thread.join(timeout)
        if "result" in outcome:
            return outcome["result"]
        if "error" in outcome and not thread.is_alive():
            raise outcome["error"]
        raise TimeoutError

    def kill(self) -> None:
        """Kill the worker, then close its pipes -- but never while an exchange
        thread may still be inside a call on them. Closing a descriptor another
        thread is using frees its NUMBER, and the next open() anywhere in the
        server (a client's socket, say) can be given it, so the stray thread
        would then write to or read from the wrong thing. Killing the worker
        breaks both pipes, which ends the thread almost at once; if it somehow
        does not, the pipes are left open (a small leak) rather than reused."""
        global _last_killed
        _last_killed = self.proc
        try:
            self.proc.kill()
            self.proc.wait(1)
        finally:
            thread = self._exchange
            if thread is not None and thread.is_alive():
                thread.join(_KILL_JOIN_SECONDS)
            if thread is None or not thread.is_alive():
                self.conn_in.close()
                self.conn_out.close()


_last_killed = None  # the most recently killed worker process, for tests
# How long kill() waits for an exchange thread to let go of the pipes.
_KILL_JOIN_SECONDS = 5.0
_IDLE: list[_Worker] = []
_IDLE_LOCK = threading.Lock()
_SLOTS = threading.BoundedSemaphore(4)
# {(old hash, new hash): ((document, reason), size in bytes)}, least recently
# used first. Capped in entries AND in bytes: near the 3 MB page cap, 64
# entries alone could hold hundreds of MB in a pod with 512Mi.
_CACHE: OrderedDict[tuple[str, str], tuple[tuple[str, str | None], int]] = OrderedDict()
_CACHE_LOCK = threading.Lock()
CACHE_SIZE = 64
CACHE_BYTES = 32 * 1024 * 1024
_cache_bytes = 0  # what the cache holds now, recounted on every store


def _remember(key: tuple[str, str], result: tuple[str, str | None]) -> None:
    global _cache_bytes
    size = len(result[0].encode("utf-8", "surrogatepass")) + len(result[1] or "")
    with _CACHE_LOCK:
        _CACHE.pop(key, None)
        if size <= CACHE_BYTES:
            _CACHE[key] = (result, size)
        total = sum(n for _r, n in _CACHE.values())
        while _CACHE and (len(_CACHE) > CACHE_SIZE or total > CACHE_BYTES):
            _k, (_r, n) = _CACHE.popitem(last=False)
            total -= n
        _cache_bytes = total


# How long a view waits for the worker before showing the page as it now is.
# Module-level so the test suite can widen it: a cold worker takes ~0.5 s even
# on a quiet machine, and a loaded CI box must not flip a test on 2 s.
TIMEOUT_SECONDS = 2.0


def render_changes_safely(
    old_html: str, new_html: str, *, timeout: float | None = None
) -> tuple[str, str | None]:
    """``render_changes`` in a separate process with a hard timeout.

    Whatever goes wrong (a stall, a crash, an output that fails its check), the
    answer is the new page unchanged with ``could not mark up changes``. Results
    are cached by the two pages' hashes, so a repeat view never recomputes --
    except a fallback caused by load (a timeout, or every worker busy), which
    says nothing about the pages and is tried again on the next view.
    """
    try:
        key = (
            hashlib.sha256(old_html.encode("utf-8", "surrogatepass")).hexdigest(),
            hashlib.sha256(new_html.encode("utf-8", "surrogatepass")).hexdigest(),
        )
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit is not None:
                _CACHE.move_to_end(key)
                return hit[0]
        result, transient = _run_in_worker(
            old_html, new_html, TIMEOUT_SECONDS if timeout is None else timeout
        )
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], str)
            or not (result[1] is None or isinstance(result[1], str))
        ):
            result = (new_html, REASON_FAILED)
        if transient:
            return result
        _remember(key, result)
        return result
    except Exception:  # noqa: BLE001 - any failure is a fallback
        return new_html, REASON_FAILED


def _run_in_worker(
    old_html: str,
    new_html: str,
    timeout: float,
    func=None,
    fallback=None,
    on_timeout=None,
):
    """``(result, transient)``: transient is True when the answer is not one to keep
    in the cache: it came from load, a broken worker or a worker that ran out of time,
    rather than from a finished comparison of the pages. `func` is another module-level
    function of two page texts for the same worker to run (partdiff.compare), with
    `fallback` as its answer when none comes. `on_timeout`, when given, is the answer
    for a worker that ran out of time; being slow on a busy machine says little about
    the pages, so it is transient too and is asked for again."""
    fallback = fallback if fallback is not None else (new_html, REASON_FAILED)
    if not _SLOTS.acquire(timeout=timeout):
        return fallback, True
    try:
        with _IDLE_LOCK:
            worker = _IDLE.pop() if _IDLE else None
        if worker is None or not worker.alive():
            worker = _Worker()
        try:
            result = worker.run(old_html, new_html, timeout, func)
        except BaseException as exc:  # noqa: BLE001 - timed out or broken
            worker.kill()  # it never serves again
            if on_timeout is not None and isinstance(exc, TimeoutError):
                return on_timeout, True
            return fallback, True
        with _IDLE_LOCK:
            _IDLE.append(worker)
        return result, False
    finally:
        _SLOTS.release()
