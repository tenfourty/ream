"""What a caller may reach (the MCP sign-in design, section 11.2): one value, computed once
per request, from a key (exactly today's reach) or from a signed-in app (wider).

The `/mcp` endpoint sets the caller's reach for the length of one message (`acting`); the
store's one page check (`store._owned`, and `store.list_readable`) asks `widened(agent)`
whether the agent it was given is acting with a wider reach than a key's. Nothing else sets
it, so `/api/v1` and every key behave exactly as before: a key's reach widens nothing.
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


@dataclass(frozen=True)
class Reach:
    agent: str  # who acts: stored as the author, the publisher, the asker
    readable: str = READS_AS_AGENT
    # The agents whose pages it may change; empty means only its own.
    writable_agents: tuple[str, ...] = ()
    can_relay: bool = True
    permissions: frozenset[str] = frozenset(PERMISSIONS)

    def writes(self) -> tuple[str, ...]:
        return self.writable_agents or (self.agent,)

    def reads_all(self) -> bool:
        return self.readable == READS_AS_OWNER

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


def widened(agent: str | None) -> Reach | None:
    """The reach `agent` acts with right now, when it is wider than a key's; None
    otherwise, and always for a call made for any other agent (a nudge, a housekeeping
    pass), so a widened reach never leaks onto someone else's work."""
    found = _current.get()
    if found is None or agent is None or found.agent != agent:
        return None
    return found if found.reads_all() or found.writes_others() else None
