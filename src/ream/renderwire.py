"""What a render answers, and the checks every answer passes before it is used.

A render is one browser screenshot of one page plus what the page's own check
script reported. Whether the browser ran in this process (`ream.rendercore`) or
in a renderer the app asks over HTTP, its answer is accepted on one path:
`validate_png` for the picture, `parse_answer` for the whole answer on the wire.
Anything that fails a check is treated as a render that did not run: no
picture, and the verdict stays `unchecked`.

The wire, `POST /v1/render?w=900&h=640&check=1` with the stamped HTML as the
body, answers 200 with JSON:

    {"v": 1, "outcome": ..., "png_base64": ..., "errors": [...], "check": {...} | null}

`outcome` is one of OUTCOMES:
  * `rendered`: the browser ran and made a picture; `png_base64` holds it.
  * `no_picture`: the browser ran but made no picture; `png_base64` is null,
    and the errors and check numbers it did report are kept.
  * `not_run`: the browser did not run (timeout, memory, no turn, no start);
    `png_base64` and `check` are null and `errors` is empty.

`call` is the app's side of that wire: one request to a renderer at
`http://host:port` (`REAM_RENDERER_URL`), and anything short of a valid 200
answer raises `RenderUnavailable`.

Standard library only, and nothing of the rest of `ream`, so the renderer can
load it without the service around it. Errors are raised with a fixed phrase
and never quote the bytes they refused.
"""

from __future__ import annotations

import binascii
import http.client
import json
import os
import re
import socket
import struct
import threading
import time
import zlib
from urllib.parse import urlsplit

WIRE_VERSION = 1

# The whole JSON answer, read by the app.
RESPONSE_MAX_BYTES = 11 * 1024 * 1024
# The page's script errors: how many, and how long each may be.
ERRORS_MAX_COUNT = 10
ERROR_MAX_CHARS = 320
# The check script's numbers: exactly these keys, each an int in [0, CHECK_MAX].
CHECK_KEYS = ("text", "media", "height", "scroll", "view")
CHECK_MAX = 10**7
# The picture, decoded.
PNG_MAX_BYTES = 8 * 1024 * 1024
PNG_BASE64_MAX_CHARS = 4 * ((PNG_MAX_BYTES + 2) // 3)
# The viewport a render may ask for (the preview's own bounds).
WIDTH_MIN, WIDTH_MAX = 320, 1600
HEIGHT_MIN, HEIGHT_MAX = 320, 4000
QUERY_MAX_CHARS = 256
# The header every render request carries, and the renderer requires (a fixed value,
# not a secret: a request a browser could be made to send never carries it).
RENDER_HEADER = "X-Ream-Render"
RENDER_HEADER_VALUE = "1"
# The app's call: how long to wait for the renderer to accept, and for the whole call.
CONNECT_TIMEOUT_SECONDS = 2.0
CALL_DEADLINE_SECONDS = 30.0

OUTCOMES = ("rendered", "no_picture", "not_run")
_ANSWER_KEYS = frozenset({"v", "outcome", "png_base64", "errors", "check"})
_QUERY_KEYS = frozenset({"w", "h", "check"})

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_IHDR_LENGTH = 13
# The end chunk is always the same twelve bytes: no data, so a fixed CRC.
_IEND = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND"))
_PNG_MIN_BYTES = len(_PNG_MAGIC) + 4 + 4 + _IHDR_LENGTH + 4 + len(_IEND)
_DIGITS = re.compile(r"[0-9]{1,5}")


class RenderAnswerInvalid(ValueError):
    """A render's answer failed a check. The message is a fixed phrase."""


class RenderRequestInvalid(ValueError):
    """A render request's query failed a check (a 400). A fixed phrase."""


class RenderUnavailable(RuntimeError):
    """The renderer gave no usable answer: it could not be reached, was too slow,
    was busy, or answered something that failed a check. One fixed phrase, never
    anything of the response."""

    def __init__(self) -> None:
        super().__init__(UNAVAILABLE)


# Why a render did not run, when that is not "no browser here" (`Report.reason`).
UNAVAILABLE = "renderer_unavailable"


class Report(tuple):
    """`(png or None, script errors)`, which is all most callers unpack, with
    the check script's numbers (or None: it did not report) kept alongside as
    `.check`, and `.reason` (None, or UNAVAILABLE: the renderer gave no usable
    answer). A tuple, so a caller and a test double that know only two things
    keep working."""

    check: dict | None
    reason: str | None

    def __new__(cls, png, errors, check=None, reason=None):
        self = super().__new__(cls, (png, errors))
        self.check = check
        self.reason = reason
        return self


def _is_int(value) -> bool:
    return type(value) is int  # a bool is an int to isinstance, not here


def validate_png(data: bytes, width: int, height: int) -> bytes:
    """`data` if it is a PNG of exactly `width` x `height`, else
    RenderAnswerInvalid. Checks the signature, that the first chunk is a
    13-byte IHDR with a correct CRC and the requested size, and that the data
    ends with the IEND chunk. The pixels themselves are not decoded."""
    if not isinstance(data, bytes):
        raise RenderAnswerInvalid("picture is not bytes")
    if len(data) > PNG_MAX_BYTES:
        raise RenderAnswerInvalid("picture is over the size limit")
    if not data.startswith(_PNG_MAGIC) or len(data) < _PNG_MIN_BYTES:
        raise RenderAnswerInvalid("picture is not a PNG")
    (length,) = struct.unpack(">I", data[8:12])
    if length != _IHDR_LENGTH or data[12:16] != b"IHDR":
        raise RenderAnswerInvalid("picture's first chunk is not a PNG header")
    (crc,) = struct.unpack(">I", data[29:33])
    if crc != zlib.crc32(data[12:29]):
        raise RenderAnswerInvalid("picture's header checksum is wrong")
    if struct.unpack(">II", data[16:24]) != (width, height):
        raise RenderAnswerInvalid("picture's size is not the size asked for")
    if not data.endswith(_IEND):
        raise RenderAnswerInvalid("picture has no end chunk")
    return data


def _errors(value) -> list[str]:
    if not isinstance(value, list) or len(value) > ERRORS_MAX_COUNT:
        raise RenderAnswerInvalid("errors are not a short list")
    for item in value:
        if not isinstance(item, str) or len(item) > ERROR_MAX_CHARS:
            raise RenderAnswerInvalid("an error is not a short string")
    return list(value)


def _check(value) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(CHECK_KEYS):
        raise RenderAnswerInvalid("check does not have its five numbers")
    for number in value.values():
        if not _is_int(number) or not 0 <= number <= CHECK_MAX:
            raise RenderAnswerInvalid("a check number is out of bounds")
    return {k: value[k] for k in CHECK_KEYS}


def _no_duplicates(pairs):
    found = {}
    for key, value in pairs:
        if key in found:
            raise RenderAnswerInvalid("answer repeats a key")
        found[key] = value
    return found


def _no_constant(name):
    raise RenderAnswerInvalid("answer holds a number JSON does not have")


def parse_answer(raw: bytes, width: int, height: int) -> Report:
    """The `Report` a renderer's JSON answer stands for, or RenderAnswerInvalid.
    `width` and `height` are what was asked for; the picture must be that size."""
    if not isinstance(raw, bytes) or len(raw) > RESPONSE_MAX_BYTES:
        raise RenderAnswerInvalid("answer is over the size limit")
    try:
        answer = json.loads(
            raw, object_pairs_hook=_no_duplicates, parse_constant=_no_constant
        )
    except RenderAnswerInvalid:
        raise  # a repeated key or a constant, with its own phrase
    except (ValueError, RecursionError):
        raise RenderAnswerInvalid("answer is not JSON") from None
    if not isinstance(answer, dict) or set(answer) != _ANSWER_KEYS:
        raise RenderAnswerInvalid("answer does not have exactly its keys")
    if not _is_int(answer["v"]) or answer["v"] != WIRE_VERSION:
        raise RenderAnswerInvalid("answer is of an unknown version")
    outcome = answer["outcome"]
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise RenderAnswerInvalid("answer has an unknown outcome")
    errors = _errors(answer["errors"])
    check = _check(answer["check"])
    encoded = answer["png_base64"]

    if outcome == "not_run":
        if encoded is not None or errors or check is not None:
            raise RenderAnswerInvalid("a render that did not run reports nothing")
        return Report(None, None, None)
    if outcome == "no_picture":
        if encoded is not None:
            raise RenderAnswerInvalid("answer has a picture it says it has not")
        return Report(None, errors, check)
    if not isinstance(encoded, str):
        raise RenderAnswerInvalid("answer has no picture")
    if len(encoded) > PNG_BASE64_MAX_CHARS or not encoded.isascii():
        raise RenderAnswerInvalid("picture is over the size limit")
    try:
        png = binascii.a2b_base64(encoded.encode("ascii"), strict_mode=True)
    except binascii.Error:
        raise RenderAnswerInvalid("picture is not strict base64") from None
    return Report(validate_png(png, width, height), errors, check)


def encode_answer(report) -> bytes:
    """The JSON answer for `report` (a `Report`, or a `(png, errors)` pair), the
    renderer's side of `parse_answer`. A picture is expected to have passed
    `validate_png` already; an answer over any limit raises RenderAnswerInvalid
    rather than be sent for the app to refuse."""
    png, errors = report[0], report[1]
    check = getattr(report, "check", None)
    if png is None and errors is None:
        outcome, errors, check, encoded = "not_run", [], None, None
    else:
        errors = _errors(list(errors or []))
        check = _check(check)
        if png is None:
            outcome, encoded = "no_picture", None
        else:
            if not isinstance(png, bytes) or len(png) > PNG_MAX_BYTES:
                raise RenderAnswerInvalid("picture is over the size limit")
            outcome = "rendered"
            encoded = binascii.b2a_base64(png, newline=False).decode("ascii")
    raw = json.dumps(
        {
            "v": WIRE_VERSION,
            "outcome": outcome,
            "png_base64": encoded,
            "errors": errors,
            "check": check,
        },
        separators=(",", ":"),
    ).encode("ascii")
    if len(raw) > RESPONSE_MAX_BYTES:
        raise RenderAnswerInvalid("answer is over the size limit")
    return raw


def parse_request_query(query: str | bytes) -> tuple[int, int, bool]:
    """`(width, height, check)` from a render request's query string, or
    RenderRequestInvalid. `w` and `h` are required ints in bounds, `check` is
    `0` or `1` (absent: 0). An unknown or repeated key is refused, so a key a
    later client sends fails loudly against a renderer that does not know it."""
    if isinstance(query, bytes):
        try:
            query = query.decode("ascii")
        except UnicodeDecodeError:
            raise RenderRequestInvalid("query is not plain text") from None
    if not isinstance(query, str) or len(query) > QUERY_MAX_CHARS:
        raise RenderRequestInvalid("query is too long")
    found: dict[str, str] = {}
    for part in query.split("&") if query else []:
        key, sep, value = part.partition("=")
        if not sep:
            raise RenderRequestInvalid("query has a key with no value")
        if key not in _QUERY_KEYS:
            raise RenderRequestInvalid("query has an unknown key")
        if key in found:
            raise RenderRequestInvalid("query repeats a key")
        found[key] = value
    if "w" not in found or "h" not in found:
        raise RenderRequestInvalid("query needs w and h")
    for value in found.values():
        if not _DIGITS.fullmatch(value):
            raise RenderRequestInvalid("query value is not a number")
    width, height = int(found["w"]), int(found["h"])
    if not WIDTH_MIN <= width <= WIDTH_MAX or not HEIGHT_MIN <= height <= HEIGHT_MAX:
        raise RenderRequestInvalid("query asks for a size out of bounds")
    flag = found.get("check", "0")
    if flag not in ("0", "1"):
        raise RenderRequestInvalid("query check is not 0 or 1")
    return width, height, flag == "1"


def call(url: str, html: str, width: int, height: int, check: bool) -> Report:
    """Render `html` (already stamped) at `width` x `height` on the renderer at
    `url` (`http://host:port`), and return its answer as a `Report`. Raises
    RenderUnavailable for anything short of a 200 whose answer passes
    `parse_answer`: no answer within CONNECT_TIMEOUT_SECONDS, the whole call
    over CALL_DEADLINE_SECONDS (a timer cuts the socket, so an answer dripped a
    byte at a time cannot hold it open), any other status (a redirect is never
    followed, a 503 is not waited out), or an answer over RESPONSE_MAX_BYTES or
    failing a check. No proxy is used: the request goes to `url` itself."""
    parts = urlsplit(url)
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        raise RenderUnavailable() from None
    if parts.scheme != "http" or not host or port is None:
        raise RenderUnavailable()
    body = html.encode("utf-8")
    target = f"/v1/render?w={int(width)}&h={int(height)}&check={1 if check else 0}"
    deadline = time.monotonic() + CALL_DEADLINE_SECONDS
    conn = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT_SECONDS)
    # A dup of the connected socket's descriptor, for the timer: http.client drops
    # `conn.sock` once a response says it will close, while the response reads on.
    handle: list[socket.socket] = []
    expired = threading.Event()

    def cut() -> None:
        expired.set()
        for sock in tuple(handle):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(CALL_DEADLINE_SECONDS, cut)
    timer.daemon = True
    timer.start()
    try:
        conn.connect()
        handle.append(socket.socket(fileno=os.dup(conn.sock.fileno())))
        if expired.is_set():
            raise RenderUnavailable()
        left = deadline - time.monotonic()
        if left <= 0:
            raise RenderUnavailable()
        conn.sock.settimeout(left)
        conn.request(
            "POST",
            target,
            body,
            {
                RENDER_HEADER: RENDER_HEADER_VALUE,
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": str(len(body)),
            },
        )
        response = conn.getresponse()
        if response.status != 200:
            raise RenderUnavailable()
        declared = response.getheader("Content-Length")
        if declared is not None and (
            not declared.isdigit() or int(declared) > RESPONSE_MAX_BYTES
        ):
            raise RenderUnavailable()
        raw = response.read(RESPONSE_MAX_BYTES + 1)
        if len(raw) > RESPONSE_MAX_BYTES:
            raise RenderUnavailable()
        return parse_answer(raw, width, height)
    except RenderUnavailable:
        raise
    except (OSError, http.client.HTTPException, RenderAnswerInvalid, ValueError):
        raise RenderUnavailable() from None
    finally:
        timer.cancel()
        conn.close()
        for sock in handle:
            sock.close()
