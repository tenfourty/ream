"""Page review step 1, Task 4: a selection offers three actions, not one, and
its anchor carries context either side (homelab-ij8.108). String-level checks
only -- the shim and the gallery script are run for real in
`tests/test_shim_js.py`/`tests/test_viewer_js.py` and their Task 6 successors."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from artifacts import content, gallery, thumbs


def test_the_selection_carries_context_either_side():
    source = content._viewer_report()
    assert "prefix:" in source and "suffix:" in source


def test_the_gallery_offers_three_actions_on_a_selection():
    js = gallery._COMMENTS_JS
    for label in ("Comment", "Remove this", "Looks good"):
        assert f'"{label}"' in js
    assert 'field("kind"' in js


def test_the_selection_bar_actually_hides():
    """Review fix, round 1: `.selbar{display:flex}` is author-origin CSS, so it
    always beats the user-agent stylesheet's `[hidden]{display:none}` --
    origin, not specificity, decides that fight. Without an explicit
    `[hidden]` rule of its own, `bar.hidden=true` is a no-op in a real
    browser and the bar never disappears; the fake-DOM node harness in
    tests/js/comments.test.mjs only reads the `hidden` property and applies
    no stylesheet at all, so it cannot catch this -- a string check here is
    the only thing that can."""
    assert ".selbar[hidden]{display:none}" in gallery._CSS


def test_the_shim_paints_through_css_highlights_and_never_the_dom():
    source = Path(content.__file__).read_text()
    assert "artifact-marks" in source and "CSS.highlights" in source
    assert "surroundContents" not in source and "insertAdjacentHTML" not in source


def test_the_shim_accepts_marks_only_from_the_gallery(monkeypatch):
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    source = content._viewer_report()
    marks_leg = source[source.index('"artifact-marks"') - 200 :]
    assert 'if(e.origin!=="https://artifacts.tenfourty.site")return;' in marks_leg
    assert '"artifact-marks-placed"' in source


def test_the_gallery_posts_marks_and_reports_what_could_not_be_placed():
    js = gallery._COMMENTS_JS
    assert '"artifact-marks"' in js and '"artifact-marks-placed"' in js
    assert "could not be placed on this version of the page" in js


_MARKS = Path(__file__).parent / "js" / "marks.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime here")
def test_the_shim_places_marks_when_actually_run(tmp_path, monkeypatch):
    """Run the served shim against a fake DOM: placed where the anchor says,
    found by search when the page moved, unplaced when ambiguous or gone,
    only from the gallery, never throwing, and nothing added but one style."""
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    served = content._viewer_report()
    body = served.removeprefix("<script>try{(function(){").removesuffix(
        "})()}catch(e){}</script>"
    )
    assert body != served, "the shim's wrapper is not the shape this test strips"
    script = tmp_path / "shim.js"
    script.write_text(body)
    result = subprocess.run(  # noqa: S603
        [shutil.which("node"), str(_MARKS), str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout
    passed = int(result.stdout.rsplit("\n", 2)[-2].split(" passed")[0].strip())
    assert passed >= 54, result.stdout


# --- Task 6: the same placement rules, proved in a real browser -------------
#
# `marks.test.mjs` above stands in a fake DOM for `search`'s TreeWalker and
# Range work. A fake DOM cannot tell us whether a real Range actually spans
# two elements, whether a real TreeWalker really skips a <script>, or whether
# CSS.highlights even exists in a shipped browser and actually paints -- only
# running the shim in one can. `window.__arPlace` is a test-only hook the shim
# exposes ONLY when `location.protocol==="file:"` -- see the comment above it
# in content.py -- because the real `artifact-marks` listener demands
# `e.origin` equal the gallery's origin, which a file:// page posting to
# itself can never satisfy.

_ARPLACE_CASES = """<html><head><title>t</title></head><body>
<div id="case1">
<p>Alpha bravo. <span id="c1-first">TargetPhraseOne</span> charlie delta echo.</p>
<p>Foxtrot golf. <span id="c1-second">TargetPhraseOne</span> hotel india juliet.</p>
</div>
<div id="case2">
<p>Zulu yankee. SameQuoteTwo xray whiskey.</p>
<p>Zulu yankee. SameQuoteTwo xray whiskey.</p>
</div>
<div id="case3">
<p>Kilo lima mike endsentence NovemberFoo</p>
<p>OscarBar continuesentence papa quebec.</p>
</div>
<div id="case4">
<p id="c4-wrong">WrongPathTextValue extra words here</p>
<p>Filler content. <span id="c4-real">RealQuoteFour</span> more filler.</p>
</div>
<script>
(function(){
  function closest(r, sel) {
    try {
      if (!r) return false;
      var n = r.startContainer;
      var el = n.nodeType === 1 ? n : n.parentElement;
      return !!(el && el.closest(sel));
    } catch (e) { return false; }
  }
  var results = [];

  // Case 1: the quote appears twice; prefix/suffix point at the second.
  var m1 = {id: "m1", kind: "comment", quote: "TargetPhraseOne",
            anchor: {path: "", prefix: "Foxtrot golf. ", suffix: " hotel india"}};
  var r1 = window.__arPlace(m1);
  results.push(r1 === null ? "unplaced"
    : closest(r1, "#c1-second") ? "second"
    : closest(r1, "#c1-first") ? "first" : "placed");

  // Case 2: the same quote twice, no context at all -- a tie, unplaced.
  var m2 = {id: "m2", kind: "comment", quote: "SameQuoteTwo", anchor: {path: ""}};
  var r2 = window.__arPlace(m2);
  results.push(r2 === null ? "unplaced" : "placed");

  // Case 3: a quote spanning two <p> elements, whitespace-free.
  var m3 = {id: "m3", kind: "comment", quote: "NovemberFoo OscarBar", anchor: {path: ""}};
  var r3 = window.__arPlace(m3);
  results.push(r3 === null ? "unplaced" : "placed");

  // Case 4: the stored path now points at other text; the quote is elsewhere.
  var m4 = {id: "m4", kind: "comment", quote: "RealQuoteFour",
            anchor: {path: "#c4-wrong", start: 0, end: 5}};
  var r4 = window.__arPlace(m4);
  results.push(r4 === null ? "unplaced"
    : closest(r4, "#c4-real") ? "followed"
    : closest(r4, "#c4-wrong") ? "path" : "placed");

  // Case 5: the quote is not on the page at all.
  var m5 = {id: "m5", kind: "comment", quote: "CompletelyAbsentQuoteFive", anchor: {path: ""}};
  var r5 = window.__arPlace(m5);
  results.push(r5 === null ? "unplaced" : "placed");

  // Bonus: CSS.highlights is a real thing here, and painting into it sticks.
  var hl = "no-highlights-api";
  try {
    if (window.CSS && CSS.highlights && window.Highlight && r3) {
      var h = new Highlight(r3);
      CSS.highlights.set("ar-comment", h);
      hl = (CSS.highlights.has("ar-comment") && CSS.highlights.get("ar-comment") === h)
        ? "highlights-ok" : "highlights-bad";
    }
  } catch (e) { hl = "highlights-threw"; }
  results.push(hl);

  document.title = results.join("|");
})();
</script>
</body></html>"""

needs_browser = pytest.mark.skipif(
    not thumbs.available(), reason="no headless browser in this environment"
)


@needs_browser
def test_placement_rules_in_a_real_browser(tmp_path, monkeypatch):
    """Runs the served page shim -- `content.SHIM + content._viewer_report()`,
    injected with `content.inject()`, exactly as `_serve` builds a live page --
    in a real headless chromium, and drives its placement logic through the
    file://-only `__arPlace` hook for five cases: a quote that appears twice,
    disambiguated by prefix/suffix, placed in the second; the same quote twice
    with no context, unplaced (a tie); a quote spanning two <p> elements,
    placed; a stored path now pointing at other text while the quote survives
    elsewhere, placed at the quote and not the path; and a quote nowhere on
    the page, unplaced. Also checks that CSS.highlights really exists here and
    a Range painted into it actually sticks.

    SKIPPED here, not failed, wherever there is no headless browser -- CI has
    none. It runs for real, and passes, on baba, where chromium is installed.
    """
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    body = "<!doctype html>" + _ARPLACE_CASES
    shim = content.SHIM + content._viewer_report()
    html = content.inject(body, shim)
    path = tmp_path / "p.html"
    path.write_text(html, encoding="utf-8")
    browser = thumbs.browser_path()
    assert browser is not None
    out = subprocess.run(  # noqa: S603
        [
            browser,
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--dump-dom",
            path.as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    ).stdout
    match = re.search(r"<title>(.*?)</title>", out)
    assert match, out
    assert match.group(1) == ("second|unplaced|placed|followed|unplaced|highlights-ok")
