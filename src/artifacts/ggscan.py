"""The second opinion on every page: GitGuardian's ggshield, via baba.

homelab-ij8.2. Jeremy's ruling, 24 Sep 2026: every publish, update and preview
is also checked by ggshield; a finding refuses the page exactly as the built-in
scanner does; and when ggshield cannot be reached the page publishes on the
built-in scan alone and that is logged. (The bead first asked for fail-closed;
the ruling replaced it, with the service already public on the built-in scan.)

WHY A RELAY ON BABA AND NOT GGSHIELD IN THE IMAGE: baba already has ggshield
installed and authenticated for the commit guards, so the service needs no
GitGuardian key of its own, and the image carries no credential for an outside
service. The relay (homelab `scripts/artifacts-ggshield-relay.py`) hands back
the kind of secret and its line, never the value.

WHY THE ORIGINAL BODY: the bead's critical design point. The built-in scanner
detags, unescapes and defangs before it matches; handing ggshield those views
would give it the same blind spots, and a second opinion with the first one's
blind spots is not a second opinion. So this sends `body` exactly as the agent
submitted it.

Never raises. `None` means "no second opinion was had" -- not configured,
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

from artifacts import settings
from artifacts.scan import Finding

URL_ENV = "ARTIFACTS_GGSHIELD_URL"
TOKEN_ENV = "ARTIFACTS_GGSHIELD_TOKEN"  # noqa: S105 -- the variable NAME, not a value
# Inside a publish, and under the public ingress's 90 s read timeout with room
# to spare. A real scan of a 40 KB page takes about two seconds.
TIMEOUT_SECONDS = 20.0
# Reaching baba at all gets far less. A firewall that DROPS rather than refuses
# (baba down, a rule missing) would otherwise hold every publish for the whole
# scan timeout before it fell back to the built-in scan.
CONNECT_SECONDS = 3.0
MAX_FINDINGS = 20

log = logging.getLogger("artifacts.ggscan")
_said_unconfigured: list[bool] = []

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


def configured() -> bool:
    return bool(settings.current().ggshield_url and settings.current().ggshield_token)


def _shape(kind: str) -> str:
    """'Slack Bot Token' -> 'ggshield_slack_bot_token': the same slug shape the
    built-in scanner's findings carry, prefixed so a reader knows which of the
    two scanners refused the page."""
    slug = re.sub(r"[^a-z0-9]+", "_", kind.lower()).strip("_")[:60]
    return f"ggshield_{slug or 'secret'}"


class _Refused(Exception):
    def __init__(self, status: int):
        self.status = status


def _post(url: str, token: str, body: str) -> object:
    """One POST, with a short connect and a long read."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("ARTIFACTS_GGSHIELD_URL is not an http(s) URL")
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
            body=json.dumps({"body": body}, ensure_ascii=False).encode(),
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
