"""How a page asks for a frame the height of the window (decisions.md, 16 Nov 2026).

By default the gallery sizes a page's frame to the height the page reports, so the
gallery scrolls and the frame never does. A page that sizes ITSELF from the window
(`innerHeight`, `100vh`, a stage of `H * pages`, `position: fixed` controls, a canvas
the size of the window) feeds that back: the frame grows, so the window inside it
grows, so the page grows, up to the gallery's 100000px cap. Such a page declares, in
its own HTML:

    <meta name="ream-frame" content="viewport">

and the gallery then keeps the frame at the visible height under its bar, lets the
frame scroll, and ignores the page's height reports. The value is read without case
or surrounding white space; any other value (or none) is the default, `content`.

The name is published: it sits in stored pages and never changes. It is a new name,
so it says Ream; there is no `artifacts-` spelling, because no page ever used one
(AGENTS.md, "One name, two spellings": old names are kept for old callers, and a new
one has none). A `<meta>` in the head, not a `data-artifact-*` attribute, because it
is a setting of the whole page, where HTML already puts `viewport` and `color-scheme`.

Read in two places, both from the page's own DOM while it runs, never stored:

- the shim (`content._viewer_report`) reports `frame` with every height report, and
  the gallery acts on it for that page's own frame only, which the page already sizes;
- the screenshot (`rendercore._RESOLVE_JS`) takes a whole-page picture at the window's
  height, as the frame shows it.
"""

from __future__ import annotations

META_NAME = "ream-frame"
VIEWPORT = "viewport"
CONTENT = "content"
