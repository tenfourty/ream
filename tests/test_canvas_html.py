"""HTML-in-Canvas draws in Ream's pictures: a page that puts live HTML in a
`<canvas layoutsubtree>` and draws it with `drawElementImage` on the canvas's `paint`
event shows that drawing in a screenshot (`rendercore.shoot`) and in a render
(`thumbs.render_report`: the wall's tiles and preview). Chromium 147-156 has the
feature behind a flag, which `rendercore._isolation_flags` turns on; CI's browser
step runs this with REAM_REQUIRE_BROWSER=1, so a browser that cannot draw fails
here and names its version, rather than a blank canvas passing as a picture.
"""

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
