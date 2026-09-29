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
    assert passed >= 78, result.stdout


_NEWER = Path(__file__).parent / "js" / "newer.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime here")
def test_the_newer_version_offer_behaves_when_actually_run(tmp_path, monkeypatch):
    """homelab-ij8.74. What matters is what it must NOT do -- reload by itself,
    re-ask about a version he dismissed, or keep asking while nobody is
    looking -- and none of that can be read off the source text."""
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    node = shutil.which("node")
    script = tmp_path / "gallery.js"
    script.write_text(_GALLERY_JS)
    result = subprocess.run(  # noqa: S603
        [node, str(_NEWER), str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout
    passed = int(result.stdout.rsplit("\n", 2)[-2].split(" passed")[0].strip())
    assert passed >= 14, result.stdout


_RENEW = Path(__file__).parent / "js" / "renew.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime here")
def test_a_spent_frame_pass_is_renewed_when_actually_run(tmp_path, monkeypatch):
    """homelab-ij8.101. A frame's pass works once; a reload re-requests it. The
    gallery must fetch a fresh one, only for its own frame, only onto the same
    origin, and never in a loop."""
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    node = shutil.which("node")
    script = tmp_path / "gallery.js"
    script.write_text(_GALLERY_JS)
    result = subprocess.run(  # noqa: S603
        [node, str(_RENEW), str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout
    passed = int(result.stdout.rsplit("\n", 2)[-2].split(" passed")[0].strip())
    assert passed >= 7, result.stdout


_CODEBOXES = Path(__file__).parent / "js" / "codeboxes.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime here")
def test_the_code_boxes_behave_when_actually_run(tmp_path):
    """One box per character: advance, backspace, paste, and a full code
    continues once -- Jeremy's ask of 23 Sep 2026, run rather than read."""
    from artifacts.gallery import _CONNECT_JS

    script = tmp_path / "connect.js"
    script.write_text(_CONNECT_JS)
    result = subprocess.run(  # noqa: S603
        [shutil.which("node"), str(_CODEBOXES), str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout
    passed = int(result.stdout.rsplit("\n", 2)[-2].split(" passed")[0].strip())
    assert passed >= 19, result.stdout


_COMMENTSJS = Path(__file__).parent / "js" / "comments.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime here")
def test_the_comment_on_selection_button_behaves_when_actually_run(tmp_path):
    """homelab-ij8.18: select text, choose Comment/Remove this/Looks good
    (page review step 1, homelab-ij8.108), the right thing happens."""
    from artifacts.gallery import _COMMENTS_JS

    script = tmp_path / "comments.js"
    script.write_text(_COMMENTS_JS)
    result = subprocess.run(  # noqa: S603
        [shutil.which("node"), str(_COMMENTSJS), str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout
    passed = int(result.stdout.rsplit("\n", 2)[-2].split(" passed")[0].strip())
    assert passed >= 121, result.stdout
