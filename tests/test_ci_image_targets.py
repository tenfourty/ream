"""Which Dockerfile target each image of the `image` job is built from (decisions.md, 22 Nov 2026):
the core image (`artifacts-core`, the one the deployed service is built on) is the `app` target,
which has no browser; `artifacts` and the renderer image (`artifacts-renderer`) are the `full`
target; and a step runs the core image that was pushed and asserts it finds no browser. The same
holds for `.forgejo/workflows/sec.yml`'s private core (no renderer is built there).

Read from the workflows as text, comment lines dropped (a workflow cannot be run here); CI's own
run of the check step is the proof on the built images. The Dockerfile's side is
`tests/test_dockerfile_targets.py`.
"""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".forgejo" / "workflows"
CI = WORKFLOWS / "ci.yml"
SEC = WORKFLOWS / "sec.yml"


def _steps(path: Path, job: str, next_job: str | None) -> dict[str, str]:
    """step name -> its text without comment lines, for the steps of `job`."""
    text = path.read_text()
    body = text.split(f"\n  {job}:\n", 1)[1]
    if next_job:
        body = body.split(f"\n  {next_job}:\n", 1)[0]
    steps: dict[str, str] = {}
    for chunk in body.split("\n      - ")[1:]:
        match = re.match(r"name: (.+)", chunk)
        if match:
            code = [ln for ln in chunk.splitlines() if not ln.lstrip().startswith("#")]
            steps[match.group(1).strip()] = "\n".join(code)
    return steps


def _image_steps() -> dict[str, str]:
    return _steps(CI, "image", "pin")


def _sec_image_steps() -> dict[str, str]:
    return _steps(SEC, "image", None)


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


def test_sec_builds_its_private_core_from_the_app_target_and_no_renderer():
    steps = _sec_image_steps()
    step = steps["build and push the core image"]
    assert "docker build" in step and "--target app" in step
    assert "--target full" not in step and "CHROMIUM_REFRESH" not in step
    # no renderer image is built or pushed by this pipeline, so none is named
    assert not any("renderer" in code for code in steps.values())


def test_sec_runs_the_pushed_private_core_and_proves_it_has_no_browser():
    step = _sec_image_steps()["core image has no browser"]
    assert "steps.core.outputs.ref" in step
    assert 'docker pull "$CORE_REF"' in step
    assert re.search(
        r'docker run --rm --entrypoint \S+ "\$CORE_REF" -c "\$check" core', step
    )
    # the positive control is a full image built here and never pushed
    assert "--target full" in step and "docker push" not in step
    assert re.search(
        r'docker run --rm --entrypoint \S+ "\$full" -c "\$check" full', step
    )
    assert '[ "$core_id" != "$full_id" ]' in step
    assert "carries a browser" in step and "carries no browser" in step


def test_sec_proves_no_browser_before_it_builds_the_deployed_image():
    names = list(_sec_image_steps())
    assert names.index("core image has no browser") < names.index(
        "build and push the deployed image"
    )


def test_the_ci_check_step_cannot_be_skipped_or_ignored():
    steps = _image_steps()
    step = steps["core image has no browser"]
    assert "continue-on-error" not in step
    assert not re.search(r"^\s*if:", step, re.MULTILINE)
    names = list(steps)
    assert names.index("core image has no browser") > names.index("push core image")
    # the fallback for a re-run names the core repository, not another image
    assert "git.tenfourty.site/runcove/artifacts-core:${sha12}" in step
    # a fresh run with no core ref fails here, even if the push itself went through
    assert '[ -z "$CORE_REF" ] && [ "$BUILD_REUSED" = 0 ]' in step


def test_the_browser_probe_exits_nonzero_explicitly_instead_of_asserting():
    # `python -O` drops asserts; the probe must fail with sys.exit(1) and a message
    for step in (
        _image_steps()["core image has no browser"],
        _sec_image_steps()["core image has no browser"],
    ):
        assert "assert " not in step
        assert "sys.exit(1)" in step
