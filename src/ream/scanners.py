"""Scanners a plug-in registers (core-and-plugins design, 4.2 item 4).

The core's own pattern scan (`scan.py`) always runs. A registered scanner is an extra
opinion taken in the same place the core takes its second opinion (`ggscan`): after the
pattern scan came back clean, on a body and on a field whose caller asks for the second
opinion, and on an agent comment's second scan. Any finding refuses exactly as a
pattern finding does. With none registered `run` returns [] without doing anything.

`on_error` says what a scanner that raises (or returns something that is not a list of
`Finding`) means: "skip" -- no opinion, logged and counted, the page publishes on the
rest (what `ggscan` does today when the relay cannot be reached) -- or "block" -- the
action fails (`ScannerBlocked`). Only the exception's type is logged, never its message
or the text.

Each call is bounded: `SCAN_TIMEOUT_SECONDS` unless the scanner declared its own `timeout`
when it registered, which is capped at `MAX_SCAN_TIMEOUT_SECONDS`.

A scanner named `gitguardian` (step 9c: `ggscan`, moved into the house plug-in) makes the
core's own relay scan stand down while it is registered: see `ggscan.stood_down`.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass

from ream.scan import Finding

_log = logging.getLogger(__name__)
_lock = threading.Lock()
MAX_FINDINGS = 20
MAX_SHAPE_LENGTH = 64
# One plug-in scanner call is abandoned after this long and counts as a scanner error
# (`on_error` applies). It runs in a daemon thread that cannot be killed, so a scanner
# that really hangs leaks one thread per call (bounded only by how often the caller
# calls; never reclaimed), as a hung sink does.
SCAN_TIMEOUT_SECONDS = 10.0
# A scanner may declare its own bound (`register(..., timeout=)`), never more than this:
# the time the core's own relay scan (`ggscan.TIMEOUT_SECONDS`) always had, so that when
# `ggscan` stands down for the `gitguardian` scanner (step 9c) a 10 to 20 second answer
# is still an answer. A test pins the two together.
MAX_SCAN_TIMEOUT_SECONDS = 20.0


class ScannerBlocked(Exception):
    """A `block` scanner failed. Names the plug-in and scanner, never a value."""

    def __init__(self, plugin: str, name: str):
        super().__init__(f"scanner {name!r} of plug-in {plugin!r} failed")
        self.plugin = plugin
        self.name = name


@dataclass(frozen=True)
class _Scanner:
    plugin: str
    name: str
    scan: Callable
    on_error: str
    timeout: float | None = None  # None: SCAN_TIMEOUT_SECONDS

    def bound(self) -> float:
        """Seconds one call may take: the declared bound, capped at the relay's, else
        the default. Read at call time, so the two module constants stay settable."""
        if self.timeout is None:
            return SCAN_TIMEOUT_SECONDS
        return min(self.timeout, MAX_SCAN_TIMEOUT_SECONDS)


_SCANNERS: list[_Scanner] = []


def register(
    plugin: str,
    name: str,
    scan: Callable,
    on_error: str = "skip",
    timeout: float | None = None,
) -> None:
    if not callable(scan):
        raise TypeError(f"scanner {name!r}: scan must be callable")
    if on_error not in ("skip", "block"):
        raise ValueError(f"scanner {name!r}: on_error must be 'skip' or 'block'")
    if timeout is not None and not (
        isinstance(timeout, (int, float))
        and not isinstance(timeout, bool)
        and math.isfinite(timeout)
        and timeout > 0
    ):
        raise ValueError(
            f"scanner {name!r}: timeout must be a positive number of seconds"
        )
    with _lock:
        if any(s.name == name for s in _SCANNERS):
            raise ValueError(f"scanner {name!r} is already registered")
        _SCANNERS.append(_Scanner(plugin, name, scan, on_error, timeout))


def registered() -> list[str]:
    with _lock:
        return [s.name for s in _SCANNERS]


def _valid(result: object) -> bool:
    return isinstance(result, (list, tuple)) and all(
        isinstance(f, Finding)
        and isinstance(f.shape, str)
        and 0 < len(f.shape) <= MAX_SHAPE_LENGTH
        for f in result
    )


def _call_bounded(scan: Callable, text: str, name: str, bound: float) -> object:
    """`scan(text)` within `bound` seconds, else TimeoutError. What it raises is
    raised here."""
    outcome: list[tuple[bool, object]] = []

    def work() -> None:
        try:
            outcome.append((True, scan(text)))
        except Exception as exc:  # noqa: BLE001 -- handed to the caller
            outcome.append((False, exc))
        except BaseException as exc:  # noqa: BLE001 -- sys.exit() etc. in a plug-in
            # Must not escape the caller's `except Exception`: it is a scanner error.
            outcome.append(
                (False, RuntimeError(f"scanner raised {type(exc).__name__}"))
            )

    worker = threading.Thread(target=work, name="plugin-scanner", daemon=True)
    worker.start()
    worker.join(bound)
    if not outcome:
        raise TimeoutError(f"scanner {name}: exceeded {bound}s")
    ok, value = outcome[0]
    if not ok:
        raise value  # type: ignore[misc]
    return value


def run(text: str) -> list[Finding]:
    """Every registered scanner on `text`, findings joined. Raises ScannerBlocked."""
    return run_detailed(text)[0]


def run_detailed(text: str) -> tuple[list[Finding], bool]:
    """Like `run`, and whether at least one scanner gave a real answer (a skipped,
    failed scanner is no answer). Raises ScannerBlocked."""
    from ream import plugin_api  # late: plugin_api imports the modules that call this

    with _lock:
        scanners = list(_SCANNERS)
    found: list[Finding] = []
    answered = False
    for s in scanners:
        try:
            result = _call_bounded(s.scan, text, s.name, s.bound())
            if not _valid(result):
                raise TypeError("scanner did not return a list of Finding")
        except Exception as exc:  # noqa: BLE001 -- a plug-in may raise anything
            plugin_api.count_failure(s.plugin, "scanner")
            _log.warning(
                "plug-in %s: scanner %s failed (%s): %s",
                s.plugin,
                s.name,
                s.on_error,
                type(exc).__name__,
            )
            if s.on_error == "block":
                raise ScannerBlocked(s.plugin, s.name) from None
            continue
        answered = True
        found.extend(result[:MAX_FINDINGS])  # type: ignore[index]
    return found, answered
