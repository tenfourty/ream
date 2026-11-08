"""The Ream mark as the gallery draws it (docs/decisions.md, 8 Nov 2026).

The drawings live in `brand/`, one file per size and theme, and those files are the only
geometry: everything here is cut from the light files, so the bar and the icon cannot drift
from them. The small drawing is the one made to read at tab size; the large one needs about
32 px. One palette for both uses, the named mark colours in `design.py`, so a light or dark
reader gets the mark as it was drawn for that theme.
"""

import re
from pathlib import Path

from ream import design

_BRAND = Path(__file__).resolve().parent / "brand"
_INK = f'fill="{design.light("mark-ink")}"'
_LIFT = f'stroke="{design.light("mark-lift")}"'

# Class names a stylesheet can hold beside the gallery's own without colliding. The colours
# are the named `--mark-ink` and `--mark-lift` (design.py): the gallery's stylesheet defines
# them, so its pages take `PAGE_CSS`; the icon stands alone, so `CSS` writes their values in.
PAGE_CSS = ".mark-i{fill:var(--mark-ink)}.mark-l{stroke:var(--mark-lift)}"
CSS = (
    f".mark-i{{fill:{design.light('mark-ink')}}}.mark-l{{stroke:{design.light('mark-lift')}}}"
    "@media (prefers-color-scheme:dark){"
    f".mark-i{{fill:{design.dark('mark-ink')}}}.mark-l{{stroke:{design.dark('mark-lift')}}}}}"
)


def _drawing(size: str) -> str:
    """The `<g>` of the light file, its colours swapped for the classes in `CSS` and its
    `id` gone (a page holds the mark beside other markup)."""
    text = (_BRAND / f"rooted-{size}-lt.svg").read_text(encoding="utf-8")
    found = re.search(r"<g .*</g>", text, re.DOTALL)
    if found is None:
        raise ValueError(f"no drawing in the {size} mark")
    drawing = re.sub(r">\s+<", "><", found[0]).replace(' id="mark"', "")
    return drawing.replace(_INK, 'class="mark-i"').replace(_LIFT, 'class="mark-l"')


def _inline(size: str) -> str:
    """For the bar: inline, so it costs no request and takes the page's stylesheet."""
    return (
        f'<svg class="mark mark-{size}" viewBox="0 0 512 512" aria-hidden="true">'
        f"{_drawing(size)}</svg>"
    )


# Standalone, so it carries its own style. A browser honours prefers-color-scheme inside an
# SVG icon, which is what makes one file right on a light and a dark tab strip.
ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    f"<title>Ream</title><style>{CSS}</style>{_drawing('sm')}</svg>"
)
LARGE = _inline("lg")
SMALL = _inline("sm")
