"""How a version is stored once it is no longer the newest: zstd, as a full copy or a delta.

docs/agent/specs/2026-10-26-delta-versions-design.md. The codec's name carries the format's
version (`zstd1-...`), so the format can change later without guessing what old rows are.

- **full**: the whole body, compressed on its own. Version 1 and every `n` where
  `(n - 1) % KEYFRAME_EVERY == 0`, so a chain of deltas is never longer than 99.
- **delta**: the body compressed with the previous version's text as a raw-content
  dictionary (what `zstd --patch-from` does). Always against `n - 1`.

Every encode is decoded straight back and compared before it is returned, and every decode is
checked against the plain text's SHA-256: a damaged row raises `PackError`, it never yields
wrong text.

LEVEL 9, not the spec's 19 (measured 26 Oct 2026 on a 3.2 MB page of mixed text): a delta
took 2.1 s at level 19 and 0.12 s at level 9, for 414 and 453 bytes. A full copy took 3.2 s
against 0.25 s, for 807 KB against 859 KB. The size of a delta comes from the match, not the
level, and the encode sits on the publish path.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import zstandard

FULL = "zstd1-full"
DELTA = "zstd1-delta"
KEYFRAME_EVERY = 100
LEVEL = 9
# A body is at most 8 MiB, so a delta's reach back into its base is at most 16 MiB.
_WINDOW_LOG = 24
# A body is at most 8 MiB (store.MAX_BODY_BYTES); nothing larger is ever decoded.
_MAX_OUTPUT = 8 * 1024 * 1024


class PackError(Exception):
    """A stored version could not be rebuilt exactly."""


@dataclass(frozen=True)
class Packed:
    codec: str
    data: bytes
    base_n: int | None


def is_keyframe(n: int) -> bool:
    return (n - 1) % KEYFRAME_EVERY == 0


def keyframe_at_or_below(n: int) -> int:
    return ((n - 1) // KEYFRAME_EVERY) * KEYFRAME_EVERY + 1


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _compressor(base: bytes | None) -> zstandard.ZstdCompressor:
    params = zstandard.ZstdCompressionParameters.from_level(
        LEVEL, window_log=_WINDOW_LOG
    )
    if base is None:
        return zstandard.ZstdCompressor(compression_params=params)
    dictionary = zstandard.ZstdCompressionDict(
        base, dict_type=zstandard.DICT_TYPE_RAWCONTENT
    )
    return zstandard.ZstdCompressor(compression_params=params, dict_data=dictionary)


def pack(n: int, body: str, base: str | None) -> Packed:
    """Version `n`'s `body` encoded: a full copy on a keyframe (or with no base), else
    a delta against `base`, the text of version `n - 1`. Decoded again before it is
    returned."""
    if is_keyframe(n) or base is None:
        codec, base_n, base_bytes = FULL, None, None
    else:
        codec, base_n, base_bytes = DELTA, n - 1, base.encode()
    try:
        data = _compressor(base_bytes).compress(body.encode())
    except zstandard.ZstdError as exc:
        raise PackError(f"version {n} could not be encoded: {exc}") from exc
    if (
        unpack(codec, data, base if base_bytes is not None else None, sha256(body))
        != body
    ):
        raise PackError(f"version {n} did not decode to what was encoded")
    return Packed(codec, data, base_n)


def unpack(
    codec: str, data: bytes, base: str | None, expected_sha256: str | None
) -> str:
    """The plain text `data` encodes. `base` is the previous version's text for a delta.
    Raises `PackError` on an unknown codec, a damaged frame or a hash that does not match."""
    if codec == FULL:
        dictionary = None
    elif codec == DELTA:
        if base is None:
            raise PackError("a delta needs its base version")
        dictionary = zstandard.ZstdCompressionDict(
            base.encode(), dict_type=zstandard.DICT_TYPE_RAWCONTENT
        )
    else:
        raise PackError(f"unknown codec {codec!r}")
    try:
        data = bytes(data)
        # python-zstandard ignores max_output_size when the frame DECLARES its size, so a
        # crafted frame claiming hundreds of MiB would be decoded in full. Read the
        # declared size first and refuse anything over the cap.
        declared = zstandard.get_frame_parameters(data).content_size
        if declared > _MAX_OUTPUT:
            raise PackError("a stored version declares an impossible size")
        raw = zstandard.ZstdDecompressor(
            dict_data=dictionary, max_window_size=1 << _WINDOW_LOG
        ).decompress(data, max_output_size=_MAX_OUTPUT)
        text = raw.decode()
    except (zstandard.ZstdError, UnicodeDecodeError) as exc:
        raise PackError(f"a stored version is damaged: {exc}") from exc
    if expected_sha256 is None or sha256(text) != expected_sha256:
        raise PackError("a stored version does not match its hash")
    return text
