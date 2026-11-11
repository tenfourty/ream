"""Updating one section of a page: locate an element by its id in the LIVE
body and apply one operation to it, producing the whole new body.

Why this exists (docs/agent/specs/2026-10-06-section-updates-design.md):
every maintainer's real failure was a whole-page republish from a local copy
that had drifted from what was live, silently dropping content outside the
part they meant to change. Applying the change to what is live, on the
server, means nothing outside the section can be lost.

Pure functions only -- no database. `store.patch_section` reads the live
body, calls `apply`, and stores the result through the ordinary version path,
so every check a whole-page update faces runs on the whole resulting page.

A real HTML parser (the stdlib one, as `pagediff` uses) rather than a regex:
an id inside a comment, a script string or an attribute value of another
element is not an element, and a regex cannot tell.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ream.htmlbound import BoundedParser, TooComplex

OPS = ("replace", "append", "prepend", "remove")

# Elements that never have content or an end tag.
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

# Elements whose end tag HTML lets an author leave out. A fragment that
# leaves one of these open is still balanced (`<li>new row` is a whole row);
# one that leaves a <div> or a <script> open would swallow the rest of the
# page, and is refused.
_OPTIONAL_END = frozenset(
    [
        "li",
        "p",
        "dt",
        "dd",
        "tr",
        "td",
        "th",
        "option",
        "optgroup",
        "thead",
        "tbody",
        "tfoot",
        "colgroup",
        "rp",
        "rt",
    ]
)

# Starting one of the keys implicitly closes an open element in its value set
# when that element is on top of the stack -- the cases a hand-written status
# page actually uses (lists, tables, paragraphs).
_IMPLIED_CLOSE = {
    "li": {"li"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "tr": {"tr", "td", "th"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "option": {"option"},
}
_CLOSES_P = frozenset(
    [
        "address",
        "article",
        "aside",
        "blockquote",
        "details",
        "div",
        "dl",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "ul",
    ]
)
# Roots of foreign content, where `/>` self-closes (HTML spec, 13.2.6.5).
_FOREIGN = frozenset(["svg", "math"])
_HEADINGS = frozenset(["h1", "h2", "h3", "h4", "h5", "h6"])

# The longest a part name may be on the wall; a heading is a heading, not a
# paragraph.
LABEL_MAX = 80


class SectionError(Exception):
    """A refusal the caller can act on; `str(exc)` is the message to show."""


class NotFoundError(SectionError): ...


class AmbiguousError(SectionError): ...


class UnbalancedError(SectionError): ...


class TooComplexError(SectionError, TooComplex):
    """A tag in the markup is too long or too busy for the parser (`htmlbound`):
    where a section is, or whether a fragment is balanced, cannot be told. A
    refusal like the rest (the caller's 422), and a `TooComplex` for the readers
    that already stop on one."""


@dataclass(frozen=True)
class Located:
    tag: str
    start: int  # the "<" of the start tag
    inner_start: int  # just after the start tag's ">"
    inner_end: int  # the "<" of its end tag (== end when implicitly closed)
    end: int  # just after the end tag's ">"
    void: bool
    # Inside inline SVG or MathML, where `/>` self-closes: a fragment for it
    # is checked in that context.
    foreign: bool
    label: str

    def outer(self, body: str) -> str:
        return body[self.start : self.end]

    def inner(self, body: str) -> str:
        return body[self.inner_start : self.inner_end]


class _Walker(BoundedParser):
    """Tracks the open-element stack with absolute offsets into `src`."""

    def __init__(
        self,
        src: str,
        target: str | None = None,
        foreign: bool = False,
        wanted: frozenset[str] | None = None,
    ):
        super().__init__(convert_charrefs=False)
        self.src = src
        self.target = target
        # Many ids in one pass (`locate_many`): every match carries its "id".
        self.wanted = wanted
        # Absolute offset of each line's first character, for getpos().
        self._lines = [0]
        for m in re.finditer("\n", src):
            self._lines.append(m.end())
        # A fragment checked for a section inside SVG starts in that context:
        # a base entry the fragment must never close.
        self._base = ["svg", 0, 0, False, False] if foreign else None
        self.stack: list[list] = [self._base] if foreign else []
        # Open count per tag name, so an end tag that matches nothing is a
        # constant-time miss rather than a scan of the stack.
        self._open: dict[str, int] = {"svg": 1} if foreign else {}
        # Stack entries that are a target, for the heading check.
        self._targets_open = 0
        self.matches: list[dict] = []
        self.stray_end: list[str] = []
        self._heading: dict | None = None  # capturing the target's first heading

    def _pos(self) -> int:
        line, col = self.getpos()
        return self._lines[line - 1] + col

    # -- closing --------------------------------------------------------------
    # Stack entries: [tag, start, inner_start, is_target, is_heading]
    def _close(self, entry: list, inner_end: int, end: int) -> None:
        if entry[3]:
            entry[3]["inner_end"] = inner_end
            entry[3]["end"] = end
        if entry[4]:
            self._heading["done"] = True

    def _pop_to(self, index: int, at: int) -> None:
        """Implicitly close everything above `index` (exclusive) at `at`."""
        while len(self.stack) > index:
            self._close(self._pop(), at, at)

    def _pop(self) -> list:
        entry = self.stack.pop()
        self._open[entry[0]] -= 1
        if entry[3]:
            self._targets_open -= 1
        return entry

    # -- handlers ---------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        start = self._pos()
        raw = self.get_starttag_text() or ""
        inner_start = start + len(raw)
        if self.stack:
            top = self.stack[-1][0]
            if (tag in _IMPLIED_CLOSE and top in _IMPLIED_CLOSE[tag]) or (
                tag in _CLOSES_P and top == "p"
            ):
                self._pop_to(len(self.stack) - 1, start)
        attr = dict(attrs)
        # PARSED AS A BROWSER PARSES IT, so the element located here is the one
        # a reader sees. In HTML a `/>` on a non-void element is ignored --
        # `<div id="a"/>text` is an OPEN div holding "text". Only void
        # elements have no content. The one place `/>` does self-close is
        # foreign content (inline SVG and MathML: `<path/>`), as in a browser.
        foreign = tag in _FOREIGN or any(self._open.get(f) for f in _FOREIGN)
        closed = tag in _VOID or (foreign and raw.rstrip().endswith("/>"))
        if self.wanted is not None:
            is_target = attr.get("id") in self.wanted
        else:
            is_target = self.target is not None and attr.get("id") == self.target
        if is_target:
            self._current = {
                "tag": tag,
                "start": start,
                "inner_start": inner_start,
                "inner_end": inner_start if closed else None,
                "end": inner_start if closed else None,
                "void": closed,
                "foreign": foreign,
                "title": attr.get("data-section-title"),
                "id": attr.get("id"),
            }
            self.matches.append(self._current)
        # The target's first heading names the part on his wall.
        is_heading = (
            tag in _HEADINGS
            and not closed
            and self._heading is None
            and self.wanted is None
            and len(self.matches) == 1
            and self._targets_open
        )
        if is_heading:
            self._heading = {"text": [], "done": False}
        if not closed:
            self._open[tag] = self._open.get(tag, 0) + 1
            if is_target:
                self._targets_open += 1
            self.stack.append(
                [
                    tag,
                    start,
                    inner_start,
                    self._current if is_target else None,
                    is_heading,
                ]
            )

    # `<x/>` reaches here, not handle_starttag; the default would also call
    # handle_endtag and record a stray end tag.
    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        at = self._pos()
        gt = self.src.find(">", at)
        end = gt + 1 if gt >= 0 else len(self.src)
        if not self._open.get(tag):
            self.stray_end.append(tag)
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self._pop_to(index + 1, at)
                self._close(self._pop(), at, end)
                return
        self.stray_end.append(tag)

    def handle_data(self, data):
        h = self._heading
        if h is not None and not h["done"]:
            h["text"].append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")

    def finish(self) -> str:
        """Feed everything and return what the parser could not consume."""
        try:
            self.feed(self.src)
            leftover = self.rawdata
            self.close()
        except TooComplex:
            raise TooComplexError(
                "a tag in the markup is too long or has too many attributes to read,"
                " so where its elements end cannot be told -- nothing was changed."
            ) from None
        return leftover


def _clean_label(raw: str) -> str:
    """Heading text as a one-line name: entities decoded, whitespace
    collapsed, control and escape characters stripped (it reaches terminals
    through --versions and the update echo), capped at LABEL_MAX."""
    import html as _html

    from ream import text

    return text.sanitize(_html.unescape(raw), LABEL_MAX).strip()


# What an addressable id may contain. Real ids on these pages are slugs; a
# narrow charset keeps the id safe to echo into a version note, a URL and a
# terminal, with no escaping anywhere it travels.
ELEMENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def locate_many(body: str, ids: set[str]) -> dict[str, Located]:
    """One pass over `body`: the element for each id that is present exactly
    once and closed. An id that is absent, repeated, unclosed or not addressable
    is simply missing from the answer (`locate` says which, with an error)."""
    wanted = frozenset(i for i in ids if ELEMENT_ID.fullmatch(i))
    if not wanted:
        return {}
    walker = _Walker(body, wanted=wanted)
    walker.finish()
    by_id: dict[str, list[dict]] = {}
    for m in walker.matches:
        by_id.setdefault(m["id"], []).append(m)
    out = {}
    for element_id, found in by_id.items():
        if len(found) != 1 or found[0]["end"] is None:
            continue
        m = found[0]
        out[element_id] = Located(
            tag=m["tag"],
            start=m["start"],
            inner_start=m["inner_start"],
            inner_end=m["inner_end"],
            end=m["end"],
            void=m["void"],
            foreign=m["foreign"],
            label=element_id,
        )
    return out


def locate(body: str, element_id: str) -> Located:
    """The one element whose id is `element_id`, or a SectionError."""
    if not ELEMENT_ID.fullmatch(element_id):
        raise SectionError(
            "a section id may use only letters, digits, _ . : and -, up to 128 "
            "characters. Nothing was changed."
        )
    walker = _Walker(body, element_id)
    walker.finish()
    found = walker.matches
    if not found:
        raise NotFoundError(
            f"no element with id {element_id!r} on the live page -- nothing was "
            "changed. Fetch the page and check the id; a section update never "
            "applies anywhere else."
        )
    if len(found) > 1:
        raise AmbiguousError(
            f"{len(found)} elements have id {element_id!r} on the live page -- "
            "nothing was changed. An id must be unique to address a section; "
            "fix the page with a whole-page update first."
        )
    m = found[0]
    if m["end"] is None:
        # Never closed before the end of the page: everything after it would
        # be "the section", which is never what an author meant.
        raise UnbalancedError(
            f"the element with id {element_id!r} is never closed on the live "
            "page, so where its section ends is unknowable -- nothing was "
            "changed. Fix the page with a whole-page update first."
        )
    label = m["title"] or ""
    h = walker._heading
    if not label and h is not None:
        label = "".join(h["text"])
    label = _clean_label(label) or element_id
    return Located(
        tag=m["tag"],
        start=m["start"],
        inner_start=m["inner_start"],
        inner_end=m["inner_end"],
        end=m["end"],
        void=m["void"],
        foreign=m["foreign"],
        label=label,
    )


def section_revision(outer_html: str) -> str:
    """A stable hash of a section's outerHTML -- what `if_section` compares."""
    return hashlib.sha256(outer_html.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def check_balanced(fragment: str, *, foreign: bool = False) -> None:
    """Refuse a fragment that would leave the section's tags open or closed
    wrongly: an end tag with no start in the fragment (it would close the
    section or one of its ancestors), an element left open whose end tag is
    not optional, or a construct cut off mid-way (`<!--` or `<div` at the end,
    which would swallow what follows it)."""
    walker = _Walker(fragment, foreign=foreign)
    leftover = walker.finish()
    if walker._base is not None and walker._base not in walker.stack:
        walker.stray_end.insert(0, "svg")
    if walker.stray_end:
        raise UnbalancedError(
            f"the fragment closes </{walker.stray_end[0]}> without opening it, "
            "which would close the section or a part of the page around it. "
            "Nothing was changed."
        )
    still_open = [
        e[0]
        for e in walker.stack
        if e is not walker._base and e[0] not in _OPTIONAL_END
    ]
    if still_open:
        raise UnbalancedError(
            f"the fragment leaves <{still_open[0]}> open, which would swallow "
            "the rest of the page. Nothing was changed."
        )
    if "<" in leftover:
        raise UnbalancedError(
            "the fragment ends in the middle of a tag or comment, which would "
            "swallow the rest of the page. Nothing was changed."
        )


# --- the gate: the page as a browser reads it ---------------------------------
#
# The walker above finds byte offsets fast, but it is hand-rolled, and HTML's
# parsing rules (foreign-content integration points, CDATA, foster parenting,
# implied ends, duplicate attributes, <template>) are long. So it is not the
# gate. The gate is an INVARIANT checked with a spec-compliant parser
# (html5lib): parse the old and new pages as a browser would, and require that
# everything OUTSIDE the target element serialises identically. Whatever the
# walker got wrong, a change that reaches outside the section -- or a section
# the browser does not see where the walker did -- is refused, never written.

_PLACEHOLDER = "artifacts-section-placeholder"

# html5lib (pure Python) reads a page at about 4.6 s and 94 MiB per MiB of
# attribute-heavy markup, with no bound of its own, and an update parses twice. So
# every parse of a page goes through `_parse`, which refuses one over this size
# (in bytes, as the publish limit is), and `apply` caps what it would produce too.
# About 2 s for a 100 KB page; the pages agents keep updating run 40-140 KB
# (feedback, 6 Oct 2026); larger ones are republished whole.
SECTION_PAGE_MAX = 512 * 1024


class TooLargeError(SectionError): ...


def _parse(body: str):
    """The one way into html5lib: a page over SECTION_PAGE_MAX is refused here, so no
    caller can hand it a page it would take minutes and a gigabyte on."""
    size = len(body.encode("utf-8"))
    if size > SECTION_PAGE_MAX:
        raise TooLargeError(
            f"this page is {size // 1024} KB; the browser-faithful check works on "
            f"pages up to {SECTION_PAGE_MAX // 1024} KB -- read or republish the "
            "whole page instead"
        )
    import html5lib

    return html5lib.parse(body, treebuilder="etree", namespaceHTMLElements=False)


def _render(root) -> str:
    from html5lib import serializer, treewalkers

    walker = treewalkers.getTreeWalker("etree")
    out = serializer.HTMLSerializer(
        omit_optional_tags=False,
        quote_attr_values="always",
        alphabetical_attributes=True,
        resolve_entities=False,
    )
    return out.render(walker(root))


def _with_id(root, element_id: str) -> list[tuple]:
    """Every element whose id (the FIRST id attribute, as a browser keeps it)
    is `element_id`, as (element, parent, inside a <template>)."""
    found = []
    stack = [(root, None, False)]
    while stack:
        el, parent, in_template = stack.pop()
        if not isinstance(el.tag, str):
            continue  # a comment
        if el.attrib.get("id") == element_id:
            found.append((el, parent, in_template))
        inner = in_template or el.tag == "template"
        stack.extend((child, el, inner) for child in reversed(el))
    return found


def _slots_in(root) -> set[str]:
    """Question slots on real elements, as the page shim's querySelectorAll
    finds them -- never a match in a comment, CDATA or another attribute."""
    return {
        el.attrib["data-artifact-decision"]
        for el in root.iter()
        if isinstance(el.tag, str) and "data-artifact-decision" in el.attrib
    }


def _one_target(root, element_id: str):
    hits = _with_id(root, element_id)
    if not hits:
        raise NotFoundError(
            f"a browser finds no element with id {element_id!r} on the live "
            "page -- nothing was changed."
        )
    if len(hits) > 1:
        raise AmbiguousError(
            f"a browser finds {len(hits)} elements with id {element_id!r} -- "
            "nothing was changed. An id must be unique to address a section."
        )
    el, parent, in_template = hits[0]
    if in_template:
        raise SectionError(
            f"the element with id {element_id!r} is inside a <template>, which "
            "a reader never sees -- nothing was changed."
        )
    return el, parent


def _excise(el, parent) -> None:
    """Put a placeholder comment where `el` was, keeping the text after it."""
    from xml.etree import ElementTree

    mark = ElementTree.Comment(_PLACEHOLDER)
    mark.tail = el.tail
    index = list(parent).index(el)
    parent.remove(el)
    parent.insert(index, mark)


class OutsideChanged(SectionError): ...


def check_readable(body: str) -> None:
    """Refuse a page too large to read one section of, before any parsing: the
    section read checks the element with a browser-faithful parse, which is only
    run on a page up to SECTION_PAGE_MAX."""
    size = len(body.encode("utf-8"))
    if size > SECTION_PAGE_MAX:
        raise TooLargeError(
            f"this page is {size // 1024} KB; reading one section works on pages up "
            f"to {SECTION_PAGE_MAX // 1024} KB -- read the whole page instead"
        )


def check_target(body: str, element_id: str) -> None:
    """The browser sees exactly one element with this id, outside <template>.
    Raises TooLargeError for a page over SECTION_PAGE_MAX."""
    check_readable(body)
    _one_target(_parse(body), element_id)


def verify(old: str, new: str, element_id: str, op: str) -> tuple[set, set]:
    """Refuse unless `new` differs from `old`, as a browser reads both, only
    inside the target element. Returns (slots before, slots after)."""
    before_tree = _parse(old)
    target, parent = _one_target(before_tree, element_id)
    tag, attrs = target.tag, dict(target.attrib)
    slots_before = _slots_in(before_tree)
    _excise(target, parent)
    outside_before = _render(before_tree)

    after_tree = _parse(new)
    slots_after = _slots_in(after_tree)
    outside = OutsideChanged(
        f"this change would alter the page outside #{element_id} as a browser "
        "reads it -- nothing was changed."
    )
    if op == "remove":
        if _with_id(after_tree, element_id):
            raise outside
        mark = f"<!--{_PLACEHOLDER}-->"
        if outside_before.replace(mark, "") != _render(after_tree):
            raise outside
        return slots_before, slots_after
    hits = _with_id(after_tree, element_id)
    if len(hits) != 1 or hits[0][2]:
        raise outside
    el, el_parent, _ = hits[0]
    if el.tag != tag or dict(el.attrib) != attrs:
        raise outside
    _excise(el, el_parent)
    if _render(after_tree) != outside_before:
        raise outside
    return slots_before, slots_after


@dataclass(frozen=True)
class Applied:
    body: str
    label: str  # the part's name, for his wall
    section_revision: str | None  # None after a remove: there is no section
    # Question slots on real elements before and after, as a browser reads
    # the page (`data-artifact-decision` attributes, never a text match).
    slots_before: frozenset = frozenset()
    slots_after: frozenset = frozenset()


def apply(
    body: str, element_id: str, op: str, fragment: str | None, *, if_section=None
) -> Applied:
    size = len(body.encode("utf-8"))
    if size > SECTION_PAGE_MAX:
        raise TooLargeError(
            f"this page is {size // 1024} KB; section updates work on pages up to "
            f"{SECTION_PAGE_MAX // 1024} KB -- republish the whole page instead"
        )
    if op not in OPS:
        raise SectionError(f"op must be one of {', '.join(OPS)}")
    where = locate(body, element_id)
    if if_section is not None and section_revision(where.outer(body)) != if_section:
        raise SectionChanged()
    if op == "remove":
        new = body[: where.start] + body[where.end :]
        before, after = verify(body, new, element_id, op)
        return Applied(
            body=new,
            label=where.label,
            section_revision=None,
            slots_before=frozenset(before),
            slots_after=frozenset(after),
        )
    if fragment is None:
        raise SectionError(f"op {op!r} needs html")
    # The RESULT is capped too, before any parsing: a small page plus a huge
    # fragment would otherwise make the browser-faithful check parse megabytes
    # (security review, 6 Oct 2026).
    grown = size + len(fragment.encode("utf-8"))
    if grown > SECTION_PAGE_MAX:
        raise TooLargeError(
            f"with this change the page would be {grown // 1024} KB; section "
            f"updates work on pages up to {SECTION_PAGE_MAX // 1024} KB -- "
            "republish the whole page instead"
        )
    if where.void:
        raise SectionError(
            f"<{where.tag} id={element_id!r}> is a void element and has no "
            "content to change; use remove, or address its container."
        )
    check_balanced(fragment, foreign=where.foreign)
    if op == "replace":
        cut = (where.inner_start, where.inner_end)
    elif op == "append":
        cut = (where.inner_end, where.inner_end)
    else:  # prepend
        cut = (where.inner_start, where.inner_start)
    new = body[: cut[0]] + fragment + body[cut[1] :]
    before, after = verify(body, new, element_id, op)
    again = locate(new, element_id)
    # The label is taken from the RESULT, so a replace that renames the
    # heading shows the new name -- that is the section he will now see.
    return Applied(
        body=new,
        label=again.label,
        section_revision=section_revision(again.outer(new)),
        slots_before=frozenset(before),
        slots_after=frozenset(after),
    )


class SectionChanged(SectionError):
    def __init__(self):
        super().__init__("section changed, re-fetch")


# What the wall says happened, after the part's name. Jeremy's ruling (1a):
# the tile says WHICH PART changed, e.g. "Running now: added".
OP_WORDS = {
    "replace": "updated",
    "append": "added",
    "prepend": "added",
    "remove": "removed",
}
