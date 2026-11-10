"""The service's settings, read at call time (docs/agent/specs/2026-11-04-core-and-plugins-design.md, 3.1).

Every `os.environ` read the SERVER makes goes through here (`scripts/guards/no-environ-guard.sh`
refuses one anywhere else, bar the start-up switches in serve.py and the command-line tools'
own variables). The variables are `REAM_*`; the old `ARTIFACTS_*` forms still work (envnames.py).

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
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, fields
from urllib.parse import urlsplit

from ream import envnames

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
# The command-line tools' endpoint when `REAM_API` is unset. This is OUR house's address,
# kept here so the many agents on several machines that run the tools with no variable set
# keep working (core-and-plugins design, slice 2). It moves into the house plug-in's client
# hook in slice 7, and the tools then have no default endpoint.
HOUSE_DEFAULT_API = "https://artifacts.tenfourty.site/api/v1"

# The owner the command-line tools name in their own `--help` and news when the machine has
# no `REAM_OWNER_*` variables, which is nearly every agent's machine. OUR house's, kept
# here for the same reason as HOUSE_DEFAULT_API and removed with it in slice 7 (when the
# plug-in's client hook supplies them).
HOUSE_DEFAULT_OWNER = {
    "owner_id": "jeremy",
    "owner_name": "Jeremy",
    "owner_pronoun": "he",
}

# The all-or-none groups: a half-set group is a start-up problem naming the variables.
PARTNER_OUT = (
    "REAM_PARTNER_NAME",
    "REAM_PARTNER_EVENTS_URL",
    "REAM_PARTNER_SIGNING_KEY_FILE",
    "REAM_PARTNER_KEY_ID",
)
# The values an on/off setting takes.
_SWITCH = {
    "1": True,
    "on": True,
    "true": True,
    "0": False,
    "off": False,
    "false": False,
}


def mcp_origin(url: str) -> str:
    """`url` as an origin to compare: lower-case scheme and host, the port only when it is
    not the scheme's default, no trailing slash. An `Origin` header and the setting compare
    equal through this whatever their case or an explicit default port."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        return url.strip().lower()
    if port is None or port == {"https": 443, "http": 80}.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


PARTNER_IN = ("REAM_PARTNER_INBOUND_KEYS_FILE", "REAM_PARTNER_INBOUND_HOST")

# Cove's trusted client hosts: an app from one of these is labelled Verified (MCP spec 5).
MCP_TRUSTED_DEFAULT = frozenset(
    {
        "claude.ai",
        "chatgpt.com",
        "vscode.dev",
        "zed.dev",
        "opencode.ai",
        "goose-docs.ai",
        "pi.dev",
    }
)
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_NAME = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*$")
# What `host_list` answers for an entry that is not a bare host name.
BAD_LIST = object()


def host_list(value: str):
    """A comma-separated host list setting: None when unset, an empty set for `none`, the
    lower-cased names otherwise, or `BAD_LIST` when an entry is not a bare host name (a
    scheme, a path, a port) or `none` is mixed with names."""
    value = value.strip().lower()
    if not value:
        return None
    if value == "none":
        return frozenset()
    names = [n.strip().removesuffix(".") for n in value.split(",")]
    if any(n == "none" or not _HOST_NAME.match(n) for n in names):
        return BAD_LIST
    return frozenset(names)


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
    # The gallery's PREVIOUS host name, kept answering after a move: it serves the agent API
    # as ever (agents' clients refuse redirects) and forwards every other path to the
    # gallery origin with a 308. Unset: no former host exists, every other name 404s.
    former_host: str = ""
    # Self-updating tools (clientlevel.py, selfupdate.py). The version (a tag or commit hash) a
    # tool older than the minimum level is told it is out of date, and a note of where tools
    # are installed from (advertised, never installed from: each tool has its own fixed source
    # and installs only that source's main branch). Unset: no source is advertised.
    client_install_source: str = ""
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
    browser: str = ""
    # The command-line tools' endpoint (`REAM_API`); see HOUSE_DEFAULT_API.
    api_url: str = HOUSE_DEFAULT_API
    # Served text (3.4): what the service calls itself, the line under every shipped template,
    # the word after the kind on a template's eyebrow ("DESIGN . BABA"), and the file holding
    # the notes about a proxy in front (see textvars.proxy_notes).
    product: str = "artifacts"
    footer: str = ""
    host_label: str = ""
    proxy_notes_file: str = ""
    # Which installed plug-ins to load (`ream.plugins` or `artifacts.plugins` entry points): unset = all,
    # `none` = none, `a,b` = only those (plugin_api.py).
    plugins: str = ""
    # The generic signed webhook sinks: a TOML file of `[[sink]]` tables (webhook.py).
    sinks_file: str = ""
    # MCP for people's agents (docs/agent/specs/2026-11-08-mcp-oauth-design.md, 3 and 12).
    # The origin `/mcp` is served on: the gallery origin or https://<api host>. Unset: no
    # `/mcp` anywhere (404).
    mcp_public_url: str = ""
    # Whether `/mcp` takes agent keys. Unset: yes while sign-in is off, no while it is on;
    # `0` with sign-in off refuses to start (section 7).
    mcp_accept_keys: str = ""
    # `on` turns the authorization server on: the discovery documents, open registration
    # and the sign-in challenge at `/mcp` (sections 4, 5 and 7). Unset: off.
    mcp_sign_in: str = ""
    # Hosts whose apps are labelled Verified on the consent screen (section 5). Unset:
    # MCP_TRUSTED_DEFAULT; `none`: no host is.
    mcp_trusted_client_hosts: str = ""
    # The only hosts whose apps may ask to connect, refused before anything is fetched.
    # Unset: any https host; `none`: no app at all.
    mcp_allowed_client_hosts: str = ""
    # The return hosts open registration accepts; `localhost` means loopback. Unset:
    # `localhost`; `none`: registration is off.
    mcp_self_registration_redirects: str = ""
    # Token lifetimes (section 9): an access token's seconds, a refresh token's idle days,
    # and an app's absolute cap in days. Unset: 3600, 30, 90 (the cap is the owner's to
    # decide; 90 until he does). Each must be a positive integer.
    mcp_access_token_ttl_secs: str = ""
    mcp_refresh_token_ttl_days: str = ""
    mcp_app_max_lifetime_days: str = ""
    # The upstream OpenID Connect provider the one reader signs in with (section 12; oidc.py).
    # Needed, all three, when sign-in is on. The secret is a FILE's path, never a value.
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret_file: str = ""
    # Where the server-to-server calls to the provider go (discovery, token, key set), when the
    # issuer's public address is not one Ream should reach: scheme://host[:port], nothing else.
    # Unset: those calls go to the issuer. Needs the issuer; checks against the issuer unchanged.
    oidc_backchannel_url: str = ""
    # Unset: `openid profile email groups`.
    oidc_scopes: str = ""
    # The ID-token claim that names the person (unset: `preferred_username`) and the value it
    # must have for the one reader (unset: the owner id).
    oidc_owner_claim: str = ""
    oidc_owner_value: str = ""
    # The provider's `sub` of the one reader. Unset: the first owner sign-in pins issuer+sub in
    # `service_meta` and a later different sub is refused. Set, it wins over the pin.
    oidc_owner_subject: str = ""
    # Stored on the app; unused in the core (unset: `groups`).
    oidc_groups_claim: str = ""

    def __repr__(self) -> str:
        return "Settings(...)"

    def problems(self) -> list[str]:
        """Every reason these values cannot serve, each naming the variable, never a value
        that could be a secret."""
        found = []
        if not OWNER_ID.fullmatch(self.owner_id):
            found.append(
                f"REAM_OWNER_ID must match {OWNER_ID.pattern!r}"
                " (lower-case letters, digits and hyphens, no spaces)"
            )
        if not self.owner_name.strip() or len(self.owner_name) > _NAME_MAX:
            found.append(f"REAM_OWNER_NAME must be 1 to {_NAME_MAX} characters")
        if self.owner_pronoun not in PRONOUNS:
            found.append(f"REAM_OWNER_PRONOUN must be one of {', '.join(PRONOUNS)}")
        parts = urlsplit(self.gallery_origin)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            found.append(
                "REAM_GALLERY_ORIGIN must be an origin such as https://gallery.example"
                " (scheme and host, no path)"
            )
        if not self.content_domain:
            found.append(
                "REAM_CONTENT_DOMAIN must be set: the domain pages are served under"
                " as <id>.<domain> (only a gallery on localhost has a default)"
            )
        elif "/" in self.content_domain or "@" in self.content_domain:
            found.append(
                "REAM_CONTENT_DOMAIN must be a domain such as example.com,"
                " optionally with :port, no scheme or path"
            )
        if "/" in self.api_host or "://" in self.api_host:
            found.append("REAM_API_HOST must be a host name, no scheme or path")
        if self.former_host:
            former = self.former_host.strip().lower().removesuffix(".")
            if "/" in self.former_host or "://" in self.former_host or ":" in former:
                found.append(
                    "REAM_FORMER_HOST must be a host name, no scheme, port or path"
                )
            elif former == (parts.hostname or "").lower():
                found.append(
                    "REAM_FORMER_HOST must differ from the host of REAM_GALLERY_ORIGIN"
                )
            elif former == self.api_host.strip().lower().removesuffix("."):
                found.append("REAM_FORMER_HOST must differ from REAM_API_HOST")
        if self.client_install_source and not re.fullmatch(
            r"git\+https://[A-Za-z0-9._~/:-]+", self.client_install_source
        ):
            found.append("REAM_CLIENT_INSTALL_SOURCE must be a git+https:// address")
        if self.trusted_ip_header and not _HEADER_NAME.fullmatch(
            self.trusted_ip_header
        ):
            found.append("REAM_TRUSTED_IP_HEADER must be one header name")
        if self.mailgun_key:
            missing = [
                name
                for name, value in (
                    ("REAM_MAILGUN_API", self.mailgun_api),
                    ("REAM_MAILGUN_DOMAIN", self.mailgun_domain),
                    ("REAM_MAIL_FROM", self.mail_from),
                )
                if not value
            ]
            if missing:
                found.append(
                    "mail is half-configured: REAM_MAILGUN_KEY is set, so also set "
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
        found.extend(self._mcp_problems())
        return found

    def _mcp_problems(self) -> list[str]:
        found = []
        if self.mcp_public_url:
            parts = urlsplit(self.mcp_public_url)
            try:
                port = parts.port
            except ValueError:
                port = -1
            host = (parts.hostname or "").lower()
            api = self.api_host.strip().lower().removesuffix(".")
            if (
                parts.scheme not in ("http", "https")
                or not host
                or port == -1
                or parts.path
                or parts.query
                or parts.fragment
                or parts.username is not None
            ):
                found.append(
                    "REAM_MCP_PUBLIC_URL must be an origin such as https://ream.example"
                    " (scheme and host, no path, no trailing slash)"
                )
            elif parts.scheme != "https" and not is_loopback(host):
                found.append(
                    "REAM_MCP_PUBLIC_URL must be https (plain http only on loopback)"
                )
            elif mcp_origin(self.mcp_public_url) not in {
                mcp_origin(self.gallery_origin),
                *([mcp_origin(f"https://{api}")] if api else []),
            }:
                found.append(
                    "REAM_MCP_PUBLIC_URL must be the gallery origin"
                    " (REAM_GALLERY_ORIGIN) or https://<REAM_API_HOST>"
                )
        sign_in = self.mcp_sign_in.lower()
        if sign_in and sign_in not in _SWITCH:
            found.append("REAM_MCP_SIGN_IN must be on or off")
        elif _SWITCH.get(sign_in):
            if not self.mcp_public_url:
                found.append("REAM_MCP_SIGN_IN=on needs REAM_MCP_PUBLIC_URL")
            found.extend(self._oidc_problems())
        if self.mcp_accept_keys and self.mcp_accept_keys.lower() not in _SWITCH:
            found.append("REAM_MCP_ACCEPT_KEYS must be 1 or 0")
        elif _SWITCH.get(self.mcp_accept_keys.lower()) is False and not _SWITCH.get(
            sign_in
        ):
            found.append(
                "REAM_MCP_ACCEPT_KEYS=0 needs sign-in (REAM_MCP_SIGN_IN=on): with"
                " sign-in off, agent keys are the only credential /mcp takes"
            )
        for field_name in (
            "mcp_access_token_ttl_secs",
            "mcp_refresh_token_ttl_days",
            "mcp_app_max_lifetime_days",
        ):
            raw = getattr(self, field_name)
            if raw and not (raw.isascii() and raw.isdigit() and int(raw) > 0):
                found.append(f"{_ENV[field_name]} must be a positive whole number")
        for field_name in (
            "mcp_trusted_client_hosts",
            "mcp_allowed_client_hosts",
            "mcp_self_registration_redirects",
        ):
            if host_list(getattr(self, field_name)) is BAD_LIST:
                found.append(
                    f"{_ENV[field_name]} must be host names separated by commas,"
                    " or none"
                )
        return found

    def _oidc_problems(self) -> list[str]:
        """What sign-in needs of its upstream provider (section 12). Names settings, never
        values; the secret file is checked for being readable and not empty, not shown."""
        found = []
        for field_name in ("oidc_issuer", "oidc_client_id", "oidc_client_secret_file"):
            if not getattr(self, field_name):
                found.append(f"REAM_MCP_SIGN_IN=on needs {_ENV[field_name]}")
        if self.oidc_issuer:
            parts = urlsplit(self.oidc_issuer)
            try:
                _ = parts.port
            except ValueError:
                parts = urlsplit("")
            host = (parts.hostname or "").lower()
            if (
                parts.scheme not in ("http", "https")
                or not host
                or parts.query
                or parts.fragment
                or parts.username is not None
            ):
                found.append(
                    "REAM_OIDC_ISSUER must be the provider's issuer URL (https, no query"
                    " or fragment)"
                )
            elif parts.scheme != "https" and not is_loopback(host):
                found.append(
                    "REAM_OIDC_ISSUER must be https (plain http only on loopback)"
                )
        if self.oidc_backchannel_url:
            parts = urlsplit(self.oidc_backchannel_url)
            try:
                _ = parts.port
            except ValueError:
                parts = urlsplit("")
            if not self.oidc_issuer:
                found.append("REAM_OIDC_BACKCHANNEL_URL needs REAM_OIDC_ISSUER")
            raw = self.oidc_backchannel_url
            if (
                "\\" in raw
                or any(c.isspace() for c in raw)
                or raw.endswith(":")
                or parts.scheme not in ("http", "https")
                or not parts.hostname
                or parts.path not in ("", "/")
                or parts.query
                or parts.fragment
                or parts.username is not None
                or parts.password is not None
            ):
                found.append(
                    "REAM_OIDC_BACKCHANNEL_URL must be scheme://host[:port] (http or https,"
                    " no path, query, fragment, credentials, backslash or space)"
                )
        if self.oidc_client_secret_file:
            try:
                with open(self.oidc_client_secret_file, "rb") as handle:
                    empty = not handle.read(4096).strip()
            except OSError as exc:
                found.append(
                    f"REAM_OIDC_CLIENT_SECRET_FILE cannot be read ({type(exc).__name__})"
                )
            else:
                if empty:
                    found.append("REAM_OIDC_CLIENT_SECRET_FILE is empty")
        if "openid" not in self.oidc_scope_list():
            found.append("REAM_OIDC_SCOPES must include openid")
        if self.oidc_owner_value_or_default() == "":
            found.append("REAM_OIDC_OWNER_VALUE must not be blank")
        return found

    def oidc_scope_list(self) -> list[str]:
        return (self.oidc_scopes or "openid profile email groups").split()

    def oidc_owner_claim_name(self) -> str:
        return self.oidc_owner_claim or "preferred_username"

    def oidc_owner_value_or_default(self) -> str:
        return self.oidc_owner_value or self.owner_id

    def oidc_groups_claim_name(self) -> str:
        return self.oidc_groups_claim or "groups"

    def access_token_ttl_secs(self) -> int:
        return int(self.mcp_access_token_ttl_secs or 3600)

    def refresh_token_ttl_days(self) -> int:
        return int(self.mcp_refresh_token_ttl_days or 30)

    def app_max_lifetime_days(self) -> int:
        return int(self.mcp_app_max_lifetime_days or 90)

    def mcp_signs_in(self) -> bool:
        """Whether the authorization server is on (section 7): `REAM_MCP_SIGN_IN=on`
        with `/mcp` itself on."""
        return bool(self.mcp_public_url) and bool(_SWITCH.get(self.mcp_sign_in.lower()))

    def mcp_accepts_keys(self) -> bool:
        """Whether `/mcp` takes agent keys (section 7). Unset: yes without sign-in, no
        with it; `1` takes them beside sign-in. Without sign-in, always."""
        if not self.mcp_signs_in():
            return True
        return bool(_SWITCH.get(self.mcp_accept_keys.lower()))

    def mcp_trusted_hosts(self) -> frozenset[str]:
        """The hosts labelled Verified. A malformed list (refused at start) trusts none."""
        found = host_list(self.mcp_trusted_client_hosts)
        if found is None:
            return MCP_TRUSTED_DEFAULT
        return found if isinstance(found, frozenset) else frozenset()

    def mcp_allowed_hosts(self) -> frozenset[str] | None:
        """The only hosts that may ask, or None for any. A malformed list allows none."""
        found = host_list(self.mcp_allowed_client_hosts)
        return found if found is None or isinstance(found, frozenset) else frozenset()

    def mcp_registration_hosts(self) -> frozenset[str]:
        """The return hosts open registration accepts (`localhost`: loopback); empty
        means registration is off. A malformed list allows none."""
        found = host_list(self.mcp_self_registration_redirects)
        if found is None:
            return frozenset({"localhost"})
        return found if isinstance(found, frozenset) else frozenset()


_ENV = {
    "owner_id": "REAM_OWNER_ID",
    "owner_name": "REAM_OWNER_NAME",
    "owner_pronoun": "REAM_OWNER_PRONOUN",
    "gallery_origin": "REAM_GALLERY_ORIGIN",
    "content_domain": "REAM_CONTENT_DOMAIN",
    "api_host": "REAM_API_HOST",
    "former_host": "REAM_FORMER_HOST",
    "client_install_source": "REAM_CLIENT_INSTALL_SOURCE",
    "display_tz": "REAM_DISPLAY_TZ",
    "trusted_ip_header": "REAM_TRUSTED_IP_HEADER",
    "bind_host": "REAM_BIND_HOST",
    "mailgun_key": "REAM_MAILGUN_KEY",
    "mailgun_api": "REAM_MAILGUN_API",
    "mailgun_domain": "REAM_MAILGUN_DOMAIN",
    "mail_from": "REAM_MAIL_FROM",
    "nudge_url": "REAM_NUDGE_URL",
    "nudge_agent": "REAM_NUDGE_AGENT",
    "nudge_token": "REAM_NUDGE_TOKEN",
    "nudge_timeout": "REAM_NUDGE_TIMEOUT",
    "ggshield_url": "REAM_GGSHIELD_URL",
    "ggshield_token": "REAM_GGSHIELD_TOKEN",
    "partner_name": "REAM_PARTNER_NAME",
    "partner_events_url": "REAM_PARTNER_EVENTS_URL",
    "partner_signing_key_file": "REAM_PARTNER_SIGNING_KEY_FILE",
    "partner_key_id": "REAM_PARTNER_KEY_ID",
    "partner_allow_http": "REAM_PARTNER_ALLOW_HTTP",
    "partner_inbound_keys_file": "REAM_PARTNER_INBOUND_KEYS_FILE",
    "partner_inbound_host": "REAM_PARTNER_INBOUND_HOST",
    "backup_dir": "REAM_BACKUP_DIR",
    "tools_dir": "REAM_TOOLS_DIR",
    "browser": "REAM_BROWSER",
    "api_url": "REAM_API",
    "product": "REAM_PRODUCT",
    "footer": "REAM_FOOTER",
    "host_label": "REAM_HOST_LABEL",
    "proxy_notes_file": "REAM_PROXY_NOTES_FILE",
    "plugins": "REAM_PLUGINS",
    "sinks_file": "REAM_SINKS_FILE",
    "mcp_public_url": "REAM_MCP_PUBLIC_URL",
    "mcp_accept_keys": "REAM_MCP_ACCEPT_KEYS",
    "mcp_sign_in": "REAM_MCP_SIGN_IN",
    "mcp_trusted_client_hosts": "REAM_MCP_TRUSTED_CLIENT_HOSTS",
    "mcp_allowed_client_hosts": "REAM_MCP_ALLOWED_CLIENT_HOSTS",
    "mcp_self_registration_redirects": "REAM_MCP_SELF_REGISTRATION_REDIRECTS",
    "mcp_access_token_ttl_secs": "REAM_MCP_ACCESS_TOKEN_TTL_SECS",
    "mcp_refresh_token_ttl_days": "REAM_MCP_REFRESH_TOKEN_TTL_DAYS",
    "mcp_app_max_lifetime_days": "REAM_MCP_APP_MAX_LIFETIME_DAYS",
    "oidc_issuer": "REAM_OIDC_ISSUER",
    "oidc_client_id": "REAM_OIDC_CLIENT_ID",
    "oidc_client_secret_file": "REAM_OIDC_CLIENT_SECRET_FILE",
    "oidc_backchannel_url": "REAM_OIDC_BACKCHANNEL_URL",
    "oidc_scopes": "REAM_OIDC_SCOPES",
    "oidc_owner_claim": "REAM_OIDC_OWNER_CLAIM",
    "oidc_owner_value": "REAM_OIDC_OWNER_VALUE",
    "oidc_owner_subject": "REAM_OIDC_OWNER_SUBJECT",
    "oidc_groups_claim": "REAM_OIDC_GROUPS_CLAIM",
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


def _env(field_name: str) -> str:
    """The environment's value for a setting, under either of its names; blank is unset.
    Two names set to different values refuse to start (envnames.EnvConflict)."""
    return envnames.get(_ENV[field_name].removeprefix(envnames.NEW_PREFIX)) or ""


def _lookup(env_name: str) -> str:
    """The stripped value of a variable by its ENV name, honouring `override`."""
    for field_name, name in _ENV.items():
        if name == env_name and field_name in _overrides:
            return _overrides[field_name]
    return envnames.get(env_name.removeprefix(envnames.NEW_PREFIX)) or ""


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
        raw = _env(f.name)
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
        _ENV[name] for name in _REPORTED if name not in _overrides and not _env(name)
    ]


def is_set(name: str) -> bool:
    """True when the setting `name` has been given a value (environment or `override`), as
    against taking its default."""
    return name in _overrides or bool(_env(name))


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
    origin_set = "gallery_origin" in _overrides or _env("gallery_origin")
    if not origin_set and not is_loopback(s.bind_host):
        found.append(
            f"REAM_GALLERY_ORIGIN is not set but the server binds to {s.bind_host!r}:"
            " set it to the address people open (for example https://gallery.example),"
            " or set REAM_BIND_HOST=127.0.0.1 for a local try-out"
        )
    if found:
        raise SystemExit("settings: " + "; ".join(found))
    return s


def owner_id() -> str:
    return current().owner_id


def owner_name() -> str:
    return current().owner_name
