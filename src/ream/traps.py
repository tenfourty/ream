"""Ways a page is wrong that the serving policy has no opinion about.

`csp.py` answers one question -- what will the browser refuse to run? -- and
every shape in it is pinned to a directive in the policy this service actually
sends. These are the other kind: the page renders perfectly and is still
wrong, because nobody could look at it.

homelab-ij8.36, from an agent on 19 Sep 2026: "the hard constraints are right
and well argued but spread across prose in three places, and every one is
discovered only AFTER publishing". It wrote its own greps before publishing --
a title count, a form count, external refs, target=_blank on the gallery link
-- and said every agent would rediscover them. "A check that runs beats a
warning that is read."

Kept out of `csp.py` on purpose. That module's whole argument is that a shape
which fires on something the policy permits trains authors to ignore the
warning, and it earns that by pinning each shape to a directive. A placeholder
title has no directive behind it; folding it in would quietly end that
property.

Both halves use these: `artifacts-publish --check` REFUSES on them (the author
is looking at a file in front of them), and a publish returns them as advisory
warnings alongside the CSP ones (the author cannot see the rendered page, so
this is the only moment it learns).
"""

import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

from ream import htmlbound, renderfind, settings, textvars
from ream.csp import CspWarning, line_of, newline_offsets, outside_comments
from ream.renderfind import Budget, TooComplex


# The gallery's own host. A link to it from inside a published page is the one
# link that needs `target="_blank"`: the page is served on its own origin in a
# sandboxed frame, the gallery refuses to be framed, and the sandbox forbids
# top-level navigation -- so a same-tab link goes nowhere at all. The owner found
# this on a real page on 19 Sep, which is why every template carries the
# attribute and why this checks for it.
def _gallery_host(client: bool) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """The start of an address that reaches the configured gallery's host, or the former
    host while one is set (a page written before a move still links the old name, which
    forwards to the new one and needs the same `target`), built at call time (both are
    settings). It takes `https://host`, `http://host` and `//host` (the page's own
    scheme), as an address reads after `_decoded`."""
    names = [urlsplit(textvars.gallery_origin(client)).hostname or ""]
    former = settings.current().former_host.strip().lower().removesuffix(".")
    if former:
        names.append(former)
    alternatives = "|".join(re.escape(n) for n in names)
    bare = re.compile(alternatives, re.IGNORECASE)
    return re.compile(r"(?:https?:)?//(?:" + alternatives + ")", re.IGNORECASE), bare


# What follows `href=`: an optional quote, whitespace, then the value as far as the next quote
# or `>`, read ahead (zero-width) so an `href=` inside another's value is still found, and
# capped so a tag of many of them costs no more than its length times the cap.
_HREF_VALUE = re.compile(r"\bhref\s*=\s*[\"']?\s*(?=([^\"'>]{0,2048}))", re.IGNORECASE)
_URL_NOISE = re.compile(r"[\t\n\r]")


def _decoded(value: str) -> str:
    """An `href` value as a browser reads it as an address: character references
    decoded, tab and newline dropped from anywhere, leading and trailing ASCII
    whitespace and control characters stripped, `\\` taken for `/`."""
    if "&" in value:
        value = htmlbound.unescape(value)
    value = _URL_NOISE.sub("", value).strip("".join(map(chr, range(0x21))))
    return value.replace("\\", "/")


def _reaches_gallery(tag: str, host: re.Pattern[str]) -> bool:
    return any(
        host.match(_decoded(found.group(1))) is not None
        for found in _HREF_VALUE.finditer(tag)
    )


_HAS_BLANK_TARGET = re.compile(r"\btarget\s*=\s*[\"']?_blank\b", re.IGNORECASE)
_A_OPEN = re.compile(r"<a\b", re.IGNORECASE)


def _gallery_links(
    text: str, client: bool, budget: Budget
) -> Iterator[tuple[int, bool]]:
    """(where, whether it has a `target` of `_blank`) of each `<a ...>` tag with an
    `href` to the gallery, in page order.

    The page is read once, front to back: each search starts where the last stopped,
    so the time grows with the size of the page, never with the number of tags in it.
    (A pattern of the shape `<a ... [^>]* href ... [^>]*>` run over a page of unclosed
    tags looks to the end of the page from each one.) It gives what that pattern gave:
    a tag runs from its `<a` to the first `>` after it, a tag with none ends the search
    (no later one has a `>` either), and an `<a` inside another tag's reach is part of
    that tag, whether or not that one is a gallery link.
    """
    host, bare = _gallery_host(client)
    # Without a character reference, an address to the gallery names its host in the
    # page; with one, the host may be spelled out of references, so every tag is read.
    if "&" not in text and bare.search(text) is None:
        return  # no link to the gallery anywhere: nothing to find
    at = 0
    while True:
        opened = _A_OPEN.search(text, at)
        if opened is None:
            return
        end = text.find(">", opened.end())
        if end < 0:
            return
        budget.step()
        at = end + 1
        if _reaches_gallery(text[opened.end() : end], host):
            yield (
                opened.start(),
                _HAS_BLANK_TARGET.search(text, opened.start(), at) is not None,
            )


# `<code>ID</code>`, and nothing looser. The old skeleton's footer read "Page
# id <code>ID</code>", so a page built from a copy of it still carries one --
# and a page id does not exist until the publish call returns (homelab-ij8.31),
# so the placeholder either ships as-is or costs a second publish that changes
# nothing else. Narrow on purpose: "ID" appears in ordinary prose, and only
# this exact element is unambiguous.
_PAGE_ID_PLACEHOLDER = re.compile(r"<code>\s*ID\s*</code>", re.IGNORECASE)

_TITLE = (
    re.compile(r"<title\b", re.IGNORECASE),
    re.compile(r"</title\s*>", re.IGNORECASE),
)
_H1 = (re.compile(r"<h1\b", re.IGNORECASE), re.compile(r"</h1\s*>", re.IGNORECASE))
_TAGS = re.compile(r"<[^>]*>")


def _element(
    text: str, name: tuple[re.Pattern[str], re.Pattern[str]]
) -> tuple[int, str] | None:
    """(where, the text inside) of the first `<name ...>text</name>`, as a pattern of
    that shape finds it, without looking to the end of the page from each opening tag.
    The first opening tag is the only one that can answer: if it has no `>`, or nothing
    closes it after its `>`, no later one has either."""
    opening, closing = name
    opened = opening.search(text)
    if opened is None:
        return None
    end = text.find(">", opened.end())
    if end < 0:
        return None
    closed = closing.search(text, end + 1)
    if closed is None:
        return None
    return opened.start(), text[end + 1 : closed.start()]


# What the templates and the skeleton actually leave behind, not a guess at
# what a placeholder might look like. The first board published here shipped
# with `PAGE TITLE` unedited in <title> and the browser tab read that for two
# hours -- nobody could see it, because an agent cannot look at its own page.
_PLACEHOLDERS = (
    "page title",
    "replace me",
    "your title here",
    "untitled",
    "untitled document",
    "document",
    "todo",
    "tbd",
    "lorem ipsum",
)


# A template's guidance marker, `<!-- REPLACE ... -->`, sits above each block of
# worked example. The comment itself never renders -- which is the point: the
# cove seat, 24 Sep 2026, migrated a page from the explainer template, left one
# at line 25, and `--check` said "no trap the templates warn about". A marker
# still in the file is the only trace an author can see of a block that may
# still be the example. So this looks INSIDE comments, unlike everything else
# here, and matches only the templates' exact spelling.
_REPLACE_MARKER = re.compile(r"<!--\s*REPLACE\b")


def _load_templates() -> tuple[frozenset[str], frozenset[str]]:
    """(each shipped template's whole text, stripped; each one's example title).

    A template published EXACTLY as shipped is a specimen -- the wall carries
    one of each on purpose -- and must stay clean, or the check teaches every
    seat that it is noise (tests/test_traps.py). So the two template traps
    fire only on a page that has been started and not finished, which is the
    one that ships an example as though it were a report.
    """
    bodies, titles = set(), set()
    for path in (Path(__file__).resolve().parent / "templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        bodies.add(text.strip())
        found = _element(text, _TITLE)
        if found:
            titles.add(_plain(found[1]).lower())
    return frozenset(bodies), frozenset(titles)


def _plain(fragment: str) -> str:
    """`fragment` with each `<...>` (from a `<` to the first `>` after it) as a space,
    and its whitespace collapsed.

    The pattern is given only what comes before the last `>`. A `<` there always has a
    `>` after it, so each `<` that starts a match ends it at once and the pattern never
    reads a stretch twice; a `<` with no `>` after it, which would send it to the end of
    the text from each one, is after the last `>` and is kept as it is."""
    end = fragment.rfind(">") + 1
    return " ".join((_TAGS.sub(" ", fragment[:end]) + fragment[end:]).split()).strip()


_TEMPLATES: list[tuple[frozenset[str], frozenset[str]]] = []


def _templates() -> tuple[frozenset[str], frozenset[str]]:
    if not _TEMPLATES:
        _TEMPLATES.append(_load_templates())
    return _TEMPLATES[0]


NOT_RUN = (
    "this page is too large or complex for the page checks (placeholder title, "
    "template marker, page id placeholder, link to the gallery) to read in the time "
    "allowed: they did not run, so their silence on this page means nothing"
)


def check(
    body: str, client: bool = False, budget: Budget | None = None
) -> list[CspWarning]:
    """Every trap in `body`, oldest concern first, as the same warning shape the
    CSP advisory returns -- one list reaches the author, and it should not
    matter to them which module noticed.

    Comments are stripped first, exactly as the CSP check does: a construct
    inside `<!-- -->` never reaches a browser, and a check that fires on
    correct pages is how a warning becomes noise. Every shipped template
    carries its guidance in comments.

    A page over `renderfind.MAX_SCAN_CHARS`, or one that uses up the `budget` (a
    `Budget` of its own when none is given), is not checked any further: the answer
    is then the one warning, `trap_checks_not_run`, and never a partial list or an
    empty one, which would read as a clean page.
    """
    budget = Budget() if budget is None else budget
    try:
        if len(body) > renderfind.MAX_SCAN_CHARS:
            raise TooComplex
        budget.check()
        return _check(body, client, budget)
    except TooComplex:
        return [CspWarning(shape="trap_checks_not_run", line=1, hint=NOT_RUN)]


def _check(body: str, client: bool, budget: Budget) -> list[CspWarning]:
    visible = outside_comments(body, budget)
    newlines = newline_offsets(visible)
    budget.check()
    found: list[CspWarning] = []
    specimens, examples = _templates()
    started = body.strip() not in specimens

    for name, where in ((_TITLE, "<title>"), (_H1, "the page's <h1>")):
        element = _element(visible, name)
        if not element:
            continue
        at, inside = element
        text = _plain(inside)
        if started and text.lower() in examples:
            found.append(
                CspWarning(
                    shape="template_example_title",
                    line=line_of(at, newlines),
                    hint=(
                        f"{where} is still the template's worked example "
                        f"({text!r}). The rest of the page probably is too: "
                        "every block under a REPLACE marker is example content."
                    ),
                )
            )
        elif text.lower() in _PLACEHOLDERS:
            found.append(
                CspWarning(
                    shape="placeholder_title",
                    line=line_of(at, newlines),
                    hint=(
                        f"{where} still carries the template's placeholder. It "
                        "renders exactly as written and you cannot see it: the "
                        "first board published here read PAGE TITLE in the "
                        "browser tab for two hours."
                    ),
                )
            )

    # The raw body, not `visible`: the marker IS a comment. Its line numbers are
    # the author's, so count them against the body.
    raw_newlines = newline_offsets(body)
    for match in _REPLACE_MARKER.finditer(body if started else ""):
        budget.step()
        found.append(
            CspWarning(
                shape="template_marker_left",
                line=line_of(match.start(), raw_newlines),
                hint=(
                    "a template's REPLACE marker is still here. It never "
                    "renders, so nobody reading the page will see that the "
                    "block under it may still be the worked example. Rewrite "
                    "the block, then delete the marker."
                ),
            )
        )

    for match in _PAGE_ID_PLACEHOLDER.finditer(visible):
        budget.step()
        found.append(
            CspWarning(
                shape="placeholder_page_id",
                line=line_of(match.start(), newlines),
                hint=(
                    "a page id does not exist until the publish call returns, "
                    "so this renders as the literal word ID. Leave it out -- "
                    "the gallery shows the id beside the page, and --list "
                    "prints it."
                ),
            )
        )

    for at, has_target in _gallery_links(visible, client, budget):
        if has_target:
            continue
        found.append(
            CspWarning(
                shape="gallery_link_same_tab",
                line=line_of(at, newlines),
                hint=(
                    'a link to the gallery needs target="_blank" '
                    'rel="noopener". This page is served in a sandboxed '
                    "frame that forbids top-level navigation, and the gallery "
                    "refuses to be framed, so in the same tab the link does "
                    "nothing at all."
                ),
            )
        )

    return sorted(found, key=lambda w: (w.line, w.shape))


def as_json(body: str, budget: Budget | None = None) -> list[dict]:
    """`check()` in the shape the API returns it, matching `csp.as_json`."""
    return [warning.__dict__ for warning in check(body, budget=budget)]
