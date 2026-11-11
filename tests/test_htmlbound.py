"""Every HTML parser in `src/` is a `ream.htmlbound.BoundedParser`, and none of them
can be made to run the standard parser's long match on a whole page.

Two kinds of test. The first reads the source (with `ast`, never by importing a module)
and fails for a class that subclasses `html.parser.HTMLParser` itself, under any
spelling of the import, or that overrides what the base replaces. The second feeds each
reader the pages that cost the standard parser seconds and a gigabyte: it must stop
at once, in a few copies of the page, with the answer its module chose for a page it
cannot read."""

from __future__ import annotations

import ast
import pathlib
import time
import tracemalloc
from html.parser import HTMLParser

import pytest
import scalecheck

from ream import anchors, forms, htmlbound, pagediff, partdiff, sections, text

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
BASE = SRC / "ream" / "htmlbound.py"
ROOTS = (SRC / "ream", SRC / "artifacts")

# --- the source walk ----------------------------------------------------------

# Every spelling that reaches the standard parser: the module, the class, the base
# class under it (`_markupbase.ParserBase`), however it is imported or looked up.
_PARSER_MODULES = {"html.parser", "_markupbase"}
_PARSER_NAMES = {"HTMLParser", "ParserBase"}

# What a subclass of the base may define: the handlers (and `__init__`). Anything else
# that `HTMLParser` has -- a method of its own, or a name it keeps on an instance
# (`interesting`, `rawdata`, ...) -- is how a subclass would read a page differently
# from the base, and so around its bound.
_HANDLERS = {"__init__", "unknown_decl"}


def _held_by_the_parser() -> set[str]:
    return set(dir(HTMLParser())) | set(dir(htmlbound.BoundedParser))


def _handlers(name: str) -> bool:
    return name in _HANDLERS or name.startswith("handle_")


def _spellings(tree: ast.AST) -> list[str]:
    """What in this module names the standard parser, however it is spelled."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")
                if (
                    alias.name in _PARSER_MODULES
                    or ".".join(top[:2]) in _PARSER_MODULES
                ):
                    found.append(f"imports {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in _PARSER_MODULES or module.startswith("html.parser."):
                found.append(f"imports from {module}")
            for alias in node.names:
                if module == "html" and alias.name == "parser":
                    found.append("imports parser from html")
                if alias.name in _PARSER_NAMES | _PARSER_MODULES:
                    found.append(f"imports {alias.name} from {module}")
        elif isinstance(node, ast.Name) and node.id in _PARSER_NAMES:
            found.append(f"names {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr in _PARSER_NAMES:
                found.append(f"names .{node.attr}")
            if node.attr == "parser" and getattr(node.value, "id", "") == "html":
                found.append("names html.parser")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _PARSER_NAMES | _PARSER_MODULES
        ):
            # importlib.import_module("html.parser"), getattr(m, "HTMLParser"), ...
            found.append(f"has the string {node.value!r}")
    return found


def _aliases(tree: ast.AST) -> dict[str, str]:
    """`from x import A as B` and `import x as B`: B -> A."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = alias.name.split(".")[-1]
    return out


def _base_names(cls: ast.ClassDef, aliases: dict[str, str]) -> set[str]:
    names = set()
    for base in cls.bases:
        if isinstance(base, ast.Name):
            names.add(aliases.get(base.id, base.id))
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _defined(cls: ast.ClassDef) -> list[str]:
    """Every name the class body binds, and every `self.name = ...` in its methods."""
    names: list[str] = []
    for node in ast.walk(cls):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.append(node.name)
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for part in ast.walk(target):
                    if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Store):
                        names.append(part.id)
                    elif (
                        isinstance(part, ast.Attribute)
                        and isinstance(part.ctx, ast.Store)
                        and getattr(part.value, "id", "") == "self"
                    ):
                        names.append(part.attr)
    return names


def violations(sources: dict[str, str], base: str = str(BASE)) -> list[str]:
    """What breaks the rule in these modules (path -> source), as lines to read."""
    trees = {path: ast.parse(source) for path, source in sources.items()}
    out: list[str] = []
    for path, tree in trees.items():
        if path != base:
            out += [f"{path}: {line}" for line in _spellings(tree)]
    # The classes that are the base, or come from it, by name, across modules.
    bounded = {"BoundedParser"}
    classes = [
        (path, cls, _aliases(tree))
        for path, tree in trees.items()
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef)
    ]
    grew = True
    while grew:
        grew = False
        for _path, cls, aliases in classes:
            if cls.name not in bounded and _base_names(cls, aliases) & bounded:
                bounded.add(cls.name)
                grew = True
    held = _held_by_the_parser()
    for path, cls, aliases in classes:
        if path == base or not _base_names(cls, aliases) & bounded:
            continue
        for name in _defined(cls):
            if name in held and not _handlers(name):
                out.append(f"{path}: {cls.name} defines {name}, which the parser has")
    return out


def _the_source() -> dict[str, str]:
    return {
        str(path): path.read_text()
        for root in ROOTS
        if root.is_dir()
        for path in sorted(root.rglob("*.py"))
    }


def test_no_class_of_src_reads_html_except_through_the_bounded_base():
    sources = _the_source()
    assert str(BASE) in sources and len(sources) > 20, "the walk did not find src"
    assert violations(sources) == []


def test_the_base_replaces_what_the_guard_says_it_replaces():
    """`htmlbound.REPLACED` is what a subclass may not define; it is the base's own."""
    for name in htmlbound.REPLACED:
        assert name in vars(htmlbound.BoundedParser)
        assert name in _held_by_the_parser()
        assert not _handlers(name)


def _only(source: str) -> list[str]:
    return violations({"a.py": source, "b.py": "class BoundedParser: ..."}, base="b.py")


_BASE = "from ream.htmlbound import BoundedParser\n"
_NOT_ALLOWED = [
    "from html.parser import HTMLParser\nclass A(HTMLParser): ...",
    "from html.parser import HTMLParser as P\nclass A(P): ...",
    "import html.parser as hp\nclass A(hp.HTMLParser): ...",
    "import html.parser\nclass A(html.parser.HTMLParser): ...",
    "import html\nclass A(html.parser.HTMLParser): ...",
    "from html import parser\nclass A(parser.HTMLParser): ...",
    "from html import parser as q\nx = q.locatetagend",
    "from _markupbase import ParserBase\nclass A(ParserBase): ...",
    "import importlib\nm = importlib.import_module('html.parser')",
    "m = __import__('html.parser')",
    "x = getattr(m, 'HTMLParser')",
    _BASE + "class A(BoundedParser):\n    def check_for_whole_start_tag(self, i): ...",
    _BASE.replace("Parser\n", "Parser as B\n")
    + "class A(B):\n    def parse_endtag(self, i): ...",
    _BASE + "class A(BoundedParser): ...\nclass C(A):\n    def goahead(self, end): ...",
    "from ream import htmlbound\nclass A(htmlbound.BoundedParser):\n    def parse_starttag(self, i): ...",
    _BASE
    + "class A(BoundedParser):\n    def __init__(self):\n        self.interesting = 1",
    _BASE + "class A(BoundedParser):\n    CDATA_CONTENT_ELEMENTS = ()",
]


@pytest.mark.parametrize("source", _NOT_ALLOWED)
def test_the_guard_refuses_each_way_around_the_base(source):
    """Without this, a guard that finds nothing would look like a clean tree."""
    assert _only(source) != []


def test_the_guard_lets_a_handler_through():
    source = (
        "from ream.htmlbound import BoundedParser\n"
        "class A(BoundedParser):\n"
        "    def __init__(self):\n"
        "        super().__init__(convert_charrefs=False)\n"
        "        self.found = []\n"
        "    def handle_starttag(self, tag, attrs):\n"
        "        self.found.append(tag)\n"
        "    handle_startendtag = handle_starttag\n"
    )
    assert _only(source) == []


# --- what each reader does with the pages that cost the standard parser a gigabyte -


MIB = 2**20

# (what comes first, what is repeated, what ends it): a ">" at the very end closes the
# first tag, so the standard parser's match runs there from the first tag. The second
# has a ">" in quotes at its start (no ">" in the tag is the measure); the third reads
# the same match for an end tag.
THREE = {
    "start tags": ("fetch( ", "<img ", ">"),
    "a > in quotes first": ('fetch( <img a=">" ', "x ", ">"),
    "end tags": ("fetch( ", "</a ", ">"),
}
FAST = 2.0  # seconds, on a page of 8 MiB; the standard parser takes 3 to 11
FEW_COPIES = 4  # times the page: the bound on what a read keeps at once


def _page(shape: str, chars: int = 8 * MIB, lead: str = "") -> str:
    first, unit, last = THREE[shape]
    return scalecheck.page(lead + first, unit, chars, last)


def _peak(read, page):
    """(what the read gave or raised, the most memory in use at once, in bytes)."""
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        try:
            gave = read(page)
        except Exception as exc:  # noqa: BLE001 - which one is the test's to say
            gave = exc
        return gave, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


# Each read: (a function of the page, the page's length it is given, what it gives
# for a page it cannot read). The length is what the module lets the read see: the
# publish limit for forms and sections, the caps of text, anchors and the diff.
def _forms_read(page):
    return forms.parse("<input name=a>" + page)


READS = {
    "forms": (_forms_read, 8 * MIB, forms.FormRefused),
    "sections.locate": (
        lambda page: sections.locate(page, "x"),
        8 * MIB,
        sections.SectionError,
    ),
    "sections.locate_many": (
        lambda page: sections.locate_many(page, {"x"}),
        8 * MIB,
        sections.SectionError,
    ),
    "anchors._headings": (anchors._headings, 8 * MIB, htmlbound.TooComplex),
    "pagediff._start_tags": (pagediff._start_tags, 8 * MIB, htmlbound.TooComplex),
    "partdiff._parts": (partdiff._parts, 8 * MIB, htmlbound.TooComplex),
    "partdiff.compare": (
        lambda page: partdiff.compare(page, page),
        pagediff.MAX_BYTES - 1,
        None,
    ),
    "text.visible": (text.visible, text.INPUT_LIMIT, None),
    "text.readable": (text.readable, text.INPUT_LIMIT, None),
    "anchors.heading_before": (
        lambda page: anchors.heading_before(page, 0),
        anchors.SECTION_PAGE_MAX,
        None,
    ),
    "anchors.resolve": (
        lambda page: anchors.resolve(
            {"kind": "quote", "heading": "x"}, "fetch", page, ("fetch", "fetch")
        ),
        anchors.SECTION_PAGE_MAX,
        None,
    ),
}


def _answer(name, page):
    read, _, raises = READS[name]
    try:
        return read(page)
    except Exception as exc:  # noqa: BLE001
        assert raises is not None and isinstance(exc, raises), repr(exc)
        return exc


def _chars(name: str) -> int:
    """The page's length for this read: what the module lets it see, so that the ">"
    that ends the page is inside it."""
    return READS[name][1]


@pytest.mark.parametrize("shape", sorted(THREE))
@pytest.mark.parametrize("name", sorted(READS))
@pytest.mark.scaling
def test_every_reader_gives_up_on_the_three_pages_at_once(name, shape):
    page = _page(shape, _chars(name))
    began = time.perf_counter()
    _answer(name, page)
    took = time.perf_counter() - began
    assert took < FAST, f"{took:.2f} s on {len(page) / MIB:.0f} MiB"


@pytest.mark.parametrize("shape", sorted(THREE))
@pytest.mark.parametrize("name", sorted(READS))
def test_every_reader_gives_up_on_the_three_pages_in_a_few_copies_of_them(name, shape):
    page = _page(shape, _chars(name))
    _, peak = _peak(lambda p: _answer(name, p), page)
    assert peak < FEW_COPIES * len(page), (
        f"{peak / MIB:.0f} MiB for {len(page) / MIB:.0f}"
    )


# What each module chose for a page it cannot read: the answer is the one it already
# gives for input that is bad, and no request gets a 500 from it.


@pytest.mark.parametrize("shape", sorted(THREE))
def test_forms_refuses_a_page_whose_forms_it_cannot_read(shape):
    with pytest.raises(forms.FormRefused, match="too long or has too many"):
        forms.parse("<input name=a>" + _page(shape))
    assert forms.warnings("<input name=a>" + _page(shape)) == []


@pytest.mark.parametrize("shape", sorted(THREE))
def test_sections_refuses_with_its_own_refusal(shape):
    page = _page(shape)
    with pytest.raises(sections.TooComplexError) as seen:
        sections.locate(page, "x")
    assert isinstance(seen.value, sections.SectionError)
    assert isinstance(seen.value, htmlbound.TooComplex)
    with pytest.raises(sections.SectionError):
        sections.check_balanced(page)


@pytest.mark.parametrize("shape", sorted(THREE))
def test_text_reads_a_page_it_cannot_parse_as_one_with_too_much_text(shape):
    page = _page(shape, text.INPUT_LIMIT)
    assert text.visible(page) == ("", True)
    assert text.readable(page) == ("", True)


@pytest.mark.parametrize("shape", sorted(THREE))
def test_anchors_know_nothing_of_a_page_they_cannot_read(shape):
    page = _page(shape, anchors.SECTION_PAGE_MAX)
    pin = {"kind": "quote", "heading": "x"}
    # The page's words given, so that it is the headings that cannot be read.
    assert anchors.resolve(pin, "fetch", page, ("fetch", "fetch")) == ("", "")
    assert anchors.resolve(pin, "fetch", page) == ("", "")
    assert anchors.heading_before(page, 0) == ("", "")
    assert anchors.heading_for_quote(page, "fetch", "Named") == ("Named", "")
    assert anchors.current_quote({"kind": "quote"}, "fetch", page) == ""
    with pytest.raises(htmlbound.TooComplex):
        anchors._section_slices(page, "x")


@pytest.mark.parametrize("shape", sorted(THREE))
def test_a_diff_of_a_page_it_cannot_read_is_the_page_and_a_reason(shape):
    page = _page(shape, pagediff.MAX_BYTES - 1)
    result = partdiff.compare(page, page)
    assert result["parts"] == []
    assert result["unknown_reason"] in {pagediff.REASON_LARGE, partdiff.REASON_FAILED}
    doc, reason = pagediff._checked_render(page, page)
    assert doc == page
    assert reason is None or isinstance(reason, str)


def test_the_threes_ends_are_inside_every_read():
    """A page whose ">" falls outside what the read sees is a page that proves nothing."""
    for name, (_, chars, _) in READS.items():
        for shape in THREE:
            page = _page(shape, chars)
            assert page.endswith(">") and len(page) == chars, name


# --- the standard parser's other long matches ---------------------------------------
#
# The three pages above are for the match that reads a tag. The parser also looks
# ahead, or loops, in every other place a page can open something and not close it:
# a comment, a declaration, a marked section, a processing instruction, a "bogus"
# comment, the end of a script, style or textarea, an entity. None of them is bounded
# by `BoundedParser` (and the one that decides where a tag ends is not what costs time
# here), so each is held to the same two claims on pages of 1 MiB and 8 MiB: the time
# grows with the page and not with its square (tests/scalecheck.py), and the most
# memory in use at once is a few copies of the page (tracemalloc), whatever the page
# is built of. Linear is not the same as quick: the shapes made of one tiny thing
# after another ("<" and "&<a>") cost the standard parser a call of its own each, so
# 8 MiB takes it seconds, not milliseconds; the bound is on how that grows.
#
# (what comes first, what is repeated, what ends it); `scalecheck.page` builds each.
OTHER = {
    # comments, and what is nearly the end of one
    "unclosed comment": ("<!--", "x", ""),
    "comments opened over and over": ("", "<!--", ""),
    "comment of dashes": ("<!--", "-", ""),
    "<!- flood": ("", "<!-", ""),
    "--! flood in a comment": ("<!-- ", "--!", ""),
    "--!- flood in a comment": ("<!-- ", "--!-", ""),
    "-- > flood in a comment": ("<!-- ", "-- >", ""),
    "<!--> flood": ("", "<!-->", ""),
    # declarations, marked sections, processing instructions
    "unclosed doctype": ("<!DOCTYPE", " x", ""),
    "doctypes opened over and over": ("", "<!DOCTYPE ", ""),
    "doctype with a quote left open": ('<!DOCTYPE x "', "y", ""),
    "unclosed CDATA": ("<![CDATA[", "x", ""),
    "CDATA opened over and over": ("", "<![CDATA[", ""),
    "CDATA closed over and over": ("", "<![CDATA[x]]>", ""),
    "CDATA of ] only": ("<![CDATA[", "]", ""),
    "<![ flood": ("", "<![", ""),
    "<![if unclosed": ("<![if x", " ", ""),
    "<![endif]> flood": ("", "<![endif]>", ""),
    "unclosed <?": ("<?", "x", ""),
    "<? flood": ("", "<?", ""),
    "<?x ? flood": ("<?x ", "?", ""),
    # "bogus comments": what the parser takes for a comment and that nothing closes
    "unclosed </ x": ("</ x", " x", ""),
    "</ x flood": ("", "</ x", ""),
    "unclosed <!x": ("<!x", "y", ""),
    "<!x flood": ("", "<!x", ""),
    "<! flood": ("", "<!", ""),
    # inside script, style and textarea: what is nearly their end tag
    "script, </scrip flood": ("<script>", "</scrip", ""),
    "script, </script x flood": ("<script>", "</script x", ""),
    "script, </ flood": ("<script>", "</", ""),
    "script, <!-- flood": ("<script>", "<!--", ""),
    "style, </styl flood": ("<style>", "</styl", ""),
    "style, </style x flood": ("<style>", "</style x", ""),
    "textarea, </textare flood": ("<textarea>", "</textare", ""),
    "title, </titl flood": ("<title>", "</titl", ""),
    # entities in text (convert_charrefs is on)
    "& flood": ("", "&", ""),
    "&a flood": ("", "&a", ""),
    "&# flood": ("", "&#", ""),
    "&#x flood": ("", "&#x", ""),
    "&amp; flood": ("", "&amp;", ""),
    "& with spaces": ("", "& ", ""),
    "one & and a long name": ("&", "a", ""),
    "&#x and hex digits": ("&#x", "1", ""),
    "& before each tag": ("", "&a<b>", ""),
    # the same inside a textarea, whose text is unescaped in one piece
    "textarea, & flood": ("<textarea>", "&", ""),
    "textarea, &a flood": ("<textarea>", "&a", ""),
    "textarea, &# flood": ("<textarea>", "&#", ""),
    "textarea, &#x flood": ("<textarea>", "&#x", ""),
    "textarea, &amp; flood": ("<textarea>", "&amp;", ""),
    "textarea, & and a long name": ("<textarea>&", "a", ""),
    "textarea, &#x and hex digits": ("<textarea>&#x", "1", ""),
    # "<" with no tag after it
    "<<< flood": ("", "<<<", ""),
    "<a< flood": ("", "<a<", ""),
    # a quote never closed in a tag
    '<a x=" unclosed': ('<a x="', "x", ""),
    "<a x=' unclosed": ("<a x='", "x", ""),
    '<a x=" flood': ("", '<a x="', ""),
    '<a x=" long value, then >': ('<a x="', "x", ">"),
    # tags with a very long name, and many with a long one
    "long tag name": ("<", "a", ">"),
    "long tag name, no >": ("<", "a", ""),
    "long end tag name": ("</", "a", ">"),
    "long end tag name, no >": ("</", "a", ""),
    "long tag name with a quote": ("<a'", "a", ">"),
    "many tags with long names": ("", "<" + "a" * 60 + ">", ""),
    # after <plaintext> the rest of the page is text
    "plaintext, then x": ("<plaintext>", "x", ""),
    "plaintext, then <a": ("<plaintext>", "<a ", ""),
    "plaintext, then &": ("<plaintext>", "&", ""),
    "plaintext, then its end tag": ("<plaintext>", "</plaintext>", ""),
}


# How much memory the reads may hold at once, as copies of the page (`tracemalloc`).
# The base keeps its unread text (one copy) and hands the handlers pieces of it, so a
# few copies is the most that is wanted: OTHER_COPIES. A few shapes make the standard
# library, or the handler, keep more, and each is named with its reason:
OTHER_COPIES = 4
#  - `html.unescape` makes one str of about 50 bytes for each "&x" it replaces, all at
#    once, so a page of "&a&a&a..." peaks at 27 times its own size. That is what the
#    library builds for text outside a tag (and, at the end of the page, in a textarea
#    or title), so it is the most a page can force on any reader, and no copy of it is
#    kept after the call.
UNESCAPES = {"&a flood": 32, "textarea, &a flood": 32}
#  - `text._Visible` keeps what it is given: with a piece of one or two characters
#    for each "<" or "&a" that is a list entry (8 bytes) and a str for every 5
#    characters of the page, about 8 to 10 times the page. (It stops at
#    `text.VISIBLE_LIMIT`, so the bigger the page the smaller the multiple.)
KEPT_PIECES = {"<<< flood": 12, "& before each tag": 12}
# Under tracemalloc a read that is called back for every few characters takes 60 to
# 200 times as long (a page of "<" takes a minute and a half per MiB), so these are
# read at a 64th of the size: 16 KiB and 128 KiB. Their time is measured at the full
# size by the test above, and the copies they keep do not depend on the size.
TRACED_SMALL = {
    "<!--> flood",
    "CDATA closed over and over",
    "<![endif]> flood",
    "&a flood",
    "&amp; flood",
    "& before each tag",
    "textarea, &a flood",
    "textarea, &amp; flood",
    "<<< flood",
    "many tags with long names",
}


def _bounded_read(page):
    """The base itself, with no handler: what every subclass's reading starts from."""
    parser = htmlbound.BoundedParser()
    try:
        parser.feed(page)
        parser.close()
    except htmlbound.TooComplex as exc:
        return exc


def _visible_read(page):
    """A typical subclass: `text._Visible` keeps the text it is given (up to its limit)."""
    parser = text._Visible(text.VISIBLE_LIMIT)
    try:
        parser.feed(page)
        parser.close()
    except htmlbound.TooComplex as exc:
        return exc


OTHER_READS = {"BoundedParser": _bounded_read, "text._Visible": _visible_read}


def _other_page(shape: str, chars: int) -> str:
    first, unit, last = OTHER[shape]
    return scalecheck.page(first, unit, chars, last)


def _copies(read: str, shape: str) -> int:
    if shape in UNESCAPES:
        return UNESCAPES[shape]
    if read == "text._Visible" and shape in KEPT_PIECES:
        return KEPT_PIECES[shape]
    return OTHER_COPIES


@pytest.mark.parametrize("shape", sorted(OTHER))
@pytest.mark.parametrize("read", sorted(OTHER_READS))
@pytest.mark.scaling
def test_the_time_of_every_other_long_match_grows_with_the_page(read, shape):
    first, unit, last = OTHER[shape]
    scalecheck.assert_linear(OTHER_READS[read], first, unit, last)


@pytest.mark.parametrize("chars", [MIB, 8 * MIB], ids=["1 MiB", "8 MiB"])
@pytest.mark.parametrize("shape", sorted(OTHER))
@pytest.mark.parametrize("read", sorted(OTHER_READS))
def test_the_memory_of_every_other_long_match_is_a_few_copies_of_the_page(
    read, shape, chars
):
    page = _other_page(shape, chars // 64 if shape in TRACED_SMALL else chars)
    gave, peak = _peak(OTHER_READS[read], page)
    assert gave is None or isinstance(gave, htmlbound.TooComplex), repr(gave)
    copies = _copies(read, shape)
    assert peak < copies * len(page), (
        f"{peak / len(page):.1f} times the page of {len(page)} characters"
        f" (the bound is {copies})"
    )


def test_the_sets_name_shapes_of_the_table():
    for named in (UNESCAPES, KEPT_PIECES, TRACED_SMALL):
        assert set(named) <= set(OTHER), set(named) - set(OTHER)
    for shape in OTHER:
        assert len(_other_page(shape, MIB)) == MIB, shape


# One page of the table above is left out of it because it is not a matter of time or
# memory: a decimal reference of more digits than `int` will read. `html.unescape` (which
# the parser calls on text when `convert_charrefs` is on, as it is for `text`, `pagediff`
# and `partdiff`) reads the digits with `int()`, which refuses more than
# `sys.get_int_max_str_digits()` (4300), and the ValueError comes out of `feed()`: a
# reader that catches only `TooComplex` does not catch it. `&#x` with any number of hex
# digits is fine (a power of two is never limited), and is in the table. Strict, so that
# the test that fails when this is fixed is the one that says to take the mark off.
@pytest.mark.parametrize("lead", ["", "<textarea>"], ids=["in text", "in a textarea"])
@pytest.mark.parametrize("read", sorted(OTHER_READS))
@pytest.mark.xfail(
    strict=True,
    raises=ValueError,
    reason="html.unescape: int() refuses a decimal reference of over 4300 digits",
)
def test_a_decimal_reference_of_thousands_of_digits_is_not_an_error(read, lead):
    assert OTHER_READS[read](lead + "&#" + "1" * 5000) is None
