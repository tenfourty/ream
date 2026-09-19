"""Execute the page-side shim, rather than asserting on its text.

The sibling of `test_viewer_js.py`, for the other end of the same
conversation: the gallery lifts a decision control out of the frame and posts
back how tall it turned out, and this shim reserves that room in the page. A
served-bytes assertion cannot tell whether the origin check on that return leg
runs, which is the one thing about it that matters.

Skipped, not failed, where node is absent.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from artifacts.content import _viewer_report

_HARNESS = Path(__file__).parent / "js" / "shim.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime here")
def test_the_page_shim_behaves_when_actually_run(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    node = shutil.which("node")
    # The served markup, with its wrapper stripped back to the body node runs.
    # Taking it from the app rather than a checked-in copy keeps one source of
    # truth; stripping here rather than in the harness keeps the harness about
    # behaviour.
    served = _viewer_report()
    body = served.removeprefix("<script>try{(function(){").removesuffix(
        "})()}catch(e){}</script>"
    )
    assert body != served, "the shim's wrapper is not the shape this test strips"
    script = tmp_path / "shim.js"
    script.write_text(body)
    result = subprocess.run(  # noqa: S603
        [node, str(_HARNESS), str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout
    passed = int(result.stdout.rsplit("\n", 2)[-2].split(" passed")[0].strip())
    assert passed >= 26, result.stdout
