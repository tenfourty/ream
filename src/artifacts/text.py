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

# Every C0 control character except the whitespace ones `sanitize` already
# turns into a plain space by splitting on whitespace (tab, LF, CR, FF, VT),
# plus the C1 range — an escape sequence, a bell, a NUL: anything that could
# reorder or forge structure in a terminal, a mailbox line or an agent's
# context.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# A summary is a line in someone else's list, not a document.
SUMMARY_LIMIT = 200


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
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'",
    "&mdash;": "—", "&ndash;": "–", "&nbsp;": " ", "&middot;": "·",
    "&ldquo;": "“", "&rdquo;": "”", "&hellip;": "…",
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
