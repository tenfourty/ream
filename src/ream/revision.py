"""How `if_revision` names a page's revision (stdlib only: the server and the client share it).

A revision is the 16-hex-character hash every update returns (`store._revision`). A caller may
give it whole, shortened to 8 or more of its leading characters, in either case, or give the
version number instead (all digits). A prefix is only ever compared with the CURRENT revision.
"""

MIN_PREFIX = 8
_HEX = frozenset("0123456789abcdef")

FORM = (
    f"a revision is the hash an update returns ({MIN_PREFIX} or more of its characters)"
    " or the version number"
)


def well_formed(given: str) -> bool:
    """Whether `given` could name a revision: a version number, or {MIN_PREFIX}+ hex characters."""
    given = given.strip().lower()
    if given.isascii() and given.isdigit():
        return True
    return len(given) >= MIN_PREFIX and set(given) <= _HEX


def matches(given: str, version_n: int | None, revision: str) -> bool:
    """True when `given` names the page's current `revision` or `version_n`."""
    given = given.strip().lower()
    if given.isascii() and given.isdigit():
        return int(given) == version_n
    if not well_formed(given):
        return False
    return revision.startswith(given)
