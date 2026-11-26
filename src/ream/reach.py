"""What a caller may reach (the MCP sign-in design, section 11.2): one value, computed once
per request, from a key (exactly today's reach) or from a signed-in app (wider).

The `/mcp` endpoint sets the caller's reach for the length of one message (`acting`); the
store's one page check (`store._owned`, and `store.list_readable`) asks `widened(agent)`
whether the agent it was given is acting with a wider reach than a key's. Nothing else sets
it, so `/api/v1` and every SHARED key behave exactly as before. A PERSONAL key (slice 4) reads
as its person does (`agents.key_reach`, `access.reads_sql`) but still changes only its own pages.
`_scoped` (rename, pin, archive, delete, restore) widens the same way; purge (`destroy`),
transfer and who-can-read never do (`widen=False`).

An app reads as its person (agents design, slice 2): the owner's app as the owner
(`READS_AS_OWNER`), any other person's through `access.reads_sql` (`READS_AS_PERSON`: the
pages of that person's personal agents and the pages open to agents). What an app may
CHANGE is `writes()` in both cases: the agents that person owns, never anyone else's.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

READ = "ream:read"
WRITE = "ream:write"
RELAY = "ream:relay"
PERMISSIONS = (READ, WRITE, RELAY)

# What `readable` can say. A key reads its own pages and others' open to agents; a
# signed-in app reads everything the owner can read, pages closed to agents included,
# never the trash.
READS_AS_AGENT = "agent"
READS_AS_OWNER = "owner"
# A signed-in app of a person who is not the owner: what `access.reads_sql` says.
READS_AS_PERSON = "person"


@dataclass(frozen=True)
class Reach:
    agent: str  # who acts: stored as the author, the publisher, the asker
    readable: str = READS_AS_AGENT
    # The agents whose pages it may change; empty means only its own.
    writable_agents: tuple[str, ...] = ()
    can_relay: bool = True
    permissions: frozenset[str] = frozenset(PERMISSIONS)
    # Whose app this is (None for a key).
    person: str | None = None

    def writes(self) -> tuple[str, ...]:
        return self.writable_agents or (self.agent,)

    def reads_all(self) -> bool:
        return self.readable == READS_AS_OWNER

    def reads_person(self) -> bool:
        return self.readable == READS_AS_PERSON and self.person is not None

    def writes_others(self) -> bool:
        return self.writes() != (self.agent,)


def of_key(agent: str) -> Reach:
    """A key's reach: today's, unchanged. A key holds all three permissions."""
    return Reach(agent=agent)


_current: ContextVar[Reach | None] = ContextVar("ream_reach", default=None)


@contextmanager
def acting(reach: Reach) -> Iterator[Reach]:
    """Act with `reach` inside the block (one MCP message)."""
    token = _current.set(reach)
    try:
        yield reach
    finally:
        _current.reset(token)


def bind(wider: Reach) -> None:
    """Act with `wider` for the rest of this request: a personal key's reach, set by the
    `/api/v1` auth dependency (agents design, slice 4). There is nothing to reset: every request
    runs in its own task, whose context ends with it (an `acting` block is for a message that
    shares a task with others)."""
    _current.set(wider)


def widened(agent: str | None) -> Reach | None:
    """The reach `agent` acts with right now, when it is wider than a key's; None
    otherwise, and always for a call made for any other agent (a nudge, a housekeeping
    pass), so a widened reach never leaks onto someone else's work."""
    found = _current.get()
    if found is None or agent is None or found.agent != agent:
        return None
    if found.reads_all() or found.reads_person() or found.writes_others():
        return found
    return None
