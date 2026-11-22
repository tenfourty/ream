"""The internal port: a second listener that serves only /healthz and the partner inbound
endpoint (`ream.internal_port`). Everything else there is a plain 404 whatever the Host
header says, and port 8000's app is unchanged."""

import asyncio
import base64
import json
import signal
import socket
import threading
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from ream import internal_port, keys, partner, partner_in, serve, settings
from ream.app import create_app

ROOT = Path(__file__).resolve().parent.parent
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
    assert 200 <= ok.status_code < 300, ok.text
    assert ok.status_code == via_main.status_code
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
        # HEAD is not served on /healthz, as on the main port (a 405 there)
        assert c.head("/healthz").status_code in (404, 405)
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


def fetch(port, path, host):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers={"Host": host}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return None  # nothing is listening


def test_the_real_listener_runs_on_the_main_loop_and_serves_only_the_internal_app(
    db, inbound
):
    port = free_port()

    async def scenario():
        term = signal.getsignal(signal.SIGTERM)
        intr = signal.getsignal(signal.SIGINT)
        listener = internal_port.InternalListener(
            internal_port.create_internal_app(db, inbound), "127.0.0.1", port
        )
        before = {t.ident for t in threading.enumerate()}
        await listener.start()
        loop = asyncio.get_running_loop()
        # The same loop as the caller's, and no thread of its own.
        assert listener._task.get_loop() is loop
        assert {t.ident for t in threading.enumerate()} - before <= set()
        assert signal.getsignal(signal.SIGTERM) is term
        assert signal.getsignal(signal.SIGINT) is intr

        async def get(path, host):
            return await loop.run_in_executor(None, fetch, port, path, host)

        assert await get("/healthz", "probe") == 200
        assert await get("/", GALLERY) == 404
        assert await get("/api/v1/pages", API) == 404
        await listener.stop()
        assert listener._task.done()
        assert await get("/healthz", "probe") is None

    asyncio.run(scenario())


def drive_lifespan(app):
    """Run `app` (an ASGI app) through lifespan; returns `(startup, shutdown, finish)`
    coroutines: `startup()` sends lifespan.startup and waits for its answer, `shutdown()`
    the same for shutdown, `finish()` awaits the app's end."""
    inbox, outbox = asyncio.Queue(), asyncio.Queue()
    task = asyncio.ensure_future(app({"type": "lifespan"}, inbox.get, outbox.put))

    async def startup():
        await inbox.put({"type": "lifespan.startup"})
        return await outbox.get()

    async def shutdown():
        await inbox.put({"type": "lifespan.shutdown"})
        return await outbox.get()

    return startup, shutdown, task


def test_the_internal_port_answers_only_between_lifespan_startup_and_shutdown(
    db, inbound, monkeypatch
):
    monkeypatch.setattr(serve.signal, "signal", lambda *a, **k: None)
    port = free_port()
    listener = internal_port.InternalListener(
        internal_port.create_internal_app(db, inbound), "127.0.0.1", port
    )

    async def main_app(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            else:
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def scenario():
        loop = asyncio.get_running_loop()

        async def get():
            return await loop.run_in_executor(None, fetch, port, "/healthz", "probe")

        startup, shutdown, task = drive_lifespan(
            serve._arm_shutdown_signals(main_app, listener)
        )
        assert await get() is None  # bound, but nothing answers before startup
        assert (await startup())["type"] == "lifespan.startup.complete"
        assert await get() == 200
        assert (await shutdown())["type"] == "lifespan.shutdown.complete"
        await task
        assert listener._task.done()  # shutdown stopped it
        assert await get() is None

    asyncio.run(scenario())


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


def test_the_main_port_constant_matches_the_dockerfile_cmd():
    cmd = next(
        line
        for line in (ROOT / "Dockerfile").read_text().splitlines()
        if line.startswith("CMD ")
    )
    argv = json.loads(cmd.removeprefix("CMD "))
    assert int(argv[argv.index("--port") + 1]) == settings.MAIN_PORT


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
        assert internal_port.maybe_create(db, inbound, current) is None
