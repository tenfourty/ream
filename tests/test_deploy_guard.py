"""`.forgejo/scripts/deploy-guard.sh`: the "never deploy an older build over a
newer one" check `ci.yml`'s `bump_and_push` runs after every
`git switch -C main origin/main` -- including the one retry, which is the
whole reason this lives in a script rather than inline where the retry could
skip it again. Exercised here against a real throwaway git repo, not mocked,
because the bug being guarded against (a `git log` error read as "nothing
newer") only shows up against real git plumbing.
"""

import shutil
import subprocess
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[1] / ".forgejo" / "scripts" / "deploy-guard.sh"
)
# Resolved once to full paths -- bandit's S607 objects to a bare "git"/"bash"
# (a $PATH lookup an attacker could redirect); every argument list below is
# otherwise fixed by this file, never built from external input (S603).
_GIT = shutil.which("git")
_BASH = shutil.which("bash")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        [_GIT, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> str:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ci@git.tenfourty.site")
    _git(repo, "config", "user.name", "artifacts-ci")
    (repo / "f").write_text("base\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "chore: base")
    return _git(repo, "rev-parse", "HEAD")


def _commit(repo: Path, message: str) -> str:
    (repo / "f").write_text(message)
    _git(repo, "commit", "-q", "-a", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _run_guard(repo: Path, base: str, ref: str = "HEAD") -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [_BASH, str(_SCRIPT), base, ref],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_newer_non_bump_commit_stands_down(tmp_path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _commit(repo, "fix: something unrelated")

    result = _run_guard(repo, base)

    assert result.returncode == 1
    assert "newer" in result.stderr.lower()


def test_bump_only_ahead_proceeds(tmp_path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _commit(repo, "deploy: artifacts abcdef012345 [skip ci]")

    result = _run_guard(repo, base)

    assert result.returncode == 0
    assert result.stderr == ""


def test_nothing_ahead_at_all_proceeds(tmp_path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)

    result = _run_guard(repo, base)

    assert result.returncode == 0


def test_a_skip_ci_subject_that_is_not_the_exact_bump_format_still_counts_as_newer(
    tmp_path,
):
    # A `[skip ci]` SUBSTRING anywhere in the subject is not enough to call a
    # commit "our own bump" -- only the exact `deploy: artifacts <12 hex>
    # [skip ci]` subject does. A looser substring check would let a human
    # commit that merely happens to carry `[skip ci]` in its subject (to skip
    # ITS OWN run) be silently treated as if it were this workflow's bump,
    # and an older run would then overwrite it.
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _commit(repo, "chore: unrelated cleanup [skip ci]")

    result = _run_guard(repo, base)

    assert result.returncode == 1


def test_an_unknown_base_revision_fails_loudly_instead_of_reading_as_nothing_newer(
    tmp_path,
):
    repo = tmp_path / "repo"
    _init_repo(repo)

    result = _run_guard(repo, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")

    assert result.returncode == 2
    assert result.stderr != ""
