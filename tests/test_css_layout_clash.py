"""One class, one layout: a bare class selector may not be given conflicting layout in
two separate rules of the same stylesheet (runcove-5t3oj.85).

The comment bar (`.selbar`, placed with page-coordinate `top`/`left`) and the wall's
select-mode bar once shared that class. The gallery serves one stylesheet, so the later
`.selbar{position:sticky;bottom:0}` won on every page and pushed the comment bar off
screen for any selection more than about a window down. A browser test catches that one
bar; this catches the next name clash before it ships, for every class.

The check: for every top-level rule whose selector list holds a bare `.name`, collect
the layout properties it sets; if two rules set the same property for the same `.name`
to different values, that is a clash. Rules inside `@media` (and other at-blocks) are
left out, as overriding at a breakpoint is their purpose; a qualified selector such as
`.name[hidden]` or `.parent .name` is a deliberate override and is not a bare class.
"""

import re
from collections import defaultdict

from ream import gallery, logo

_LAYOUT = ("position", "display", "top", "bottom", "left", "right", "float")
_BARE_CLASS = re.compile(r"\.[A-Za-z_][\w-]*")


def _top_level_rules(css: str) -> list[tuple[str, str]]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    rules, pos = [], 0
    while True:
        brace = css.find("{", pos)
        if brace < 0:
            return rules
        selector = css[pos:brace].strip()
        if selector.startswith("@"):
            depth, pos = 1, brace + 1
            while depth:
                depth += {"{": 1, "}": -1}.get(css[pos], 0)
                pos += 1
            continue
        end = css.index("}", brace)
        rules.append((selector, css[brace + 1 : end]))
        pos = end + 1


def clashes(css: str) -> dict[str, dict[str, set[str]]]:
    seen: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for selector, body in _top_level_rules(css):
        layout = {}
        for decl in body.split(";"):
            prop, colon, value = decl.partition(":")
            if colon and prop.strip() in _LAYOUT:
                layout[prop.strip()] = value.strip()
        for one in selector.split(","):
            if _BARE_CLASS.fullmatch(one.strip()):
                for prop, value in layout.items():
                    seen[one.strip()][prop].add(value)
    return {
        cls: {p: v for p, v in props.items() if len(v) > 1}
        for cls, props in seen.items()
        if any(len(v) > 1 for v in props.values())
    }


def test_the_check_catches_the_clash_that_hid_the_comment_bar():
    css = (
        ".selbar{position:absolute;z-index:60;display:flex}"
        "/* later, elsewhere */ .tile{display:block}"
        ".selbar{position:sticky;bottom:0;display:flex}"
    )
    assert clashes(css) == {".selbar": {"position": {"absolute", "sticky"}}}


def test_the_check_lets_overrides_through():
    css = (
        ".bar{position:absolute}.bar[hidden]{display:none}.x .bar{position:fixed}"
        "@media (max-width:40rem){.bar{position:static}}"
        ".bar{display:flex}"  # a second rule adding a property is not a clash
    )
    assert clashes(css) == {}


def test_no_class_in_the_gallery_stylesheets_gets_two_layouts():
    for css in (gallery._RULES, gallery._CONNECT_CSS, logo.PAGE_CSS):
        assert clashes(css) == {}
