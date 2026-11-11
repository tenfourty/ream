"""Sending mail, through the one mail backend a plug-in registered.

The core sends nothing itself (core-and-plugins design, 4.2 item 5; step 9b took the
built-in Mailgun code out): with no backend registered mail is OFF, `enabled()` says so,
and the share page tells the owner rather than failing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

_log = logging.getLogger("ream.mail")


class MailFailed(Exception):
    """The mail was not accepted. Carries a status, never a key."""


# The mail backend a plug-in registered (core-and-plugins design, 4.2 item 5): one at
# most. With none, mail is off.
_backend: tuple[str, Callable, Callable] | None = None
# A raising `enabled` is logged once per process (gallery asks on every render).
_enabled_fault_logged = False


def register_backend(plugin: str, send: Callable, enabled: Callable) -> None:
    """`send(to, subject, text)` and `enabled()`. A second backend is an error."""
    global _backend
    if _backend is not None:
        raise ValueError(f"a mail backend is already registered by {_backend[0]!r}")
    _backend = (plugin, send, enabled)


def _fail(plugin: str, exc: Exception, *, once: bool = False) -> None:
    global _enabled_fault_logged
    from ream import plugin_api  # late: plugin_api imports the modules that call this

    plugin_api.count_failure(plugin, "mail")
    level = logging.WARNING
    if once:
        if _enabled_fault_logged:
            level = logging.DEBUG
        _enabled_fault_logged = True
    _log.log(level, "plug-in %s: mail backend failed: %s", plugin, type(exc).__name__)


def has_backend() -> bool:
    """Whether a plug-in registered a mail backend (call after the plug-ins load)."""
    return _backend is not None


def enabled() -> bool:
    """Whether mail can be sent: the registered backend's answer; off with none.
    A backend whose `enabled` raises counts as off."""
    if _backend is None:
        return False
    try:
        return bool(_backend[2]())
    except Exception as exc:  # noqa: BLE001 -- a plug-in may raise anything
        _fail(_backend[0], exc, once=True)
        return False


def send(to: str, subject: str, text: str) -> None:
    """Send one message through the registered backend; with none, `MailFailed`. A backend
    that raises is a `MailFailed` (the callers already handle it); nothing of its message
    is kept."""
    if not enabled():
        raise MailFailed("mail is not configured")
    try:
        _backend[1](to, subject, text)
    except Exception as exc:  # noqa: BLE001 -- a plug-in may raise anything
        _fail(_backend[0], exc)
        raise MailFailed("the mail backend failed") from None
