"""Pictures on a comment: what is accepted, what is kept, where it lives, who may see it.

Pinned comments, slice 3 (docs/agent/specs/2026-10-29-pinned-comments.md section 6). Bytes
arrive from the network, so every rule here runs BEFORE the next costs anything, in this order:

1. the request body is read only up to the cap (`api.py`, with `BODY_SLACK` for the multipart
   framing) and the one part is lifted out by `file_from_multipart`;
2. the file is at most `MAX_BYTES`;
3. its type is decided from its BYTES (`inspect`), never from a client's Content-Type or name;
4. the size it declares in its header is within `MAX_SIDE` and `MAX_PIXELS`;
5. the file is walked to its end and REWRITTEN, keeping only the chunks that draw the picture;
6. only then the database: quotas, same-bytes-same-id, the row.

NO DECODER. The service never decodes pixels, so a decompression bomb costs it nothing: it
reads the header for the size and copies chunks. Camera EXIF (GPS, device), XMP and text chunks
never reach storage; what is stored, and the `sha256` kept with it, is the rewritten file.

Pictures are not scanned for secrets: pixels cannot be (a stated gap, spec 6.3).

This module imports nothing from the service, so `artifacts-decide` checks a file with the same
code before it uploads one. Storage, serving and tokens are `image_store`.
"""

from __future__ import annotations

import hashlib
import re
import struct
import zlib
from dataclasses import dataclass
from datetime import timedelta

MAX_BYTES = 5 * 1024 * 1024
MAX_SIDE = 4000
MAX_PIXELS = 16 * 1000 * 1000
PER_COMMENT = 4
PER_PAGE = 40
STAGED_PER_AUTHOR = 12
STAGED_TTL = timedelta(hours=1)
# What a multipart envelope (boundary lines, part headers) may add to the file itself before
# the body is refused unread. The file is then held to MAX_BYTES exactly.
BODY_SLACK = 16 * 1024

_ID = re.compile(r"[a-z2-7]{26}")
_NAMED = "send PNG, JPEG or WebP"

# A real picture has a handful of chunks or segments; a crafted one can have hundreds of
# thousands, each a pass round a Python loop.
MAX_CHUNKS = 10_000


def _too_many() -> ImageRefused:
    return ImageRefused(
        f"the image has more than {MAX_CHUNKS} chunks or segments: it is not taken"
    )


class ImageRefused(Exception):
    """A picture (or an attach) that cannot be taken. `status` is 413 for size, else 422.
    `detail` names the reason and never echoes the content."""

    def __init__(self, detail: str, status: int = 422, field: str = "image") -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.field = field


@dataclass(frozen=True)
class Inspected:
    content_type: str
    width: int
    height: int
    data: bytes  # the rewritten file
    sha256: str


def _refuse(detail: str) -> ImageRefused:
    return ImageRefused(detail)


def _truncated() -> ImageRefused:
    return _refuse("the image is cut off or damaged: it could not be read to its end")


# --- the multipart envelope ---------------------------------------------------------------


def file_from_multipart(content_type: str | None, body: bytes) -> bytes:
    """The bytes of the ONE file in a multipart/form-data body. The part's own Content-Type
    and file name are ignored (the type comes from the bytes, the name is not stored)."""
    ask = _refuse("send multipart/form-data with exactly one file in the field `file`")
    header = content_type or ""
    if not header.lower().lstrip().startswith("multipart/form-data"):
        raise ask
    found = re.search(
        r'boundary=(?:"([^"\r\n]{1,70})"|([^;\s"]{1,70}))', header, re.IGNORECASE
    )
    if not found:
        raise ask
    delimiter = b"--" + (found.group(1) or found.group(2)).encode("latin-1")
    pieces = body.split(delimiter)
    if len(pieces) < 3 or not pieces[-1].startswith(b"--"):
        raise ask  # no closing delimiter: the body was cut short, or is not multipart
    parts = [p for p in pieces[1:] if not p.startswith(b"--")]
    if len(parts) != 1:
        raise ask
    head, sep, content = parts[0].lstrip(b"\r\n").partition(b"\r\n\r\n")
    if not sep:
        raise ask
    named = re.search(rb'name="([^"]*)"', head, re.IGNORECASE)
    if not named or named.group(1) not in (b"file", b"image"):
        raise ask
    return content.removesuffix(b"\r\n")


# --- sniffing ------------------------------------------------------------------------------


_REFUSED_BY_MAGIC = (
    (lambda d: d[:6] in (b"GIF87a", b"GIF89a"), "GIF"),
    (lambda d: d[:4] in (b"II*\x00", b"MM\x00*"), "TIFF"),
    (lambda d: d[:2] == b"BM", "BMP"),
    (lambda d: d[4:8] == b"ftyp" and d[8:12] in (b"avif", b"avis"), "AVIF"),
    (
        lambda d: (
            d[4:8] == b"ftyp"
            and d[8:12]
            in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1")
        ),
        "HEIC",
    ),
)


def _sniff(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    for matches, name in _REFUSED_BY_MAGIC:
        if matches(data):
            raise _refuse(f"{name} is not accepted: {_NAMED}")
    head = data[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<svg", b"<?xml", b"<!doctype svg")):
        raise _refuse(f"SVG is not accepted: {_NAMED}")
    raise _refuse(f"this is not a PNG, JPEG or WebP image: {_NAMED}")


def _check_size(width: int, height: int) -> None:
    if width < 1 or height < 1:
        raise _refuse("the image declares no size")
    if width > MAX_SIDE or height > MAX_SIDE:
        raise _refuse(
            f"the image is {width} x {height} px: at most {MAX_SIDE} px a side"
        )
    if width * height > MAX_PIXELS:
        raise _refuse(
            f"the image is {width * height / 1e6:.1f} megapixels: at most "
            f"{MAX_PIXELS // 1_000_000}"
        )


# --- PNG ----------------------------------------------------------------------------------

_PNG_KEEP = frozenset(
    {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"sRGB", b"gAMA", b"cHRM", b"iCCP"}
)
_PNG_CRITICAL = frozenset({b"IHDR", b"PLTE", b"IDAT", b"IEND"})


def _png(data: bytes) -> tuple[int, int, bytes]:
    pos, size = 8, len(data)
    out = [data[:8]]
    width = height = 0
    first = True
    idat = ended = False
    chunks = 0
    while pos < size and not ended:
        chunks += 1
        if chunks > MAX_CHUNKS:
            raise _too_many()
        if pos + 8 > size:
            raise _truncated()
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if length > 0x7FFFFFFF or end > size:
            raise _truncated()
        if not kind.isalpha():
            raise _truncated()
        body = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[end - 4 : end])
        if zlib.crc32(kind + body) != crc:
            raise _truncated()
        if first:
            if kind != b"IHDR" or length != 13:
                raise _truncated()
            width, height, depth, colour, method, filtering, interlace = struct.unpack(
                ">IIBBBBB", body
            )
            if (
                depth not in (1, 2, 4, 8, 16)
                or colour not in (0, 2, 3, 4, 6)
                or method
                or filtering
                or interlace > 1
            ):
                raise _truncated()
            _check_size(width, height)
            first = False
        elif kind == b"acTL":
            raise _refuse(f"APNG (an animated PNG) is not accepted: {_NAMED}")
        elif kind == b"IHDR":
            raise _truncated()
        elif kind[0:1].isupper() and kind not in _PNG_CRITICAL:
            raise _refuse("this PNG needs a feature the service does not take")
        if kind == b"IDAT":
            idat = True
        if kind in _PNG_KEEP:
            out.append(data[pos:end])
        ended = kind == b"IEND"
        pos = end
    if first or not ended or not idat:
        raise _truncated()
    return width, height, b"".join(out)


# --- JPEG ---------------------------------------------------------------------------------

# An allowlist. Kept: the table, frame and scan markers a decoder needs, and only those
# application segments that change how the picture is DRAWN AND whose payload begins with the
# signature that proves it: JFIF (APP0), the ICC profile (APP2) and Adobe's colour flag
# (APP14). Every other APPn (EXIF and XMP in APP1, Photoshop's APP13, an APP2 that is not ICC),
# every comment and the reserved markers (0x02-0xBF, 0xF0-0xFD) are dropped.
_JPEG_APP_SIGNATURE = {0xE0: b"JFIF\x00", 0xE2: b"ICC_PROFILE\x00", 0xEE: b"Adobe"}
_JPEG_FILL = re.compile(rb"\xff+")
_JPEG_SCAN_END = re.compile(rb"\xff[^\x00\xd0-\xd7\xff]")


def _jpeg_keeps(marker: int, segment: bytes) -> bool:
    if 0xE0 <= marker <= 0xEF:
        signature = _JPEG_APP_SIGNATURE.get(marker)
        return signature is not None and segment[2:].startswith(signature)
    return not (marker == 0xFE or 0x02 <= marker <= 0xBF or 0xF0 <= marker <= 0xFD)


_JPEG_SOF = frozenset({0xC0, 0xC1, 0xC2})
_JPEG_OTHER_SOF = frozenset(
    {0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _jpeg(data: bytes) -> tuple[int, int, bytes]:
    size, pos = len(data), 2
    out = [b"\xff\xd8"]
    width = height = 0
    framed = scanned = False
    segments = 0
    while True:
        segments += 1
        if segments > MAX_CHUNKS:
            raise _too_many()
        if pos >= size or data[pos] != 0xFF:
            raise _truncated()
        pos = _JPEG_FILL.match(data, pos).end()  # fill bytes
        if pos >= size:
            raise _truncated()
        marker = data[pos]
        pos += 1
        if marker == 0xD9:  # EOI
            if not scanned:
                raise _truncated()
            out.append(b"\xff\xd9")
            return width, height, b"".join(out)
        if marker in (0x00, 0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            raise _truncated()  # not valid between segments
        if pos + 2 > size:
            raise _truncated()
        (length,) = struct.unpack(">H", data[pos : pos + 2])
        end = pos + length
        if length < 2 or end > size:
            raise _truncated()
        segment = data[pos:end]
        if marker in _JPEG_OTHER_SOF:
            raise _refuse(
                "this JPEG is lossless, arithmetic or hierarchical: not taken"
            )
        if marker in _JPEG_SOF:
            if framed or length < 8:
                raise _truncated()
            _precision, height, width = struct.unpack(">BHH", segment[2:7])
            _check_size(width, height)
            framed = True
        if _jpeg_keeps(marker, segment):
            out.append(b"\xff" + bytes([marker]) + segment)
        pos = end
        if (
            marker == 0xDA
        ):  # SOS: entropy-coded data follows, up to the next real marker
            if not framed:
                raise _truncated()
            found = _JPEG_SCAN_END.search(data, pos)
            if found is None:
                raise _truncated()
            out.append(data[pos : found.start()])
            pos = found.start()
            scanned = True


# --- WebP ---------------------------------------------------------------------------------

_WEBP_KEEP = frozenset({b"ICCP", b"ALPH", b"VP8 ", b"VP8L"})


def _le24(raw: bytes) -> int:
    return raw[0] | raw[1] << 8 | raw[2] << 16


def _webp(data: bytes) -> tuple[int, int, bytes]:
    if len(data) < 20:
        raise _truncated()
    (riff,) = struct.unpack("<I", data[4:8])
    total = riff + 8
    if riff < 12 or total > len(data):
        raise _truncated()
    pos = 12
    canvas: tuple[int, int] | None = None
    image: tuple[int, int] | None = None
    kept: list[bytes] = []
    flags = 0
    vp8x: bytes | None = None
    chunks = 0
    while pos < total:
        if chunks >= MAX_CHUNKS:
            raise _too_many()
        if pos + 8 > total:
            raise _truncated()
        fourcc = data[pos : pos + 4]
        (length,) = struct.unpack("<I", data[pos + 4 : pos + 8])
        body_end = pos + 8 + length
        padded = body_end + (length & 1)
        if body_end > total or (padded > total and body_end != total):
            raise _truncated()
        body = data[pos + 8 : body_end]
        if chunks == 0 and fourcc == b"VP8X":
            if length < 10:
                raise _truncated()
            flags = body[0]
            if flags & 0x02:
                raise _refuse(f"animated WebP is not accepted: {_NAMED}")
            canvas = (1 + _le24(body[4:7]), 1 + _le24(body[7:10]))
            vp8x = body
        elif fourcc in (b"ANIM", b"ANMF"):
            raise _refuse(f"animated WebP is not accepted: {_NAMED}")
        elif fourcc in (b"VP8 ", b"VP8L"):
            if image is not None:
                raise _truncated()
            if fourcc == b"VP8 ":
                if length < 10 or body[3:6] != b"\x9d\x01\x2a":
                    raise _truncated()
                image = (
                    struct.unpack("<H", body[6:8])[0] & 0x3FFF,
                    struct.unpack("<H", body[8:10])[0] & 0x3FFF,
                )
            else:
                if length < 5 or body[0] != 0x2F:
                    raise _truncated()
                (bits,) = struct.unpack("<I", body[1:5])
                image = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        chunks += 1
        if fourcc in _WEBP_KEEP:
            kept.append(
                fourcc + struct.pack("<I", length) + body + b"\x00" * (length & 1)
            )
        pos = padded
    if image is None or (canvas is not None and canvas != image):
        raise _truncated()
    _check_size(*image)
    head = b""
    if vp8x is not None:
        # The flags no longer promise EXIF (0x08) or XMP (0x04): both chunks are gone.
        # Only the fixed 10 bytes are kept: anything after them is not part of the format.
        rewritten = bytes([flags & ~0x0C & 0xFF]) + vp8x[1:10]
        head = b"VP8X" + struct.pack("<I", len(rewritten)) + rewritten
        head += b"\x00" * (len(rewritten) & 1)
    payload = head + b"".join(kept)
    return (
        image[0],
        image[1],
        b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WEBP" + payload,
    )


# --- the one door ---------------------------------------------------------------------------

_KINDS = {
    "png": ("image/png", _png),
    "jpeg": ("image/jpeg", _jpeg),
    "webp": ("image/webp", _webp),
}


def inspect(data: bytes) -> Inspected:
    """Decide the type from the bytes, read the size from the header, walk the file to its end
    and return it REWRITTEN. Raises `ImageRefused` (413 over the size cap, else 422)."""
    if len(data) > MAX_BYTES:
        raise ImageRefused(
            f"the image is over {MAX_BYTES // (1024 * 1024)} MB", status=413
        )
    if not data:
        raise _refuse("the file is empty")
    content_type, walk = _KINDS[_sniff(data)]
    width, height, rewritten = walk(data)
    return Inspected(
        content_type, width, height, rewritten, hashlib.sha256(rewritten).hexdigest()
    )


_NOT_ATTACHABLE = (
    "an image id is unknown, was not staged by you on this page, or is already attached"
)


def check_ids(image_ids: list[str] | None) -> list[str]:
    """The list of ids a comment names, shaped before any database is touched: at most
    four, each well formed, none twice."""
    wanted = list(image_ids or [])
    if len(wanted) > PER_COMMENT:
        raise ImageRefused(
            f"a comment takes at most {PER_COMMENT} pictures", field="images"
        )
    if len(set(wanted)) != len(wanted) or not all(
        isinstance(i, str) and _ID.fullmatch(i) for i in wanted
    ):
        raise ImageRefused(_NOT_ATTACHABLE, field="images")
    return wanted
