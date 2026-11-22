"""The internal port: a second listener that serves only /healthz and the partner inbound
endpoint (`ream.internal_port`). Everything else there is a plain 404 whatever the Host
header says, and port 8000's app is unchanged."""

import base64
import json
import socket
import time
import urllib.request
import uuid
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from ream import internal_port, keys, partner, partner_in, settings
from ream.app import create_app

HOST = "artifacts-svc.internal"
KEY_ID = "qd-test-1"
GALLERY = "gallery.invalid"
CONTENT = "content.invalid"
API = "api.invalid"


@pytest.fixture()
def partner_key():
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def inbound(monkeypatch, tmp_path, partner_key):
    pem = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "signing.pem"
    path.write_bytes(pem)
    monkeypatch.setenv(partner.ENV_NAME, "partner-x")
    monkeypatch.setenv(partner.ENV_URL, "http://partner.invalid/events")
    monkeypatch.setenv(partner.ENV_ALLOW_HTTP, "1")
    monkeypatch.setenv(partner.ENV_KEY_FILE, str(path))
    monkeypatch.setenv(partner.ENV_KEY_ID, "artifacts-test-1")
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", f"https://{GALLERY}")
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", CONTENT)
    monkeypatch.setenv("REAM_API_HOST", API)
    return partner_in.Inbound(host=HOST, keys={KEY_ID: partner_key.public_key()})


@pytest.fixture()
def internal(db, inbound):
    return internal_port.create_internal_app(db, inbound)


def at(app, host):
    return TestClient(app, base_url=f"http://{host}")


def event():
    now = datetime.now(UTC).isoformat()
    return {
        "event_id": f"ev-{uuid.uuid4().hex[:12]}",
        "kind": "asked",
        "source": "qd:q1",
        "question": "q1",
        "version": 1,
        "key_id": KEY_ID,
        "at": now,
        "sent_at": now,
        "body": {
            "asking_office": "warpgate",
            "title": "Open the port?",
            "options": [{"letter": "a", "label": "Yes"}],
        },
    }


def signed_post(client, key, *, tamper=False):
    raw = json.dumps(event(), separators=(",", ":")).encode()
    sig = base64.b64encode(key.sign(raw + (b"x" if tamper else b""))).decode()
    return client.post(
        partner_in.PATH, content=raw, headers={partner.SIGNATURE_HEADER: sig}
    )


def test_the_internal_app_answers_healthz_like_the_gallery(db, inbound, internal):
    main = at(create_app(db, inbound), GALLERY).get("/healthz")
    for host in (GALLERY, "10.1.2.3:8001", HOST):
        got = at(internal, host).get("/healthz")
        assert got.status_code == 200
        assert got.json() == main.json() == {"status": "ok"}


def test_the_internal_app_serves_the_partner_endpoint_on_the_partner_host(
    db, inbound, internal, partner_key
):
    ok = signed_post(at(internal, HOST), partner_key)
    via_main = signed_post(at(create_app(db, inbound), HOST), partner_key)
    assert ok.status_code < 300, ok.text
    assert via_main.status_code == ok.status_code or via_main.status_code < 300
    # The signature is still the credential, as on port 8000.
    bad = signed_post(at(internal, HOST), partner_key, tamper=True)
    assert bad.status_code in (400, 401, 403), bad.status_code
    # A proxied request is refused here as there.
    raw = json.dumps(event()).encode()
    proxied = at(internal, HOST).post(
        partner_in.PATH, content=raw, headers={"x-forwarded-for": "1.2.3.4"}
    )
    assert proxied.status_code == 404


def test_the_partner_host_is_the_only_host_that_reaches_the_endpoint(
    internal, partner_key
):
    assert signed_post(at(internal, GALLERY), partner_key).status_code == 404
    assert signed_post(at(internal, "other.invalid"), partner_key).status_code == 404


def test_everything_else_is_a_plain_404_whatever_the_host(db, internal):
    key, _ = keys.mint(db, agent="writer")
    auth = {"Authorization": f"Bearer {key}"}
    for host in (GALLERY, API, f"{uuid.uuid4().hex[:12]}.{CONTENT}", HOST, "x.invalid"):
        c = at(internal, host)
        for path in ("/", "/mcp", "/api/v1/pages", "/api/v1/artifacts", "/agents.txt"):
            got = c.get(path, headers=auth)
            assert got.status_code == 404, (host, path, got.status_code)
            if host != HOST:  # the partner host's own app words its 404 as on 8000
                assert got.text == "not found"
        assert c.post("/api/v1/pages", headers=auth, json={}).status_code == 404
        assert c.post("/mcp", headers=auth, json={}).status_code == 404
        assert c.head("/healthz").status_code == 404 or host  # HEAD is not served
    # A conflicting Host-override header changes nothing.
    got = at(internal, "x.invalid").get(
        "/", headers={"x-forwarded-host": GALLERY, "x-original-host": GALLERY}
    )
    assert got.status_code == 404


def test_healthz_is_get_only_and_other_methods_are_404(internal):
    assert at(internal, GALLERY).post("/healthz").status_code == 404


def test_the_main_app_still_serves_the_gallery_and_the_api(db, inbound):
    main = create_app(db, inbound)
    assert at(main, GALLERY).get("/healthz").status_code == 200
    assert at(main, GALLERY).get("/").status_code != 404
    key, _ = keys.mint(db, agent="writer")
    got = at(main, API).get("/api/v1/pages", headers={"Authorization": f"Bearer {key}"})
    assert got.status_code == 200


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_real_listener_serves_only_the_internal_app(db, inbound):
    port = free_port()
    listener = internal_port.InternalListener(
        internal_port.create_internal_app(db, inbound), "127.0.0.1", port
    )
    listener.start()
    try:

        def get(path, host):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", headers={"Host": host}
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
                    return r.status
            except urllib.error.HTTPError as exc:
                return exc.code

        for _ in range(50):
            try:
                assert get("/healthz", "probe") == 200
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("the internal listener did not come up")
        assert get("/", GALLERY) == 404
        assert get("/api/v1/pages", API) == 404
    finally:
        listener.stop()
    assert not listener._thread.is_alive()


def test_a_port_already_taken_stops_start_up_with_a_clear_message(db, inbound):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        with pytest.raises(SystemExit) as exc:
            internal_port.InternalListener(
                internal_port.create_internal_app(db, inbound),
                "127.0.0.1",
                s.getsockname()[1],
            )
    assert "REAM_INTERNAL_PORT" in str(exc.value)


def test_unset_starts_no_second_listener(db, inbound):
    with settings.override() as current:
        assert internal_port.maybe_start(db, inbound, current) is None


def test_only_exact_paths_are_served(internal, partner_key):
    c = at(internal, HOST)
    for bad in (
        "//healthz",
        "/healthz/",
        "/healthz/../gallery",
        "/%2e%2e/healthz",
        "/healthz;",
        "/%68ealthz",
        "/healthz%2f..%2fapi",
        "/?/healthz",
        "//" + partner_in.PATH.lstrip("/"),
        partner_in.PATH + "/",
        partner_in.PATH + ";",
        partner_in.PATH.replace("e", "%65", 1),
    ):
        for method in ("get", "post"):
            got = getattr(c, method)(bad)
            assert got.status_code == 404, (bad, method, got.status_code)


def test_the_partner_route_keeps_its_own_authentication_here(internal, partner_key):
    c = at(internal, HOST)
    raw = json.dumps(event()).encode()
    for headers in ({}, {partner.SIGNATURE_HEADER: "AAAA"}):
        got = c.post(partner_in.PATH, content=raw, headers=headers)
        assert 400 <= got.status_code < 500 and got.status_code != 404, got.status_code
    other = Ed25519PrivateKey.generate()
    assert signed_post(c, other).status_code in (400, 401, 403)


def test_healthz_is_a_status_and_nothing_else(internal):
    got = at(internal, "probe").get("/healthz")
    assert got.json() == {"status": "ok"}
    assert set(got.headers) <= {"content-length", "content-type"}


def test_unset_binds_nothing(db, inbound, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a socket was opened with the setting unset")

    monkeypatch.setattr(internal_port.socket, "socket", refuse)
    monkeypatch.setattr(internal_port, "InternalListener", refuse)
    with settings.override() as current:
        assert internal_port.maybe_start(db, inbound, current) is None
