"""The generic signed webhook sink (core-and-plugins design, 4.3).

A deployment names sinks in a TOML file (`REAM_SINKS_FILE`, also read as `ARTIFACTS_SINKS_FILE`):

    [[sink]]
    name = "chase"                         # lower-case letters, digits, hyphens
    kinds = ["answer_dropped"]             # any of events.KINDS and events.BRIDGE_KINDS
    url = "https://hooks.example/in"
    signing_key_file = "chase.pem"         # Ed25519, PEM PKCS8, unencrypted; relative to this file
    key_id = "k1"                          # the receiver picks its public key from this
    label = "Chase"                        # optional: the word for the sink in the gallery
    allow_http = false                     # optional: plain http:// only when true
    allow_private = false                  # optional: private, loopback and link-local addresses

Each sink is an outbox sink (at-least-once, backoff, parking, the circuit breaker: sinks.py), so
a down receiver costs retries, never an event. The request is a POST of one JSON document, the
generic envelope, with an Ed25519 signature over its exact bytes:

    {"key_id": "k1", "sent_at": "<ISO 8601 UTC>", "event": <Event.as_json()>}

    Content-Type: application/json
    X-Ream-Signature: <base64 of the signature>
    X-Ream-Key-Id: k1
    X-Ream-Event-Id: <event id: the receiver's idempotency key>

Every attempt is re-signed with a fresh `sent_at`, so a receiver can refuse a stale body.

THE URL SURFACE. The URLs come from the operator's file, never from an event, and an event
cannot change where it goes. Still, a webhook is a request an operator-chosen name makes from
inside the deployment's network, so:

* https only (`allow_http` per sink to permit http), and no credentials in the URL;
* the connection goes only to a global address (IPv6 forms that embed an IPv4 address, NAT64,
  6to4, Teredo, IPv4-compatible and site-local, are refused whole; an IPv4-mapped one is judged
  as the IPv4 address): private (10/8, 172.16/12, 192.168/16, ULA),
  loopback, link-local (cloud metadata is 169.254.169.254), CGNAT, multicast and unspecified
  addresses are refused unless the sink says `allow_private`. The check is on the address the
  socket is actually opened to (resolved once, then connected), so a name that resolves to a
  private address, or changes its answer between a check and a connect, cannot get past it;
* redirects are never followed and any 3xx is a failure (a signed body goes only where it was
  addressed); no proxy from the environment;
* 10 s per socket operation (the sender gives a whole delivery 30 s: sinks.py); the response
  body is never read;
* the request body is at most 64 KiB; a larger event is a failure (it is parked after the usual
  attempts, never dropped), not a truncated send.

Errors are short and safe to store: a status or a class of failure. They never carry the URL,
the response, or anything from a key file.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import re
import socket
import ssl
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from ream import events, settings, sinks

SIGNATURE_HEADER = "X-Ream-Signature"
KEY_ID_HEADER = "X-Ream-Key-Id"
EVENT_ID_HEADER = "X-Ream-Event-Id"

REQUEST_TIMEOUT_SECONDS = 10.0
MAX_BODY_BYTES = 64 * 1024

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_REQUIRED = ("name", "kinds", "url", "signing_key_file", "key_id")
_OPTIONAL = ("label", "allow_http", "allow_private")
_ENV = "REAM_SINKS_FILE"


class WebhookConfigError(Exception):
    """The sinks file cannot be used. The message names the sink and the field, never a
    value that could be a URL with credentials or a key."""


class WebhookError(Exception):
    """One delivery attempt failed. Short and safe to store."""


@dataclass(frozen=True)
class Webhook:
    name: str
    kinds: tuple[str, ...]
    url: str
    key_id: str
    private_key: object = field(repr=False)  # an Ed25519PrivateKey; never printed
    label: str | None = None
    allow_http: bool = False
    allow_private: bool = False


# --- the file ---------------------------------------------------------------------


def _load_key(path: Path, where: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    try:
        pem = path.read_bytes()
    except OSError as exc:
        raise WebhookConfigError(
            f"{where}: signing_key_file cannot be read ({type(exc).__name__})"
        ) from None
    try:
        key = load_pem_private_key(pem, password=None)
    except Exception:  # noqa: BLE001 -- the library's message can quote the input
        raise WebhookConfigError(
            f"{where}: signing_key_file is not a PEM private key (PKCS8, unencrypted)"
        ) from None
    if not isinstance(key, Ed25519PrivateKey):
        raise WebhookConfigError(f"{where}: signing_key_file is not an Ed25519 key")
    return key


def _entry(raw: object, index: int, base: Path) -> Webhook:
    if not isinstance(raw, dict):
        raise WebhookConfigError(f"sink #{index + 1}: must be a table")
    name = raw.get("name")
    where = f"sink {name!r}" if isinstance(name, str) else f"sink #{index + 1}"
    unknown = sorted(set(raw) - set(_REQUIRED) - set(_OPTIONAL))
    if unknown:
        raise WebhookConfigError(f"{where}: unknown field {unknown[0][:40]!r}")
    for f in _REQUIRED:
        if f not in raw:
            raise WebhookConfigError(f"{where}: {f} is missing")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise WebhookConfigError(
            f"{where}: name must match {_NAME.pattern!r} (lower-case letters, digits, hyphens)"
        )
    kinds = raw["kinds"]
    if (
        not isinstance(kinds, list)
        or not kinds
        or not all(isinstance(k, str) for k in kinds)
    ):
        raise WebhookConfigError(f"{where}: kinds must be a non-empty list of names")
    known = (*events.KINDS, *events.BRIDGE_KINDS)
    for k in kinds:
        if k not in known:
            raise WebhookConfigError(
                f"{where}: kinds names unknown event kind {k[:60]!r}"
            )
    for f in ("allow_http", "allow_private"):
        if f in raw and not isinstance(raw[f], bool):
            raise WebhookConfigError(f"{where}: {f} must be true or false")
    label = raw.get("label")
    if label is not None and (not isinstance(label, str) or not 0 < len(label) <= 80):
        raise WebhookConfigError(f"{where}: label must be 1 to 80 characters")
    for f in ("url", "signing_key_file", "key_id"):
        if not isinstance(raw[f], str) or not raw[f].strip():
            raise WebhookConfigError(f"{where}: {f} must be a non-empty string")
    allow_http = bool(raw.get("allow_http", False))
    parts = urlsplit(raw["url"])
    try:
        parts.port  # noqa: B018 -- raises on a bad port
    except ValueError:
        raise WebhookConfigError(f"{where}: url has a bad port") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebhookConfigError(f"{where}: url must be an https:// address")
    if parts.username is not None or parts.password is not None:
        raise WebhookConfigError(
            f"{where}: url must not carry credentials (user:password@)"
        )
    if parts.scheme == "http" and not allow_http:
        raise WebhookConfigError(
            f"{where}: url must be https:// (plain http:// only with allow_http = true)"
        )
    key_path = base / raw["signing_key_file"]
    return Webhook(
        name=name,
        kinds=tuple(kinds),
        url=raw["url"],
        key_id=raw["key_id"].strip(),
        private_key=_load_key(key_path, where),
        label=label,
        allow_http=allow_http,
        allow_private=bool(raw.get("allow_private", False)),
    )


def load_file(path: str) -> list[Webhook]:
    """The sinks in the TOML file at `path`. Raises WebhookConfigError."""
    p = Path(path)
    try:
        with p.open("rb") as handle:
            doc = tomllib.load(handle)
    except OSError as exc:
        raise WebhookConfigError(
            f"{_ENV} cannot be read ({type(exc).__name__})"
        ) from None
    except tomllib.TOMLDecodeError:
        raise WebhookConfigError(f"{_ENV} is not valid TOML") from None
    stray = sorted(set(doc) - {"sink"})
    if stray:
        raise WebhookConfigError(
            f"{_ENV}: only [[sink]] tables are allowed, found other {stray[0][:40]!r}"
        )
    entries = doc.get("sink", [])
    if not isinstance(entries, list):
        raise WebhookConfigError(f"{_ENV}: sink must be written as [[sink]]")
    hooks, seen = [], set()
    for i, raw in enumerate(entries):
        hook = _entry(raw, i, p.parent)
        if hook.name in seen:
            raise WebhookConfigError(f"sink {hook.name!r}: named twice")
        seen.add(hook.name)
        hooks.append(hook)
    return hooks


def register_all() -> list[str]:
    """Read `REAM_SINKS_FILE` and register each sink. Returns their names ([] when the
    setting is unset). Raises WebhookConfigError, which stops start-up: a sink that is
    silently absent is worse than a pod that does not start."""
    path = settings.current().sinks_file
    if not path:
        return []
    names = []
    for hook in load_file(path):
        try:
            sinks.register(
                sinks.Sink(
                    hook.name,
                    hook.kinds,
                    deliver=lambda event, h=hook: deliver(h, event),
                    delivery="outbox",
                    label=hook.label,
                )
            )
        except ValueError:
            raise WebhookConfigError(
                f"sink {hook.name!r}: that name is already a registered sink"
            ) from None
        names.append(hook.name)
    return names


# --- the request ------------------------------------------------------------------


# IPv6 ranges that carry an IPv4 address (or are deprecated/unroutable): refused whole,
# without unwrapping, because a translator or tunnel on the path could turn them into a
# connection to the embedded (possibly internal) IPv4 address.
_EMBEDDING_V6 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "::/96",  # IPv4-compatible (deprecated)
        "64:ff9b::/96",  # NAT64
        "64:ff9b:1::/48",  # local-use NAT64
        "2002::/16",  # 6to4
        "2001::/32",  # Teredo
        "fec0::/10",  # site-local (deprecated)
    )
)


def address_allowed(ip: str, allow_private: bool) -> bool:
    """Whether a connection may be opened to `ip`."""
    if allow_private:
        return True
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif any(addr in net for net in _EMBEDDING_V6):
            return False
    return addr.is_global and not addr.is_multicast


def build_request(
    hook: Webhook, event: events.Event, sent_at: datetime
) -> tuple[bytes, dict[str, str]]:
    """The exact body and headers of one attempt. `sent_at` and `key_id` are inside the
    body, so the one signature covers them."""
    doc = {
        "key_id": hook.key_id,
        "sent_at": sent_at.astimezone(UTC).isoformat(),
        "event": event.as_json(),
    }
    body = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
    if len(body) > MAX_BODY_BYTES:
        raise WebhookError("event too large to send")
    signature = base64.b64encode(hook.private_key.sign(body)).decode("ascii")  # type: ignore[attr-defined]
    return body, {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature,
        KEY_ID_HEADER: hook.key_id,
        EVENT_ID_HEADER: event.id or "",
        "User-Agent": "ream-webhook/1",
    }


def _pinned_connect(allow_private: bool):
    """A `create_connection` that resolves once, refuses what the policy refuses, and
    connects to the address it checked."""

    def create(address, timeout=None, source_address=None):
        host, port = address
        try:
            found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            raise WebhookError("could not resolve host") from None
        refused = False
        last: OSError | None = None
        for family, kind, proto, _canon, sockaddr in found:
            if not address_allowed(sockaddr[0], allow_private):
                refused = True
                continue
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(timeout)
                sock.connect(sockaddr)
                return sock
            except OSError as exc:
                sock.close()
                last = exc
        if last is None and refused:
            raise WebhookError("refused address (not a public one)")
        raise WebhookError(
            "timed out" if isinstance(last, TimeoutError) else "could not connect"
        )

    return create


def http_post(hook: Webhook, body: bytes, headers: dict[str, str]) -> int:
    """POST and return the status. No redirect is followed, no proxy is used, the response
    body is not read. Raises WebhookError for a transport failure."""
    parts = urlsplit(hook.url)
    cls = (
        http.client.HTTPSConnection
        if parts.scheme == "https"
        else http.client.HTTPConnection
    )
    conn = cls(parts.hostname, parts.port, timeout=REQUEST_TIMEOUT_SECONDS)
    conn._create_connection = _pinned_connect(hook.allow_private)  # type: ignore[attr-defined]
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    try:
        conn.request("POST", target, body, headers)
        return conn.getresponse().status
    except WebhookError:
        raise
    except TimeoutError:
        raise WebhookError("timed out") from None
    except ssl.SSLError:
        raise WebhookError("TLS failed") from None
    except http.client.HTTPException:
        raise WebhookError("bad response") from None
    except OSError:
        raise WebhookError("connection failed") from None
    finally:
        conn.close()


def deliver(
    hook: Webhook,
    event: events.Event,
    post: Callable[..., int] | None = None,
) -> None:
    """One signed attempt: returns on a 2xx, raises WebhookError otherwise."""
    body, headers = build_request(hook, event, datetime.now(UTC))
    status = (post or http_post)(hook, body, headers)
    if not 200 <= status < 300:
        raise WebhookError(f"HTTP {status}")
