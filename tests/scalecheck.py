"""Times a read of a page on a page and on one eight times as big.

The tests that use this are marked `scaling`, skip under xdist (tests/conftest.py), and
run serially in CI's own step; the ones that prove the check still fails a slow read
(`..._fails_the_old_patterns...`, `..._grows_with_the_square`) run anywhere.

A read whose time grows with the page takes about 8 times as long on 8 times the
page; one that grows with its square takes about 64 times as long. `assert_linear`
holds the ratio under 20. The ratio is only worth anything when the small run is long
enough to measure, so a small page that takes under MIN_SMALL seconds (best of three
runs) is doubled, up to MOST: the ratio is always taken from what was measured, with
no floor under it to hide a read that is a little worse than linear. The one exception
is a read that does the 8 MiB page in under QUICK (0.25) seconds: it cannot tie up the server,
and its ratio is of two times too short to compare.

Three things keep a busy machine (CI's shared runners) from deciding the result:

- Every time is the CPU time of the thread doing the read (`time.thread_time`), not
  the clock on the wall: a read here only computes, in the one thread, and the time
  other jobs take the CPU away is not its time. (Not `time.process_time`: while a
  process CPU timer is set, as the stop below sets one, Linux counts the process's
  time only at its ticks, and a read of a millisecond measured as nothing.) (11 Nov 2026: a linear read measured 22 ms on 1 MiB and 455 ms on 8 MiB by
  the wall clock on a loaded runner, a ratio of 21.)
- A comparison that fails is measured once more, and the shortest time of each page
  over both is what is judged: a read that grows with the square fails again, a
  timing that a busy moment stretched does not.
- No read runs unbounded once a smaller page has been timed. Its first run is stopped
  (a CPU-time timer, SIGPROF) at SLACK times what the steepest passing growth allows
  from the time before it, and never under SLOW seconds; a run stopped there grew
  faster than RATIO allows, and is reported as failing. So a read that grows with the
  square fails in seconds even when the early probe is fooled, where it once ran on to
  the 1 MiB page and the 300 s test timeout (core-bare, 11 Nov 2026). A regular
  expression's search looks for the signal only every few thousand starting places,
  so a stop can come a few seconds late; it still comes.

The pages start at 1 MiB, which is also as big as the small one gets: a page of
1 MiB to 8 MiB (the cap on what a read looks at) does not cross the size where the
machine's cache stops holding it, which can make a linear read cost twice as much per
character on a page a few times bigger and so push the ratio toward the limit.
"""

import math
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

RATIO = 20  # linear is about 8, squared about 64
STEEPEST = math.log(RATIO, 8)  # the growth with size that RATIO allows: size ** 1.44
MIN_SMALL = (
    0.05  # seconds: the shortest timing the ratio is taken from (repeats make it up)
)
RUNS = 3
MOST = 1024 * 1024  # the most the small page grows to; eight times that is the scan cap
SLOW = 2.0  # seconds: a run this long is not repeated, and no run is stopped sooner
SLACK = 2  # a first run is stopped at this many times what STEEPEST allows
PROBE = (4096, 32768)  # the two small pages tried first, so a read that grows with the
PROBE_FLOOR = (
    0.005  # square fails in seconds, not in the hours its 8 MiB page would take
)
# Seconds: a read of the 8 MiB cap page this quick is harmless at any ratio
# (0.25, not 0.1: on a busy CI runner a linear read of 8 MiB took 119 ms against 5 ms for
# 1 MiB on 11 Nov 2026, a ratio of two times too short to compare; a quadratic read of
# 8 MiB takes seconds, so 0.25 s still catches one.)
QUICK = 0.25

clock = time.thread_time  # CPU time: what a busy machine takes away is not counted


class _Stopped(BaseException):
    """Raised inside a read that ran past its limit. Not an Exception, so a read's own
    `except Exception` cannot take it for a finding."""


def _stop(_signum, _frame):
    raise _Stopped


@contextmanager
def _stopped_after(seconds: float | None) -> Iterator[None]:
    """Stops the code inside after `seconds` of CPU time (where a timer can be set: the
    main thread of a POSIX process; elsewhere the run is judged after it ends)."""
    if (
        seconds is None
        or not hasattr(signal, "setitimer")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return
    before = signal.signal(signal.SIGPROF, _stop)
    signal.setitimer(signal.ITIMER_PROF, seconds)
    try:
        yield
    finally:
        try:
            signal.setitimer(signal.ITIMER_PROF, 0)
        finally:
            signal.signal(signal.SIGPROF, signal.SIG_DFL if before is None else before)


def page(first: str, unit: str, chars: int, last: str = "") -> str:
    """`first`, then `unit` repeated, then `last`, in exactly `chars` characters."""
    body = chars - len(first) - len(last)
    return first + (unit * (body // len(unit) + 1))[:body] + last


def best(
    read: Callable[[str], object],
    text: str,
    runs: int = RUNS,
    limit: float | None = None,
) -> float:
    """The shortest of `runs` timings of one `read(text)`, in CPU seconds. A read
    quicker than MIN_SMALL is timed as a batch of repeats lasting at least MIN_SMALL and
    divided, so a read of a millisecond is not lost in the clock's noise; a run over
    SLOW seconds is the last. A first run that takes over `limit` seconds is stopped
    there, and the answer is infinity."""
    try:
        with _stopped_after(limit):
            began = clock()
            read(text)
            once = clock() - began
    except _Stopped:
        return math.inf
    if limit is not None and once > limit:
        return math.inf
    if once > SLOW:
        return once
    repeats = max(1, math.ceil(MIN_SMALL / max(once, 1e-6)))
    shortest = once
    for _ in range(runs):
        began = clock()
        for _ in range(repeats):
            read(text)
        took = (clock() - began) / repeats
        shortest = min(shortest, took)
        if took > SLOW:
            break
    return shortest


def allowed(after: tuple[int, float] | None, chars: int) -> float | None:
    """How long a first read of `chars` characters may run, given that one of `after`'s
    characters took its seconds: SLACK times the time that growing as STEEPEST from
    there gives, and never under SLOW. None (no limit) with nothing timed before."""
    if after is None:
        return None
    size, seconds = after
    return max(SLOW, SLACK * seconds * (chars / size) ** STEEPEST)


def measure(
    read: Callable[[str], object],
    first: str,
    unit: str,
    last: str = "",
    start: int = MOST,
    after: tuple[int, float] | None = None,
) -> tuple[int, float, float]:
    """(characters in the small page, its best time, the best time on eight times
    as many). The small page starts at `start` and doubles until it takes MIN_SMALL,
    or is MOST. Each first run is limited by the time before it (`after`, for the
    first); a time that is infinity was stopped at its limit, and nothing bigger is
    timed after it."""
    size = start
    while True:
        small = best(read, page(first, unit, size, last), limit=allowed(after, size))
        if math.isinf(small):
            return size, small, math.inf
        after = (size, small)
        if small >= MIN_SMALL or size * 2 > MOST:
            break
        size *= 2
    large = best(
        read, page(first, unit, 8 * size, last), limit=allowed(after, 8 * size)
    )
    return size, small, large


def _twice(
    measured: Callable[[], tuple[int, float, float]],
    passes: Callable[[int, float, float], bool],
) -> tuple[int, float, float]:
    """`measured()`, and when that fails `passes`, `measured()` once more, keeping the
    shorter time of each page: a ratio that fails twice is the read's, where one that a
    busy moment stretched passes the second time."""
    once = measured()
    if passes(*once):
        return once
    again = measured()
    if again[0] != once[0]:  # the small page doubled to another size: judge the last
        return again
    return once[0], min(once[1], again[1]), min(once[2], again[2])


def _why(small_chars: int, small: float, large_chars: int, large: float) -> str:
    times = large_chars // small_chars
    if math.isinf(small):
        return (
            f"{small_chars} characters ran past the time the smaller page before it"
            f" allows: more than {RATIO} times as long for {times} times the page"
        )
    if math.isinf(large):
        return (
            f"{small_chars} characters took {small * 1000:.0f} ms, {large_chars} ran past"
            f" {allowed((small_chars, small), large_chars):.1f} s: more than"
            f" {SLACK * RATIO} times as long for {times} times the page"
        )
    return (
        f"{small_chars} characters took {small * 1000:.0f} ms, {large_chars} took"
        f" {large * 1000:.0f} ms: {large / small:.0f} times as long for {times} times"
        " the page"
    )


def assert_linear(
    read: Callable[[str], object],
    first: str,
    unit: str,
    last: str = "",
    start: int = MOST,
    probe: bool = True,
) -> tuple[int, float, float]:
    """Fails when the time on eight times the page is RATIO times the time on the
    page or more, measured twice (`_twice`). Returns what it measured.

    Two small pages are timed first, with a floor under the smaller time so that a read
    too quick to time passes; the real measure follows either way, with no floor under
    the ratio, but a read of the 8 MiB cap page in under QUICK seconds passes. Every
    first run after the first page is limited (`allowed`), so no read runs long."""
    after = None
    if probe:
        low_chars, high_chars = PROBE

        def probed() -> tuple[int, float, float]:
            low = max(best(read, page(first, unit, low_chars, last)), PROBE_FLOOR)
            high = best(
                read,
                page(first, unit, high_chars, last),
                limit=allowed((low_chars, low), high_chars),
            )
            return low_chars, low, high

        _, low, high = _twice(probed, lambda _n, low, high: high < RATIO * low)
        assert high < RATIO * low, _why(low_chars, low, high_chars, high)
        after = (high_chars, max(high, PROBE_FLOOR))

    def linear(size: int, small: float, large: float) -> bool:
        # Under QUICK on the biggest page a read is ever given, a read cannot tie up the
        # server whatever its shape, and a ratio of two sub-millisecond times is cache
        # noise (CI measured 0.4 ms and 9 ms on a linear read, a ratio of 24).
        return large < RATIO * small or (size >= MOST and large < QUICK)

    size, small, large = _twice(
        lambda: measure(read, first, unit, last, start, after), linear
    )
    assert linear(size, small, large), _why(size, small, 8 * size, large)
    return size, small, large
