"""The local secret-shape scanner.

Runs against every body an agent asks to publish, and against every state
document read back (D12, D15) -- ggshield is the authoritative gate but only
runs in the deploy pipeline with Jeremy's token, so this is the one scanner
that always runs.

A `Finding` says which shape fired and on which line, never what matched: a
scanner that leaks the credential it caught is worse than no scanner. Do not
add a field, a log line, or an exception message anywhere in this module that
carries scanned text.

`scan()` checks two VIEWS of the body against every shape, once each (not
line by line): the raw body, and a tag-stripped, entity-unescaped TEXT view.
The text view exists because the scanner's whole input is HTML -- a credential
sitting in `<td>AKIA...</td>`, or split key-in-one-cell/value-in-the-next
across a table row, is still there once the markup is treated as what it is,
whitespace, rather than as part of the value. `_detag()` replaces every tag
with the SAME number of newlines it contained (never more, never fewer), so
line numbers computed from a view's own newline offsets always mean the same
line in the raw body -- a tag can split or join text within a line, but it
never manufactures or deletes one.

Findings are bounded: at most `_MAX_LINES_PER_SHAPE` distinct lines are kept
per shape and at most `_MAX_FINDINGS_TOTAL` overall, with (shape, line)
de-duplicated across both views. Before this bound existed, a single dense
1 MB body could produce on the order of 200k `Finding` objects -- Task 5's
422 echoes every shape name, so that became several MB of response body for
one publish. A shape whose matches keep landing on the same handful of lines
also stops scanning once `_MAX_RAW_MATCHES_PER_SHAPE` matches have been
examined, not just once enough distinct lines are found, because a body that
repeats one match on a single line never grows past one distinct line and
would otherwise scan to the very end regardless of body size.
"""

import bisect
import html
import re
from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class Finding:
    shape: str
    line: int


class ShapesError(Exception):
    """The shape patterns could not be loaded or parsed.

    Raised rather than falling back to fewer shapes, or none: a caller uses
    "no findings" as "safe to publish", so a scanner that quietly loses
    coverage is indistinguishable from one that found nothing. Failing to
    load must fail closed -- an exception the caller cannot mistake for a
    clean scan -- not silently shrink what gets checked.
    """


# \A/\Z anchor the whole body regardless of where a match starts scanning
# from, and mean something different from ^/$ under MULTILINE (per-line).
# Since scan() searches each view in one pass, a shape that reaches for one
# of these almost certainly means "start/end of a line" -- reject it at load
# time rather than let it silently mean "start/end of the body".
_FORBIDDEN_ANCHORS = ("\\A", "\\Z")

# Shapes whose own pattern ends exactly where a value begins: an assignment
# operator. Only these -- plus any shape whose regex captures the value
# directly via a named `value` group (url_inline_credentials) -- have a
# "value" to classify as a placeholder at all. Every other shape (a bare key
# name, a header, a format-strict value like an AWS key id or a PEM header)
# always fires once matched, full stop. Applying placeholder logic to those
# was fix round 1's regression: `</td>` sitting right after an AWS key id is
# not "the AWS key's value", it is markup that happens to follow it, and
# treating it as one hid the key in an HTML table cell.
_ASSIGNMENT_SHAPES = frozenset(
    {
        "password_assignment",
        "passwd_assignment",
        "pass_assignment",
        "token_assignment",
        "secret_assignment",
        "private_key_field",
        "preshared_key_field",
    }
)

# Values that are plainly placeholders, not accidentally-included secrets:
# angle-bracket/shell/template fill-ins, redaction runs, and the handful of
# words people actually type in place of a real credential. Checked with
# `fullmatch`, never a prefix `match`: `changeme2026!Xq9` and `xxxYfakeZ` are
# real-looking values that merely start the way a placeholder does, and a
# prefix check excluded them wrongly (fix round 1's other defect). The
# angle-bracket form is deliberately narrow -- placeholder-style content
# only (letters, digits, `_ .-`) -- never an arbitrary HTML tag: `</td>` must
# not fullmatch this, or every value immediately followed by markup would be
# excluded. This is deliberately not entropy scoring -- the threat this
# guards against is an agent accidentally including a real secret, not an
# adversary evading a gate, and a real secret does not look like `${FOO}`.
_PLACEHOLDER = re.compile(
    r"""(
        <[A-Za-z0-9_ .-]+>  # <YOUR_TOKEN>, <your-token-here>, <PASSWORD>
        |\$\{[^}\n]*\}      # ${VAR}
        |\{\{[^}\n]*\}\}    # {{ template }}
        |[*xX]{3,}          # ****, xxxxxxxx, XXXXXXXX (masked)
        |changeme
        |example
        |redacted
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# A naive tag matcher, not an HTML parser: it can misfire on a stray `< >`
# pair in prose or code (e.g. `5 < 10 > 3`), but that only ever makes the
# TEXT view blind to that one span -- the RAW view still sees it unchanged,
# and findings from both views are merged, so nothing is lost by erring
# toward stripping too much here.
_HTML_TAG = re.compile(r"<[^>]*>")

_VALUE_BOUNDARY = re.compile(r"""[\s"'&;,]""")

_MAX_LINES_PER_SHAPE = 5
_MAX_RAW_MATCHES_PER_SHAPE = 500
_MAX_FINDINGS_TOTAL = 50


def parse_shapes(text: str) -> list[tuple[str, re.Pattern]]:
    """Parse `name<TAB>pattern` lines (blank lines and `#` comments ignored).

    Every line must supply both a name and a non-empty pattern: a line with
    no tab, or an empty pattern either side of one, is rejected rather than
    silently becoming a shape whose regex is the empty string -- an empty
    pattern matches every line, so one malformed line would turn into a
    scanner that flags everything it is ever given. A pattern that matches
    the empty string once compiled is rejected for the same reason, a
    duplicate name is rejected because it would let one shape silently
    shadow another's identity in a Finding, and \\A/\\Z are rejected because
    scan() matches over a whole view, not per line (see module docstring).
    """
    shapes: list[tuple[str, re.Pattern]] = []
    seen: set[str] = set()
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, pattern = line.partition("\t")
        if not sep:
            raise ShapesError(f"secret shape line {number}: no <TAB> between name and pattern")
        name = name.strip()
        if not name or not pattern:
            raise ShapesError(f"secret shape line {number}: empty name or pattern")
        if name in seen:
            raise ShapesError(f"secret shape line {number}: duplicate shape name {name!r}")
        if any(token in pattern for token in _FORBIDDEN_ANCHORS):
            raise ShapesError(
                f"secret shape line {number} ({name}): \\A/\\Z anchor the whole body -- use"
                " ^/$ with the (implicit) MULTILINE compile if a line boundary is meant"
            )
        try:
            compiled = re.compile(pattern, re.MULTILINE)
        except re.error as exc:
            raise ShapesError(f"secret shape line {number} ({name}): invalid pattern") from exc
        if compiled.search(""):
            raise ShapesError(
                f"secret shape line {number} ({name}): pattern matches the empty string --"
                " it would flag every line of every body"
            )
        shapes.append((name, compiled))
        seen.add(name)
    if not shapes:
        raise ShapesError("no secret shapes parsed -- refusing to scan with zero shapes")
    return shapes


def _load_shapes() -> list[tuple[str, re.Pattern]]:
    text = resources.files("artifacts").joinpath("secret_shapes.txt").read_text(encoding="utf-8")
    return parse_shapes(text)


SHAPES = _load_shapes()


def _detag(text: str) -> str:
    """A tag-free view of `text`: every HTML tag becomes a single leading and
    trailing space around the SAME number of newlines the tag itself
    contained. Never more, never fewer -- a tag can split or join text
    within a line, but this must never add or remove a line, since line
    numbers for this view are computed from its own newline offsets and are
    meant to mean the same line in the raw body.
    """

    def _replace(match: re.Match) -> str:
        return " " + ("\n" * match.group(0).count("\n")) + " "

    return _HTML_TAG.sub(_replace, text)


def _value_after(text: str, start: int) -> str:
    """The value token immediately following an assignment match: bounded at
    the next whitespace, quote, `&`, `;`, `,`, or end of text. For
    placeholder classification only -- never returned, logged, or included
    in a `Finding`. Only ever called for a shape whose match already ends
    exactly at the assignment operator, so no separator needs stripping;
    a leading space or quote (`password = "x"`) is stripped before the
    boundary search so the token itself, not its wrapper, gets classified.

    A `${...}`/`{{...}}`/`<...>` wrapper's OWN closing delimiter is found
    first, and the boundary search resumes only after it: `{{ vault_secret
    }}` has a plain space inside the braces that is not a value boundary,
    while `${PW}hunter2fake` has real content trailing the closing brace
    that very much is one. Skip only past a wrapper that actually closes;
    an unclosed one falls through to the plain boundary search unchanged.
    """
    window = text[start : start + 200].lstrip(" \t'\"")
    skip = 0
    if window.startswith("${"):
        end = window.find("}")
        skip = end + 1 if end != -1 else 0
    elif window.startswith("{{"):
        end = window.find("}}")
        skip = end + 2 if end != -1 else 0
    elif window.startswith("<"):
        end = window.find(">")
        skip = end + 1 if end != -1 else 0
    boundary = _VALUE_BOUNDARY.search(window, skip)
    return window[: boundary.start()] if boundary else window


def _is_placeholder(shape: str, match: re.Match, text: str) -> bool:
    """Whether `match`'s value is plainly a placeholder. Scoped deliberately
    to shapes that actually have a value to classify (see
    `_ASSIGNMENT_SHAPES`); every other shape always returns False here --
    it always fires once matched.
    """
    named = match.groupdict().get("value")
    if named is None and shape not in _ASSIGNMENT_SHAPES:
        return False
    value = named if named is not None else _value_after(text, match.end())
    stripped = value.strip(" \t\r\n'\"")
    if not stripped:
        return True
    return _PLACEHOLDER.fullmatch(stripped) is not None


def _newline_offsets(view: str) -> list[int]:
    return [m.start() for m in re.finditer("\n", view)]


def _line_of(offset: int, newline_offsets: list[int]) -> int:
    return bisect.bisect_right(newline_offsets, offset) + 1


def _shape_lines(
    name: str, pattern: re.Pattern, views: list[tuple[str, list[int]]]
) -> list[int]:
    """The distinct lines (up to `_MAX_LINES_PER_SHAPE`) where `pattern`
    matches something that isn't a placeholder, across both views --
    de-duplicated, and stopping early either once enough lines are found or
    once `_MAX_RAW_MATCHES_PER_SHAPE` matches have been examined regardless.
    The second bound matters even when the first never triggers: a body
    that repeats one match on a single line keeps `lines` at size 1 forever
    and would otherwise scan to the end of a body of any size.
    """
    lines: dict[int, None] = {}
    examined = 0
    for view, offsets in views:
        for match in pattern.finditer(view):
            examined += 1
            if not _is_placeholder(name, match, view):
                lines.setdefault(_line_of(match.start(), offsets), None)
            if len(lines) >= _MAX_LINES_PER_SHAPE or examined >= _MAX_RAW_MATCHES_PER_SHAPE:
                break
        if len(lines) >= _MAX_LINES_PER_SHAPE or examined >= _MAX_RAW_MATCHES_PER_SHAPE:
            break
    return list(lines)


def scan(text: str) -> list[Finding]:
    """Return every (shape, line) a shape pattern matched in `text` -- across
    the raw body and a tag-stripped, entity-unescaped text view -- minus any
    match whose value is plainly a placeholder, bounded to at most
    `_MAX_LINES_PER_SHAPE` lines per shape and `_MAX_FINDINGS_TOTAL` overall.

    An empty list means clean. It never means "the scanner didn't run" --
    a failure to load shapes raises `ShapesError` at import time instead,
    so a caller checking `scan(body) == []` is never looking at a scanner
    that silently gave up.
    """
    raw_view = "\n".join(text.splitlines())
    text_view = html.unescape(_detag(raw_view))
    views = [(raw_view, _newline_offsets(raw_view)), (text_view, _newline_offsets(text_view))]

    findings: list[Finding] = []
    for name, pattern in SHAPES:
        for line in _shape_lines(name, pattern, views):
            findings.append(Finding(shape=name, line=line))
        if len(findings) >= _MAX_FINDINGS_TOTAL:
            break
    findings.sort(key=lambda f: (f.line, f.shape))
    return findings[:_MAX_FINDINGS_TOTAL]
