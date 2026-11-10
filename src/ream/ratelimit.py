"""Slow down a caller that keeps presenting keys that do not work.

homelab-ij8.100, 22 Sep 2026. Jeremy made external access the priority, and the
agent API is about to be reachable from the internet. Every route there
authenticates a bearer key; until now nothing counted how often a caller got it
wrong. On a local network that was defensible -- the possible callers were the machines
in the house. On the internet a failed key is free and silent, and a key is the
only thing between a stranger and his pages.

The keys themselves are strong (`art_` + 43 characters, hashed at rest), so this
is not about making guessing feasible or not. It is about failure no longer
being free, a burst being visible afterwards, and a misconfigured agent being
told plainly why it is being turned away instead of retrying a 401 for ever.

WHO IS "A CALLER": the address nginx hands us in `X-Real-IP`, which it sets
itself and overwrites whatever a client sent. Behind the public ingress that is
the visitor's real address, recovered from Cloudflare's `CF-Connecting-IP` and
trusted only from the tunnel's own pods (the deployment's platform config, and a
network policy that makes that CIDR trust safe). Behind the internal
ingress it is the local-network peer. With no such header -- tests, a direct connection --
the socket's own peer.

WHAT THIS DELIBERATELY DOES NOT DO, and why:

  * It never refuses a key that WORKS. A blocked address still has its key
    looked up (one indexed sha256 query), and a valid key is served. The first
    draft refused everything from a blocked address before the database, and
    that would have been an outage generator: the internal ingress's
    LoadBalancer runs with the default externalTrafficPolicy (Cluster), so local
    callers may reach the app SNATed to a node address. One misconfigured agent
    would then have locked every seat in the house out for the cooldown. So a
    block turns a bad key's 401 into a 429 with Retry-After -- a plain "stop" a
    well-behaved client obeys -- and quietens the log; it is not a wall.
    Refusing VOLUME is Cloudflare's half of homelab-ij8.100, in front of the
    tunnel, where it costs nothing here.
  * A success does not wipe the count. On a shared address a good seat would
    otherwise keep resetting a broken one, and it would never be told to stop.
    Failures simply age out of the window.
  * It is not shared across replicas. One replica runs today (deploy/); with
    more, each would count separately and a caller would get N times the
    budget. Revisit when that changes, not before.
  * It does not stop a pod INSIDE the cluster, which can reach the service
    directly and send any `X-Real-IP` it likes. Something already inside the
    cluster is not what this is for.
  * It never records the key, not even a hash of it. The log says which
    address failed and how many times; that is enough to see a burst.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ream import settings

# Ten wrong keys inside a minute from one address earns five minutes of
# refusal. Generous for a person fixing a typo, short enough that a mistake is
# not a long outage, and far below anything useful for guessing.
THRESHOLD = 10
WINDOW_SECONDS = 60.0
COOLDOWN_SECONDS = 300.0

# The memory bound. An attacker cycling addresses must not be able to grow this
# without limit; past the cap the oldest entries go first, which only ever
# forgets a caller, never blocks one wrongly.
MAX_TRACKED = 10_000


@dataclass
class _Entry:
    failures: list[float]
    blocked_until: float = 0.0


class FailedAuthLimiter:
    """Counts failed authentications per address and refuses an address that
    has earned it. Thread-safe: the agent routes are sync and run in a pool."""

    def __init__(
        self,
        *,
        threshold: int = THRESHOLD,
        window: float = WINDOW_SECONDS,
        cooldown: float = COOLDOWN_SECONDS,
        max_tracked: int = MAX_TRACKED,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.window = window
        self.cooldown = cooldown
        self.max_tracked = max_tracked
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def retry_after(self, address: str) -> float:
        """Seconds until `address` may try again, or 0 when it is not blocked."""
        now = self._clock()
        with self._lock:
            entry = self._entries.get(address)
            if entry is None or entry.blocked_until <= now:
                return 0.0
            return entry.blocked_until - now

    def failed(self, address: str) -> tuple[int, bool, bool]:
        """Record one failure. Returns (failures inside the window, whether the
        address is blocked now, whether THIS failure is the one that blocked
        it). The last is what the caller logs on, so a blocked address costs
        one log line per cooldown rather than one per request."""
        now = self._clock()
        with self._lock:
            entry = self._entries.pop(address, None) or _Entry(failures=[])
            entry.failures = [t for t in entry.failures if now - t < self.window]
            entry.failures.append(now)
            newly_blocked = False
            if entry.blocked_until <= now and len(entry.failures) >= self.threshold:
                entry.blocked_until = now + self.cooldown
                newly_blocked = True
            # Re-inserted at the end, so dict order is least-recently-failed
            # first and eviction drops the caller that has been quiet longest.
            self._entries[address] = entry
            while len(self._entries) > self.max_tracked:
                self._entries.pop(next(iter(self._entries)))
            return len(entry.failures), entry.blocked_until > now, newly_blocked


def client_address(headers, peer: str | None) -> str:
    """The address to count against: the header the `trusted_ip_header` setting names (ours
    is `X-Real-IP`; unset means none, the socket address). See the module docstring for why
    `X-Real-IP` is trusted and `X-Forwarded-For` is not: nginx overwrites the
    first and merely appends to the second, so only the first cannot be written
    by the caller."""
    name = settings.current().trusted_ip_header.lower()
    real = (headers.get(name) or "").strip() if name else ""
    return real or (peer or "unknown")
