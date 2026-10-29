"""Viewer-typed text on its way into somewhere that is not a browser.

A page's own content is HTML and a browser renders it; that is not this. This
is free text a human typed — a decision note, a comment — travelling to an
agent's context, a terminal, a log line or a chat message, where there is no
renderer and a control character is not inert. A newline can fake a second
line in a mailbox format. An escape sequence can move the cursor, recolour, or
overwrite what was printed above it. A bell rings.

So the rule is: this text is DATA, never structure, and it is flattened to
data before it leaves the service — not by whoever happens to be printing it.
The nudge path had this right and had it alone; a consumer reading the event
feed got the raw string, which meant every future client would have had to
re-implement this or quietly not (homelab-ij8.55).

Sanitising at the boundary, not at the point of display, is the whole point.
There is no list of consumers to keep in step.
"""

import re
from html.parser import HTMLParser
from typing import ClassVar

# Every C0 control character except the whitespace ones `sanitize` already
# turns into a plain space by splitting on whitespace (tab, LF, CR, FF, VT),
# plus the C1 range — an escape sequence, a bell, a NUL: anything that could
# reorder or forge structure in a terminal, a mailbox line or an agent's
# context.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# A summary is a line in someone else's list, not a document.
SUMMARY_LIMIT = 200


# Control characters with no business in an agent's terminal, KEEPING newline
# and tab: his notes are multi-line and must arrive as he wrote them.
_CONTROL_EXCEPT_LINES = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def strip_controls(text: str | None) -> str | None:
    """Remove terminal-steering control characters (an escape sequence in a
    comment could rewrite what an agent's terminal shows), keep the words, the
    newlines and the length exactly (homelab-ij8.55). For the /events feed,
    which went out raw while only the nudge was cleaned."""
    if text is None:
        return None
    return _CONTROL_EXCEPT_LINES.sub("", text)


def sanitize(text: str, limit: int = SUMMARY_LIMIT) -> str:
    """Collapse every run of whitespace to one space, strip any remaining
    C0/C1 control character, and cap at `limit` characters with a trailing
    ellipsis.

    Deliberately not escaping: this text is going somewhere with no renderer,
    so an escaped `\\x1b` would just be noise a human has to read past. The
    character is removed because there is nothing it could legitimately mean
    here.
    """
    collapsed = " ".join(text.split())
    cleaned = _CONTROL_CHARS.sub("", collapsed)
    if len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


# --- a page's own one-line description, for a gallery tile ------------------
#
# homelab-ij8.22. Jeremy, 19 Sep 2026: the index is bare, he wants tiles with
# "the title and some detail". The detail has to come from the page itself --
# asking agents to supply a separate summary would mean the summary drifts from
# the page, which is the failure this whole service is built to avoid.
#
# This is TEXT EXTRACTION, never rendering. The gallery must not load an agent's
# HTML into its own origin under any circumstances, so nothing here parses a
# document or builds a tree: it finds one string, strips tags out of it, and
# hands back plain text that the caller escapes like any other untrusted value.

_DESCRIPTION_META = re.compile(
    r'<meta\s[^>]*name\s*=\s*["\']description["\'][^>]*>', re.IGNORECASE
)
_CONTENT_ATTR = re.compile(r'content\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
_STANDFIRST = re.compile(
    r'<p\s[^>]*class\s*=\s*["\'][^"\']*\bstandfirst\b[^"\']*["\'][^>]*>(.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)
_ANY_PARAGRAPH = re.compile(r"<p(?:\s[^>]*)?>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG = re.compile(r"<[^>]*>")
_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&mdash;": "—",
    "&ndash;": "–",
    "&nbsp;": " ",
    "&middot;": "·",
    "&ldquo;": "“",
    "&rdquo;": "”",
    "&hellip;": "…",
}

DESCRIPTION_LIMIT = 150


def _plain(fragment: str) -> str:
    """Tags out, the handful of entities a page actually uses turned back into
    characters, whitespace collapsed. Unknown entities are left as written
    rather than guessed at -- a literal `&foo;` in a tile is honest, and a
    guessed one is a quiet mistranslation of someone's prose."""
    text = _TAG.sub(" ", fragment)
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    return sanitize(text, limit=DESCRIPTION_LIMIT)


# What a search looks at. Big enough to cover a real page's prose, small enough
# that scanning the wall stays cheap -- and it is a bound on the SOURCE, so a
# page with 200 KB of inline data does not make every keystroke expensive.
SEARCH_LIMIT = 40_000

# Content that is in the document but is not the document's words. Searching
# these produces hits a reader cannot see anywhere on the page, which reads as
# the search being broken rather than as the query being odd.
_NOT_PROSE = re.compile(
    r"<(script|style|svg|template)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)


def searchable(body: str) -> str:
    """A page's readable words, lowercased, for matching a query against.

    NOT `describe`. That answers "what is this page about?" and returns "" when
    the page does not say -- which is most pages, and would have made a search
    silently miss them. This answers "what words are on it?", which is the
    question a search is actually asking.

    Script, style, inline SVG and templates come out first, then tags, so a
    query can never match a class name, a CSS colour or an SVG path.
    """
    trimmed = _NOT_PROSE.sub(" ", _COMMENT.sub(" ", body[:SEARCH_LIMIT]))
    text = _TAG.sub(" ", trimmed)
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    return " ".join(text.split()).lower()


# The most visible text a comment's quote is matched against (22 Oct 2026).
VISIBLE_LIMIT = 2_000_000
# The most HTML `visible` will parse, whatever the caller: the parser never sees
# more than this.
INPUT_LIMIT = 4_000_000


class _Visible(HTMLParser):
    """The words outside script, style, svg and template, in one linear pass."""

    _SKIP: ClassVar[set[str]] = {"script", "style", "svg", "template"}

    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.size = 0
        self.truncated = False
        self.parts: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth or self.truncated:
            return
        self.size += len(data) + 1
        if self.size > self.limit:
            self.truncated = True
            return
        self.parts.append(data)


def visible(body: str, limit: int | None = None) -> tuple[str, bool]:
    """A page's readable words, lowercased, with script, style, SVG and
    templates removed BEFORE any length limit is applied, and whether the text
    was cut at `limit`. Unlike `searchable`, which cuts the HTML first and so
    misses text that follows a large inline stylesheet. A small page takes the
    same quick path as `searchable`; a large one is walked once by a parser, so
    no input makes it slow."""
    limit = VISIBLE_LIMIT if limit is None else limit
    if len(body) <= SEARCH_LIMIT:
        words = searchable(body)
        return words, len(words) > limit
    parser = _Visible(limit)
    parser.feed(body[:INPUT_LIMIT])
    parser.close()
    return " ".join(" ".join(parser.parts).split()).lower(), parser.truncated


def readable(body: str, limit: int | None = None) -> tuple[str, bool]:
    """`visible`, with the page's own capitals: the words a reader sees, spaces
    collapsed, for text that is shown back rather than searched. The same
    parser, the same limits."""
    limit = VISIBLE_LIMIT if limit is None else limit
    parser = _Visible(limit)
    parser.feed(body[:INPUT_LIMIT])
    parser.close()
    return " ".join(" ".join(parser.parts).split()), parser.truncated


def describe(body: str) -> str:
    """One line saying what a page is about, or "" when the page does not say.

    Tried in the order a page is most likely to have meant it:
      1. `<meta name="description">`, which is an explicit statement of intent
      2. `<p class="standfirst">`, which every template here carries and which
         is written to be exactly this sentence
      3. the first paragraph with any real words in it

    Returns "" rather than a guess when none of those yields anything. A tile
    with no description is a tile with no description; inventing one from a
    heading or a fragment of navigation would put words in an agent's mouth on
    a page Jeremy reads as that agent's own.
    """
    # Comments first: a template's REPLACE notes are prose inside <!-- -->, and
    # the first paragraph of one would otherwise become the page's description.
    visible = _COMMENT.sub(" ", body)

    meta = _DESCRIPTION_META.search(visible)
    if meta:
        content = _CONTENT_ATTR.search(meta.group(0))
        if content and (text := _plain(content.group(1))):
            return text

    standfirst = _STANDFIRST.search(visible)
    if standfirst and (text := _plain(standfirst.group(1))):
        return text

    for paragraph in _ANY_PARAGRAPH.finditer(visible):
        text = _plain(paragraph.group(1))
        # Two words is a caption or a chip, not a description of anything.
        if len(text.split()) >= 4:
            return text
    return ""


# An optional BOM, then leading whitespace and whole HTML comments, then the leading
# doctype -- everything up to and including it is one match, so the shim can be spliced
# in right after it. The comment alternative is not a lazy `.*?-->`: under backtracking
# that can span from the first `<!--` all the way to the LAST `-->` before a later
# doctype, treating a genuine `<script>` (and anything else) in between as if it were
# commented out, and landing the shim after it, or inside it.
#
# The WHATWG HTML tokenizer ends a comment at more than plain `-->`: also at `<!-->` and
# `<!--->` (abrupt closing of an empty comment) and at `--!>` (incorrectly closed
# comment). Missing those lets a comment appear to "end" only at the FIRST real `-->`
# that follows, which can be well past a page's own `<script>` -- so the alternative
# below is `-?>` (the abrupt-close forms, immediately after `<!--`) or a body that
# contains neither `-->` nor `--!>`, terminated by either. The negative lookahead right
# after `<!--` is load-bearing: without it, backtracking can still reopen `<!-->`.
LEADING_DOCTYPE = re.compile(
    r"^(﻿?\s*(?:<!--(?:-?>|(?!-?>)(?:(?!--!?>).)*--!?>)\s*)*<!doctype[^>]*>)",
    re.IGNORECASE | re.DOTALL,
)

# Public, and living here rather than in `content.py`, because TWO callers need
# the same answer to "does this document declare a doctype, and where does it
# end?": the shim injector splices after it, and `store` refuses a body that
# never claims to be HTML at all (homelab-ij8.28). `store` cannot import
# `content` -- content imports hosts imports store -- and a second copy of a
# regex this carefully argued is how two parts of one service start disagreeing
# about what a page is.
