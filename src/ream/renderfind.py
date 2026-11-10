"""What the service's own render found wrong with a page (runcove-5t3oj.56).

Pure functions, no browser: the check script added to the render copy, the
reading of the one line it logs, the static missing-file check, and the verdict
that sums them up. `thumbs.py` runs the browser and calls these.

A finding is never "fine by default": no check line is `unchecked`, and a page
that stops scripts can only get that. A page can forge nothing without the
nonce, and can only affect its own verdict.
"""

from __future__ import annotations

import re
import secrets
from html.parser import HTMLParser

from ream import csp, text

MAX_FINDINGS = 10
DETAIL_LIMIT = 300
_MAX_MISSING = 5

LIVE_NOTE = "this page loads live data, which the check cannot load"


def new_nonce() -> str:
    return secrets.token_hex(8)


# The script that measures the render copy. It keeps a private reference to
# console.log, removes its own element, and reports twice: after load and 2.5 s
# later (the render allows 3 s of simulated time). Plain `key=value` integers,
# not JSON, so the line survives whatever quoting the browser's log applies.
_SCRIPT = (
    "<script>(function(){var n='%s',log=console.log.bind(console),"
    "s=document.currentScript;if(s&&s.parentNode)s.parentNode.removeChild(s);"
    "addEventListener('error',function(e){try{if(e&&e.message)log('ream-error '+n+"
    "' line='+(e.lineno|0)+': '+String(e.message).replace(/[\\\\\"\\r\\n]/g,' ').slice(0,300));}"
    "catch(x){}},true);"
    "function vis(e){var r=e.getBoundingClientRect(),c=getComputedStyle(e);"
    "return r.width>0&&r.height>0&&c.visibility!=='hidden'&&c.display!=='none'"
    "&&c.opacity!=='0';}"
    "function go(){try{var b=document.body,d=document.documentElement,t=0,m=0;"
    "if(b){t=(b.innerText||'').replace(/\\s+/g,'').length;"
    "var q=b.querySelectorAll('img,svg,canvas,video,picture,iframe,object,embed');"
    "for(var i=0;i<q.length;i++){if(vis(q[i]))m++;}}"
    "var h=b?Math.round(b.getBoundingClientRect().height):0;"
    "log('ream-check '+n+' text='+t+' media='+m+' height='+h+' scroll='"
    "+d.scrollWidth+' view='+d.clientWidth);}catch(e){}}"
    "addEventListener('load',function(){go();setTimeout(go,2500);});"
    "})();</script>"
)


def script(nonce: str) -> str:
    return _SCRIPT % nonce


class _Opener(HTMLParser):
    """Finds where the check script must go, by tokenising the page as a browser
    would: never by searching the text, which finds "<head" inside a comment or a
    title, where the script would not run."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.head: tuple[int, int] | None = None  # (line, col) of the tag, and its text
        self.html: tuple[int, int, str] | None = None
        self.first: tuple[int, int] | None = None
        self.raw: str | None = (
            None  # inside a title or textarea, whose text is not markup
        )
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done or self.raw:
            return
        pos = self.getpos()
        text = self.get_starttag_text() or ""
        if tag == "head":
            self.head = (*pos, text)
            self.done = True
        elif tag == "html":
            self.html = (*pos, text)
        elif tag == "body":
            self.done = True
            if self.first is None:
                self.first = pos
        else:
            if self.first is None:
                self.first = pos
            if tag in ("title", "textarea"):
                self.raw = tag

    def handle_startendtag(self, tag, attrs):
        if self.done or self.raw:
            return
        if self.first is None and tag not in ("html", "head"):
            self.first = self.getpos()

    def handle_endtag(self, tag):
        if self.raw == tag:
            self.raw = None


def _offset(html: str, line: int, col: int) -> int:
    at = 0
    for _ in range(line - 1):
        at = html.index("\n", at) + 1
    return at + col


def inject(html: str, nonce: str) -> str:
    """`html` with the check script as the first thing to run: just inside its
    head, else inside its html element, else before its first element. Found
    with a tokenizer, so a "<head>" in a comment or a title is never the place.
    For the render copy only: the stored body is never touched."""
    tag = script(nonce)
    finder = _Opener()
    try:
        finder.feed(html)
        finder.close()
    except Exception:  # noqa: BLE001 - a page the parser cannot take is still checked
        return tag + html
    for found in (finder.head, finder.html):
        if found:
            line, col, text = found
            at = _offset(html, line, col) + len(text)
            return html[:at] + tag + html[at:]
    if finder.first is not None:
        at = _offset(html, *finder.first)
        return html[:at] + tag + html[at:]
    return tag + html


_CHECK = re.compile(
    r"ream-check ([0-9a-f]{16}) text=(\d{1,9}) media=(\d{1,9}) height=(\d{1,9})"
    r" scroll=(\d{1,9}) view=(\d{1,9})"
)


def parse_check(stderr: str, nonce: str) -> dict | None:
    """The numbers from the last check line carrying `nonce`, or None. A line
    with another nonce, or a malformed or oversized one, is not read."""
    found = None
    for match in _CHECK.finditer(stderr or ""):
        if match.group(1) == nonce:
            found = match
    if found is None:
        return None
    keys = ("text", "media", "height", "scroll", "view")
    return {k: int(v) for k, v in zip(keys, found.groups()[1:], strict=True)}


_ERROR = re.compile(r"ream-error ([0-9a-f]{16}) line=(\d{1,9}): ([^\"\n]{0,300})")


def parse_errors(stderr: str, nonce: str) -> list[str]:
    """The script errors the check script itself saw (it listens in the capture
    phase before any page code, so a page's own `window.onerror` returning true
    cannot hide one), as "line N: message". Only lines carrying `nonce`."""
    return [
        f"line {m.group(2)}: {m.group(3)}"
        for m in _ERROR.finditer(stderr or "")
        if m.group(1) == nonce
    ][:MAX_FINDINGS]


# --- static: a file the page points to that Ream does not serve ---------------

# What Ream serves on a page's own address besides the page: the shared sheet.
_SERVED = {"/_shared.css"}
_RESOURCE = re.compile(
    r"<(img|script|source|video|audio|track|iframe|embed|link|object)\b([^>]*)>",
    re.IGNORECASE | re.DOTALL,
)
_ATTR = re.compile(r"""\b(src|href|data)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_REL = re.compile(r"""\brel\s*=\s*["']?([^"'>]*)""", re.IGNORECASE)
_FILE_RELS = {
    "stylesheet",
    "icon",
    "shortcut",
    "preload",
    "modulepreload",
    "apple-touch-icon",
}
_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _blank_script_bodies(html: str) -> str:
    """Script contents blanked, their opening tags kept (a `src` on one counts)."""

    def keep_open(match: re.Match) -> str:
        block = match.group(0)
        return block[: block.index(">") + 1] + "</script>"

    return _SCRIPT_BLOCK.sub(keep_open, html)


def missing_files(html: str) -> list[str]:
    """Relative or root-relative paths a picture, stylesheet, script or other
    resource points to, that this page does not have. Navigation links, `data:`
    and fragment links, other hosts and anything inside a comment or a script's
    text are not looked at. In page order, each once, capped."""
    seen: list[str] = []
    body = _blank_script_bodies(csp.outside_comments(html))
    for match in _RESOURCE.finditer(body):
        tag, attrs = match.group(1).lower(), match.group(2)
        if tag == "link":
            rel = _REL.search(attrs)
            if rel is None or not (set(rel.group(1).lower().split()) & _FILE_RELS):
                continue
        for attr in _ATTR.finditer(attrs):
            name = attr.group(1)
            if name == "href" and tag != "link":
                continue
            if name == "data" and tag != "object":
                continue
            value = (attr.group(2) or attr.group(3) or attr.group(4) or "").strip()
            if not value or value.startswith(("#", "//")) or _SCHEME.match(value):
                continue
            path = re.split(r"[?#]", value, maxsplit=1)[0]
            if path in _SERVED or not path or path in seen:
                continue
            seen.append(path)
            if len(seen) >= MAX_FINDINGS:
                return seen
    return seen


_LIVE = re.compile(r"\bclaude\s*\.\s*use\s*\(|\bfetch\s*\(|XMLHttpRequest|EventSource")


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.in_script = False
        self.live = False

    def handle_starttag(self, tag, attrs):
        self.in_script = tag == "script"

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script and _LIVE.search(data):
            self.live = True


def loads_live_data(html: str) -> bool:
    """Whether the page's SCRIPT asks Ream for data, which the render (no
    network) cannot give it. Ream keeps no per-page flag for this (a page's
    connect-src follows whether the version is the live one), so the script is
    read, not the text or the comments around it. It decides only whether a
    blank page or a script error is told to the reader or to the agent alone."""
    reader = _Scripts()
    try:
        reader.feed(html)
        reader.close()
    except Exception:  # noqa: BLE001
        return False
    return reader.live


# --- the verdict ----------------------------------------------------------------


def _clean(detail: str) -> str:
    return text.sanitize(detail, DETAIL_LIMIT)


def assess(
    errors: list[str] | None,
    check: dict | None,
    missing: list[str],
    live: bool,
) -> tuple[str, list[dict]]:
    """(verdict, findings) for one render. `ok`, `broken` or `unchecked`.

    `broken` is a script error, a blank page or a missing file. A page too wide
    for its window is a finding for the agent and does not break it. No check
    line is `unchecked`. On a page that loads live data a blank page or a
    script error may be fine for the reader, so they are told to the agent
    alone: the verdict is `unchecked`, not `broken`."""
    found: list[dict] = []
    breaks = False
    uncertain = False
    note = f" ({LIVE_NOTE})" if live else ""

    def add(kind: str, detail: str, counts: bool = True) -> None:
        nonlocal breaks, uncertain
        found.append(
            {
                "kind": kind,
                "detail": _clean(
                    detail
                    + (note if live and kind in ("blank", "script_error") else "")
                ),
            }
        )
        if kind in ("blank", "script_error") and live:
            uncertain = True
        elif counts:
            breaks = True

    if check is None:
        uncertain = True
        found.append(
            {
                "kind": "unchecked",
                "detail": "the check did not report: scripts were blocked, or the page never finished loading",
            }
        )
    else:
        if check["text"] == 0 and check["media"] == 0:
            add("blank", "nothing a reader could see: no text and no picture")
        elif check["height"] == 0:
            add("blank", "the page body has no height")
    for path in missing[:_MAX_MISSING]:
        add("missing_file", f"points to {path}, which this page does not have")
    for error in errors or []:
        add("script_error", error)
    if check is not None and check["scroll"] > check["view"] + 1:
        found.append(
            {
                "kind": "too_wide",
                "detail": f"scrolls sideways: {check['scroll']} px wide in a {check['view']} px window",
            }
        )
    found = found[:MAX_FINDINGS]
    if breaks:
        return "broken", found
    if uncertain:
        return "unchecked", found
    return "ok", found
