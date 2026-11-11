"""Which PARTS of a page changed between two versions (runcove-5t3oj.56).

Pure and standard-library only, like `pagediff`, and run in the same worker with
the same limits. A part is something found in any HTML and recognisable from one
version to the next:

1. an element with an id that holds a heading or a label (an `aria-label` or
   `data-section-title`), named by its first heading, then its label, then its
   id, and matched across versions BY ITS ID;
2. otherwise a heading (h1 to h3) and the text after it up to the next h1 to h3,
   matched BY ITS HEADING TEXT (white space collapsed; a repeated heading also
   carries its position among its twins);
3. text before the first heading or id: "Top of the page".

A part is hashed by its visible text and the attributes that change what a
reader sees; script, style and the head are left out, so a change that only
touches a class or an inline style is not a changed part. Parts are added,
removed or changed; "moved" is not reported.

An answer is never an empty list for a page this cannot read: a page past a limit
or drawn by its own script answers `unknown_reason`, because an empty list reads
as "nothing changed".
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import OrderedDict
from html.parser import HTMLParser

from ream import pagediff, sections, text

MAX_PARTS = 50
LABEL_MAX = sections.LABEL_MAX
MAX_NODES = 200_000
MAX_DEPTH = 400
TOP_LABEL = "Top of the page"
REASON_FAILED = "could not tell which parts changed"
REASON_SLOW = "too large to compare in time"

_VOID = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    ]
)
_SKIP = frozenset({"script", "style", "template", "head", "noscript", "title"})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_PART_HEADINGS = frozenset({"h1", "h2", "h3"})
_SEEN_ATTRS = ("src", "href", "alt", "value", "checked", "open")
_SCRIPT_BLOCK = re.compile(
    rb"<script\b.*?(?:</script\s*>|\Z)", re.IGNORECASE | re.DOTALL
)
_SPACES = re.compile(r"\s+")


class _TooBig(Exception):
    pass


class _Node:
    __slots__ = ("_first", "attrs", "kids", "tag")

    def __init__(self, tag: str, attrs: dict[str, str | None]):
        self.tag = tag
        self.attrs = attrs
        self.kids: list[_Node | str] = []
        self._first: tuple | None = (
            None  # memo of the first heading below: (found, text)
        )


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self.stack = [self.root]
        self.count = 0

    def _count(self) -> None:
        self.count += 1
        if self.count > MAX_NODES:
            raise _TooBig

    def handle_starttag(self, tag, attrs):
        self._count()
        node = _Node(tag, {k: v for k, v in attrs})
        self.stack[-1].kids.append(node)
        if tag not in _VOID and len(self.stack) < MAX_DEPTH:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._count()
        self.stack[-1].kids.append(_Node(tag, {k: v for k, v in attrs}))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].kids.append(data)


def _plain(node: _Node | str) -> str:
    """All the visible text under `node`, white space collapsed."""
    out: list[str] = []
    todo: list[_Node | str] = [node]
    while todo:
        item = todo.pop()
        if isinstance(item, str):
            out.append(item)
        elif item.tag not in _SKIP:
            todo.extend(reversed(item.kids))
    return _SPACES.sub(" ", " ".join(out)).strip()


def _first_heading(node: _Node) -> tuple[bool, str]:
    """(whether a heading h1 to h6 is inside `node`, the first one's text), memoised."""
    if node._first is not None:
        return node._first
    found: tuple[bool, str] = (False, "")
    todo: list[_Node] = [node]
    # Iterative and memoised per node, so nested ids cost one pass each, not one each per level.
    order: list[_Node] = []
    while todo:
        current = todo.pop()
        order.append(current)
        todo.extend(k for k in reversed(current.kids) if isinstance(k, _Node))
    for current in reversed(order):  # children before parents
        if current._first is not None:
            continue
        if current.tag in _HEADINGS:
            current._first = (True, _plain(current))
            continue
        result: tuple[bool, str] = (False, "")
        for kid in current.kids:
            if isinstance(kid, _Node) and kid.tag not in _SKIP:
                sub = kid._first
                if sub is not None and sub[0]:
                    result = sub
                    break
        current._first = result
    return node._first or found


class _Part:
    __slots__ = ("id", "items", "key", "label")

    def __init__(self, key: tuple, id_: str | None, label: str):
        self.key = key
        self.id = id_
        self.label = label
        self.items: list[str] = []

    def digest(self) -> str:
        return hashlib.sha256(
            "\x1f".join(self.items).encode("utf-8", "surrogatepass")
        ).hexdigest()


class _Collector:
    def __init__(self) -> None:
        self.parts: OrderedDict[tuple, _Part] = OrderedDict()
        self.seen: dict[tuple, int] = {}
        self.heading: _Part | None = None
        self.top: _Part | None = None

    def _make(self, base: tuple, id_: str | None, label: str) -> _Part:
        n = self.seen.get(base, 0)
        self.seen[base] = n + 1
        part = _Part(
            base if n == 0 else (*base, n), id_, text.sanitize(label, LABEL_MAX)
        )
        self.parts[part.key] = part
        return part

    def _home(self, owner: _Part | None) -> _Part:
        if owner is not None:
            return owner
        if self.heading is not None:
            return self.heading
        if self.top is None:
            self.top = self._make(("top",), None, TOP_LABEL)
        return self.top

    def walk(self, root: _Node) -> None:
        # (node, owner part of the nearest id element above it)
        todo: list[tuple[_Node | str, _Part | None]] = [(root, None)]
        while todo:
            item, owner = todo.pop()
            if isinstance(item, str):
                piece = _SPACES.sub(" ", item).strip()
                if piece:
                    self._home(owner).items.append(piece)
                continue
            if item.tag in _SKIP:
                continue
            attrs = item.attrs
            id_ = attrs.get("id")
            label = attrs.get("data-section-title") or attrs.get("aria-label")
            inside = owner
            if id_ and sections.ELEMENT_ID.fullmatch(id_):
                found, heading = _first_heading(item)
                if found or label:
                    name = heading or (label or "") or id_
                    inside = self._make(("id", id_), id_, name)
            elif item.tag in _PART_HEADINGS and owner is None:
                words = _plain(item)
                if words:
                    inside = self.heading = self._make(("h", words), None, words)
            target = inside if inside is not None else None
            seen = [f"{a}={attrs[a]}" for a in _SEEN_ATTRS if a in attrs]
            if seen:
                self._home(target).items.append(" ".join(seen))
            # Children in order: pushed reversed onto the stack.
            todo.extend((kid, inside) for kid in reversed(item.kids))


def _parts(html: str) -> OrderedDict[tuple, _Part]:
    tree = _Tree()
    tree.feed(html)
    tree.close()
    collector = _Collector()
    collector.walk(tree.root)
    return collector.parts


def _script_drawn(html: str) -> bool:
    raw = html.encode("utf-8", "surrogatepass")
    share = sum(len(m.group(0)) for m in _SCRIPT_BLOCK.finditer(raw))
    return share > pagediff.MAX_SCRIPT_SHARE * len(raw) > 0


def _unknown(reason: str) -> dict:
    return {"parts": [], "more": 0, "unknown_reason": reason}


def compare(old_html: str, new_html: str) -> dict:
    """`{"parts": [{id?, label, change}], "more": int, "unknown_reason": str | None}`.

    `change` is `added`, `changed` or `removed`; parts come in the new page's
    order, the removed ones last. At most 50 are listed, `more` counts the rest.
    Never raises: a page this cannot read gets `unknown_reason`, not an empty list."""
    try:
        for html in (old_html, new_html):
            if len(html.encode("utf-8", "surrogatepass")) > pagediff.MAX_BYTES:
                return _unknown(pagediff.REASON_LARGE)
        if _script_drawn(new_html) or _script_drawn(old_html):
            return _unknown(pagediff.REASON_SCRIPT)
        old = _parts(old_html)
        new = _parts(new_html)
    except _TooBig:
        return _unknown(pagediff.REASON_LARGE)
    except Exception:  # noqa: BLE001 - any failure is "could not tell", never "no change"
        return _unknown(REASON_FAILED)
    found: list[dict] = []
    for key, part in new.items():
        before = old.get(key)
        if before is None:
            change = "added"
        elif before.digest() != part.digest():
            change = "changed"
        else:
            continue
        found.append(_row(part, change))
    found.extend(_row(part, "removed") for key, part in old.items() if key not in new)
    return {
        "parts": found[:MAX_PARTS],
        "more": max(0, len(found) - MAX_PARTS),
        "unknown_reason": None,
    }


def _row(part: _Part, change: str) -> dict:
    row = {"label": part.label, "change": change}
    if part.id is not None:
        row = {"id": part.id, **row}
    return row


def changes_safely(
    old_html: str, new_html: str, *, timeout: float | None = None
) -> dict:
    """`compare` in the diff worker (a separate process, 2 seconds, four at a time),
    cached by the hashes of the two pages. An answer from load, a broken worker or a worker
    that ran out of time (`REASON_SLOW`) is given but not cached: it is asked for again."""
    key = (
        "parts",
        hashlib.sha256(old_html.encode("utf-8", "surrogatepass")).hexdigest(),
        hashlib.sha256(new_html.encode("utf-8", "surrogatepass")).hexdigest(),
    )
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit
    fallback = _unknown(REASON_FAILED)
    try:
        result, transient = pagediff._run_in_worker(
            old_html,
            new_html,
            pagediff.TIMEOUT_SECONDS if timeout is None else timeout,
            func=compare,
            fallback=fallback,
            on_timeout=_unknown(REASON_SLOW),
        )
    except Exception:  # noqa: BLE001
        return fallback
    if not (
        isinstance(result, dict)
        and isinstance(result.get("parts"), list)
        and "unknown_reason" in result
    ):
        return fallback
    if not transient:
        with _LOCK:
            _CACHE[key] = result
            while len(_CACHE) > _CACHE_SIZE:
                _CACHE.popitem(last=False)
    return result


_LOCK = threading.Lock()
_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_CACHE_SIZE = 128
