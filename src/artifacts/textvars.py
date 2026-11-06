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

The proxy notes come from `ARTIFACTS_PROXY_NOTES_FILE`: two paragraphs, the first a note on
what a client of the proxy should know ("Send a User-Agent of your own: ..."), the second the
clause saying what it does to a request held open. They are separated by a line holding only
`%%`. A deployment with no proxy sets nothing and says nothing about one.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from artifacts import settings
from artifacts.text import pronoun

_NAME = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}")
_SPLIT = re.compile(r"^%%[ \t]*$", re.MULTILINE)
# What a reader sees when no notes file is set: a clause that is true of any proxy.
_NO_HOLD_NOTE = "may pass through a proxy that cuts a request held too long,"


class UnknownVariable(KeyError):
    """A `{{name}}` that nothing defines."""


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
    a machine that does not set `ARTIFACTS_GALLERY_ORIGIN` has the origin of the API it talks
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
    them seldom has `ARTIFACTS_OWNER_*` set, and an owner left unset there is OUR house's
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
    if name in table:
        return table[name]
    lower = name[0].lower() + name[1:]
    if name.upper() == name and name.lower() in table:
        return table[name.lower()].upper()
    if lower in table:
        value = table[lower]
        return value[:1].upper() + value[1:]
    raise UnknownVariable(name)


def render(text: str, *, client: bool = False, **extra: object) -> str:
    """`text` with each `{{name}}` filled in; an unknown name raises `UnknownVariable`."""
    table = {**variables(client), **{k: str(v) for k, v in extra.items()}}
    return _NAME.sub(lambda m: _lookup(m.group(1), table), text)


def names_in(text: str) -> set[str]:
    """The names a text uses, as written (for the test that finds a typo)."""
    return {m.group(1) for m in _NAME.finditer(text)}
