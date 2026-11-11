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
import time
from collections.abc import Iterator

from ream import csp, text
from ream.htmlbound import BoundedParser, TooComplex

MAX_FINDINGS = 10
DETAIL_LIMIT = 300
_MAX_MISSING = 5

LIVE_NOTE = "this page loads live data, which the check cannot load"
UNREAD_NOTE = (
    "the page is too large or complex to check for missing files and live data:"
    " those checks did not run"
)

# What reading a page's markup may cost. A page over MAX_SCAN_CHARS is not read
# at all: that is the publish limit (`store.MAX_BODY_BYTES`, in bytes; a page
# has no more characters than bytes), so a page that could be published is
# never over it. SCAN_SECONDS is what the readings of ONE request may take
# together: `Budget` is made once per request, every reading draws on it, and
# when it is spent each reading stops at its next step. A reading that did not
# finish reports None, which is "not read" and never "nothing found".
MAX_SCAN_CHARS = 8 * 1024 * 1024
SCAN_SECONDS = 2.0


class Budget:
    """The time one request allows for reading a page's markup."""

    def __init__(self, seconds: float = SCAN_SECONDS) -> None:
        self._deadline = time.monotonic() + seconds
        self._steps = 0
        self.spent = False

    def check(self) -> None:
        if self.spent or time.monotonic() >= self._deadline:
            self.spent = True
            raise TooComplex

    def step(self) -> None:
        """One more token read. Looks at the clock on every 64th call, and on
        every call once the budget is spent."""
        self._steps += 1
        if self.spent or not self._steps & 63:
            self.check()


def _admit(html: str, budget: Budget) -> None:
    """Stops before any work when the page is over the cap or the budget is gone."""
    if len(html) > MAX_SCAN_CHARS:
        raise TooComplex
    budget.check()


class _Metered(BoundedParser):
    """A BoundedParser that counts every token it reads against a Budget, and stops
    with TooComplex when the budget is spent or a tag is too long to read."""

    def __init__(self, budget: Budget) -> None:
        super().__init__(convert_charrefs=False, budget=budget)
        self.budget = budget

    def _tick(self, *_args) -> None:
        self.budget.step()

    handle_data = handle_comment = handle_decl = handle_pi = _tick
    unknown_decl = handle_charref = handle_entityref = _tick


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


class _Placed(Exception):
    """The opener has what it came for."""


class _Opener(_Metered):
    """Finds where the check script must go, by tokenising the page as a browser
    would: never by searching the text, which finds "<head" inside a comment or a
    title, where the script would not run."""

    def __init__(self, budget: Budget) -> None:
        super().__init__(budget)
        self.head: tuple[int, int] | None = None  # (line, col) of the tag, and its text
        self.html: tuple[int, int, str] | None = None
        self.first: tuple[int, int] | None = None
        self.raw: str | None = (
            None  # inside a title or textarea, whose text is not markup
        )

    def handle_starttag(self, tag, attrs):
        self.budget.step()
        if self.raw:
            return
        pos = self.getpos()
        text = self.get_starttag_text() or ""
        if tag == "head":
            self.head = (*pos, text)
            raise _Placed
        elif tag == "html":
            self.html = (*pos, text)
        elif tag == "body":
            if self.first is None:
                self.first = pos
            raise _Placed
        else:
            if self.first is None:
                self.first = pos
            if tag in ("title", "textarea"):
                self.raw = tag

    def handle_startendtag(self, tag, attrs):
        self.budget.step()
        if self.raw:
            return
        if self.first is None and tag not in ("html", "head"):
            self.first = self.getpos()

    def handle_endtag(self, tag):
        self.budget.step()
        if self.raw == tag:
            self.raw = None


def _offset(html: str, line: int, col: int) -> int:
    at = 0
    for _ in range(line - 1):
        at = html.index("\n", at) + 1
    return at + col


def inject(html: str, nonce: str, budget: Budget | None = None) -> str:
    """`html` with the check script as the first thing to run: just inside its
    head, else inside its html element, else before its first element. Found
    with a tokenizer, so a "<head>" in a comment or a title is never the place.
    A page over the size cap, or one the tokenizer cannot finish in its budget,
    gets the script first of all. For the render copy only: the stored body is
    never touched."""
    tag = script(nonce)
    budget = Budget() if budget is None else budget
    try:
        _admit(html, budget)
        finder = _Opener(budget)
        try:
            finder.feed(html)
            finder.close()
        except _Placed:  # the head or the body was found: nothing after it matters
            pass
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

# The page is read once, front to back: each search starts where the last one
# stopped, so the cost grows with the size of the page and never with the number
# of tags in it. (A pattern like `<tag\b[^>]*>` run over a page of unclosed tags
# looks to the end of the page from each one.) The reads give exactly what such
# patterns gave; tests/test_render_findings.py holds the patterns and compares.

# What Ream serves on a page's own address besides the page: the shared sheet.
_SERVED = {"/_shared.css"}
_TAG = re.compile(
    r"<(img|script|source|video|audio|track|iframe|embed|link|object)\b",
    re.IGNORECASE,
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
_SCRIPT_OPEN = re.compile(r"<script\b", re.IGNORECASE)
_SCRIPT_CLOSE = re.compile(r"</script\s*>", re.IGNORECASE)
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _blank_script_bodies(html: str, budget: Budget) -> str:
    """Script contents blanked, their opening tags kept (a `src` on one counts).
    A script with no closing tag is left as it is."""
    out: list[str] = []
    at = 0
    while True:
        opened = _SCRIPT_OPEN.search(html, at)
        if opened is None:
            break
        end = html.find(">", opened.end())
        if end < 0:
            break  # an opening tag that never ends: no later one ends either
        closed = _SCRIPT_CLOSE.search(html, end + 1)
        if closed is None:
            break  # nothing closes this one, so nothing closes a later one
        budget.step()
        out.append(html[at : end + 1])
        out.append("</script>")
        at = closed.end()
    out.append(html[at:])
    return "".join(out)


def _tags(html: str, budget: Budget) -> Iterator[tuple[str, str]]:
    """(lower-case name, the text up to its first ">") of each resource tag in
    the page, in order: none inside a comment or a closed script's text."""
    body = _blank_script_bodies(csp.outside_comments(html, budget), budget)
    at = 0
    while True:
        found = _TAG.search(body, at)
        if found is None:
            return
        end = body.find(">", found.end())
        if end < 0:
            return  # a tag that never ends: no later one ends either
        at = end + 1
        budget.step()
        yield found.group(1).lower(), body[found.end() : end]


def _missing_files(html: str, budget: Budget) -> list[str]:
    _admit(html, budget)
    seen: list[str] = []
    for tag, attrs in _tags(html, budget):
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


def missing_files(html: str, budget: Budget | None = None) -> list[str] | None:
    """Relative or root-relative paths a picture, stylesheet, script or other
    resource points to, that this page does not have. Navigation links, `data:`
    and fragment links, other hosts and anything inside a comment or a script's
    text are not looked at. In page order, each once, capped.

    None when the page was not read (over the size cap, the budget ran out, or a
    failure part-way): that is never the same as an empty list."""
    try:
        return _missing_files(html, Budget() if budget is None else budget)
    except TooComplex:
        return None
    except Exception:  # noqa: BLE001 - not read to its end: not "no missing files"
        return None


_LIVE = re.compile(r"\bclaude\s*\.\s*use\s*\(|\bfetch\s*\(|XMLHttpRequest|EventSource")


class _Found(Exception):
    """The reader has its answer."""


class _Scripts(_Metered):
    def __init__(self, budget: Budget) -> None:
        super().__init__(budget)
        self.in_script = False
        self.live = False

    def handle_starttag(self, tag, attrs):
        self.budget.step()
        self.in_script = tag == "script"

    def handle_endtag(self, tag):
        self.budget.step()
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        self.budget.step()
        if self.in_script and _LIVE.search(data):
            self.live = True
            raise _Found


def _loads_live_data(html: str, budget: Budget) -> bool | None:
    _admit(html, budget)
    if _LIVE.search(html) is None:
        return False  # the parser hands the script's text over unchanged: no match, no live data
    reader = _Scripts(budget)
    try:
        reader.feed(html)
        reader.close()
    except _Found:  # the answer is yes: the rest of the page cannot change it
        return True
    except TooComplex:
        raise
    except Exception:  # noqa: BLE001 - the page was not read to its end: not "no live data"
        return None
    return reader.live


def loads_live_data(html: str, budget: Budget | None = None) -> bool | None:
    """Whether the page's SCRIPT asks Ream for data, which the render (no
    network) cannot give it. Ream keeps no per-page flag for this (a page's
    connect-src follows whether the version is the live one), so the script is
    read, not the text or the comments around it. It decides only whether a
    blank page or a script error is told to the reader or to the agent alone.

    None when the page was not read (over the size cap, the budget ran out, or the
    parser failed on it): that is never the same as False."""
    try:
        return _loads_live_data(html, Budget() if budget is None else budget)
    except TooComplex:
        return None


def static_scan(html: str) -> tuple[list[str] | None, bool | None]:
    """(missing_files, loads_live_data) for one request's page, both reads
    drawing on one Budget: the time they may take is bounded for the pair, not
    for each. Pass the two to `assess`; a None in either says the page was not
    fully read."""
    budget = Budget()
    return missing_files(html, budget), loads_live_data(html, budget)


# --- the verdict ----------------------------------------------------------------


def _clean(detail: str) -> str:
    return text.sanitize(detail, DETAIL_LIMIT)


def assess(
    errors: list[str] | None,
    check: dict | None,
    missing: list[str] | None,
    live: bool | None,
) -> tuple[str, list[dict]]:
    """(verdict, findings) for one render. `ok`, `broken` or `unchecked`.

    `broken` is a script error, a blank page or a missing file. A page too wide
    for its window is a finding for the agent and does not break it. No check
    line is `unchecked`. On a page that loads live data a blank page or a
    script error may be fine for the reader, so they are told to the agent
    alone: the verdict is `unchecked`, not `broken`.

    `missing` and `live` are None when the page was not read (too large or too
    complex, see `static_scan`): the verdict is then never `ok`, with a finding
    that says so, and what is not known about live data is treated as live."""
    found: list[dict] = []
    breaks = False
    uncertain = False
    unread = missing is None or live is None
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
        if kind in ("blank", "script_error") and (live or unread):
            uncertain = True
        elif counts:
            breaks = True

    if unread:
        uncertain = True
        found.append({"kind": "unchecked", "detail": UNREAD_NOTE})
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
    for path in (missing or [])[:_MAX_MISSING]:
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
