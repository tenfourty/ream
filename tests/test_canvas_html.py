"""HTML-in-Canvas draws in Ream's pictures: a page that puts live HTML in a
`<canvas layoutsubtree>` and draws it with `drawElementImage` on the canvas's `paint`
event shows that drawing in a screenshot (`rendercore.shoot`) and in a render
(`thumbs.render_report`: the wall's tiles and preview). Chromium 147-156 has the
feature behind a flag, which `rendercore._isolation_flags` turns on; CI's browser
step runs this with REAM_REQUIRE_BROWSER=1, so a browser that cannot draw fails
here and names its version, rather than a blank canvas passing as a picture.
"""

import base64
import struct
import subprocess

from test_shots import needs_browser, pixels

from ream import rendercore, renderwire, thumbs

# A 200 by 100 canvas holding a solid green div (and some text at the top left). On
# paint the div is drawn into the canvas, then a half-opaque red rectangle over its
# right half: green stays (0, 255, 0) on the left, and the right is the blend of
# the two, about (128, 128, 0). A canvas that did not draw is the white page.
PAGE = """<!doctype html>
<body style="margin:0;background:#fff">
<canvas id="c" width="200" height="100" layoutsubtree content="drawable"
  style="display:block;width:200px;height:100px">
  <div id="d" style="width:200px;height:100px;background:#00ff00;font:12px sans-serif">hello</div>
</canvas>
<script>
const c = document.getElementById("c"), d = document.getElementById("d");
const ctx = c.getContext("2d");
c.addEventListener("paint", () => {
  ctx.reset();
  ctx.drawElementImage(d, 0, 0);
  ctx.fillStyle = "rgba(255,0,0,0.5)";
  ctx.fillRect(100, 0, 100, 100);
});
if (c.requestPaint) c.requestPaint();
</script>
</body>"""


def _version() -> str:
    try:
        out = subprocess.run(  # noqa: S603
            [rendercore.browser_path(), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        return out.stdout.strip() or out.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unknown ({exc})"


def _check(png: bytes | None, path: str) -> None:
    named = f"{path}: no HTML-in-Canvas drawing in the picture; `chromium --version` says {_version()!r}"
    assert png is not None, f"{path}: no picture"
    rows = pixels(png)[2]
    left, right = rows[90][50], rows[90][150]
    assert left == (0, 255, 0), f"{named}; left pixel {left}, wanted (0, 255, 0)"
    r, g, b = right
    assert 120 <= r <= 136 and 120 <= g <= 136 and b <= 8, (
        f"{named}; right pixel {right}, wanted the red-over-green blend ~(128, 128, 0)"
    )


@needs_browser
def test_a_screenshot_shows_what_the_page_drew_in_a_canvas():
    shot = rendercore.shoot(rendercore.browser_path(), PAGE, renderwire.shot_request())
    assert shot.outcome == "shot", shot
    _check(shot.png, "shot")


@needs_browser
def test_a_render_shows_what_the_page_drew_in_a_canvas():
    _check(thumbs.render_report(PAGE, 1280, 720)[0], "render")


# ---------------------------------------------------------------------------
# A font a page embeds as a data: URL. The browser runs with --disable-remote-fonts;
# CI's browser step showed that it also stops a data: source, so these tests pin the fallback.
# The font is built here, byte by byte (no font library is a dependency): a TrueType
# font whose only drawn glyph, for A-Z, is a solid square 0.8 em on a side. The
# fallback `monospace` letter H is nothing like a solid square.
# ---------------------------------------------------------------------------


def _be(fmt: str, *values: int) -> bytes:
    return struct.pack(">" + fmt, *values)


def _square_font() -> bytes:
    glyph = (
        _be("hhhhh", 1, 0, 0, 800, 800)  # one contour, bounding box
        + _be("h", 3)  # last point of the contour
        + _be("h", 0)  # no instructions
        + bytes([1, 1, 1, 1])  # on-curve points, 16-bit deltas
        + _be(
            "hhhh", 0, 0, 800, 0
        )  # x deltas: (0,0) (0,800) (800,800) (800,0), clockwise
        + _be("hhhh", 0, 800, 0, -800)  # y deltas
    )
    glyph += b"\0" * (-len(glyph) % 4)
    cmap = (
        _be("HH", 0, 1)
        + _be("HHL", 3, 1, 12)
        + _be(
            "HHHHHHH", 4, 32, 0, 4, 4, 1, 0
        )  # format 4: length, language, segCountX2, search values
        + _be("HH", 0x5A, 0xFFFF)  # endCode
        + _be("H", 0)
        + _be("HH", 0x41, 0xFFFF)  # startCode
        + _be("HH", (1 - 0x41) & 0xFFFF, 1)  # idDelta: A-Z -> glyph 1
        + _be("HH", 0, 0)  # idRangeOffset
    )
    head = (
        _be("LLLL", 0x00010000, 0x00010000, 0, 0x5F0F3CF5)
        + _be("HH", 0, 1000)
        + b"\0" * 16
        + _be("hhhhHHhhh", 0, 0, 800, 800, 0, 8, 2, 1, 0)
    )
    hhea = _be(
        "L" + "h" * 16,
        0x00010000,
        800,
        -200,
        0,
        1000,
        0,
        0,
        800,
        1,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        2,
    )
    os2 = (
        _be("HhHHH", 3, 600, 400, 5, 0)
        + _be("hhhhhhhh", 0, 0, 0, 0, 0, 0, 0, 0)
        + _be("hhh", 50, 300, 0)
        + b"\0" * 10  # panose
        + _be("LLLL", 1, 0, 0, 0)
        + b"TEST"
        + _be("HHH", 0x40, 0x41, 0x5A)
        + _be("hhhHH", 800, -200, 0, 800, 200)
        + _be("LL", 1, 0)
        + _be("hhHHH", 500, 800, 0x41, 0x20, 0)
    )
    names = {1: "ReamTest", 2: "Regular", 4: "ReamTest", 6: "ReamTest"}
    strings = b"".join(v.encode("utf-16-be") for v in names.values())
    records, at = b"", 0
    for ident, value in names.items():
        size = len(value.encode("utf-16-be"))
        records += _be("HHHHHH", 3, 1, 0x409, ident, size, at)
        at += size
    name = _be("HHH", 0, len(names), 6 + 12 * len(names)) + records + strings
    post = _be("LlhhLLLLL", 0x00030000, 0, -100, 50, 0, 0, 0, 0, 0)
    tables = {
        b"OS/2": os2,
        b"cmap": cmap,
        b"glyf": glyph,
        b"head": head,
        b"hhea": hhea,
        b"hmtx": _be("Hh", 1000, 0) * 2,
        b"loca": _be("LLL", 0, 0, len(glyph)),
        b"maxp": _be(
            "LHHHHHHHHHHHHHH", 0x00010000, 2, 4, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0
        ),
        b"name": name,
        b"post": post,
    }

    def checksum(data: bytes) -> int:
        data += b"\0" * (-len(data) % 4)
        return sum(struct.unpack(f">{len(data) // 4}L", data)) & 0xFFFFFFFF

    def assemble(adjustment: int) -> bytes:
        count = len(tables)
        header = _be("LHHHH", 0x00010000, count, 128, 3, count * 16 - 128)
        offset, directory, body = 12 + 16 * count, b"", b""
        for tag, data in tables.items():
            summed = checksum(
                data
            )  # the head table's is taken with its adjustment at 0
            if tag == b"head":
                data = data[:8] + _be("L", adjustment) + data[12:]
            directory += tag + _be("LLL", summed, offset + len(body), len(data))
            body += data + b"\0" * (-len(data) % 4)
        return header + directory + body

    return assemble((0xB1B0AFBA - checksum(assemble(0))) & 0xFFFFFFFF)


FONT_PAGE = f"""<!doctype html>
<style>
@font-face {{ font-family: test; src: url(data:font/ttf;base64,{base64.b64encode(_square_font()).decode()}) format("truetype"); }}
body {{ margin: 0; background: #fff }}
p {{ margin: 20px; font: 100px/100px test, monospace; color: #000 }}
</style>
<p>H</p>"""


def _check_font_is_fallback(png: bytes | None, path: str) -> None:
    named = f"{path}: the renderer now applies embedded fonts (chromium --version {_version()!r})"
    assert png is not None, f"{path}: no picture"
    _, _, rows = pixels(png)
    dark = [
        (x, y)
        for y, row in enumerate(rows)
        for x, px in enumerate(row)
        if sum(px[:3]) < 150
    ]
    assert dark, f"{path}: no dark pixels at all, not even a fallback letter"
    xs, ys = [x for x, _ in dark], [y for _, y in dark]
    w, h = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
    fill = len(dark) / (w * h)
    # The monospace letter H is taller than wide and mostly empty (CI's Chromium 156: 46x73);
    # the embedded font would give a solid square about 80x80.
    assert h > w and fill < 0.8, (
        f"{named}; dark box {w}x{h}, {fill:.0%} filled, wanted a fallback letter H "
        "(taller than wide, under 80% filled). Expected only if --disable-remote-fonts was "
        "dropped deliberately; then flip this test back to asserting the solid square and "
        "drop the onboarding 'may show a fallback font' sentence"
    )


# Pins today's behaviour: rendercore keeps --disable-remote-fonts, which also stops a data: font;
# security owns the decision to change that.
@needs_browser
def test_a_screenshot_shows_a_fallback_for_a_font_the_page_embeds_as_a_data_url():
    shot = rendercore.shoot(
        rendercore.browser_path(), FONT_PAGE, renderwire.shot_request()
    )
    assert shot.outcome == "shot", shot
    _check_font_is_fallback(shot.png, "shot")


@needs_browser
def test_a_render_shows_a_fallback_for_a_font_the_page_embeds_as_a_data_url():
    _check_font_is_fallback(thumbs.render_report(FONT_PAGE, 1280, 720)[0], "render")
