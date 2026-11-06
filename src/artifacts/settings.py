"""The service's settings, read at call time (docs/agent/specs/2026-11-04-core-and-plugins-design.md, 3.1).

Every `os.environ` read the SERVER makes goes through here (`scripts/guards/no-environ-guard.sh`
refuses one anywhere else, bar the start-up switches in serve.py and the command-line tools'
own variables). The variable names stay `ARTIFACTS_*`.

`current()` builds the object from the environment on every call and caches nothing, so a
test or a pod can change a value without an import-time freeze. There is no module-level
`SETTINGS`. `override(...)` layers values over the environment for one block (tests).

A blank variable is unset. Unset means NEUTRAL: nothing of the house is a default. A few
settings carry a fresh-install default (loopback gallery, owner `owner`); `defaulted()` names
the ones in use and the server logs them at start-up. A deployment that is not on loopback and
leaves `gallery_origin` unset refuses to start (`validate_for_serve`).

`owner_id` is a STORED value (`by`, `author`, `answered_by`, `actor`, folder owner), not
only a label: `owner.check` records it in `service_meta` and refuses to start on a database
written for another one.

SECRETS. Settings hold paths and ids (`..._KEY_FILE`). Three values are still read from the
environment as the VALUE, because the deployment mounts them that way (Mailgun key, nudge
token, ggshield token); they are never printed: `Settings` has no field-listing `__repr__`.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, fields
from urllib.parse import urlsplit

PRONOUNS = ("they", "he", "she")
# Validated like an agent name (`keys.AGENT_NAME`): the id is typed on command lines and
# stored where agent names live. Kept here too so this module imports nothing of the service.
OWNER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_NAME_MAX = 80
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")

DEFAULT_OWNER_ID = "owner"
DEFAULT_OWNER_NAME = "the owner"
DEFAULT_OWNER_PRONOUN = "they"
# A fresh install: one port on loopback.
DEFAULT_GALLERY_ORIGIN = "http://localhost:8000"
DEFAULT_BIND_HOST = "127.0.0.1"
# The command-line tools' endpoint when `ARTIFACTS_API` is unset. This is OUR house's address,
# kept here so the many agents on several machines that run the tools with no variable set
# keep working (core-and-plugins design, slice 2). It moves into the house plug-in's client
# hook in slice 7, and the tools then have no default endpoint.
HOUSE_DEFAULT_API = "https://artifacts.tenfourty.site/api/v1"

# The owner the command-line tools name in their own `--help` and news when the machine has
# no `ARTIFACTS_OWNER_*` variables, which is nearly every agent's machine. OUR house's, kept
# here for the same reason as HOUSE_DEFAULT_API and removed with it in slice 7 (when the
# plug-in's client hook supplies them).
HOUSE_DEFAULT_OWNER = {
    "owner_id": "jeremy",
    "owner_name": "Jeremy",
    "owner_pronoun": "he",
}

# The all-or-none groups: a half-set group is a start-up problem naming the variables.
PARTNER_OUT = (
    "ARTIFACTS_PARTNER_NAME",
    "ARTIFACTS_PARTNER_EVENTS_URL",
    "ARTIFACTS_PARTNER_SIGNING_KEY_FILE",
    "ARTIFACTS_PARTNER_KEY_ID",
)
PARTNER_IN = ("ARTIFACTS_PARTNER_INBOUND_KEYS_FILE", "ARTIFACTS_PARTNER_INBOUND_HOST")


@dataclass(frozen=True, repr=False)
class Settings:
    owner_id: str = DEFAULT_OWNER_ID
    owner_name: str = DEFAULT_OWNER_NAME
    owner_pronoun: str = DEFAULT_OWNER_PRONOUN
    # Where people open the gallery; every link the service writes starts here.
    gallery_origin: str = DEFAULT_GALLERY_ORIGIN
    # The domain page content is served under, as `<id>.<content_domain>`; may carry a port.
    # Unset: `localhost` on the gallery's port when the gallery is on localhost.
    content_domain: str = ""
    api_host: str = ""
    # Unset: the zone of the host.
    display_tz: str = ""
    # The request header the proxy sets to the caller's address. Unset: the socket address.
    trusted_ip_header: str = ""
    # What the server binds to (the image sets 0.0.0.0). Only used to tell a local try-out
    # from a deployment at start-up.
    bind_host: str = DEFAULT_BIND_HOST
    # Mail: the key, the API, the domain and the sender, all four or none.
    mailgun_key: str = ""  # a VALUE (the deployment mounts it as one); never printed
    mailgun_api: str = ""
    mailgun_domain: str = ""
    mail_from: str = ""
    nudge_url: str = ""
    nudge_agent: str = ""
    nudge_token: str = ""  # a VALUE; never printed
    nudge_timeout: str = "2.0"
    ggshield_url: str = ""
    ggshield_token: str = ""  # a VALUE; never printed
    partner_name: str = ""
    partner_events_url: str = ""
    partner_signing_key_file: str = ""
    partner_key_id: str = ""
    partner_allow_http: str = ""
    partner_inbound_keys_file: str = ""
    partner_inbound_host: str = ""
    backup_dir: str = ""
    tools_dir: str = "/app/dist"
    bd_command: str = "bd"
    browser: str = ""
    # The command-line tools' endpoint (`ARTIFACTS_API`); see HOUSE_DEFAULT_API.
    api_url: str = HOUSE_DEFAULT_API
    # Served text (3.4): what the service calls itself, the line under every shipped template,
    # the word after the kind on a template's eyebrow ("DESIGN . BABA"), and the file holding
    # the notes about a proxy in front (see textvars.proxy_notes).
    product: str = "artifacts"
    footer: str = ""
    host_label: str = ""
    proxy_notes_file: str = ""

    def __repr__(self) -> str:
        return "Settings(...)"

    def problems(self) -> list[str]:
        """Every reason these values cannot serve, each naming the variable, never a value
        that could be a secret."""
        found = []
        if not OWNER_ID.fullmatch(self.owner_id):
            found.append(
                f"ARTIFACTS_OWNER_ID must match {OWNER_ID.pattern!r}"
                " (lower-case letters, digits and hyphens, no spaces)"
            )
        if not self.owner_name.strip() or len(self.owner_name) > _NAME_MAX:
            found.append(f"ARTIFACTS_OWNER_NAME must be 1 to {_NAME_MAX} characters")
        if self.owner_pronoun not in PRONOUNS:
            found.append(
                f"ARTIFACTS_OWNER_PRONOUN must be one of {', '.join(PRONOUNS)}"
            )
        parts = urlsplit(self.gallery_origin)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            found.append(
                "ARTIFACTS_GALLERY_ORIGIN must be an origin such as https://gallery.example"
                " (scheme and host, no path)"
            )
        if not self.content_domain:
            found.append(
                "ARTIFACTS_CONTENT_DOMAIN must be set: the domain pages are served under"
                " as <id>.<domain> (only a gallery on localhost has a default)"
            )
        elif "/" in self.content_domain or "@" in self.content_domain:
            found.append(
                "ARTIFACTS_CONTENT_DOMAIN must be a domain such as example.com,"
                " optionally with :port, no scheme or path"
            )
        if "/" in self.api_host or "://" in self.api_host:
            found.append("ARTIFACTS_API_HOST must be a host name, no scheme or path")
        if self.trusted_ip_header and not _HEADER_NAME.fullmatch(
            self.trusted_ip_header
        ):
            found.append("ARTIFACTS_TRUSTED_IP_HEADER must be one header name")
        if self.mailgun_key:
            missing = [
                name
                for name, value in (
                    ("ARTIFACTS_MAILGUN_API", self.mailgun_api),
                    ("ARTIFACTS_MAILGUN_DOMAIN", self.mailgun_domain),
                    ("ARTIFACTS_MAIL_FROM", self.mail_from),
                )
                if not value
            ]
            if missing:
                found.append(
                    "mail is half-configured: ARTIFACTS_MAILGUN_KEY is set, so also set "
                    + ", ".join(missing)
                )
        out = [n for n in PARTNER_OUT if _lookup(n)]
        if out and len(out) != len(PARTNER_OUT):
            found.append(
                "the partner bridge is half-configured: set all of "
                + ", ".join(PARTNER_OUT)
                + " or none; missing "
                + ", ".join(n for n in PARTNER_OUT if n not in out)
            )
        inbound = [n for n in PARTNER_IN if _lookup(n)]
        if inbound and len(inbound) != len(PARTNER_IN):
            found.append(
                "the inbound partner bridge is half-configured: set both of "
                + ", ".join(PARTNER_IN)
                + " or neither; missing "
                + ", ".join(n for n in PARTNER_IN if n not in inbound)
            )
        if inbound and not out:
            found.append(
                "the inbound partner bridge needs the outbound one: set "
                + ", ".join(PARTNER_OUT)
            )
        return found


_ENV = {
    "owner_id": "ARTIFACTS_OWNER_ID",
    "owner_name": "ARTIFACTS_OWNER_NAME",
    "owner_pronoun": "ARTIFACTS_OWNER_PRONOUN",
    "gallery_origin": "ARTIFACTS_GALLERY_ORIGIN",
    "content_domain": "ARTIFACTS_CONTENT_DOMAIN",
    "api_host": "ARTIFACTS_API_HOST",
    "display_tz": "ARTIFACTS_DISPLAY_TZ",
    "trusted_ip_header": "ARTIFACTS_TRUSTED_IP_HEADER",
    "bind_host": "ARTIFACTS_BIND_HOST",
    "mailgun_key": "ARTIFACTS_MAILGUN_KEY",
    "mailgun_api": "ARTIFACTS_MAILGUN_API",
    "mailgun_domain": "ARTIFACTS_MAILGUN_DOMAIN",
    "mail_from": "ARTIFACTS_MAIL_FROM",
    "nudge_url": "ARTIFACTS_NUDGE_URL",
    "nudge_agent": "ARTIFACTS_NUDGE_AGENT",
    "nudge_token": "ARTIFACTS_NUDGE_TOKEN",
    "nudge_timeout": "ARTIFACTS_NUDGE_TIMEOUT",
    "ggshield_url": "ARTIFACTS_GGSHIELD_URL",
    "ggshield_token": "ARTIFACTS_GGSHIELD_TOKEN",
    "partner_name": "ARTIFACTS_PARTNER_NAME",
    "partner_events_url": "ARTIFACTS_PARTNER_EVENTS_URL",
    "partner_signing_key_file": "ARTIFACTS_PARTNER_SIGNING_KEY_FILE",
    "partner_key_id": "ARTIFACTS_PARTNER_KEY_ID",
    "partner_allow_http": "ARTIFACTS_PARTNER_ALLOW_HTTP",
    "partner_inbound_keys_file": "ARTIFACTS_PARTNER_INBOUND_KEYS_FILE",
    "partner_inbound_host": "ARTIFACTS_PARTNER_INBOUND_HOST",
    "backup_dir": "ARTIFACTS_BACKUP_DIR",
    "tools_dir": "ARTIFACTS_TOOLS_DIR",
    "bd_command": "ARTIFACTS_BD",
    "browser": "ARTIFACTS_BROWSER",
    "api_url": "ARTIFACTS_API",
    "product": "ARTIFACTS_PRODUCT",
    "footer": "ARTIFACTS_FOOTER",
    "host_label": "ARTIFACTS_HOST_LABEL",
    "proxy_notes_file": "ARTIFACTS_PROXY_NOTES_FILE",
}
if set(_ENV) != {f.name for f in fields(Settings)}:
    raise RuntimeError("settings._ENV and Settings disagree")
# Named in the start-up line when unset: the ones a fresh install defaults to something.
_REPORTED = (
    "owner_id",
    "owner_name",
    "owner_pronoun",
    "gallery_origin",
    "content_domain",
)
_overrides: dict[str, str] = {}
_lock = threading.Lock()


def _lookup(env_name: str) -> str:
    """The stripped value of a variable by its ENV name, honouring `override`."""
    for field_name, name in _ENV.items():
        if name == env_name and field_name in _overrides:
            return _overrides[field_name]
    return os.environ.get(env_name, "").strip()


def _port_of(origin: str) -> int:
    parts = urlsplit(origin)
    try:
        port = parts.port
    except ValueError:
        port = None
    return port or (443 if parts.scheme == "https" else 80)


def current() -> Settings:
    """The settings now: the environment, then any `override`. A blank variable is unset."""
    values = {}
    for f in fields(Settings):
        raw = os.environ.get(_ENV[f.name], "").strip()
        if raw:
            values[f.name] = raw
    values.update(_overrides)
    if not values.get("content_domain"):
        # A fresh install on localhost serves page content on a sibling origin of the
        # gallery, `<id>.localhost:8000`, which a browser resolves to loopback by itself.
        # Anywhere else the content domain must be set; an empty one serves no page.
        origin = values.get("gallery_origin", DEFAULT_GALLERY_ORIGIN)
        host = urlsplit(origin).hostname or ""
        values["content_domain"] = (
            f"localhost:{_port_of(origin)}"
            if host == "localhost" or host.endswith(".localhost")
            else ""
        )
    return Settings(**values)


def defaulted() -> list[str]:
    """The variables a fresh install leaves to their defaults, by env name."""
    return [
        _ENV[name]
        for name in _REPORTED
        if name not in _overrides and not os.environ.get(_ENV[name], "").strip()
    ]


def is_set(name: str) -> bool:
    """True when the setting `name` has been given a value (environment or `override`), as
    against taking its default."""
    return name in _overrides or bool(os.environ.get(_ENV[name], "").strip())


def is_loopback(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@contextlib.contextmanager
def override(**values: str) -> Iterator[Settings]:
    """Layer `values` over the environment until the block ends (tests). Process-wide,
    not per thread: a request handler on another thread sees it too."""
    unknown = set(values) - set(_ENV)
    if unknown:
        raise TypeError(f"unknown setting {sorted(unknown)}")
    with _lock:
        before = dict(_overrides)
        _overrides.update(values)
    try:
        yield current()
    finally:
        with _lock:
            _overrides.clear()
            _overrides.update(before)


def validate_for_serve() -> Settings:
    """Settings for start-up, or a `SystemExit` listing every problem at once."""
    s = current()
    found = s.problems()
    origin_set = (
        "gallery_origin" in _overrides
        or os.environ.get(_ENV["gallery_origin"], "").strip()
    )
    if not origin_set and not is_loopback(s.bind_host):
        found.append(
            f"ARTIFACTS_GALLERY_ORIGIN is not set but the server binds to {s.bind_host!r}:"
            " set it to the address people open (for example https://gallery.example),"
            " or set ARTIFACTS_BIND_HOST=127.0.0.1 for a local try-out"
        )
    if found:
        raise SystemExit("settings: " + "; ".join(found))
    return s


def owner_id() -> str:
    return current().owner_id
