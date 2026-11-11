"""Advisory checks for page constructs this service's own CSP will not run.

An artifact is served from a sandboxed origin under a deliberately narrow
Content-Security-Policy (`ream.content._BASE_CSP`). A page that reaches
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

DISCIPLINE, inherited from `ream.scan`: a `CspWarning` says which shape
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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import NamedTuple

from ream import renderfind

# Absolute or protocol-relative: both leave this origin, both are what the
# CSP's 'self' refuses. A root-relative "/x.js" or a bare "x.js" stays here
# and is never flagged. Written for the page folded to lower case (`_fold`).
_REMOTE = r"(?:[a-z][a-z0-9+.-]*:)?//"

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


# What the checks read, and how. Every pattern a shape needs that looks to a closing
# character (`<link ... href=//x>`, `@import ... //x;`) would, written as one regex,
# look to the end of the page from each opening it finds when no closing character
# follows: a page of "<link " repeated took 100 seconds at 256 KiB. So the page is
# read once, front to back (`_Ahead`: each search starts where the last stopped), and
# a regex is run only on an opening already known to reach what it wants. The answers
# are the ones the single patterns gave; `tests/test_csp.py` holds the old patterns
# and compares both on random pages.
#
# The shapes are written for the page folded to lower case (`_fold`), so that the
# literal each starts with is searched for the way a plain string is, and not tried at
# every character with its case ignored (about ten times as slow on a large page).


class _Page(NamedTuple):
    visible: str  # the page with every comment blanked
    folded: str  # the same, in lower case, the same length


# A finder yields the offset of each match of a shape in the page, in order, as
# `finditer` would find them.
Finder = Callable[[_Page, "renderfind.Budget"], Iterator[int]]

_WORD = re.compile(r"\w")


def _fold(text: str) -> str:
    """`text` in lower case, the same length, so that a pattern with no flag finds
    what the same pattern with `re.IGNORECASE` finds. That flag also makes `ı`,
    `ſ` (which a pattern's `i`, `s` match), and `İ` and the Kelvin sign (whose lower
    case is `i` and `k`) equal to the letter; `lower()` does the last of them, and `İ`
    would grow to two characters, so these three are changed first.
    `tests/test_csp.py` holds this to the flag over every character there is."""
    if not text.isascii():
        text = text.replace("İ", "i").replace("ı", "i").replace("ſ", "s")
    return text.lower()


def _next_word(pattern: re.Pattern[str], text: str, at: int) -> int:
    """Where `pattern` next matches at or after `at`, from a character that is not
    preceded by a word character (a pattern that begins with a letter, and in the old
    patterns with `\\b`, which this stands for: the pattern is left without it so that
    its first letters can be searched for as a string), or -1."""
    while True:
        found = pattern.search(text, at)
        if found is None:
            return -1
        start = found.start()
        if not start or _WORD.match(text, start - 1) is None:
            return start
        at = start + 1


def _words(pattern: re.Pattern[str], text: str) -> Iterator[int]:
    """The start of each match of `pattern` as `finditer` finds them, but not one
    that begins in the middle of a word (see `_next_word`)."""
    at = 0
    while True:
        found = pattern.search(text, at)
        if found is None:
            return
        start = found.start()
        if start and _WORD.match(text, start - 1) is not None:
            at = start + 1
            continue
        yield start
        at = found.end()


def _starts(pattern: re.Pattern[str]) -> Finder:
    """A shape that is one pattern which cannot look far past where it starts."""

    def find(page: _Page, budget: "renderfind.Budget") -> Iterator[int]:
        for found in pattern.finditer(page.folded):
            yield found.start()

    return find


class _Ahead:
    """The first place `find(at)` gives at or after each `at` asked, for `at`s that
    never go back. An answer is kept until the position asked passes it, and "none"
    is kept for good, so the stretch of text between two answers is read once, however
    many openings ask."""

    def __init__(self, find: Callable[[int], int]) -> None:
        self._find = find
        self._from = -1
        self._hit = -1

    def first(self, at: int) -> int:
        if self._from < 0 or at < self._from or 0 <= self._hit < at:
            self._from = at
            self._hit = self._find(at)
        return self._hit


def _reaching(
    head: re.Pattern[str],
    tail: Callable[[str, int], int],
    last_end: Callable[[str, int, int, int], int],
    stop: str,
) -> Finder:
    """A shape of the form `head [^stop]* tail`, as `finditer` finds it: the start of
    each opening that has a tail before the next `stop`, the next one looked for
    after where the last ended (the greedy `[^stop]*` ends it at the last tail before
    the `stop`).

    `tail(text, at)` is where the first tail at or after `at` begins (or -1), and
    `last_end(text, start, end of head, where the stop is or -1)` is where the match
    that starts at `start` ends. Neither is asked until the opening is known to have
    a tail before its `stop`; every opening between that `stop` and the one before
    the tail is passed over at once."""

    def find(page: _Page, budget: "renderfind.Budget") -> Iterator[int]:
        text = page.folded
        stops = _Ahead(lambda at: text.find(stop, at))
        tails = _Ahead(lambda at: tail(text, at))
        at = 0
        while True:
            opened = head.search(text, at)
            if opened is None:
                return
            budget.step()
            start, end = opened.start(), opened.end()
            reach = tails.first(end)
            if reach < 0:
                return  # no tail anywhere after this: nothing later has one
            closing = stops.first(end)
            if 0 <= closing < reach:
                # The tag ends before the first tail, and so does every opening up to
                # the last `stop` before it: go to the stretch the tail is in.
                at = text.rfind(stop, closing, reach) + 1
                continue
            yield start
            at = last_end(text, start, end, closing)

    return find


def _tail_search(pattern: re.Pattern[str]) -> Callable[[str, int], int]:
    return lambda text, at: _next_word(pattern, text, at)


def _tag_end(full: re.Pattern[str]) -> Callable[[str, int, int, int], int]:
    """Where the old, single pattern `full` ends a match that starts at `start`. It is
    run only where the match is known to be there, and then it reads no further than
    the `stop` after it."""

    def end(text: str, start: int, _head_end: int, _stop: int) -> int:
        found = full.match(text, start)
        return found.end() if found is not None else start + 1

    return end


def _attribute_shape(head: str, attribute: str) -> Finder:
    """`<link ... href=//x>`: a tag, then an attribute whose value is remote."""
    value = r"\s*=\s*[\"']?" + _REMOTE
    return _reaching(
        re.compile(head),
        _tail_search(re.compile(attribute + value)),
        _tag_end(re.compile(head + r"[^>]*\b" + attribute + value)),
        ">",
    )


def _import_end(text: str, _start: int, head_end: int, stop: int) -> int:
    # The last `//` before the `;` (or the end): the greedy `[^;]*` runs to the stop
    # and gives back until a remote address begins, and no address begins after the
    # last `//` and ends with one.
    return text.rfind("//", head_end, len(text) if stop < 0 else stop) + 2


_IMPORT = _reaching(
    re.compile(r"@import\b"),
    lambda text, at: text.find("//", at),
    _import_end,
    ";",
)

_FORM = re.compile(r"<form\b")
_DATA_FORM = re.compile(r"data-artifact-form\b")
_FORM_WINDOW = 2000


def _forms(page: _Page, budget: "renderfind.Budget") -> Iterator[int]:
    """Each `<form` with no `data-artifact-form` among the next 2000 characters or
    before the next `>`. Both are asked of one place that moves forward only, not
    looked for again from each form."""
    text = page.folded
    stops = _Ahead(lambda at: text.find(">", at))
    marked = _Ahead(lambda at: _next_word(_DATA_FORM, text, at))
    for opened in _FORM.finditer(text):
        budget.step()
        end = opened.end()
        mark = marked.first(end)
        closing = stops.first(end)
        reach = end + _FORM_WINDOW
        if 0 <= closing < reach:
            reach = closing
        if 0 <= mark <= reach:
            continue
        yield opened.start()


def _anchored(
    alternatives: tuple[tuple[str, re.Pattern[str]], ...], folded: bool = True
) -> Finder:
    """A shape that is several patterns, each beginning with its own literal: the
    start of each match, as the one pattern of all of them would find them.

    Where each literal is is found as a plain string is (and the pattern tried there
    only), not by trying the pattern at every character, which is five times as slow
    on an ordinary page: a pattern that begins with a choice of letters has no one
    character to look for. `folded` says which text is read: the case of the page is
    what a pattern with no flag asks for."""

    def find(page: _Page, budget: "renderfind.Budget") -> Iterator[int]:
        text = page.folded if folded else page.visible
        literals = [
            (_Ahead(lambda at, literal=literal: text.find(literal, at)), pattern)
            for literal, pattern in alternatives
        ]
        at = 0
        while True:
            where, pattern = min(
                ((ahead.first(at), pattern) for ahead, pattern in literals),
                key=lambda found: found[0] if found[0] >= 0 else len(text) + 1,
            )
            if where < 0:
                return
            budget.step()
            found = pattern.match(text, where)
            if found is None:
                at = where + 1
                continue
            yield where
            at = found.end()

    return find


_CONNECT_ADDRESS = r"\s*\(\s*[\"'`]" + _REMOTE
_CLAUDE = _anchored(
    (
        ("window", re.compile(r"\bwindow\s*\.\s*claude\b")),
        ("claude", re.compile(r"\bclaude\s*\.\s*use\s*\(")),
    ),
    folded=False,  # case matters here, so the page is read as it is
)
_CONNECT = _anchored(
    tuple(
        (name, re.compile(name + _CONNECT_ADDRESS))
        for name in ("fetch", "eventsource", "websocket", "sendbeacon")
    )
    + (
        (
            ".open",
            re.compile(
                r"\.open\s*\(\s*[\"'][a-z]+[\"']\s*,\s*[\"'`]" + _REMOTE,
            ),
        ),
    )
)


# (shape, finder, hint). The hint is what a reader needs to act: what will
# happen, and why. It never names the matched text.
_SHAPES: tuple[tuple[str, Finder, str], ...] = (
    (
        "remote_script",
        _attribute_shape(r"<script\b", "src"),
        (
            "script-src is 'self' 'unsafe-inline': a script loaded from another "
            "origin (a CDN) is blocked and never runs. Inline the code instead."
        ),
    ),
    (
        "remote_stylesheet",
        _attribute_shape(r"<link\b", "href"),
        (
            "style-src is 'self' 'unsafe-inline' and font-src is 'self': a "
            "stylesheet or web font from another origin (Google Fonts) is "
            "blocked. Use an inline <style> and the system font stack."
        ),
    ),
    (
        "remote_css_url",
        _starts(re.compile(r"url\(\s*[\"']?" + _REMOTE)),
        (
            "A CSS url() pointing at another origin -- a @font-face source or a "
            "background image -- is blocked by font-src/img-src 'self'. Inline "
            "the asset as a data: URI, or drop it."
        ),
    ),
    (
        "css_import",
        _IMPORT,
        (
            "@import from another origin is blocked by style-src 'self'. Put the "
            "rules directly in the page's own <style>."
        ),
    ),
    (
        "remote_image",
        _attribute_shape(r"<(?:img|source)\b", r"src(?:set)?"),
        (
            "img-src is 'self' data:: an image hotlinked from another origin is "
            "blocked. Inline it as a data: URI, or use inline SVG."
        ),
    ),
    (
        "media_element",
        _starts(re.compile(r"<(?:video|audio)\b")),
        (
            "There is no media-src directive, so <video>/<audio> falls back to "
            "default-src 'none' and no media plays at all -- not even same-origin "
            "or data:. A page cannot carry audio or video here."
        ),
    ),
    (
        "iframe",
        _starts(re.compile(r"<iframe\b")),
        (
            "There is no frame-src directive, so it falls back to default-src "
            "'none': a nested iframe never loads. The page is itself already "
            "framed by the gallery."
        ),
    ),
    (
        "form_element",
        # `<form\b(?![^>]{0,2000}\bdata-artifact-form\b)`: an unbounded [^>]* would
        # rescan to the end of the body from every `<form` when no `>` follows, and
        # even bounded it costs 2000 steps for each one; see `_forms`.
        _forms,
        (
            "form-action is 'none': a form cannot submit anywhere. This is the "
            "trust boundary that stops a page forging an answer. To ask a "
            'question inside the page, put data-artifact-form="<name>" on the '
            "form (its controls are read at publish); otherwise use the "
            "decisions API and a data-artifact-decision slot."
        ),
    ),
    (
        "base_element",
        _starts(re.compile(r"<base\b")),
        (
            "base-uri is 'none': a <base> tag is ignored, so relative URLs will "
            "not resolve the way the page expects."
        ),
    ),
    (
        "web_worker",
        _starts(re.compile(r"new\s+(?:shared)?worker\s*\(")),
        (
            "worker-src is 'none': a Web Worker cannot start. Do the work on the "
            "main thread, or precompute it before publishing."
        ),
    ),
    (
        "remote_connect",
        _CONNECT,
        (
            "connect-src is 'self' at most, and 'none' for a page with no live "
            "features: a request to another origin is blocked. Put the data in "
            "the page at publish time, and republish to update it."
        ),
    ),
    (
        "window_claude",
        _CLAUDE,
        (
            "window.claude is the claude.ai artifact runtime. It does not exist "
            "here -- this service serves your file as-is -- so the call resolves "
            "to nothing and any feature behind it is silently missing."
        ),
    ),
)


def newline_offsets(body: str) -> list[int]:
    """Public because `traps.py` reports in the same shape and must count lines
    the same way -- two copies of "which line is this offset on" is how two
    checks on one page start disagreeing about where a problem is."""
    return [m.start() for m in re.finditer("\n", body)]


def line_of(offset: int, newline_offsets: list[int]) -> int:
    return bisect.bisect_right(newline_offsets, offset) + 1


class _Lines:
    """`line_of` for offsets asked in increasing order, counting the newlines between
    one and the next, so that no list of every newline in the page is made."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._seen = 0
        self._line = 1

    def of(self, offset: int) -> int:
        self._line += self._text.count("\n", self._seen, offset + 1)
        self._seen = max(self._seen, offset + 1)
        return self._line


# A whole HTML comment, including the tokenizer's abrupt closes (`<!-->`,
# `<!--->`) and its incorrect one (`--!>`); an unterminated comment runs to the
# end of the document, as it does in a browser. As one pattern, `<!--(?:-?>|.*?
# (?:--!?>|\Z))` with `.` taking a newline.
_COMMENT_CLOSE = re.compile(r"--!?>")


def outside_comments(body: str, budget: "renderfind.Budget | None" = None) -> str:
    """`body` with every HTML comment blanked, newlines kept so lines still count.

    A commented-out construct never runs, so the CSP never blocks it, so a
    warning about it is a false alarm -- and a warning that fires on correct
    pages trains authors to ignore the warning. Found on 21 Sep through the
    decision-slot check (homelab-ij8.50): every shipped template with a slot
    ships it commented out, to be uncommented once the question exists, and
    the first version of that check warned every agent that used a template
    exactly as told. The same test here: a commented-out CDN `<script>` came
    back as `remote_script`.

    Each comment is found by a search that starts where the last one ended, and
    with `budget` each is one step.
    """
    opening = body.find("<!--")
    if opening < 0:
        return body
    parts: list[str] = []
    done = 0
    while opening >= 0:
        if budget is not None:
            budget.step()
        after = opening + 4
        if body.startswith(">", after):
            end = after + 1
        elif body.startswith("->", after):
            end = after + 2
        else:
            closed = _COMMENT_CLOSE.search(body, after)
            end = closed.end() if closed is not None else len(body)
        parts.append(body[done:opening])
        parts.append("\n" * body.count("\n", opening, end))
        done = end
        opening = body.find("<!--", end)
    parts.append(body[done:])
    return "".join(parts)


NOT_RUN = (
    "this page is too large or complex for the serving-policy checks (remote "
    "scripts, stylesheets, images and requests, forms, workers, media) to read in "
    "the time allowed: they did not run, so their silence on this page means nothing"
)


def check(body: str, budget: "renderfind.Budget | None" = None) -> list[CspWarning]:
    """Every construct in `body` the serving CSP will not run, bounded.

    Advisory only: this never raises on a finding and never refuses a body.
    Returns `[]` for a page with nothing to say, which is the common case.
    Nothing inside an HTML comment is examined.

    A page over `renderfind.MAX_SCAN_CHARS`, or one that uses up the `budget` (a
    `Budget` of its own when none is given), is not checked any further: the answer
    is then the one warning, `csp_checks_not_run`, and never a partial list or an
    empty one, which would read as a clean page. (`--check` takes any warning as a
    refusal; the publish response carries it to the author.)
    """
    budget = renderfind.Budget() if budget is None else budget
    try:
        if len(body) > renderfind.MAX_SCAN_CHARS:
            raise renderfind.TooComplex
        budget.check()
        return _check(body, budget)
    except renderfind.TooComplex:
        return [CspWarning(shape="csp_checks_not_run", line=1, hint=NOT_RUN)]


def _check(body: str, budget: "renderfind.Budget") -> list[CspWarning]:
    visible = outside_comments(body, budget)
    page = _Page(visible, _fold(visible))
    budget.check()
    warnings: list[CspWarning] = []
    for shape, find, hint in _SHAPES:
        lines: list[int] = []
        counted = _Lines(page.visible)
        for examined, start in enumerate(find(page, budget), start=1):
            budget.step()
            line = counted.of(start)
            if line not in lines:
                lines.append(line)
            if len(lines) >= _MAX_LINES_PER_SHAPE or examined >= _MAX_MATCHES_PER_SHAPE:
                break
        warnings.extend(CspWarning(shape=shape, line=line, hint=hint) for line in lines)
        if len(warnings) >= _MAX_WARNINGS_TOTAL:
            break
        budget.check()
    return warnings[:_MAX_WARNINGS_TOTAL]


def as_json(body: str, budget: "renderfind.Budget | None" = None) -> list[dict]:
    """`check()` in the shape the API returns it: plain dicts, ordered."""
    return [warning.__dict__ for warning in check(body, budget)]
