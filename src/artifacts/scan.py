"""The local secret-shape scanner.

Runs against every body an agent asks to publish, and against every state
document read back (D12, D15) -- ggshield is the authoritative gate but only
runs in the deploy pipeline with Jeremy's token, so this is the one scanner
that always runs.

A `Finding` says which shape fired and on which line, never what matched: a
scanner that leaks the credential it caught is worse than no scanner. Do not
add a field, a log line, or an exception message anywhere in this module that
carries scanned text.
"""

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


def parse_shapes(text: str) -> list[tuple[str, re.Pattern]]:
    """Parse `name<TAB>pattern` lines (blank lines and `#` comments ignored).

    Every line must supply both a name and a non-empty pattern: a line with
    no tab, or an empty pattern either side of one, is rejected rather than
    silently becoming a shape whose regex is the empty string -- an empty
    pattern matches every line, so one malformed line would turn into a
    scanner that flags everything it is ever given.
    """
    shapes: list[tuple[str, re.Pattern]] = []
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
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ShapesError(f"secret shape line {number} ({name}): invalid pattern") from exc
        shapes.append((name, compiled))
    if not shapes:
        raise ShapesError("no secret shapes parsed -- refusing to scan with zero shapes")
    return shapes


def _load_shapes() -> list[tuple[str, re.Pattern]]:
    text = resources.files("artifacts").joinpath("secret_shapes.txt").read_text()
    return parse_shapes(text)


SHAPES = _load_shapes()


def scan(text: str) -> list[Finding]:
    """Return every (shape, line) a shape pattern matched in `text`.

    An empty list means clean. It never means "the scanner didn't run" --
    a failure to load shapes raises `ShapesError` at import time instead,
    so a caller checking `scan(body) == []` is never looking at a scanner
    that silently gave up.
    """
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in SHAPES:
            if pattern.search(line):
                findings.append(Finding(shape=name, line=number))
    return findings
