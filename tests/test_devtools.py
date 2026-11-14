"""The DevTools pipe (`ream.devtools`) against a fake browser: two OS pipes, the
browser's side written from a thread. No browser is started."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from ream import devtools

MAX = 64 * 1024
WAITED = frozenset({"Page.loadEventFired", "Target.attachedToTarget"})


@pytest.fixture()
def browser():
    """`start(*messages, **pipe_options)`: a Pipe whose browser sends `messages`
    (dicts as JSON, bytes as they are), each ended by a NUL, then closes."""
    fds = []
    threads = []

    def start(*messages, **options):
        to_r, to_w = os.pipe()
        from_r, from_w = os.pipe()
        fds.extend([to_r, to_w, from_r])
        data = b"".join(
            (m if isinstance(m, bytes) else json.dumps(m).encode()) + b"\0"
            for m in messages
        )

        def write():
            try:
                _write_all(from_w, data)
            except OSError:
                pass
            finally:
                os.close(from_w)

        thread = threading.Thread(target=write, daemon=True)
        thread.start()
        threads.append(thread)
        options.setdefault("deadline", time.monotonic() + 10)
        options.setdefault("max_message", MAX)
        return devtools.Pipe(to_w, from_r, **options)

    yield start
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass
    for thread in threads:
        thread.join(timeout=5)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def event(method, session=None, **params):
    message = {"method": method, "params": params}
    if session is not None:
        message["sessionId"] = session
    return message


ANSWER = {"id": 1, "result": {}}


def test_an_event_flood_is_seen_and_dropped_unless_waited_for(browser):
    seen = []
    big = "x" * 8000
    flood = [
        event(method, f"S{n % 64}", url=big)
        for n in range(400)
        for method in ("Network.requestWillBeSent", "Page.frameNavigated")
    ]
    pipe = browser(
        *flood,
        ANSWER,
        keep=WAITED,
        max_kept=MAX,
        on_event=lambda method, params, session: seen.append(method),
    )
    assert pipe.call("Page.enable") == {}
    assert len(seen) == 800, "every event was handed to on_event"
    assert pipe.kept_bytes() == 0
    assert not pipe.seen("Network.requestWillBeSent", "S0")


def test_a_waited_for_kind_keeps_only_its_last_few(browser):
    filler = "y" * 2000
    flood = [event("Page.loadEventFired", "S1", n=n, pad=filler) for n in range(200)]
    pipe = browser(*flood, ANSWER, keep=WAITED, max_kept=10**9)
    pipe.call("Page.enable")
    size = len(json.dumps(flood[0]))
    assert pipe.kept_bytes() <= (devtools.EVENTS_KEPT + 1) * size
    first = pipe.wait_for("Page.loadEventFired", "S1")
    assert first["n"] == 200 - devtools.EVENTS_KEPT
    assert pipe.kept_bytes() < devtools.EVENTS_KEPT * size


def test_the_kept_events_have_a_bound_in_bytes_across_sessions(browser):
    many = [
        event("Target.attachedToTarget", f"S{n}", url="z" * 3000) for n in range(40)
    ]
    pipe = browser(*many, ANSWER, keep=WAITED, max_kept=32 * 1024)
    with pytest.raises(devtools.TooMuchKept):
        pipe.call("Page.enable")


def test_waiting_for_an_event_not_kept_is_refused(browser):
    pipe = browser(keep=WAITED)
    with pytest.raises(ValueError):
        pipe.wait_for("Network.requestWillBeSent")


def test_an_event_over_the_limit_is_not_the_answers_size(browser):
    huge = event("Page.frameNavigated", "S1", url="u" * (MAX + 10))
    pipe = browser(huge, ANSWER)
    with pytest.raises(devtools.MessageTooBig) as caught:
        pipe.call("Page.captureScreenshot")
    assert not isinstance(caught.value, devtools.AnswerTooBig)


def test_an_answer_over_the_limit_says_so(browser):
    pipe = browser({"id": 1, "result": {"data": "A" * (MAX + 10)}})
    with pytest.raises(devtools.AnswerTooBig):
        pipe.call("Page.captureScreenshot")


def test_another_commands_answer_over_the_limit_is_not_this_ones(browser):
    pipe = browser({"id": 7, "result": {"data": "A" * (MAX + 10)}})
    with pytest.raises(devtools.MessageTooBig) as caught:
        pipe.call("Page.captureScreenshot")
    assert not isinstance(caught.value, devtools.AnswerTooBig)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[1, 2]",
        b'"a string"',
        b'{"id": 1, "result": {"x": NaN}}',
        b'{"id": 1, "result": {"x": Infinity}}',
        b'{"id": 1, "result": {"x": -Infinity}}',
        b'{"id": 1, "result": {"x": 1e400}}',
    ],
)
def test_a_message_that_is_not_a_json_object_fails_the_call(browser, raw):
    pipe = browser(raw)
    with pytest.raises(devtools.CommandFailed):
        pipe.call("Page.enable")


def test_a_closed_pipe_is_closed(browser):
    pipe = browser()
    with pytest.raises(devtools.Closed):
        pipe.call("Page.enable")
