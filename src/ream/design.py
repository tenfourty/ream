"""The gallery's look as one set of named values (docs/decisions.md, 8 Nov 2026).

Every colour, shadow, font stack, recurring type size and recurring spacing step the
gallery's stylesheet uses is named here once, and the stylesheet (`gallery._CSS`) starts
from `ROOT_CSS`, the `:root` block this table produces. A rule says `var(--name)`, never
the value. Swapping a look is changing this table; nothing else needs to be touched. The
names and their values are also the table in `docs/brand.md`, kept equal by a test.

The neutral scale is claude.ai's own, read off the saved page with getComputedStyle rather
than sampled from a screenshot: page rgb(252,252,251), ink rgb(11,11,11), muted
rgb(137,135,129), hairline rgba(11,11,11,.1). The ACCENT stays the homelab teal: every
template on this service shares it (`templates/_shared.css` uses the same names), and
swapping it here would leave the gallery and the pages it frames looking like two
different products.

Only values that recur exactly are named. A one-off spacing or an off-scale size stays a
literal in its rule; folding those onto the scale would change the look, which this table
was built not to do.
"""

from typing import NamedTuple


class Value(NamedTuple):
    name: str  # the custom property, without its leading dashes
    light: str
    dark: str | None  # None: the same in both themes
    use: str


VALUES: tuple[Value, ...] = (
    # Colours.
    Value("paper", "#fcfcfb", "#141619", "the page behind everything"),
    Value("ink", "#0b0b0b", "#e9eae4", "body text"),
    Value("muted", "#898781", "#989a90", "secondary text"),
    Value("card", "#fff", "#1c1f23", "a card, panel or dialog"),
    Value("line", "rgba(11,11,11,.1)", "#2b2e34", "hairlines and borders"),
    Value("line-hover", "rgba(11,11,11,.28)", "#5a606a", "a border under the pointer"),
    Value("accent", "#0f6f63", "#5ec9b8", "the homelab teal: links, focus, fills"),
    Value(
        "accent-soft", "rgba(15,111,99,.10)", "rgba(94,201,184,.12)", "an accent tint"
    ),
    Value("accent-ink", "#0b4f46", "#9fe3d7", "text in the accent colour"),
    # Text on a SOLID accent fill. Its own name for the same reason --on-now is:
    # --accent is a light teal in dark mode, where white on it measures 2.0:1. Any
    # value used as a FILL needs a paired ink that flips with the scheme.
    Value("on-accent", "#fff", "#0b2723", "text on a solid accent fill"),
    # A chosen answer is solid accent too, with its own dark ink (it predates on-accent).
    Value("on-chosen", "#fff", "#10201d", "text on a chosen answer"),
    Value("ok", "#2f7a45", "#6cc48a", "success"),
    Value("now", "#b06a12", "#e0a458", "waiting on the owner"),
    # The waiting badge is SOLID, so its background and its text are their own names
    # rather than --now plus white. Computed, not chosen: white on --now (#b06a12) is
    # 4.27:1, which fails for 11px bold; #9c5e10 takes it to 5.22:1. In dark mode --now
    # is a LIGHT amber and white on it is 2.18:1, so the dark scheme flips the text
    # instead of the fill: #2a1d0a on #e0a458 is 7.52:1.
    Value("now-solid", "#9c5e10", "#e0a458", "the waiting badge's fill"),
    Value("on-now", "#fff", "#2a1d0a", "text on the waiting badge"),
    # The only red in the interface: destructive actions and marks (the archive and
    # destroy controls, the "Remove this" mark label) reuse it rather than mint a
    # second, uncalibrated red.
    Value("danger", "#a8271b", "#f08b7f", "destructive actions"),
    Value(
        "danger-soft", "rgba(168,39,27,.08)", "rgba(240,139,127,.12)", "a danger tint"
    ),
    Value("on-danger", "#fff", None, "text on a solid danger fill"),
    # A control sitting ON a card: the dots chip, the pin's hover. It must be a step
    # AWAY from the card in whichever direction reads as lifted: very slightly darker in
    # light mode, lifted in dark mode, where darker is a hole.
    Value("chip", "#fcfcfb", "#2b3038", "a control on a card"),
    Value("chip-hover", "#fff", "#343a44", "that control under the pointer"),
    Value("pin-fill", "rgba(217,164,0,.12)", None, "the outline of a pinned spot"),
    Value("backdrop", "rgba(11,11,11,.45)", None, "behind a dialog"),
    Value("connect-err", "#b42318", None, "an error on the connect pages"),
    Value("connect-ok", "#067647", None, "a success on the connect pages"),
    # The mark's own two colours, as drawn in brand/ (docs/brand.md).
    Value("mark-ink", "#191a17", "#e9eae4", "the logo's letter and sheets"),
    Value("mark-lift", "#0f6f63", "#5ec9b8", "the logo's lifting sheet"),
    # Shadows.
    Value(
        "shadow",
        "0 1px 2px rgba(20,20,15,.05), 0 8px 24px -16px rgba(20,20,15,.25)",
        "0 1px 2px rgba(0,0,0,.4), 0 10px 30px -20px rgba(0,0,0,.8)",
        "a card at rest",
    ),
    Value(
        "shadow-menu",
        "0 2px 6px rgba(20,20,15,.06),0 14px 34px -18px rgba(20,20,15,.45)",
        None,
        "an open menu",
    ),
    Value(
        "shadow-float", "0 2px 8px rgba(0,0,0,.18)", None, "a button floating on a page"
    ),
    Value("shadow-pin", "0 1px 4px rgba(0,0,0,.3)", None, "a pin's numbered badge"),
    Value("shadow-chip", "0 1px 3px rgba(20,20,15,.10)", None, "a tile's dots chip"),
    Value(
        "shadow-chip-hover", "0 2px 6px rgba(20,20,15,.16)", None, "that chip, raised"
    ),
    Value(
        "shadow-badge", "0 1px 4px rgba(20,20,15,.28)", None, "a tile's waiting badge"
    ),
    Value("shadow-dialog", "0 12px 40px -12px rgba(11,11,11,.4)", None, "a dialog"),
    Value(
        "shadow-toast", "0 6px 24px rgba(0,0,0,.14)", None, "the newer-version notice"
    ),
    Value(
        "shadow-dock",
        "0 -2px 10px rgba(20,20,15,.12)",
        None,
        "the bar docked at the foot",
    ),
    # Font stacks. Three sans stacks, kept apart rather than merged, so every element
    # renders in exactly the face it did.
    Value(
        "display",
        'ui-serif,"Iowan Old Style",Georgia,"Times New Roman",serif',
        None,
        "the wall's title",
    ),
    Value(
        "sans",
        'ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif',
        None,
        "body text, tile and page titles",
    ),
    Value(
        "sans-system",
        "ui-sans-serif,system-ui,-apple-system,sans-serif",
        None,
        "the bar, badges, lists",
    ),
    Value(
        "sans-short", "ui-sans-serif,system-ui,sans-serif", None, "comments, selection"
    ),
    Value(
        "mono",
        'ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace',
        None,
        "code, ids, labels",
    ),
    Value("mono-short", "ui-monospace,monospace", None, "the connect code"),
    # Type sizes: the reference's own pixel steps, 11 to 14 px.
    Value("text-2xs", ".6875rem", None, "11px: badges"),
    Value("text-xs", ".75rem", None, "12px: small print"),
    Value("text-sm", ".8125rem", None, "13px: secondary text"),
    Value("text-md", ".875rem", None, "14px: titles in the chrome"),
    # Spacing: the margin, padding and gap steps used ten times or more.
    Value("space-2", ".2rem", None, "spacing"),
    Value("space-3", ".3rem", None, "spacing"),
    Value("space-35", ".35rem", None, "spacing"),
    Value("space-4", ".4rem", None, "spacing"),
    Value("space-45", ".45rem", None, "spacing"),
    Value("space-5", ".5rem", None, "spacing"),
    Value("space-6", ".6rem", None, "spacing"),
    Value("space-7", ".7rem", None, "spacing"),
    Value("space-75", ".75rem", None, "spacing"),
    Value("space-10", "1rem", None, "spacing"),
)

BY_NAME = {v.name: v for v in VALUES}


def light(name: str) -> str:
    return BY_NAME[name].light


def dark(name: str) -> str:
    value = BY_NAME[name]
    return value.light if value.dark is None else value.dark


def _block(pairs: list[tuple[str, str]]) -> str:
    return "".join(f"\n  --{name}:{value};" for name, value in pairs)


ROOT_CSS = (
    ":root{"
    + _block([(v.name, v.light) for v in VALUES])
    + "\n}\n@media (prefers-color-scheme:dark){\n:root{"
    + _block([(v.name, v.dark) for v in VALUES if v.dark is not None])
    + "\n}}\n"
)


def markdown_table() -> str:
    """The table of names in docs/brand.md, one row per value."""
    rows = ["| Name | Light | Dark | Use |", "|---|---|---|---|"]
    for v in VALUES:
        shade = "same" if v.dark is None else f"`{v.dark}`"
        rows.append(f"| `--{v.name}` | `{v.light}` | {shade} | {v.use} |")
    return "\n".join(rows) + "\n"
