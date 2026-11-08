"""Pictures on a comment, slice 3 server half (docs/agent/specs/2026-10-29-pinned-comments.md,
section 6 and 9): what is accepted, what is kept, who may attach and see one.

Every file here is built from LITERAL BYTES, so a test reads as what a real camera, screenshot
tool or hostile client sends. Nothing decodes pixels (neither does the service): a small IDAT
under a huge declared size is the point of the limits.
"""

import asyncio
import struct
import zlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_client import key_file, live  # noqa: F401

from ream import (
    comments,
    content,
    decide,
    image_store,
    images,
    keys,
    store,
)
from ream.app import create_api_app, create_gallery_app

PAGE = "<!doctype html><html><head><title>t</title></head><body><p>x</p></body></html>"

GPS = b"GPSLatitude 51.5014 N GPSLongitude 0.1419 W Make=Canon Model=EOS"

# --- literal files ------------------------------------------------------------------------


def _chunk(kind: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body))
    )


def png(width=2, height=2, *, extra=(), idat=None) -> bytes:
    """A PNG: IHDR, any `extra` (kind, body) chunks before IDAT, IDAT, IEND. The IDAT is real
    zlib data for 2x2 and merely small for a bigger declared size (nothing decodes it)."""
    header = _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    rows = b"\x00" + b"\xff\x00\x00" * 2
    data = idat if idat is not None else zlib.compress(rows * 2)
    return (
        b"\x89PNG\r\n\x1a\n"
        + header
        + b"".join(_chunk(k, b) for k, b in extra)
        + _chunk(b"IDAT", data)
        + _chunk(b"IEND", b"")
    )


def jpeg(width=3, height=2, *, extra=b"", trailer=b"") -> bytes:
    """A JPEG with a JFIF header, `extra` segments after it, a baseline frame, one scan whose
    entropy data holds a stuffed 0xFF00 and a restart marker, and EOI, then `trailer`."""
    jfif = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    dqt = b"\xff\xdb\x00\x43\x00" + bytes(range(1, 65))
    sof = (
        b"\xff\xc0\x00\x0b\x08"
        + struct.pack(">HH", height, width)
        + b"\x01\x01\x11\x00"
    )
    dht = b"\xff\xc4\x00\x14\x00" + b"\x01" + b"\x00" * 15 + b"\x00"
    sos = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
    scan = b"\x12\x34\xff\x00\x56\xff\xd0\x78"
    return (
        b"\xff\xd8"
        + jfif
        + extra
        + dqt
        + sof
        + dht
        + sos
        + scan
        + b"\xff\xd9"
        + trailer
    )


def exif_app1(payload=GPS) -> bytes:
    body = b"Exif\x00\x00II*\x00\x08\x00\x00\x00" + payload
    return b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body


def _riff(chunks: bytes) -> bytes:
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WEBP" + chunks


def _wchunk(fourcc: bytes, body: bytes) -> bytes:
    return fourcc + struct.pack("<I", len(body)) + body + b"\x00" * (len(body) & 1)


def _vp8l(width, height) -> bytes:
    bits = (width - 1) | (height - 1) << 14
    return _wchunk(b"VP8L", b"\x2f" + struct.pack("<I", bits) + b"\x00" * 3)


def webp(width=4, height=3) -> bytes:
    """A simple lossless WebP."""
    return _riff(_vp8l(width, height))


def webp_exif(width=4, height=3, flags=0x08 | 0x04, tail=b"") -> bytes:
    """An extended WebP carrying EXIF (with GPS) and XMP chunks around the picture."""
    vp8x = _wchunk(
        b"VP8X",
        bytes([flags])
        + b"\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little"),
    )
    return _riff(
        vp8x
        + _vp8l(width, height)
        + _wchunk(b"EXIF", b"Exif\x00\x00" + GPS)
        + _wchunk(b"XMP ", b"<x:xmpmeta>" + GPS + b"</x:xmpmeta>")
        + tail
    )


def varied(n: int) -> bytes:
    """A different valid PNG per n, so two are never 'the same bytes'."""
    return png(2 + n, 2)


# --- what is accepted and what is kept ------------------------------------------------------


def test_a_png_keeps_only_the_chunks_that_draw_it():
    source = png(
        extra=[
            (b"sRGB", b"\x00"),
            (b"tEXt", b"Comment\x00" + GPS),
            (b"eXIf", b"MM\x00*" + GPS),
            (b"zTXt", b"k\x00\x00" + zlib.compress(GPS)),
            (b"iTXt", b"k\x00\x00\x00\x00\x00" + GPS),
            (b"tIME", b"\x07\xea\x0a\x1d\x00\x00\x00"),
            (b"tRNS", b"\x00\x00"),
            (b"prVt", b"private " + GPS),
        ]
    )
    got = images.inspect(source)
    assert (got.content_type, got.width, got.height) == ("image/png", 2, 2)
    for gone in (
        b"tEXt",
        b"eXIf",
        b"zTXt",
        b"iTXt",
        b"tIME",
        b"prVt",
        b"GPS",
        b"Canon",
    ):
        assert gone not in got.data
    for kept in (b"IHDR", b"sRGB", b"tRNS", b"IDAT", b"IEND"):
        assert kept in got.data
    assert got.data.startswith(b"\x89PNG\r\n\x1a\n") and len(got.data) < len(source)
    assert got.sha256 == __import__("hashlib").sha256(got.data).hexdigest()
    assert images.inspect(got.data).data == got.data  # rewriting is stable


def test_a_jpeg_loses_exif_gps_xmp_photoshop_and_comments_but_keeps_the_picture():
    xmp = (
        b"\xff\xe1"
        + struct.pack(">H", 2 + 29 + len(GPS))
        + b"http://ns.adobe.com/xap/1.0/\x00"
        + GPS
    )
    app13 = b"\xff\xed\x00\x0bPhotoshop"
    com = b"\xff\xfe" + struct.pack(">H", 2 + len(GPS)) + GPS
    app3 = b"\xff\xe3\x00\x08vendor"
    icc = b"\xff\xe2\x00\x0eICC_PROFILE\x00"
    source = jpeg(
        extra=exif_app1() + xmp + app13 + com + app3 + icc, trailer=b"TRAILING" + GPS
    )
    got = images.inspect(source)
    assert (got.content_type, got.width, got.height) == ("image/jpeg", 3, 2)
    for gone in (
        b"GPS",
        b"Exif",
        b"Canon",
        b"Photoshop",
        b"vendor",
        b"TRAILING",
        b"adobe",
    ):
        assert gone not in got.data
    assert b"JFIF" in got.data and b"ICC_PROFILE" in got.data
    assert got.data.endswith(b"\x12\x34\xff\x00\x56\xff\xd0\x78\xff\xd9")
    assert images.inspect(got.data).data == got.data


def test_a_webp_loses_exif_and_xmp_and_the_extended_flags_say_so():
    got = images.inspect(webp_exif())
    assert (got.content_type, got.width, got.height) == ("image/webp", 4, 3)
    for gone in (b"EXIF", b"XMP ", b"GPS", b"Canon"):
        assert gone not in got.data
    assert (
        got.data[8:12] == b"WEBP" and got.data[12:16] == b"VP8X" and b"VP8L" in got.data
    )
    assert got.data[20] & 0x0C == 0  # no EXIF or XMP flag left
    assert struct.unpack("<I", got.data[4:8])[0] == len(got.data) - 8
    assert images.inspect(got.data).data == got.data


def test_a_plain_lossy_webp_is_read_for_its_size():
    vp8 = _wchunk(
        b"VP8 ",
        b"\x10\x02\x00\x9d\x01\x2a" + struct.pack("<HH", 640, 480) + b"\x00" * 5,
    )
    got = images.inspect(_riff(vp8))
    assert (got.width, got.height) == (640, 480)


def test_a_jpeg_keeps_an_app_segment_only_when_it_carries_its_signature():
    def seg(marker, body):
        return b"\xff" + bytes([marker]) + struct.pack(">H", len(body) + 2) + body

    not_icc = seg(0xE2, b"NOTICC" + GPS[:4])
    not_jfif = seg(0xE0, b"EXTRA" + b"GPSX")
    not_adobe = seg(0xEE, b"Other" + b"GPSX")
    reserved = seg(0x02, b"JPGN")  # a reserved marker (JPGn range)
    reserved_high = seg(0xF4, b"JPGN")
    got = images.inspect(
        jpeg(extra=not_icc + not_jfif + not_adobe + reserved + reserved_high)
    )
    for gone in (b"NOTICC", b"EXTRA", b"Other", b"JPGN", b"GPS"):
        assert gone not in got.data
    assert got.data.count(b"JFIF\x00") == 1  # only the real one
    adobe = seg(0xEE, b"Adobe\x00\x64\x00\x00\x00\x00\x01")
    assert b"Adobe" in images.inspect(jpeg(extra=adobe)).data


def test_a_webp_vp8x_loses_bytes_beyond_its_fixed_ten():
    vp8x = _wchunk(
        b"VP8X",
        b"\x00\x00\x00\x00"
        + (3).to_bytes(3, "little")
        + (2).to_bytes(3, "little")
        + b"HIDDEN",
    )
    got = images.inspect(_riff(vp8x + _vp8l(4, 3)))
    assert b"HIDDEN" not in got.data
    assert struct.unpack("<I", got.data[16:20])[0] == 10
    assert images.inspect(got.data).data == got.data


def _timed(data):
    """CPU seconds, not wall seconds: the bound is there to catch a walk that does too much
    work, and a CI runner that starves this worker of a CPU is not that (3 Nov 2026: 1.15 s on
    the wall for a scan that costs 0.05 s)."""
    import time

    started = time.process_time()
    with pytest.raises(images.ImageRefused) as caught:
        images.inspect(data)
    return caught.value, time.process_time() - started


def test_a_png_of_zero_length_chunks_is_refused_fast_and_by_name():
    head = png()[:33]  # signature + IHDR
    data = head + _chunk(b"tEXt", b"") * 400_000
    assert len(data) < images.MAX_BYTES
    err, took = _timed(data)
    assert "chunks or segments" in err.detail and took < 1


def test_a_jpeg_of_endless_segments_is_refused_fast_and_by_name():
    data = jpeg(extra=b"\xff\xe1\x00\x02" * 400_000)
    err, took = _timed(data)
    assert "chunks or segments" in err.detail and took < 1


def test_a_webp_of_endless_chunks_is_refused_fast_and_by_name():
    data = _riff(_vp8l(4, 3) + _wchunk(b"JUNK", b"") * 400_000)
    err, took = _timed(data)
    assert "chunks or segments" in err.detail and took < 1


def test_a_jpeg_scan_of_stuffed_bytes_is_walked_fast():
    import time

    stuffed = b"\xff\x00" * 2_400_000
    data = jpeg().replace(b"\x12\x34\xff\x00\x56\xff\xd0\x78", stuffed)
    started = time.process_time()  # CPU time, as in _timed above
    got = images.inspect(data)
    assert time.process_time() - started < 1 and got.width == 3
    # and the same through the route, which hands the walk to a thread
    assert images.inspect(got.data).data == got.data


def test_attaching_past_the_pages_forty_is_refused_whole(db, page, monkeypatch):
    monkeypatch.setattr(image_store, "STAGED_PER_AUTHOR", 100)

    def stage(n):
        return image_store.stage(db, page.id, "owner", images.inspect(varied(n)))[
            "image_id"
        ]

    def attach(batch):
        root = comments.add(
            db, artifact_id=page.id, author_kind="agent", author="owner", body="x"
        )
        with db.connect() as conn, conn.transaction():
            image_store.attach(conn, page.id, root.id, "owner", batch)

    for first in range(0, 36, 4):
        attach([stage(n) for n in range(first, first + 4)])
    attach([stage(n) for n in range(36, 39)])  # 39 attached
    pair = [stage(40), stage(41)]
    with pytest.raises(images.ImageRefused):
        attach(pair)
    assert _count(db, "comment_id is not null") == 39
    assert _count(db, "comment_id is null") == 2  # nothing was half attached
    attach(pair[:1])
    assert _count(db, "comment_id is not null") == 40


# --- what is refused, with a name ------------------------------------------------------------


GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
SVG = (
    b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'
)
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"
AVIF = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00mif1avif"
TIFF = b"II*\x00\x08\x00\x00\x00" + b"\x00" * 16
ANIMATED = _riff(
    _wchunk(b"VP8X", b"\x02\x00\x00\x00\x03\x00\x00\x02\x00\x00")
    + _wchunk(b"ANIM", b"\x00" * 6)
    + _wchunk(b"ANMF", b"\x00" * 16)
)
APNG = png(extra=[(b"acTL", b"\x00\x00\x00\x02\x00\x00\x00\x00")])


@pytest.mark.parametrize(
    ("data", "named"),
    [
        (GIF, "GIF is not accepted: send PNG, JPEG or WebP"),
        (SVG, "SVG is not accepted: send PNG, JPEG or WebP"),
        (HEIC, "HEIC is not accepted: send PNG, JPEG or WebP"),
        (AVIF, "AVIF is not accepted: send PNG, JPEG or WebP"),
        (TIFF, "TIFF is not accepted: send PNG, JPEG or WebP"),
        (ANIMATED, "animated WebP is not accepted: send PNG, JPEG or WebP"),
        (APNG, "APNG (an animated PNG) is not accepted: send PNG, JPEG or WebP"),
        (
            b"just some text",
            "this is not a PNG, JPEG or WebP image: send PNG, JPEG or WebP",
        ),
        (b"", "the file is empty"),
    ],
)
def test_the_refused_formats_are_named(data, named):
    with pytest.raises(images.ImageRefused) as caught:
        images.inspect(data)
    assert caught.value.detail == named and caught.value.status == 422


GOOD_PNG, GOOD_JPEG, GOOD_WEBP = png(), jpeg(), webp()


@pytest.mark.parametrize(
    "data",
    [
        GOOD_PNG[:8],  # a signature and nothing else
        GOOD_PNG[:40],  # cut in IHDR's neighbourhood
        GOOD_PNG[: len(GOOD_PNG) - 12],  # no IEND
        GOOD_PNG[: len(GOOD_PNG) - 20],  # cut inside IDAT
        GOOD_PNG[:20]
        + bytes([GOOD_PNG[20] ^ 0xFF])
        + GOOD_PNG[21:],  # a flipped byte: the CRC fails
        GOOD_JPEG[:2] + b"\xff",  # SOI and a stray 0xFF
        GOOD_JPEG[: len(GOOD_JPEG) - 2],  # no EOI
        GOOD_JPEG[:30],  # cut inside a segment
        GOOD_JPEG[: GOOD_JPEG.index(b"\xff\xda")],  # cut before the scan
        b"\xff\xd8\xff\xd9",  # no frame, no scan
        GOOD_WEBP[:20],
        GOOD_WEBP[: len(GOOD_WEBP) - 4],  # RIFF says more than there is
        _riff(_wchunk(b"EXIF", b"x")),  # a RIFF with no picture
    ],
)
def test_a_file_that_cannot_be_walked_to_its_end_is_refused(data):
    with pytest.raises(images.ImageRefused) as caught:
        images.inspect(data)
    assert caught.value.status == 422
    assert "cut off or damaged" in caught.value.detail


# --- the limits, at the edge and one past ------------------------------------------------------


def test_four_thousand_pixels_a_side_passes_and_one_more_does_not():
    assert images.inspect(png(4000, 4000)).width == 4000
    assert images.inspect(jpeg(4000, 4000)).height == 4000
    assert images.inspect(webp(4000, 4000)).width == 4000
    for wide in (png(4001, 10), jpeg(4001, 10), webp(4001, 10)):
        with pytest.raises(
            images.ImageRefused, match="4001 x 10 px: at most 4000 px a side"
        ):
            images.inspect(wide)
    for tall in (png(10, 4001), jpeg(10, 4001), webp(10, 4001)):
        with pytest.raises(images.ImageRefused, match="at most 4000 px a side"):
            images.inspect(tall)


def test_the_megapixel_cap_is_its_own_check(monkeypatch):
    monkeypatch.setattr(images, "MAX_PIXELS", 15_000_000)
    with pytest.raises(images.ImageRefused, match="16.0 megapixels: at most 15"):
        images.inspect(png(4000, 4000))
    assert images.inspect(png(3000, 4000)).width == 3000


def test_a_declared_size_of_zero_is_refused():
    with pytest.raises(images.ImageRefused, match="no size"):
        images.inspect(png(0, 5))


def _padded_png(total: int) -> bytes:
    """A valid PNG of exactly `total` bytes, the difference carried by a text chunk."""
    base = len(png(extra=[(b"tEXt", b"")]))
    return png(extra=[(b"tEXt", b"x" * (total - base))])


def test_five_megabytes_passes_and_one_byte_more_is_a_413():
    at_limit = _padded_png(images.MAX_BYTES)
    assert len(at_limit) == 5 * 1024 * 1024
    assert len(images.inspect(at_limit).data) < 1000  # the text is stripped
    with pytest.raises(images.ImageRefused) as caught:
        images.inspect(_padded_png(images.MAX_BYTES + 1))
    assert caught.value.status == 413 and "over 5 MB" in caught.value.detail


# --- the multipart envelope ------------------------------------------------------------------


def _multipart(
    data: bytes, name="file", boundary="B0UNDARY", ctype="text/plain"
) -> tuple[bytes, str]:
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="evil.svg"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


def test_the_file_is_lifted_out_of_the_envelope_byte_for_byte():
    data = b"\x00\r\n--B0UNDAR\r\n\xff" + GOOD_PNG
    body, header = _multipart(data)
    assert images.file_from_multipart(header, body) == data


@pytest.mark.parametrize(
    ("body", "header"),
    [
        (b"", "multipart/form-data; boundary=B"),
        (b"--B\r\n\r\nx", "multipart/form-data; boundary=B"),  # never closed
        (b"raw png bytes", "application/octet-stream"),
        (b"x", None),
        (b"--B--\r\n", "multipart/form-data; boundary=B"),  # no part at all
        (_multipart(b"x", name="avatar")[0], "multipart/form-data; boundary=B0UNDARY"),
        (
            _multipart(b"x")[0].replace(b"--B0UNDARY--\r\n", b"") + _multipart(b"y")[0],
            "multipart/form-data; boundary=B0UNDARY",
        ),  # two parts
    ],
)
def test_anything_but_one_file_in_a_multipart_body_is_refused(body, header):
    with pytest.raises(images.ImageRefused, match="exactly one file"):
        images.file_from_multipart(header, body)


# --- the service ----------------------------------------------------------------------------


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db), base_url="https://testserver")


@pytest.fixture()
def seats(db):
    owner, _ = keys.mint(db, agent="owner")
    other, _ = keys.mint(db, agent="other")
    return {
        "owner": {"Authorization": f"Bearer {owner}"},
        "other": {"Authorization": f"Bearer {other}"},
    }


@pytest.fixture()
def page(db):
    return store.publish(db, "owner", "Boot design", PAGE)


def _upload(client, seats, page_id, data, who="owner", name="file", ctype="image/png"):
    return client.post(
        f"/api/v1/artifacts/{page_id}/comment-images",
        files={name: ("anything.svg", data, ctype)},
        headers=seats[who],
    )


def _comment(client, seats, page_id, who="owner", **body):
    return client.post(
        f"/api/v1/artifacts/{page_id}/comments",
        json={"body": "look at this", **body},
        headers=seats[who],
    )


def _staged(client, seats, page_id, data=GOOD_PNG, who="owner") -> str:
    got = _upload(client, seats, page_id, data, who)
    assert got.status_code == 201, got.text
    return got.json()["image_id"]


def _count(db, where="1 = 1", params=()):
    with db.connect() as conn:
        return conn.execute(
            f"select count(*) from comment_image where {where}",  # noqa: S608 -- literal clauses from this file
            params,
        ).fetchone()[0]


def test_upload_attach_list_and_download_round_trip(client, db, seats, page):
    source = jpeg(extra=exif_app1())
    got = _upload(
        client, seats, page.id, source, ctype="text/plain"
    )  # a lie: it is a JPEG
    assert got.status_code == 201
    staged = got.json()
    assert staged["content_type"] == "image/jpeg"
    assert (staged["width"], staged["height"]) == (3, 2)
    assert set(staged) == {"image_id", "content_type", "width", "height", "byte_size"}
    posted = _comment(client, seats, page.id, images=[staged["image_id"]])
    assert posted.status_code == 201
    [pic] = posted.json()["images"]
    comment_id = posted.json()["id"]
    assert pic["id"] == staged["image_id"] and pic["byte_size"] == staged["byte_size"]
    assert pic["url"] == f"/api/v1/comments/{comment_id}/images/{pic['id']}"
    assert "token" not in pic["url"] and "?" not in pic["url"]
    listed = client.get(f"/api/v1/artifacts/{page.id}/comments", headers=seats["owner"])
    assert listed.json()[0]["images"] == [pic]
    everywhere = client.get("/api/v1/comments", headers=seats["owner"]).json()
    assert everywhere[0]["images"] == [pic]
    served = client.get(pic["url"], headers=seats["owner"])
    assert served.status_code == 200
    assert GPS not in served.content and b"Exif" not in served.content
    assert served.content == images.inspect(source).data
    assert len(served.content) == pic["byte_size"]
    assert __import__("hashlib").sha256(served.content).hexdigest() == pic["sha256"]
    head = served.headers
    assert head["content-type"] == "image/jpeg"
    assert head["x-content-type-options"] == "nosniff"
    assert head["content-security-policy"] == "default-src 'none'; sandbox"
    assert head["content-disposition"] == "attachment"
    assert "set-cookie" not in head


def test_the_type_comes_from_the_bytes_not_from_the_client(client, seats, page):
    lying = _upload(client, seats, page.id, GOOD_PNG, name="image", ctype="image/jpeg")
    assert lying.status_code == 201 and lying.json()["content_type"] == "image/png"
    refused = _upload(client, seats, page.id, GIF, ctype="image/png")
    assert refused.status_code == 422
    assert refused.json()["detail"] == {
        "field": "image",
        "detail": "GIF is not accepted: send PNG, JPEG or WebP",
    }
    assert "onload" not in _upload(client, seats, page.id, SVG).text


def test_a_refusal_never_echoes_what_was_sent(client, seats, page):
    secret = b"sk-live-0123456789abcdefghijklmnopqrstuv"
    got = _upload(client, seats, page.id, GOOD_PNG[:-12] + secret)
    assert got.status_code == 422 and secret not in got.content


def test_the_same_bytes_from_the_same_author_are_the_same_picture(
    client, db, seats, page
):
    first = _staged(client, seats, page.id)
    assert _staged(client, seats, page.id) == first
    assert _count(db) == 1
    comment = _comment(client, seats, page.id, images=[first])
    assert comment.status_code == 201
    assert (
        _staged(client, seats, page.id) != first
    )  # attached: a new upload is a new picture


def test_bytes_that_differ_only_in_stripped_metadata_are_one_picture(
    client, seats, page
):
    plain = _staged(client, seats, page.id, jpeg())
    tagged = _staged(client, seats, page.id, jpeg(extra=exif_app1()))
    assert plain == tagged


def test_another_agent_gets_its_own_id_for_the_same_bytes(client, db, seats, page):
    from ream import store as s

    s.set_agents_can_read(db, page.id, True, agent=None)
    assert _staged(client, seats, page.id) != _staged(
        client, seats, page.id, who="other"
    )


# --- attaching: all or nothing, and no clue which id was wrong --------------------------------


def test_attaching_a_bad_id_is_one_answer_whatever_was_wrong_with_it(
    client, db, seats, page
):
    store.set_agents_can_read(db, page.id, True, agent=None)
    elsewhere = store.publish(db, "owner", "Other page", PAGE)
    mine = _staged(client, seats, page.id)
    theirs = _staged(client, seats, page.id, varied(1), who="other")
    other_page = _staged(client, seats, elsewhere.id, varied(2))
    used = _staged(client, seats, page.id, varied(3))
    assert _comment(client, seats, page.id, images=[used]).status_code == 201
    answers = {
        "unknown": "a" * 26,
        "someone else's": theirs,
        "staged on another page": other_page,
        "already attached": used,
    }
    seen = {}
    for why, bad in answers.items():
        got = _comment(client, seats, page.id, images=[mine, bad])
        seen[why] = (got.status_code, got.json())
    assert len(set(map(repr, seen.values()))) == 1, seen
    status, body = next(iter(seen.values()))
    assert status == 422 and body["detail"]["field"] == "images"
    for malformed in ("not-an-id", "A" * 26, "", "x" * 500):
        got = _comment(client, seats, page.id, images=[malformed])
        assert (got.status_code, got.json()) == (status, body)
    # all or nothing: no comment was written, and the good picture is still waiting
    listed = client.get(f"/api/v1/artifacts/{page.id}/comments", headers=seats["owner"])
    assert len(listed.json()) == 1  # only the one that carried `used`
    assert _comment(client, seats, page.id, images=[mine]).status_code == 201


def test_a_comment_takes_four_pictures_in_order_and_not_five_or_a_repeat(
    client, seats, page
):
    ids = [_staged(client, seats, page.id, varied(n)) for n in range(5)]
    five = _comment(client, seats, page.id, images=ids)
    assert five.status_code == 422 and "at most 4" in five.json()["detail"]["detail"]
    twice = _comment(client, seats, page.id, images=[ids[0], ids[0]])
    assert twice.status_code == 422
    four = _comment(client, seats, page.id, images=[ids[3], ids[1], ids[0], ids[2]])
    assert four.status_code == 201
    assert [p["id"] for p in four.json()["images"]] == [ids[3], ids[1], ids[0], ids[2]]
    assert _comment(client, seats, page.id, images=[ids[4]]).status_code == 201


def test_a_reply_may_carry_pictures_and_a_replay_does_not_attach_twice(
    client, seats, page
):
    root = _comment(client, seats, page.id).json()["id"]
    pic = _staged(client, seats, page.id)
    headers = {**seats["owner"], "Idempotency-Key": "k1"}
    body = {"body": "and this", "thread_root": root, "images": [pic]}
    url = f"/api/v1/artifacts/{page.id}/comments"
    first = client.post(url, json=body, headers=headers)
    again = client.post(url, json=body, headers=headers)
    assert first.status_code == again.status_code == 201
    assert first.json() == again.json()
    assert [p["id"] for p in first.json()["images"]] == [pic]


# --- quotas and expiry -----------------------------------------------------------------------


def test_twelve_staged_pictures_an_author_and_the_thirteenth_waits(
    client, db, seats, page
):
    for n in range(12):
        _staged(client, seats, page.id, varied(n))
    got = _upload(client, seats, page.id, varied(12))
    assert (
        got.status_code == 422
        and "12 pictures are waiting" in got.json()["detail"]["detail"]
    )
    again = _upload(
        client, seats, page.id, varied(11)
    )  # one already staged is not a new one
    assert again.status_code == 201
    assert _count(db) == 12


def test_forty_pictures_a_page_and_staged_ones_do_not_squat_on_them(
    client, db, seats, page, monkeypatch
):
    monkeypatch.setattr(image_store, "STAGED_PER_AUTHOR", 100)
    for batch in range(10):
        ids = [_staged(client, seats, page.id, varied(batch * 4 + n)) for n in range(4)]
        assert _comment(client, seats, page.id, images=ids).status_code == 201
    got = _upload(client, seats, page.id, varied(40))
    assert got.status_code == 422 and "40 pictures" in got.json()["detail"]["detail"]


def test_pictures_waiting_to_be_attached_do_not_count_against_the_page(
    client, db, seats, page
):
    store.set_agents_can_read(db, page.id, True, agent=None)
    for n in range(12):
        _staged(client, seats, page.id, varied(n), who="other")
    assert _upload(client, seats, page.id, varied(30)).status_code == 201


def _age(db, hours):
    with db.connect() as conn:
        conn.execute(
            "update comment_image set created_at = %s",
            (datetime.now(UTC) - timedelta(hours=hours),),
        )


def test_a_staged_picture_expires_after_an_hour_and_the_sweep_frees_it(
    client, db, seats, page
):
    old = _staged(client, seats, page.id)
    _age(db, 2)
    expired = _comment(client, seats, page.id, images=[old])
    assert expired.status_code == 422  # the same answer as an unknown id
    fresh = _staged(client, seats, page.id)
    assert fresh != old  # an expired row is not reused
    assert _count(db) == 2
    assert image_store.sweep(db) == 1
    assert _count(db) == 1
    assert _comment(client, seats, page.id, images=[fresh]).status_code == 201
    _age(db, 5)
    assert image_store.sweep(db) == 0  # an attached picture never expires


def test_expired_staged_pictures_do_not_count_against_the_quota(
    client, db, seats, page
):
    for n in range(12):
        _staged(client, seats, page.id, varied(n))
    _age(db, 2)
    assert _upload(client, seats, page.id, varied(20)).status_code == 201


# --- the body is never read past the cap --------------------------------------------------------


def _raw_post(app, path, token, headers, receive):
    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "https",
        "server": ("testserver", 443),
        "client": ("192.0.2.9", 4000),
        "headers": [
            (b"host", b"testserver"),
            (b"authorization", f"Bearer {token}".encode()),
            *headers,
        ],
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"]


def test_a_body_over_the_cap_is_refused_without_being_held(db, page, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    token, _ = keys.mint(db, agent="owner")
    app = create_gallery_app(db)
    path = f"/api/v1/artifacts/{page.id}/comment-images"
    chunk = b"x" * 65536
    read = []

    async def endless():
        read.append(len(chunk))
        return {"type": "http.request", "body": chunk, "more_body": True}

    status = _raw_post(
        app,
        path,
        token,
        [(b"content-type", b"multipart/form-data; boundary=B")],
        endless,
    )
    assert status == 413
    assert sum(read) <= images.MAX_BYTES + images.BODY_SLACK + 2 * len(chunk)
    assert sum(read) > images.MAX_BYTES  # it did read up to the cap, and stopped there


def test_a_declared_length_over_the_cap_is_refused_before_one_byte_is_read(
    db, page, monkeypatch
):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    token, _ = keys.mint(db, agent="owner")

    async def never():
        raise AssertionError("the body was read")

    declared = images.MAX_BYTES + images.BODY_SLACK + 1
    status = _raw_post(
        create_gallery_app(db),
        f"/api/v1/artifacts/{page.id}/comment-images",
        token,
        [
            (b"content-type", b"multipart/form-data; boundary=B"),
            (b"content-length", str(declared).encode()),
        ],
        never,
    )
    assert status == 413


def test_a_whole_envelope_around_a_file_at_the_limit_is_taken(client, seats, page):
    got = _upload(client, seats, page.id, _padded_png(images.MAX_BYTES))
    assert got.status_code == 201
    over = _upload(client, seats, page.id, _padded_png(images.MAX_BYTES + 1))
    assert over.status_code == 413
    assert over.json()["detail"]["field"] == "image"


# --- who may see what --------------------------------------------------------------------------


def _same(a, b):
    return (a.status_code, a.content) == (b.status_code, b.content)


def test_uploading_to_a_page_you_cannot_read_is_the_404_of_a_page_that_is_not_there(
    client, db, seats, page
):
    store.set_agents_can_read(db, page.id, False, agent=None)
    shut = _upload(client, seats, page.id, GOOD_PNG, who="other")
    missing = _upload(client, seats, "z" * 26, GOOD_PNG, who="other")
    assert shut.status_code == 404 and _same(shut, missing)
    store.set_agents_can_read(db, page.id, True, agent=None)
    assert _upload(client, seats, page.id, GOOD_PNG, who="other").status_code == 201


def test_a_picture_on_a_closed_page_and_an_unknown_one_are_the_same_404(
    client, db, seats, page
):
    store.set_agents_can_read(db, page.id, True, agent=None)
    pic = _staged(client, seats, page.id)
    posted = _comment(client, seats, page.id, who="owner", images=[pic]).json()
    url = f"/api/v1/comments/{posted['id']}/images/{pic}"
    assert client.get(url, headers=seats["other"]).status_code == 200  # open: readable
    store.set_agents_can_read(db, page.id, False, agent=None)
    closed = client.get(url, headers=seats["other"])
    unknown = client.get(
        f"/api/v1/comments/{posted['id']}/images/{'a' * 26}", headers=seats["other"]
    )
    no_comment = client.get(
        f"/api/v1/comments/{'a' * 26}/images/{pic}", headers=seats["other"]
    )
    malformed = client.get("/api/v1/comments/x/images/y", headers=seats["other"])
    assert closed.status_code == 404
    assert (
        _same(closed, unknown)
        and _same(closed, no_comment)
        and _same(closed, malformed)
    )
    assert (
        client.get(url, headers=seats["owner"]).status_code == 200
    )  # the owner still does
    assert client.get(url).status_code in (401, 403)  # no key, no picture


def test_a_picture_is_fetched_only_through_its_own_comment(client, seats, page):
    first = _staged(client, seats, page.id)
    second = _staged(client, seats, page.id, varied(1))
    a = _comment(client, seats, page.id, images=[first]).json()["id"]
    b = _comment(client, seats, page.id, images=[second]).json()["id"]
    assert (
        client.get(
            f"/api/v1/comments/{a}/images/{first}", headers=seats["owner"]
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/v1/comments/{a}/images/{second}", headers=seats["owner"]
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/comments/{b}/images/{first}", headers=seats["owner"]
        ).status_code
        == 404
    )


def test_a_staged_picture_is_not_served_to_anyone(client, db, seats, page):
    pic = _staged(client, seats, page.id)
    for comment_id in ("a" * 26, "b" * 26):
        assert (
            client.get(
                f"/api/v1/comments/{comment_id}/images/{pic}", headers=seats["owner"]
            ).status_code
            == 404
        )


def test_a_held_or_withdrawn_comments_picture_is_not_served(client, db, seats, page):
    held_pic = _staged(client, seats, page.id)
    gone_pic = _staged(client, seats, page.id, varied(1))
    kept_pic = _staged(client, seats, page.id, varied(2))
    held = _comment(client, seats, page.id, images=[held_pic]).json()["id"]
    gone = _comment(client, seats, page.id, images=[gone_pic]).json()["id"]
    kept = _comment(client, seats, page.id, images=[kept_pic]).json()["id"]
    with db.connect() as conn:
        conn.execute("update comment set held = %s where id = %s", (True, held))
    assert comments.withdraw(db, gone, kind="ggshield_test")
    ok = client.get(
        f"/api/v1/comments/{kept}/images/{kept_pic}", headers=seats["owner"]
    )
    assert ok.status_code == 200
    for comment_id, pic in ((held, held_pic), (gone, gone_pic)):
        assert (
            client.get(
                f"/api/v1/comments/{comment_id}/images/{pic}", headers=seats["owner"]
            ).status_code
            == 404
        )


def test_deleting_a_page_deletes_its_pictures(client, db, seats, page):
    pic = _staged(client, seats, page.id)
    _comment(client, seats, page.id, images=[pic])
    staged = _staged(client, seats, page.id, varied(1))
    assert _count(db) == 2 and staged
    assert store.destroy(db, page.id, agent="owner")
    assert _count(db) == 0


def test_deleting_a_thread_deletes_its_pictures(client, db, seats, page):
    pic = _staged(client, seats, page.id)
    root = _comment(client, seats, page.id, images=[pic]).json()["id"]
    with db.connect() as conn:
        conn.execute("delete from comment where id = %s", (root,))
    assert _count(db) == 0


# --- the content origin: the only place a reader's browser gets one ------------------------------


@pytest.fixture()
def origin(db, page, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(
        content.create_content_app(db), base_url=f"https://{page.id}.tenfourty.com"
    )


def _attached(client, seats, page_id, data=GOOD_PNG):
    pic = _staged(client, seats, page_id, data)
    comment = _comment(client, seats, page_id, images=[pic]).json()["id"]
    return pic, comment


def test_the_content_origin_serves_a_picture_to_a_valid_token_with_the_sandbox_headers(
    client, db, seats, page, origin
):
    pic, _ = _attached(client, seats, page.id)
    token = image_store.mint_token(db, pic)
    got = origin.get(f"/ci/{pic}", params={"t": token})
    assert got.status_code == 200
    assert got.content == images.inspect(GOOD_PNG).data
    head = got.headers
    assert head["content-type"] == "image/png"
    assert head["x-content-type-options"] == "nosniff"
    assert head["content-security-policy"] == "default-src 'none'; sandbox"
    assert head["cache-control"] == "private, max-age=3600"
    assert head["content-disposition"] == "inline"
    assert head["cross-origin-resource-policy"] == "cross-origin"
    assert "set-cookie" not in head
    assert image_store.origin_url(page.id, pic, token) == (
        f"https://{page.id}.tenfourty.com/ci/{pic}?t={token}"
    )


def test_the_content_origin_serves_nothing_without_a_valid_unexpired_token(
    client, db, seats, page, origin
):
    pic, _ = _attached(client, seats, page.id)
    other, _ = _attached(client, seats, page.id, varied(1))
    good = image_store.mint_token(db, pic)
    stamp, _, mac = good.partition(".")
    now = datetime.now(UTC).timestamp()
    answers = {
        "none": origin.get(f"/ci/{pic}"),
        "empty": origin.get(f"/ci/{pic}", params={"t": ""}),
        "garbage": origin.get(f"/ci/{pic}", params={"t": "nonsense"}),
        "forged mac": origin.get(
            f"/ci/{pic}", params={"t": f"{stamp}.{'A' * len(mac)}"}
        ),
        "extended expiry": origin.get(
            f"/ci/{pic}", params={"t": f"{int(stamp) + 99999}.{mac}"}
        ),
        "another picture's token": origin.get(
            f"/ci/{pic}", params={"t": image_store.mint_token(db, other)}
        ),
        "expired": origin.get(
            f"/ci/{pic}", params={"t": image_store.mint_token(db, pic, now=now - 7200)}
        ),
        "unknown id": origin.get(
            f"/ci/{'a' * 26}", params={"t": image_store.mint_token(db, "a" * 26)}
        ),
        "malformed id": origin.get("/ci/x", params={"t": good}),
        "non-ascii": origin.get(f"/ci/{pic}", params={"t": "\u00b2.\u00e9"}),
        "non-ascii mac": origin.get(
            f"/ci/{pic}", params={"t": f"{stamp}.\u00e9\u00e9"}
        ),
    }
    for why, got in answers.items():
        assert got.status_code == 404, why
        assert got.content == b"not found", why
    assert origin.get(f"/ci/{pic}", params={"t": good}).status_code == 200


def test_a_token_is_good_for_an_hour_and_not_much_more(db):
    now = datetime.now(UTC).timestamp()
    token = image_store.mint_token(db, "a" * 26, now=now)
    assert image_store.token_valid(db, "a" * 26, token, now=now + 3500)
    assert not image_store.token_valid(db, "a" * 26, token, now=now + 3700)
    assert not image_store.token_valid(db, "b" * 26, token, now=now)


def test_the_signing_key_is_made_once_and_kept(db):
    first = image_store.mint_token(db, "a" * 26, now=1000.0)
    assert image_store.mint_token(db, "a" * 26, now=1000.0) == first
    with db.connect() as conn:
        assert conn.execute("select count(*) from server_secret").fetchone()[0] == 1


def test_a_picture_is_served_only_on_its_own_pages_origin(
    client, db, seats, page, origin, monkeypatch
):
    elsewhere = store.publish(db, "owner", "Other page", PAGE)
    pic, _ = _attached(client, seats, elsewhere.id)
    token = image_store.mint_token(db, pic)
    assert (
        origin.get(f"/ci/{pic}", params={"t": token}).status_code == 404
    )  # wrong page's host
    there = TestClient(
        content.create_content_app(db), base_url=f"https://{elsewhere.id}.tenfourty.com"
    )
    assert there.get(f"/ci/{pic}", params={"t": token}).status_code == 200
    bare = TestClient(content.create_content_app(db), base_url="https://tenfourty.com")
    assert bare.get(f"/ci/{pic}", params={"t": token}).status_code == 404


def test_the_content_origin_does_not_serve_a_withdrawn_or_a_deleted_pages_picture(
    client, db, seats, page, origin
):
    pic, comment = _attached(client, seats, page.id)
    token = image_store.mint_token(db, pic)
    staged = _staged(client, seats, page.id, varied(1))
    assert (
        origin.get(
            f"/ci/{staged}", params={"t": image_store.mint_token(db, staged)}
        ).status_code
        == 404
    )
    assert origin.get(f"/ci/{pic}", params={"t": token}).status_code == 200
    assert store.set_deleted(db, page.id, True, agent="owner")
    assert origin.get(f"/ci/{pic}", params={"t": token}).status_code == 404
    assert store.set_deleted(db, page.id, False, agent="owner")
    assert origin.get(f"/ci/{pic}", params={"t": token}).status_code == 200
    assert comments.withdraw(db, comment, kind="ggshield_test")
    assert origin.get(f"/ci/{pic}", params={"t": token}).status_code == 404


def test_the_gallery_and_api_origins_serve_no_picture_bytes_at_all(
    client, db, seats, page, monkeypatch
):
    pic, _ = _attached(client, seats, page.id)
    token = image_store.mint_token(db, pic)
    for path in (f"/ci/{pic}", f"/c/{page.id}/image/{pic}", f"/a/{page.id}/ci/{pic}"):
        got = client.get(path, params={"t": token})
        assert (
            got.status_code in (404, 405)
            and got.content != images.inspect(GOOD_PNG).data
        )
    api = TestClient(create_api_app(db), base_url="https://api.example")
    assert api.get(f"/ci/{pic}", params={"t": token}).status_code == 404


# --- the CLI ------------------------------------------------------------------------------------


def _run(args, path):
    return decide.main(["--key-file", str(path), "--session", "t/1", *args])


@pytest.fixture()
def files(tmp_path):
    def write(name, data):
        target = tmp_path / name
        target.write_bytes(data)
        return str(target)

    return write


def test_comment_image_uploads_attaches_and_the_service_keeps_no_metadata(
    db, live, key_file, files, capsys
):
    path, _ = key_file
    theirs = store.publish(db, "cove", "Costs", PAGE)
    shot = files("shot.png", png(extra=[(b"tEXt", b"x" + GPS)]))
    photo = files("p.jpg", jpeg(extra=exif_app1()))
    assert (
        _run(
            ["comment", theirs.id, "two pictures", "--image", shot, "--image", photo],
            path,
        )
        == 0
    )
    [row] = comments.list_for_artifact(db, theirs.id)
    assert [i["content_type"] for i in row.images] == ["image/png", "image/jpeg"]
    with db.connect() as conn:
        stored = [
            bytes(b[0])
            for b in conn.execute("select bytes from comment_image").fetchall()
        ]
    assert stored and all(GPS not in b for b in stored)


def test_comment_image_limits_are_checked_here_before_anything_is_sent(
    db, live, key_file, files, capsys
):
    path, _ = key_file
    theirs = store.publish(db, "cove", "Costs", PAGE)
    good = files("a.png", GOOD_PNG)
    cases = {
        "gif.png": (files("gif.png", GIF), "GIF is not accepted"),
        "wide.png": (files("wide.png", png(4001, 1)), "at most 4000 px a side"),
        "huge.png": (files("huge.png", _padded_png(images.MAX_BYTES + 1)), "over 5 MB"),
        "cut.png": (files("cut.png", GOOD_PNG[:-9]), "cut off or damaged"),
        "missing": (str(path) + ".nope", "cannot read that file"),
    }
    for name, (bad, why) in cases.items():
        assert (
            _run(["comment", theirs.id, "x", "--image", good, "--image", bad], path)
            == 2
        ), name
        err = capsys.readouterr().err
        assert why in err, (name, err)
    five = [files(f"{n}.png", varied(n)) for n in range(5)]
    args = [a for f in five for a in ("--image", f)]
    assert _run(["comment", theirs.id, "x", *args], path) == 2
    assert "at most 4 pictures" in capsys.readouterr().err
    assert _count(db) == 0  # the good first file was never uploaded
    assert comments.list_for_artifact(db, theirs.id) == []


def test_reply_image_attaches_to_the_thread(db, live, key_file, files, capsys):
    path, _ = key_file
    mine = store.publish(db, "ops-seat", "Mine", PAGE)
    root = comments.add(
        db, artifact_id=mine.id, author_kind="human", author="jeremy", body="hm?"
    )
    assert (
        _run(["reply", root.id, "see", "--image", files("a.png", GOOD_PNG)], path) == 0
    )
    rows = comments.list_for_artifact(db, mine.id)
    assert [len(r.images) for r in rows] == [0, 1]


def _pictured_thread(db, files):
    mine = store.publish(db, "ops-seat", "Mine", PAGE)
    root = comments.add(
        db, artifact_id=mine.id, author_kind="human", author="jeremy", body="look"
    )
    pic = image_store.stage(db, mine.id, "jeremy", images.inspect(jpeg(1200, 800)))
    with db.connect() as conn:
        conn.execute(
            "update comment_image set comment_id = %s, position = 1, staged_by = null where id = %s",
            (root.id, pic["image_id"]),
        )
    return mine, root, pic


def test_comments_prints_the_cached_path_and_a_repeat_run_downloads_nothing(
    db, live, key_file, files, capsys, monkeypatch, tmp_path
):
    path, _ = key_file
    cache = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    _, root, _pic = _pictured_thread(db, files)
    assert _run(["comments"], path) == 0
    first = capsys.readouterr().out
    folder = cache / "artifacts" / "images" / root.id
    [saved] = list(folder.iterdir())
    assert saved.suffix == ".jpg" and saved.name.startswith("1-")
    assert f"image: {saved}" in first and "(jpeg 1200x800," in first and " KB)" in first
    assert "token" not in first and "?t=" not in first and "/api/v1" not in first
    assert saved.read_bytes() == images.inspect(jpeg(1200, 800)).data
    assert oct(saved.stat().st_mode & 0o777) == "0o600"
    assert oct(folder.stat().st_mode & 0o777) == "0o700"
    calls = []
    real = decide._download
    monkeypatch.setattr(
        decide, "_download", lambda *a, **k: calls.append(a) or real(*a, **k)
    )
    assert _run(["comments"], path) == 0
    assert f"image: {saved}" in capsys.readouterr().out
    assert calls == []  # nothing fetched the second time
    saved.unlink()
    assert _run(["comments"], path) == 0
    assert len(calls) == 1 and saved.exists()


def test_comments_no_images_prints_sizes_and_downloads_nothing(
    db, live, key_file, files, capsys, monkeypatch, tmp_path
):
    path, _ = key_file
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _pictured_thread(db, files)
    assert _run(["comments", "--no-images"], path) == 0
    out = capsys.readouterr().out
    assert "image: (not downloaded)  (jpeg 1200x800," in out
    assert not (tmp_path / "xdg").exists()


def test_a_download_that_is_not_the_listed_picture_is_kept_nowhere(
    db, live, key_file, files, capsys, monkeypatch, tmp_path
):
    path, _ = key_file
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _pictured_thread(db, files)
    monkeypatch.setattr(decide, "_download", lambda *a, **k: b"not the picture")
    assert _run(["comments"], path) == 0
    assert "(not downloaded)" in capsys.readouterr().out
    assert not list((tmp_path / "xdg").rglob("*.jpg"))


def test_comments_downloads_at_most_twenty_pictures_a_call(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    fetched = []
    monkeypatch.setattr(
        decide, "_download", lambda url, key: fetched.append(url) or b""
    )
    messages = [
        {
            "id": "a" * 24 + "abcdefghijklmnopqrstuvwxyz"[n % 26] + "c",
            "images": [
                {"id": "b" * 26, "sha256": "0" * 64, "content_type": "image/png"}
            ],
        }
        for n in range(1, 30)
    ]
    decide._fetch_images([{"messages": messages}], "key", "https://x/api/v1")
    assert len(fetched) == 20


def test_image_flags_are_on_comment_and_reply_and_comments_has_no_images():
    parser = decide.build_parser()
    got = parser.parse_args(
        ["comment", "P", "hi", "--image", "a.png", "--image", "b.png"]
    )
    assert got.image == ["a.png", "b.png"]
    assert parser.parse_args(["reply", "T", "hi", "--image", "a.png"]).image == [
        "a.png"
    ]
    assert parser.parse_args(["comments", "--no-images"]).no_images is True
    assert parser.parse_args(["comments"]).no_images is False
    helps = parser._subparsers._group_actions[0].choices
    assert "--image" in helps["comment"].format_help()
    assert "--no-images" in helps["comments"].format_help()


def test_a_server_supplied_id_that_is_not_an_id_never_reaches_a_path_or_a_url(
    db, live, key_file, capsys, monkeypatch, tmp_path
):
    cache = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    calls = []
    monkeypatch.setattr(decide, "_download", lambda *a, **k: calls.append(a) or b"")
    sha = "a" * 64
    threads = [
        {
            "messages": [
                {
                    "id": "../../x",
                    "images": [
                        {"id": "a" * 26, "sha256": sha, "content_type": "image/png"}
                    ],
                },
                {
                    "id": "a" * 26,
                    "images": [
                        {"id": "../../y", "sha256": sha, "content_type": "image/png"}
                    ],
                },
            ]
        }
    ]
    decide._fetch_images(threads, "key", "http://base")
    assert calls == [] and not cache.exists()
    assert "local" not in threads[0]["messages"][0]["images"][0]
    assert capsys.readouterr().err.count("unexpected id") == 2


# --- the gallery half: his twin route, his form, his thumbnails ----------------------------

HIS = {"Sec-Fetch-Site": "same-origin"}


def _his_upload(client, page_id, data=GOOD_PNG, headers=HIS, name="file"):
    return client.post(
        f"/c/{page_id}/image",
        files={name: ("anything.svg", data, "image/png")},
        headers=headers,
    )


def _his_staged(client, page_id, data=GOOD_PNG) -> str:
    got = _his_upload(client, page_id, data)
    assert got.status_code == 201, got.text
    return got.json()["image_id"]


def _his_comment(client, page_id, *ids, accept=None, **fields):
    headers = dict(HIS)
    if accept:
        headers["Accept"] = accept
    return client.post(
        f"/c/{page_id}",
        data={"body": "look at this", "image": list(ids), **fields},
        headers=headers,
        follow_redirects=False,
    )


def test_he_stages_a_picture_through_the_gallery_twin(client, db, page):
    got = _his_upload(client, page.id)
    assert got.status_code == 201
    meta = got.json()
    assert set(meta) == {"image_id", "content_type", "width", "height", "byte_size"}
    assert meta["content_type"] == "image/png"
    assert _count(db, "staged_by = %s and comment_id is null", ("jeremy",)) == 1
    again = _his_upload(client, page.id)
    assert again.json()["image_id"] == meta["image_id"]  # same bytes, same id


def test_the_twin_wears_the_gallerys_same_origin_guard(client, page):
    for headers in ({"Sec-Fetch-Site": "cross-site"}, {}):
        got = _his_upload(client, page.id, headers=headers)
        assert got.status_code == 403


def test_the_twin_holds_the_agent_routes_limits_and_error_shapes(
    client, db, seats, page
):
    agent = _upload(client, seats, page.id, b"GIF89a" + b"\0" * 20)
    his = _his_upload(client, page.id, b"GIF89a" + b"\0" * 20)
    assert his.status_code == agent.status_code == 422
    assert his.json() == agent.json()
    assert _his_upload(client, page.id, GOOD_PNG, name="other").status_code == 422
    big = _padded_png(images.MAX_BYTES + 1)
    assert _his_upload(client, page.id, big).status_code == 413
    assert _count(db) == 0


def test_the_twin_streams_and_stops_at_the_cap(db, page, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    app = create_gallery_app(db)
    chunk = b"x" * 65536
    read = []

    async def endless():
        read.append(len(chunk))
        return {"type": "http.request", "body": chunk, "more_body": True}

    scope_headers = [
        (b"content-type", b"multipart/form-data; boundary=B"),
        (b"sec-fetch-site", b"same-origin"),
    ]
    sent = []

    async def send(message):
        sent.append(message)

    async def run():
        scope = {
            "type": "http",
            "method": "POST",
            "path": f"/c/{page.id}/image",
            "raw_path": f"/c/{page.id}/image".encode(),
            "query_string": b"",
            "headers": scope_headers,
            "server": ("testserver", 443),
            "scheme": "https",
        }
        await app(scope, endless, send)

    asyncio.run(run())
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 413
    assert sum(read) <= images.MAX_BYTES + images.BODY_SLACK + 2 * len(chunk)


def test_an_unknown_page_and_a_deleted_one_are_the_same_404(client, db, seats, page):
    gone = store.publish(db, "owner", "Gone", PAGE)
    assert store.destroy(db, gone.id, agent="owner")
    unknown = _his_upload(client, "a" * 26)
    deleted = _his_upload(client, gone.id)
    assert unknown.status_code == deleted.status_code == 404
    assert _same(unknown, deleted)


def test_a_picture_staged_by_an_agent_cannot_be_attached_by_him(
    client, db, seats, page
):
    theirs = _staged(client, seats, page.id)
    unknown = _his_comment(client, page.id, "a" * 26)
    other = _his_comment(client, page.id, theirs)
    assert unknown.status_code == other.status_code == 422
    assert _same(unknown, other)
    assert comments.list_for_artifact(db, page.id) == []  # all or nothing


def test_the_form_attaches_repeated_image_fields_in_order(client, db, page):
    first, second = (
        _his_staged(client, page.id),
        _his_staged(client, page.id, varied(1)),
    )
    got = _his_comment(client, page.id, second, first)
    assert got.status_code == 303
    [row] = comments.list_for_artifact(db, page.id)
    assert [pic["id"] for pic in row.images] == [second, first]


def test_a_refused_id_on_the_form_is_one_error_and_no_comment(client, db, page):
    good = _his_staged(client, page.id)
    got = _his_comment(client, page.id, good, "a" * 26)
    assert got.status_code == 422
    assert comments.list_for_artifact(db, page.id) == []
    assert _count(db, "comment_id is null") == 1  # the good one is still staged
    five = [_his_staged(client, page.id, varied(n)) for n in range(5)]
    assert _his_comment(client, page.id, *five).status_code == 422


def test_a_reply_may_carry_pictures_too(client, db, page):
    root = _his_comment(client, page.id)
    assert root.status_code == 303
    [first] = comments.list_for_artifact(db, page.id)
    pic = _his_staged(client, page.id)
    got = _his_comment(client, page.id, pic, thread_root=first.id)
    assert got.status_code == 303
    rows = comments.list_for_artifact(db, page.id)
    assert [len(r.images) for r in rows] == [0, 1]


def test_the_panel_shows_each_picture_from_the_content_origin_with_a_token(
    client, db, page, origin
):
    pic = _his_staged(client, page.id)
    _his_comment(client, page.id, pic)
    html = client.get(f"/a/{page.id}").text
    start = html.index(f"https://{page.id}.tenfourty.com/ci/{pic}?t=")
    url = html[start : html.index('"', start)].replace("&amp;", "&")
    assert f'<img class="cthumb" src="{url}"' in html
    assert f'class="cimg" href="{url}"' in html
    token = url.split("?t=")[1]
    assert image_store.token_valid(db, pic, token)
    got = origin.get(f"/ci/{pic}", params={"t": token})
    assert got.status_code == 200 and got.content == images.inspect(GOOD_PNG).data
    assert "Images are not checked for secrets." in html
    assert 'type="file"' in html and 'class="cattachbtn"' in html


def test_a_held_comment_of_his_own_shows_its_pictures(client, db, page):
    pic = _his_staged(client, page.id)
    posted = _his_comment(client, page.id, pic)
    assert posted.status_code == 303
    [row] = comments.list_for_artifact(db, page.id)
    with db.connect() as conn:
        conn.execute("update comment set held = %s where id = %s", (True, row.id))
    html = client.get(f"/a/{page.id}").text
    assert f"/ci/{pic}?t=" in html


def test_the_in_place_post_returns_the_thread_with_its_thumbnails(client, page):
    pic = _his_staged(client, page.id)
    got = _his_comment(client, page.id, pic, accept="application/json")
    assert got.status_code == 201
    assert f"https://{page.id}.tenfourty.com/ci/{pic}?t=" in got.json()["html"]


def test_the_gallery_origin_serves_no_picture_bytes(client, db, seats, page):
    pic = _his_staged(client, page.id)
    _his_comment(client, page.id, pic)
    [row] = comments.list_for_artifact(db, page.id)
    token = image_store.mint_token(db, pic)
    for path in (
        f"/ci/{pic}",
        f"/c/{row.id}/images/{pic}",
        f"/c/{page.id}/image/{pic}",
        f"/a/{page.id}/images/{pic}",
    ):
        got = client.get(path, params={"t": token})
        assert got.status_code in (404, 405), path
        assert images.inspect(GOOD_PNG).data not in got.content


def test_the_gallery_csp_allows_the_content_origin_wildcard_for_images_and_nothing_more(
    client, page
):
    csp = client.get(f"/a/{page.id}").headers["content-security-policy"]
    assert "img-src 'self' https://*.tenfourty.com;" in csp
    assert "data:" not in csp and "blob:" not in csp
    assert "script-src 'self';" in csp and "default-src 'none'" in csp
    assert "connect-src 'self';" in csp


def test_a_panel_mints_every_token_in_one_batch(client, db, page, monkeypatch):
    pics = [_his_staged(client, page.id, varied(n)) for n in range(3)]
    _his_comment(client, page.id, *pics)
    batches = []
    real = image_store.mint_tokens

    def counted(database, ids, **kw):
        batches.append(list(ids))
        return real(database, ids, **kw)

    monkeypatch.setattr(image_store, "mint_tokens", counted)
    html = client.get(f"/a/{page.id}").text
    assert batches == [pics]
    assert all(f"/ci/{pic}?t=" in html for pic in pics)
    assert image_store.mint_tokens(db, []) == {}
