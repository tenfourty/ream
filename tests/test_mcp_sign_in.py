"""MCP sign-in, the parts that say how and who (docs/agent/specs/2026-11-08-mcp-oauth-design.md,
sections 4, 5, 7 and 8): the two discovery documents, the 401 challenge, keys at `/mcp` while
sign-in is on, client metadata documents behind the address guard, open registration and its
limits, and the trusted and allowed host lists."""

import json
import logging
import socket
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ream import keys, mcp_clients, ratelimit, settings
from ream.app import create_api_app, create_app

HOST = "artifacts.tenfourty.site"
PUBLIC = f"https://{HOST}"
# What Claude.ai really sends: its client_id is this document's address, and it returns here.
CLAUDE_ID = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
CHALLENGE = (
    'Bearer realm="ream", resource_metadata="https://artifacts.tenfourty.site'
    '/.well-known/oauth-protected-resource/mcp", scope="ream:read ream:write ream:relay"'
)


@pytest.fixture()
def signs_in():
    with settings.override(mcp_public_url=PUBLIC, mcp_sign_in="on"):
        yield


@pytest.fixture()
def client(db) -> TestClient:
    return TestClient(create_app(db), follow_redirects=False)


def h(**extra: str) -> dict[str, str]:
    return {"host": HOST, "content-type": "application/json", **extra}


# --- The discovery documents (section 4) ---------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource",
    ],
)
def test_the_protected_resource_document_field_by_field(client, signs_in, path):
    answer = client.get(path, headers={"host": HOST})
    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("application/json")
    assert answer.json() == {
        # The MCP URL exactly, path included: Claude compares it with what was typed.
        "resource": "https://artifacts.tenfourty.site/mcp",
        # Exactly one: Claude uses only the first.
        "authorization_servers": ["https://artifacts.tenfourty.site"],
        "scopes_supported": ["ream:read", "ream:write", "ream:relay"],
        "bearer_methods_supported": ["header"],
        "resource_name": "Ream",
    }


def test_the_authorization_server_document_field_by_field(client, signs_in):
    answer = client.get(
        "/.well-known/oauth-authorization-server", headers={"host": HOST}
    )
    assert answer.status_code == 200
    doc = answer.json()
    assert doc == {
        "issuer": "https://artifacts.tenfourty.site",
        "authorization_endpoint": "https://artifacts.tenfourty.site/oauth/authorize",
        "token_endpoint": "https://artifacts.tenfourty.site/oauth/token",
        "revocation_endpoint": "https://artifacts.tenfourty.site/oauth/revoke",
        "registration_endpoint": "https://artifacts.tenfourty.site/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["ream:read", "ream:write", "ream:relay"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    }
    # What Claude needs before it uses a client metadata document: both of these.
    assert doc["client_id_metadata_document_supported"] is True
    assert "none" in doc["token_endpoint_auth_methods_supported"]
    # Claude would append offline_access if it were listed; the 2026 revision says not to.
    assert "offline_access" not in doc["scopes_supported"]


def test_on_the_api_host_the_issuer_is_the_api_host_and_authorize_the_gallerys(db):
    api = "api.tenfourty.site"
    with settings.override(
        api_host=api, mcp_public_url=f"https://{api}", mcp_sign_in="on"
    ):
        on_api = TestClient(create_api_app(db), follow_redirects=False)
        doc = on_api.get(
            "/.well-known/oauth-authorization-server", headers={"host": api}
        ).json()
        resource = on_api.get(
            "/.well-known/oauth-protected-resource/mcp", headers={"host": api}
        ).json()
    assert doc["issuer"] == "https://api.tenfourty.site"
    assert doc["token_endpoint"] == "https://api.tenfourty.site/oauth/token"  # noqa: S105
    assert doc["authorization_endpoint"] == (
        "https://artifacts.tenfourty.site/oauth/authorize"
    )
    assert resource["resource"] == "https://api.tenfourty.site/mcp"
    assert resource["authorization_servers"] == ["https://api.tenfourty.site"]


def test_registration_is_left_out_while_it_is_off(client, signs_in):
    with settings.override(mcp_self_registration_redirects="none"):
        doc = client.get(
            "/.well-known/oauth-authorization-server", headers={"host": HOST}
        ).json()
        posted = client.post(
            "/oauth/register",
            headers=h(),
            json={"redirect_uris": ["http://localhost:3000/cb"]},
        )
    assert "registration_endpoint" not in doc
    assert posted.status_code == 404


WELL_KNOWN = (
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
)


@pytest.mark.parametrize("path", WELL_KNOWN)
def test_with_sign_in_off_nothing_new_is_served(client, path):
    # Neither setting, then /mcp alone: no document, no registration, the old challenge.
    assert client.get(path, headers={"host": HOST}).status_code == 404
    with settings.override(mcp_public_url=PUBLIC):
        assert client.get(path, headers={"host": HOST}).status_code == 404
        assert (
            client.post(
                "/oauth/register",
                headers=h(),
                json={"redirect_uris": ["http://localhost:3000/cb"]},
            ).status_code
            == 404
        )
        refused = client.post("/mcp", headers=h(), json=PING)
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == 'Bearer realm="ream"'


@pytest.mark.parametrize("path", WELL_KNOWN)
def test_the_documents_are_served_only_on_the_public_hosts_name(db, signs_in, path):
    with settings.override(api_host="api.tenfourty.site"):
        on_api = TestClient(create_api_app(db), follow_redirects=False)
        assert (
            on_api.get(path, headers={"host": "api.tenfourty.site"}).status_code == 404
        )


# --- The challenge, and keys while sign-in is on (sections 4 and 7) ---------------------------


def test_no_token_is_401_with_the_sign_in_challenge(client, signs_in):
    answer = client.post("/mcp", headers=h(), json=PING)
    assert answer.status_code == 401
    assert answer.headers["www-authenticate"] == CHALLENGE
    assert answer.json()["error"]["message"].startswith("Sign in from your MCP app")


def test_an_unknown_token_is_401_with_the_challenge(client, signs_in):
    answer = client.post(
        "/mcp", headers=h(authorization="Bearer rmo_not_issued_by_anyone"), json=PING
    )
    assert answer.status_code == 401
    assert answer.headers["www-authenticate"] == CHALLENGE


def test_a_working_key_is_refused_while_sign_in_is_on_and_logged(
    client, db, signs_in, caplog
):
    token, key_id = keys.mint(db, agent="mcp-seat")
    with caplog.at_level(logging.WARNING, logger="ream.mcp"):
        answer = client.post(
            "/mcp", headers=h(authorization=f"Bearer {token}"), json=PING
        )
    assert answer.status_code == 401
    assert answer.headers["www-authenticate"] == CHALLENGE
    assert answer.json()["error"]["message"] == (
        "Agent keys are not accepted at this server's /mcp. Sign in from your MCP app,"
        " or use the agent API at /api/v1 with your key."
    )
    logged = [r.getMessage() for r in caplog.records if r.name == "ream.mcp"]
    assert any("reason=key_on_mcp" in m and key_id in m for m in logged), logged
    assert all(token not in m for m in caplog.messages)
    # The same key still works where keys belong.
    assert (
        client.get(
            "/api/v1/me", headers={"host": HOST, "authorization": f"Bearer {token}"}
        ).status_code
        == 200
    )


def test_accept_keys_takes_a_key_beside_sign_in(client, db, signs_in):
    token, _ = keys.mint(db, agent="mcp-seat")
    with settings.override(mcp_accept_keys="1"):
        ok = client.post("/mcp", headers=h(authorization=f"Bearer {token}"), json=PING)
        bad = client.post(
            "/mcp", headers=h(authorization="Bearer art_not_a_real_key"), json=PING
        )
    assert ok.status_code == 200
    assert bad.status_code == 401
    assert bad.headers["www-authenticate"] == CHALLENGE


def test_failed_credentials_at_mcp_count_in_the_shared_limiter(client, signs_in):
    codes = [
        client.post("/mcp", headers=h(), json=PING).status_code
        for _ in range(ratelimit.THRESHOLD)
    ]
    assert codes[0] == 401 and codes[-1] == 429, codes
    # The same count holds the agent API back for that address.
    assert (
        client.get(
            "/api/v1/me", headers={"host": HOST, "authorization": "Bearer art_x"}
        ).status_code
        == 429
    )


def test_keys_are_taken_by_default_while_sign_in_is_off():
    with settings.override(mcp_public_url=PUBLIC) as current:
        assert current.mcp_accepts_keys() is True
    with settings.override(mcp_public_url=PUBLIC, mcp_sign_in="on") as current:
        assert current.mcp_accepts_keys() is False
    with settings.override(
        mcp_public_url=PUBLIC, mcp_sign_in="on", mcp_accept_keys="1"
    ) as current:
        assert current.mcp_accepts_keys() is True


# --- Client metadata documents behind the address guard (section 5) ---------------------------


def _doc(**overrides) -> dict:
    doc = {
        "client_id": CLAUDE_ID,
        "client_name": "Claude",
        "redirect_uris": [CLAUDE_CALLBACK],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    doc.update(overrides)
    return doc


class Stub:
    """The one fetch, replaced: records each URL asked for and serves one answer."""

    def __init__(self, status=200, content_type="application/json", body=None):
        self.asked: list[str] = []
        self.answer = mcp_clients.Fetched(
            status,
            content_type,
            json.dumps(_doc()).encode() if body is None else body,
        )

    def __call__(self, url: str) -> mcp_clients.Fetched:
        self.asked.append(url)
        return self.answer


@pytest.fixture()
def stub(monkeypatch):
    def install(**kwargs) -> Stub:
        s = Stub(**kwargs)
        monkeypatch.setattr(mcp_clients, "_get", s)
        return s

    return install


def _stored(db) -> int:
    with db.connect() as conn:
        return conn.execute("select count(*) from mcp_client").fetchone()[0]


def test_claude_ais_document_is_read_checked_and_kept_for_60_seconds(db, stub):
    s = stub()
    now = datetime.now(UTC)
    client = mcp_clients.client_for(db, CLAUDE_ID, now)
    assert (client.id, client.kind, client.host, client.name) == (
        CLAUDE_ID,
        "cimd",
        "claude.ai",
        "Claude",
    )
    assert client.redirect_uris == (CLAUDE_CALLBACK,)
    assert client.verified is True
    assert mcp_clients.client_for(db, CLAUDE_ID, now + timedelta(seconds=59)) == client
    assert s.asked == [CLAUDE_ID]
    mcp_clients.client_for(db, CLAUDE_ID, now + timedelta(seconds=61))
    assert s.asked == [CLAUDE_ID, CLAUDE_ID]
    assert _stored(db) == 1


@pytest.mark.parametrize(
    "answer",
    [
        ("10.0.0.5", socket.AF_INET),  # private
        ("192.168.1.20", socket.AF_INET),  # private
        ("127.0.0.1", socket.AF_INET),  # loopback
        ("169.254.169.254", socket.AF_INET),  # link-local (cloud metadata)
        ("::1", socket.AF_INET6),  # loopback
        ("fe80::1", socket.AF_INET6),  # link-local
        ("fd00::5", socket.AF_INET6),  # unique local
        ("::ffff:10.0.0.5", socket.AF_INET6),  # private, mapped
    ],
)
def test_a_document_on_a_non_public_address_is_never_connected_to(
    db, monkeypatch, answer
):
    address, family = answer
    resolved, connected = [], []

    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *a, **k):
        # Answer only the fetch's host: the test database resolves its own host
        # through the same function and must reach the real address.
        if host != "claude.ai":
            return real_getaddrinfo(host, port, *a, **k)
        resolved.append(host)
        sockaddr = (
            (address, port) if family == socket.AF_INET else (address, port, 0, 0)
        )
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]

    real_connect = socket.socket.connect

    def spy_connect(self, sockaddr):
        connected.append(sockaddr)
        return real_connect(self, sockaddr)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", spy_connect)
    with pytest.raises(mcp_clients.ClientRefused, match="not a public one"):
        mcp_clients.client_for(db, CLAUDE_ID)
    assert resolved == ["claude.ai"]
    assert connected == []
    assert _stored(db) == 0


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirecting_answer_is_refused_and_not_followed(db, stub, status):
    s = stub(status=status, body=b"")
    with pytest.raises(mcp_clients.ClientRefused, match="redirects are not followed"):
        mcp_clients.client_for(db, CLAUDE_ID)
    assert s.asked == [CLAUDE_ID]
    assert _stored(db) == 0


def test_the_real_fetch_hands_a_redirect_back_instead_of_following_it(monkeypatch):
    """`_get` returns the 3xx itself: http.client follows nothing, and nothing is read
    from the Location."""
    seen = {}

    class Response:
        status = 302

        def getheader(self, name):
            return {"content-type": "text/html", "location": "https://10.0.0.5/x"}.get(
                name
            )

        def read(self, n):
            seen["read"] = n
            return b""

    def fake_fenced(url, method, body, headers, **kw):
        seen["url"], seen["method"], seen["kw"] = url, method, kw
        return kw["read"](Response())

    monkeypatch.setattr(mcp_clients.webhook, "fenced_request", fake_fenced)
    got = mcp_clients._get(CLAUDE_ID)
    assert got.status == 302
    assert seen["url"] == CLAUDE_ID and seen["method"] == "GET"
    assert seen["kw"]["allow_private"] is False
    assert seen["kw"]["deadline_seconds"] == 5.0
    assert seen["read"] == 64 * 1024 + 1


def test_an_oversized_document_is_refused(db, stub):
    body = json.dumps(_doc()).encode()
    stub(body=body + b" " * (64 * 1024 + 1 - len(body)))
    with pytest.raises(mcp_clients.ClientRefused, match="larger than 64 KiB"):
        mcp_clients.client_for(db, CLAUDE_ID)


def test_a_document_of_exactly_64_kib_is_read(db, stub):
    body = json.dumps(_doc()).encode()
    stub(body=body + b" " * (64 * 1024 - len(body)))
    assert mcp_clients.client_for(db, CLAUDE_ID).host == "claude.ai"


@pytest.mark.parametrize(
    ("content_type", "body", "words"),
    [
        ("text/html", b"<html>sign in</html>", "not application/json"),
        ("text/plain", json.dumps(_doc()).encode(), "not application/json"),
        ("", json.dumps(_doc()).encode(), "not application/json"),
        ("application/json", b"<html>sign in</html>", "not JSON"),
        ("application/json", b"\xff\xfe", "not JSON"),
        ("application/json", b'["a list"]', "not a JSON object"),
    ],
)
def test_a_document_that_is_not_json_is_refused(db, stub, content_type, body, words):
    stub(content_type=content_type, body=body)
    with pytest.raises(mcp_clients.ClientRefused, match=words):
        mcp_clients.client_for(db, CLAUDE_ID)


def test_json_with_a_charset_is_json(db, stub):
    stub(content_type="application/json; charset=utf-8")
    assert mcp_clients.client_for(db, CLAUDE_ID).name == "Claude"


@pytest.mark.parametrize(
    "named",
    [
        CLAUDE_ID + "/",
        CLAUDE_ID[:-1],
        CLAUDE_ID[:-1] + "A",  # metadatA
        "https://Claude.ai/oauth/mcp-oauth-client-metadata",
        "http://claude.ai/oauth/mcp-oauth-client-metadata",
        CLAUDE_ID + " ",
    ],
)
def test_a_client_id_one_byte_off_is_refused(db, stub, named):
    stub(body=json.dumps(_doc(client_id=named)).encode())
    with pytest.raises(mcp_clients.ClientRefused, match="different client_id"):
        mcp_clients.client_for(db, CLAUDE_ID)
    assert _stored(db) == 0


@pytest.mark.parametrize(
    ("overrides", "words"),
    [
        ({"client_name": ""}, "client_name"),
        ({"client_name": None}, "client_name"),
        ({"redirect_uris": []}, "redirect_uris"),
        ({"redirect_uris": "https://claude.ai/cb"}, "redirect_uris"),
        ({"redirect_uris": ["https://evil.example/cb"]}, "neither"),
        ({"redirect_uris": ["http://claude.ai/cb"]}, "neither"),
        ({"redirect_uris": ["https://claude.ai.evil.example/cb"]}, "neither"),
        ({"token_endpoint_auth_method": "client_secret_basic"}, "public clients"),
    ],
)
def test_a_document_with_bad_fields_is_refused(db, stub, overrides, words):
    stub(body=json.dumps(_doc(**overrides)).encode())
    with pytest.raises(mcp_clients.ClientRefused, match=words):
        mcp_clients.client_for(db, CLAUDE_ID)


def test_a_document_may_list_a_loopback_return_address(db, stub):
    uris = [CLAUDE_CALLBACK, "http://127.0.0.1:33418/callback"]
    stub(body=json.dumps(_doc(redirect_uris=uris)).encode())
    assert mcp_clients.client_for(db, CLAUDE_ID).redirect_uris == tuple(uris)


@pytest.mark.parametrize(
    "client_id",
    [
        "https://claude.ai",
        "https://claude.ai/",
        "http://claude.ai/oauth/mcp-oauth-client-metadata",
        "https://claude.ai/oauth/x#frag",
        "https://user@claude.ai/oauth/x",
        "https://claude.ai/oauth/../x",
        "claude-desktop",
        "",
    ],
)
def test_a_client_id_that_is_no_document_address_is_refused_without_a_fetch(
    db, stub, client_id
):
    s = stub()
    with pytest.raises(mcp_clients.ClientRefused):
        mcp_clients.client_for(db, client_id)
    assert s.asked == []


# --- The trusted and allowed host lists (section 5) -------------------------------------------


@pytest.mark.parametrize("allowed", ["chatgpt.com", "none", "localhost"])
def test_the_allowed_list_refuses_before_fetching(db, stub, allowed):
    s = stub()
    with (
        settings.override(mcp_allowed_client_hosts=allowed),
        pytest.raises(mcp_clients.ClientRefused, match="does not accept apps"),
    ):
        mcp_clients.client_for(db, CLAUDE_ID)
    assert s.asked == []
    assert _stored(db) == 0


def test_the_allowed_list_lets_its_hosts_ask(db, stub):
    stub()
    with settings.override(mcp_allowed_client_hosts="chatgpt.com, Claude.ai"):
        assert mcp_clients.client_for(db, CLAUDE_ID).host == "claude.ai"


def test_the_allowed_list_refuses_a_cached_app_too(db, stub):
    stub()
    mcp_clients.client_for(db, CLAUDE_ID)
    with (
        settings.override(mcp_allowed_client_hosts="chatgpt.com"),
        pytest.raises(mcp_clients.ClientRefused),
    ):
        mcp_clients.client_for(db, CLAUDE_ID)


def test_trusted_hosts_only_label_an_app(db, stub):
    stub()
    assert mcp_clients.client_for(db, CLAUDE_ID).verified is True
    with settings.override(mcp_trusted_client_hosts="zed.dev"):
        client = mcp_clients.client_for(db, CLAUDE_ID)
        assert client.verified is False and client.host == "claude.ai"
    assert settings.current().mcp_trusted_hosts() == {
        "claude.ai",
        "chatgpt.com",
        "vscode.dev",
        "zed.dev",
        "opencode.ai",
        "goose-docs.ai",
        "pi.dev",
    }


# --- Return addresses (section 5) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("registered", "asked", "matches"),
    [
        (CLAUDE_CALLBACK, CLAUDE_CALLBACK, True),
        (CLAUDE_CALLBACK, CLAUDE_CALLBACK + "/", False),
        (CLAUDE_CALLBACK, CLAUDE_CALLBACK.upper(), False),
        ("http://127.0.0.1/callback", "http://127.0.0.1:51004/callback", True),
        ("http://localhost:3000/callback", "http://localhost:51004/callback", True),
        ("http://[::1]/callback", "http://[::1]:51004/callback", True),
        ("http://127.0.0.1/callback", "http://127.0.0.1:0/callback", False),
        ("http://127.0.0.1/callback", "http://127.0.0.1:80/callback", False),
        ("http://127.0.0.1/callback", "http://localhost:51004/callback", False),
        ("http://127.0.0.1/callback", "http://127.0.0.1:51004/other", False),
        ("http://127.0.0.1/callback", "http://127.0.0.1:51004/callback?x=1", False),
        ("https://127.0.0.1/callback", "https://127.0.0.1:51004/callback", False),
    ],
)
def test_return_addresses_match_exactly_or_loopback_on_all_but_the_port(
    registered, asked, matches
):
    assert mcp_clients.redirect_matches([registered], asked) is matches


# --- Open registration (section 5) ------------------------------------------------------------


def register(client, body, address="198.51.100.7"):
    return client.post(
        "/oauth/register", headers=h(**{"x-real-ip": address}), json=body
    )


def test_claude_codes_registration_is_201_and_stored_by_its_checked_host(
    client, db, signs_in
):
    answer = register(
        client,
        {
            "client_name": "Claude Code (ream)",
            "redirect_uris": ["http://localhost:51004/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert answer.status_code == 201, answer.text
    body = answer.json()
    assert body["client_id"].startswith("reg_") and len(body["client_id"]) > 20
    assert body["token_endpoint_auth_method"] == "none"  # noqa: S105
    assert body["redirect_uris"] == ["http://localhost:51004/callback"]
    assert body["client_name"] == "Claude Code (ream)"
    assert "client_secret" not in body
    assert answer.headers["cache-control"] == "no-store"
    found = mcp_clients.client_for(db, body["client_id"])
    assert (found.kind, found.host, found.name) == (
        "registered",
        "localhost",
        "Claude Code (ream)",
    )
    assert found.verified is False


def test_the_11th_registration_in_an_hour_is_429(client, db, signs_in):
    body = {"redirect_uris": ["http://127.0.0.1:9000/cb"]}
    codes = [register(client, body).status_code for _ in range(10)]
    assert codes == [201] * 10
    eleventh = register(client, body)
    assert eleventh.status_code == 429
    assert eleventh.json()["error"] == "invalid_request"
    assert 0 < int(eleventh.headers["retry-after"]) <= 3601
    # Another address is not held back by this one.
    assert register(client, body, address="203.0.113.9").status_code == 201


def test_ipv6_registrations_count_by_their_64(client, signs_in):
    body = {"redirect_uris": ["http://[::1]:9000/cb"]}
    for n in range(10):
        assert register(client, body, f"2001:db8:5:6::{n + 1:x}").status_code == 201
    assert register(client, body, "2001:db8:5:6:ffff::1").status_code == 429
    assert register(client, body, "2001:db8:5:7::1").status_code == 201


def test_an_hour_later_the_address_may_register_again(db, signs_in):
    body = {"redirect_uris": ["http://localhost:9000/cb"]}
    then = datetime.now(UTC) - timedelta(minutes=61)
    for _ in range(10):
        mcp_clients.register(db, body, "198.51.100.8", then)
    assert mcp_clients.register(db, body, "198.51.100.8")["client_id"]


def test_the_store_full_is_503(client, db, signs_in, monkeypatch):
    monkeypatch.setattr(mcp_clients, "REGISTRATIONS_STORED", 2)
    body = {"redirect_uris": ["http://localhost:9000/cb"]}
    assert register(client, body, "198.51.100.1").status_code == 201
    assert register(client, body, "198.51.100.2").status_code == 201
    full = register(client, body, "198.51.100.3")
    assert full.status_code == 503
    assert full.json()["error"] == "temporarily_unavailable"


def test_one_never_used_is_gone_after_24_hours(db, signs_in):
    body = {"redirect_uris": ["http://localhost:9000/cb"]}
    old = mcp_clients.register(
        db, body, "198.51.100.4", datetime.now(UTC) - timedelta(hours=25)
    )["client_id"]
    with pytest.raises(mcp_clients.ClientRefused, match="lapsed"):
        mcp_clients.client_for(db, old)
    mcp_clients.register(db, body, "198.51.100.5")
    with db.connect() as conn:
        left = conn.execute("select id from mcp_client").fetchall()
    assert old not in [r[0] for r in left] and len(left) == 1


def test_one_used_is_kept_past_24_hours(db, signs_in):
    body = {"redirect_uris": ["http://localhost:9000/cb"]}
    then = datetime.now(UTC) - timedelta(hours=25)
    kept = mcp_clients.register(db, body, "198.51.100.6", then)["client_id"]
    with db.connect() as conn:
        conn.execute(
            "update mcp_client set last_used_at = %s where id = %s", (then, kept)
        )
    mcp_clients.register(db, body, "198.51.100.6")
    assert mcp_clients.client_for(db, kept).host == "localhost"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({}, "invalid_redirect_uri"),
        ({"redirect_uris": []}, "invalid_redirect_uri"),
        ({"redirect_uris": ["https://app.example/cb"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["http://app.example/cb"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["http://localhost:80/cb"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["http://127.0.0.1:0/cb"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["http://10.0.0.5:9000/cb"]}, "invalid_redirect_uri"),
        (
            {
                "redirect_uris": ["http://localhost:9000/cb"],
                "token_endpoint_auth_method": "client_secret_basic",
            },
            "invalid_client_metadata",
        ),
        (
            {
                "redirect_uris": ["http://localhost:9000/cb"],
                "grant_types": ["client_credentials"],
            },
            "invalid_client_metadata",
        ),
        (
            {
                "redirect_uris": ["http://localhost:9000/cb"],
                "response_types": ["token"],
            },
            "invalid_client_metadata",
        ),
        (
            {"redirect_uris": ["http://localhost:9000/cb"], "client_name": 7},
            "invalid_client_metadata",
        ),
        (["http://localhost:9000/cb"], "invalid_client_metadata"),
    ],
)
def test_a_bad_registration_is_400_and_stores_nothing(
    client, db, signs_in, body, error
):
    answer = register(client, body)
    assert answer.status_code == 400
    assert answer.json()["error"] == error
    assert answer.json()["error_description"]
    assert _stored(db) == 0


def test_a_registration_that_is_not_json_is_400(client, signs_in):
    answer = client.post(
        "/oauth/register", headers=h(), content=b"redirect_uris=http://localhost/cb"
    )
    assert answer.status_code == 400
    assert answer.json()["error"] == "invalid_client_metadata"


def test_an_https_return_host_may_be_listed_for_registration(client, db, signs_in):
    with settings.override(mcp_self_registration_redirects="localhost, app.example"):
        ok = register(client, {"redirect_uris": ["https://app.example/cb"]})
        mixed = register(
            client,
            {"redirect_uris": ["https://app.example/cb", "http://localhost:9/cb"]},
        )
    assert ok.status_code == 201
    assert mcp_clients.client_for(db, ok.json()["client_id"]).host == "app.example"
    assert mixed.status_code == 400


def test_the_allowed_list_refuses_a_registration_before_storing(client, db, signs_in):
    with settings.override(mcp_allowed_client_hosts="claude.ai"):
        answer = register(client, {"redirect_uris": ["http://localhost:9000/cb"]})
    assert answer.status_code == 400
    assert "does not accept apps from localhost" in answer.json()["error_description"]
    assert _stored(db) == 0


def test_a_registered_app_outside_a_later_allowed_list_is_refused(db, signs_in):
    made = mcp_clients.register(
        db, {"redirect_uris": ["http://localhost:9000/cb"]}, "198.51.100.9"
    )["client_id"]
    with (
        settings.override(mcp_allowed_client_hosts="claude.ai"),
        pytest.raises(mcp_clients.ClientRefused, match="localhost"),
    ):
        mcp_clients.client_for(db, made)


def test_an_unknown_registered_id_is_refused(db):
    with pytest.raises(mcp_clients.ClientRefused):
        mcp_clients.client_for(db, "reg_nobody")


def test_the_registration_body_is_capped(client, signs_in):
    big = {"redirect_uris": ["http://localhost:9000/cb"], "x": "y" * (64 * 1024)}
    assert register(client, big).status_code == 400


def test_the_stored_names_are_json_on_both_databases(db, signs_in):
    made = mcp_clients.register(
        db,
        {"redirect_uris": ["http://localhost:9000/cb"], "client_name": "Zürich"},
        "198.51.100.10",
    )["client_id"]
    with db.connect() as conn:
        row = conn.execute(
            "select redirect_uris, name, registered_from from mcp_client where id = %s",
            (made,),
        ).fetchone()
    assert row[0] == ["http://localhost:9000/cb"]
    assert row[1] == "Zürich" and row[2] == "198.51.100.10"
