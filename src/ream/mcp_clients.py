"""Who the app asking to connect over MCP is (docs/agent/specs/2026-11-08-mcp-oauth-design.md,
section 5). Two kinds, never confused, both public clients with no secret:

* **A client metadata document** (`kind = 'cimd'`): the `client_id` is an `https` URL with a
  path, and the document at it says the app's name and return addresses. It is fetched
  through `webhook.py`'s address guard (the address connected to is the one checked, the DNS
  answer pinned, no proxy), with no redirect followed, `application/json` only, at most
  64 KiB, in 5 s, and kept 60 s in `mcp_client`. It must name itself byte for byte.
* **A self-registered client** (`kind = 'registered'`, RFC 7591), the fallback for an app
  that cannot publish a document (Claude Code's local sign-in): `reg_` plus random, return
  hosts limited by `REAM_MCP_SELF_REGISTRATION_REDIRECTS`, 10 an hour from one address (an
  IPv6 one counted by its /64), 1000 stored, and one never used is gone after 24 hours.

An app is shown by its CHECKED address (`host`): the host of its document's URL, or for a
registered app its return address's host (`localhost` for loopback), never by the name it
gives itself. `REAM_MCP_ALLOWED_CLIENT_HOSTS` refuses any other host before anything is
fetched or stored; `REAM_MCP_TRUSTED_CLIENT_HOSTS` only labels an app Verified.

`_get` is the one fetch: tests replace it with a stub serving fake documents.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from ream import db as dbmod
from ream import settings, webhook
from ream.db import Database

MAX_DOCUMENT = 64 * 1024
FETCH_SECONDS = 5.0
CACHE_FOR = timedelta(seconds=60)
MAX_URL = 2048
MAX_NAME = 200
MAX_REDIRECTS = 10

REGISTERED_PREFIX = "reg_"
REGISTRATIONS_PER_HOUR = 10
REGISTRATIONS_STORED = 1000
UNUSED_REGISTRATION_LASTS = timedelta(hours=24)

# The loopback return addresses (RFC 8252): `http` on these, any port but 0 or an explicit 80.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# What a registered app with a loopback return address counts as in the host lists.
LOOPBACK_NAME = "localhost"


class FetchBusy(Exception):
    """Too many document fetches are in flight (or one is, for this host): try again shortly."""


class ClientRefused(Exception):
    """This app may not ask: the words say why, for the person, and never quote what a
    fetched document said."""


class RegistrationRefused(Exception):
    """An RFC 7591 error answer: the status, the `error` code and its description."""

    def __init__(
        self, status: int, error: str, description: str, retry_after: int | None = None
    ) -> None:
        super().__init__(description)
        self.status, self.error, self.description = status, error, description
        self.retry_after = retry_after

    def body(self) -> dict[str, str]:
        return {"error": self.error, "error_description": self.description}


@dataclass(frozen=True)
class Client:
    id: str
    kind: str  # 'cimd' | 'registered'
    host: str  # the checked address it is shown by
    name: str  # its own claim, shown second
    redirect_uris: tuple[str, ...]

    @property
    def verified(self) -> bool:
        return self.host in settings.current().mcp_trusted_hosts()


@dataclass(frozen=True)
class Fetched:
    status: int
    content_type: str
    body: bytes  # at most MAX_DOCUMENT + 1 bytes: one more says it was too large


# --- Addresses -----------------------------------------------------------------------------


def _port(parts) -> int | None:
    """The explicit port, None when there is none; ValueError when it is not a port."""
    return parts.port


def is_loopback_redirect(uri: str) -> bool:
    """`http` on localhost, 127.0.0.1 or [::1], with no port or any port but 0 and 80."""
    parts = urlsplit(uri)
    if parts.scheme != "http" or parts.username is not None or parts.fragment:
        return False
    if (parts.hostname or "") not in LOOPBACK_HOSTS:
        return False
    try:
        port = _port(parts)
    except ValueError:
        return False
    return port not in (0, 80)


def _https_host(uri: str) -> str | None:
    """The host of an `https` return address with no user part or fragment, else None."""
    parts = urlsplit(uri)
    if parts.scheme != "https" or parts.username is not None or parts.fragment:
        return None
    try:
        _port(parts)
    except ValueError:
        return None
    return parts.hostname or None


def redirect_matches(registered: tuple[str, ...] | list[str], asked: str) -> bool:
    """Whether `asked` is one of an app's return addresses: exactly, or for a loopback one
    on everything but the port (RFC 8252)."""
    if asked in registered:
        return True
    if not is_loopback_redirect(asked):
        return False
    a = urlsplit(asked)
    for uri in registered:
        if not is_loopback_redirect(uri):
            continue
        r = urlsplit(uri)
        if (r.scheme, r.hostname, r.path, r.query) == (
            a.scheme,
            a.hostname,
            a.path,
            a.query,
        ):
            return True
    return False


def is_document_id(client_id: str) -> bool:
    """A `client_id` that names a client metadata document: an `https` URL with a path
    (not just `/`), no user part, no fragment and no `.` or `..` segment."""
    if not isinstance(client_id, str) or len(client_id) > MAX_URL:
        return False
    parts = urlsplit(client_id)
    if parts.scheme != "https" or not parts.hostname or parts.username is not None:
        return False
    if parts.fragment or "#" in client_id:
        return False
    try:
        _port(parts)
    except ValueError:
        return False
    segments = parts.path.split("/")[1:]
    return (
        bool(segments)
        and segments != [""]
        and all(s not in (".", "..") for s in segments)
    )


def may_ask(host: str) -> bool:
    """Whether an app shown by `host` may ask at all (`REAM_MCP_ALLOWED_CLIENT_HOSTS`)."""
    allowed = settings.current().mcp_allowed_hosts()
    return allowed is None or host in allowed


def _refuse_host(host: str) -> None:
    if not may_ask(host):
        raise ClientRefused(f"this server does not accept apps from {host}")


# --- Client metadata documents --------------------------------------------------------------


def _get(url: str) -> Fetched:
    """The one fetch: GET through the address guard. No redirect is followed (a 3xx comes
    back as its status), at most MAX_DOCUMENT + 1 bytes are read, all in FETCH_SECONDS."""

    def read(response: http.client.HTTPResponse) -> Fetched:
        return Fetched(
            response.status,
            response.getheader("content-type") or "",
            response.read(MAX_DOCUMENT + 1),
        )

    try:
        return webhook.fenced_request(
            url,
            "GET",
            None,
            {"Accept": "application/json", "User-Agent": "ream-mcp/1"},
            allow_private=False,
            deadline_seconds=FETCH_SECONDS,
            timeout=FETCH_SECONDS,
            read=read,
        )
    except webhook.WebhookError as exc:
        raise ClientRefused(
            f"its client metadata document could not be fetched ({exc})"
        ) from None


MAX_FETCHES = 4  # in flight at once, and at most one per client_id
_fetch_lock = threading.Lock()
_fetching: set[str] = set()


def _document(url: str) -> Client:
    """Fetch and check the document at `url`; a Client, or ClientRefused. Takes a fetch slot
    (FetchBusy when none is free or the client_id already has a fetch in flight)."""
    with _fetch_lock:
        if url in _fetching or len(_fetching) >= MAX_FETCHES:
            raise FetchBusy(url)
        _fetching.add(url)
    try:
        got = _get(url)
    finally:
        with _fetch_lock:
            _fetching.discard(url)
    if 300 <= got.status < 400:
        raise ClientRefused(
            f"its client metadata document answered HTTP {got.status}:"
            " redirects are not followed"
        )
    if got.status != 200:
        raise ClientRefused(f"its client metadata document answered HTTP {got.status}")
    media = got.content_type.split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise ClientRefused("its client metadata document is not application/json")
    if len(got.body) > MAX_DOCUMENT:
        raise ClientRefused("its client metadata document is larger than 64 KiB")
    try:
        doc = json.loads(got.body)
    except (ValueError, UnicodeDecodeError):
        raise ClientRefused("its client metadata document is not JSON") from None
    if not isinstance(doc, dict):
        raise ClientRefused("its client metadata document is not a JSON object")
    if doc.get("client_id") != url:
        raise ClientRefused(
            "its client metadata document names a different client_id than its address"
        )
    name = doc.get("client_name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME:
        raise ClientRefused("its client metadata document has no usable client_name")
    method = doc.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise ClientRefused(
            "its client metadata document asks for a client secret; this server signs"
            " in public clients only"
        )
    host = urlsplit(url).hostname or ""
    uris = _redirect_list(doc.get("redirect_uris"))
    if uris is None:
        raise ClientRefused("its client metadata document has no usable redirect_uris")
    for uri in uris:
        if not is_loopback_redirect(uri) and _https_host(uri) != host:
            raise ClientRefused(
                "its client metadata document lists a return address that is neither"
                f" https on {host} nor loopback"
            )
    return Client(url, "cimd", host, name.strip(), tuple(uris))


def _redirect_list(value: Any) -> list[str] | None:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_REDIRECTS
        or not all(isinstance(u, str) and 0 < len(u) <= MAX_URL for u in value)
    ):
        return None
    return list(value)


def client_for(db: Database, client_id: str, now: datetime | None = None) -> Client:
    """The app `client_id` names, checked, or ClientRefused. A document is refused by the
    allowed list before it is fetched, and fetched again once its copy is 60 s old."""
    now = now or datetime.now(UTC)
    if isinstance(client_id, str) and client_id.startswith(REGISTERED_PREFIX):
        return _registered(db, client_id, now)
    if not is_document_id(client_id):
        raise ClientRefused(
            "its client_id is neither a document address nor registered"
        )
    host = urlsplit(client_id).hostname or ""
    _refuse_host(host)
    with db.connect() as conn:
        row = conn.execute(
            "select host, name, redirect_uris, fetched_at from mcp_client"
            " where id = %s and kind = 'cimd'",
            (client_id,),
        ).fetchone()
    if row is not None and row[3] is not None and now - row[3] < CACHE_FOR:
        return Client(client_id, "cimd", row[0], row[1], tuple(row[2]))
    client = _document(client_id)
    with db.connect() as conn:
        conn.execute(
            "insert into mcp_client (id, kind, host, name, redirect_uris, created_at,"
            " fetched_at) values (%s, 'cimd', %s, %s, %s, %s, %s)"
            " on conflict (id) do update set host = excluded.host,"
            " name = excluded.name, redirect_uris = excluded.redirect_uris,"
            " fetched_at = excluded.fetched_at",
            (
                client.id,
                client.host,
                client.name,
                dbmod.jsonb(list(client.redirect_uris)),
                now,
                now,
            ),
        )
    return client


def recorded(db: Database, client_id: str) -> Client:
    """The client as `client_for` last stored it, with no network fetch and no freshness
    check: for the steps after /oauth/authorize, which checked it. ClientRefused when it is
    gone, or its host is no longer allowed."""
    now = datetime.now(UTC)
    if isinstance(client_id, str) and client_id.startswith(REGISTERED_PREFIX):
        return _registered(db, client_id, now)
    with db.connect() as conn:
        row = conn.execute(
            "select host, name, redirect_uris from mcp_client"
            " where id = %s and kind = 'cimd'",
            (client_id,),
        ).fetchone()
    if row is None:
        raise ClientRefused("this app is not known here any more")
    _refuse_host(row[0])
    return Client(client_id, "cimd", row[0], row[1], tuple(row[2]))


def _registered(db: Database, client_id: str, now: datetime) -> Client:
    with db.connect() as conn:
        row = conn.execute(
            "select host, name, redirect_uris, created_at, last_used_at from mcp_client"
            " where id = %s and kind = 'registered'",
            (client_id,),
        ).fetchone()
    if row is None or (row[4] is None and now - row[3] >= UNUSED_REGISTRATION_LASTS):
        raise ClientRefused("its client_id is not registered here, or has lapsed")
    _refuse_host(row[0])
    return Client(client_id, "registered", row[0], row[1], tuple(row[2]))


# --- Open registration (RFC 7591) -----------------------------------------------------------


def counted_as(address: str) -> str:
    """The address a registration counts against: an IPv6 address as its /64."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def _invalid(error: str, description: str) -> RegistrationRefused:
    return RegistrationRefused(400, error, description)


def _registration_host(uris: list[str], hosts: frozenset[str]) -> str:
    """The one host a registration's return addresses share, each allowed by
    `REAM_MCP_SELF_REGISTRATION_REDIRECTS`; RegistrationRefused otherwise."""
    shown = set()
    for uri in uris:
        if is_loopback_redirect(uri):
            host = LOOPBACK_NAME
        else:
            host = _https_host(uri)
            if host is None:
                raise _invalid(
                    "invalid_redirect_uri",
                    "every redirect_uri must be https, or http on a loopback address"
                    " (localhost, 127.0.0.1, [::1]) without port 0 or 80",
                )
        if host not in hosts:
            raise _invalid(
                "invalid_redirect_uri",
                "this server registers apps returning only to: "
                + ", ".join(sorted(hosts)),
            )
        shown.add(host)
    if len(shown) != 1:
        raise _invalid(
            "invalid_redirect_uri", "every redirect_uri must be on the same host"
        )
    return shown.pop()


def register(
    db: Database, metadata: Any, address: str, now: datetime | None = None
) -> dict[str, Any]:
    """Register a public client from RFC 7591 `metadata` sent from `address`; the 201
    answer's body, or RegistrationRefused (400, 429 over the hourly limit, 503 when the
    store is full). Registration must be on (`mcp_registration_hosts()` not empty)."""
    now = now or datetime.now(UTC)
    hosts = settings.current().mcp_registration_hosts()
    if not hosts:
        raise RegistrationRefused(404, "invalid_request", "registration is off")
    if not isinstance(metadata, dict):
        raise _invalid("invalid_client_metadata", "the body must be a JSON object")
    uris = _redirect_list(metadata.get("redirect_uris"))
    if uris is None:
        raise _invalid(
            "invalid_redirect_uri",
            f"redirect_uris must be a list of 1 to {MAX_REDIRECTS} addresses",
        )
    method = metadata.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise _invalid(
            "invalid_client_metadata",
            'token_endpoint_auth_method must be "none": public clients only',
        )
    grants = metadata.get("grant_types", ["authorization_code", "refresh_token"])
    if (
        not isinstance(grants, list)
        or not grants
        or not set(grants) <= {"authorization_code", "refresh_token"}
        or "authorization_code" not in grants
    ):
        raise _invalid(
            "invalid_client_metadata",
            "grant_types may be only authorization_code and refresh_token",
        )
    responses = metadata.get("response_types", ["code"])
    if responses != ["code"]:
        raise _invalid("invalid_client_metadata", 'response_types must be ["code"]')
    name = metadata.get("client_name", "")
    if not isinstance(name, str) or len(name) > MAX_NAME:
        raise _invalid(
            "invalid_client_metadata",
            f"client_name must be text of at most {MAX_NAME} characters",
        )
    host = _registration_host(uris, hosts)
    if not may_ask(host):
        raise _invalid(
            "invalid_redirect_uri", f"this server does not accept apps from {host}"
        )
    source = counted_as(address)
    client_id = REGISTERED_PREFIX + secrets.token_urlsafe(24)
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "delete from mcp_client where kind = 'registered'"
            " and last_used_at is null and created_at < %s",
            (now - UNUSED_REGISTRATION_LASTS,),
        )
        recent = conn.execute(
            "select created_at from mcp_client where kind = 'registered'"
            " and registered_from = %s and created_at > %s order by created_at",
            (source, now - timedelta(hours=1)),
        ).fetchall()
        if len(recent) >= REGISTRATIONS_PER_HOUR:
            wait = recent[0][0] + timedelta(hours=1) - now
            raise RegistrationRefused(
                429,
                "invalid_request",
                f"too many registrations from this address: at most"
                f" {REGISTRATIONS_PER_HOUR} an hour",
                retry_after=max(1, int(wait.total_seconds()) + 1),
            )
        stored = conn.execute(
            "select count(*) from mcp_client where kind = 'registered'"
        ).fetchone()[0]
        if stored >= REGISTRATIONS_STORED:
            raise RegistrationRefused(
                503,
                "temporarily_unavailable",
                "this server holds as many registered apps as it keeps; try later",
                retry_after=3600,
            )
        conn.execute(
            "insert into mcp_client (id, kind, host, name, redirect_uris, created_at,"
            " registered_from) values (%s, 'registered', %s, %s, %s, %s, %s)",
            (client_id, host, name, dbmod.jsonb(uris), now, source),
        )
    answer: dict[str, Any] = {
        "client_id": client_id,
        "client_id_issued_at": int(now.timestamp()),
        "redirect_uris": uris,
        "grant_types": grants,
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if name:
        answer["client_name"] = name
    return answer
