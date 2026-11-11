"""The local secret-shape scanner.

Runs against every body an agent asks to publish, and against every state
document read back (D12, D15) -- ggshield is the authoritative gate but only
runs where a plug-in registers the `gitguardian` scanner (scanners.py), so this is
the one scanner that always runs.

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
import functools
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
        |&lt;[A-Za-z0-9_ .-]+&gt;  # the same, HTML-escaped, as the raw view reads it
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
# after `html.unescape`, not before). Removed with str.replace, never
# str.translate: translate is fast only on ASCII text, and one emoji
# anywhere in a 512 KiB page made it cost about 50 ms (fix round 5).
_INVISIBLE = ("\u00ad", "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff")

# ASCII-only, length-preserving lowercase: only A-Z is remapped, every other
# character, ASCII or not, passes through unchanged. str.lower() is never
# used on a view: U+0130 (LATIN CAPITAL LETTER I WITH DOT ABOVE) lowercases
# to TWO code points, which would shift every offset after it in a lowered
# copy, corrupting line numbers and placeholder value extraction; and the
# Kelvin sign lowercases to an ASCII `k`, which a negated class such as
# `[^A-Za-z]` then refuses although it matched the original (fix round 5).
# _lower_ascii applies this table through UTF-8 bytes; the result is the
# same as `view.translate(_ASCII_LOWER)`.
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_ASCII_LOWER_BYTES = bytes.maketrans(
    string.ascii_uppercase.encode("ascii"), string.ascii_lowercase.encode("ascii")
)


def _lower_ascii(view: str) -> str:
    """`view` with A-Z lowered and nothing else changed.

    str.translate has a fast path for ASCII text only: with a single
    character outside ASCII anywhere in a 512 KiB view it took about 50 ms
    against 1.4 ms (fix round 5). bytes.translate has no such cliff, and in
    UTF-8 the bytes 0x41-0x5A only ever encode A-Z -- every byte of a
    multi-byte character is 0x80 or above -- so translating the encoded view
    and decoding it lowers exactly A-Z. `surrogatepass` carries a lone
    surrogate (JSON can deliver one) through unchanged.
    """
    encoded = view.encode("utf-8", "surrogatepass")
    return encoded.translate(_ASCII_LOWER_BYTES).decode("utf-8", "surrogatepass")


# The grammar of a scoped case-insensitive group whose body is PLAIN LITERAL
# text, e.g. `(?i:password=)`, matched at a group's opening `(`: the body may
# hold no regex metacharacter at all, only escaped punctuation (`\.`, `\-`,
# `\)`). That is the only kind of group the fold keeps correct: lowercasing
# `\S` gives `\s`, `\x41` never matches a lowercased body, and a class,
# alternation, nested group or quantifier inside the group matches
# something else once lowered. The group's own closing `)` is where this
# grammar stops, never the first `)` in the text -- the body can hold `\)`.
_INLINE_CASE_INSENSITIVE = re.compile(
    r"\(\?i:((?:[^\\.^$*+?{}\[\]()|]|\\[^A-Za-z0-9])*)\)"
)

# A shape with a `(?i:...)` group is searched, in full, over an ASCII-
# lowercased copy of the body, with each group folded into a non-capturing
# group of its lowercased text (see _fast_pattern). parse_shapes refuses any
# such shape the lowered search would not match the same way (fix rounds 4
# and 5):
#   - any non-ASCII character: the copy lowers A-Z only, so `(?i:contraseña)`
#     folded to `contraseña` never meets the `Ñ` of `CONTRASEÑA`, and `İ`
#     lowercases to two code points;
#   - verbose mode (an inline flag set naming `x`) and `(?#...)` comments:
#     either can hide a quantifier from anything reading the pattern's text,
#     `(?x)x(?i:tok) ?` or `x(?i:tok)(?#c)?`;
#   - a `(?i:` that is not a real group -- inside a character class, or
#     straight after a backslash -- or whose body is not plain literal text;
#   - a quantifier straight after a group. Folding into `(?:...)` already
#     keeps such a quantifier on the whole group; refusing it as well means
#     neither safeguard is the only one;
#   - an uppercase letter outside a character class;
#   - a class naming an uppercase letter without its lowercase counterpart
#     (`[A-Z0-9]`, `[A-Za-f]`), since the lowered text only ever offers the
#     counterpart. A wider lowercase side is fine: `[0-9A-Fa-f]`, `[A-Fa-z]`.
#     For a NEGATED class the same holds the other way round too: `[^a-z]`
#     matches `A` in the original and not the `a` it becomes, so a negated
#     class must name both cases of every letter it names, or neither;
#   - any escape outside the case-neutral set below: `\x41`, `\u0041`,
#     `\N{...}` and numeric escapes can name an uppercase letter unseen.
# The pattern is read token by token, the way `re` reads it -- escapes and
# classes skipped whole -- never searched as text: `\(?P<Abc>` is a literal
# paren followed by the text `P<Abc>`, not a group name, and `[(?i:=)]` is
# a class, not a group.
_CASE_NEUTRAL_ESCAPES = frozenset("sSdDwWbBtnrfv")
_VERBOSE_OR_COMMENT = re.compile(r"\(\?(?:[A-Za-z-]*x|#)")
# At a `(` outside a class: syntax whose letters are not matched text -- a
# group name, a named backreference, an inline flag set.
_GROUP_SYNTAX = re.compile(r"\(\?(?:P<[^>]*>|P=[^)]*\)|[aiLmsux]*(?:-[imsx]*)?[:)])")


def _case_insensitive_groups(pattern: str) -> list[tuple[int, int]] | None:
    """The `(start, end)` span of every `(?i:...)` group in `pattern`, found
    by reading the pattern as `re` does, or None if any `(?i:` in it is not
    a real, plain-literal, unquantified group (see above)."""
    groups: list[tuple[int, int]] = []
    in_class = False
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "\\":
            i += 2
            continue
        if in_class:
            in_class = char != "]"
            i += 1
            continue
        if char == "[":
            i += 1
            if pattern.startswith("^", i):
                i += 1
            if pattern.startswith("]", i):  # a leading `]` is a literal
                i += 1
            in_class = True
            continue
        if pattern.startswith("(?i:", i):
            group = _INLINE_CASE_INSENSITIVE.match(pattern, i)
            if group is None or (i and pattern[i - 1] == "\\"):
                return None
            if pattern[group.end() : group.end() + 1] in ("?", "*", "+", "{"):
                return None
            groups.append(group.span())
            i = group.end()
            continue
        i += 1
    # Any `(?i:` not counted above sits in a class or an escape.
    return groups if len(groups) == pattern.count("(?i:") else None


@functools.cache
def _fold(source: str) -> str | None:
    """`source` with each `(?i:...)` group replaced by a non-capturing group
    of its lowercased text, or None if it has a `(?i:` that is not a real
    plain-literal group. A non-capturing group, never bare text: bare text
    re-attaches whatever follows -- `(?i:tok=)?` would become `tok=?`, and
    `x{1,(?i:2)}` the quantifier `x{1,2}`."""
    groups = _case_insensitive_groups(source)
    if groups is None:
        return None
    parts = []
    last = 0
    for start, end in groups:
        parts += [source[last:start], "(?:", source[start + 4 : end - 1].lower(), ")"]
        last = end
    parts.append(source[last:])
    return "".join(parts)


def _class_survives_lowering(text: str) -> bool:
    """Whether the character class `text` matches, on the lowercased copy,
    everything it matches on the original. Its letters are read by
    compiling the class itself, not by parsing its ranges by hand."""
    try:
        admits = re.compile(text).fullmatch
    except re.error:
        return False
    negated = text.startswith("[^")
    named = {c for c in string.ascii_letters if bool(admits(c)) != negated}
    for upper, lower in zip(
        string.ascii_uppercase, string.ascii_lowercase, strict=True
    ):
        if upper in named and lower not in named:
            return False
        if negated and lower in named and upper not in named:
            return False
    return True


def _survives_lowering(folded: str) -> str | None:
    """Why a search for `folded` over the lowercased copy would not find
    everything the shape finds on the original (see above), or None if it
    would."""
    class_start = None
    i = 0
    while i < len(folded):
        char = folded[i]
        if char == "\\":
            escaped = folded[i + 1 : i + 2]
            if not escaped or (
                escaped.isalnum() and escaped not in _CASE_NEUTRAL_ESCAPES
            ):
                return f"the escape \\{escaped} can name an uppercase letter"
            i += 2
            continue
        if class_start is not None:
            if char == "]":
                if not _class_survives_lowering(folded[class_start : i + 1]):
                    return "a character class names a letter in one case only"
                class_start = None
            i += 1
            continue
        if char == "[":
            class_start = i
            i += 1
            if folded.startswith("^", i):
                i += 1
            if folded.startswith("]", i):  # a leading `]` is a literal
                i += 1
            continue
        if char == "(":
            syntax = _GROUP_SYNTAX.match(folded, i)
            if syntax:
                i = syntax.end()
                continue
            if folded.startswith("(?!", i) or folded.startswith("(?<!", i):
                # A negative lookaround says what must NOT be there, so
                # lowering, which can only ever match MORE, takes real
                # matches away rather than adding spurious ones:
                # `(?i:password=)(?!changeme)\S+` lets
                # `PASSWORD=Changeme2026!Xq9fake` through on the original and
                # excludes it on the lowered copy. No packaged shape uses one.
                return "a negative lookaround excludes more text once lowered"
        if char in string.ascii_uppercase:
            return "an uppercase letter outside a character class"
        i += 1
    return None if class_start is None else "an unclosed character class"


# What ends a value once it has started: whitespace, a quote, `&`, `;`, `,`,
# or a whole HTML tag (fix round 5). A tag is markup, not value text: read
# as value text, `</code>` in the raw view turned `<code>API_TOKEN=&lt;your-
# token&gt;</code>` -- the visible way to write a placeholder in HTML -- into
# a value that is no placeholder. Only a whole tag counts, the same shape
# _HTML_TAG deletes; the text view has those deleted already, and glues
# the pieces either side of one back together, so a value split by markup
# is still read whole there.
_VALUE_BOUNDARY = re.compile(r"""[\s"'&;,]|<[A-Za-z/!?][^<>]*>""")

# Where an assignment's value starts. Skipped first: whatever sits between
# the operator and the value without being part of it -- whitespace of any
# kind, quotes (`password = "x"`), the invisible characters the text view
# strips, and every way of writing one of those as an HTML character
# reference, which is how the raw view meets them: `&nbsp;`, `&ensp;`,
# `&#32;`, `&Tab;`, `&quot;`, `&#x27;`. The escaped forms are derived from
# html.entities.html5 and from the code points themselves rather than
# listed, so no form is missed (fix round 5: `&ensp;` and friends made a
# placeholder read as a value, and the page was refused).
#
# Line breaks are skipped like any other whitespace, so a value below its
# key is found however far down it sits: what ends the search is the first
# line with something on it, not a count of lines (fix round 5 -- a count
# could not work, because deleting a tag leaves the blank line it stood on,
# and `<td>DB_PASSWORD=</td>` followed by three opening tags on their own
# lines already spends three). Landing on the end of the text means the
# value is genuinely empty. The runs skipped hold nothing but those
# characters, so no two matches' runs overlap and the total work over a
# view stays linear however far a value sits from its key.
_SKIPPABLE = frozenset(
    {chr(code) for code in range(0x3001) if chr(code).isspace()}
    | set("'\"")
    | set(_INVISIBLE)
)


def _reference_forms(characters: frozenset) -> str:
    """A regex alternation of every way to write one of `characters` as an
    HTML character reference: the html5 named forms (with and without their
    semicolon, both of which html.unescape reads) and the numeric forms."""
    forms = [
        re.escape(name)
        for name, text in html.entities.html5.items()
        if text and all(char in characters for char in text)
    ]
    forms += [rf"#0*{ord(char)};?" for char in characters]
    forms += [
        "#[xX]0*"
        + "".join(
            f"[{digit}{digit.upper()}]" if digit.isalpha() else digit
            for digit in f"{ord(char):x}"
        )
        + ";?"
        for char in characters
    ]
    # Longest first: `nbsp;` must win over `nbsp`, as html.unescape's own
    # longest-match rule has it.
    return "|".join(sorted(forms, key=len, reverse=True))


_SKIPPED_BEFORE_VALUE = (
    f"(?:[{''.join(sorted(re.escape(char) for char in _SKIPPABLE))}]"
    f"|&(?:{_reference_forms(_SKIPPABLE)}))"
)
_VALUE_START = re.compile(f"{_SKIPPED_BEFORE_VALUE}*")

# A value that starts with `&`, `;` or `,` is a value -- a generated
# password starts with one of the three about once in 30 -- except where the
# character only closes an empty one (fix round 5): an `&` (or `&amp;`) that
# starts the next query parameter (`?password=&next=1`) or has nothing after
# it, and a `;` or `,` with nothing after it (`pass=;`). "Nothing" is
# whitespace, a quote, a tag or the end of the text. Anywhere after the
# value's first character, `&`, `;` and `,` still end it (_VALUE_BOUNDARY).
_ENDS = r"""(?=[\s"'<]|\Z)"""
_EMPTY_BEFORE_AMPERSAND = re.compile(rf"&(?:amp;)?(?:[\w.~%\[\]-]+=|{_ENDS})")
_EMPTY_BEFORE_SEPARATOR = re.compile(rf"[;,]{_ENDS}")

# How much of a value token is read for classification, counted from where
# the token starts (not from the operator, so leading whitespace cannot push
# the value out of reach).
_VALUE_WINDOW = 200

_MAX_LINES_PER_SHAPE = 5
_MAX_RAW_MATCHES_PER_SHAPE = 500
_MAX_FINDINGS_TOTAL = 50


def _check_case_insensitive(number: int, name: str, pattern: str) -> None:
    """Raise ShapesError unless a shape holding `(?i:` searches the same way
    folded, over the lowercased copy (see _INLINE_CASE_INSENSITIVE)."""
    where = f"secret shape line {number} ({name})"
    if not pattern.isascii():
        raise ShapesError(
            f"{where}: a shape with a (?i:...) group must be ASCII -- scan() lowers A-Z"
            " only, so a non-ASCII letter would never meet its other case"
        )
    if _VERBOSE_OR_COMMENT.search(pattern):
        raise ShapesError(
            f"{where}: a shape with a (?i:...) group may not use verbose mode or (?#...)"
            " comments -- either can hide what follows a group"
        )
    folded = _fold(pattern)
    if folded is None:
        raise ShapesError(
            f"{where}: every (?i: must open a group of plain literal text, outside any"
            " class, not after a backslash, and with no quantifier after it -- scan()"
            " lowercases the group and searches a lowercased copy of the body, which is"
            " only the same search for such a group"
        )
    reason = _survives_lowering(folded)
    if reason is not None:
        raise ShapesError(
            f"{where}: {reason} -- searched over a lowercased copy of the body, this"
            " shape would not match what it matches on the original"
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
            raise ShapesError(
                f"secret shape line {number}: no <TAB> between name and pattern"
            )
        name = name.strip()
        if not name or not pattern:
            raise ShapesError(f"secret shape line {number}: empty name or pattern")
        if name in seen:
            raise ShapesError(
                f"secret shape line {number}: duplicate shape name {name!r}"
            )
        if any(token in pattern for token in _FORBIDDEN_ANCHORS):
            raise ShapesError(
                f"secret shape line {number} ({name}): \\A/\\Z anchor the whole body -- use"
                " ^/$ with the (implicit) MULTILINE compile if a line boundary is meant"
            )
        if "(?i:" in pattern:
            _check_case_insensitive(number, name, pattern)
        try:
            compiled = re.compile(pattern, re.MULTILINE)
        except re.error as exc:
            raise ShapesError(
                f"secret shape line {number} ({name}): invalid pattern"
            ) from exc
        if compiled.search(""):
            raise ShapesError(
                f"secret shape line {number} ({name}): pattern matches the empty string --"
                " it would flag every line of every body"
            )
        shapes.append((name, compiled))
        seen.add(name)
    if not shapes:
        raise ShapesError(
            "no secret shapes parsed -- refusing to scan with zero shapes"
        )
    return shapes


def _load_shapes() -> list[tuple[str, re.Pattern]]:
    text = (
        resources.files("ream")
        .joinpath("secret_shapes.txt")
        .read_text(encoding="utf-8")
    )
    return parse_shapes(text)


SHAPES = _load_shapes()


def _fast_pattern(compiled: re.Pattern) -> tuple[re.Pattern, bool]:
    """A `(pattern, needs_lowered_view)` pair for fast matching.

    Python's `re` has no literal fast path for an inline scoped
    case-insensitive group: profiling a clean 1 MiB body showed the 13
    `(?i:...)` shapes here costing about 125 ms per view against ~12 ms for
    a same-length case-sensitive literal. Folding the `(?i:...)` group's
    literal content to lowercase and dropping the flag turns it into a
    plain literal search (a non-capturing group of literal text is searched
    as fast as the bare literal) against a pre-lowered copy of the view --
    every original-case form collapses to the same lowered text, so a
    case-sensitive search for the lowered literal against the lowered view
    finds exactly what the case-insensitive search against the original
    would have. Every other pattern (no inline flag) is returned unchanged,
    to search the view as-is: `needs_lowered=False`.

    Only the flagged literal is folded, not the whole pattern -- bearer_token
    is `(?i:bearer)\\s+[A-Za-z0-9._~+/-]{16,}`, where the value class outside
    the flagged group already matches lowercase characters, so it needs no
    change to keep working against a lowered body. parse_shapes refuses any
    shape where that would not hold (see _INLINE_CASE_INSENSITIVE). Four
    non-ASCII letters that `re` folds to an ASCII one (the Kelvin sign, long
    s, and dotted and dotless I) stay as they are in the lowered copy, so a
    folded group does not match them.

    A pattern whose groups cannot be folded -- never one parse_shapes has
    loaded -- is searched as it is, over the view as it is: slower, never
    wrong.
    """
    source = compiled.pattern
    if "(?i:" not in source:
        return compiled, False
    folded = _fold(source)
    if folded is None:
        return compiled, False
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


# `html.unescape`'s own charref regex places no limit on how many decimal
# digits can follow `&#`, and CPython's int(str) conversion refuses one past
# a few thousand digits (its integer-string conversion limit) -- a numeric
# reference carrying an absurd digit run crashed here, at `html.unescape`
# itself, before this scanner ever saw a finding (task 5 fix round 2:
# `store.ScanFailed` answered 422, then 503; fix round 3 removed the crash).
#
# The invariant THIS MODULE runs on is newline preservation, not length
# preservation: `_detag` above already changes length wholesale -- it
# DELETES every tag, keeping only the newlines it contained, specifically
# so a line number computed from this view's own newline offsets still
# means the same line in the raw body -- and so does `html.unescape` on
# every ordinary reference (`&#65;` becomes one character). fix round 3's
# first attempt at this defanged every digit run past seven characters
# unconditionally, which also ate legitimate references padded with
# leading zeros (`&#00000065;` is `A`, and real content is encoded that
# way) -- 24 lost detections across 8 secret families, all through the one
# form a length-preserving substitution cannot touch. Leading zeros carry
# no information: stripping them is a pure string operation (`str.lstrip`),
# never an `int()` conversion, so it is exactly as cheap on a 4301-zero run
# as on none -- CPython's limit is on the conversion, not on counting
# characters, which is the trap in doing this any other way (`int(digits)`
# to normalise it would hit the very crash this function exists to avoid).
#
# So: strip leading zeros first. What is left is the reference's real
# magnitude in digits. At most seven of those can ever be a genuine
# reference, however much zero-padding surrounded it -- 0x10FFFF, the
# highest real code point, is `1114111`, seven digits -- so hand those
# straight to `html.unescape` exactly as `int()` would read them, and
# `&#00000065;` decodes precisely as `&#65;` does. More than seven
# significant digits is never a real code point at ANY padding: defang it
# rather than reach its `int()` call. The ≤7 branch only ever hands
# `int()` at most seven characters and the >7 branch hands it none, so
# nothing here ever reaches the actual limit regardless of how many digits
# -- leading zeros included -- the input held.
#
# The >7 branch's own replacement text must not itself become a new
# problem (fix round 4). Two inert fillers were tried and both failed for
# reasons specific to what THIS scanner does with the surrounding text, not
# to the replacement's content in the abstract: `&#` + a same-count run of
# `x` is still `&`-initial, and `url_query_secret`'s
# `[?&](api_)?(key|token|secret|password)=(?=[^&\s])` reads `&`-initial as
# "no value at all" and never fires; a fixed mixed-letter literal fixed
# that but was still an invented filler, one more thing to justify never
# colliding with a shape or the placeholder valve.
#
# The actual answer needs no invention: an out-of-range numeric character
# reference is not something real `html.unescape` refuses -- its own
# bounds check (`0xD800 <= num <= 0xDFFF or num > 0x10FFFF`) substitutes
# U+FFFD, the standard Unicode replacement character, and returns
# normally. Every number with more than seven significant decimal digits
# already exceeds 0x10FFFF (`1114111`) -- the smallest eight-digit number,
# 10,000,000, already clears it -- so it would always land in that branch
# if CPython's int(str) limit did not intervene first. Substituting U+FFFD
# directly does not approximate what `html.unescape` would have done: it
# IS what `html.unescape` would have done, for every possible input in
# this branch, without exception -- confirmed against `html.unescape` with
# the limit lifted (`sys.set_int_max_str_digits(0)`) over 6000 random
# bodies and a hand-picked set spanning `&#12345678` through
# `&#99999999999999999999;` and `&#`+4301 zeros+`7`: identical output
# every time. Both the `&`-initial problem and the placeholder-valve
# question (raised in the second failed attempt, above) disappear on
# their own: U+FFFD is not `&`, and it is not `x`, so it can neither read
# as "no value" nor `fullmatch` `_PLACEHOLDER`'s `[*xX]{3,}`, alone or
# concatenated with a real value either side of it.
#
# The whole reference is replaced, `&#` and a trailing `;` included --
# exactly what `html.unescape` itself consumes for one substitution -- so
# no stray digits or semicolon are left as literal text afterward. Length
# is not preserved (round 3 already established that length was never the
# actual invariant); newline count is, trivially, since neither the
# reference being removed nor the single replacement character it becomes
# contains one.
#
# Applied to the DETAGGED view, not the raw one: `&#1<b></b>1<b></b>1...`
# holds no long digit run until the tags between the digits are gone, so
# checking before `_detag` would walk straight past it.
_MAX_LEGIT_DECIMAL_CHARREF_DIGITS = 7
_DECIMAL_CHARREF = re.compile(r"&#([0-9]+)(;?)")


def _defang_huge_decimal_charrefs(detagged: str) -> str:
    def _replace(match: re.Match) -> str:
        digits, semicolon = match.group(1), match.group(2)
        significant = digits.lstrip("0") or "0"
        if len(significant) <= _MAX_LEGIT_DECIMAL_CHARREF_DIGITS:
            return "&#" + significant + semicolon
        return "\ufffd"

    return _DECIMAL_CHARREF.sub(_replace, detagged)


# `&#10;`, `&#x0a;` (with or without the semicolon, but never the prefix of a
# longer number such as `&#100`) and the named `&NewLine;`.
_ENCODED_NEWLINE = re.compile(
    r"&#0*10(?:;|(?![0-9]))|&#[xX]0*[aA](?:;|(?![0-9a-fA-F]))|&NewLine;"
)


def _text_view(raw_view: str) -> str:
    """The raw view with tags deleted, entities unescaped, and soft-hyphen/
    zero-width characters removed -- what exposes a credential that markup,
    an HTML entity, or an invisible line-wrap hint runs through. Entities
    are unescaped before the zero-width strip, not after, because an entity
    such as `&shy;` decodes TO a zero-width character in the first place.
    """
    detagged = _defang_huge_decimal_charrefs(_detag(raw_view))
    # An ENCODED line break becomes a space, not a line (homelab-ij8.8):
    # unescaped to a real "\n" it gave the text view a line the raw body does
    # not have, and every finding after it was reported one line too far down.
    # A space separates exactly as the newline would, so nothing hides behind it.
    detagged = _ENCODED_NEWLINE.sub(" ", detagged)
    text_view = html.unescape(detagged)
    if not text_view.isascii():
        for char in _INVISIBLE:
            text_view = text_view.replace(char, "")
    return text_view


def _value_after(text: str, start: int) -> str:
    """The value token following an assignment match that ends at `start`:
    bounded at the next whitespace, quote, `&`, `;`, `,`, whole HTML tag, or
    the end of a `_VALUE_WINDOW`-character window. For placeholder classification only --
    never returned, logged, or included in a `Finding`. Only ever called for
    a shape whose match already ends exactly at the assignment operator, so
    no separator needs stripping; `_VALUE_START` skips leading whitespace and
    quotes so the token itself, not its wrapper, gets classified.

    With nothing after the operator on its own line, the token is the first
    one on the next line that holds anything at all, however far down that
    is: `password=` with `hunter2` below it has a value, not "no value"
    (fix round 4 -- reading it as empty made it a placeholder, and the
    credential was dropped -- and fix round 5's final commit, which stopped
    counting the blank lines). With nothing below it either, the value is
    genuinely empty, and an empty value is a placeholder.

    A value's FIRST character is part of it even when it is `&`, `;` or `,`
    (fix round 5: `DB_PASSWORD=&k2...` was read as empty and dropped), unless
    it only closes an empty value (see _EMPTY_BEFORE_AMPERSAND). A whole
    HTML tag in first place that is not placeholder-shaped (`</td>`) is not
    a value either; a tag later on ends one (see _VALUE_BOUNDARY).

    A `${...}`/`{{...}}`/`<...>`/`&lt;...&gt;` wrapper's OWN closing
    delimiter is found first, and the boundary search resumes only after
    it: `{{ vault_secret }}` has a plain space inside the braces that is not
    a value boundary, while `${PW}hunter2fake` has real content trailing the
    closing brace that very much is one. Skip only past a wrapper that
    actually closes; an unclosed one falls through to the plain boundary
    search unchanged.
    """
    # Everything below works on `text` in place, slicing out only the token
    # itself: this runs once per assignment match, and a placeholder flood
    # is one match every few characters. The skipping regex runs only when
    # the operator is followed by whitespace, a quote or an `&` at all.
    end = len(text)
    if start == end:
        return ""
    first = text[start]
    pos = start
    if first in _SKIPPABLE or first == "&":
        pos = _VALUE_START.match(text, start).end()
        if pos == end:
            return ""
        first = text[pos]
    limit = pos + _VALUE_WINDOW
    # Where the boundary search starts: past the first character when that
    # character is itself a boundary, so the value keeps it.
    skip = pos
    if first == "&":
        if _EMPTY_BEFORE_AMPERSAND.match(text, pos, limit):
            return ""
        close = text.find("&gt;", pos, limit) if text.startswith("&lt;", pos) else -1
        skip = close + 4 if close != -1 else pos + 1
    elif first in ";,":
        if _EMPTY_BEFORE_SEPARATOR.match(text, pos):
            return ""
        skip = pos + 1
    elif first == "$" and text.startswith("${", pos):
        close = text.find("}", pos, limit)
        skip = close + 1 if close != -1 else pos
    elif first == "{" and text.startswith("{{", pos):
        close = text.find("}}", pos, limit)
        skip = close + 2 if close != -1 else pos
    elif first == "<":
        close = text.find(">", pos, limit)
        # Markup, not a value (`<td>DB_PASSWORD=</td>`): the text view
        # deletes the tag and reads whatever follows it.
        if (
            close != -1
            and _PLACEHOLDER.fullmatch(text, pos, close + 1) is None
            and _HTML_TAG.fullmatch(text, pos, close + 1)
        ):
            return ""
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
            if (
                len(lines) >= _MAX_LINES_PER_SHAPE
                or real_examined >= _MAX_RAW_MATCHES_PER_SHAPE
            ):
                break
        if (
            len(lines) >= _MAX_LINES_PER_SHAPE
            or real_examined >= _MAX_RAW_MATCHES_PER_SHAPE
        ):
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
    views = [(view, _lower_ascii(view)) for view in view_texts]

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
