"""`emit` hands its one POST off to a background thread and returns immediately
-- these tests never call the network from the test's own thread, so every
assertion about what was (or wasn't) sent has to either wait for a signal the
fake receiver sets, or poll a log record, rather than checking a return value.

The fake receiver is a real `http.server.ThreadingHTTPServer` on an ephemeral
loopback port, exactly as the brief asks: `emit` really does go over a socket
in these tests, not through a mock of `urllib.request`.
"""

import http.server
import json
import logging
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, nudge, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"
_OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture(autouse=True)
def _clean_nudge_state(monkeypatch):
    """Every test starts from the same module state: no config set, the
    "warn once" flag and the drop counter both at their fresh-process values.
    Without this, whichever test happens to hit the missing-token warning
    FIRST in a run would consume the one-per-process warning and every test
    after it would see none."""
    monkeypatch.delenv("ARTIFACTS_NUDGE_URL", raising=False)
    monkeypatch.delenv("ARTIFACTS_NUDGE_TOKEN", raising=False)
    monkeypatch.delenv("ARTIFACTS_NUDGE_TIMEOUT", raising=False)
    monkeypatch.setattr(nudge, "_token_warned", False)
    monkeypatch.setattr(nudge, "_dropped_count", 0)


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs) -> None:  # silence per-request access logs
        pass


class _Receiver:
    """A stand-in for the operator's inbox: records the bearer header and body
    of every POST it gets, and answers with `respond_status`. `event` is set
    once a request has been fully handled, which is what a test waits on
    instead of a return value from `emit` (which never carries one)."""

    def __init__(self, respond_status: int = 200):
        self.received: list[dict] = []
        self.bodies: list[bytes] = []
        self.event = threading.Event()
        outer = self

        class Handler(_QuietHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                outer.bodies.append(body)
                outer.received.append(
                    {
                        "authorization": self.headers.get("Authorization"),
                        "content-type": self.headers.get("Content-Type"),
                    }
                )
                payload = b"{}"
                self.send_response(respond_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                outer.event.set()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> str:
        self.thread.start()
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def wait(self, timeout: float = 5.0) -> bool:
        return self.event.wait(timeout)

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)


def _closed_port() -> int:
    """A loopback port nothing is listening on -- bind, learn the port, close
    it again -- so a connection to it is refused rather than merely slow."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _emit(**overrides) -> None:
    kwargs = {
        "kind": "decision_answered",
        "artifact_id": "art1",
        "title": "Board",
        "agent": "ops-seat",
        "summary": "a: yes",
        "event_id": "1",
    }
    kwargs.update(overrides)
    nudge.emit(**kwargs)


# ---------------------------------------------------------------------------
# Configuration -- unset URL, and URL-set-but-no-token, are both "stay a
# no-op", proven by showing nothing was even queued (not merely "no request
# arrived", which a slow test could get right for the wrong reason).
# ---------------------------------------------------------------------------


def test_unset_url_sends_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(nudge._executor, "submit", lambda *a, **k: calls.append(1))
    _emit()
    assert calls == []


def test_url_set_but_token_unset_sends_nothing_and_warns_once(monkeypatch, caplog):
    monkeypatch.setenv("ARTIFACTS_NUDGE_URL", "http://127.0.0.1:1/x")
    calls = []
    monkeypatch.setattr(nudge._executor, "submit", lambda *a, **k: calls.append(1))
    with caplog.at_level(logging.INFO, logger="artifacts.nudge"):
        _emit(artifact_id="art1")
        _emit(artifact_id="art2")
    assert calls == []
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    assert "ARTIFACTS_NUDGE_TOKEN" in info_records[0].getMessage()


# ---------------------------------------------------------------------------
# A well-formed send: the bearer header, the fixed alertname, the artifact id
# as `instance`, and `event_id` carried through as a string.
# ---------------------------------------------------------------------------


def test_a_well_formed_post_carries_the_bearer_header_and_fixed_fields(monkeypatch):
    receiver = _Receiver()
    origin = receiver.start()
    try:
        monkeypatch.setenv("ARTIFACTS_NUDGE_URL", f"{origin}/v1/events/artifacts")
        monkeypatch.setenv(
            "ARTIFACTS_NUDGE_TOKEN", "test-token-fake"
        )  # obviously fake, short
        _emit(
            kind="decision_answered",
            artifact_id="art123",
            title="Board",
            agent="ops-seat",
            summary="a: yes",
            event_id="42",
        )
        assert receiver.wait(), "the receiver never got a request"
        assert receiver.received[0]["authorization"] == "Bearer test-token-fake"
        assert receiver.received[0]["content-type"] == "application/json"
        body = json.loads(receiver.bodies[0])
        assert body["alertname"] == "Jeremy answered a decision"
        assert body["instance"] == "art123"
        assert body["artifact_id"] == "art123"
        assert body["kind"] == "decision_answered"
        assert body["event_id"] == "42"
        assert body["state"] == "message"
        assert body["severity"] == "info"
        assert body["agent"] == "ops-seat"
    finally:
        receiver.stop()


@pytest.mark.parametrize(
    ("kind", "alertname"),
    [
        ("decision_answered", "Jeremy answered a decision"),
        ("comment_added", "Jeremy left a comment"),
        ("thread_resolved", "Jeremy resolved a thread"),
        ("something_unknown", "Artifacts event"),
    ],
)
def test_each_kind_gets_its_own_fixed_alertname(monkeypatch, kind, alertname):
    receiver = _Receiver()
    origin = receiver.start()
    try:
        monkeypatch.setenv("ARTIFACTS_NUDGE_URL", origin)
        monkeypatch.setenv("ARTIFACTS_NUDGE_TOKEN", "tok")
        _emit(kind=kind)
        assert receiver.wait()
        assert json.loads(receiver.bodies[0])["alertname"] == alertname
    finally:
        receiver.stop()


# ---------------------------------------------------------------------------
# Sanitisation: viewer-typed text must never carry structure into another
# agent's context.
# ---------------------------------------------------------------------------


def test_a_hostile_summary_arrives_stripped_collapsed_and_truncated_to_200(monkeypatch):
    receiver = _Receiver()
    origin = receiver.start()
    try:
        monkeypatch.setenv("ARTIFACTS_NUDGE_URL", origin)
        monkeypatch.setenv("ARTIFACTS_NUDGE_TOKEN", "tok")
        hostile = "line one\nline two\n" + "\x1b[31m" + ("z" * 500)
        _emit(summary=hostile, title=hostile)
        assert receiver.wait()
        body = json.loads(receiver.bodies[0])
        for field in ("summary", "title"):
            value = body[field]
            assert len(value) == 200
            assert value.endswith("…")
            assert "\n" not in value
            assert "\x1b" not in value
            assert "line one line two" in value
    finally:
        receiver.stop()


# ---------------------------------------------------------------------------
# A down or unhappy receiver: `emit` (and the request it was called from)
# must never see any of this.
# ---------------------------------------------------------------------------


def test_a_refused_connection_leaves_emit_returning_normally(monkeypatch, caplog):
    monkeypatch.setenv("ARTIFACTS_NUDGE_URL", f"http://127.0.0.1:{_closed_port()}/x")
    monkeypatch.setenv("ARTIFACTS_NUDGE_TOKEN", "tok")
    with caplog.at_level(logging.WARNING, logger="artifacts.nudge"):
        _emit()  # must not raise
        assert _wait_until(
            lambda: any("send failed" in r.getMessage() for r in caplog.records)
        )


def test_a_hanging_receiver_leaves_emit_returning_promptly_and_normally(
    monkeypatch, caplog
):
    class _HangingHandler(_QuietHandler):
        def do_POST(self) -> None:
            time.sleep(5)  # well past the timeout this test configures below

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HangingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        monkeypatch.setenv("ARTIFACTS_NUDGE_URL", f"http://{host}:{port}/x")
        monkeypatch.setenv("ARTIFACTS_NUDGE_TOKEN", "tok")
        monkeypatch.setenv("ARTIFACTS_NUDGE_TIMEOUT", "0.2")
        with caplog.at_level(logging.WARNING, logger="artifacts.nudge"):
            started = time.monotonic()
            _emit()
            # emit() itself must return almost instantly -- the slow part
            # happens on a background thread this call never waits for.
            assert time.monotonic() - started < 1.0
            assert _wait_until(
                lambda: any("send failed" in r.getMessage() for r in caplog.records),
                timeout=3,
            )
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_a_500_response_leaves_emit_returning_normally(monkeypatch, caplog):
    receiver = _Receiver(respond_status=500)
    origin = receiver.start()
    try:
        monkeypatch.setenv("ARTIFACTS_NUDGE_URL", origin)
        monkeypatch.setenv("ARTIFACTS_NUDGE_TOKEN", "tok")
        with caplog.at_level(logging.WARNING, logger="artifacts.nudge"):
            _emit()
            assert receiver.wait()
            assert _wait_until(
                lambda: any("send failed" in r.getMessage() for r in caplog.records)
            )
    finally:
        receiver.stop()


def test_the_token_never_appears_in_any_log_line(monkeypatch, caplog):
    fake_bearer_value = (
        "should-never-be-logged-fake"  # obviously-fake, short, never a real credential
    )
    receiver = _Receiver(respond_status=500)  # forces the failure log path too
    origin = receiver.start()
    try:
        monkeypatch.setenv("ARTIFACTS_NUDGE_URL", origin)
        monkeypatch.setenv("ARTIFACTS_NUDGE_TOKEN", fake_bearer_value)
        with caplog.at_level(logging.DEBUG):
            _emit()
            assert receiver.wait()
            _wait_until(
                lambda: any("send failed" in r.getMessage() for r in caplog.records)
            )
        assert all(
            fake_bearer_value not in record.getMessage() for record in caplog.records
        )
    finally:
        receiver.stop()


# ---------------------------------------------------------------------------
# The bounded queue: a burst larger than the configured headroom drops rather
# than piling up an ever-growing backlog behind a receiver that is down.
# ---------------------------------------------------------------------------


def test_a_full_queue_drops_the_event_and_counts_it_without_raising(monkeypatch):
    monkeypatch.setattr(nudge, "_slots", threading.BoundedSemaphore(1))
    nudge._slots.acquire()  # occupy the only slot -- the next emit must be dropped
    monkeypatch.setenv("ARTIFACTS_NUDGE_URL", "http://127.0.0.1:1/x")
    monkeypatch.setenv("ARTIFACTS_NUDGE_TOKEN", "tok")

    _emit()  # must not raise, must not block

    assert nudge._dropped_count == 1


# ---------------------------------------------------------------------------
# The whole point: answering a decision must not become slower or riskier
# just because the operator's inbox is unreachable.
# ---------------------------------------------------------------------------


def test_answering_a_decision_still_redirects_303_while_the_receiver_is_down(
    db, monkeypatch
):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_NUDGE_URL", f"http://127.0.0.1:{_closed_port()}/x")
    monkeypatch.setenv("ARTIFACTS_NUDGE_TOKEN", "tok")

    published = store.publish(db, "ops-seat", "Board", PAGE)
    row = decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=published.id,
        question="ship it?",
        options=_OPTIONS,
    )

    response = TestClient(create_gallery_app(db)).post(
        f"/d/{row.id}/answer",
        data={"letter": "a", "note": "go ahead"},
        headers={"origin": store.gallery_origin(), "sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/a/{published.id}"
