"""Markdown in, a page in the house look out (runcove-5t3oj.33, 20 Oct 2026).

Jeremy asked for agents to hand over markdown and get a formatted page, with the
service doing the conversion so that any agent can use it through the API, not
only the ones with our CLI. Every office was asked what it needs
(docs/agent/feedback/2026-10-20-markdown-pages.md); this is the first cut:

- CommonMark, plus GFM tables, strikethrough and task lists.
- Fenced code keeps its language as `class="language-x"`.
- Headings from `##` down get stable ids (GitHub-style slugs), so a reader can
  link to one and `--section` can target one. The page's `#` title gets none, so
  it cannot collide with a raw `id=` of the same word.
- Raw HTML passes through untouched. A decision slot
  (`<div data-artifact-decision=...>`), a `<details>` fold, an inline SVG or an
  `id=` anchor works on a markdown page exactly as on an HTML one. Nothing is
  sanitised here, as nothing is on an HTML publish: the page origin's CSP and
  the secret scan apply to the result the same way.

The first `#` heading becomes the masthead title and, when the publisher gives
none, the page's name. The house look is the shared stylesheet, INLINED, as the
templates do: a page that only links `/_shared.css` is styled on its own origin
but plain in its wall thumbnail, in `--preview` and in a fetched copy, because
those render the body with no origin to load it from. Jeremy's 19 Sep 2026
ruling (serve it as a stylesheet) spared agents pasting 150 lines by hand;
here the service does the pasting, so nobody carries it.
"""

from __future__ import annotations

import re
from html import escape

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.anchors import anchors_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

from artifacts import templates

FORMATS = ("html", "markdown")

# The link back to the wall, as every template carries it.
_TOPLINE = """\
  <p class="topline">
    <a href="https://artifacts.tenfourty.site/" target="_blank" rel="noopener"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M7.1 1.3 1.4 6.2c-.3.2-.4.6-.2.9.2.3.6.4.9.2l.3-.3V14c0 .4.3.7.7.7h3.2c.4 0 .7-.3.7-.7v-3h2.1v3c0 .4.3.7.7.7h3.2c.4 0 .7-.3.7-.7V7l.3.3c.3.2.7.1.9-.2.2-.3.1-.7-.2-.9L8.9 1.3a1.4 1.4 0 0 0-1.8 0Z"/></svg> Gallery</a>
  </p>
"""

# What the shared stylesheet does not already style: code blocks, images, task
# lists and the odd wide table. Kept small; a page may add its own.
_EXTRA_CSS = """\
  pre{background:var(--card);border:1px solid var(--line);border-radius:6px;
    padding:.7rem .9rem;overflow-x:auto;font:.86rem/1.45 var(--mono)}
  pre code{background:none;color:inherit;padding:0}
  img{max-width:100%;height:auto}
  .table-wrap{overflow-x:auto}
  ul.contains-task-list{list-style:none;padding-left:.2rem}
  .task-list-item-checkbox{margin-right:.45rem}
  h2,h3,h4{scroll-margin-top:1rem}
"""


def _slug(text: str) -> str:
    """GitHub-style: lower case, spaces to hyphens, drop other punctuation.
    Ids stay inside what `--section` accepts ([A-Za-z0-9_.:-])."""
    s = re.sub(r"[^\w\- ]", "", text.strip().lower(), flags=re.UNICODE)
    s = re.sub(r"[^A-Za-z0-9_\- ]", "", s)
    return re.sub(r"\s+", "-", s).strip("-") or "section"


def _engine() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True, "typographer": False})
    md.enable(["table", "strikethrough"])
    md.use(tasklists_plugin, enabled=False)
    md.use(anchors_plugin, min_level=2, max_level=6, slug_func=_slug, permalink=False)
    return md


_MD = _engine()


def _first_h1(tokens: list[Token]) -> tuple[str | None, int | None]:
    """The text of the first level-1 heading, and its token index."""
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and tok.tag == "h1":
            inline = tokens[i + 1]
            return inline.content.strip(), i
    return None, None


def _wrap_tables(html: str) -> str:
    return html.replace("<table>", '<div class="table-wrap"><table>').replace(
        "</table>", "</table></div>"
    )


def render_fragment(text: str) -> str:
    """Markdown to an HTML fragment, for a section update."""
    return _wrap_tables(_MD.render(text))


def first_heading(text: str) -> str | None:
    title, _ = _first_h1(_MD.parse(text))
    return title


def render_page(text: str, title: str | None = None) -> tuple[str, str | None]:
    """Markdown to a whole page: (html, title).

    `title` is the publisher's name for the page; without one it is the first
    `#` heading, or None when there is none (the caller decides whether that is
    an error). The first `#` heading moves into the masthead; everything else
    goes in <main>.
    """
    tokens = _MD.parse(text)
    h1, at = _first_h1(tokens)
    if at is not None:
        # heading_open, inline, heading_close
        heading_html = _MD.renderer.render(tokens[at : at + 3], _MD.options, {})
        rest = tokens[:at] + tokens[at + 3 :]
    else:
        heading_html = ""
        rest = tokens
    body = _wrap_tables(_MD.renderer.render(rest, _MD.options, {}))
    name = (title or "").strip() or h1
    head_title = escape(name or "Untitled page")
    page = (
        "<!doctype html>\n"
        '<html lang="en-GB">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{head_title}</title>\n"
        f"<style>\n{templates.shared_css()[0]}\n{_EXTRA_CSS}</style>\n"
        "</head>\n<body>\n"
        '<header class="masthead">\n'
        f"{_TOPLINE}"
        '  <p class="eyebrow">PAGE &middot; <!--artifacts:updated--></p>\n'
        f"  {heading_html.strip()}\n"
        "  <hr>\n</header>\n"
        f"<main>\n{body}</main>\n"
        "</body>\n</html>\n"
    )
    return page, name
