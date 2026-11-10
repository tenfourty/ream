"""The second opinion on every page: GitGuardian's ggshield, via a relay.

homelab-ij8.2. Jeremy's ruling, 24 Sep 2026: every publish, update and preview
is also checked by ggshield; a finding refuses the page exactly as the built-in
scanner does; and when ggshield cannot be reached the page publishes on the
built-in scan alone and that is logged. (The bead first asked for fail-closed;
the ruling replaced it, with the service already public on the built-in scan.)

WHY A RELAY AND NOT GGSHIELD IN THE IMAGE: a machine of the house already has
ggshield installed and authenticated for the commit guards, so the service needs
no GitGuardian key of its own, and the image carries no credential for an outside
service. The relay (the deployment's own script) hands back the kind of secret
and its line, never the value.

WHY THE ORIGINAL BODY: the bead's critical design point. The built-in scanner
detags, unescapes and defangs before it matches; handing ggshield those views
would give it the same blind spots, and a second opinion with the first one's
blind spots is not a second opinion. So this sends `body` exactly as the agent
submitted it.

STANDS DOWN FOR THE PLUG-IN (core-and-plugins design, step 9c): the same scan has moved
into the house plug-in as a scanner named `gitguardian`. While a scanner of that name is
registered (`stood_down`), `scan` asks nobody and answers `None`, so every body goes to the
relay once, not twice. This is the only place that rule lives. Without such a scanner nothing
here changes.

THREE QUESTIONS, KEPT APART: `configured()` is only "core's settings name a relay";
`stood_down()` is "the plug-in's scanner has the relay scan"; `relay_expected()` is "somebody
is expected to have scanned this with the relay" (either). A `None` from `scan` while stood
down is NOT the plug-in's clean answer, and not "no relay": `comments.second_scan` accepts a
plug-in's clean answer as the second opinion only when `relay_expected()` is False, and while
stood down it takes the answer of the `gitguardian` scanner itself (`scanners.run_named`),
never another scanner's. A `gitguardian` that gave no opinion is exactly an unreachable relay.

Never raises for anything the relay does; the one exception is text that cannot be encoded as
UTF-8 (`UnencodableText`, a `UnicodeEncodeError`), which is the text's fault and not "the relay
could not be reached" (the stores refuse such text before they ask). `None` means "no second
opinion was had" -- not configured,
unreachable, refused, timed out, or answered in a shape this does not know --
and the caller logs that and publishes. An empty list means ggshield looked and
found nothing.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import re
import threading
import time
import urllib.parse

from ream import scanners, settings
from ream.scan import Finding

# The name of the plug-in scanner this module's relay scan stands down for.
STAND_DOWN_FOR = "gitguardian"
URL_ENV = "REAM_GGSHIELD_URL"
TOKEN_ENV = "REAM_GGSHIELD_TOKEN"  # noqa: S105 -- the variable NAME, not a value
# Inside a publish, and under the public ingress's 90 s read timeout with room
# to spare. A real scan of a 40 KB page takes about two seconds.
TIMEOUT_SECONDS = 20.0
# Reaching the relay's host at all gets far less. A firewall that DROPS rather than
# refuses (the host down, a rule missing) would otherwise hold every publish for the whole
# scan timeout before it fell back to the built-in scan.
CONNECT_SECONDS = 3.0
MAX_FINDINGS = 20

log = logging.getLogger("ream.ggscan")
_said_unconfigured: list[bool] = []
_said_stood_down: list[bool] = []

# ALREADY SCANNED CLEAN (Jeremy, 20 Oct 2026, runcove-5t3oj.35): a body
# GitGuardian passed is not sent again while the answer is fresh, so a retry, a
# repeat publish or a preview followed by its publish waits once, not twice.
# Only a clean answer from a scan that actually ran is remembered: a finding,
# or "could not scan", is asked again every time. Keyed by the body's SHA-256,
# so no page text is held. Fresh for a day, because GitGuardian's detectors
# change; bounded, so a busy day cannot grow it without limit. Per process: a
# restart forgets it, which costs one scan, never a missed one.
CLEAN_FRESH_SECONDS = 24 * 3600
CLEAN_MAX = 2048
_clean: dict[str, float] = {}
_clean_lock = threading.Lock()


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()


def _known_clean(digest: str, now: float) -> bool:
    with _clean_lock:
        at = _clean.get(digest)
        if at is None:
            return False
        if now - at > CLEAN_FRESH_SECONDS:
            del _clean[digest]
            return False
        return True


def _remember_clean(digest: str, now: float) -> None:
    with _clean_lock:
        _clean.pop(digest, None)
        _clean[digest] = now
        while len(_clean) > CLEAN_MAX:
            del _clean[next(iter(_clean))]  # the oldest, by insertion


def stood_down() -> bool:
    """Whether a plug-in scanner named `gitguardian` is registered, so this module's relay
    scan is the plug-in's to do. Read at each call: registration happens at start-up."""
    return STAND_DOWN_FOR in scanners.registered()


def configured() -> bool:
    """Whether core's settings name a relay. Unchanged by a stand-down: the plug-in may
    or may not use the same settings."""
    return bool(settings.current().ggshield_url and settings.current().ggshield_token)


def relay_expected() -> bool:
    """Whether a relay scan is expected for a text: core's relay is set, or the plug-in
    has taken the scan over. When it is, a missing answer is "the relay could not be
    reached", never "there is no relay", and nothing else's clean answer stands in."""
    return stood_down() or configured()


def _shape(kind: str) -> str:
    """'Slack Bot Token' -> 'ggshield_slack_bot_token': the same slug shape the
    built-in scanner's findings carry, prefixed so a reader knows which of the
    two scanners refused the page."""
    slug = re.sub(r"[^a-z0-9]+", "_", kind.lower()).strip("_")[:60]
    return f"ggshield_{slug or 'secret'}"


class _Refused(Exception):
    def __init__(self, status: int):
        self.status = status


class UnencodableText(UnicodeEncodeError):
    """The text cannot be encoded as UTF-8, so nothing was sent. The text's fault, never the
    relay's: `scan` raises it instead of reading it as an unreachable relay."""


def _post(url: str, token: str, body: str) -> object:
    """One POST, with a short connect and a long read."""
    try:
        payload = json.dumps({"body": body}, ensure_ascii=False).encode()
    except UnicodeEncodeError as exc:
        # Kept apart from every other encoding error in this call (a header, say), which is
        # the relay's own configuration and does read as unreachable.
        raise UnencodableText(*exc.args) from exc
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("REAM_GGSHIELD_URL is not an http(s) URL")
    cls = (
        http.client.HTTPSConnection
        if parts.scheme == "https"
        else http.client.HTTPConnection
    )
    conn = cls(parts.hostname, parts.port, timeout=CONNECT_SECONDS)
    try:
        conn.connect()
        conn.sock.settimeout(TIMEOUT_SECONDS)
        conn.request(
            "POST",
            parts.path or "/",
            body=payload,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {token}",
            },
        )
        response = conn.getresponse()
        data = response.read()
        if response.status != 200:
            raise _Refused(response.status)
        return json.loads(data)
    finally:
        conn.close()


def scan(body: str) -> list[Finding] | None:
    if stood_down():
        if not _said_stood_down:
            _said_stood_down.append(True)
            log.info(
                "ggshield relay scan stands down: plug-in scanner %s is registered",
                STAND_DOWN_FOR,
            )
        return None
    cfg = settings.current()
    url, token = cfg.ggshield_url, cfg.ggshield_token
    if not url or not token:
        if not _said_unconfigured:
            _said_unconfigured.append(True)
            log.warning(
                "ggshield relay not configured: pages get the built-in scan only"
            )
        return None
    digest, now = _digest(body), time.monotonic()
    if _known_clean(digest, now):
        return []
    try:
        answer = _post(url, token, body)
    except UnencodableText:
        raise
    except _Refused as exc:
        log.warning("ggshield relay refused the scan: HTTP %s", exc.status)
        return None
    except (OSError, ValueError, http.client.HTTPException) as exc:
        log.warning("ggshield relay unreachable: %s", type(exc).__name__)
        return None
    if not isinstance(answer, dict) or answer.get("scanned") is not True:
        reason = answer.get("reason") if isinstance(answer, dict) else None
        log.warning("ggshield could not scan: %s", str(reason or "unknown")[:120])
        return None
    raw = answer.get("findings")
    if not isinstance(raw, list):
        log.warning("ggshield relay answered without a findings list")
        return None
    found = []
    for item in raw[:MAX_FINDINGS]:
        if not isinstance(item, dict):
            continue
        line = item.get("line")
        found.append(
            Finding(
                shape=_shape(str(item.get("type") or "")),
                line=line if isinstance(line, int) and line > 0 else 1,
            )
        )
    if raw and not found:
        # ggshield said something and none of it was readable: that is not
        # "clean", and it must not publish as though it were.
        found.append(Finding(shape="ggshield_secret", line=1))
    if not found:
        _remember_clean(digest, now)
    return found
