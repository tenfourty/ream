"""The comments panel, in a REAL browser against a REAL served gallery.

The markup and the harness say the panel is docked and opens by class; only a
browser can say that the page area actually narrows by the panel's width, that
nothing is laid over the frame, that the frame's height follows its new width,
and that the address never changes. That is the whole claim of "comments open
beside the page at once", so it is checked where it is true or false.

Needs the system's headless browser (`REAM_BROWSER`, found the same way as
the thumbnail tests); without one it skips -- except where REAM_REQUIRE_BROWSER=1
(CI), where a missing browser fails the whole run at start (tests/conftest.py). playwright is a dev
dependency and drives that browser; it never downloads one of its own.

The frame's content origin (`<id>.tenfourty.com`) is routed back to the same
live server with that Host header, so the framed page, its shim and its height
reports are the real ones, not a stub.
"""

import json
import os
import urllib.error
import urllib.request

import pytest
from playwright import sync_api
from test_client import _LiveServer

from ream import comments, events, store, thumbs
from ream.app import create_app

pytestmark = [
    pytest.mark.skipif(
        not thumbs.available(),
        reason="no headless browser: set REAM_BROWSER to a chrome binary, or put "
        "chromium on PATH; these browser tests do NOT run without one",
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
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    server = _LiveServer(create_app(db))
    origin = server.start()
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", origin)
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
        # CI's job container runs as root, where chromium refuses to start without
        # --no-sandbox (thumbs.py passes it for the same reason); a person's shell
        # is not root and keeps the sandbox.
        b = p.chromium.launch(
            executable_path=thumbs.browser_path(),
            args=["--no-sandbox"] if os.geteuid() == 0 else [],
        )
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

        # Escape turns Comment off first, a second one closes the panel, with the address
        # untouched.
        pg.keyboard.press("Escape")
        pg.wait_for_function(
            "() => document.querySelector('.cbtn').getAttribute('aria-pressed') === 'false'"
        )
        assert _geometry(pg)["open"]
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


# Every frame records what it is sent; the gallery records what its frame
# sends it. The init script runs in every document, the framed one included.
_SPY = """
window.__got = [];
addEventListener('message', function (e) {
  try { window.__got.push(JSON.stringify(e.data)); } catch (x) {}
}, true);
"""


def test_hovering_marked_text_shows_its_comment_beside_it(browser, served, db):
    """Rest the pointer on marked text in the agent's page: a card with the
    note appears in the GALLERY document beside it; move away and it goes.
    The frame is never sent the note, and what it sends is only type, id and
    rect -- checked on the wire, both ways, not inferred from the source."""
    origin, ident = served
    quote = "Paragraph 3: a sentence that wraps"
    note = "HOVERNOTE keep this sentence shorter"
    root = comments.add(
        db,
        artifact_id=ident,
        author_kind="human",
        author="jeremy",
        body=note,
        quote=quote,
        anchor={
            "kind": "range",
            "path": "body>p:nth-of-type(4)",
            "start": 0,
            "end": len(quote),
        },
    )
    ctx = browser.new_context(viewport={"width": 1400, "height": 800})
    ctx.add_init_script(_SPY)
    ctx.route("https://*.tenfourty.com/**", _forward(origin))
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(f"{origin}/a/{ident}")
        pg.wait_for_load_state("load")
        _frame_height_settled(pg)
        frame = pg.frames[1]
        frame.wait_for_function(
            "() => CSS.highlights.has('ar-comment')"
            " && CSS.highlights.get('ar-comment').size === 1",
            timeout=10000,
        )
        # Where the marked words are, in the gallery's coordinates.
        box = pg.locator("#artifact-frame").bounding_box()
        r = frame.evaluate(
            """() => {const t=document.querySelectorAll('p')[3].firstChild;
                      const g=document.createRange();g.setStart(t,2);g.setEnd(t,8);
                      const b=g.getBoundingClientRect();
                      return {x:b.left+b.width/2,y:b.top+b.height/2}}"""
        )
        pg.mouse.move(box["x"] + r["x"], box["y"] + r["y"])
        card = pg.locator(".hovercard")
        card.wait_for(state="visible", timeout=3000)
        assert note in card.inner_text()
        assert card.get_attribute("data-mark-id") == root.id
        cb = card.bounding_box()
        assert 0 <= cb["x"] and cb["x"] + cb["width"] <= 1400
        assert abs(cb["y"] - (box["y"] + r["y"])) < 80, (
            "the card is not beside the text"
        )
        assert cb["width"] <= 22 * 16 + 1
        assert pg.evaluate("() => document.querySelectorAll('.hovercard').length") == 1
        shot = os.environ.get("REAM_HOVER_SHOT")
        if shot:
            pg.screenshot(path=shot)

        # Away from any mark, inside the frame AND inside the window (the
        # window is 800 high: a pointer parked below it sends the page nothing,
        # so no leave ever reaches the gallery): the card goes after its grace.
        away = {"x": box["x"] + 900, "y": box["y"] + r["y"] + 40}
        assert away["y"] < 800 and away["x"] < 1400, away
        pg.mouse.move(away["x"], away["y"])
        card.wait_for(state="detached", timeout=3000)

        # On the wire. What the frame was sent: marks (no note) and nothing
        # carrying the note. What the frame sent about hovering: only these keys.
        got = frame.evaluate("() => window.__got")
        assert got and not any(note in m for m in got), got
        sent = [m for m in pg.evaluate("() => window.__got") if '"artifact-mark-' in m]
        hovers = [json.loads(m) for m in sent]
        assert any(h["type"] == "artifact-mark-hover" for h in hovers), sent
        assert any(h["type"] == "artifact-mark-leave" for h in hovers), sent
        for h in hovers:
            if h["type"] == "artifact-mark-hover":
                assert sorted(h) == ["id", "rect", "type"] and h["id"] == root.id
                assert sorted(h["rect"]) == ["height", "left", "top", "width"]
            else:
                assert h == {"type": "artifact-mark-leave"}
        assert not any(quote in m or note in m for m in sent)
        assert not errors, errors
    finally:
        ctx.close()


def _mark(db, ident, n, note):
    quote = f"Paragraph {n}: a sentence that wraps"
    return comments.add(
        db,
        artifact_id=ident,
        author_kind="human",
        author="jeremy",
        body=note,
        quote=quote,
        anchor={
            "kind": "range",
            "path": f"body>p:nth-of-type({n + 1})",
            "start": 0,
            "end": len(quote),
        },
    )


def _point_at(pg, n):
    """Put the pointer on paragraph n's marked words, in gallery coordinates."""
    box = pg.locator("#artifact-frame").bounding_box()
    r = pg.frames[1].evaluate(
        """n => {const t=document.querySelectorAll('p')[n].firstChild;
                 const g=document.createRange();g.setStart(t,2);g.setEnd(t,8);
                 const b=g.getBoundingClientRect();
                 return {x:b.left+b.width/2,y:b.top+b.height/2}}""",
        n,
    )
    pg.mouse.move(box["x"] + r["x"], box["y"] + r["y"])


def test_a_reply_being_typed_survives_and_full_screen_shows_the_card(
    browser, served, db
):
    """The card reads Reply . Open; Reply reveals its box. Text typed there
    survives hovering another mark. In full screen the card is drawn inside the
    full-screen sheet, where it can actually be seen."""
    origin, ident = served
    first = _mark(db, ident, 3, "FIRSTNOTE shorten this")
    _mark(db, ident, 5, "SECONDNOTE and this")
    ctx, pg, errors = _open_page(browser, origin, ident, 1400)
    try:
        _frame_height_settled(pg)
        pg.frames[1].wait_for_function(
            "() => CSS.highlights.get('ar-comment')"
            " && CSS.highlights.get('ar-comment').size === 2",
            timeout=10000,
        )
        _point_at(pg, 3)
        card = pg.locator(".hovercard")
        card.wait_for(state="visible", timeout=3000)
        assert card.locator(".hcact button").all_inner_texts() == ["Reply", "Open"]
        assert not card.locator("form.creply").is_visible()
        shot = os.environ.get("REAM_HOVER_SHOT2")
        if shot:
            pg.screenshot(path=shot)
        card.get_by_role("button", name="Reply").click()
        box = card.locator("input[name=body]")
        assert box.is_visible()
        box.type("half a thought")
        _point_at(pg, 5)
        pg.wait_for_timeout(700)  # past the rest and the grace, both
        assert pg.locator(".hovercard").count() == 1
        assert card.get_attribute("data-mark-id") == first.id
        assert box.input_value() == "half a thought"
        # Escape is a choice to leave.
        pg.keyboard.press("Escape")
        card.wait_for(state="detached", timeout=2000)

        pg.click(".fsbtn")
        pg.wait_for_timeout(300)
        if pg.evaluate("() => document.fullscreenElement?.id") != "artifact-sheet":
            pytest.skip("this headless browser refused full screen")
        pg.frames[1].evaluate("() => window.scrollTo(0, 0)")
        _point_at(pg, 5)
        card.wait_for(state="attached", timeout=3000)
        seen = pg.evaluate(
            """() => {const c=document.querySelector('.hovercard'),b=c.getBoundingClientRect();
                      const hit=document.elementFromPoint(b.left+b.width/2,b.top+10);
                      return {inside: document.fullscreenElement.contains(c),
                              drawn: !!(hit && c.contains(hit)), text: c.innerText}}"""
        )
        assert seen["inside"] and seen["drawn"], seen
        assert "SECONDNOTE" in seen["text"]
        assert not errors, errors
    finally:
        ctx.close()


def test_round_two_opens_on_green_insertions_and_hides_them_on_request(
    browser, served, db
):
    """Page review step 4: in round 2 the frame shows what changed since the
    version he sent back -- insertions green, deletions struck -- and the
    card's Hide changes link brings back the plain page."""
    from ream import decisions, nudge

    origin, ident = served
    nudge_emit, nudge.emit = nudge.emit, lambda **kw: None
    try:
        review = decisions.ask_review(db, agent="seat", artifact_id=ident)
        comments.add(
            db,
            artifact_id=ident,
            author_kind="human",
            author="jeremy",
            kind="comment",
            anchor={"kind": "range", "path": "/p[1]", "start": 0, "end": 11},
            quote="Paragraph 0",
            body="say more",
        )
        decisions.answer(db, review.id, letter="c", note="", by="jeremy")
        page = store.get_owned(db, "seat", ident)
        store.patch(
            db,
            "seat",
            ident,
            PAGE.replace("Paragraph 0:", "Paragraph 0, now clearer:"),
            page.revision,
        )
    finally:
        nudge.emit = nudge_emit
    ctx, pg, errors = _open_page(browser, origin, ident, 1400)
    try:
        assert "Round 2" in pg.text_content(".ask")
        frame = pg.frame_locator("#artifact-frame")
        ins = frame.locator("ins.ar-diff-ins").first
        ins.wait_for(timeout=10000)
        assert "clearer" in ins.inner_text()
        assert ins.evaluate("e => getComputedStyle(e).backgroundColor") == (
            "rgba(46, 157, 87, 0.22)"
        )
        assert ins.evaluate("e => getComputedStyle(e).textDecorationLine") == "none"
        struck = frame.locator("del.ar-diff-del").first
        assert struck.evaluate("e => getComputedStyle(e).textDecorationLine") == (
            "line-through"
        )
        assert "still on page" in pg.text_content(".carried")
        shot = os.environ.get("REAM_ROUND2_SHOT")
        if shot:
            pg.screenshot(path=shot)

        pg.click("text=Hide changes")
        pg.wait_for_load_state("load")
        assert pg.url.endswith("?changes=0")
        frame = pg.frame_locator("#artifact-frame")
        frame.locator("p").first.wait_for(timeout=10000)
        assert frame.locator("ins.ar-diff-ins").count() == 0
        assert "Show changes" in pg.text_content(".ask")
        assert not errors, errors
    finally:
        ctx.close()


def test_a_mark_made_across_a_deletion_stores_the_live_words(browser, served, db):
    """Page review step 4, review fix: selecting across struck-out old words
    in round 2's changes view stores the live page's words and offsets, so
    the mark paints on the plain page too."""
    from ream import decisions, nudge

    origin, ident = served
    nudge_emit, nudge.emit = nudge.emit, lambda **kw: None
    try:
        review = decisions.ask_review(db, agent="seat", artifact_id=ident)
        decisions.answer(db, review.id, letter="c", note="", by="jeremy")
        page = store.get_owned(db, "seat", ident)
        store.patch(
            db,
            "seat",
            ident,
            PAGE.replace("Paragraph 0: a sentence", "Paragraph 0: one sentence"),
            page.revision,
        )
    finally:
        nudge.emit = nudge_emit
    ctx, pg, errors = _open_page(browser, origin, ident, 1400)
    try:
        frame = pg.frame_locator("#artifact-frame")
        frame.locator("del.ar-diff-del").first.wait_for(timeout=10000)
        assert frame.locator("del.ar-diff-del").first.evaluate(
            "e => getComputedStyle(e).userSelect"
        ) == ("none")
        # From "Paragraph" to "sentence", across the struck "a".
        frame.locator("body").evaluate(
            """b => {const p = b.querySelector('p');
                     const first = p.firstChild, last = p.querySelector('ins').nextSibling;
                     const r = document.createRange();
                     r.setStart(first, 0); r.setEnd(last, ' sentence'.length);
                     const s = getSelection(); s.removeAllRanges(); s.addRange(r);}"""
        )
        pg.locator(".selbar").wait_for(state="visible", timeout=5000)
        pg.click(".selbar button[data-kind=comment]")
        pg.fill("form.cnew input[name=body]", "a note on the words")
        pg.press("form.cnew input[name=body]", "Enter")
        for _ in range(50):
            rows = [
                c
                for c in comments.list_for_artifact(db, ident, include_held=True)
                if c.author_kind == "human"
            ]
            if rows:
                break
            pg.wait_for_timeout(100)
        assert len(rows) == 1, rows
        mark = rows[0]
        assert mark.held  # round 2 holds his marks by default, so it paints dotted
        assert mark.quote == "Paragraph 0: one sentence", mark.quote
        plain = PAGE.replace("Paragraph 0: a sentence", "Paragraph 0: one sentence")
        first_p = plain.split("<p>")[1].split("</p>")[0]
        assert first_p[mark.anchor["start"] : mark.anchor["end"]] == mark.quote
        ctx.close()

        ctx, pg, errors = _open_page(browser, origin, ident, 1400, hash_="?changes=0")
        pg.frame_locator("#artifact-frame").locator("p").first.wait_for(timeout=10000)
        frame = pg.frames[1]
        frame.wait_for_function(
            "() => CSS.highlights.get('ar-held') && CSS.highlights.get('ar-held').size === 1",
            timeout=10000,
        )
        painted = frame.evaluate(
            "() => [...CSS.highlights.get('ar-held')][0].toString()"
        )
        assert painted == "Paragraph 0: one sentence"
        assert not errors, errors
    finally:
        ctx.close()


CHART_PAGE = (
    "<!doctype html><html><head><title>C</title></head><body>"
    "<h2>Latency</h2><p>Before the chart comes a sentence.</p>"
    '<figure id="chart" style="margin:20px 0;width:420px">'
    '<svg width="400" height="160" viewBox="0 0 400 160">'
    "<title>Latency by region</title>"
    '<rect x="10" y="40" width="80" height="110" fill="#0f6f63"/>'
    '<rect x="120" y="70" width="80" height="80" fill="#5ec9b8"/></svg>'
    "<figcaption>Latency, p95</figcaption></figure>"
    "<p>After the chart comes another sentence.</p></body></html>"
)


def test_with_comment_lit_pointing_at_a_chart_boxes_it_and_a_click_starts_a_comment_on_it(
    browser, served, db
):
    """Press Comment, then point at a chart: a box is drawn round it by the page's
    own shim (one host on the document element, closed shadow root), and a click
    on it opens the composer on that element. The paragraph beside it gets no box,
    and with Comment off nothing is drawn at all."""
    origin, _ = served
    page = store.publish(db, "seat", "Chart", CHART_PAGE)
    ctx, pg, errors = _open_page(browser, origin, page.id, 1400)
    try:
        _frame_height_settled(pg)
        frame = pg.frames[1]
        host_state = """() => { const h = document.documentElement.lastElementChild;
            return h && h.nodeName === 'AR-COMMENT-HOST'
              ? getComputedStyle(h).display : 'none' }"""
        assert frame.evaluate(host_state) == "none"
        # Unlit: pointing at the chart draws nothing.
        box = pg.locator("#artifact-frame").bounding_box()
        pg.mouse.move(box["x"] + 200, box["y"] + 200)
        pg.wait_for_timeout(300)
        assert frame.evaluate(host_state) == "none", "unlit, nothing is drawn"
        pg.click(".cbtn")
        pg.wait_for_function("() => document.body.classList.contains('cmode-on')")
        _frame_height_settled(pg)
        pg.wait_for_timeout(300)
        box = pg.locator("#artifact-frame").bounding_box()
        rect = frame.evaluate(
            """() => { const b = document.getElementById('chart').getBoundingClientRect();
                      return {l: b.left, t: b.top, r: b.right, b: b.bottom} }"""
        )
        para = frame.evaluate(
            """() => { const b = document.querySelectorAll('p')[0].getBoundingClientRect();
                      return {x: b.left + 20, y: b.top + b.height / 2} }"""
        )
        pg.mouse.move(box["x"] + para["x"], box["y"] + para["y"])
        pg.wait_for_timeout(300)
        assert frame.evaluate(host_state) == "none", "text gets no button"
        pg.mouse.move(box["x"] + rect["l"] + 120, box["y"] + rect["t"] + 90)
        frame.wait_for_function(
            "() => document.documentElement.lastElementChild.nodeName"
            " === 'AR-COMMENT-HOST' && getComputedStyle("
            "document.documentElement.lastElementChild).display === 'block'",
            timeout=5000,
        )
        host = frame.evaluate(
            """() => { const b = document.documentElement.lastElementChild
                         .getBoundingClientRect();
                      return {l: b.left, t: b.top, r: b.right, b: b.bottom,
                              inbody: !!document.body.querySelector('ar-comment-host')} }"""
        )
        assert not host["inbody"], "the button is outside the page's own content"
        assert rect["t"] - 2 <= host["t"] <= rect["t"] + 2
        assert rect["r"] - 2 <= host["r"] <= rect["r"] + 2, "the box is round the chart"
        # The page's own text is untouched by it.
        assert "Comment" not in frame.evaluate("() => document.body.innerText")
        # The box lets the click through to the chart itself.
        pg.mouse.click(box["x"] + rect["l"] + 120, box["y"] + rect["t"] + 90)
        quote = pg.locator(".cquote")
        quote.wait_for(state="visible", timeout=5000)
        assert "Latency" in quote.inner_text()
        anchor = json.loads(
            pg.locator("form input[name=anchor]").first.get_attribute("value")
        )
        assert anchor["kind"] == "element" and anchor["id"] == "chart"
        assert "pinned" in pg.locator("form input[name=body]").first.get_attribute(
            "placeholder"
        )
        assert not errors, errors
    finally:
        ctx.close()


FAR_PAGE = (
    "<!doctype html><html><head><title>F</title></head><body>"
    + "".join(
        f"<p>Filler {n}: " + "words to push the heading down. " * 12 + "</p>"
        for n in range(60)
    )
    + '<h2 id="far">A heading far down the page</h2>'
    + "<p>And a last paragraph under it.</p></body></html>"
)


def test_selecting_text_far_down_a_page_shows_the_bar_under_it(browser, served, db):
    """runcove-5t3oj.85: the wall's select-mode bar once shared the class `.selbar`
    with this bar, and its later `position:sticky` rule won, so the page-coordinate
    `top` set here pushed the bar off screen for any selection more than about one
    window down. Playwright's "visible" ignores the viewport, so the box is checked."""
    origin, _ = served
    page = store.publish(db, "seat", "Far", FAR_PAGE)
    ctx, pg, errors = _open_page(browser, origin, page.id, 1400, height=800)
    try:
        _frame_height_settled(pg)
        heading = pg.frame_locator("#artifact-frame").locator("#far")
        heading.scroll_into_view_if_needed()
        frame = pg.frames[1]
        frame.evaluate(
            """() => {const h = document.getElementById('far');
                     const r = document.createRange(); r.selectNodeContents(h);
                     const s = getSelection(); s.removeAllRanges(); s.addRange(r);}"""
        )
        pg.locator(".selbar").wait_for(state="visible", timeout=5000)
        got = pg.evaluate(
            """() => { const bar = document.querySelector('.selbar');
                      const b = bar.getBoundingClientRect();
                      const h = document.getElementById('artifact-frame')
                        .getBoundingClientRect().top + window.scrollY;
                      return {top: b.top, bottom: b.bottom, left: b.left, right: b.right,
                              position: getComputedStyle(bar).position,
                              scrollY, innerHeight, innerWidth, frameTop: h} }"""
        )
        far = frame.evaluate(
            "() => document.getElementById('far').getBoundingClientRect().top"
        )
        # The heading really is far down: at least one and a half windows.
        assert got["frameTop"] + far >= 1.5 * got["innerHeight"], (got, far)
        assert got["position"] == "absolute", got
        assert 0 <= got["top"] and got["bottom"] <= got["innerHeight"], got
        assert 0 <= got["left"] and got["right"] <= got["innerWidth"], got
        assert not errors, errors
    finally:
        ctx.close()


def _with_a_question_slot(db, ident):
    """Ask one question on the page and make the page mark where it goes."""
    from ream import decisions

    asked = decisions.ask(
        db,
        agent="seat",
        artifact_id=ident,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    page = store.get_owned(db, "seat", ident)
    short = (
        "<!doctype html><html><body><h1>Short</h1>"
        f'<div data-artifact-decision="{asked.id}"></div></body></html>'
    )
    store.patch(db, "seat", ident, short, page.revision)


_PLACES = """() => {
  const top = e => { const x = document.querySelector(e);
    return x ? x.getBoundingClientRect() : null };
  const ask = top('.ask'), sheet = top('.sheet');
  return {askBottom: ask && ask.bottom, sheetTop: sheet && sheet.top,
          askInSheet: !!document.querySelector('.sheet .ask')}
}"""


def test_a_narrow_window_puts_the_question_above_a_frame_that_fills_the_rest(
    browser, served, db
):
    """At 390px the question sits above the frame, the frame follows at once,
    and no room is held open inside the frame where the question was lifted at
    a wider width -- also when the window is narrowed AFTER the page loaded
    (a phone turned to portrait), which used to leave the question at the foot
    of the sheet under a blank frame."""
    origin, ident = served
    _with_a_question_slot(db, ident)

    ctx, pg, errors = _open_page(browser, origin, ident, 390)
    try:
        pg.wait_for_timeout(1500)
        fresh = pg.evaluate(_PLACES)
    finally:
        ctx.close()
    assert not fresh["askInSheet"]
    assert 0 <= fresh["sheetTop"] - fresh["askBottom"] < 120

    ctx, pg, errors = _open_page(browser, origin, ident, 1400)
    try:
        pg.wait_for_function(
            "() => !!document.querySelector('.sheet .ask')", timeout=10000
        )  # lifted onto the frame, as on a desktop
        pg.set_viewport_size({"width": 390, "height": 800})
        pg.wait_for_function(
            "() => !document.querySelector('.sheet .ask')", timeout=10000
        )
        pg.wait_for_timeout(500)
        narrowed = pg.evaluate(_PLACES)
        held = pg.frames[1].evaluate(
            "() => document.querySelector('[data-artifact-decision]').style.minHeight"
        )
    finally:
        ctx.close()
    assert not narrowed["askInSheet"]
    assert narrowed["askBottom"] <= narrowed["sheetTop"]
    assert held in ("", "0px"), "the frame still holds room open for the question"
    assert errors == []


def test_the_connect_page_fits_a_phone_without_clipping_its_boxes(browser, served):
    """At 390px the code boxes and the paste box stay inside the window: the
    page does not scroll sideways and no box or card reaches past the right
    edge (a long unbroken line in the paste box used to widen the whole column)."""
    origin, _ = served
    ctx = browser.new_context(viewport={"width": 390, "height": 800})
    try:
        pg = ctx.new_page()
        pg.goto(f"{origin}/connect")
        pg.wait_for_load_state("load")
        pg.wait_for_selector(".cx .boxes input")  # the script swapped in the boxes
        got = pg.evaluate(
            """() => {
              const w = document.documentElement.clientWidth;
              const right = e => e.getBoundingClientRect().right;
              const els = [...document.querySelectorAll(
                '.cx section, .cx pre, .cx input')];
              return {w, scroll: document.documentElement.scrollWidth,
                      n: document.querySelectorAll('.cx .boxes input').length,
                      pre: !!document.querySelector('.cx pre'),
                      over: els.filter(e => right(e) > w + 0.5)
                               .map(e => e.className || e.tagName)}
            }"""
        )
    finally:
        ctx.close()
    assert got["n"] == 8 and got["pre"]
    assert got["over"] == []
    assert got["scroll"] <= got["w"]


def _retro_page():
    """A long report as agents publish them: a title, a lead paragraph, a row of
    stat cards, then sections with a table and a picture far down."""
    cards = "".join(
        f'<div class="card"><b>{n}</b><span>stat {n}</span></div>' for n in range(4)
    )
    filler = "".join(
        f"<p>Filler paragraph {n}. " + "words " * 40 + "</p>" for n in range(12)
    )
    return (
        "<!doctype html><html><head><title>Retro</title><style>"
        ".card{padding:20px;border:1px solid #ccc}"
        ".stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}"
        "</style></head><body><div class='wrap' style='max-width:900px;margin:0 auto;padding:24px'>"
        "<h1>Week 45 retro</h1>"
        "<p class='lead'>The lead paragraph says how the week went.</p>"
        f"<div class='stats'>{cards}</div>"
        "<section><h2>Early</h2>" + filler + "</section>"
        "<section><h2>Numbers</h2>"
        "<table id='nums'><tr><th>Team</th><th>Score</th></tr>"
        "<tr><td>Alpha</td><td>3</td></tr><tr><td>Beta</td><td>5</td></tr></table>"
        + filler
        + "<img id='pic' alt='A diagram' width='300' height='120' "
        "src='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'>"
        + filler
        + "</section></div>"
        # The column is not the whole page: a tall footer outside it, so the
        # shim's page-wide-wrapper test does not rule the column out.
        "<footer style='height:3000px'>Footer</footer></body></html>"
    )


def test_the_comment_box_sits_round_the_thing_pointed_at_on_a_long_page(
    browser, served, db
):
    """Owner, 9 Nov: the Comment pill showed at the top of the content column,
    far from what he pointed at. A block that merely CONTAINS a table or a
    picture (the card row's section, a page-wide wrapper) is not a chart, table
    or picture: only the thing itself, or a small box around it, is offered
    the button, and then the button is at its own corner, top of the page or
    far down it."""
    origin, _ = served
    page = store.publish(db, "seat", "Retro", _retro_page())
    ctx, pg, errors = _open_page(browser, origin, page.id, 1400)
    try:
        _frame_height_settled(pg)
        frame = pg.frames[1]
        shown = """() => { const h = document.documentElement.lastElementChild;
            if (!h || h.nodeName !== 'AR-COMMENT-HOST') return null;
            if (getComputedStyle(h).display === 'none') return null;
            const b = h.getBoundingClientRect();
            return {t: b.top, l: b.left, r: b.right, b: b.bottom} }"""
        pg.click(".cbtn")
        pg.wait_for_function("() => document.body.classList.contains('cmode-on')")
        _frame_height_settled(pg)

        def point_at(selector, dx=0.5, dy=0.5):
            # The frame is as tall as the page, so it is the gallery that scrolls.
            top = frame.evaluate(
                "(s) => document.querySelector(s).getBoundingClientRect().top"
                " + scrollY",
                selector,
            )
            pg.evaluate(
                "(y) => window.scrollTo(0, document.getElementById('artifact-frame')"
                ".getBoundingClientRect().top + scrollY + y - 300)",
                top,
            )
            pg.wait_for_timeout(200)
            fb = pg.locator("#artifact-frame").bounding_box()
            r = frame.evaluate(
                """(s) => { const b = document.querySelector(s).getBoundingClientRect();
                           return {l: b.left, t: b.top, w: b.width, h: b.height} }""",
                selector,
            )
            pg.mouse.move(0, 0)
            pg.wait_for_timeout(450)
            pg.mouse.move(
                fb["x"] + r["l"] + r["w"] * dx, fb["y"] + r["t"] + r["h"] * dy
            )
            pg.wait_for_timeout(450)
            return r

        # Text, the lead, and the stat cards: no button at all.
        for sel in ("p.lead", ".stats .card", "h1", "section p"):
            point_at(sel)
            assert frame.evaluate(shown) is None, f"{sel} must get no button"
        # Padding and gaps around blocks hit the column or a section, which only
        # CONTAIN a table or picture.
        for sel in (".wrap", "section"):
            for dx, dy in ((0.5, 0.0), (0.5, 0.005), (0.001, 0.5), (0.999, 0.1)):
                point_at(sel, dx, dy)
                assert frame.evaluate(shown) is None, f"{sel} {dx},{dy}: no button"
        # The table and the picture, far down: the button is at their corner.
        for sel in ("#nums", "#pic"):
            r = point_at(sel)
            got = frame.evaluate(shown)
            for _ in range(20):
                if got:
                    break
                pg.wait_for_timeout(100)
                got = frame.evaluate(shown)
            assert got, sel
            # At the corner of what was pointed at (a table's button may sit at
            # the corner of the cell under the pointer): inside its box, never
            # at the top of the column.
            assert r["t"] - 1 <= got["t"] and got["b"] <= r["t"] + r["h"] + 1, (
                sel,
                r,
                got,
            )
            assert r["l"] - 1 <= got["l"] and got["r"] <= r["l"] + r["w"] + 1, (
                sel,
                r,
                got,
            )
        # Selecting words, at the top and far down: the bar is under the words.
        for sel in ("p.lead", "section:last-of-type p:last-of-type"):
            r = point_at(sel)
            frame.evaluate(
                """(s) => { const p = document.querySelector(s), g = document.createRange();
                    g.selectNodeContents(p.firstChild);
                    const x = getSelection(); x.removeAllRanges(); x.addRange(g);
                    document.dispatchEvent(new Event('selectionchange')) }""",
                sel,
            )
            fb = pg.locator("#artifact-frame").bounding_box()
            line = fb["y"] + r["t"]
            bar = None
            for _ in range(30):  # the bar moves when the new selection arrives
                bar = pg.locator(".selbar").bounding_box()
                if bar and line - 60 <= bar["y"] <= line + r["h"] + 40:
                    break
                pg.wait_for_timeout(100)
            assert bar and line - 60 <= bar["y"] <= line + r["h"] + 40, (sel, bar, line)
        assert not errors, errors
    finally:
        ctx.close()
