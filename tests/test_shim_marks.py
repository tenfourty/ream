"""Page review step 1, Task 4: a selection offers three actions, not one, and
its anchor carries context either side (homelab-ij8.108). String-level checks
only -- the shim and the gallery script are run for real in
`tests/test_shim_js.py`/`tests/test_viewer_js.py` and their Task 6 successors."""

import shutil
import subprocess
from pathlib import Path

import pytest

from artifacts import content, gallery


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
    assert passed >= 41, result.stdout
