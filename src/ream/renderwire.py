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

`call` is the app's side of that wire: a request to a renderer at
`http://host:port` (`REAM_RENDERER_URL`), asked once more after a 503's
`Retry-After` when that fits in the deadline, and anything short of a valid 200
answer raises `RenderUnavailable`. A `not_run` answer is a `Report` whose
`reason` is NOT_RUN, so the app can say the renderer did not render.

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
import unicodedata
import zlib
from urllib.parse import quote, unquote, urlsplit

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
# How many chunks a picture may hold (a browser's screenshot holds a few dozen).
PNG_MAX_CHUNKS = 8192
_PNG_CHUNK_MAX_LENGTH = 2**31 - 1
# The bit depths each PNG colour type allows.
_PNG_DEPTHS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
# A renderer busy: the longest Retry-After waited for (the call's deadline bounds it too).
RETRY_AFTER_MAX_SECONDS = 10
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


# Why a render did not run, when that is not "no browser here" (`Report.reason`):
# the renderer gave no usable answer, or answered that it did not render.
UNAVAILABLE = "renderer_unavailable"
NOT_RUN = "renderer_not_run"
REASONS = (UNAVAILABLE, NOT_RUN)


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
    RenderAnswerInvalid. Walks every chunk: the signature; each chunk's length
    within the data and its CRC correct; at most PNG_MAX_CHUNKS of them; exactly
    one IHDR, first, 13 bytes, of the size asked for, with a bit depth its colour
    type allows (and compression, filter and interlace methods PNG has); at
    least one IDAT; and IEND last, empty, with nothing after it. The pixels
    themselves are not decoded."""
    if not isinstance(data, bytes):
        raise RenderAnswerInvalid("picture is not bytes")
    if len(data) > PNG_MAX_BYTES:
        raise RenderAnswerInvalid("picture is over the size limit")
    if not data.startswith(_PNG_MAGIC) or len(data) < _PNG_MIN_BYTES:
        raise RenderAnswerInvalid("picture is not a PNG")
    view = memoryview(data)
    pos, chunks, idat = len(_PNG_MAGIC), 0, False
    while True:
        if pos == len(data):
            raise RenderAnswerInvalid("picture has no end chunk")
        if len(data) - pos < 12:
            raise RenderAnswerInvalid("picture has a chunk cut short")
        (length,) = struct.unpack_from(">I", data, pos)
        kind = data[pos + 4 : pos + 8]
        if length > _PNG_CHUNK_MAX_LENGTH or len(data) - pos - 12 < length:
            raise RenderAnswerInvalid("picture has a chunk cut short")
        (crc,) = struct.unpack_from(">I", data, pos + 8 + length)
        if crc != zlib.crc32(view[pos + 4 : pos + 8 + length]):
            raise RenderAnswerInvalid("picture has a chunk checksum that is wrong")
        chunks += 1
        if chunks > PNG_MAX_CHUNKS:
            raise RenderAnswerInvalid("picture has too many chunks")
        if chunks == 1:
            if kind != b"IHDR" or length != _IHDR_LENGTH:
                raise RenderAnswerInvalid("picture's first chunk is not a PNG header")
            _ihdr(data[pos + 8 : pos + 8 + length], width, height)
        elif kind == b"IHDR":
            raise RenderAnswerInvalid("picture has a second PNG header")
        elif kind == b"IDAT":
            idat = True
        elif kind == b"IEND":
            if length != 0:
                raise RenderAnswerInvalid("picture has no end chunk")
            if pos + 12 != len(data):
                raise RenderAnswerInvalid("picture has bytes after its end chunk")
            break
        pos += 12 + length
    if not idat:
        raise RenderAnswerInvalid("picture has no image data")
    return data


def _ihdr(fields: bytes, width: int, height: int) -> None:
    w, h, depth, colour, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", fields
    )
    if (w, h) != (width, height):
        raise RenderAnswerInvalid("picture's size is not the size asked for")
    if depth not in _PNG_DEPTHS.get(colour, ()):
        raise RenderAnswerInvalid("picture's bit depth and colour type do not go")
    if compression != 0 or filtering != 0 or interlace not in (0, 1):
        raise RenderAnswerInvalid("picture's header names a method PNG has not")


def check_error_text(item) -> str:
    """`item` if it is a string a page's script error may be: no control character
    (Unicode category Cc, tab and newline included) and nothing that does not
    encode as UTF-8 (a lone surrogate). RenderAnswerInvalid otherwise."""
    if not isinstance(item, str):
        raise RenderAnswerInvalid("an error is not a short string")
    try:
        item.encode("utf-8")
    except UnicodeEncodeError:
        raise RenderAnswerInvalid("an error is not UTF-8") from None
    if any(unicodedata.category(c) == "Cc" for c in item):
        raise RenderAnswerInvalid("an error holds a control character")
    return item


def _errors(value) -> list[str]:
    if not isinstance(value, list) or len(value) > ERRORS_MAX_COUNT:
        raise RenderAnswerInvalid("errors are not a short list")
    for item in value:
        if not isinstance(item, str) or len(item) > ERROR_MAX_CHARS:
            raise RenderAnswerInvalid("an error is not a short string")
        check_error_text(item)
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
        return Report(None, None, None, reason=NOT_RUN)
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


class _Busy(Exception):
    """The renderer answered 503; `after` is its Retry-After in seconds, or None."""

    def __init__(self, after: int | None) -> None:
        super().__init__()
        self.after = after


# Waits between the two tries of a busy renderer (a test's to replace).
_sleep = time.sleep


def call(url: str, html: str, width: int, height: int, check: bool) -> Report:
    """Render `html` (already stamped) at `width` x `height` on the renderer at
    `url` (`http://host:port`), and return its answer as a `Report`. Raises
    RenderUnavailable for anything short of a 200 whose answer passes
    `parse_answer`: no answer within CONNECT_TIMEOUT_SECONDS, the whole call
    over CALL_DEADLINE_SECONDS (a timer cuts the socket, so an answer dripped a
    byte at a time cannot hold it open), any other status (a redirect is never
    followed), or an answer over RESPONSE_MAX_BYTES or failing a check. A 503
    is asked once more after its `Retry-After` (whole seconds, at most
    RETRY_AFTER_MAX_SECONDS), when the second try can start within the same
    deadline; a 503 without one, or a second 503, is unavailable. No proxy is
    used: the request goes to `url` itself."""
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
    try:
        return _ask(host, port, target, body, width, height, deadline)
    except _Busy as busy:
        if busy.after is None or time.monotonic() + busy.after >= deadline:
            raise RenderUnavailable() from None
        _sleep(busy.after)
    try:
        return _ask(host, port, target, body, width, height, deadline)
    except _Busy:
        raise RenderUnavailable() from None


def _retry_after(value: str | None) -> int | None:
    if value is None or not re.fullmatch(r"[0-9]{1,3}", value):
        return None
    after = int(value)
    return after if after <= RETRY_AFTER_MAX_SECONDS else None


def _ask(host, port, target, body, width, height, deadline) -> Report:
    """One request of `call`'s, all of it before `deadline` (monotonic)."""
    raw = _post(host, port, target, body, deadline)
    try:
        return parse_answer(raw, width, height)
    except RenderAnswerInvalid:
        raise RenderUnavailable() from None


def _post(host, port, target, body, deadline) -> bytes:
    """One POST of `body` (HTML) to `target`, all of it before `deadline`
    (monotonic): the 200 answer's bytes, _Busy for a 503, RenderUnavailable for
    anything else."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise RenderUnavailable()
    conn = http.client.HTTPConnection(
        host, port, timeout=min(CONNECT_TIMEOUT_SECONDS, left)
    )
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

    timer = threading.Timer(left, cut)
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
        if response.status == 503:
            raise _Busy(_retry_after(response.getheader("Retry-After")))
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
        return raw
    except (RenderUnavailable, _Busy):
        raise
    except (OSError, http.client.HTTPException, ValueError):
        raise RenderUnavailable() from None
    finally:
        timer.cancel()
        conn.close()
        for sock in handle:
            sock.close()


# --- a screenshot ------------------------------------------------------------------
#
# A screenshot is a picture of a page as a reader sees it, whole or one part of it,
# taken on request (`rendercore.shoot`), never stored. Over the wire it is its own
# route, `POST /v1/shot?w=&theme=[&element=|&section=]`, with the stamped HTML as the
# body; a renderer that does not know the route answers 404, and a shot key sent to
# `/v1/render` is a 400 there (`parse_request_query`), so an old renderer fails
# loudly rather than answering a render for a shot. It answers 200 with JSON:
#
#     {"v": 1, "outcome": ..., "png_base64": ..., "width": ..., "height": ...,
#      "page_height": ..., "truncated": ..., "matches": ...}
#
# `outcome` is one of SHOT_OUTCOMES (`Shot.outcome` says what each means). A busy
# renderer answers 503 with Retry-After, as for a render.

# The widths a shot is taken at, and the height of the window the page is laid out
# in at each: a desktop and a phone.
SHOT_VIEWPORTS = {1280: 800, 390: 844}
SHOT_THEMES = ("light", "dark")
# The tallest picture: a page or part taller than this is cut to it, and the answer
# says so (`truncated`). The widest: a part wider than this (a table that overflows
# the page) is cut to it, also `truncated`.
SHOT_MAX_HEIGHT = 8000
SHOT_MAX_WIDTH = 2560
# The largest picture kept, encoded; a larger one is the outcome `picture_too_big`.
SHOT_PNG_MAX_BYTES = 5 * 1024 * 1024
SHOT_PNG_BASE64_MAX_CHARS = 4 * ((SHOT_PNG_MAX_BYTES + 2) // 3)
# An element is named by its id, under the same rule as an anchor's
# (`anchors.ID_RE`; `tests/test_shots.py` holds them equal), and a section by its
# heading's words, at most as long as an anchor's heading (`anchors.HEADING_MAX`).
SHOT_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
SHOT_SECTION_MAX_CHARS = 200
SHOT_QUERY_MAX_CHARS = 4096
# How many matches a refused selector reports, at most.
SHOT_MATCHES_MAX = 10**6

SHOT_OUTCOMES = (
    "shot",
    "no_match",
    "ambiguous",
    "empty",
    "page_too_big",
    "picture_too_big",
    "timeout",
    "memory",
    "busy",
    "no_browser",
    "failed",
)
_SHOT_KEYS = frozenset(
    {
        "v",
        "outcome",
        "png_base64",
        "width",
        "height",
        "page_height",
        "truncated",
        "matches",
    }
)
_SHOT_QUERY_KEYS = frozenset({"w", "theme", "element", "section"})


class ShotRequestInvalid(ValueError):
    """A screenshot's parameters failed a check. `field` names the one at fault;
    the message is a fixed phrase."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class ShotRequest(tuple):
    """`(width, theme, element, section)`, checked (`shot_request`)."""

    def __new__(cls, width, theme, element, section):
        return super().__new__(cls, (width, theme, element, section))

    width = property(lambda self: self[0])
    theme = property(lambda self: self[1])
    element = property(lambda self: self[2])
    section = property(lambda self: self[3])


def fold_heading(text: str) -> str:
    """A heading's words as a section is matched by them: runs of white space as
    one space, trimmed, lower-cased (as `anchors` and the pin script fold them)."""
    return " ".join(text.split()).lower()


def shot_request(width=1280, theme="light", element=None, section=None) -> ShotRequest:
    """The checked parameters of a screenshot, or ShotRequestInvalid naming the
    field: `width` one of SHOT_VIEWPORTS, `theme` one of SHOT_THEMES, at most one
    of `element` (an id, SHOT_ID_RE) and `section` (a heading's words, folded,
    non-empty, at most SHOT_SECTION_MAX_CHARS, no control character)."""
    if not _is_int(width) or width not in SHOT_VIEWPORTS:
        raise ShotRequestInvalid("width", "width is not one a shot is taken at")
    if theme not in SHOT_THEMES:
        raise ShotRequestInvalid("theme", "theme is not light or dark")
    if element is not None and section is not None:
        raise ShotRequestInvalid("element", "name an element or a section, not both")
    if element is not None and (
        not isinstance(element, str) or not SHOT_ID_RE.fullmatch(element)
    ):
        raise ShotRequestInvalid("element", "element is not an id")
    if section is not None:
        if not isinstance(section, str) or len(section) > SHOT_SECTION_MAX_CHARS:
            raise ShotRequestInvalid("section", "section is not a heading's words")
        try:
            section.encode("utf-8")
        except UnicodeEncodeError:
            raise ShotRequestInvalid("section", "section is not UTF-8") from None
        if any(unicodedata.category(c) == "Cc" for c in section):
            raise ShotRequestInvalid("section", "section holds a control character")
        section = fold_heading(section)
        if not section:
            raise ShotRequestInvalid("section", "section is not a heading's words")
    return ShotRequest(width, theme, element, section)


def shot_query(request: ShotRequest) -> str:
    """The query string of `POST /v1/shot` for `request`."""
    parts = [f"w={int(request.width)}", f"theme={request.theme}"]
    if request.element is not None:
        parts.append("element=" + quote(request.element, safe=""))
    if request.section is not None:
        parts.append("section=" + quote(request.section, safe=""))
    return "&".join(parts)


def parse_shot_query(query: str | bytes) -> ShotRequest:
    """The `ShotRequest` a `/v1/shot` query stands for, or ShotRequestInvalid. An
    unknown or repeated key is refused, as on `/v1/render`."""
    if isinstance(query, bytes):
        try:
            query = query.decode("ascii")
        except UnicodeDecodeError:
            raise ShotRequestInvalid("query", "query is not plain text") from None
    if not isinstance(query, str) or len(query) > SHOT_QUERY_MAX_CHARS:
        raise ShotRequestInvalid("query", "query is too long")
    found: dict[str, str] = {}
    for part in query.split("&") if query else []:
        key, sep, value = part.partition("=")
        if not sep:
            raise ShotRequestInvalid("query", "query has a key with no value")
        if key not in _SHOT_QUERY_KEYS:
            raise ShotRequestInvalid("query", "query has an unknown key")
        if key in found:
            raise ShotRequestInvalid("query", "query repeats a key")
        try:
            found[key] = unquote(value, errors="strict")
        except UnicodeDecodeError:
            raise ShotRequestInvalid(key, f"{key} is not UTF-8") from None
    if "w" not in found or "theme" not in found:
        raise ShotRequestInvalid("query", "query needs w and theme")
    if not _DIGITS.fullmatch(found["w"]):
        raise ShotRequestInvalid("width", "width is not one a shot is taken at")
    return shot_request(
        int(found["w"]), found["theme"], found.get("element"), found.get("section")
    )


class Shot(tuple):
    """What a screenshot came to: `(outcome, png)`, with the rest as attributes.

    `outcome`, one of SHOT_OUTCOMES:
      * `shot`: `png` is the picture, `width` x `height`; `page_height` is the
        whole page's height, and `truncated` says the part shot was cut to
        SHOT_MAX_HEIGHT (or SHOT_MAX_WIDTH).
      * `no_match`, `ambiguous`: the element or section named matched no part of
        the page, or `matches` (two or more) of them.
      * `empty`: what was named is drawn with no size.
      * `page_too_big`: the page is over the render limit; no browser ran.
      * `picture_too_big`: the picture came to more than SHOT_PNG_MAX_BYTES.
      * `timeout`, `memory`: the browser ran out of time, or past the memory
        ceiling, and was ended.
      * `busy`: another render held the browser past the wait.
      * `no_browser`: there is no browser here.
      * `failed`: anything else; nothing of what went wrong is passed on.
    """

    outcome: str
    png: bytes | None
    width: int
    height: int
    page_height: int
    truncated: bool
    matches: int | None

    def __new__(
        cls,
        outcome,
        png=None,
        *,
        width=0,
        height=0,
        page_height=0,
        truncated=False,
        matches=None,
    ):
        self = super().__new__(cls, (outcome, png))
        self.outcome = outcome
        self.png = png
        self.width = width
        self.height = height
        self.page_height = page_height
        self.truncated = truncated
        self.matches = matches
        return self

    def __repr__(self) -> str:  # never the picture's bytes
        return (
            f"Shot({self.outcome!r}, {self.width}x{self.height},"
            f" page_height={self.page_height}, truncated={self.truncated},"
            f" matches={self.matches})"
        )


def _bounded_int(value, low: int, high: int, what: str) -> int:
    if not _is_int(value) or not low <= value <= high:
        raise RenderAnswerInvalid(f"{what} is out of bounds")
    return value


def check_shot(shot: Shot) -> Shot:
    """`shot` if it holds together (the same rule both sides apply), else
    RenderAnswerInvalid: a picture only with `shot`, of its stated size within the
    limits; `matches` only with `no_match` (0) and `ambiguous` (2 or more)."""
    if not isinstance(shot, Shot) or shot.outcome not in SHOT_OUTCOMES:
        raise RenderAnswerInvalid("shot has an unknown outcome")
    if shot.outcome == "shot":
        width = _bounded_int(shot.width, 1, SHOT_MAX_WIDTH, "shot width")
        height = _bounded_int(shot.height, 1, SHOT_MAX_HEIGHT, "shot height")
        _bounded_int(shot.page_height, 0, CHECK_MAX, "page height")
        if type(shot.truncated) is not bool:
            raise RenderAnswerInvalid("truncated is not true or false")
        if shot.matches is not None:
            raise RenderAnswerInvalid("a shot reports no matches")
        if not isinstance(shot.png, bytes) or len(shot.png) > SHOT_PNG_MAX_BYTES:
            raise RenderAnswerInvalid("picture is over the size limit")
        validate_png(shot.png, width, height)
        return shot
    if shot.png is not None or shot.width or shot.height or shot.page_height:
        raise RenderAnswerInvalid("a shot not taken has no picture")
    if shot.truncated is not False:
        raise RenderAnswerInvalid("a shot not taken is not truncated")
    if shot.outcome == "no_match":
        if shot.matches != 0 or type(shot.matches) is not int:
            raise RenderAnswerInvalid("no match reports 0 matches")
    elif shot.outcome == "ambiguous":
        _bounded_int(shot.matches, 2, SHOT_MATCHES_MAX, "matches")
    elif shot.matches is not None:
        raise RenderAnswerInvalid("only a selector's outcome reports matches")
    return shot


def encode_shot_answer(shot: Shot) -> bytes:
    """The JSON answer for `shot`, the renderer's side of `parse_shot_answer`;
    RenderAnswerInvalid rather than send one the app would refuse."""
    check_shot(shot)
    encoded = None
    if shot.png is not None:
        encoded = binascii.b2a_base64(shot.png, newline=False).decode("ascii")
    return json.dumps(
        {
            "v": WIRE_VERSION,
            "outcome": shot.outcome,
            "png_base64": encoded,
            "width": shot.width,
            "height": shot.height,
            "page_height": shot.page_height,
            "truncated": shot.truncated,
            "matches": shot.matches,
        },
        separators=(",", ":"),
    ).encode("ascii")


def parse_shot_answer(raw: bytes) -> Shot:
    """The `Shot` a renderer's JSON answer stands for, or RenderAnswerInvalid."""
    if not isinstance(raw, bytes) or len(raw) > RESPONSE_MAX_BYTES:
        raise RenderAnswerInvalid("answer is over the size limit")
    try:
        answer = json.loads(
            raw, object_pairs_hook=_no_duplicates, parse_constant=_no_constant
        )
    except RenderAnswerInvalid:
        raise
    except (ValueError, RecursionError):
        raise RenderAnswerInvalid("answer is not JSON") from None
    if not isinstance(answer, dict) or set(answer) != _SHOT_KEYS:
        raise RenderAnswerInvalid("answer does not have exactly its keys")
    if not _is_int(answer["v"]) or answer["v"] != WIRE_VERSION:
        raise RenderAnswerInvalid("answer is of an unknown version")
    if not isinstance(answer["outcome"], str):
        raise RenderAnswerInvalid("shot has an unknown outcome")
    encoded, png = answer["png_base64"], None
    if encoded is not None:
        if (
            not isinstance(encoded, str)
            or len(encoded) > SHOT_PNG_BASE64_MAX_CHARS
            or not encoded.isascii()
        ):
            raise RenderAnswerInvalid("picture is over the size limit")
        try:
            png = binascii.a2b_base64(encoded.encode("ascii"), strict_mode=True)
        except binascii.Error:
            raise RenderAnswerInvalid("picture is not strict base64") from None
    return check_shot(
        Shot(
            answer["outcome"],
            png,
            width=answer["width"],
            height=answer["height"],
            page_height=answer["page_height"],
            truncated=answer["truncated"],
            matches=answer["matches"],
        )
    )


def call_shot(url: str, html: str, request: ShotRequest) -> Shot:
    """Take a screenshot of `html` (already stamped) on the renderer at `url`, as
    `call` renders: the same deadline, no redirect, no proxy. A 503 is the outcome
    `busy` (the caller says so to whoever asked, rather than wait here); anything
    else short of a 200 whose answer passes `parse_shot_answer` raises
    RenderUnavailable."""
    parts = urlsplit(url)
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        raise RenderUnavailable() from None
    if parts.scheme != "http" or not host or port is None:
        raise RenderUnavailable()
    target = "/v1/shot?" + shot_query(request)
    deadline = time.monotonic() + CALL_DEADLINE_SECONDS
    try:
        raw = _post(host, port, target, html.encode("utf-8"), deadline)
    except _Busy:
        return Shot("busy")
    try:
        return parse_shot_answer(raw)
    except RenderAnswerInvalid:
        raise RenderUnavailable() from None
