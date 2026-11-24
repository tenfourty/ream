"""Which Dockerfile target each image of the `image` job is built from (decisions.md, 22 Nov 2026):
the core image (`artifacts-core`, the one the deployed service is built on) is the `app` target,
which has no browser; `artifacts` and the renderer image (`artifacts-renderer`) are the `full`
target; and a step runs the core image that was pushed and asserts it finds no browser. The same
holds for `.forgejo/workflows/sec.yml`'s private core (no renderer is built there).

Read from the workflows as text, comment lines dropped (a workflow cannot be run here); CI's own
run of the check step is the proof on the built images. The Dockerfile's side is
`tests/test_dockerfile_targets.py`.
"""

import os
import re
import subprocess
import textwrap
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
        "push Ream-named images",
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


def test_every_image_is_also_pushed_under_its_ream_name_with_the_same_digest():
    step = _image_steps()["push Ream-named images"]
    # one push per old name, tagged from the ref the old step output (an exact digest)
    assert 'docker tag "$old_ref" "$new"' in step and 'docker push "$new"' in step
    assert "docker build" not in step
    # the digest is read back from the registry and must equal the old name's, or the job fails
    assert "RepoDigests" in step
    assert '[ "$new_digest" = "$want" ] ||' in step and "exit 1" in step
    assert 'want="${old_ref##*@}"' in step
    # core still never has the full image's digest, under the Ream names either
    assert '[ "$new_digest" != "$full_digest" ]' in step
    # the old names' labels carry the new path, with nothing after the digest (the pin lander's shape)
    assert 'publish artifacts "$BUILD_REF" ream "PUBLISHED"\n' in step
    assert 'publish artifacts-core "$CORE_REF" ream-core "PUBLISHED core"\n' in step
    assert (
        'publish artifacts-renderer "$RENDERER_REF" ream-renderer "PUBLISHED renderer"\n'
        in step
    )
    assert 'echo "${label} image=${new} digest=${new_digest}"' in step
    # and the equality proof is a line of its own
    assert 'echo "SAME-DIGEST ${new_name}=${new_digest} ${old_name}=${want}"' in step


def test_the_ream_name_step_runs_after_the_old_pushes_and_cannot_be_ignored():
    steps = _image_steps()
    step = steps["push Ream-named images"]
    assert "continue-on-error" not in step
    assert not re.search(r"^\s*if:", step, re.MULTILINE)
    names = list(steps)
    assert names.index("push Ream-named images") > names.index("push renderer image")
    assert names.index("push Ream-named images") < names.index(
        "core image has no browser"
    )
    # a re-run (nothing built by this run) pushes nothing, as for the old names
    assert '[ "$BUILD_REUSED" != 0 ]' in step
    # a fresh run with no core ref fails
    assert '[ -z "$CORE_REF" ]' in step


def test_the_job_outputs_still_name_the_old_images_only():
    job = CI.read_text().split("\n  image:\n", 1)[1].split("\n    steps:\n", 1)[0]
    assert "ref: ${{ steps.build.outputs.ref }}" in job
    assert "core_ref: ${{ steps.core.outputs.ref }}" in job
    assert "renderer_ref: ${{ steps.renderer.outputs.ref }}" in job
    assert "ream" not in "\n".join(
        ln for ln in job.splitlines() if "outputs" in ln and "steps." in ln
    )


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


def test_sec_proves_public_main_is_in_the_commit_before_it_builds_and_again_before_the_line():
    steps = _sec_image_steps()
    names = list(steps)
    check = "prove public main is in this commit"
    assert ".forgejo/scripts/sec-public-main-check.sh" in steps[check]
    # before anything is built or pushed
    assert names.index(check) < names.index("build and push the core image")
    # the last step runs it again, before it echoes the SEC BUILT line (the only place it appears)
    last = steps[names[-1]]
    assert "SEC BUILT image=" in last
    assert last.index("sec-public-main-check.sh") < last.index("SEC BUILT image=")
    assert sum("SEC BUILT image=" in s for s in steps.values()) == 1
    # the ancestry needs the whole history, and the check can be neither skipped nor allowed to fail
    image = SEC.read_text().split("\n  image:\n", 1)[1]
    checkout = image.split("\n      - ")[1]  # the first step, which has no name
    code = "\n".join(
        ln for ln in checkout.splitlines() if not ln.lstrip().startswith("#")
    )
    assert code.startswith("uses: actions/checkout@") and re.search(
        r"^\s+fetch-depth: 0\b", code, re.MULTILINE
    )
    for text in (steps[check], last):
        assert "SEC_PUBLIC_READ_TOKEN: ${{ secrets.SEC_PUBLIC_READ_TOKEN }}" in text
        assert "continue-on-error" not in text and "\n        if:" not in text


# --- the Ream-name step, run against a fake docker ---------------------------------------------

SHA = "0123456789abcdef0123456789abcdef01234567"
SHA12 = SHA[:12]
REG = "git.tenfourty.site/runcove"
D_FULL, D_CORE, D_OTHER = ("sha256:" + c * 64 for c in "abc")

FAKE_DOCKER = """#!/bin/bash
# state: $FAKE/exists/<repo name> (a tag that is in the registry), $FAKE/digest/<repo name>
last="${@: -1}"
name="${last%%:*}"; name="${name##*/}"
case "$1" in
  login|tag|push) exit 0 ;;
  pull)
    if [ -e "$FAKE/exists/$name" ]; then exit 0; fi
    echo "Error: manifest unknown"; exit 1 ;;
  inspect)
    if [ -s "$FAKE/digest/$name" ]; then echo "git.tenfourty.site/runcove/${name}@$(cat "$FAKE/digest/$name")"; fi ;;
esac
"""


def _ream_step_script() -> str:
    step = _image_steps()["push Ream-named images"]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


def _run_ream_step(
    tmp_path: Path,
    new_digests: dict[str, str],
    existing: tuple[str, ...] = (),
    core_ref: str | None = None,
):
    (tmp_path / "bin").mkdir()
    docker = tmp_path / "bin" / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    (tmp_path / "exists").mkdir()
    (tmp_path / "digest").mkdir()
    for name, digest in new_digests.items():
        (tmp_path / "digest" / name).write_text(digest)
    for name in existing:
        (tmp_path / "exists" / name).write_text("")
    script = tmp_path / "step.sh"
    script.write_text("#!/bin/bash\n" + _ream_step_script())
    env = {
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "FAKE": str(tmp_path),
        "GITHUB_SHA": SHA,
        "REGISTRY_PASSWORD": "not-a-secret",
        "BUILD_REUSED": "0",
        "BUILD_REF": f"{REG}/artifacts:{SHA12}@{D_FULL}",
        "CORE_REF": core_ref or f"{REG}/artifacts-core:{SHA12}@{D_CORE}",
        "RENDERER_REF": f"{REG}/artifacts-renderer:{SHA12}@{D_FULL}",
    }
    return subprocess.run(  # noqa: S603 -- bash on a script this test wrote
        ["/bin/bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _lander_matches(log: str, label: str, name: str) -> list[str]:
    """The pin lander's pattern (homelab qd-pin-lander.py), bound to one registry name."""
    pattern = re.compile(
        r"(?:^|\s)" + re.escape(label) + " image=" + re.escape(f"{REG}/{name}")
        + r":([0-9a-f]{12}) digest=(sha256:[0-9a-f]{64})\s*$"
    )  # fmt: skip
    return [ln for ln in log.splitlines() if pattern.search(ln)]


GOOD = {"ream": D_FULL, "ream-core": D_CORE, "ream-renderer": D_FULL}
LABELS = (
    ("PUBLISHED", "ream"),
    ("PUBLISHED core", "ream-core"),
    ("PUBLISHED renderer", "ream-renderer"),
)


def test_ream_step_publishes_one_lander_line_per_name_and_proves_the_digests(tmp_path):
    done = _run_ream_step(tmp_path, GOOD)
    assert done.returncode == 0, done.stderr
    for label, name in LABELS:
        assert len(_lander_matches(done.stdout, label, name)) == 1, (label, name)
    # no Ream line is matched by the lander's pattern for another name, and none by the old paths
    for label, name in LABELS:
        old = name.replace("ream", "artifacts", 1)
        assert not _lander_matches(done.stdout, label, old)
    assert (
        f"SAME-DIGEST ream-renderer={D_FULL} artifacts-renderer={D_FULL}" in done.stdout
    )


def test_ream_step_fails_when_a_pushed_digest_differs_from_the_old_names(tmp_path):
    done = _run_ream_step(tmp_path, {**GOOD, "ream-renderer": D_OTHER})
    assert done.returncode != 0
    assert "ream-renderer" in done.stderr and "PUBLISHED renderer" not in done.stdout


def test_ream_step_fails_when_the_core_digest_equals_the_full_digest(tmp_path):
    done = _run_ream_step(
        tmp_path,
        {**GOOD, "ream-core": D_FULL},
        core_ref=f"{REG}/artifacts-core:{SHA12}@{D_FULL}",
    )
    assert done.returncode != 0 and "must be the app target" in done.stderr


def test_ream_step_reuses_an_existing_tag_with_the_same_digest(tmp_path):
    done = _run_ream_step(tmp_path, GOOD, existing=("ream-core",))
    assert done.returncode == 0, done.stderr
    assert (
        f"REUSED ream-core image={REG}/ream-core:{SHA12} digest={D_CORE}" in done.stdout
    )
    # the reused tag is not certified: no PUBLISHED line for it, the other two still have one
    assert not _lander_matches(done.stdout, "PUBLISHED core", "ream-core")
    assert len(_lander_matches(done.stdout, "PUBLISHED", "ream")) == 1
    assert len(_lander_matches(done.stdout, "PUBLISHED renderer", "ream-renderer")) == 1


def test_ream_step_fails_on_an_existing_tag_with_another_digest(tmp_path):
    done = _run_ream_step(
        tmp_path, {**GOOD, "ream-renderer": D_OTHER}, existing=("ream-renderer",)
    )
    assert done.returncode != 0
    assert "existing" in done.stderr and "PUBLISHED renderer" not in done.stdout
