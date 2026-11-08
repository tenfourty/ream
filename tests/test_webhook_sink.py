"""The generic signed webhook sink (core-and-plugins design, 4.3): REAM_SINKS_FILE, the
Ed25519-signed JSON POST, and what it refuses to talk to."""

import base64
import http.server
import json
import threading
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from ream import envnames, events, settings, sinks, webhook

AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


@pytest.fixture()
def key(tmp_path):
    k = Ed25519PrivateKey.generate()
    (tmp_path / "k.pem").write_bytes(_pem(k))
    return k


def _sinks_file(tmp_path, body: str):
    path = tmp_path / "sinks.toml"
    path.write_text(body)
    return str(path)


def _entry(**over):
    fields = {
        "name": "hook-a",
        "kinds": '["decision_answered", "set_aside"]',
        "url": '"https://hooks.example/in"',
        "signing_key_file": '"k.pem"',
        "key_id": '"k1"',
    }
    for k, v in over.items():
        if v is None:
            fields.pop(k)
        else:
            fields[k] = v
    out = ["[[sink]]"]
    for k, v in fields.items():
        out.append(f'{k} = "{v}"' if k == "name" else f"{k} = {v}")
    return "\n".join(out) + "\n"


def _event(**over):
    base = {
        "kind": "decision_answered",
        "artifact_id": "a1",
        "agent": "seat-a",
        "actor": "owner",
        "summary": "answered",
        "id": "ev_1",
        "data": {"letter": "b"},
    }
    base.update(over)
    return events.Event(**base)


# --- the file --------------------------------------------------------------------


def test_a_valid_file_loads(tmp_path, key):
    path = _sinks_file(tmp_path, _entry() + _entry(name="hook-b", key_id='"k2"'))
    hooks = webhook.load_file(path)
    assert [h.name for h in hooks] == ["hook-a", "hook-b"]
    assert hooks[0].kinds == ("decision_answered", "set_aside")
    assert hooks[0].key_id == "k1"
    assert "k.pem" not in repr(hooks[0]) and "PRIVATE" not in repr(hooks[0])


@pytest.mark.parametrize(
    "mutation, words",
    [
        ({"name": "Bad Name"}, "name"),
        ({"kinds": '["no_such_kind"]'}, "no_such_kind"),
        ({"kinds": "[]"}, "kinds"),
        ({"kinds": '"decision_answered"'}, "kinds"),
        ({"url": '"http://hooks.example/in"'}, "https"),
        ({"url": '"https://user:pw@hooks.example/in"'}, "credentials"),
        ({"url": '"ftp://hooks.example/in"'}, "https"),
        ({"url": None}, "url"),
        ({"key_id": None}, "key_id"),
        ({"signing_key_file": None}, "signing_key_file"),
        ({"signing_key_file": '"missing.pem"'}, "signing_key_file"),
        ({"surprise": "1"}, "surprise"),
    ],
)
def test_a_bad_entry_stops_with_its_name_and_field(tmp_path, key, mutation, words):
    path = _sinks_file(tmp_path, _entry(**mutation))
    with pytest.raises(webhook.WebhookConfigError) as err:
        webhook.load_file(path)
    assert words in str(err.value)
    assert "hook-a" in str(err.value) or "Bad Name" in str(err.value)


def test_a_bad_entry_never_echoes_a_url_or_a_key(tmp_path, key):
    path = _sinks_file(tmp_path, _entry(url='"https://user:topsecret@hooks.example/x"'))
    with pytest.raises(webhook.WebhookConfigError) as err:
        webhook.load_file(path)
    assert "topsecret" not in str(err.value)


def test_a_duplicate_name_is_refused(tmp_path, key):
    path = _sinks_file(tmp_path, _entry() + _entry())
    with pytest.raises(webhook.WebhookConfigError, match="twice"):
        webhook.load_file(path)


def test_a_key_that_is_not_ed25519_is_refused_without_quoting_it(tmp_path):
    rsa = generate_private_key(65537, 2048)
    (tmp_path / "k.pem").write_bytes(_pem(rsa))
    with pytest.raises(webhook.WebhookConfigError, match="Ed25519") as err:
        webhook.load_file(_sinks_file(tmp_path, _entry()))
    assert "PRIVATE" not in str(err.value)


def test_garbage_in_the_key_file_is_not_quoted(tmp_path):
    (tmp_path / "k.pem").write_text("not-a-key-hunter2")
    with pytest.raises(webhook.WebhookConfigError) as err:
        webhook.load_file(_sinks_file(tmp_path, _entry()))
    assert "hunter2" not in str(err.value)


def test_toml_errors_and_stray_tables_are_refused(tmp_path, key):
    with pytest.raises(webhook.WebhookConfigError, match="TOML"):
        webhook.load_file(_sinks_file(tmp_path, "[[sink"))
    with pytest.raises(webhook.WebhookConfigError, match="other"):
        webhook.load_file(_sinks_file(tmp_path, "[other]\nx = 1\n"))


def test_an_unreadable_file_is_a_config_error(tmp_path):
    with pytest.raises(webhook.WebhookConfigError, match="REAM_SINKS_FILE"):
        webhook.load_file(str(tmp_path / "nope.toml"))


# --- the setting, under both names -------------------------------------------------


def test_the_setting_reads_under_either_name(monkeypatch):
    monkeypatch.delenv("REAM_SINKS_FILE", raising=False)
    monkeypatch.setenv("ARTIFACTS_SINKS_FILE", "/x/old.toml")
    assert settings.current().sinks_file == "/x/old.toml"
    monkeypatch.setenv("REAM_SINKS_FILE", "/x/old.toml")
    assert settings.current().sinks_file == "/x/old.toml"
    monkeypatch.setenv("REAM_SINKS_FILE", "/y/new.toml")
    with pytest.raises(envnames.EnvConflict):
        settings.current()


# --- registering ---------------------------------------------------------------------


def test_register_all_adds_each_sink_to_the_registry(tmp_path, key):
    path = _sinks_file(tmp_path, _entry())
    with sinks.isolated(), settings.override(sinks_file=path):
        assert webhook.register_all() == ["hook-a"]
        found = {s.name: s for s in sinks.registered()}
        assert found["hook-a"].kinds == ("decision_answered", "set_aside")
        assert found["hook-a"].delivery == "outbox"
        assert [s.name for s in sinks.matching("set_aside")] == ["hook-a"]


def test_no_setting_registers_nothing():
    with sinks.isolated(), settings.override(sinks_file=""):
        assert webhook.register_all() == []
        assert sinks.registered() == []


def test_a_name_that_is_already_a_sink_stops_the_start(tmp_path, key):
    path = _sinks_file(tmp_path, _entry(name="partner"))
    taken = sinks.Sink("partner", ("asked",), deliver=lambda e: None)
    with (
        sinks.isolated(taken),
        settings.override(sinks_file=path),
        pytest.raises(webhook.WebhookConfigError, match="already"),
    ):
        webhook.register_all()


@pytest.mark.parametrize("name", ["nudge", "partner"])
def test_a_built_in_name_is_refused_at_registration(name):
    with pytest.raises(ValueError, match="already registered"):
        sinks.register(sinks.Sink(name, ("asked",), deliver=lambda e: None))
    # the registry is still sound: nothing raises later, in an emitting call
    assert [s.name for s in sinks.registered()].count(name) == 1


@pytest.mark.parametrize("name", ["nudge", "partner"])
def test_a_webhook_named_like_a_built_in_stops_the_start(tmp_path, key, name):
    path = _sinks_file(tmp_path, _entry(name=name))
    with (
        settings.override(sinks_file=path),
        pytest.raises(webhook.WebhookConfigError, match="already"),
    ):
        webhook.register_all()
    assert [s.name for s in sinks.registered()].count(name) == 1


@pytest.mark.parametrize(
    "bad", ['"k1\\nX: y"', '"k\\u00e9"', '""', '"' + "a" * 65 + '"', '"a b"']
)
def test_a_key_id_that_cannot_ride_in_a_header_is_refused(tmp_path, key, bad):
    path = _sinks_file(tmp_path, _entry(key_id=bad))
    with pytest.raises(webhook.WebhookConfigError, match="key_id"):
        webhook.load_file(path)


def test_a_webhook_error_stores_its_message(tmp_path, key):
    assert webhook.WebhookError.stored_message is True


# --- the request ---------------------------------------------------------------------


def test_the_envelope_is_signed_over_its_exact_bytes(tmp_path, key):
    hook = webhook.load_file(_sinks_file(tmp_path, _entry()))[0]
    body, headers = webhook.build_request(hook, _event(), AT)
    key.public_key().verify(base64.b64decode(headers[webhook.SIGNATURE_HEADER]), body)
    assert headers[webhook.KEY_ID_HEADER] == "k1"
    assert headers[webhook.EVENT_ID_HEADER] == "ev_1"
    assert headers["Content-Type"] == "application/json"
    doc = json.loads(body)
    assert doc["key_id"] == "k1"
    assert doc["sent_at"] == "2026-01-02T03:04:05+00:00"
    assert doc["event"]["kind"] == "decision_answered"
    assert doc["event"]["id"] == "ev_1"
    assert doc["event"]["data"] == {"letter": "b"}


def test_a_retry_is_re_signed_with_a_fresh_sent_at(tmp_path, key):
    hook = webhook.load_file(_sinks_file(tmp_path, _entry()))[0]
    a, _ = webhook.build_request(hook, _event(), AT)
    b, _ = webhook.build_request(hook, _event(), datetime(2026, 1, 2, 3, 5, tzinfo=UTC))
    assert a != b
    assert json.loads(a)["event"] == json.loads(b)["event"]


def test_an_oversize_event_is_refused_not_sent(tmp_path, key):
    hook = webhook.load_file(_sinks_file(tmp_path, _entry()))[0]
    big = _event(data={"blob": "x" * (webhook.MAX_BODY_BYTES + 1)})
    with pytest.raises(webhook.WebhookError, match="too large"):
        webhook.build_request(hook, big, AT)


def test_deliver_raises_on_a_non_2xx_and_not_on_a_2xx(tmp_path, key):
    hook = webhook.load_file(_sinks_file(tmp_path, _entry()))[0]
    sent = []
    webhook.deliver(hook, _event(), post=lambda *a: sent.append(a) or 204)
    assert len(sent) == 1
    for status in (301, 400, 500):
        with pytest.raises(webhook.WebhookError, match=f"HTTP {status}"):
            webhook.deliver(hook, _event(), post=lambda *a, s=status: s)


# --- where it may connect -------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",  # noqa: S104
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:10.0.0.1",
        "100.64.0.1",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b::8.8.8.8",
        "64:ff9b:1::1",
        "::127.0.0.1",
        "::8.8.8.8",
        "2002:7f00:1::1",
        "2002:a9fe:a9fe::1",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
        "fec0::1",
        "::ffff:0:a9fe:a9fe",
        "4000::1",
        "100::1",
    ],
)
def test_private_and_special_addresses_are_refused_by_default(ip):
    assert not webhook.address_allowed(ip, allow_private=False)
    assert webhook.address_allowed(ip, allow_private=True)


def test_public_addresses_are_allowed():
    assert webhook.address_allowed("93.184.216.34", allow_private=False)
    assert webhook.address_allowed("::ffff:93.184.216.34", allow_private=False)
    assert webhook.address_allowed("2606:2800:220:1:248:1893:25c8:1946", False)


class _Receiver:
    """A local HTTP server that records what it is sent."""

    def __init__(self, status=204, location=None):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                outer.requests.append(
                    (self.path, dict(self.headers), self.rfile.read(n))
                )
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def receiver():
    made = []

    def make(**kw):
        r = _Receiver(**kw)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


def _local_hook(tmp_path, port, **extra):
    lines = _entry(
        url=f'"http://127.0.0.1:{port}/in?x=1"',
        **{k: str(v).lower() for k, v in extra.items()},
    )
    return webhook.load_file(_sinks_file(tmp_path, lines))[0]


def test_loopback_is_refused_without_allow_private(tmp_path, key, receiver):
    r = receiver()
    hook = _local_hook(tmp_path, r.port, allow_http="true")
    with pytest.raises(webhook.WebhookError, match="address"):
        webhook.deliver(hook, _event())
    assert r.requests == []


def test_http_needs_allow_http(tmp_path, key):
    with pytest.raises(webhook.WebhookConfigError, match="allow_http"):
        _local_hook(tmp_path, 9, allow_private="true")


def test_a_real_delivery_with_the_flags_set(tmp_path, key, receiver):
    r = receiver()
    hook = _local_hook(tmp_path, r.port, allow_http="true", allow_private="true")
    webhook.deliver(hook, _event())
    ((path, headers, body),) = r.requests
    assert path == "/in?x=1"
    key.public_key().verify(base64.b64decode(headers[webhook.SIGNATURE_HEADER]), body)
    assert headers[webhook.KEY_ID_HEADER] == "k1"


def test_a_redirect_is_not_followed(tmp_path, key, receiver):
    target = receiver()
    first = receiver(status=307, location=f"http://127.0.0.1:{target.port}/steal")
    hook = _local_hook(tmp_path, first.port, allow_http="true", allow_private="true")
    with pytest.raises(webhook.WebhookError, match="HTTP 307"):
        webhook.deliver(hook, _event())
    assert len(first.requests) == 1
    assert target.requests == []


def test_a_server_that_does_not_answer_times_out(tmp_path, key, monkeypatch):
    import socket

    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)
    monkeypatch.setattr(webhook, "REQUEST_TIMEOUT_SECONDS", 0.3)
    try:
        hook = _local_hook(
            tmp_path,
            silent.getsockname()[1],
            allow_http="true",
            allow_private="true",
        )
        with pytest.raises(webhook.WebhookError, match="timed out"):
            webhook.deliver(hook, _event())
    finally:
        silent.close()


def test_a_refused_connection_names_no_url(tmp_path, key):
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    hook = _local_hook(tmp_path, port, allow_http="true", allow_private="true")
    with pytest.raises(webhook.WebhookError) as err:
        webhook.deliver(hook, _event())
    assert "127.0.0.1" not in str(err.value)


def test_the_checked_address_is_the_one_connected_to(monkeypatch):
    """One lookup; a refused answer is never retried by name, so a second lookup that
    would answer differently (rebinding) is never made."""
    import socket

    calls = []

    def fake(host, port, **kw):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    create = webhook._pinned_connect(False)
    with pytest.raises(webhook.WebhookError, match="refused address"):
        create(("rebind.example", 443), 1.0)
    assert calls == ["rebind.example"]


def test_an_ipv6_literal_url_gets_an_explicit_port(tmp_path, key, monkeypatch):
    seen = {}

    class Fake:
        def __init__(self, host, port=None, timeout=None):
            seen["args"] = (host, port)

        def request(self, *a):
            raise OSError

        def close(self):
            pass

    monkeypatch.setattr(webhook.http.client, "HTTPSConnection", Fake)
    monkeypatch.setattr(webhook.http.client, "HTTPConnection", Fake)
    for url, want in (
        ("https://[2001:db8::1]/in", ("2001:db8::1", 443)),
        ("http://[2001:db8::1]/in", ("2001:db8::1", 80)),
        ("https://[2001:db8::1]:8443/in", ("2001:db8::1", 8443)),
    ):
        hook = webhook.load_file(
            _sinks_file(tmp_path, _entry(url=f'"{url}"', allow_http="true"))
        )[0]
        with pytest.raises(webhook.WebhookError):
            webhook.http_post(hook, b"{}", {})
        assert seen["args"] == want


def test_a_connect_gets_what_is_left_of_the_deadline(monkeypatch):
    import socket

    timeouts = []

    class Sock:
        def __init__(self, *a):
            pass

        def settimeout(self, t):
            timeouts.append(t)

        def connect(self, addr):
            raise TimeoutError

        def close(self):
            pass

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda h, p, **kw: (
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))] * 3
        ),
    )
    monkeypatch.setattr(socket, "socket", Sock)
    now = webhook.time.monotonic()
    create = webhook._pinned_connect(False, now + 4.0)
    with pytest.raises(webhook.WebhookError, match="timed out"):
        create(("h.example", 443), 10.0)
    assert timeouts and all(t <= 4.0 for t in timeouts)
    # past the deadline no further attempt is made
    timeouts.clear()
    with pytest.raises(webhook.WebhookError, match="timed out"):
        webhook._pinned_connect(False, now - 1)(("h.example", 443), 10.0)
    assert timeouts == []


def test_a_server_that_drips_bytes_is_cut_at_the_deadline(tmp_path, key, monkeypatch):
    import socket
    import time

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def drip():
        conn, _ = srv.accept()
        conn.recv(65536)
        while not stop.is_set():
            try:
                conn.send(b"H")  # never finishes the status line
            except OSError:
                return
            time.sleep(0.05)

    threading.Thread(target=drip, daemon=True).start()
    monkeypatch.setattr(webhook, "DEADLINE_SECONDS", 0.5)
    try:
        hook = _local_hook(
            tmp_path, srv.getsockname()[1], allow_http="true", allow_private="true"
        )
        started = time.monotonic()
        with pytest.raises(webhook.WebhookError, match="timed out"):
            webhook.deliver(hook, _event())
        assert time.monotonic() - started < 3
    finally:
        stop.set()
        srv.close()


def _tls_server_files(tmp_path):
    import datetime as dt

    from cryptography import x509
    from cryptography.x509.oid import NameOID

    k = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(k.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(k, None)
    )
    (tmp_path / "srv.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "srv.key").write_bytes(_pem(k))
    return str(tmp_path / "srv.crt"), str(tmp_path / "srv.key")


@pytest.mark.parametrize("phase", ["handshake", "response"])
def test_a_tls_server_that_drips_is_cut_at_the_deadline(
    tmp_path, key, monkeypatch, phase
):
    """The deadline timer must reach the TLS socket in use, not the one wrapping detached."""
    import socket
    import ssl
    import time

    crt, keyf = _tls_server_files(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, keyf)
    monkeypatch.setattr(
        ssl,
        "_create_default_https_context",
        lambda: _unverified_client_context(),
    )
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def serve():
        raw, _ = srv.accept()
        try:
            if phase == "handshake":
                raw.recv(65536)
                # A valid handshake record header announcing 16 KiB, then the body a
                # byte at a time: the client is mid-handshake and keeps waiting. (Bytes
                # that are not a record header fail at once with a TLS error.)
                raw.send(b"\x16\x03\x03\x40\x00")
                while not stop.is_set():
                    raw.send(b"\x00")
                    time.sleep(0.05)
            else:
                conn = ctx.wrap_socket(raw, server_side=True)
                conn.recv(65536)
                while not stop.is_set():
                    conn.send(b"H")  # never finishes the status line
                    time.sleep(0.05)
        except (OSError, ssl.SSLError):
            return

    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setattr(webhook, "DEADLINE_SECONDS", 0.7)
    try:
        hook = webhook.load_file(
            _sinks_file(
                tmp_path,
                _entry(
                    url=f'"https://127.0.0.1:{srv.getsockname()[1]}/in"',
                    allow_private="true",
                ),
            )
        )[0]
        started = time.monotonic()
        with pytest.raises(webhook.WebhookError, match="timed out"):
            webhook.deliver(hook, _event())
        assert time.monotonic() - started < 4
    finally:
        stop.set()
        srv.close()


def _unverified_client_context():
    import ssl

    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c
