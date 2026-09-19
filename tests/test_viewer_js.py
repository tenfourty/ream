"""Execute the gallery's browser script, rather than asserting on its text.

The script is served to a browser and never runs under pytest, so every other
test of it can only assert that certain bytes appear in the response. That
catches a missing line and nothing else: it cannot tell an origin check that
runs from one that is written down, nor `!==` from `===`. This runs the real
script in node against a small fake DOM and asserts on what it does to that
DOM -- the frame it sizes, the element it moves, the message it refuses.

Skipped, not failed, where node is absent: it is a real gap in that
environment, but a missing runtime is not a defect in this app.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from artifacts.gallery import _GALLERY_JS

_HARNESS = Path(__file__).parent / "js" / "viewer.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime here")
def test_the_gallery_script_behaves_when_actually_run(tmp_path):
    node = shutil.which("node")
    # Written from the module the app serves, never from a checked-in copy: a
    # copy is a second source of truth that silently stops matching.
    script = tmp_path / "gallery.js"
    script.write_text(_GALLERY_JS)
    # Resolved absolute path, and both arguments are paths this test just built
    # itself. `check` is deliberately off: a non-zero exit is the harness
    # reporting a real failure, and its stdout names WHICH case, which a
    # CalledProcessError would throw away.
    result = subprocess.run(  # noqa: S603
        [node, str(_HARNESS), str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # The count is asserted, not just the exit code: a harness that silently
    # stopped running its cases would still exit 0 with nothing to show.
    assert "0 failed" in result.stdout
    passed = int(result.stdout.rsplit("\n", 2)[-2].split(" passed")[0].strip())
    assert passed >= 35, result.stdout
