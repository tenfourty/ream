"""Execute the page-side form listener (content.py FORM_JS) against a fake DOM, rather
than asserting on its text. Skipped, not failed, where node is absent."""

import shutil
import subprocess
from pathlib import Path

import pytest

from artifacts.content import _viewer_report

_HARNESS = Path(__file__).parent / "js" / "forms.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime here")
def test_the_form_listener_behaves_when_actually_run(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    served = _viewer_report()
    body = served.removeprefix("<script>try{(function(){").removesuffix(
        "})()}catch(e){}</script>"
    )
    assert body != served, "the shim's wrapper is not the shape this test strips"
    script = tmp_path / "shim.js"
    script.write_text(body)
    result = subprocess.run(  # noqa: S603
        [shutil.which("node"), str(_HARNESS), str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout
    passed = int(result.stdout.rsplit("\n", 2)[-2].split(" passed")[0].strip())
    assert passed >= 32, result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime here")
def test_the_gallerys_submit_bar_behaves_when_actually_run(tmp_path):
    from artifacts.forms_ui import FORMS_JS

    script = tmp_path / "forms.js"
    script.write_text(FORMS_JS)
    result = subprocess.run(  # noqa: S603
        [
            shutil.which("node"),
            str(Path(__file__).parent / "js" / "formsbar.test.mjs"),
            str(script),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout
    passed = int(result.stdout.rsplit("\n", 2)[-2].split(" passed")[0].strip())
    assert passed >= 24, result.stdout
