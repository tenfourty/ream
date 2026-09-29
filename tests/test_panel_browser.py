"""The comments panel, in a REAL browser against a REAL served gallery.

The markup and the harness say the panel is docked and opens by class; only a
browser can say that the page area actually narrows by the panel's width, that
nothing is laid over the frame, that the frame's height follows its new width,
and that the address never changes. That is the whole claim of "comments open
beside the page at once", so it is checked where it is true or false.

Needs the system's headless browser (`ARTIFACTS_BROWSER`, found the same way as
the thumbnail tests); without one -- CI -- it skips. playwright is a dev
dependency and drives that browser; it never downloads one of its own.

The frame's content origin (`<id>.tenfourty.com`) is routed back to the same
live server with that Host header, so the framed page, its shim and its height
reports are the real ones, not a stub.
"""

import urllib.error
import urllib.request

import pytest
from playwright import sync_api
from test_client import _LiveServer

from artifacts import events, store, thumbs
from artifacts.app import create_app

pytestmark = [
    pytest.mark.skipif(
        not thumbs.available(), reason="no headless browser in this environment"
    ),
    pytest.mark.slow(
        reason="a real browser against a live server: seconds, not milliseconds"
    ),
]

# Tall enough to scroll, and prose, so a narrower frame is a TALLER frame: the
# height the gallery sets must follow the width the panel leaves it.
PAGE = (
    "<!doctype html><html><head><title>B</title></head><body>"
    + "".join(
        f"<p>Paragraph {n}: "
        + "a sentence that wraps when the frame narrows. " * 10
        + "</p>"
        for n in range(40)
    )
    + "</body></html>"
)

PANEL_PX = 384  # min(24rem,100%) at a 16px root, on any window over 24rem


@pytest.fixture()
def served(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    server = _LiveServer(create_app(db))
    origin = server.start()
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", origin)
    page = store.publish(db, "seat", "Board", PAGE)
    try:
        yield origin, page.id
    finally:
        # The page holds a "has this been republished" request open; end it as
        # SIGTERM would, or the server's stop finds it still running.
        events.begin_shutdown()
        try:
            server.stop()
        finally:
            events.clear_shutdown()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # hand the redirect to the browser as it was sent


def _forward(origin):
    """Serve `https://<id>.tenfourty.com/...` from the live server, with the
    browser's own request headers (the content app reads Sec-Fetch-Dest) and
    the content Host kept."""
    opener = urllib.request.build_opener(_NoRedirect)

    def handle(route):
        req = route.request
        host = req.url.split("/")[2]
        path = "/" + req.url.split("/", 3)[3]
        headers = {k: v for k, v in req.all_headers().items() if not k.startswith(":")}
        headers["host"] = host
        # Added by the network stack after interception, so absent here; every
        # content-host document in these tests is the gallery's own frame.
        if req.resource_type == "document":
            headers["sec-fetch-dest"] = "iframe"
        fwd = urllib.request.Request(origin + path, headers=headers)  # noqa: S310
        try:
            with opener.open(fwd, timeout=10) as got:
                route.fulfill(
                    status=got.status, headers=dict(got.headers), body=got.read()
                )
        except urllib.error.HTTPError as err:
            route.fulfill(status=err.code, headers=dict(err.headers), body=err.read())

    return handle


@pytest.fixture()
def browser():
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch(executable_path=thumbs.browser_path())
        try:
            yield b
        finally:
            b.close()


def _open_page(browser, origin, ident, width, height=800, js=True, hash_=""):
    ctx = browser.new_context(
        viewport={"width": width, "height": height}, java_script_enabled=js
    )
    ctx.route("https://*.tenfourty.com/**", _forward(origin))
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(f"{origin}/a/{ident}{hash_}")
    pg.wait_for_load_state("load")
    return ctx, pg, errors


def _frame_height_settled(pg):
    """Wait until the gallery has sized the frame to what the page reports."""
    pg.wait_for_function(
        """() => {const f=document.getElementById('artifact-frame');
                 return f && parseFloat(f.style.height) >= 200}""",
        timeout=10000,
    )


def _geometry(pg):
    return pg.evaluate(
        """() => {
          const f = document.getElementById('artifact-frame').getBoundingClientRect();
          const p = document.getElementById('comments').getBoundingClientRect();
          const shown = getComputedStyle(document.getElementById('comments')).display !== 'none';
          // Nothing but the frame (or the page's own lifted blocks, which live
          // on the sheet) may be hit anywhere inside the frame's visible box.
          const hits = [];
          const bottom = Math.min(f.bottom, innerHeight) - 2;
          for (const x of [f.left + 2, (f.left + f.right) / 2, f.right - 2])
            for (const y of [Math.max(f.top, 50) + 2, bottom]) {
              const el = document.elementFromPoint(x, y);
              hits.push(el && (el.id === 'artifact-frame' || el.closest('#artifact-sheet')) ? 'frame'
                        : (el ? el.tagName + '.' + el.className : 'none'));
            }
          return {frameLeft: f.left, frameRight: f.right, frameWidth: f.width,
                  frameHeight: parseFloat(document.getElementById('artifact-frame').style.height),
                  panelLeft: p.left, panelTop: p.top, panelBottom: p.bottom,
                  panelWidth: p.width, panelHeight: p.height, shown,
                  open: document.body.classList.contains('cpanel-open'),
                  href: location.href, entries: history.length, hits,
                  innerWidth, innerHeight};
        }"""
    )


def test_on_a_wide_window_the_panel_docks_in_place_and_is_remembered(
    browser, served, tmp_path
):
    origin, ident = served
    ctx, pg, errors = _open_page(browser, origin, ident, 1400)
    try:
        _frame_height_settled(pg)
        closed = _geometry(pg)
        assert not closed["open"] and not closed["shown"]
        assert closed["frameWidth"] == closed["innerWidth"]

        # Opens beside the page: narrower frame, nothing over it, no navigation.
        pg.click(".cbtn")
        pg.wait_for_function(
            """w => document.getElementById('artifact-frame')
                     .getBoundingClientRect().width < w""",
            arg=closed["frameWidth"],
        )
        # The frame is narrower, so the page inside it is taller; the gallery
        # must have been told, or the frame is clipped or has a gap.
        pg.wait_for_function(
            "h => parseFloat(document.getElementById('artifact-frame').style.height) > h",
            arg=closed["frameHeight"],
            timeout=10000,
        )
        opened = _geometry(pg)
        assert opened["open"] and opened["shown"]
        assert opened["href"] == closed["href"], "opening the panel navigated"
        assert opened["entries"] == closed["entries"], "opening left a history entry"
        assert opened["panelWidth"] == PANEL_PX
        assert opened["frameWidth"] == closed["frameWidth"] - PANEL_PX
        assert opened["panelLeft"] >= opened["frameRight"], "the panel covers the page"
        assert (
            opened["panelTop"] == 48 and opened["panelBottom"] == opened["innerHeight"]
        )
        assert set(opened["hits"]) == {"frame"}, opened["hits"]
        # Not stale: the frame's viewport is the height the gallery set, and
        # the page's content ends inside it, with no strip of it cut off.
        view, bottom = pg.frames[1].evaluate(
            "[innerHeight, Math.ceil(document.body.getBoundingClientRect().bottom)]"
        )
        assert view == opened["frameHeight"], "the frame's height is stale"
        assert bottom <= view, "the narrower page is clipped"
        pg.screenshot(path=str(tmp_path / "panel-1400-open.png"))

        # The "This page" tab, already on: opens in place, no history entry.
        pg.click("#comments .cscope .tab.on")
        same = _geometry(pg)
        assert same["open"] and same["entries"] == closed["entries"]

        # Escape closes it, with the address untouched.
        pg.keyboard.press("Escape")
        pg.wait_for_function("() => !document.body.classList.contains('cpanel-open')")
        after = _geometry(pg)
        assert not after["shown"] and after["frameWidth"] == closed["frameWidth"]
        assert after["href"] == closed["href"] and after["entries"] == closed["entries"]
        pg.screenshot(path=str(tmp_path / "panel-1400-closed.png"))

        # The close button hands focus back rather than dropping it on <body>.
        pg.click(".cbtn")
        pg.click("#comments .cclose")
        assert not _geometry(pg)["open"]
        assert pg.evaluate("() => document.activeElement.className").startswith("cbtn")

        # A reload keeps the panel as it was left, open or closed.
        pg.click(".cbtn")
        pg.reload()
        pg.wait_for_load_state("load")
        assert _geometry(pg)["open"], "left open, a reload closed it"
        pg.keyboard.press("Escape")
        pg.reload()
        pg.wait_for_load_state("load")
        assert not _geometry(pg)["open"], "closed, a reload opened it"

        # Full screen: headless may refuse it; pressing it must be safe, and
        # the button must say what it will do next.
        fs = pg.locator(".fsbtn")
        if fs.is_visible():
            pg.click(".fsbtn")
            pg.wait_for_timeout(200)
            on = pg.evaluate(
                "() => document.fullscreenElement && document.fullscreenElement.id"
            )
            assert fs.get_attribute("aria-label") == (
                "Exit full screen" if on == "artifact-sheet" else "Full screen"
            )
        assert not errors, errors
    finally:
        ctx.close()


def test_on_a_phone_a_sheet_and_with_scripts_off_still_docked(
    browser, served, tmp_path
):
    origin, ident = served
    ctx, pg, errors = _open_page(browser, origin, ident, 390)
    try:
        before = _geometry(pg)
        pg.click(".cbtn")
        pg.wait_for_timeout(300)  # the slide-up
        sheet = _geometry(pg)
        assert sheet["open"] and sheet["shown"]
        assert sheet["panelWidth"] == 390 and sheet["panelLeft"] == 0
        assert sheet["panelBottom"] == sheet["innerHeight"]
        assert abs(sheet["panelHeight"] - 0.7 * sheet["innerHeight"]) <= 1
        assert sheet["frameWidth"] == before["frameWidth"], (
            "a phone's page must not narrow"
        )
        pg.screenshot(path=str(tmp_path / "panel-390-open.png"))
        assert not errors, errors
    finally:
        ctx.close()

    ctx, pg, _errors = _open_page(
        browser, origin, ident, 1400, js=False, hash_="#comments"
    )
    try:
        panel = pg.locator("#comments")
        assert panel.is_visible()
        box, frame = panel.bounding_box(), pg.locator("#artifact-frame").bounding_box()
        assert box["width"] == PANEL_PX
        assert box["x"] >= frame["x"] + frame["width"], "docked, not laid over the page"
        assert frame["width"] == 1400 - PANEL_PX
        styles = panel.evaluate(
            "p => [getComputedStyle(p).backdropFilter,"
            " getComputedStyle(document.body).backdropFilter]"
        )
        assert styles == ["none", "none"]
        # And its close link drops the target without jumping to the top.
        assert panel.locator(".cclose").get_attribute("href") == "#_"
    finally:
        ctx.close()
