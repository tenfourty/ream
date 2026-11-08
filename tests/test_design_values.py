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

# None left: the connect page's fallbacks are gone, and the box that rendered one (after
# an undefined --soft) names --well.
_RAW_COLOUR_EXCEPTIONS: list[str] = []


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
    assert used - set(design.BY_NAME) == set()


def test_no_var_carries_a_fallback():
    """A fallback after a name that is always defined never renders, and one after a
    name that is not hides the missing name (--soft drew a light box in dark mode)."""
    for css in _sheets():
        assert re.findall(r"var\(--[a-z0-9-]+,", css) == []


def test_no_comment_text_is_left_outside_a_comment():
    """Prose after a stray `*/` is read as a selector: the browser drops it and the rule
    behind it, which is how the narrow-screen `.sheet .ask` rule never applied."""
    for css in _sheets():
        bare = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
        assert "*/" not in bare
        assert "`" not in bare


def test_the_narrow_screen_rule_for_a_lifted_question_stands_alone():
    bare = re.sub(r"/\*.*?\*/", "", gallery._RULES, flags=re.DOTALL)
    before, after = bare.split("@media (max-width:55.99rem){", 1)
    assert before.rstrip().endswith("}"), "something not a rule sits before it"
    assert after.lstrip().startswith(".sheet .ask{position:static;")


def test_since_and_the_connect_box_have_a_defined_ground():
    assert re.search(r"\.since\{[^}]*background:var\(--paper\)", gallery._RULES)
    assert "background:var(--well)" in gallery._CONNECT_CSS
    assert design.light("well") == "#f5f5f4"  # the light look did not move
    assert design.dark("well") != design.light("well")


def test_the_icon_carries_the_mark_colours_as_values():
    light, dark = logo.CSS.split("@media (prefers-color-scheme:dark)", 1)
    for name in ("mark-ink", "mark-lift"):
        assert design.light(name) in light and design.dark(name) in dark


def test_the_brand_guide_lists_the_values_as_defined():
    guide = (ROOT / "docs" / "brand.md").read_text(encoding="utf-8")
    assert design.markdown_table() in guide
