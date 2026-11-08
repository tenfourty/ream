"""Served text as templates (core-and-plugins design 3.4; slice 3).

Everything an agent or a reader is served that names the owner, the gallery or the house is
written with `{{name}}` and filled from the settings at the moment it is served. Nothing of
the house is in the words; it is in the settings.

    render("Ask {{owner_name}}; {{owner_they}} answer{{owner_s}} in a day.")
    -> "Ask Jeremy; he answers in a day."   (owner "Jeremy", pronoun "he")
    -> "Ask the owner; they answer in a day." (the defaults)

A name that is not known RAISES, so a typo is a failing test and a 500 in development, never a
silent `{{owner_nmae}}` in front of a reader (`tests/test_textvars.py` renders every served
text under two sets of settings). `client=True` is for the tools' own words (see `variables`). A caller adds its own names with keyword arguments
(`base=`, `connect_line=`); they win over the settings'.

Capitals follow the name: `{{Owner_they}}` is "He" or "They", `{{OWNER_THEY}}` is "HE".

Variables:
    owner_id        the owner's id, as stored ("jeremy")
    owner_name      the owner's display name ("Jeremy", "the owner")
    owner_themselves  the reflexive ("himself")
    owner_they / owner_them / owner_their / owner_theirs      the pronoun forms
    owner_are / owner_has   the verbs that agree with it ("is"/"are")
    owner_s / owner_es      the verb ending ("answers" / "answer"): `answer{{owner_s}}`
    gallery_origin  where people open the gallery, no trailing slash
    api_host        the API's host name (the gallery's host when none is set)
    product         what the service calls itself ("artifacts")
    footer          the line under a shipped template; empty by default
    host_suffix     " &middot; <host_label>" for a template's eyebrow, empty when unset
    proxy_notes     a note about the proxy in front, with a space before it; see below
    proxy_hold_note how a proxy treats a held request (a clause)

The proxy notes come from `REAM_PROXY_NOTES_FILE`: two paragraphs, the first a note on
what a client of the proxy should know ("Send a User-Agent of your own: ..."), the second the
clause saying what it does to a request held open. They are separated by a line holding only
`%%`. A deployment with no proxy sets nothing and says nothing about one.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from ream import settings
from ream.text import pronoun

_NAME = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}")
_SPLIT = re.compile(r"^%%[ \t]*$", re.MULTILINE)
# What a reader sees when no notes file is set: a clause that is true of any proxy.
_NO_HOLD_NOTE = "may pass through a proxy that cuts a request held too long,"


_log = logging.getLogger(__name__)

# What a plug-in may add (design 4.2, "text"). Both registries are empty with no plug-in, and
# then nothing below changes a served word.
SLOTS = ("proxy", "tools", "integrations")  # named places in the served documents
DOCS = ("agents.txt", "skill.md")
# The slots each document carries a `{{section_<slot>}}` marker for.
DOC_SLOTS = {"agents.txt": SLOTS, "skill.md": ("integrations",)}
_MAX_PLUGIN_TEXT = 20_000
_PLUGIN_VARS: dict[str, tuple[str, str | Callable[[], str]]] = {}
_SECTIONS: list[tuple[str, str, Callable[[str], str]]] = []
# A plug-in's function runs inside the unauthenticated onboarding documents, so its result is
# kept this many seconds per (plug-in, variable) or (plug-in, slot, document): a slow function
# costs at most once per period. A fault (the empty string) is kept too.
_TEXT_TTL = 60.0
_CACHE: dict[tuple, tuple[float, str]] = {}


class UnknownVariable(KeyError):
    """A `{{name}}` that nothing defines."""


def plugin_prefix(plugin: str) -> str:
    """The namespace a plug-in's variables live in: its name, lower-cased, runs of anything
    but letters and digits made `_`, then `_`. Core names never start with a plug-in's."""
    return re.sub(r"[^a-z0-9]+", "_", plugin.lower()).strip("_") + "_"


def register_var(plugin: str, name: str, value: str | Callable[[], str]) -> None:
    """Add `{{name}}` for `plugin`. `name` must be lower-case, start with the plug-in's own
    prefix (`plugin_prefix`), not be a core variable, and not be taken. `value` is a string
    or a function of no arguments read each time the text is served."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(
            f"plug-in {plugin!r}: variable name {name!r} is not lower_snake"
        )
    if not name.startswith(plugin_prefix(plugin)):
        raise ValueError(
            f"plug-in {plugin!r}: variable {name!r} must start with {plugin_prefix(plugin)!r}"
        )
    if name in variables() or name.startswith("section_"):
        raise ValueError(f"plug-in {plugin!r}: variable {name!r} is a core name")
    if name in _PLUGIN_VARS:
        raise ValueError(f"plug-in {plugin!r}: variable {name!r} is already registered")
    if not isinstance(value, str) and not callable(value):
        raise TypeError(
            f"plug-in {plugin!r}: variable {name!r} needs a string or function"
        )
    _PLUGIN_VARS[name] = (plugin, value)


def register_section(plugin: str, slot: str, fn: Callable[[str], str]) -> None:
    """Add text to a named slot. `fn(doc)` gets `agents.txt` or `skill.md` and returns the
    paragraph(s) for it, or "" for none. One section per (plug-in, slot)."""
    if slot not in SLOTS:
        raise ValueError(f"plug-in {plugin!r}: unknown text slot {slot!r}")
    if not callable(fn):
        raise TypeError(f"plug-in {plugin!r}: text section {slot!r} needs a function")
    if any(p == plugin and sl == slot for p, sl, _f in _SECTIONS):
        raise ValueError(f"plug-in {plugin!r}: text section {slot!r} is registered")
    _SECTIONS.append((plugin, slot, fn))


def _fault(plugin: str, exc: BaseException | None = None) -> str:
    """A plug-in's text could not be had: say nothing in its place. The type, never the
    message (it may hold content), is all that is logged."""
    from ream import plugin_api

    plugin_api.count_failure(plugin, "text")
    _log.warning(
        "plug-in %s: text failed: %s", plugin, type(exc).__name__ if exc else "BadValue"
    )
    return ""


def _checked(plugin: str, value: object) -> str:
    if not isinstance(value, str) or len(value) > _MAX_PLUGIN_TEXT:
        return _fault(plugin)
    return value


def _cached(key: tuple, compute: Callable[[], str]) -> str:
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and 0 <= now - hit[0] < _TEXT_TTL:
        return hit[1]
    value = compute()
    _CACHE[key] = (now, value)
    return value


def _plugin_value(key: str) -> str | None:
    found = _PLUGIN_VARS.get(key)
    if found is None:
        return None
    plugin, value = found
    if isinstance(value, str):
        return value

    def compute() -> str:
        try:
            return _checked(plugin, value())
        except Exception as exc:  # noqa: BLE001 - a plug-in's fault is isolated
            return _fault(plugin, exc)

    return _cached(("var", plugin, key), compute)


def section(slot: str, doc: str) -> str:
    """What plug-ins add at `slot` of `doc`: each non-empty section as its own paragraph
    (a newline before, one after), in registration order. Empty with no plug-in."""
    out = []
    for plugin, sl, fn in _SECTIONS:
        if sl != slot or slot not in DOC_SLOTS.get(doc, ()):
            continue

        def compute(plugin=plugin, fn=fn) -> str:
            try:
                return _checked(plugin, fn(doc))
            except Exception as exc:  # noqa: BLE001 - a plug-in's fault is isolated
                return _fault(plugin, exc)

        text = _cached(("section", plugin, slot, doc), compute)
        if text.strip("\n"):
            out.append("\n" + text.strip("\n") + "\n")
    return "".join(out)


def _read_notes(path: str) -> tuple[str, str]:
    if not path:
        return "", ""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return "", ""
    parts = _SPLIT.split(raw, maxsplit=1)
    first, second = parts[0], parts[1] if len(parts) > 1 else ""
    return first.strip(), second.strip()


def gallery_origin(client: bool = False) -> str:
    """Where people open the gallery, no trailing slash. For the command-line tools (`client`)
    a machine that does not set `REAM_GALLERY_ORIGIN` has the origin of the API it talks
    to, which is the same place for every deployment that serves both from one host."""
    s = settings.current()
    if client and not settings.is_set("gallery_origin"):
        parts = urlsplit(s.api_url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return s.gallery_origin.rstrip("/")


def variables(client: bool = False) -> dict[str, str]:
    """Every variable, from the settings as they are now.

    `client` is for the command-line tools' own words (`--help`, the news): a machine that runs
    them seldom has `REAM_OWNER_*` set, and an owner left unset there is OUR house's
    (`settings.HOUSE_DEFAULT_OWNER`, until slice 7), not the neutral one the server takes."""
    s = settings.current()
    if client:
        s = replace(
            s,
            **{
                k: v
                for k, v in settings.HOUSE_DEFAULT_OWNER.items()
                if not settings.is_set(k)
            },
        )
    p = pronoun(s.owner_pronoun)
    origin = gallery_origin(client)
    notes, hold = _read_notes(s.proxy_notes_file)
    return {
        "owner_id": s.owner_id,
        "owner_name": s.owner_name,
        "owner_they": p.subject,
        "owner_them": p.object,
        "owner_themselves": p.reflexive,
        "owner_their": p.possessive,
        "owner_theirs": p.possessive_pronoun,
        "owner_are": p.be,
        "owner_has": p.have,
        "owner_s": "" if p.plural else "s",
        "owner_es": "" if p.plural else "es",
        "gallery_origin": origin,
        "api_host": s.api_host or (urlsplit(origin).hostname or ""),
        "product": s.product,
        "footer": s.footer,
        "host_suffix": f" &middot; {s.host_label}" if s.host_label else "",
        "proxy_notes": f" {notes}" if notes else "",
        "proxy_hold_note": hold or _NO_HOLD_NOTE,
    }


def _lookup(name: str, table: dict[str, str]) -> str:
    def get(key: str) -> str | None:
        return table[key] if key in table else _plugin_value(key)

    lower = name[0].lower() + name[1:]
    for key, shape in (
        (name, str),
        (name.lower() if name.upper() == name else "", str.upper),
        (lower, lambda v: v[:1].upper() + v[1:]),
    ):
        value = get(key) if key else None
        if value is not None:
            return shape(value)
    raise UnknownVariable(name)


def render(text: str, *, client: bool = False, **extra: object) -> str:
    """`text` with each `{{name}}` filled in; an unknown name raises `UnknownVariable`."""
    table = {**variables(client), **{k: str(v) for k, v in extra.items()}}
    return _NAME.sub(lambda m: _lookup(m.group(1), table), text)


def words(text: str) -> str:
    """A command-line tool's own fixed words, filled for the machine it runs on. Pass it only
    literals from the source, never text that came from a user or a server."""
    return render(text, client=True)


def names_in(text: str) -> set[str]:
    """The names a text uses, as written (for the test that finds a typo)."""
    return {m.group(1) for m in _NAME.finditer(text)}
