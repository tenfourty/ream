"""The plug-in API and its loader (core-and-plugins design, 4.1, 4.2, 4.5, 4.6).

A plug-in is a Python package with an entry point in the group `ream.plugins` (or the old
`artifacts.plugins`, kept for good)
naming a function `register(api: PluginAPI) -> None`. The server calls each once at
start-up, in name order. Registrations: `sink`, `answer_source`, `on_answer`, `scanner`, `mail_backend`.

A plug-in that raises while registering, names a major API version this core does not
speak, or is named in `REAM_PLUGINS` but not installed, stops start-up
(`PluginError`, naming it): a source or sink silently absent is worse than a pod
that does not start.

A sink a plug-in registers is wrapped: when `deliver` raises, the failure is logged with the
plug-in and interface and counted per plug-in (`failure_counts()`; also under
`plugin_failures` in `sinks.status()`), and then raised on, so the outbox retries and an
after-commit failure is dropped by the machinery that already does both. A timeout is
not counted there: the outbox gives up waiting while the plug-in's `deliver` is still
running, so the wrapper sees no failure; the outbox row's `last_error` says
`TimeoutError`, and the circuit breaker counts it.
"""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Callable
from importlib import metadata

from ream import decisions, mail, scanners, settings, sinks

API_VERSION = (1, 0)
GROUP = "ream.plugins"
# The old group name, read for good beside the new one.
OLD_GROUP = "artifacts.plugins"

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_failures: dict[tuple[str, str], int] = {}
_loaded: list[str] = []


class PluginError(Exception):
    """A plug-in could not be loaded. The message names it, never a value."""


def failure_counts() -> dict[str, int]:
    """Failures caught since the process started, keyed `<plug-in>:<interface>`."""
    with _lock:
        return {f"{p}:{i}": n for (p, i), n in sorted(_failures.items())}


def loaded() -> list[str]:
    """The plug-ins `load` registered, in the order it called them."""
    return list(_loaded)


def count_failure(plugin: str, interface: str) -> None:
    """Count one caught failure of `plugin`'s `interface` (see `failure_counts`)."""
    _count(plugin, interface)


def _count(plugin: str, interface: str) -> None:
    with _lock:
        _failures[(plugin, interface)] = _failures.get((plugin, interface), 0) + 1


class PluginAPI:
    """What `register` is handed. One per plug-in, so a registration knows whose it is."""

    version = API_VERSION

    def __init__(self, plugin: str) -> None:
        self._plugin = plugin
        # The answer sources this plug-in registered: `on_answer` needs one, and
        # runs only for answers stored with one of them.
        self._sources: set[str] = set()

    def sink(
        self,
        name: str,
        kinds: tuple[str, ...] | list[str],
        deliver: Callable,
        delivery: str = "outbox",
        *,
        label: str | None = None,
    ) -> None:
        """Register an event sink (4.2, 1). `deliver(event)` returns on success and
        raises to retry (outbox) or be logged (after commit)."""
        plugin = self._plugin

        def guarded(event):
            try:
                deliver(event)
            except Exception as exc:
                _count(plugin, "sink")
                _log.warning(
                    "plug-in %s: sink %s failed: %s", plugin, name, type(exc).__name__
                )
                raise

        sinks.register(
            sinks.Sink(
                name, tuple(kinds), deliver=guarded, delivery=delivery, label=label
            )
        )

    def answer_source(
        self,
        name: str,
        label: str,
        *,
        relay: bool = False,
        verified_label: str | None = None,
        route_label: str | None = None,
    ) -> None:
        """Declare a stored `answer_source` value and its wording (4.2, 2)."""
        self._sources.add(name)
        decisions.register_source(
            name,
            label,
            relay=relay,
            verified_label=verified_label,
            route_label=route_label,
        )

    def on_answer(self, name: str, fn: Callable) -> None:
        """Register an answer hook (4.2, 3). `fn(conn, decision, answer)` runs inside the
        answer's transaction, after the row is written and before the commit, for answers
        stored with a source THIS plug-in registered (`answer_source`, called first: a
        plug-in with none is refused, so the hook stays with the plug-in that vouches
        for its own inbound answers). It is for answers the person gave through the
        plug-in's own inbound path (`decisions.answer`), NOT for relays: an agent's
        `record_elsewhere` never runs it. `answer` is a `decisions.Answer`. A raise aborts
        the whole answer (`decisions.AnswerHookFailed`, naming the plug-in)."""
        if not self._sources:
            raise ValueError(
                f"plug-in {self._plugin!r}: on_answer needs an answer_source"
                " registered first"
            )
        # The live set, so a source registered after the hook counts too.
        decisions.register_answer_hook(self._plugin, name, fn, self._sources)

    def scanner(self, name: str, scan: Callable, on_error: str = "skip") -> None:
        """Register a scanner (4.2, 4). `scan(text) -> list[scan.Finding]` is run where
        the core takes its second opinion (after a clean pattern scan: bodies, section
        notes, agent comments' second scan). `on_error="skip"`: a raise is logged and
        counted and the text is judged on the other scanners; `"block"`: it fails the
        action (`scanners.ScannerBlocked`). Under `block` an agent comment not yet
        cleared is hidden (no event), so replies to it fail as for a withdrawn comment
        until a clean scan shows it again. Each call is bounded by
        `scanners.SCAN_TIMEOUT_SECONDS`; a scanner that hangs costs one daemon thread
        per call, never reclaimed."""
        scanners.register(self._plugin, name, scan, on_error)

    def mail_backend(self, name: str, send: Callable, enabled: Callable) -> None:
        """Register the mail backend (4.2, 5). `send(to, subject, text)` raises to fail;
        `enabled()` says whether mail is on. One backend at most; it replaces the
        built-in Mailgun code. `name` is only for the log."""
        mail.register_backend(self._plugin, send, enabled)


def _entry_points() -> list:
    """The installed `ream.plugins` and (for good) `artifacts.plugins` entry points. Where one
    name is in both groups the `ream.plugins` one wins. Tests replace this."""
    found = {ep.name: ep for ep in metadata.entry_points(group=OLD_GROUP)}
    found.update({ep.name: ep for ep in metadata.entry_points(group=GROUP)})
    return list(found.values())


def _requires_major(register: Callable) -> int | None:
    module = inspect.getmodule(register)
    declared = getattr(module, "REQUIRES_API", None)
    if declared is None:
        return None
    return int(str(declared).split(".")[0])


def load() -> list[str]:
    """Register every plug-in `REAM_PLUGINS` selects, once, in name order.
    Raises PluginError (naming the plug-in) and loads nothing further on a failure."""
    wanted = settings.current().plugins.strip()
    found = {ep.name: ep for ep in _entry_points()}
    if wanted.lower() == "none":
        names: list[str] = []
    elif wanted:
        names = [n.strip() for n in wanted.split(",") if n.strip()]
        for name in names:
            if name not in found:
                raise PluginError(
                    f"REAM_PLUGINS names plug-in {name!r}, which is not installed"
                )
        names = sorted(set(names))
    else:
        names = sorted(found)
    done: list[str] = []
    for name in names:
        try:
            register = found[name].load()
            major = _requires_major(register)
            if major is not None and major != API_VERSION[0]:
                raise PluginError(
                    f"plug-in {name!r} requires plug-in API {major}, this core speaks"
                    f" {API_VERSION[0]}"
                )
            register(PluginAPI(name))
        except PluginError:
            raise
        except Exception as exc:
            raise PluginError(
                f"plug-in {name!r} failed to register: {type(exc).__name__}"
            ) from exc
        done.append(name)
        _log.info("plug-in %s registered", name)
    _loaded[:] = done
    return done
