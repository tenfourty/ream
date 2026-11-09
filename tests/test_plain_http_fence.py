"""Plain http through the address fence goes to a private or loopback address only
(webhook.fenced_request(require_private=True)); the oidc back channel relies on it."""

from __future__ import annotations

import socket
import threading

import pytest

from ream import webhook


@pytest.mark.parametrize(
    ("ip", "private"),
    [
        ("10.1.2.3", True),
        ("192.168.0.1", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("fd00::1", True),
        ("::ffff:10.0.0.1", True),
        ("fe80::1%eth0", False),
        ("169.254.169.254", False),
        ("::ffff:169.254.169.254", False),
        ("0.0.0.0", False),  # noqa: S104
        ("224.0.0.1", False),
        ("93.184.216.34", False),
        ("2606:4700::1", False),
        ("not an ip", False),
    ],
)
def test_address_private(ip, private):
    assert webhook.address_private(ip) is private


def _serve():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    hits = []

    def run():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            hits.append(1)
            conn.recv(4096)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
            )
            conn.close()

    threading.Thread(target=run, daemon=True).start()
    return listener, hits


def _request(url, **more):
    return webhook.fenced_request(
        url,
        "GET",
        None,
        {},
        allow_private=more.pop("allow_private", True),
        deadline_seconds=5,
        timeout=5,
        read=lambda r: (r.status, r.read()),
        **more,
    )


def _resolve_to(monkeypatch, ip, port):
    def fake(host, p, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def test_plain_http_to_loopback_connects(monkeypatch):
    listener, hits = _serve()
    try:
        port = listener.getsockname()[1]
        _resolve_to(monkeypatch, "127.0.0.1", port)
        got = _request(f"http://backchannel.test:{port}/x", require_private=True)
    finally:
        listener.close()
    assert got == (200, b"ok") and hits


@pytest.mark.parametrize("ip", ["93.184.216.34", "169.254.169.254"])
@pytest.mark.parametrize("allow_private", [True, False])
def test_plain_http_to_a_public_or_metadata_address_is_refused(
    monkeypatch, ip, allow_private
):
    listener, hits = _serve()
    try:
        port = listener.getsockname()[1]
        _resolve_to(monkeypatch, ip, port)
        with pytest.raises(webhook.WebhookError):
            _request(
                f"http://backchannel.test:{port}/x",
                allow_private=allow_private,
                require_private=True,
            )
    finally:
        listener.close()
    assert not hits


def test_the_refusal_names_plain_http_when_the_address_is_public(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34", 80)
    with pytest.raises(webhook.WebhookError, match="plain http needs a private"):
        _request("http://backchannel.test/x", require_private=True)


def test_without_require_private_nothing_changes(monkeypatch):
    listener, hits = _serve()
    try:
        port = listener.getsockname()[1]
        _resolve_to(monkeypatch, "127.0.0.1", port)
        assert _request(f"http://x.test:{port}/x")[0] == 200
    finally:
        listener.close()
    assert hits
