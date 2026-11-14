"""The browser's DevTools protocol over a pipe: the few calls a screenshot needs.

Chromium started with `--remote-debugging-pipe` reads commands on its fd 3 and
writes answers and events on its fd 4, each message one JSON object ended by a
NUL byte. `Pipe` is the caller's end of that pair: it sends a command, reads
until that command's answer comes back, and keeps only the events it is asked
to wait for. Everything is bounded:

  * every read and write waits at most until the deadline the pipe was made
    with (a `time.monotonic()` value), then raises `Timeout`;
  * a message longer than `max_message` bytes raises `MessageTooBig` before the
    rest of it is read, so a page cannot make the caller hold an unbounded
    answer;
  * the browser closing its end raises `Closed`.

Standard library only, and nothing of the rest of `ream`, as `ream.renderwire`.
"""

from __future__ import annotations

import json
import os
import select
import time

# How many events of one kind are kept while waiting for something else: a page
# can make the browser send many (one per request it tries), and only the last
# few of a kind are ever needed.
EVENTS_KEPT = 16
_READ_CHUNK = 1024 * 1024


class DevToolsError(Exception):
    """The conversation with the browser failed. The message is a fixed phrase."""


class Timeout(DevToolsError):
    """The deadline passed."""


class Closed(DevToolsError):
    """The browser closed its end of the pipe (it exited, or was killed)."""


class MessageTooBig(DevToolsError):
    """A message from the browser was longer than the pipe takes."""


class CommandFailed(DevToolsError):
    """The browser answered a command with an error, or with something that is
    not an answer."""


class Pipe:
    """The caller's end: `to_browser` is written (the browser's fd 3), and
    `from_browser` read (its fd 4). The descriptors stay the caller's to close."""

    def __init__(
        self,
        to_browser: int,
        from_browser: int,
        *,
        deadline: float,
        max_message: int,
        on_event=None,
    ) -> None:
        self._out = to_browser
        self._in = from_browser
        self.deadline = deadline
        self.max_message = max_message
        self._buffer = bytearray()
        self._next_id = 0
        self._events: dict[tuple[str | None, str], list[dict]] = {}
        # Called with (method, params, session) for every event as it is read; it
        # may `send` (never `call`: it runs inside a read).
        self._on_event = on_event
        # Commands sent with `send(..., checked=True)`: an error answer to one of
        # them fails whatever call is reading when it comes.
        self._checked: set[int] = set()

    # -- writing --

    def _left(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise Timeout("the browser did not answer in time")
        return left

    def _write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            _, ready, _ = select.select([], [self._out], [], self._left())
            if not ready:
                continue
            try:
                n = os.write(self._out, view)
            except BrokenPipeError:
                raise Closed("the browser closed the pipe") from None
            view = view[n:]

    def send(
        self,
        method: str,
        params: dict | None = None,
        session: str | None = None,
        *,
        checked: bool = False,
    ):
        """Send one command; its id. Its answer is not waited for; `checked`, an
        error answer to it raises CommandFailed from the read that meets it."""
        self._next_id += 1
        message = {"id": self._next_id, "method": method, "params": params or {}}
        if session is not None:
            message["sessionId"] = session
        if checked:
            self._checked.add(self._next_id)
        self._write(json.dumps(message, separators=(",", ":")).encode() + b"\0")
        return self._next_id

    # -- reading --

    def _message(self) -> dict:
        """The next whole message from the browser."""
        while True:
            end = self._buffer.find(b"\0")
            if end >= 0:
                raw = bytes(self._buffer[:end])
                del self._buffer[: end + 1]
                if len(raw) > self.max_message:
                    raise MessageTooBig("a message from the browser is too long")
                try:
                    message = json.loads(raw)
                except (ValueError, RecursionError):
                    raise CommandFailed("the browser sent something not JSON") from None
                if not isinstance(message, dict):
                    raise CommandFailed("the browser sent something not an object")
                return message
            if len(self._buffer) > self.max_message:
                raise MessageTooBig("a message from the browser is too long")
            ready, _, _ = select.select([self._in], [], [], self._left())
            if not ready:
                continue
            try:
                part = os.read(self._in, _READ_CHUNK)
            except OSError:
                raise Closed("the browser closed the pipe") from None
            if not part:
                raise Closed("the browser closed the pipe")
            self._buffer += part

    def _keep(self, message: dict) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            return
        session = message.get("sessionId")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        kept = self._events.setdefault((session, method), [])
        kept.append(params)
        del kept[:-EVENTS_KEPT]
        if self._on_event is not None:
            self._on_event(method, params, session)

    def _answered(self, message: dict) -> None:
        """An answer nothing is waiting for: fail if it is a checked one's error."""
        ident = message.get("id")
        if ident in self._checked:
            self._checked.discard(ident)
            if "error" in message or not isinstance(message.get("result"), dict):
                raise CommandFailed("the browser refused a command")

    def call(
        self, method: str, params: dict | None = None, session: str | None = None
    ) -> dict:
        """Send one command and return its result; CommandFailed if it was
        answered with an error. Events read meanwhile are kept (`wait_for`)."""
        want = self.send(method, params, session)
        while True:
            message = self._message()
            if "id" not in message:
                self._keep(message)
                continue
            if message["id"] != want:
                self._answered(message)
                continue
            if "error" in message or not isinstance(message.get("result"), dict):
                raise CommandFailed(f"the browser refused {method}")
            return message["result"]

    def seen(self, method: str, session: str | None = None) -> bool:
        return bool(self._events.get((session, method)))

    def wait_for(
        self, method: str, session: str | None = None, *, until: float | None = None
    ) -> dict | None:
        """The params of the first event `method` (on `session`) not yet taken,
        reading until it comes; None if `until` (monotonic, no later than the
        pipe's deadline) passes first."""
        while True:
            kept = self._events.get((session, method))
            if kept:
                return kept.pop(0)
            if until is not None and time.monotonic() >= until:
                return None
            saved = self.deadline
            if until is not None:
                self.deadline = min(saved, until)
            try:
                message = self._message()
            except Timeout:
                if until is not None and saved > until:
                    return None
                raise
            finally:
                self.deadline = saved
            if "id" not in message:
                self._keep(message)
            else:
                self._answered(message)
