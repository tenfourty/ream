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
line by line): the raw body, and a tag-stripped, entity-unescaped, zero-
width-stripped TEXT view. The text view exists because the scanner's whole
input is HTML -- a credential sitting in `<td>AKIA...</td>`, split key-in-
one-cell/value-in-the-next across a table row, or split token-by-token by
syntax-highlighting spans (`<span>DB_PASSWORD</span><span>=</span>...`), is
still there once the markup is treated as what it is: nothing. `_detag()`
DELETES every tag, keeping only the newlines it contained (never more, never
fewer), so line numbers computed from a view's own newline offsets always
mean the same line in the raw body -- a tag can join or split text within a
line, but it never manufactures or deletes one. Deleting rather than
blanking with whitespace (an earlier version's approach) is deliberate:
padding with a space is exactly what let a syntax-highlighted key=value pair
split across three spans slip past every shape (fix round 3). The text view
is skipped entirely when it would be byte-identical to the raw view -- a
body with nothing to strip gains nothing from a second pass.

Findings are bounded: at most `_MAX_LINES_PER_SHAPE` distinct lines are kept
per shape and at most `_MAX_FINDINGS_TOTAL` overall, with (shape, line)
de-duplicated across both views. Before this bound existed, a single dense
1 MB body could produce on the order of 200k `Finding` objects -- Task 5's
422 echoes every shape name, so that became several MB of response body for
one publish. A shape whose matches keep landing on the same handful of lines
also stops scanning once `_MAX_RAW_MATCHES_PER_SHAPE` REAL (non-placeholder)
matches have been examined, not just once enough distinct lines are found,
because a body that repeats one match on a single line never grows past one
distinct line and would otherwise scan to the very end regardless of body
size. Placeholder matches never count toward that bound (fix round 3): a
body of 500 `DB_PASSWORD=${DB_PASSWORD}` lines followed by one real
`DB_PASSWORD=hunter2fake` line must still find the real one, and the
underlying `finditer` pass is already linear in the view regardless, so
letting placeholder matches run past the valve costs nothing extra that
Task 5's body-size cap does not already bound.
"""

import bisect
import html
import re
import string
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
# excluded. An UNESCAPED `password=<REDACTED>` with more text within three
# lines is still refused, on purpose (fix round 4): the text view deletes
# `<REDACTED>` exactly as it deletes `<br>`, and then reads the value from the
# next line. A browser hides an unescaped `<REDACTED>` as an unknown tag; a
# visible placeholder is `&lt;REDACTED&gt;`, which stays excluded. This is
# deliberately not entropy scoring -- the threat this
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

# A `<` starts a tag only when followed by a letter, `/`, `!`, or `?` --
# matching the HTML tokenizer's own rule that `<` followed by anything else
# (a space, a digit, ...) is not a tag start -- and the body class excludes
# both `<` and `>` so one tag's match can never span across another. Both
# choices matter for the same reason: the previous `<[^>]*>` was quadratic
# on any body containing a `<` with no LATER `>` at all (minified JS, R's
# `x <- 1`, C++'s `<<`), because the engine had to scan to the end of the
# body looking for a `>` that never comes, for every such `<`. Measured: a
# clean 4 MiB page cost 204.6 s of CPU under the old pattern; this one is
# linear (~6 ms on 256 KiB where the old one cost 748 ms).
_HTML_TAG = re.compile(r"<[A-Za-z/!?][^<>]*>")

# Soft hyphen and the zero-width family: characters a syntax highlighter or
# an editor's line-wrap hint inserts that are invisible (or nearly so) to a
# human reading the page, but which break a shape's regex mid-token exactly
# as markup would -- `AKIA<soft-hyphen>IOSFODNN7EXAMPLE`, `AKIA&shy;...` once
# unescaped. Stripped from the text view only, after entities are decoded
# (an entity like `&shy;` decodes TO one of these, so stripping must happen
# after `html.unescape`, not before).
# str.translate() keys a mapping by CODE POINT (an int), not by character --
# a dict keyed by the characters themselves is silently ignored, matching
# nothing (a real bug caught here in fix round 3's own tests: an earlier
# draft did exactly this and stripped nothing at all).
_ZERO_WIDTH = dict.fromkeys((0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF), None)

# ASCII-only, length-preserving lowercase. str.translate over this table
# never changes a string's length; str.lower() can -- U+0130 (LATIN CAPITAL
# LETTER I WITH DOT ABOVE) lowercases to TWO code points -- which would
# shift every offset after it in a lowered copy relative to the original,
# corrupting both line numbers and placeholder value extraction for
# anything found past it. Only A-Z is remapped; every other character,
# ASCII or not, passes through unchanged.
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)

# Matches an inline scoped case-insensitive group whose body is PLAIN LITERAL
# text, e.g. `(?i:password=)`, so it can be folded to lowercase and the flag
# stripped -- see _fast_pattern. The body may hold no regex metacharacter
# at all, only escaped punctuation (`\.`, `\-`). That is the only kind of
# group the fold keeps correct: lowercasing `\S` gives `\s`, `\x41` never
# matches a lowercased body, and a class, alternation, nested group or
# quantifier inside the group is either mis-parsed here or matches
# something else once lowered. parse_shapes refuses any `(?i:` that does not
# open a group of this kind, so such a shape cannot load at all.
_INLINE_CASE_INSENSITIVE = re.compile(r"\(\?i:((?:[^\\.^$*+?{}\[\]()|]|\\[^A-Za-z0-9])*)\)")

_VALUE_BOUNDARY = re.compile(r"""[\s"'&;,]""")

# Where an assignment's value starts. Skipped first: whatever sits between
# the operator and the value without being the value -- whitespace of any
# kind except a line break (a non-breaking space, `&nbsp;` once unescaped,
# hides a value no better than a plain space does) and quotes
# (`password = "x"`). If that reaches the end of the line, the value is on a
# later line (fix round 4): the match goes on through at most
# _VALUE_LOOKAHEAD_LINES blank lines -- each a line break plus the same
# skippable run -- and the value is the first token of the next non-blank
# line. Landing on a line break or the end of the text after that means the
# value is genuinely empty. The pattern is unambiguous -- the two skipped
# classes are disjoint, and each repetition starts with a line break neither
# of them matches -- so it never backtracks; and the runs it skips are
# blank, so no two matches' runs overlap and the total work over a view
# stays linear.
_VALUE_LOOKAHEAD_LINES = 3
_VALUE_START = re.compile(
    rf"""(?:[^\S\n]|['"])*(?:\n(?:[^\S\n]|['"])*){{0,{_VALUE_LOOKAHEAD_LINES}}}"""
)

# How much of a value token is read for classification, counted from where
# the token starts (not from the operator, so leading whitespace cannot push
# the value out of reach).
_VALUE_WINDOW = 200

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
        # Every `(?i:` must open a plain-literal group: a safe group holds no
        # `(`, so each one accounts for exactly one `(?i:`, and any shortfall
        # is a group _fast_pattern would fold wrongly.
        if pattern.count("(?i:") != len(_INLINE_CASE_INSENSITIVE.findall(pattern)):
            raise ShapesError(
                f"secret shape line {number} ({name}): a (?i:...) group must be plain literal"
                " text -- scan() lowercases it and searches a lowercased copy of the body,"
                " which is only the same search for literal text"
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


def _fast_pattern(compiled: re.Pattern) -> tuple[re.Pattern, bool]:
    """A `(pattern, needs_lowered_view)` pair for fast matching.

    Python's `re` has no literal fast path for an inline scoped
    case-insensitive group: profiling a clean 1 MiB body showed the 13
    `(?i:...)` shapes here costing about 125 ms per view against ~12 ms for
    a same-length case-sensitive literal. Folding the `(?i:...)` group's
    literal content to lowercase and dropping the flag turns it into a
    plain literal search against a pre-lowered copy of the view instead --
    every original-case form collapses to the same lowered text, so a
    case-sensitive search for the lowered literal against the lowered view
    finds exactly what the case-insensitive search against the original
    would have. Every other pattern (no inline flag) is returned unchanged,
    to search the view as-is: `needs_lowered=False`.

    Only the flagged literal is folded, not the whole pattern -- bearer_token
    is `(?i:bearer)\\s+[A-Za-z0-9._~+/-]{16,}`, where the value class outside
    the flagged group already matches lowercase characters, so it needs no
    change to keep working against a lowered body.
    """
    source = compiled.pattern
    if "(?i:" not in source:
        return compiled, False
    folded = _INLINE_CASE_INSENSITIVE.sub(lambda m: m.group(1).lower(), source)
    return re.compile(folded, re.MULTILINE), True


def _detag(text: str) -> str:
    """A tag-free view of `text`: every HTML tag is DELETED, keeping only the
    newlines it contained (never more, never fewer), so line numbers
    computed from this view's own newline offsets still mean the same line
    in the raw body.

    Deleting rather than replacing with whitespace (an earlier version's
    approach) is deliberate: padding a tag with a space splits any token
    whose markup runs through its MIDDLE, which is exactly the shape
    syntax-highlighted config output takes --
    `<span>DB_PASSWORD</span><span>=</span><span>hunter2fake</span>` -- and
    hid it from both views (fix round 3). Deleting glues the pieces back
    together instead, which can only ever add a match, never lose one.
    """

    def _replace(match: re.Match) -> str:
        return "\n" * match.group(0).count("\n")

    return _HTML_TAG.sub(_replace, text)


def _text_view(raw_view: str) -> str:
    """The raw view with tags deleted, entities unescaped, and soft-hyphen/
    zero-width characters removed -- what exposes a credential that markup,
    an HTML entity, or an invisible line-wrap hint runs through. Entities
    are unescaped before the zero-width strip, not after, because an entity
    such as `&shy;` decodes TO a zero-width character in the first place.
    """
    return html.unescape(_detag(raw_view)).translate(_ZERO_WIDTH)


def _value_after(text: str, start: int) -> str:
    """The value token following an assignment match that ends at `start`:
    bounded at the next whitespace, quote, `&`, `;`, `,`, or the end of a
    `_VALUE_WINDOW`-character window. For placeholder classification only --
    never returned, logged, or included in a `Finding`. Only ever called for
    a shape whose match already ends exactly at the assignment operator, so
    no separator needs stripping; `_VALUE_START` skips leading whitespace and
    quotes so the token itself, not its wrapper, gets classified.

    With nothing after the operator on its own line, the token is the first
    one on the next non-blank line, at most `_VALUE_LOOKAHEAD_LINES` lines
    ahead: `password=` with `hunter2` on the next line has a value, not "no
    value" (fix round 4 -- reading it as empty made it a placeholder, and
    the credential was dropped). With no non-blank line in reach, the value
    is genuinely empty, and an empty value is a placeholder.

    A `${...}`/`{{...}}`/`<...>` wrapper's OWN closing delimiter is found
    first, and the boundary search resumes only after it: `{{ vault_secret
    }}` has a plain space inside the braces that is not a value boundary,
    while `${PW}hunter2fake` has real content trailing the closing brace
    that very much is one. Skip only past a wrapper that actually closes;
    an unclosed one falls through to the plain boundary search unchanged.
    """
    # Everything below works on `text` in place, slicing out only the token
    # itself: this runs once per assignment match, and a placeholder flood
    # is one match every few characters. The skipping regex runs only when
    # the operator is followed by whitespace or a quote at all.
    end = len(text)
    if start == end:
        return ""
    first = text[start]
    pos = start
    if first.isspace() or first in "'\"":
        pos = _VALUE_START.match(text, start).end()
        if pos == end:
            return ""
        first = text[pos]
    # `first` is now neither whitespace (bar a line break) nor a quote, so
    # the token is empty exactly when `first` is one of _VALUE_BOUNDARY's
    # other characters.
    if first in "\n&;,":
        return ""
    limit = pos + _VALUE_WINDOW
    skip = pos
    if first == "$" and text.startswith("${", pos):
        close = text.find("}", pos, limit)
        skip = close + 1 if close != -1 else pos
    elif first == "{" and text.startswith("{{", pos):
        close = text.find("}}", pos, limit)
        skip = close + 2 if close != -1 else pos
    elif first == "<":
        close = text.find(">", pos, limit)
        skip = close + 1 if close != -1 else pos
    boundary = _VALUE_BOUNDARY.search(text, skip, limit)
    return text[pos : boundary.start() if boundary else limit]


def _is_placeholder(shape: str, match: re.Match, text: str) -> bool:
    """Whether `match`'s value is plainly a placeholder. Scoped deliberately
    to shapes that actually have a value to classify (see
    `_ASSIGNMENT_SHAPES`); every other shape always returns False here --
    it always fires once matched.

    `text` must be the ORIGINAL (not lowered) view: `match`'s offsets are
    valid there regardless of which text the match itself was found
    against, because the fast-path lowering is length-preserving, but the
    VALUE read back for classification must keep its original case and
    content, not the lowered stand-in used only to locate it.
    """
    named = match.groupdict().get("value")
    if named is not None:
        value = named.strip(" \t\r\n'\"")
    elif shape in _ASSIGNMENT_SHAPES:
        # _value_after's token never starts or ends with whitespace or a
        # quote, so there is nothing to strip.
        value = _value_after(text, match.end())
    else:
        return False
    return not value or _PLACEHOLDER.fullmatch(value) is not None


def _newline_offsets(view: str) -> list[int]:
    return [m.start() for m in re.finditer("\n", view)]


def _line_of(offset: int, newline_offsets: list[int]) -> int:
    return bisect.bisect_right(newline_offsets, offset) + 1


def _shape_lines(
    name: str,
    pattern: re.Pattern,
    needs_lowered: bool,
    views: list[tuple[str, str]],
    offsets_for,
) -> list[int]:
    """The distinct lines (up to `_MAX_LINES_PER_SHAPE`) where `pattern`
    matches something that isn't a placeholder, across both `(view,
    lowered)` pairs -- de-duplicated, and stopping early either once enough
    lines are found or once `_MAX_RAW_MATCHES_PER_SHAPE` REAL matches have
    been examined. A placeholder match is never counted toward that second
    bound (see module docstring): only a real (non-placeholder) match
    advances `real_examined`, so a flood of placeholder matches ahead of one
    real secret cannot let the real one through unexamined.

    `pattern` searches `lowered` when `needs_lowered` is true (the
    fast-path form for an originally case-insensitive shape), but every
    other function here -- line lookup, placeholder classification -- is
    always given `view`, the original, never `lowered`: the two are the
    same length, so a match offset found in one is valid in the other, but
    only `view` has the right case and content to classify or report.
    """
    lines: dict[int, None] = {}
    real_examined = 0
    for index, (view, lowered) in enumerate(views):
        search_text = lowered if needs_lowered else view
        for match in pattern.finditer(search_text):
            if _is_placeholder(name, match, view):
                continue
            real_examined += 1
            lines.setdefault(_line_of(match.start(), offsets_for(index)), None)
            if len(lines) >= _MAX_LINES_PER_SHAPE or real_examined >= _MAX_RAW_MATCHES_PER_SHAPE:
                break
        if len(lines) >= _MAX_LINES_PER_SHAPE or real_examined >= _MAX_RAW_MATCHES_PER_SHAPE:
            break
    return list(lines)


def scan(text: str) -> list[Finding]:
    """Return every (shape, line) a shape pattern matched in `text` -- across
    the raw body and (unless it would be identical) a tag-stripped, entity-
    unescaped, zero-width-stripped text view -- minus any match whose value
    is plainly a placeholder, bounded to at most `_MAX_LINES_PER_SHAPE`
    lines per shape and `_MAX_FINDINGS_TOTAL` overall.

    An empty list means clean. It never means "the scanner didn't run" --
    a failure to load shapes raises `ShapesError` at import time instead,
    so a caller checking `scan(body) == []` is never looking at a scanner
    that silently gave up.

    Newline-offset tables are built lazily, at most once per view, only if
    something in that view actually needs a line number -- a clean body (the
    common case) never pays for one at all. `SHAPES` is re-read fresh on
    every call, not cached at import time, so a caller that swaps it out
    (only ever done in tests) is honoured rather than silently bypassed by a
    stale fast-path cache.
    """
    raw_view = "\n".join(text.splitlines())
    text_view = _text_view(raw_view)
    view_texts = (raw_view,) if text_view == raw_view else (raw_view, text_view)
    views = [(view, view.translate(_ASCII_LOWER)) for view in view_texts]

    # Keyed by the view's index in `views`, so each view gets its own table.
    offsets_cache: dict[int, list[int]] = {}

    def offsets_for(index: int) -> list[int]:
        cached = offsets_cache.get(index)
        if cached is None:
            cached = _newline_offsets(views[index][0])
            offsets_cache[index] = cached
        return cached

    findings: list[Finding] = []
    for name, pattern in SHAPES:
        fast_pattern, needs_lowered = _fast_pattern(pattern)
        for line in _shape_lines(name, fast_pattern, needs_lowered, views, offsets_for):
            findings.append(Finding(shape=name, line=line))
        if len(findings) >= _MAX_FINDINGS_TOTAL:
            break
    findings.sort(key=lambda f: (f.line, f.shape))
    return findings[:_MAX_FINDINGS_TOTAL]
