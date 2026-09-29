"""ggshield as the second opinion on every page (homelab-ij8.2; Jeremy's ruling
of 24 Sep 2026: a finding refuses, an unreachable relay publishes and is logged,
previews are scanned too)."""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from artifacts import ggscan, keys, thumbs
from artifacts.app import create_gallery_app

TOKEN = "relay-test-token"  # noqa: S105 -- a test fixture, not a credential
PAGE = "<!doctype html><title>t</title><p>caf&eacute; &#x3c;b&#x3e; ok</p>"


class Relay:
    def __init__(self):
        self.answer = {"scanned": True, "findings": []}
        self.status = 200
        self.received = []
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers["content-length"])
                relay.received.append(
                    (
                        self.headers.get("authorization"),
                        json.loads(self.rfile.read(length)),
                    )
                )
                data = json.dumps(relay.answer).encode()
                self.send_response(relay.status)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        ).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/scan"


@pytest.fixture()
def relay(monkeypatch):
    r = Relay()
    monkeypatch.setenv(ggscan.URL_ENV, r.url)
    monkeypatch.setenv(ggscan.TOKEN_ENV, TOKEN)
    yield r
    r.server.shutdown()


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def auth(db):
    token, _ = keys.mint(db, agent="seat")
    return {"Authorization": f"Bearer {token}"}


def _publish(api, auth, body=PAGE):
    return api.post(
        "/api/v1/artifacts", json={"title": "Board", "body": body}, headers=auth
    )


def test_a_ggshield_finding_refuses_the_publish_and_names_the_kind(api, auth, relay):
    relay.answer = {
        "scanned": True,
        "findings": [{"type": "Slack Bot Token", "line": 4}],
    }
    refused = _publish(api, auth)
    assert refused.status_code == 422
    assert refused.json()["detail"]["refused"] == [
        {"field": "body", "shape": "ggshield_slack_bot_token", "line": 4}
    ]


def test_ggshield_is_handed_the_original_body_with_its_token(api, auth, relay):
    """The bead's critical design point: not our unescaped view."""
    assert _publish(api, auth).status_code == 201
    presented, payload = relay.received[-1]
    assert presented == f"Bearer {TOKEN}"
    assert payload == {"body": PAGE}


def test_an_update_is_scanned_too(api, auth, relay):
    page = _publish(api, auth).json()
    relay.answer = {"scanned": True, "findings": [{"type": "AWS Keys", "line": 2}]}
    changed = api.patch(
        f"/api/v1/artifacts/{page['id']}",
        json={"body": PAGE + "<p>more</p>", "expected_revision": page["revision"]},
        headers=auth,
    )
    assert changed.status_code == 422


def test_an_unreachable_relay_publishes_and_says_so_in_the_log(
    api, auth, monkeypatch, caplog
):
    monkeypatch.setenv(ggscan.URL_ENV, "http://127.0.0.1:9/scan")
    monkeypatch.setenv(ggscan.TOKEN_ENV, TOKEN)
    logger = logging.getLogger("artifacts.refusals")
    logger.addHandler(caplog.handler)
    try:
        assert _publish(api, auth).status_code == 201
    finally:
        logger.removeHandler(caplog.handler)
    assert any("ggshield_unavailable" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "answer,status",
    [
        ({"scanned": False, "reason": "ggshield exited 128"}, 200),
        ({"detail": "bad token"}, 401),
        ({"scanned": True}, 200),
    ],
)
def test_a_relay_that_did_not_scan_is_never_read_as_clean_or_dirty(
    api, auth, relay, answer, status
):
    relay.answer, relay.status = answer, status
    assert ggscan.scan(PAGE) is None
    assert _publish(api, auth).status_code == 201


def test_findings_it_cannot_read_still_refuse(api, auth, relay):
    relay.answer = {"scanned": True, "findings": ["???"]}
    assert _publish(api, auth).status_code == 422


def test_a_local_finding_never_leaves_the_house(api, auth, relay):
    aws = "aws_secret_access_key = " + "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzq" + "Q8Ud"
    refused = _publish(api, auth, PAGE.replace("ok", "AKIAIOSFODNN7QWERTYU " + aws))
    assert refused.status_code == 422
    assert relay.received == [], "an obvious secret is refused before any send"


def test_a_preview_is_scanned_too(api, auth, relay, monkeypatch):
    monkeypatch.setattr(thumbs, "available", lambda: True)
    monkeypatch.setattr(thumbs, "render_report", lambda h, w, ht: (None, None))
    relay.answer = {
        "scanned": True,
        "findings": [{"type": "Generic Password", "line": 1}],
    }
    got = api.post("/api/v1/preview", json={"body": PAGE}, headers=auth)
    assert got.status_code == 422


def test_not_configured_means_the_built_in_scan_only(api, auth, monkeypatch):
    monkeypatch.delenv(ggscan.URL_ENV, raising=False)
    monkeypatch.delenv(ggscan.TOKEN_ENV, raising=False)
    assert ggscan.scan(PAGE) is None
    assert _publish(api, auth).status_code == 201


def test_a_relay_that_swallows_packets_costs_seconds_not_the_scan_timeout(
    monkeypatch,
):
    """baba down, or a firewall rule missing: a DROP, not a refusal. Every
    publish would otherwise wait the full scan timeout."""
    import time

    # TEST-NET-1 (RFC 5737): routable nowhere, so a SYN goes unanswered.
    monkeypatch.setenv(ggscan.URL_ENV, "http://192.0.2.1:9098/scan")
    monkeypatch.setenv(ggscan.TOKEN_ENV, TOKEN)
    # The production connect bound is seconds; a short one proves the same
    # thing -- the connect bound, not the 20 s scan timeout, is what is paid.
    monkeypatch.setattr(ggscan, "CONNECT_SECONDS", 0.2)
    started = time.monotonic()
    assert ggscan.scan(PAGE) is None
    assert time.monotonic() - started < ggscan.CONNECT_SECONDS + 2
