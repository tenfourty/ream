"""A render's answer is checked before it is used (`ream.renderwire`).

The pictures here are built byte by byte with zlib and struct, so each check is
proved against exactly the shape it refuses.
"""

import base64
import json
import logging
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from ream import rendercore, renderwire, thumbs
from ream.renderwire import RenderAnswerInvalid, RenderRequestInvalid

W, H = 900, 640
CHECK = {"text": 12, "media": 0, "height": 640, "scroll": 900, "view": 640}


def chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data))
    )


def png(width=W, height=H, *, pad=0) -> bytes:
    """A real 1-bit greyscale PNG of `width` x `height`, its IDAT padded with
    `pad` extra bytes in an ancillary chunk when a test needs a size."""
    ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00" * ((width + 7) // 8) for _ in range(height))
    body = chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows))
    if pad:
        body += chunk(b"tEXt", b"x" * pad)
    return b"\x89PNG\r\n\x1a\n" + body + chunk(b"IEND", b"")


def answer(**over) -> bytes:
    fields = {
        "v": 1,
        "outcome": "rendered",
        "png_base64": base64.b64encode(png()).decode(),
        "errors": ["line 3: Uncaught ReferenceError: x is not defined"],
        "check": dict(CHECK),
    }
    fields.update(over)
    return json.dumps(fields).encode()


def refused(raw, width=W, height=H):
    with pytest.raises(RenderAnswerInvalid) as caught:
        renderwire.parse_answer(raw, width, height)
    return str(caught.value)


# --- the module stands alone ------------------------------------------------


def test_renderwire_imports_nothing_else_of_ream():
    probe = (
        "import sys, ream.renderwire; "
        "print(','.join(sorted(m for m in sys.modules "
        "if m.startswith(('ream.', 'artifacts')) and m != 'ream.renderwire')))"
    )
    done = subprocess.run(  # noqa: S603 -- fixed argv, our own interpreter
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ""


def test_the_runner_and_the_wire_share_one_report():
    assert rendercore.Report is renderwire.Report is thumbs.Report


# --- validate_png -----------------------------------------------------------


def test_a_real_png_of_the_size_asked_for_passes():
    data = png()
    assert renderwire.validate_png(data, W, H) is data


def test_bad_magic_is_refused():
    with pytest.raises(RenderAnswerInvalid, match="not a PNG"):
        renderwire.validate_png(b"\x89PNG\r\n\x1a\x00" + png()[8:], W, H)


def test_too_short_to_hold_a_header_is_refused():
    with pytest.raises(RenderAnswerInvalid, match="not a PNG"):
        renderwire.validate_png(b"\x89PNG\r\n\x1a\n" + b"x" * 20, W, H)


def test_an_ihdr_of_the_wrong_length_is_refused():
    ihdr = struct.pack(">IIBBBBBB", W, H, 1, 0, 0, 0, 0, 0)  # 14 bytes
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")
    with pytest.raises(RenderAnswerInvalid, match="first chunk"):
        renderwire.validate_png(data, W, H)


def test_a_first_chunk_that_is_not_ihdr_is_refused():
    data = bytearray(png())
    data[12:16] = b"IHDX"
    with pytest.raises(RenderAnswerInvalid, match="first chunk"):
        renderwire.validate_png(bytes(data), W, H)


def test_a_bad_ihdr_crc_is_refused():
    data = bytearray(png())
    data[32] ^= 0xFF
    with pytest.raises(RenderAnswerInvalid, match="checksum"):
        renderwire.validate_png(bytes(data), W, H)


@pytest.mark.parametrize("size", [(W + 1, H), (W, H - 1), (H, W)])
def test_other_dimensions_than_asked_for_are_refused(size):
    with pytest.raises(RenderAnswerInvalid, match="size asked for"):
        renderwire.validate_png(png(*size), W, H)


def test_a_missing_iend_is_refused():
    with pytest.raises(RenderAnswerInvalid, match="end chunk"):
        renderwire.validate_png(png()[:-12], W, H)
    with pytest.raises(RenderAnswerInvalid, match="end chunk"):
        renderwire.validate_png(png() + b"\x00", W, H)


def test_a_png_at_the_size_limit_passes_and_one_byte_over_is_refused():
    base = len(png())
    pad = renderwire.PNG_MAX_BYTES - base - 12  # the tEXt chunk's own 12 bytes
    at = png(pad=pad)
    assert len(at) == renderwire.PNG_MAX_BYTES
    assert renderwire.validate_png(at, W, H) is at
    over = png(pad=pad + 1)
    with pytest.raises(RenderAnswerInvalid, match="size limit"):
        renderwire.validate_png(over, W, H)


def test_a_refusal_never_quotes_the_bytes():
    secret = b"SECRET-MARKER"
    for data in (secret, b"\x89PNG\r\n\x1a\n" + secret * 10):
        with pytest.raises(RenderAnswerInvalid) as caught:
            renderwire.validate_png(data, W, H)
        assert "SECRET" not in str(caught.value)


# --- parse_answer -----------------------------------------------------------


def test_a_good_answer_parses_to_a_report():
    got = renderwire.parse_answer(answer(), W, H)
    assert isinstance(got, renderwire.Report)
    picture, errors = got
    assert picture == png()
    assert errors == ["line 3: Uncaught ReferenceError: x is not defined"]
    assert got.check == CHECK


def test_a_bad_png_inside_a_good_answer_is_refused():
    assert "size asked for" in refused(answer(), W, H + 1)


def test_bad_base64_is_refused():
    good = base64.b64encode(png()).decode()
    for bad in (good + "=", good[:-1], good[:10] + "\n" + good[10:], "@@@@" + good):
        assert "base64" in refused(answer(png_base64=bad))


def test_non_ascii_base64_is_refused():
    assert refused(answer(png_base64="é" + base64.b64encode(png()).decode()))


def test_extra_and_missing_keys_are_refused():
    assert "keys" in refused(answer(extra=1))
    fields = json.loads(answer())
    for key in list(fields):
        less = {k: v for k, v in fields.items() if k != key}
        assert "keys" in refused(json.dumps(less).encode())


def test_a_repeated_key_is_refused():
    raw = answer()[:-1] + b',"v":1}'
    assert "repeats" in refused(raw)


@pytest.mark.parametrize(
    "raw", [b"", b"not json", b"[]", b"null", b'{"v":NaN}', b"\xff\xfe"]
)
def test_what_is_not_an_answer_object_is_refused(raw):
    assert refused(raw)


@pytest.mark.parametrize("v", [2, 0, True, 1.0, "1", None])
def test_only_version_one_is_read(v):
    assert refused(answer(v=v))


@pytest.mark.parametrize("outcome", ["ok", "", None, 1, ["rendered"]])
def test_an_unknown_outcome_is_refused(outcome):
    assert refused(answer(outcome=outcome))


def test_errors_at_the_bounds_pass_and_over_them_are_refused():
    at = ["e" * renderwire.ERROR_MAX_CHARS] * renderwire.ERRORS_MAX_COUNT
    assert renderwire.parse_answer(answer(errors=at), W, H)[1] == at
    assert refused(answer(errors=at + ["e"]))
    assert refused(answer(errors=["e" * (renderwire.ERROR_MAX_CHARS + 1)]))
    assert refused(answer(errors=[1]))
    assert refused(answer(errors="line 1: x"))
    assert refused(answer(errors=None))


def test_check_at_the_bounds_passes_and_over_them_is_refused():
    top = dict.fromkeys(renderwire.CHECK_KEYS, renderwire.CHECK_MAX)
    assert renderwire.parse_answer(answer(check=top), W, H).check == top
    zero = dict.fromkeys(renderwire.CHECK_KEYS, 0)
    assert renderwire.parse_answer(answer(check=zero), W, H).check == zero
    assert renderwire.parse_answer(answer(check=None), W, H).check is None
    for key in renderwire.CHECK_KEYS:
        assert refused(answer(check={**CHECK, key: renderwire.CHECK_MAX + 1}))
        assert refused(answer(check={**CHECK, key: -1}))
        assert refused(answer(check={**CHECK, key: 1.0}))
        assert refused(answer(check={**CHECK, key: "1"}))


def test_a_bool_in_check_is_refused():
    assert "check" in refused(answer(check={**CHECK, "media": True}))
    assert "check" in refused(answer(check={**CHECK, "text": False}))


def test_check_with_extra_or_missing_numbers_is_refused():
    assert refused(answer(check={**CHECK, "more": 1}))
    assert refused(answer(check={k: v for k, v in CHECK.items() if k != "view"}))
    assert refused(answer(check=[1, 2, 3, 4, 5]))


def test_the_response_size_limit_is_at_and_over_the_bound():
    raw = answer()
    at = raw[:-1] + b" " * (renderwire.RESPONSE_MAX_BYTES - len(raw)) + b"}"
    assert len(at) == renderwire.RESPONSE_MAX_BYTES
    assert renderwire.parse_answer(at, W, H)[0] == png()
    over = raw[:-1] + b" " * (renderwire.RESPONSE_MAX_BYTES - len(raw) + 1) + b"}"
    assert "size limit" in refused(over)


def test_base64_over_the_picture_limit_is_refused_before_decoding():
    long = "A" * (renderwire.PNG_BASE64_MAX_CHARS + 4)
    assert "size limit" in refused(answer(png_base64=long))


def test_the_other_outcomes_carry_no_picture():
    report = renderwire.parse_answer(
        answer(outcome="no_picture", png_base64=None), W, H
    )
    assert tuple(report) == (
        None,
        ["line 3: Uncaught ReferenceError: x is not defined"],
    )
    assert report.check == CHECK
    assert refused(answer(outcome="no_picture"))
    none = renderwire.parse_answer(
        answer(outcome="not_run", png_base64=None, errors=[], check=None), W, H
    )
    assert tuple(none) == (None, None) and none.check is None
    assert refused(answer(outcome="not_run", png_base64=None, check=None))
    assert refused(answer(outcome="rendered", png_base64=None))


# --- encode_answer ----------------------------------------------------------


@pytest.mark.parametrize(
    "report",
    [
        renderwire.Report(png(), ["line 1: Uncaught x"], dict(CHECK)),
        renderwire.Report(png(), [], None),
        renderwire.Report(None, ["line 2: Uncaught y"], dict(CHECK)),
        renderwire.Report(None, None, None),
        (png(), []),
    ],
)
def test_encode_then_parse_is_the_same_report(report):
    raw = renderwire.encode_answer(report)
    got = renderwire.parse_answer(raw, W, H)
    assert tuple(got) == tuple(report)
    assert got.check == getattr(report, "check", None)


def test_encode_refuses_what_parse_would_refuse():
    with pytest.raises(RenderAnswerInvalid):
        renderwire.encode_answer(renderwire.Report(None, ["e"] * 11))
    with pytest.raises(RenderAnswerInvalid):
        renderwire.encode_answer(renderwire.Report(None, [], {**CHECK, "view": True}))
    with pytest.raises(RenderAnswerInvalid):
        renderwire.encode_answer((b"x" * (renderwire.PNG_MAX_BYTES + 1), []))


# --- parse_request_query ----------------------------------------------------


def test_a_good_query_parses():
    assert renderwire.parse_request_query("w=900&h=640&check=1") == (900, 640, True)
    assert renderwire.parse_request_query(b"h=640&w=900&check=0") == (900, 640, False)
    assert renderwire.parse_request_query("w=390&h=1600") == (390, 1600, False)


def test_the_size_bounds_are_at_and_over_the_bound():
    lo, hi = renderwire.WIDTH_MIN, renderwire.WIDTH_MAX
    hlo, hhi = renderwire.HEIGHT_MIN, renderwire.HEIGHT_MAX
    assert renderwire.parse_request_query(f"w={lo}&h={hlo}")[:2] == (lo, hlo)
    assert renderwire.parse_request_query(f"w={hi}&h={hhi}")[:2] == (hi, hhi)
    for query in (
        f"w={lo - 1}&h={hlo}",
        f"w={hi + 1}&h={hlo}",
        f"w={lo}&h={hlo - 1}",
        f"w={lo}&h={hhi + 1}",
    ):
        with pytest.raises(RenderRequestInvalid, match="out of bounds"):
            renderwire.parse_request_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "w=900&h=640&full=1",
        "w=900&h=640&selector=x",
        "w=900&h=640&W=1",
        "w=900&h=640&w=900",
        "w=900",
        "h=640",
        "",
        "w=900&h=640&check=2",
        "w=900&h=640&check=true",
        "w=900&h",
        "w=-900&h=640",
        "w=+900&h=640",
        "w=9e2&h=640",
        "w=900.0&h=640",
        "w=%39%30%30&h=640",
        "w=９００&h=640",
        "w=900&h=640&" + "x" * 300,
        b"w=900&h=\xff",
    ],
)
def test_a_bad_query_is_refused(query):
    with pytest.raises(RenderRequestInvalid):
        renderwire.parse_request_query(query)


# --- the in-process render takes the same path ------------------------------


def _shoot(monkeypatch, data: bytes):
    """Run the in-process screenshot with a `_run` that writes `data` as the
    shot and a check line, as a browser that ran would."""

    def fake_run(argv, **kw):
        shot = next(a for a in argv if str(a).startswith("--screenshot="))
        Path(shot.split("=", 1)[1]).write_bytes(data)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(rendercore, "_run", fake_run)
    return rendercore.screenshot("/bin/true", "<p>x</p>", W, H)


def test_an_in_process_render_with_a_valid_png_is_kept(monkeypatch):
    picture, errors = _shoot(monkeypatch, png())
    assert picture == png()
    assert errors == []


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda d: d[:-12],  # no IEND
        lambda d: d[:32] + bytes([d[32] ^ 1]) + d[33:],  # bad IHDR CRC
        lambda d: b"\x00" + d[1:],  # bad magic
    ],
)
def test_an_in_process_render_with_a_corrupt_png_is_unchecked(
    monkeypatch, caplog, corrupt
):
    with caplog.at_level(logging.WARNING, logger="ream.thumbs"):
        got = _shoot(monkeypatch, corrupt(png()))
    assert tuple(got) == (None, None)  # as a render that did not run
    assert getattr(got, "check", None) is None
    warned = [r for r in caplog.records if "no usable picture" in r.getMessage()]
    assert len(warned) == 1


def test_an_in_process_render_of_another_size_is_unchecked(monkeypatch):
    assert tuple(_shoot(monkeypatch, png(W, H + 1))) == (None, None)
