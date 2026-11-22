"""Which Dockerfile target each image of the `image` job comes from (decisions.md, 22 Nov 2026):
the core image (`artifacts-core`, the one the deployed service is built on) is the `app` target,
which has no browser; `artifacts` and the renderer image (`artifacts-renderer`) are the `full`
target; and the last step runs the core image that was pushed and asserts it finds no browser.

Read from `.forgejo/workflows/ci.yml` as text, comment lines dropped (a workflow cannot be run
here); CI's own run of the last step is the proof on the built images. The Dockerfile's side is
`tests/test_dockerfile_targets.py`.
"""

import re
from pathlib import Path

CI = Path(__file__).resolve().parent.parent / ".forgejo" / "workflows" / "ci.yml"


def _image_steps() -> dict[str, str]:
    """step name -> its text without comment lines, for the steps of the `image` job."""
    text = CI.read_text()
    job = text.split("\n  image:\n", 1)[1].split("\n  pin:\n", 1)[0]
    steps: dict[str, str] = {}
    for chunk in job.split("\n      - ")[1:]:
        match = re.match(r"name: (.+)", chunk)
        if match:
            code = [ln for ln in chunk.splitlines() if not ln.lstrip().startswith("#")]
            steps[match.group(1).strip()] = "\n".join(code)
    return steps


def test_the_steps_this_file_reads_exist():
    assert {
        "build and push",
        "push core image",
        "push renderer image",
        "core image has no browser",
    } <= set(_image_steps())


def test_artifacts_is_built_from_the_full_target():
    step = _image_steps()["build and push"]
    assert "--target full" in step and "--target app" not in step


def test_the_core_image_is_built_from_the_app_target_and_never_the_full_image_retagged():
    step = _image_steps()["push core image"]
    assert "docker build" in step and "--target app" in step
    assert "--target full" not in step
    assert 'docker tag "$BUILD_REF"' not in step
    # the core digest must differ from the full image's, and the step says so by failing
    assert '[ "$digest" != "${BUILD_REF##*@}" ]' in step


def test_the_renderer_image_is_the_full_image_and_is_not_rebuilt_as_app():
    step = _image_steps()["push renderer image"]
    assert 'docker tag "$BUILD_REF" "$renderer"' in step
    assert "--target app" not in step
    assert '[ "$digest" = "${BUILD_REF##*@}" ]' in step


def test_the_browser_check_runs_the_pushed_core_image_not_a_build_of_its_own():
    step = _image_steps()["core image has no browser"]
    assert "steps.core.outputs.ref" in step
    assert "docker build" not in step
    assert 'docker pull "$core"' in step
    assert re.search(
        r'docker run --rm --entrypoint \S+ "\$core" -c "\$check" core', step
    )
    # the positive control: the full image must find a browser, so an empty result cannot pass
    assert re.search(
        r'docker run --rm --entrypoint \S+ "\$full" -c "\$check" full', step
    )
    assert "carries a browser" in step and "carries no browser" in step
