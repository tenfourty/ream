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


# --- Review round 3 ---------------------------------------------------------------------------


def test_authorize_is_rate_limited_per_address(web):
    codes = [
        web.get("/oauth/authorize", params={"client_id": "nope"}).status_code
        for _ in range(12)
    ]
    assert codes[0] == 400 and codes[-1] == 429


def test_oversized_state_or_scope_is_invalid_request_and_not_echoed(web):
    for field in ("state", "scope"):
        answer = web.get("/oauth/authorize", params=params(**{field: "x" * 513}))
        assert answer.status_code == 302
        q = parse_qs(urlsplit(answer.headers["location"]).query)
        assert q["error"] == ["invalid_request"]
        assert "x" * 513 not in answer.headers["location"]


def test_a_full_fetch_pool_answers_503_but_registered_and_cached_clients_pass(
    web, db, monkeypatch
):
    uri = "http://localhost:5555/cb"
    made = mcp_clients.register(
        db,
        {"redirect_uris": [uri], "token_endpoint_auth_method": "none"},
        "203.0.113.9",
    )
    assert (
        web.get("/oauth/authorize", params=params()).status_code == 302
    )  # caches Claude
    full = {f"h{i}.example" for i in range(mcp_clients.MAX_FETCHES)}
    monkeypatch.setattr(mcp_clients, "_fetching", full)
    registered = web.get(
        "/oauth/authorize", params=params(client_id=made["client_id"], redirect_uri=uri)
    )
    assert registered.status_code == 302
    assert web.get("/oauth/authorize", params=params()).status_code == 302  # cached
    with db.connect() as conn:
        conn.execute(
            "update mcp_client set fetched_at = %s", ("2000-01-01T00:00:00+00:00",)
        )
    assert web.get("/oauth/authorize", params=params()).status_code == 503


def test_callback_and_decide_complete_while_fetches_are_busy(
    web, provider, monkeypatch
):
    pair = begin(web)  # authorize accepted the client and recorded it
    monkeypatch.setattr(
        mcp_clients, "_fetching", {f"h{i}" for i in range(mcp_clients.MAX_FETCHES)}
    )
    monkeypatch.setattr(
        mcp_clients, "_get", lambda *_a: pytest.fail("no fetch after authorize")
    )
    page = sign_in(web, provider, pair)
    assert page.status_code == 200
    assert landing(allow(web, page))["code"]


def test_one_client_id_has_at_most_one_fetch_in_flight(db, stub, monkeypatch):
    stub()
    monkeypatch.setattr(mcp_clients, "_fetching", {CLAUDE_ID})
    with pytest.raises(mcp_clients.FetchBusy):
        mcp_clients.client_for(db, CLAUDE_ID)


def test_a_scope_of_only_unknown_words_is_invalid_scope(web):
    answer = web.get("/oauth/authorize", params=params(scope="offline_access"))
    q = parse_qs(urlsplit(answer.headers["location"]).query)
    assert answer.status_code == 302 and q["error"] == ["invalid_scope"]
    assert web.get("/oauth/authorize", params=params(scope="")).status_code == 302


def test_reconsent_renews_an_app_past_its_cap(web, provider, db):
    first = full_flow(web, provider)
    assert exchange(web, first["code"]).status_code == 200
    with db.connect() as conn:
        conn.execute(
            "update mcp_app set expires_at = %s", ("2000-01-01T00:00:00+00:00",)
        )
    back = full_flow(web, provider)
    got = exchange(web, back["code"])
    assert got.status_code == 200, got.text
    with db.connect() as conn:
        reasons = [
            r[0]
            for r in conn.execute(
                "select revoked_reason from mcp_app where revoked_at is not null"
            ).fetchall()
        ]
    assert reasons == ["lapsed"]


def test_remembered_consent_grants_the_request_not_the_whole_hold(web, provider):
    full_flow(web, provider)
    back = landing(sign_in(web, provider, begin(web, scope="ream:read")))
    assert exchange(web, back["code"]).json()["scope"] == "ream:read"


def test_token_refuses_a_big_body_and_a_file_field(web):
    big = web.post(
        "/oauth/token",
        content=b"a=" + b"x" * 70_000,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert big.status_code == 413
    odd = web.post(
        "/oauth/token",
        data={"grant_type": "authorization_code", "code": "c"},
        files={"client_id": ("c.txt", b"x"), "redirect_uri": ("r.txt", b"y")},
    )
    assert odd.status_code == 400 and odd.json()["error"] == "invalid_request"


def test_a_client_refused_during_consent_is_a_page_not_a_500(
    web, provider, monkeypatch
):
    page = sign_in(web, provider, begin(web))
    nonce = consent_nonce(page)

    def refuse(*_a, **_k):
        raise mcp_clients.ClientRefused("gone")

    monkeypatch.setattr(mcp_clients, "recorded", refuse)
    data = {"consent": nonce, "decision": "allow"}  # nothing ticked: the re-render path
    out = web.post("/oauth/authorize", data=data, headers=SAME)
    assert out.status_code == 400 and "cannot ask to connect" in out.text


def test_a_signed_in_app_can_call_whats_new_without_a_key(web, provider):
    token = exchange(web, full_flow(web, provider)["code"]).json()["access_token"]
    for _ in range(2):
        out = web.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "whats_new", "arguments": {}},
            },
            headers={"authorization": "Bearer " + token},
        )
        assert out.status_code == 200
        assert not out.json()["result"].get("isError")
