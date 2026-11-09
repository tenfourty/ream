"""The OpenID Connect client the owner signs in with (docs/agent/specs/2026-11-08-mcp-oauth-design.md,
sections 12 and 13). Ream is the relying party of one upstream provider (at home, Authelia):
authorization-code flow with PKCE (S256), `state` and `nonce`, the client secret read from a file
each time it is needed (`REAM_OIDC_CLIENT_SECRET_FILE`; never an environment value, never logged,
never in an error), and the ID token checked before anyone is believed: signature (RS256 or ES256,
by `kid` from the provider's JWKS; `none` and the HMAC family are refused), `iss`, `aud` (and `azp`
when the audience lists more than one), `exp`, `iat` (a small skew either way) and `nonce`.

One reader: only the person whose `REAM_OIDC_OWNER_CLAIM` equals `REAM_OIDC_OWNER_VALUE` signs in;
anyone else gets `NotOwner` and nothing is stored (this module stores nothing; the caches below are
memory only and hold the provider's public documents).

The caller (oauth.py, slice 5b) keeps `state`, `nonce` and the verifier between `begin` and
`finish`. `fetch` is the one network seam: tests replace it with a fake provider.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from ream import settings, webhook

FETCH_SECONDS = 5.0
MAX_DOCUMENT = 256 * 1024
SKEW_SECONDS = 60
DISCOVERY_CACHE_SECONDS = 300
ALGORITHMS = ("RS256", "ES256")


class SignInRefused(Exception):
    """The provider's answer cannot be trusted or the sign-in cannot continue. The words are
    safe to show the person; they never quote what the provider sent."""


class NotOwner(SignInRefused):
    """A valid login by someone other than the one reader (section 13)."""

    def __init__(self) -> None:
        super().__init__(
            f"Only {settings.current().owner_name} can sign in to this Ream"
        )


@dataclass(frozen=True)
class Response:
    status: int
    content_type: str
    body: bytes


@dataclass(frozen=True)
class Start:
    """What `begin` hands the caller: send the person to `url`, keep the rest."""

    url: str
    state: str
    nonce: str
    verifier: str


@dataclass(frozen=True)
class Login:
    """A checked login of the owner. `subject` is the provider's `sub`; `groups` is the
    groups claim as the provider sent it, for the app to store."""

    person: str
    subject: str
    groups: tuple[str, ...]


# --- The seam -----------------------------------------------------------------------------


def fetch(
    method: str, url: str, body: bytes | None, headers: dict[str, str]
) -> Response:
    """The one network call: through the address guard, no redirect, no proxy, at most
    MAX_DOCUMENT + 1 bytes, FETCH_SECONDS. The provider is the operator's choice, so a private
    address (a home provider) is allowed. Tests replace this."""

    def read(response: http.client.HTTPResponse) -> Response:
        return Response(
            response.status,
            response.getheader("content-type") or "",
            response.read(MAX_DOCUMENT + 1),
        )

    try:
        return webhook.fenced_request(
            url,
            method,
            body,
            {"User-Agent": "ream-oidc/1", **headers},
            allow_private=True,
            deadline_seconds=FETCH_SECONDS,
            timeout=FETCH_SECONDS,
            read=read,
        )
    except webhook.WebhookError as exc:
        raise SignInRefused(
            f"the sign-in provider could not be reached ({exc})"
        ) from None


def _now() -> float:
    return time.time()


# --- Configuration ------------------------------------------------------------------------


def _secret() -> str:
    """The client secret, read from its file now. Errors name the setting only."""
    path = settings.current().oidc_client_secret_file
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read(4096).strip()
    except (OSError, ValueError) as exc:
        raise SignInRefused(
            f"REAM_OIDC_CLIENT_SECRET_FILE cannot be read ({type(exc).__name__})"
        ) from None
    if not value:
        raise SignInRefused("REAM_OIDC_CLIENT_SECRET_FILE is empty")
    return value


def _json(got: Response, what: str):
    if got.status != 200:
        raise SignInRefused(f"the sign-in provider's {what} answered HTTP {got.status}")
    if len(got.body) > MAX_DOCUMENT:
        raise SignInRefused(f"the sign-in provider's {what} is too large")
    try:
        doc = json.loads(got.body)
    except (ValueError, UnicodeDecodeError):
        raise SignInRefused(f"the sign-in provider's {what} is not JSON") from None
    if not isinstance(doc, dict):
        raise SignInRefused(f"the sign-in provider's {what} is not a JSON object")
    return doc


_cache: dict[str, tuple[float, dict]] = {}


def _cached(url: str, what: str, force: bool = False) -> dict:
    hit = _cache.get(url)
    if hit and not force and _now() - hit[0] < DISCOVERY_CACHE_SECONDS:
        return hit[1]
    doc = _json(fetch("GET", url, None, {"Accept": "application/json"}), what)
    _cache[url] = (_now(), doc)
    return doc


def clear_caches() -> None:
    _cache.clear()


def _endpoint(doc: dict, key: str) -> str:
    value = doc.get(key)
    if not isinstance(value, str):
        raise SignInRefused(f"the sign-in provider's discovery document has no {key}")
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if (
        parts.fragment
        or not host
        or (
            parts.scheme != "https"
            and not (parts.scheme == "http" and settings.is_loopback(host))
        )
    ):
        raise SignInRefused(f"the sign-in provider's {key} is not an https address")
    return value


def discovery() -> dict:
    """The provider's discovery document, its `issuer` checked against the setting."""
    issuer = settings.current().oidc_issuer
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    doc = _cached(url, "discovery document")
    if doc.get("issuer") != issuer:
        raise SignInRefused(
            "the sign-in provider's discovery document names another issuer"
        )
    return doc


# --- Step one: send the person ------------------------------------------------------------


def challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def begin(redirect_uri: str) -> Start:
    """The address to send the person to, with fresh `state`, `nonce` and PKCE verifier."""
    cfg = settings.current()
    endpoint = _endpoint(discovery(), "authorization_endpoint")
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": cfg.oidc_client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(cfg.oidc_scope_list()),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    sep = "&" if "?" in endpoint else "?"
    return Start(endpoint + sep + query, state, nonce, verifier)


# --- Step two: the person is back ---------------------------------------------------------


def finish(code: str, verifier: str, nonce: str, redirect_uri: str) -> Login:
    """Exchange `code`, check the ID token and the single reader. Raises SignInRefused or
    NotOwner; returns a Login for the owner."""
    cfg = settings.current()
    endpoint = _endpoint(discovery(), "token_endpoint")
    basic = base64.b64encode(
        f"{_form_quote(cfg.oidc_client_id)}:{_form_quote(_secret())}".encode()
    ).decode("ascii")
    body = urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
    ).encode("ascii")
    got = fetch(
        "POST",
        endpoint,
        body,
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Authorization": "Basic " + basic,
        },
    )
    answer = _json(got, "token endpoint")
    token = answer.get("id_token")
    if not isinstance(token, str) or not token:
        raise SignInRefused("the sign-in provider sent no ID token")
    claims = verify_id_token(token, nonce)
    return owner_login(claims)


def _form_quote(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value)


# --- The ID token -------------------------------------------------------------------------


def _b64(part: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (ValueError, TypeError):
        raise SignInRefused("the ID token is malformed") from None


def _load_json(raw: bytes) -> dict:
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise SignInRefused("the ID token is malformed") from None
    if not isinstance(doc, dict):
        raise SignInRefused("the ID token is malformed")
    return doc


def _public_key(jwk: dict, alg: str):
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    def num(name: str) -> int:
        value = jwk.get(name)
        if not isinstance(value, str):
            raise TypeError(name)
        return int.from_bytes(_b64(value), "big")

    try:
        if alg == "RS256" and jwk.get("kty") == "RSA":
            return rsa.RSAPublicNumbers(num("e"), num("n")).public_key()
        if alg == "ES256" and jwk.get("kty") == "EC" and jwk.get("crv") == "P-256":
            return ec.EllipticCurvePublicNumbers(
                num("x"), num("y"), ec.SECP256R1()
            ).public_key()
    except (ValueError, TypeError, SignInRefused):
        pass
    raise SignInRefused("the ID token's signing key is not usable")


def _find_jwk(kid: str | None, alg: str) -> dict:
    url = _endpoint(discovery(), "jwks_uri")
    for force in (False, True):  # a key rotated since the cache: one refresh
        keys = _cached(url, "key set", force=force).get("keys")
        if isinstance(keys, list):
            fits = [
                k
                for k in keys
                if isinstance(k, dict)
                and k.get("use", "sig") == "sig"
                and (kid is None or k.get("kid") == kid)
                and k.get("kty") == ("RSA" if alg == "RS256" else "EC")
            ]
            if len(fits) == 1 or (kid is not None and fits):
                return fits[0]
    raise SignInRefused(
        "the ID token is signed with a key the provider does not publish"
    )


def _verify_signature(alg: str, jwk: dict, signed: bytes, signature: bytes) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    key = _public_key(jwk, alg)
    try:
        if alg == "RS256":
            key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            if len(signature) != 64:
                raise InvalidSignature
            r = int.from_bytes(signature[:32], "big")
            s = int.from_bytes(signature[32:], "big")
            key.verify(encode_dss_signature(r, s), signed, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise SignInRefused("the ID token's signature is not valid") from None


def verify_id_token(token: str, nonce: str) -> dict:
    """The claims of `token` once its signature and claims are checked against the
    configuration, or SignInRefused."""
    cfg = settings.current()
    parts = token.split(".")
    if len(parts) != 3:
        raise SignInRefused("the ID token is malformed")
    header = _load_json(_b64(parts[0]))
    alg = header.get("alg")
    if alg not in ALGORITHMS:
        raise SignInRefused(
            "the ID token is signed with an algorithm this server refuses"
        )
    kid = header.get("kid")
    if kid is not None and not isinstance(kid, str):
        raise SignInRefused("the ID token is malformed")
    _verify_signature(
        alg,
        _find_jwk(kid, alg),
        (parts[0] + "." + parts[1]).encode("ascii"),
        _b64(parts[2]),
    )
    claims = _load_json(_b64(parts[1]))
    if claims.get("iss") != cfg.oidc_issuer:
        raise SignInRefused("the ID token comes from another issuer")
    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else aud
    if not isinstance(audiences, list) or cfg.oidc_client_id not in audiences:
        raise SignInRefused("the ID token is for another client")
    if len(audiences) > 1 and claims.get("azp") != cfg.oidc_client_id:
        raise SignInRefused("the ID token is for another client")
    now = _now()
    exp, iat = claims.get("exp"), claims.get("iat")
    if not _number(exp) or exp <= now - SKEW_SECONDS:
        raise SignInRefused("the ID token has expired")
    if not _number(iat) or iat > now + SKEW_SECONDS:
        raise SignInRefused("the ID token was issued in the future")
    sent = claims.get("nonce")
    if (
        not isinstance(sent, str)
        or not nonce
        or not hmac.compare_digest(sent.encode(), nonce.encode())
    ):
        raise SignInRefused("the ID token answers a different sign-in")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise SignInRefused("the ID token names no subject")
    return claims


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --- The one reader -----------------------------------------------------------------------


def owner_login(claims: dict) -> Login:
    """The Login for `claims` if they name the owner (section 13), else NotOwner. The claim
    is a string, or a list that holds the value."""
    cfg = settings.current()
    got = claims.get(cfg.oidc_owner_claim_name())
    want = cfg.oidc_owner_value_or_default()
    held = got if isinstance(got, list) else [got]
    if not want or not any(
        isinstance(v, str) and hmac.compare_digest(v.encode(), want.encode())
        for v in held
    ):
        raise NotOwner()
    groups = claims.get(cfg.oidc_groups_claim_name())
    return Login(
        cfg.owner_id,
        claims["sub"],
        tuple(g for g in groups if isinstance(g, str))
        if isinstance(groups, list)
        else (),
    )
