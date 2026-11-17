"""The templates an agent starts from, checked the way a reader would be hurt.

homelab-ij8.69. An agent cannot see the page it published -- the gallery holds
Jeremy's login and the page answers only a framed request with a token the agent
never gets. So every defect in a template is invisible to the agent that shipped
it, and these checks stand in for the look nobody can take. Two of them are
here because the defect reached Jeremy for real (homelab-ij8.38): a board that
kept its unedited title placeholder in the browser tab for two hours, and a
footer that asked for a page id which does not exist until after the publish.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from ream import csp, stamp

REPO = Path(__file__).resolve().parent.parent
CANON = REPO / "src" / "ream" / "templates"
SHARED = CANON / "_shared.css"

BEGIN = "/* ==== shared: begin — do not edit here; edit _shared.css and re-sync ==== */"
END = "/* ==== shared: end ==== */"

TEMPLATES = sorted(CANON.glob("*.html"))


def _body(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_there_are_templates_at_all():
    """A glob that matches nothing makes every parametrised test below vacuous.

    Asserting a positive count rather than the absence of failures: an empty
    directory would otherwise report a clean run, which is the exact shape of
    gate that has burned this house before.
    """
    assert TEMPLATES, f"no templates in {CANON}"
    assert SHARED.is_file(), f"{SHARED} is missing"


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
def test_the_service_finds_nothing_it_will_break(path: Path):
    """Zero CSP warnings, from the service's own checker.

    This is the check that matters most. Before the warnings existed, a page
    with a Google Fonts link published 201 and looked fine until Jeremy opened
    it and half of it silently did not render.
    """
    found = csp.check(_body(path))
    assert found == [], f"{path.name}: {[f'{w.shape} line {w.line}' for w in found]}"


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
def test_it_is_a_whole_document(path: Path):
    body = _body(path)
    assert body.startswith("<!doctype html>"), (
        f"{path.name} does not open with a doctype"
    )
    assert "</html>" in body, f"{path.name} has no closing html"


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
def test_no_comment_swallows_the_page(path: Path):
    """HTML comments do not nest, so a '-->' inside one ends it early.

    Caught for real while writing the first template: the banner explaining the
    stamp marker contained the marker, which terminated the banner and would
    have spilled the rest of it onto the page as visible text.
    """
    depth = 0
    for match in re.finditer(r"<!--|-->", _body(path)):
        if match.group() == "<!--":
            depth += 1
            assert depth == 1, (
                f"{path.name}: a comment opens inside another at offset "
                f"{match.start()} — the outer one already ended"
            )
        else:
            depth = max(0, depth - 1)
    assert depth == 0, f"{path.name}: a comment is never closed"


def _strip_comments(body: str) -> str:
    """What is left after every comment a reader never sees is removed.

    Both kinds: HTML comments, and the CSS comments inside <style>. The rules
    below are about what reaches a reader, and a comment recording WHY a rule
    exists -- which usually means naming the day it was learned -- must not trip
    a check on what the page says.
    """
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    return re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
def test_the_stamp_is_a_marker_and_never_a_typed_date(path: Path):
    """No template types a time, and each one carries the marker at least once.

    The service substitutes the marker when it SERVES the page, so the stored
    body stays identical across an unchanged republish and an agent never holds
    a clock. A template that typed a date would teach every page built from it
    to be wrong about one.
    """
    body = _body(path)
    assert body.count(stamp.MARKER) >= 1, f"{path.name} has no updated-stamp marker"

    # Anything that looks like a date a human wrote, in what a reader sees. The
    # marker itself contains no digits, so it cannot trip this.
    typed = re.findall(
        r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}"
        r"|\b\d{4}-\d{2}-\d{2}\b"
        r"|\b\d{1,2}:\d{2}\s*(?:UTC|CET|CEST|GMT)\b",
        _strip_comments(body),
    )
    assert not typed, f"{path.name} types a date or time: {typed}"


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
def test_no_placeholder_a_reader_would_see(path: Path):
    """The two that reached Jeremy, plus the shape they share.

    A placeholder that looks like content is worse than an empty slot, because
    the agent that left it there cannot look at the page to notice.
    """
    body = _body(path)
    assert "PAGE TITLE" not in body, f"{path.name} still carries the title placeholder"
    assert "<code>ID</code>" not in body, (
        f"{path.name} asks for a page id, which does not exist until after the publish"
    )
    # A slot with a made-up id renders as a slot that never fills, which looks
    # exactly like one still loading (homelab-ij8.50). Templates ship the slot
    # commented out instead.
    live_slots = re.findall(
        r'<div data-artifact-decision="([^"]*)"', _strip_comments(body)
    )
    assert not live_slots, (
        f"{path.name} ships a live decision slot with id(s) {live_slots}; "
        "comment the slot out so a real id has to be put in deliberately"
    )


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
def test_the_gallery_link_opens_a_new_tab(path: Path):
    """A same-tab gallery link is a dead link, found by Jeremy on a real page.

    The page is framed, the gallery refuses to be framed, and the frame's
    sandbox forbids top-level navigation.
    """
    visible = _strip_comments(_body(path))
    for link in re.findall(r"<a\b[^>]*artifacts\.tenfourty\.site[^>]*>", visible):
        assert 'target="_blank"' in link, (
            f"{path.name}: gallery link is not target=_blank"
        )
        assert "noopener" in link, f"{path.name}: gallery link has no rel=noopener"


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
def test_the_shared_block_is_the_shared_block(path: Path):
    """Both sentinels present, and the region between them equal to _shared.css.

    Asserting the markers FIRST is the point: a file that had lost one would
    otherwise contribute an empty region, and an all-empty comparison passes.
    """
    body = _body(path)
    assert BEGIN in body, f"{path.name} has lost the shared-begin sentinel"
    assert END in body, f"{path.name} has lost the shared-end sentinel"
    start = body.index(BEGIN) + len(BEGIN)
    end = body.index(END)
    assert end > start, f"{path.name}: the sentinels are in the wrong order"
    assert body[start:end].strip("\n") == SHARED.read_text(encoding="utf-8").strip(
        "\n"
    ), (
        f"{path.name}: the shared block has drifted from _shared.css — "
        "run scripts/sync-templates.py"
    )


def test_the_sync_script_agrees_that_nothing_is_stale():
    """The script's own --check, so the test and the fixer cannot disagree."""
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO / "scripts" / "sync-templates.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,  # the assertion below reports the script's own message
    )
    assert done.returncode == 0, done.stderr


def test_the_canvas_template_falls_back_to_plain_html():
    """HTML-in-Canvas (runcove-5t3oj.149) draws only where the browser has the API, and
    most readers, and an agent's own read_page, see the HTML. So the starter tests for
    the API before it moves anything into the canvas, keeps the card in normal flow
    until then, and reaches for nothing off the page."""
    body = _body(CANON / "canvas.html")
    visible = _strip_comments(body)
    check = '"drawElementImage" in CanvasRenderingContext2D.prototype'
    move = "canvas.appendChild(card)"
    assert check in visible and move in visible
    assert visible.index(check) < visible.index(move), (
        "the card moves into the canvas before the API is checked for"
    )
    assert re.search(
        r'<canvas[^>]*\blayoutsubtree\b[^>]*content="drawable"[^>]*\bhidden\b', body
    )
    after_canvas = body.split("</canvas>", 1)[1]
    assert 'id="drawn"' in after_canvas, (
        "the card must start outside the canvas, readable without the API"
    )
    drawn = re.search(r'<div\b[^>]*\bid="drawn"[^>]*>', body)
    assert drawn and re.search(r"\sdrawable[\s>]", drawn.group()), (
        "drawElementImage draws only a child marked drawable"
    )
    assert re.search(r"\.stage\[hidden\]\s*\{\s*display:\s*none", visible), (
        "the stage's display:block would show a blank band where the canvas is hidden"
    )
    assert re.search(r"catch\s*\(\w+\)\s*\{\s*fallBack\(\)", visible), (
        "a throw after the card has moved must put the plain HTML back"
    )
    assert "visibilityState" in visible, (
        "a tab opened in the background does not paint: the no-paint timer must "
        "run only while the page is visible"
    )
    off_page = re.findall(r"https?://|\b(?:src|href)=\"//|url\(", visible)
    assert not off_page, f"the starter reaches off the page: {off_page}"
    assert not re.search(r"\bnew\s+(?:Shared)?Worker\b|\bfetch\(|@import", visible)
