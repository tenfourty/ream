"""Times a read of a page on a page and on one eight times as big.

These timings mean something only on a quiet machine: the tests that use them are marked
`scaling`, skip under xdist (tests/conftest.py), and run serially in CI's own step.

A read whose time grows with the page takes about 8 times as long on 8 times the
page; one that grows with its square takes about 64 times as long. `assert_linear`
holds the ratio under 20. The ratio is only worth anything when the small run is long
enough to measure, so a small page that takes under MIN_SMALL seconds (best of three
runs) is doubled, up to MOST: the ratio is always taken from what was measured, with
no floor under it to hide a read that is a little worse than linear.

The pages start at 1 MiB, which is also as big as the small one gets: a page of
1 MiB to 8 MiB (the cap on what a read looks at) does not cross the size where the
machine's cache stops holding it, which can make a linear read cost twice as much per
character on a page a few times bigger and so push the ratio toward the limit.
"""

import math
import time
from collections.abc import Callable

RATIO = 20  # linear is about 8, squared about 64
MIN_SMALL = (
    0.05  # seconds: the shortest timing the ratio is taken from (repeats make it up)
)
RUNS = 3
MOST = 1024 * 1024  # the most the small page grows to; eight times that is the scan cap
SLOW = 2.0  # seconds: a run this long is not repeated
PROBE = (4096, 32768)  # the two small pages tried first, so a read that grows with the
PROBE_FLOOR = (
    0.005  # square fails in seconds, not in the hours its 8 MiB page would take
)


def page(first: str, unit: str, chars: int, last: str = "") -> str:
    """`first`, then `unit` repeated, then `last`, in exactly `chars` characters."""
    body = chars - len(first) - len(last)
    return first + (unit * (body // len(unit) + 1))[:body] + last


def best(read: Callable[[str], object], text: str, runs: int = RUNS) -> float:
    """The shortest of `runs` timings of one `read(text)`. A read quicker than
    MIN_SMALL is timed as a batch of repeats lasting at least MIN_SMALL and divided,
    so a read of a millisecond is not lost in the clock's noise; a run over SLOW
    seconds is the last."""
    began = time.perf_counter()
    read(text)
    once = time.perf_counter() - began
    if once > SLOW:
        return once
    repeats = max(1, math.ceil(MIN_SMALL / max(once, 1e-6)))
    shortest = once
    for _ in range(runs):
        began = time.perf_counter()
        for _ in range(repeats):
            read(text)
        took = (time.perf_counter() - began) / repeats
        shortest = min(shortest, took)
        if took > SLOW:
            break
    return shortest


def measure(
    read: Callable[[str], object],
    first: str,
    unit: str,
    last: str = "",
    start: int = MOST,
) -> tuple[int, float, float]:
    """(characters in the small page, its best time, the best time on eight times
    as many). The small page starts at `start` and doubles until it takes MIN_SMALL,
    or is MOST."""
    size = start
    while True:
        small = best(read, page(first, unit, size, last))
        if small >= MIN_SMALL or size * 2 > MOST:
            break
        size *= 2
    return size, small, best(read, page(first, unit, 8 * size, last))


def assert_linear(
    read: Callable[[str], object],
    first: str,
    unit: str,
    last: str = "",
    start: int = MOST,
    probe: bool = True,
) -> tuple[int, float, float]:
    """Fails when the time on eight times the page is RATIO times the time on the
    page or more. Returns what it measured.

    Two small pages are timed first, with a floor under the smaller time so that a read
    too quick to time passes; the real measure follows either way, with no floor."""
    if probe:
        low, high = (best(read, page(first, unit, n, last)) for n in PROBE)
        assert high < RATIO * max(low, PROBE_FLOOR), (
            f"{PROBE[0]} characters took {low * 1000:.0f} ms, {PROBE[1]} took"
            f" {high * 1000:.0f} ms: {high / max(low, PROBE_FLOOR):.0f} times as long"
            f" for {PROBE[1] // PROBE[0]} times the page"
        )
    size, small, large = measure(read, first, unit, last, start)
    assert large < RATIO * small, (
        f"{size} characters took {small * 1000:.0f} ms, {8 * size} took"
        f" {large * 1000:.0f} ms: {large / small:.0f} times as long for 8 times the page"
    )
    return size, small, large
