"""Advisory checks for page constructs this service's own CSP will not run.

An artifact is served from a sandboxed origin under a deliberately narrow
Content-Security-Policy (`artifacts.content._BASE_CSP`). A page that reaches
outside it -- a Google font, a CDN script, a hand-rolled form, an iframe --
publishes perfectly happily and then silently half-renders when Jeremy opens
it, with nothing in any log the author would ever see. The author cannot look
either: the content origin only answers a framed request holding a capability
token no agent is given.

So the finding has to travel back in the publish response. It is a WARNING,
never a refusal, and that is a deliberate call: refusing would block a page
that is 95 % right over a literal `<form` inside an escaped code sample, and
an author who cannot see the page also cannot see what upset the server. A
warning cannot silently pass, which is the actual failure this closes. The
refusing version of this check belongs in the client's `--check`, where the
author reads it and may override it.

DISCIPLINE, inherited from `artifacts.scan`: a `CspWarning` says which shape
fired and on which line, never what matched. `hint` is a fixed string
belonging to the shape, never built from the body. Do not add a field, a log
line or an exception message here that carries scanned text.

Every shape below is derived from a real directive in `_BASE_CSP`, not from
taste, and its hint says what the browser will actually do. When that policy
changes, this table changes with it -- `tests/test_csp.py` pins each shape to
the directive that justifies it so the two cannot drift apart quietly.
"""

import bisect
import re
from dataclasses import dataclass

# Absolute or protocol-relative: both leave this origin, both are what the
# CSP's 'self' refuses. A root-relative "/x.js" or a bare "x.js" stays here
# and is never flagged.
_REMOTE = r"(?:[a-zA-Z][a-zA-Z0-9+.-]*:)?//"

# Same bounding discipline as the secret scanner, and for the same reason: a
# dense 512 KiB body must not turn one publish response into megabytes of
# repeated findings. Distinct lines per shape, then findings overall.
_MAX_LINES_PER_SHAPE = 5
_MAX_WARNINGS_TOTAL = 40
_MAX_MATCHES_PER_SHAPE = 500


@dataclass(frozen=True)
class CspWarning:
    shape: str
    line: int
    hint: str


# (shape, pattern, hint). The hint is what a reader needs to act: what will
# happen, and why. It never names the matched text.
_SHAPES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "remote_script",
        re.compile(r"<script\b[^>]*\bsrc\s*=\s*[\"']?" + _REMOTE, re.IGNORECASE),
        ("script-src is 'self' 'unsafe-inline': a script loaded from another "
        "origin (a CDN) is blocked and never runs. Inline the code instead."),
    ),
    (
        "remote_stylesheet",
        re.compile(r"<link\b[^>]*\bhref\s*=\s*[\"']?" + _REMOTE, re.IGNORECASE),
        ("style-src is 'self' 'unsafe-inline' and font-src is 'self': a "
        "stylesheet or web font from another origin (Google Fonts) is "
        "blocked. Use an inline <style> and the system font stack."),
    ),
    (
        "remote_css_url",
        re.compile(r"url\(\s*[\"']?" + _REMOTE, re.IGNORECASE),
        ("A CSS url() pointing at another origin -- a @font-face source or a "
        "background image -- is blocked by font-src/img-src 'self'. Inline "
        "the asset as a data: URI, or drop it."),
    ),
    (
        "css_import",
        re.compile(r"@import\b[^;]*" + _REMOTE, re.IGNORECASE),
        ("@import from another origin is blocked by style-src 'self'. Put the "
        "rules directly in the page's own <style>."),
    ),
    (
        "remote_image",
        re.compile(
            r"<(?:img|source)\b[^>]*\bsrc(?:set)?\s*=\s*[\"']?" + _REMOTE,
            re.IGNORECASE,
        ),
        ("img-src is 'self' data:: an image hotlinked from another origin is "
        "blocked. Inline it as a data: URI, or use inline SVG."),
    ),
    (
        "media_element",
        re.compile(r"<(?:video|audio)\b", re.IGNORECASE),
        ("There is no media-src directive, so <video>/<audio> falls back to "
        "default-src 'none' and no media plays at all -- not even same-origin "
        "or data:. A page cannot carry audio or video here."),
    ),
    (
        "iframe",
        re.compile(r"<iframe\b", re.IGNORECASE),
        ("There is no frame-src directive, so it falls back to default-src "
        "'none': a nested iframe never loads. The page is itself already "
        "framed by the gallery."),
    ),
    (
        "form_element",
        re.compile(r"<form\b", re.IGNORECASE),
        ("form-action is 'none': a form cannot submit anywhere. This is the "
        "trust boundary that stops a page forging an answer -- use the "
        "decisions API and a data-artifact-decision slot instead."),
    ),
    (
        "base_element",
        re.compile(r"<base\b", re.IGNORECASE),
        ("base-uri is 'none': a <base> tag is ignored, so relative URLs will "
        "not resolve the way the page expects."),
    ),
    (
        "web_worker",
        re.compile(r"new\s+(?:Shared)?Worker\s*\(", re.IGNORECASE),
        ("worker-src is 'none': a Web Worker cannot start. Do the work on the "
        "main thread, or precompute it before publishing."),
    ),
    (
        "remote_connect",
        re.compile(
            r"(?:fetch|EventSource|WebSocket|sendBeacon)\s*\(\s*[\"'`]"
            + _REMOTE
            + r"|\.open\s*\(\s*[\"'][A-Za-z]+[\"']\s*,\s*[\"'`]"
            + _REMOTE,
            re.IGNORECASE,
        ),
        ("connect-src is 'self' at most, and 'none' for a page with no live "
        "features: a request to another origin is blocked. Put the data in "
        "the page at publish time, and republish to update it."),
    ),
    (
        "window_claude",
        re.compile(r"\bwindow\s*\.\s*claude\b|\bclaude\s*\.\s*use\s*\("),
        ("window.claude is the claude.ai artifact runtime. It does not exist "
        "here -- this service serves your file as-is -- so the call resolves "
        "to nothing and any feature behind it is silently missing."),
    ),
)


def newline_offsets(body: str) -> list[int]:
    """Public because `traps.py` reports in the same shape and must count lines
    the same way -- two copies of "which line is this offset on" is how two
    checks on one page start disagreeing about where a problem is."""
    return [m.start() for m in re.finditer("\n", body)]


def line_of(offset: int, newline_offsets: list[int]) -> int:
    return bisect.bisect_right(newline_offsets, offset) + 1


# A whole HTML comment, including the tokenizer's abrupt closes (`<!-->`,
# `<!--->`) and its incorrect one (`--!>`); an unterminated comment runs to the
# end of the document, as it does in a browser.
_HTML_COMMENT = re.compile(r"<!--(?:-?>|.*?(?:--!?>|\Z))", re.DOTALL)


def outside_comments(body: str) -> str:
    """`body` with every HTML comment blanked, newlines kept so lines still count.

    A commented-out construct never runs, so the CSP never blocks it, so a
    warning about it is a false alarm -- and a warning that fires on correct
    pages trains authors to ignore the warning. Found on 21 Sep through the
    decision-slot check (homelab-ij8.50): every shipped template with a slot
    ships it commented out, to be uncommented once the question exists, and
    the first version of that check warned every agent that used a template
    exactly as told. The same test here: a commented-out CDN `<script>` came
    back as `remote_script`.
    """
    return _HTML_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), body)


def check(body: str) -> list[CspWarning]:
    """Every construct in `body` the serving CSP will not run, bounded.

    Advisory only: this never raises on a finding and never refuses a body.
    Returns `[]` for a page with nothing to say, which is the common case.
    Nothing inside an HTML comment is examined.
    """
    body = outside_comments(body)
    newlines = newline_offsets(body)
    warnings: list[CspWarning] = []
    for shape, pattern, hint in _SHAPES:
        lines: list[int] = []
        for examined, match in enumerate(pattern.finditer(body), start=1):
            line = line_of(match.start(), newlines)
            if line not in lines:
                lines.append(line)
            if (
                len(lines) >= _MAX_LINES_PER_SHAPE
                or examined >= _MAX_MATCHES_PER_SHAPE
            ):
                break
        warnings.extend(CspWarning(shape=shape, line=line, hint=hint) for line in lines)
        if len(warnings) >= _MAX_WARNINGS_TOTAL:
            break
    return warnings[:_MAX_WARNINGS_TOTAL]


def as_json(body: str) -> list[dict]:
    """`check()` in the shape the API returns it: plain dicts, ordered."""
    return [warning.__dict__ for warning in check(body)]
