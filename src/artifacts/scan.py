"""The local secret-shape scanner.

Runs against every body an agent asks to publish, and against every state
document read back (D12, D15) -- ggshield is the authoritative gate but only
runs in the deploy pipeline with Jeremy's token, so this is the one scanner
that always runs.

A `Finding` says which shape fired and on which line, never what matched: a
scanner that leaks the credential it caught is worse than no scanner. Do not
add a field, a log line, or an exception message anywhere in this module that
carries scanned text.

`scan()` searches the WHOLE body once per shape (`re.MULTILINE`, `finditer`),
not once per line: a 22-pattern-times-line loop cost about 4 s of CPU on a
1 MB newline-dense body, and `re` holds the GIL, so that stalled the entire
process -- including its own health check -- for the duration of one
publish. Line numbers for a match are derived after the fact from its
character offset, via a newline-offset table built lazily (only when there
is at least one match to report), so the common clean-body case pays for
building that table not at all.
"""

import bisect
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
# Since scan() now searches the whole body in one pass, a shape that reaches
# for one of these almost certainly means "start/end of a line" -- reject it
# at load time rather than let it silently mean "start/end of the body".
_FORBIDDEN_ANCHORS = ("\\A", "\\Z")

# Shapes whose own pattern ends at an assignment operator, so a match with
# nothing after it genuinely means "no value was written" -- unlike a match
# that IS the whole secret (aws_access_key_id) or a header that only
# announces one follows (the PEM/age/PGP headers, x_auth_token_header),
# where "nothing after" carries no such meaning and must not be treated as a
# placeholder. Keep this in sync by hand if an assignment-style shape is
# added; the safe direction if it drifts is to under-list here, which only
# means a genuinely-empty value still gets flagged (fails closed).
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
# words people actually type in place of a real credential. This is
# deliberately not entropy scoring -- the threat this guards against is an
# agent accidentally including a real secret, not an adversary evading a
# gate, and a real secret does not look like `${FOO}`.
_PLACEHOLDER = re.compile(
    r"""^(
        <[^>\n]*>       # <placeholder>
        |\$\{[^}\n]*\}  # ${VAR}
        |\{\{[^}\n]*\}\} # {{ template }}
        |[*xX]{3,}      # ****, xxxxxxxx, XXXXXXXX (masked)
        |changeme
        |example
        |redacted
    )""",
    re.IGNORECASE | re.VERBOSE,
)


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
    scan() now matches over the whole body, not per line (see module
    docstring).
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


def _value_after(text: str, start: int) -> str:
    """A short, bounded slice of `text` starting at `start`, for placeholder
    classification only -- never returned, logged or included in a
    `Finding`. Leading separator/quote/newline characters are stripped so a
    value like `password="changeme"` or a PEM header's own trailing newline
    do not hide the token this is meant to classify.
    """
    return text[start : start + 80].lstrip(" \t\r\n=:'\"")


def _is_placeholder(shape: str, match: re.Match, text: str) -> bool:
    named = match.groupdict().get("value")
    value = named if named is not None else _value_after(text, match.end())
    stripped = value.strip(" \t\r\n'\"")
    if not stripped:
        # Only for a shape whose pattern IS an assignment operator, or a
        # shape whose regex captures the value directly, does "nothing
        # here" mean "no value was written". For every other shape -- the
        # whole match is the secret, or the match is a header announcing
        # one follows -- "nothing after" is the ordinary case and must
        # still be reported.
        return named is not None or shape in _ASSIGNMENT_SHAPES
    return _PLACEHOLDER.match(stripped) is not None


def scan(text: str) -> list[Finding]:
    """Return every (shape, line) a shape pattern matched in `text`, minus
    any match whose value is plainly a placeholder (see `_PLACEHOLDER`).

    An empty list means clean. It never means "the scanner didn't run" --
    a failure to load shapes raises `ShapesError` at import time instead,
    so a caller checking `scan(body) == []` is never looking at a scanner
    that silently gave up.
    """
    normalized = "\n".join(text.splitlines())
    raw: list[tuple[int, str]] = []
    for name, pattern in SHAPES:
        for match in pattern.finditer(normalized):
            if _is_placeholder(name, match, normalized):
                continue
            raw.append((match.start(), name))
    if not raw:
        return []
    newline_offsets = [m.start() for m in re.finditer("\n", normalized)]
    findings = [
        Finding(shape=name, line=bisect.bisect_right(newline_offsets, offset) + 1)
        for offset, name in raw
    ]
    findings.sort(key=lambda f: (f.line, f.shape))
    return findings
