"""The gallery's look is one set of named values, in design.py (docs/decisions.md, 8 Nov 2026).

A rule in the gallery's stylesheets names a value; it never writes a colour or a font stack
itself. These tests keep it so, and keep docs/brand.md's table equal to the definition.
"""

import re
from pathlib import Path

from ream import design, gallery, logo

ROOT = Path(__file__).resolve().parent.parent

# Declarations only: a selector such as `#comments` is not a colour.
_DECL = re.compile(r"(?:(?<=[{;])|^)\s*([a-z-]+)\s*:([^;{}]*)(?=[;}])")
_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(")
_FACE = re.compile(r"serif|monospace|system-ui|Roboto|Georgia|Menlo|Consolas")

# Known before the values were named, and kept so the look did not change: the connect
# pages' fallbacks after names that are always defined (so never used), and the one after
# --soft, which is not defined, so its fallback is what renders.
_RAW_COLOUR_EXCEPTIONS = ["#ddd", "#ccc", "#f5f5f4"]
# Used but never defined: existing bugs, recorded rather than fixed (the look stays as is).
_UNDEFINED_KNOWN = {"bg", "soft"}


def _sheets() -> list[str]:
    return [gallery._RULES, gallery._CONNECT_CSS, logo.PAGE_CSS]


def _declarations(css: str) -> list[tuple[str, str]]:
    without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [(m[1], m[2]) for m in _DECL.finditer(without_comments)]


def test_each_value_is_named_once_and_the_stylesheet_starts_from_them():
    names = [value.name for value in design.VALUES]
    assert len(names) == len(set(names))
    assert gallery._CSS == design.ROOT_CSS + gallery._RULES
    for name in names:
        assert design.ROOT_CSS.count(f"--{name}:") == (
            1 if design.BY_NAME[name].dark is None else 2
        )


def test_no_rule_defines_a_value_of_its_own():
    for css in _sheets():
        assert not [p for p, _ in _declarations(css) if p.startswith("--")]


def test_the_rules_write_no_colour_and_no_font_stack():
    colours, faces = [], []
    for css in _sheets():
        for prop, value in _declarations(css):
            colours += _COLOUR.findall(value)
            faces += [f"{prop}:{value.strip()}" for _ in _FACE.findall(value)]
    assert colours == _RAW_COLOUR_EXCEPTIONS
    assert faces == []


def test_every_name_used_is_defined():
    used = set()
    for css in _sheets():
        used |= set(re.findall(r"var\(--([a-z0-9-]+)", css))
    assert used - set(design.BY_NAME) == _UNDEFINED_KNOWN


def test_the_icon_carries_the_mark_colours_as_values():
    light, dark = logo.CSS.split("@media (prefers-color-scheme:dark)", 1)
    for name in ("mark-ink", "mark-lift"):
        assert design.light(name) in light and design.dark(name) in dark


def test_the_brand_guide_lists_the_values_as_defined():
    guide = (ROOT / "docs" / "brand.md").read_text(encoding="utf-8")
    assert design.markdown_table() in guide
