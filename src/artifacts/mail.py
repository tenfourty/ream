"""Sending mail, through Mailgun's HTTP API (homelab-ij8.16).

Jeremy chose his existing Mailgun account (EU, mg.tenfourty.site) with a
separate send-only key for this service. Standard library only. Mail is OFF
until ARTIFACTS_MAILGUN_KEY is set -- `enabled()` says so, and the share page
tells him rather than failing. The key is read at call time and never logged,
printed, or put in an error message.
"""

from __future__ import annotations

import base64
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

_log = logging.getLogger("artifacts.mail")
_TIMEOUT = 15


class MailFailed(Exception):
    """Mailgun did not accept the message. Carries a status, never the key."""


def enabled() -> bool:
    return bool(os.environ.get("ARTIFACTS_MAILGUN_KEY", "").strip())


def send(to: str, subject: str, text: str) -> None:
    key = os.environ.get("ARTIFACTS_MAILGUN_KEY", "").strip()
    if not key:
        raise MailFailed("mail is not configured")
    api = os.environ.get("ARTIFACTS_MAILGUN_API", "https://api.eu.mailgun.net").rstrip(
        "/"
    )
    domain = os.environ.get("ARTIFACTS_MAILGUN_DOMAIN", "mg.tenfourty.site")
    sender = os.environ.get("ARTIFACTS_MAIL_FROM", f"Artifacts <artifacts@{domain}>")
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
