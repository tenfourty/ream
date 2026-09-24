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
from pathlib import Path

from artifacts.csp import CspWarning, line_of, newline_offsets, outside_comments

# The gallery's own host. A link to it from inside a published page is the one
# link that needs `target="_blank"`: the page is served on its own origin in a
# sandboxed frame, the gallery refuses to be framed, and the sandbox forbids
# top-level navigation -- so a same-tab link goes nowhere at all. Jeremy found
# this on a real page on 19 Sep, which is why every template carries the
# attribute and why this checks for it.
_GALLERY_LINK = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*[\"']?https?://artifacts\.tenfourty\.site[^>]*>",
    re.IGNORECASE,
)
_HAS_BLANK_TARGET = re.compile(r"\btarget\s*=\s*[\"']?_blank\b", re.IGNORECASE)

# `<code>ID</code>`, and nothing looser. The old skeleton's footer read "Page
# id <code>ID</code>", so a page built from a copy of it still carries one --
# and a page id does not exist until the publish call returns (homelab-ij8.31),
# so the placeholder either ships as-is or costs a second publish that changes
# nothing else. Narrow on purpose: "ID" appears in ordinary prose, and only
# this exact element is unambiguous.
_PAGE_ID_PLACEHOLDER = re.compile(r"<code>\s*ID\s*</code>", re.IGNORECASE)

_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1\s*>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]*>")

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
        match = _TITLE.search(text)
        if match:
            titles.add(_plain(match.group(1)).lower())
    return frozenset(bodies), frozenset(titles)


def _plain(fragment: str) -> str:
    return " ".join(_TAGS.sub(" ", fragment).split()).strip()


_TEMPLATES: list[tuple[frozenset[str], frozenset[str]]] = []


def _templates() -> tuple[frozenset[str], frozenset[str]]:
    if not _TEMPLATES:
        _TEMPLATES.append(_load_templates())
    return _TEMPLATES[0]


def check(body: str) -> list[CspWarning]:
    """Every trap in `body`, oldest concern first, as the same warning shape the
    CSP advisory returns -- one list reaches the author, and it should not
    matter to them which module noticed.

    Comments are stripped first, exactly as the CSP check does: a construct
    inside `<!-- -->` never reaches a browser, and a check that fires on
    correct pages is how a warning becomes noise. Every shipped template
    carries its guidance in comments.
    """
    visible = outside_comments(body)
    newlines = newline_offsets(visible)
    found: list[CspWarning] = []
    specimens, examples = _templates()
    started = body.strip() not in specimens

    for pattern, where in ((_TITLE, "<title>"), (_H1, "the page's <h1>")):
        match = pattern.search(visible)
        if not match:
            continue
        text = _plain(match.group(1))
        if started and text.lower() in examples:
            found.append(
                CspWarning(
                    shape="template_example_title",
                    line=line_of(match.start(), newlines),
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
                    line=line_of(match.start(), newlines),
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

    for match in _GALLERY_LINK.finditer(visible):
        if _HAS_BLANK_TARGET.search(match.group(0)):
            continue
        found.append(
            CspWarning(
                shape="gallery_link_same_tab",
                line=line_of(match.start(), newlines),
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


def as_json(body: str) -> list[dict]:
    """`check()` in the shape the API returns it, matching `csp.as_json`."""
    return [warning.__dict__ for warning in check(body)]
