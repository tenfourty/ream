"""Sending mail, through Mailgun's HTTP API (homelab-ij8.16).

Jeremy chose his existing Mailgun account (EU, mg.tenfourty.site) with a
separate send-only key for this service. Standard library only. Mail is OFF
until REAM_MAILGUN_KEY is set -- `enabled()` says so, and the share page
tells him rather than failing. The key is read at call time and never logged,
printed, or put in an error message.
"""

from __future__ import annotations

import base64
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from ream import settings

_log = logging.getLogger("ream.mail")
_TIMEOUT = 15


class MailFailed(Exception):
    """The mail was not accepted. Carries a status, never a key."""


# The mail backend a plug-in registered (core-and-plugins design, 4.2 item 5): one at
# most. With none, the built-in Mailgun code below is the backend (it moves out in 9b).
_backend: tuple[str, Callable, Callable] | None = None


def register_backend(plugin: str, send: Callable, enabled: Callable) -> None:
    """`send(to, subject, text)` and `enabled()`. A second backend is an error."""
    global _backend
    if _backend is not None:
        raise ValueError(f"a mail backend is already registered by {_backend[0]!r}")
    _backend = (plugin, send, enabled)


def _fail(plugin: str, exc: Exception) -> None:
    from ream import plugin_api  # late: plugin_api imports the modules that call this

    plugin_api.count_failure(plugin, "mail")
    _log.warning("plug-in %s: mail backend failed: %s", plugin, type(exc).__name__)


def enabled() -> bool:
    """Whether mail can be sent: the registered backend's answer, else Mailgun's.
    A backend whose `enabled` raises counts as off."""
    if _backend is None:
        return _mailgun_enabled()
    try:
        return bool(_backend[2]())
    except Exception as exc:  # noqa: BLE001 -- a plug-in may raise anything
        _fail(_backend[0], exc)
        return False


def send(to: str, subject: str, text: str) -> None:
    """Send one message through the registered backend, else Mailgun. A backend that
    raises is a `MailFailed` (the callers already handle it); nothing of its message
    is kept."""
    if _backend is None:
        return _mailgun_send(to, subject, text)
    try:
        _backend[1](to, subject, text)
    except Exception as exc:  # noqa: BLE001 -- a plug-in may raise anything
        _fail(_backend[0], exc)
        raise MailFailed("the mail backend failed") from None


def _mailgun_enabled() -> bool:
    """Mail is on when the key, the API, the domain and the sender are all set
    (`Settings.problems` makes a half-set group a start-up refusal)."""
    s = settings.current()
    return bool(s.mailgun_key and s.mailgun_api and s.mailgun_domain and s.mail_from)


def _mailgun_send(to: str, subject: str, text: str) -> None:
    s = settings.current()
    key = s.mailgun_key
    if not _mailgun_enabled():
        raise MailFailed("mail is not configured")
    api = s.mailgun_api.rstrip("/")
    domain = s.mailgun_domain
    sender = s.mail_from
    if not api.startswith("https://"):
        raise MailFailed("mail API must be https")
    data = urllib.parse.urlencode(
        {"from": sender, "to": to, "subject": subject, "text": text}
    ).encode()
    request = urllib.request.Request(  # noqa: S310 -- https, checked above
        f"{api}/v3/{urllib.parse.quote(domain)}/messages", data=data, method="POST"
    )
    auth = base64.b64encode(f"api:{key}".encode()).decode()
    request.add_header("Authorization", f"Basic {auth}")
    request.add_header("User-Agent", "artifacts-service/1")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310
            if response.status >= 300:
                raise MailFailed(f"Mailgun answered {response.status}")
    except urllib.error.HTTPError as exc:
        _log.warning("mail to a recipient refused by Mailgun: HTTP %s", exc.code)
        raise MailFailed(f"Mailgun answered {exc.code}") from None
    except urllib.error.URLError as exc:
        _log.warning("mail could not reach Mailgun: %s", exc.reason)
        raise MailFailed("could not reach Mailgun") from None
