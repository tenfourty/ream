"""The authorization server up to the first tokens (docs/agent/specs/2026-11-08-mcp-oauth-design.md,
9 and 10): the whole flow with Claude.ai's literal callback against the fake OIDC provider,
the refusals, remembered consent, and the `rmo_` token at `/mcp` and not under `/api/v1`."""

import html
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from test_mcp_sign_in import (  # noqa: F401 -- stub is a fixture
    CLAUDE_CALLBACK,
    CLAUDE_ID,
    PING,
    PUBLIC,
    stub,
)
from test_oidc import provider  # noqa: F401 -- fixture

from ream import mcp_clients, oauth, oidc, scan, settings
from ream.app import create_app

VERIFIER = "v" * 60
STATE = "client-state-1"
SAME = {"sec-fetch-site": "same-origin"}
ALL = ("read", "write", "relay")


@pytest.fixture()
def web(db, provider, stub):
    stub()
    return TestClient(create_app(db), base_url=PUBLIC, follow_redirects=False)


def params(**more):
    base = {
        "response_type": "code",
        "client_id": CLAUDE_ID,
        "redirect_uri": CLAUDE_CALLBACK,
        "code_challenge": oidc.challenge(VERIFIER),
        "code_challenge_method": "S256",
        "resource": PUBLIC + "/mcp",
        "state": STATE,
        "scope": "ream:read ream:write ream:relay",
    }
    base.update(more)
    return {k: v for k, v in base.items() if v is not None}


def begin(web, **more):
    answer = web.get("/oauth/authorize", params=params(**more))
    assert answer.status_code == 302, answer.text
    q = parse_qs(urlsplit(answer.headers["location"]).query)
    return q["state"][0], q["nonce"][0]


def sign_in(web, provider, state_nonce, **claims):
    state, nonce = state_nonce
    provider.id_token_override = provider.sign(provider.good(nonce, **claims))
    return web.get("/oauth/callback", params={"code": "up", "state": state})


def consent_nonce(page):
    return re.search(r'name="consent" value="([^"]+)"', page.text).group(1)


def landing(page) -> dict:
    href = html.unescape(re.search(r'<a href="([^"]+)">continue', page.text).group(1))
    assert href.startswith(CLAUDE_CALLBACK + "?")
    return {k: v[0] for k, v in parse_qs(urlsplit(href).query).items()}


def allow(web, page, perms=ALL):
    data = {"consent": consent_nonce(page), "decision": "allow", "perm": list(perms)}
    return web.post("/oauth/authorize", data=data, headers=SAME)


def exchange(web, code, **more):
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": VERIFIER,
        "client_id": CLAUDE_ID,
        "redirect_uri": CLAUDE_CALLBACK,
        "resource": PUBLIC + "/mcp",
    }
    data.update(more)
    return web.post("/oauth/token", data=data)


def full_flow(web, provider, perms=ALL):
    page = sign_in(web, provider, begin(web))
    assert page.status_code == 200 and "claude.ai" in page.text
    return landing(allow(web, page, perms))


def test_the_whole_flow_with_claudes_callback_ends_in_a_working_token(
    web, provider, db
):
    back = full_flow(web, provider)
    assert back["state"] == STATE and back["iss"] == PUBLIC and back["code"]
    got = exchange(web, back["code"])
    assert got.status_code == 200
    body = got.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600  # noqa: S105
    assert body["access_token"].startswith("rmo_")
    assert body["refresh_token"].startswith("rmr_")
    assert body["scope"] == "ream:read ream:write ream:relay"
    with db.connect() as conn:  # only hashes are stored
        stored = [
            r[0] for r in conn.execute("select token_sha256 from mcp_token").fetchall()
        ]
    assert len(stored) == 2 and body["access_token"] not in stored
    me = web.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}},
        },
        headers={"authorization": "Bearer " + body["access_token"]},
    )
    assert me.status_code == 200
    assert "jeremy--claude-ai" in me.text


def test_an_rmo_token_is_refused_under_api_v1(web, provider):
    token = exchange(web, full_flow(web, provider)["code"]).json()["access_token"]
    answer = web.get("/api/v1/me", headers={"authorization": "Bearer " + token})
    assert answer.status_code == 401
    assert "only at /mcp" in answer.text


def test_an_unknown_rmo_token_is_refused_at_mcp(web, provider):
    answer = web.post(
        "/mcp", json=PING, headers={"authorization": "Bearer rmo_" + "a" * 43}
    )
    assert answer.status_code == 401


def test_a_wrong_verifier_is_refused(web, provider):
    code = full_flow(web, provider)["code"]
    got = exchange(web, code, code_verifier="w" * 60)
    assert got.status_code == 400 and got.json()["error"] == "invalid_grant"


def test_another_client_or_resource_or_return_address_is_refused(web, provider):
    for change in (
        {"client_id": "https://other.example/c"},
        {"resource": PUBLIC + "/other"},
        {"redirect_uri": "https://claude.ai/elsewhere"},
    ):
        code = full_flow(web, provider)["code"]
        assert exchange(web, code, **change).status_code == 400
        assert exchange(web, code).status_code == 400  # the attempt spent it


def test_a_code_used_twice_ends_the_app(web, provider, db):
    first = full_flow(web, provider)["code"]
    token = exchange(web, first).json()["access_token"]
    assert exchange(web, first).status_code == 400
    with db.connect() as conn:
        assert (
            conn.execute("select revoked_reason from mcp_app").fetchone()[0] == "reuse"
        )
        assert conn.execute("select count(*) from mcp_token").fetchone()[0] == 0
    gone = web.post("/mcp", json=PING, headers={"authorization": "Bearer " + token})
    assert gone.status_code == 401


def test_an_expired_code_is_refused(web, provider, db):
    code = full_flow(web, provider)["code"]
    with db.connect() as conn:
        conn.execute(
            "update mcp_signin set code_expires_at = %s", ("2000-01-01T00:00:00+00:00",)
        )
    assert exchange(web, code).status_code == 400


@pytest.mark.parametrize(
    "change",
    [
        {"response_type": "token"},
        {"code_challenge_method": "plain"},
        {"code_challenge": None},
        {"resource": "https://elsewhere.example/mcp"},
        {"state": None},
    ],
)
def test_a_bad_authorize_request_goes_back_with_an_error_and_iss(web, change):
    page = web.get("/oauth/authorize", params=params(**change))
    text = html.unescape(page.text)
    assert page.status_code == 200 and "error=" in text and "iss=" in text


def test_an_unknown_client_or_return_address_is_never_redirected_to(web):
    for change in (
        {"redirect_uri": "https://evil.example/cb"},
        {"client_id": "not-a-client"},
    ):
        page = web.get("/oauth/authorize", params=params(**change))
        assert page.status_code == 400 and "location" not in page.headers


def test_the_provider_redirect_comes_from_settings_not_the_host(web, provider):
    spoofed = web.get(
        "/oauth/authorize", params=params(), headers={"host": "evil.example"}
    )
    assert spoofed.status_code == 404  # not a served host
    location = web.get("/oauth/authorize", params=params()).headers["location"]
    assert parse_qs(urlsplit(location).query)["redirect_uri"] == [
        PUBLIC + "/oauth/callback"
    ]
    assert oauth.callback_uri() == PUBLIC + "/oauth/callback"


def test_a_missing_or_mismatched_cookie_is_refused(web, provider):
    pair = begin(web)
    web.cookies.clear()
    page = sign_in(web, provider, pair)
    assert page.status_code == 400 and "not started in this browser" in page.text
    pair = begin(web)
    web.cookies.set("ream_oauth", "someone-elses", path="/oauth")
    assert sign_in(web, provider, pair).status_code == 400


def test_a_used_state_is_gone(web, provider):
    pair = begin(web)
    assert sign_in(web, provider, pair).status_code == 200
    again = sign_in(web, provider, pair)
    assert again.status_code == 400 and "used already" in again.text


def test_a_non_owner_sees_the_refusal_and_nothing_is_stored(web, provider, db):
    page = sign_in(web, provider, begin(web), preferred_username="mallory")
    assert page.status_code == 403
    with db.connect() as conn:
        assert conn.execute("select count(*) from mcp_app").fetchone()[0] == 0


def test_the_consent_page_has_three_boxes_and_the_app_by_its_address(web, provider):
    page = sign_in(web, provider, begin(web))
    assert page.text.count('type="checkbox"') == 3
    assert "claude.ai" in page.text and CLAUDE_CALLBACK in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


def test_cross_site_consent_is_refused_and_does_not_spend_the_form(web, provider):
    page = sign_in(web, provider, begin(web))
    data = {"consent": consent_nonce(page), "decision": "allow", "perm": ["read"]}
    cross = web.post(
        "/oauth/authorize", data=data, headers={"sec-fetch-site": "cross-site"}
    )
    assert cross.status_code == 403
    assert web.post("/oauth/authorize", data=data, headers=SAME).status_code == 200


def test_nothing_ticked_or_relay_without_read_is_refused_on_the_page(web, provider):
    page = sign_in(web, provider, begin(web))
    none = allow(web, page, perms=())
    assert "Tick at least one" in none.text
    relay = allow(web, none, perms=("relay",))
    assert "needs reading" in relay.text
    assert landing(allow(web, relay, perms=("read",)))["code"]


def test_deny_sends_access_denied_back(web, provider):
    page = sign_in(web, provider, begin(web))
    data = {"consent": consent_nonce(page), "decision": "deny"}
    out = web.post("/oauth/authorize", data=data, headers=SAME)
    assert landing(out)["error"] == "access_denied"


def test_remembered_consent_skips_the_screen_for_the_same_or_narrower_request(
    web, provider, db
):
    full_flow(web, provider)
    assert landing(sign_in(web, provider, begin(web)))["code"]
    assert landing(sign_in(web, provider, begin(web, scope="ream:read")))["code"]
    with db.connect() as conn:
        assert conn.execute("select count(*) from mcp_app").fetchone()[0] == 1


def test_a_wider_request_shows_the_screen_again(web, provider):
    first = sign_in(web, provider, begin(web, scope="ream:read"))
    landing(allow(web, first, perms=("read",)))
    assert consent_nonce(sign_in(web, provider, begin(web)))


def test_a_different_return_address_shows_the_screen_again(web, provider, db):
    full_flow(web, provider)
    with db.connect() as conn:
        conn.execute("update mcp_app set redirect_uri = 'https://claude.ai/other'")
    assert consent_nonce(sign_in(web, provider, begin(web)))


def test_a_registered_app_is_never_remembered(web, provider, db):
    uri = "http://localhost:5555/cb"
    made = mcp_clients.register(
        db,
        {"redirect_uris": [uri], "token_endpoint_auth_method": "none"},
        "203.0.113.9",
    )
    for _ in range(2):
        pair = begin(web, client_id=made["client_id"], redirect_uri=uri)
        page = sign_in(web, provider, pair)
        assert consent_nonce(page) and "An app on this computer" in page.text
        assert allow(web, page).status_code == 200


def test_the_ttl_settings_must_be_positive_whole_numbers():
    for field in (
        "mcp_access_token_ttl_secs",
        "mcp_refresh_token_ttl_days",
        "mcp_app_max_lifetime_days",
    ):
        for bad in ("0", "-1", "x", "1.5"):
            with settings.override(**{field: bad}):
                assert any(
                    "positive whole number" in p for p in settings.current().problems()
                )
    assert settings.current().app_max_lifetime_days() == 90


def test_the_token_prefixes_are_secret_shapes():
    for token in (oauth.new_token("rmo_"), oauth.new_token("rmr_")):
        assert scan.scan(f"<p>{token}</p>")
