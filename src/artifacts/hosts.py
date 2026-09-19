"""The one place a `Host` header string becomes a routing decision. `create_app`'s router
and `content._artifact_id` both call these functions on the same normalised host, so
a case variant, a trailing dot or a malformed host is judged identically wherever it is
judged -- never routed one way by the router and read another way by the app it forwarded
to. `first_host_header` is the shared read of the raw ASGI scope that feeds `normalise`
in both places.
"""

import re
from urllib.parse import urlsplit

from artifacts.store import content_domain, gallery_origin

_ARTIFACT_ID = re.compile(r"[a-z2-7]{26}")  # ids.py's alphabet and length, exactly


def normalise(raw: str | None) -> str | None:
    """Lower-case, strip the port (bracket-aware for IPv6 literals: "[::1]:8000" ->
    "[::1]"), strip ONE trailing dot. None or empty -> None. HTTP host names are
    case-insensitive (RFC 9110) and a trailing dot is the same name, so both must route
    identically."""
    if not raw:
        return None
    host = raw.lower()
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return None
        host = host[: end + 1]
    else:
        host = host.split(":", 1)[0]
    host = host.removesuffix(".")
    return host or None


def artifact_id(host: str | None) -> str | None:
    """The artifact id when `host` (already normalised) FULLY matches
    [a-z2-7]{26}\\.<content_domain()>, else None. content_domain() is read at call time."""
    if host is None:
        return None
    match = re.fullmatch(rf"({_ARTIFACT_ID.pattern})\.{re.escape(content_domain())}", host)
    return match.group(1) if match else None


def is_gallery(host: str | None) -> bool:
    """True only when `host` (already normalised) equals urlsplit(gallery_origin()).hostname."""
    if host is None:
        return False
    return host == urlsplit(gallery_origin()).hostname


def first_host_header(scope: dict) -> str | None:
    """The FIRST `host` header from a raw ASGI scope, decoded latin-1 (HTTP header
    bytes are not guaranteed UTF-8). `app.py`'s router and `content.py`'s app both read
    Host the exact same way, so a host is never judged one way by one and another way by
    the other."""
    for key, value in scope.get("headers") or []:
        if key == b"host":
            return value.decode("latin-1")
    return None
