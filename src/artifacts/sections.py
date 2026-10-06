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
from html.parser import HTMLParser

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


@dataclass(frozen=True)
class Located:
    tag: str
    start: int  # the "<" of the start tag
    inner_start: int  # just after the start tag's ">"
    inner_end: int  # the "<" of its end tag (== end when implicitly closed)
    end: int  # just after the end tag's ">"
    void: bool
    label: str

    def outer(self, body: str) -> str:
        return body[self.start : self.end]

    def inner(self, body: str) -> str:
        return body[self.inner_start : self.inner_end]


class _Walker(HTMLParser):
    """Tracks the open-element stack with absolute offsets into `src`."""

    def __init__(self, src: str, target: str | None = None):
        super().__init__(convert_charrefs=False)
        self.src = src
        self.target = target
        # Absolute offset of each line's first character, for getpos().
        self._lines = [0]
        for m in re.finditer("\n", src):
            self._lines.append(m.end())
        self.stack: list[list] = []
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
            self._current["inner_end"] = inner_end
            self._current["end"] = end
        if entry[4]:
            self._heading["done"] = True

    def _pop_to(self, index: int, at: int) -> None:
        """Implicitly close everything above `index` (exclusive) at `at`."""
        while len(self.stack) > index:
            self._close(self.stack.pop(), at, at)

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
        foreign = tag in _FOREIGN or any(e[0] in _FOREIGN for e in self.stack)
        closed = tag in _VOID or (foreign and raw.rstrip().endswith("/>"))
        is_target = self.target is not None and attr.get("id") == self.target
        if is_target:
            self._current = {
                "tag": tag,
                "start": start,
                "inner_start": inner_start,
                "inner_end": inner_start if closed else None,
                "end": inner_start if closed else None,
                "void": closed,
                "title": attr.get("data-section-title"),
            }
            self.matches.append(self._current)
        # The target's first heading names the part on his wall.
        is_heading = (
            tag in _HEADINGS
            and not closed
            and self._heading is None
            and len(self.matches) == 1
            and any(e[3] for e in self.stack)
        )
        if is_heading:
            self._heading = {"text": [], "done": False}
        if not closed:
            self.stack.append([tag, start, inner_start, is_target, is_heading])

    # `<x/>` reaches here, not handle_starttag; the default would also call
    # handle_endtag and record a stray end tag.
    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        at = self._pos()
        gt = self.src.find(">", at)
        end = gt + 1 if gt >= 0 else len(self.src)
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self._pop_to(index + 1, at)
                self._close(self.stack.pop(), at, end)
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
        self.feed(self.src)
        leftover = self.rawdata
        self.close()
        return leftover


def _clean_label(raw: str) -> str:
    import html as _html

    text = re.sub(r"\s+", " ", _html.unescape(raw)).strip()
    if len(text) > LABEL_MAX:
        text = text[: LABEL_MAX - 1].rstrip() + "…"
    return text


def locate(body: str, element_id: str) -> Located:
    """The one element whose id is `element_id`, or a SectionError."""
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
        label=label,
    )


def section_revision(outer_html: str) -> str:
    """A stable hash of a section's outerHTML -- what `if_section` compares."""
    return hashlib.sha256(outer_html.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def check_balanced(fragment: str) -> None:
    """Refuse a fragment that would leave the section's tags open or closed
    wrongly: an end tag with no start in the fragment (it would close the
    section or one of its ancestors), an element left open whose end tag is
    not optional, or a construct cut off mid-way (`<!--` or `<div` at the end,
    which would swallow what follows it)."""
    walker = _Walker(fragment)
    leftover = walker.finish()
    if walker.stray_end:
        raise UnbalancedError(
            f"the fragment closes </{walker.stray_end[0]}> without opening it, "
            "which would close the section or a part of the page around it. "
            "Nothing was changed."
        )
    still_open = [e[0] for e in walker.stack if e[0] not in _OPTIONAL_END]
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


@dataclass(frozen=True)
class Applied:
    body: str
    label: str  # the part's name, for his wall
    section_revision: str | None  # None after a remove: there is no section


def apply(
    body: str, element_id: str, op: str, fragment: str | None, *, if_section=None
) -> Applied:
    if op not in OPS:
        raise SectionError(f"op must be one of {', '.join(OPS)}")
    where = locate(body, element_id)
    if if_section is not None and section_revision(where.outer(body)) != if_section:
        raise SectionChanged()
    if op == "remove":
        return Applied(
            body=body[: where.start] + body[where.end :],
            label=where.label,
            section_revision=None,
        )
    if fragment is None:
        raise SectionError(f"op {op!r} needs html")
    if where.void:
        raise SectionError(
            f"<{where.tag} id={element_id!r}> is a void element and has no "
            "content to change; use remove, or address its container."
        )
    check_balanced(fragment)
    if op == "replace":
        cut = (where.inner_start, where.inner_end)
    elif op == "append":
        cut = (where.inner_end, where.inner_end)
    else:  # prepend
        cut = (where.inner_start, where.inner_start)
    new = body[: cut[0]] + fragment + body[cut[1] :]
    again = locate(new, element_id)
    # The label is taken from the RESULT, so a replace that renames the
    # heading shows the new name -- that is the section he will now see.
    return Applied(
        body=new,
        label=again.label,
        section_revision=section_revision(again.outer(new)),
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
