"""The OpenID Connect client (docs/agent/specs/2026-11-08-mcp-oauth-design.md, 12 and 13): a fake
provider behind `oidc.fetch`, the ID-token checks, the single reader, and the start-up checks
sign-in puts on its settings."""

import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from ream import oidc, settings

ISSUER = "https://auth.example"
CLIENT = "ream"
CALLBACK = "https://artifacts.tenfourty.site/oauth/callback"
SECRET = "s3cret-value-never-shown"  # noqa: S105


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64int(n: int, size: int | None = None) -> str:
    size = size or (n.bit_length() + 7) // 8
    return b64(n.to_bytes(size, "big"))


class Provider:
    """A fake provider: serves discovery and keys, and signs the tokens a test asks for."""

    def __init__(self):
        self.rsa = rsa.generate_private_key(65537, 2048)
        self.ec = ec.generate_private_key(ec.SECP256R1())
        self.calls = []
        self.claims = {}
        self.token_status = 200
        self.id_token_override = None

    def jwks(self):
        n = self.rsa.public_key().public_numbers()
        p = self.ec.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": "r1",
                    "use": "sig",
                    "n": b64int(n.n),
                    "e": b64int(n.e),
                },
                {
                    "kty": "EC",
                    "kid": "e1",
                    "use": "sig",
                    "crv": "P-256",
                    "x": b64int(p.x, 32),
                    "y": b64int(p.y, 32),
                },
            ]
        }

    def sign(self, claims, alg="RS256", kid=None, key=None):
        header = {"alg": alg, "kid": kid or ("r1" if alg == "RS256" else "e1")}
        head = b64(json.dumps(header).encode()) + "." + b64(json.dumps(claims).encode())
        if alg == "RS256":
            sig = (key or self.rsa).sign(
                head.encode(), padding.PKCS1v15(), hashes.SHA256()
            )
        elif alg == "ES256":
            r, s = decode_dss_signature(
                (key or self.ec).sign(head.encode(), ec.ECDSA(hashes.SHA256()))
            )
            sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        else:
            sig = b"x"
        return head + "." + b64(sig)

    def good(self, nonce_, **more):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT,
            "sub": "u-1",
            "iat": now,
            "exp": now + 300,
            "nonce": nonce_,
            "preferred_username": "jeremy",
            "groups": ["admins"],
        }
        claims.update(more)
        return {k: v for k, v in claims.items() if v is not None}

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body, headers))
        doc = {
            ISSUER + "/.well-known/openid-configuration": {
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token",
                "jwks_uri": ISSUER + "/jwks",
            },
            ISSUER + "/jwks": self.jwks(),
        }.get(url)
        if url == ISSUER + "/token":
            if self.token_status != 200:
                return oidc.Response(self.token_status, "application/json", b"{}")
            token = self.id_token_override
            return oidc.Response(
                200, "application/json", json.dumps({"id_token": token}).encode()
            )
        if doc is None:
            return oidc.Response(404, "text/plain", b"")
        return oidc.Response(200, "application/json", json.dumps(doc).encode())


@pytest.fixture()
def provider(tmp_path, monkeypatch):
    secret = tmp_path / "secret"
    secret.write_text(SECRET + "\n")
    fake = Provider()
    monkeypatch.setattr(oidc, "fetch", fake)
    oidc.clear_caches()
    with settings.override(
        mcp_public_url="https://artifacts.tenfourty.site",
        mcp_sign_in="on",
        oidc_issuer=ISSUER,
        oidc_client_id=CLIENT,
        oidc_client_secret_file=str(secret),
    ):
        yield fake
    oidc.clear_caches()


def run(provider, db, claims=None, alg="RS256", **sign):
    """begin, let the fake provider answer with a token, finish."""
    start = oidc.begin(CALLBACK)
    provider.id_token_override = provider.sign(
        claims if claims is not None else provider.good(start.nonce), alg=alg, **sign
    )
    return start, oidc.finish(db, "the-code", start.verifier, start.nonce, CALLBACK)


def test_begin_sends_the_person_with_pkce_state_and_nonce(provider, db):
    start = oidc.begin(CALLBACK)
    parts = urlsplit(start.url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == ISSUER + "/authorize"
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT
    assert query["redirect_uri"] == CALLBACK
    assert query["scope"] == "openid profile email groups"
    assert query["state"] == start.state and query["nonce"] == start.nonce
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"] == oidc.challenge(start.verifier)
    assert len(start.verifier) >= 43
    assert oidc.begin(CALLBACK).state != start.state


def test_the_pkce_challenge_matches_rfc_7636():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert oidc.challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


@pytest.mark.parametrize("alg", ["RS256", "ES256"])
def test_the_owner_signs_in(provider, db, alg):
    start, login = run(provider, db, alg=alg)
    assert login.person == settings.owner_id()
    assert login.subject == "u-1"
    assert login.groups == ("admins",)
    method, url, body, headers = provider.calls[-1]
    assert (method, url) == ("POST", ISSUER + "/token")
    form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "the-code"
    assert form["code_verifier"] == start.verifier
    assert form["redirect_uri"] == CALLBACK
    assert SECRET not in body.decode()
    sent = base64.b64decode(headers["Authorization"].removeprefix("Basic ")).decode()
    assert sent == f"{CLIENT}:{SECRET}"


def refused(provider, db, text, **kw):
    with pytest.raises(oidc.SignInRefused) as caught:
        run(provider, db, **kw)
    assert text in str(caught.value)
    assert SECRET not in str(caught.value)
    return caught.value


def test_a_wrong_nonce_is_refused(provider, db):
    refused(provider, "different sign-in", claims=provider.good("someone-elses"))


def test_a_missing_nonce_is_refused(provider, db):
    refused(provider, "different sign-in", claims=provider.good(None, nonce=None))


def test_a_wrong_audience_is_refused(provider, db):
    start = oidc.begin(CALLBACK)
    claims = provider.good(start.nonce, aud="another-client")
    provider.id_token_override = provider.sign(claims)
    with pytest.raises(oidc.SignInRefused, match="another client"):
        oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)


def test_an_audience_list_needs_our_azp(provider, db):
    start = oidc.begin(CALLBACK)
    for azp, ok in ((None, False), ("other", False), (CLIENT, True)):
        claims = provider.good(start.nonce, aud=[CLIENT, "other"], azp=azp)
        provider.id_token_override = provider.sign(claims)
        if ok:
            assert oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)
        else:
            with pytest.raises(oidc.SignInRefused):
                oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)


def test_a_wrong_issuer_is_refused(provider, db):
    start = oidc.begin(CALLBACK)
    provider.id_token_override = provider.sign(
        provider.good(start.nonce, iss="https://evil.example")
    )
    with pytest.raises(oidc.SignInRefused, match="another issuer"):
        oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)


def test_an_expired_token_is_refused_and_a_small_skew_is_not(provider, db):
    start = oidc.begin(CALLBACK)
    now = int(time.time())
    provider.id_token_override = provider.sign(
        provider.good(start.nonce, iat=now - 400, exp=now - 120)
    )
    with pytest.raises(oidc.SignInRefused, match="expired"):
        oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)
    provider.id_token_override = provider.sign(
        provider.good(start.nonce, iat=now - 400, exp=now - 10)
    )
    assert oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)


def test_a_token_issued_in_the_future_is_refused(provider, db):
    start = oidc.begin(CALLBACK)
    now = int(time.time())
    provider.id_token_override = provider.sign(
        provider.good(start.nonce, iat=now + 600, exp=now + 900)
    )
    with pytest.raises(oidc.SignInRefused, match="future"):
        oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)


def test_a_token_without_exp_or_iat_is_refused(provider, db):
    for missing in ("exp", "iat"):
        start = oidc.begin(CALLBACK)
        provider.id_token_override = provider.sign(
            provider.good(start.nonce, **{missing: None})
        )
        with pytest.raises(oidc.SignInRefused):
            oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)


def test_a_bad_signature_or_algorithm_is_refused(provider, db):
    other = rsa.generate_private_key(65537, 2048)
    start = oidc.begin(CALLBACK)
    provider.id_token_override = provider.sign(provider.good(start.nonce), key=other)
    with pytest.raises(oidc.SignInRefused, match="signature"):
        oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)
    for alg in ("none", "HS256"):
        provider.id_token_override = provider.sign(provider.good(start.nonce), alg=alg)
        with pytest.raises(oidc.SignInRefused, match="algorithm"):
            oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)


def test_an_unknown_key_id_is_refused(provider, db):
    start = oidc.begin(CALLBACK)
    provider.id_token_override = provider.sign(provider.good(start.nonce), kid="zzz")
    with pytest.raises(oidc.SignInRefused, match="does not publish"):
        oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)


@pytest.mark.parametrize("junk", ["", "a.b", "a.b.c", "!!.??.@@"])
def test_a_malformed_token_is_refused(provider, db, junk):
    start = oidc.begin(CALLBACK)
    provider.id_token_override = junk
    with pytest.raises(oidc.SignInRefused):
        oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)


def test_a_token_endpoint_error_is_refused(provider, db):
    start = oidc.begin(CALLBACK)
    provider.token_status = 400
    with pytest.raises(oidc.SignInRefused, match="HTTP 400"):
        oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)


def test_a_non_owner_is_refused_and_nothing_is_stored(provider, db):
    start = oidc.begin(CALLBACK)
    provider.id_token_override = provider.sign(
        provider.good(start.nonce, preferred_username="mallory", sub="mallory-sub")
    )
    with pytest.raises(oidc.NotOwner) as caught:
        oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)
    assert isinstance(caught.value, oidc.SignInRefused)
    assert str(caught.value) == (
        f"Only {settings.current().owner_name} can sign in to this Ream"
    )
    # The module keeps only the provider's public documents, in memory: no login, no name.
    assert all("mallory" not in json.dumps(v[1]) for v in oidc._cache.values())


def test_the_owner_claim_and_value_are_settings(provider, db):
    with settings.override(oidc_owner_claim="email", oidc_owner_value="j@example.org"):
        start = oidc.begin(CALLBACK)
        provider.id_token_override = provider.sign(
            provider.good(start.nonce, email="j@example.org")
        )
        assert oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK).person == (
            settings.owner_id()
        )
        provider.id_token_override = provider.sign(provider.good(start.nonce))
        with pytest.raises(oidc.NotOwner):  # no email claim
            oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)


def test_a_claim_that_is_a_list_must_hold_the_value(provider, db):
    with settings.override(oidc_owner_claim="groups", oidc_owner_value="admins"):
        assert run(provider, db)[1].person == settings.owner_id()
    with (
        settings.override(oidc_owner_claim="groups", oidc_owner_value="staff"),
        pytest.raises(oidc.NotOwner),
    ):
        run(provider, db)


def test_a_discovery_document_for_another_issuer_is_refused(provider, db, monkeypatch):
    def lying(method, url, body, headers):
        return oidc.Response(
            200,
            "application/json",
            json.dumps({"issuer": "https://evil.example"}).encode(),
        )

    monkeypatch.setattr(oidc, "fetch", lying)
    oidc.clear_caches()
    with pytest.raises(oidc.SignInRefused, match="another issuer"):
        oidc.begin(CALLBACK)


def test_the_secret_is_read_when_needed_and_never_shown(provider, db, tmp_path, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    run(provider, db)
    assert SECRET not in caplog.text
    with settings.override(oidc_client_secret_file=str(tmp_path / "gone")):
        start = oidc.begin(CALLBACK)
        provider.id_token_override = provider.sign(provider.good(start.nonce))
        with pytest.raises(oidc.SignInRefused) as caught:
            oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)
        assert "gone" not in str(caught.value)


# --- Start-up checks -----------------------------------------------------------------------


def _full(tmp_path, **more):
    secret = tmp_path / "secret"
    secret.write_text("s3cret\n")
    values = {
        "mcp_public_url": "https://artifacts.tenfourty.site",
        "mcp_sign_in": "on",
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT,
        "oidc_client_secret_file": str(secret),
    }
    values.update(more)
    return values


def test_sign_in_on_names_every_missing_provider_value():
    with settings.override(
        mcp_public_url="https://artifacts.tenfourty.site", mcp_sign_in="on"
    ) as current:
        found = "; ".join(current.problems())
    for name in ("ISSUER", "CLIENT_ID", "CLIENT_SECRET_FILE"):
        assert f"needs REAM_OIDC_{name}" in found
    assert "not finished" not in found


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({}, None),
        ({"oidc_issuer": ""}, "needs REAM_OIDC_ISSUER"),
        ({"oidc_client_id": ""}, "needs REAM_OIDC_CLIENT_ID"),
        ({"oidc_client_secret_file": ""}, "needs REAM_OIDC_CLIENT_SECRET_FILE"),
        ({"oidc_issuer": "http://auth.example"}, "must be https"),
        ({"oidc_issuer": "http://localhost:9091"}, None),
        ({"oidc_issuer": "auth.example"}, "issuer URL"),
        ({"oidc_issuer": "https://auth.example/?x=1"}, "issuer URL"),
        ({"oidc_scopes": "profile email"}, "must include openid"),
        ({"oidc_scopes": "openid"}, None),
        ({"oidc_owner_claim": "email", "oidc_owner_value": "j@example.org"}, None),
    ],
)
def test_sign_in_checks_its_provider_settings(tmp_path, change, problem):
    with settings.override(**_full(tmp_path, **change)) as current:
        found = [p for p in current.problems() if "OIDC" in p]
    if problem is None:
        assert found == []
    else:
        assert len(found) == 1 and problem in found[0], found


def test_sign_in_refuses_an_unreadable_or_empty_secret_file(tmp_path):
    missing = _full(tmp_path, oidc_client_secret_file=str(tmp_path / "nope"))
    with settings.override(**missing) as current:
        found = current.problems()
    assert any("SECRET_FILE cannot be read" in p for p in found), found
    assert str(tmp_path) not in " ".join(found)
    (tmp_path / "blank").write_text("  \n")
    blank = _full(tmp_path, oidc_client_secret_file=str(tmp_path / "blank"))
    with settings.override(**blank) as current:
        assert any("SECRET_FILE is empty" in p for p in current.problems())


def test_a_full_sign_in_configuration_starts(tmp_path):
    with settings.override(**_full(tmp_path)) as current:
        assert not [p for p in current.problems() if "MCP" in p or "OIDC" in p]
        assert current.mcp_signs_in()


def test_sign_in_off_asks_nothing_of_the_provider_settings():
    with settings.override(mcp_sign_in="off") as current:
        assert not [p for p in current.problems() if "OIDC" in p]


def test_the_oidc_settings_default_as_the_spec_says():
    with settings.override(oidc_issuer=ISSUER) as current:
        assert current.oidc_scope_list() == ["openid", "profile", "email", "groups"]
        assert current.oidc_owner_claim_name() == "preferred_username"
        assert current.oidc_owner_value_or_default() == current.owner_id
        assert current.oidc_groups_claim_name() == "groups"


def test_the_oidc_settings_read_under_both_names(monkeypatch):
    monkeypatch.setenv("ARTIFACTS_OIDC_CLIENT_ID", "old")
    assert settings.current().oidc_client_id == "old"
    monkeypatch.setenv("REAM_OIDC_CLIENT_ID", "new")
    with pytest.raises(SystemExit):
        settings.current()


def sign_in_as(provider, db, **claims):
    start = oidc.begin(CALLBACK)
    provider.id_token_override = provider.sign(provider.good(start.nonce, **claims))
    return oidc.finish(db, "c", start.verifier, start.nonce, CALLBACK)


@pytest.mark.parametrize("verified", [None, False, "true", 1])
def test_an_email_that_is_not_verified_is_refused(provider, db, verified):
    with settings.override(oidc_owner_claim="email", oidc_owner_value="j@example.org"):
        with pytest.raises(oidc.NotOwner):
            sign_in_as(provider, db, email="j@example.org", email_verified=verified)
        assert not _pin(db)


def test_a_verified_email_signs_in(provider, db):
    with settings.override(oidc_owner_claim="email", oidc_owner_value="j@example.org"):
        assert sign_in_as(provider, db, email="j@example.org", email_verified=True)


def _pin(db):
    with db.connect() as conn:
        return conn.execute(
            "select value from service_meta where key = %s", (oidc.PIN_KEY,)
        ).fetchone()


def test_the_first_owner_sign_in_pins_the_subject(provider, db):
    assert sign_in_as(provider, db).subject == "u-1"
    assert _pin(db)[0] == f"{ISSUER} u-1"
    assert sign_in_as(provider, db).subject == "u-1"


def test_a_different_subject_with_the_owners_claim_is_refused_after_the_pin(
    provider, db
):
    with settings.override(oidc_owner_claim="email", oidc_owner_value="j@example.org"):
        sign_in_as(provider, db, email="j@example.org", email_verified=True)
        with pytest.raises(oidc.NotOwner):
            sign_in_as(
                provider, db, sub="u-2", email="j@example.org", email_verified=True
            )
    assert _pin(db)[0] == f"{ISSUER} u-1"


def test_a_non_owner_pins_nothing(provider, db):
    with pytest.raises(oidc.NotOwner):
        sign_in_as(provider, db, preferred_username="mallory")
    assert not _pin(db)


def test_the_configured_subject_wins_over_the_pin(provider, db):
    sign_in_as(provider, db)  # pins u-1
    with settings.override(oidc_owner_subject="u-2"):
        assert sign_in_as(provider, db, sub="u-2").subject == "u-2"
        with pytest.raises(oidc.NotOwner):
            sign_in_as(provider, db, sub="u-1")
    assert _pin(db)[0] == f"{ISSUER} u-1"


def test_a_configured_subject_pins_nothing(provider, db):
    with settings.override(oidc_owner_subject="u-1"):
        assert sign_in_as(provider, db)
    assert not _pin(db)
