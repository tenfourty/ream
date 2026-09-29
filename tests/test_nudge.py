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
    # This mailbox belongs to ONE seat. Every test below emits as "ops-seat",
    # so the seat that owns the mailbox is set to match; the tests that prove
    # the gate itself override it.
    monkeypatch.setenv("ARTIFACTS_NUDGE_AGENT", "ops-seat")
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
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )

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
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
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


def test_a_page_owned_by_another_agent_is_never_pushed_to_this_mailbox(monkeypatch):
    """Jeremy, 19 Sep: "the agent that published the artifact needs to get the
    nudge, not ops." This URL is one seat's mailbox. Firing it for every page
    delivered every agent's answers to the operator and none to the agent that
    was actually waiting for them."""
    receiver = _Receiver()
    origin = receiver.start()
    try:
        monkeypatch.setenv("ARTIFACTS_NUDGE_URL", f"{origin}/v1/events/artifacts")
        monkeypatch.setenv("ARTIFACTS_NUDGE_TOKEN", "test-token-fake")
        monkeypatch.setenv("ARTIFACTS_NUDGE_AGENT", "ops-seat")

        _emit(agent="some-other-seat")
        # Then one that SHOULD arrive. Asserting only "nothing came" would pass
        # just as well against a receiver that was simply broken.
        _emit(agent="ops-seat")
        assert receiver.wait(), "the receiver never got the one that should arrive"
        time.sleep(0.3)
        assert len(receiver.received) == 1
        assert json.loads(receiver.bodies[0])["agent"] == "ops-seat"
    finally:
        receiver.stop()


def test_with_no_seat_named_the_mailbox_is_never_used(monkeypatch):
    """Unset means "this mailbox belongs to nobody here", which must be silence
    rather than a fallback to sending everything -- a fallback is precisely the
    behaviour being removed."""
    receiver = _Receiver()
    origin = receiver.start()
    try:
        monkeypatch.setenv("ARTIFACTS_NUDGE_URL", f"{origin}/v1/events/artifacts")
        monkeypatch.setenv("ARTIFACTS_NUDGE_TOKEN", "test-token-fake")
        monkeypatch.delenv("ARTIFACTS_NUDGE_AGENT", raising=False)

        _emit(agent="ops-seat")
        time.sleep(0.5)
        assert receiver.received == []
    finally:
        receiver.stop()


# --- homelab-ij8.27: the service cannot reach Jeremy directly ----------------


def test_the_service_carries_no_telegram_capability_at_all():
    """Jeremy, 19 Sep 2026: "telegram is only for the ops agent, it is not for
    all agents." The acceptance criterion is that no artifacts-originated event
    can produce a Telegram message except by the operator deciding to relay it.

    Asserted as an ABSENCE across the whole package, not as a property of the
    nudge path alone: the point is that there is no code here to reach him
    with, so a future call site cannot acquire one by accident. A grep is a
    weak test in general and the right one here -- the claim IS "this string
    appears nowhere that could send".
    """
    import pathlib

    import artifacts

    root = pathlib.Path(artifacts.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            lowered = line.lower()
            if "tell_jeremy" in lowered or "api.telegram.org" in lowered:
                offenders.append(f"{path.name}:{number}")
    assert offenders == [], (
        "the service acquired a way to message Jeremy directly: " + ", ".join(offenders)
    )


def test_the_only_push_goes_to_one_named_seats_mailbox(monkeypatch):
    """And it is inert for every other seat. An event on ANY page firing the
    operator's mailbox is what made the operator the recipient of everyone's
    answers and the owning agent the recipient of none."""
    sent = []
    monkeypatch.setenv("ARTIFACTS_NUDGE_URL", "http://127.0.0.1:1/v1/events/artifacts")
    monkeypatch.setenv("ARTIFACTS_NUDGE_AGENT", "homelab-ops")
    monkeypatch.setenv("ARTIFACTS_NUDGE_TOKEN", "x" * 20)
    # The send is handed to a thread pool; intercepting the SUBMIT is how the
    # rest of this file observes it, and it keeps the test off the network.
    monkeypatch.setattr(nudge._executor, "submit", lambda *a, **k: sent.append(a))

    nudge.emit(
        "decision_answered",
        artifact_id="a1",
        title="T",
        agent="some-other-seat",
        summary="s",
        event_id="1",
    )
    assert sent == [], "a page belonging to another seat nudged the operator"

    nudge.emit(
        "decision_answered",
        artifact_id="a1",
        title="T",
        agent="homelab-ops",
        summary="s",
        event_id="1",
    )
    assert len(sent) == 1, "the named seat's own page did not nudge its own mailbox"


# ---------------------------------------------------------------------------
# What he WROTE, not just which letter he tapped (homelab-873).
#
# 20 Sep 2026: a decision was answered (a) with a 244-character note that
# qualified the letter -- "check all folders between the two backups ... only
# keep what is left behind that is unique". The mailbox row that woke the
# operator carried `summary = "a: <label>"` and nothing else. It acted on the
# letter for four and a half hours and found the note by a hand sweep. The
# journal had carried the note the whole time; the thing that does the waking
# did not.
# ---------------------------------------------------------------------------


def _answer_through_the_gallery(
    db, monkeypatch, origin, *, note, recommended=None, letter="a"
):
    """The real path: Jeremy taps an option in the gallery. Going through the
    route rather than calling `decisions.answer` directly is deliberate -- the
    bug was in what reached the mailbox from a REAL answer, and a test that
    calls the emitter itself can only prove the emitter."""
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("ARTIFACTS_NUDGE_URL", f"{origin}/v1/events/artifacts")
    monkeypatch.setenv("ARTIFACTS_NUDGE_TOKEN", "test-token-fake")

    published = store.publish(db, "ops-seat", "Board", PAGE)
    row = decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=published.id,
        question="ship it?",
        options=_OPTIONS,
        recommended=recommended,
    )
    response = TestClient(create_gallery_app(db)).post(
        f"/d/{row.id}/answer",
        data={"letter": letter, "note": note},
        headers={"origin": store.gallery_origin(), "sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return published, row


def test_the_mailbox_row_carries_his_note_byte_for_byte(db, monkeypatch):
    receiver = _Receiver()
    origin = receiver.start()
    try:
        note = (
            "check all folders between the two backups, then remove anything "
            "that is already elsewhere - only keep what is left behind that is "
            "unique so we can do forensics on it"
        )
        _answer_through_the_gallery(db, monkeypatch, origin, note=note)

        assert receiver.wait(), "the receiver never got a request"
        body = json.loads(receiver.bodies[0])
        assert body["note"] == note
        assert body["has_note"] is True
        # In its OWN field. A receiver showing the summary as one line has to
        # be able to show this differently, and a concatenation would take that
        # choice away from every consumer.
        assert note not in body["summary"]
    finally:
        receiver.stop()


def test_an_empty_note_says_so_instead_of_sending_a_clause(db, monkeypatch):
    receiver = _Receiver()
    origin = receiver.start()
    try:
        _answer_through_the_gallery(db, monkeypatch, origin, note="")

        assert receiver.wait()
        body = json.loads(receiver.bodies[0])
        assert body["note"] == ""
        assert body["has_note"] is False
    finally:
        receiver.stop()


def test_his_note_is_not_squeezed_into_the_summary_limit(db, monkeypatch):
    """The note that caused this was 244 characters. A cap of 200 -- the right
    size for a one-line summary -- would have cut the qualification off mid
    sentence and the row would still have LOOKED complete."""
    receiver = _Receiver()
    origin = receiver.start()
    try:
        note = "w" * 600
        _answer_through_the_gallery(db, monkeypatch, origin, note=note)

        assert receiver.wait()
        assert json.loads(receiver.bodies[0])["note"] == note
    finally:
        receiver.stop()


def test_a_hostile_note_is_sanitised_like_every_other_field(db, monkeypatch):
    receiver = _Receiver()
    origin = receiver.start()
    try:
        _answer_through_the_gallery(
            db, monkeypatch, origin, note="line one\nline two\x1b[31m"
        )

        assert receiver.wait()
        note = json.loads(receiver.bodies[0])["note"]
        assert "\n" not in note
        assert "\x1b" not in note
        assert "line one" in note and "line two" in note
    finally:
        receiver.stop()


def test_the_mailbox_row_says_when_he_did_not_take_the_recommendation(db, monkeypatch):
    """The other half of the same failure: a letter reads as agreement until
    somebody checks what was recommended."""
    receiver = _Receiver()
    origin = receiver.start()
    try:
        _answer_through_the_gallery(
            db, monkeypatch, origin, note="", recommended="a", letter="b"
        )

        assert receiver.wait()
        body = json.loads(receiver.bodies[0])
        assert body["answer"] == "b"
        assert body["recommended"] == "a"
        assert body["differs_from_recommended"] is True
    finally:
        receiver.stop()


def test_taking_the_recommendation_is_not_flagged_as_departing_from_it(db, monkeypatch):
    receiver = _Receiver()
    origin = receiver.start()
    try:
        _answer_through_the_gallery(
            db, monkeypatch, origin, note="", recommended="a", letter="a"
        )

        assert receiver.wait()
        body = json.loads(receiver.bodies[0])
        assert body["differs_from_recommended"] is False
    finally:
        receiver.stop()


def test_a_question_posed_without_a_recommendation_never_reads_as_a_departure(
    db, monkeypatch
):
    """There is nothing to depart from. A receiver told otherwise would flag
    every answer on every page that simply did not recommend anything."""
    receiver = _Receiver()
    origin = receiver.start()
    try:
        _answer_through_the_gallery(db, monkeypatch, origin, note="", letter="b")

        assert receiver.wait()
        body = json.loads(receiver.bodies[0])
        assert body["recommended"] is None
        assert body["differs_from_recommended"] is False
    finally:
        receiver.stop()
