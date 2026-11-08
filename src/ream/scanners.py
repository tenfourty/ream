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
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from ream.scan import Finding

_log = logging.getLogger(__name__)
_lock = threading.Lock()
MAX_FINDINGS = 20


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


_SCANNERS: list[_Scanner] = []


def register(plugin: str, name: str, scan: Callable, on_error: str = "skip") -> None:
    if on_error not in ("skip", "block"):
        raise ValueError(f"scanner {name!r}: on_error must be 'skip' or 'block'")
    with _lock:
        if any(s.name == name for s in _SCANNERS):
            raise ValueError(f"scanner {name!r} is already registered")
        _SCANNERS.append(_Scanner(plugin, name, scan, on_error))


def registered() -> list[str]:
    with _lock:
        return [s.name for s in _SCANNERS]


def run(text: str) -> list[Finding]:
    """Every registered scanner on `text`, findings joined. Raises ScannerBlocked."""
    from ream import plugin_api  # late: plugin_api imports the modules that call this

    with _lock:
        scanners = list(_SCANNERS)
    found: list[Finding] = []
    for s in scanners:
        try:
            result = s.scan(text)
            if not isinstance(result, (list, tuple)) or not all(
                isinstance(f, Finding) for f in result
            ):
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
        found.extend(result[:MAX_FINDINGS])
    return found
