"""The person behind a gallery request (people design, slice 1).

`current(request)` is the one place that says who a gallery request is for, and `require_owner`
the one that says a route is the owner's alone. Today there is one person, the owner (person #1,
`REAM_OWNER_ID`), so `current` answers the owner from settings whatever the request says: no
identity header, cookie or address is read, and the values are exactly what
`settings.owner_id()` gave before. Per-person sign-in (people design, slice 4) changes only the
body of `current`; the handlers that take it do not change again.

`OPEN_SIGN_IN` stays False until step 4 of the design: nothing here lets a second person in.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from ream import settings

# Precedent: `clientlevel.COMMENTS_REQUIRED`. Whether anyone but the owner may sign in.
OPEN_SIGN_IN = False


@dataclass(frozen=True)
class Person:
    """Who a request is for. `id` is the stored id (`by`, `author`, `answered_by`, `actor`)."""

    id: str
    name: str
    is_owner: bool


def owner_person() -> Person:
    """Person #1, from settings, read at call time."""
    s = settings.current()
    return Person(id=s.owner_id, name=s.owner_name, is_owner=True)


def current(request: Request) -> Person:
    """The person this gallery request is for. A FastAPI dependency; `request` is taken so the
    signature is the one slice 4 needs, and is not read yet."""
    return owner_person()


CURRENT = Depends(current)


def require_owner(person: Person = CURRENT) -> Person:
    """The route is the owner's alone (machines, keys, page ownership). Answers 404, as for a
    page that is not there, to anyone else; today `current` never returns anyone else."""
    if not person.is_owner:
        raise HTTPException(404, "not found")
    return person


# Module-level, so a handler's default is a name, not a call (ruff B008).
REQUIRE_OWNER = Depends(require_owner)
