"""The service's settings, read at call time (docs/agent/specs/2026-11-04-core-and-plugins-design.md, 3.1).

Slice 1 holds only the OWNER: the one person who reads the gallery. The other `os.environ`
reads stay where they are until slice 2 moves them in here.

`current()` builds the object from the environment on every call and caches nothing, so a
test or a pod can change a value without an import-time freeze. There is no module-level
`SETTINGS`. `override(...)` layers values over the environment for one block (tests).

`owner_id` is a STORED value (`by`, `author`, `answered_by`, `actor`, folder owner), not
only a label: `owner.check` records it in `service_meta` and refuses to start on a database
written for another one.

Unset means NEUTRAL: id `owner`, name `the owner`, pronoun `they`. A deployment whose rows
already say another id sets `ARTIFACTS_OWNER_ID`; the start-up guard stops a pod that forgot.
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, fields

PRONOUNS = ("they", "he", "she")
# Validated like an agent name (`keys.AGENT_NAME`): the id is typed on command lines and
# stored where agent names live. Kept here too so this module imports nothing of the service.
OWNER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_NAME_MAX = 80

DEFAULT_OWNER_ID = "owner"
DEFAULT_OWNER_NAME = "the owner"
DEFAULT_OWNER_PRONOUN = "they"


@dataclass(frozen=True, repr=False)
class Settings:
    owner_id: str = DEFAULT_OWNER_ID
    owner_name: str = DEFAULT_OWNER_NAME
    owner_pronoun: str = DEFAULT_OWNER_PRONOUN

    def __repr__(self) -> str:
        return "Settings(...)"

    def problems(self) -> list[str]:
        """Every reason these values cannot serve, each naming the variable, never a secret."""
        found = []
        if not OWNER_ID.fullmatch(self.owner_id):
            found.append(
                f"ARTIFACTS_OWNER_ID must match {OWNER_ID.pattern!r}"
                " (lower-case letters, digits and hyphens, no spaces)"
            )
        if not self.owner_name.strip() or len(self.owner_name) > _NAME_MAX:
            found.append(f"ARTIFACTS_OWNER_NAME must be 1 to {_NAME_MAX} characters")
        if self.owner_pronoun not in PRONOUNS:
            found.append(
                f"ARTIFACTS_OWNER_PRONOUN must be one of {', '.join(PRONOUNS)}"
            )
        return found


_ENV = {
    "owner_id": "ARTIFACTS_OWNER_ID",
    "owner_name": "ARTIFACTS_OWNER_NAME",
    "owner_pronoun": "ARTIFACTS_OWNER_PRONOUN",
}
_overrides: dict[str, str] = {}
_lock = threading.Lock()


def current() -> Settings:
    """The settings now: the environment, then any `override`. A blank variable is unset."""
    values = {}
    for f in fields(Settings):
        raw = os.environ.get(_ENV[f.name], "").strip()
        if raw:
            values[f.name] = raw
    values.update(_overrides)
    return Settings(**values)


@contextlib.contextmanager
def override(**values: str) -> Iterator[Settings]:
    """Layer `values` over the environment until the block ends (tests). Process-wide,
    not per thread: a request handled on another thread sees it too."""
    unknown = set(values) - set(_ENV)
    if unknown:
        raise TypeError(f"unknown setting {sorted(unknown)}")
    with _lock:
        before = dict(_overrides)
        _overrides.update(values)
    try:
        yield current()
    finally:
        with _lock:
            _overrides.clear()
            _overrides.update(before)


def validate_for_serve() -> Settings:
    """Settings for start-up, or a `SystemExit` listing every problem at once."""
    s = current()
    found = s.problems()
    if found:
        raise SystemExit("settings: " + "; ".join(found))
    return s


def owner_id() -> str:
    return current().owner_id
