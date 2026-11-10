"""No text the tools or the service ship may teach an install of this package that a public
package index can answer. The distribution name `ream` is an unrelated package on PyPI, so a
bare `uv tool install ream` (or pip/pipx) installs a stranger's package; the install name is
`ream-artifacts` now (docs/decisions.md, 10 Nov 2026), and a bare `ream-artifacts` is refused
too in what the service and tools ship: until the source is named in the line, nothing says
which index answers. Every such install line must name its source: the service's tools index
(`--no-index --find-links ...`) or a git address, so our agents track main.

Since `ream-artifacts` is ours on PyPI (docs/decisions.md, 10 Nov 2026, "Public docs teach the PyPI
install line"), the public docs (README.md and docs/*.md, which GitHub and PyPI show) may teach a
bare `ream-artifacts`. A bare `ream` or `artifacts` is refused everywhere."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_LINE = re.compile(r"(?:uv tool install|pipx install|pip install)[^\n\"'`]*")
_NAMES = re.compile(r"(?<![\w./+-])(?:ream-artifacts|ream|artifacts)(?![\w./-])")


def _bare_installs(text: str, *, pypi_ok: bool = False) -> list[str]:
    """Install lines naming this package with no source. With `pypi_ok`, a line whose only
    names are `ream-artifacts` passes: PyPI answers it with our package."""
    found = []
    for match in _LINE.finditer(text):
        line = match.group(0)
        names = set(_NAMES.findall(line))
        if not names:
            continue
        if "--no-index" in line or "git+" in line or "<path" in line:
            continue
        if pypi_ok and names == {"ream-artifacts"}:
            continue
        found.append(line.strip())
    return found


def test_the_detector_catches_a_bare_install_and_passes_a_sourced_one():
    assert _bare_installs("reinstall it (uv tool install ream --force, or ...)")
    assert _bare_installs("pipx install ream")
    assert not _bare_installs(
        "uv tool install --force --no-index --find-links https://x/api/v1/tools/ ream-artifacts"
    )
    assert not _bare_installs(
        "uv tool install --force git+https://forge/runcove/artifacts"
    )
    assert not _bare_installs(
        "uv tool install <path to the artifacts checkout> --force"
    )


def test_the_detector_refuses_a_bare_ream_artifacts_install_with_no_source():
    """The new install name is held to the same rule as the old one."""
    for line in (
        "uv tool install ream-artifacts",
        "run uv tool install --force ream-artifacts, then ...",
        "uv tool install --force --refresh ream-artifacts",
        "pipx install ream-artifacts",
        "pip install ream-artifacts",
        "pip install ream-artifacts[server]",
        "uv tool install ream-artifacts --force",
    ):
        assert _bare_installs(line), line
    # a source in the line is what makes it fine
    assert not _bare_installs(
        "uv tool install --force --refresh --no-index --find-links https://x/t/ ream-artifacts"
    )
    assert not _bare_installs("uv tool install --force git+https://f/x ream-artifacts")
    # a longer name is not this package
    assert not _bare_installs("uv tool install ream-artifacts-extras")


def test_public_docs_may_teach_the_pypi_install_name_but_not_the_old_names():
    assert not _bare_installs("uv tool install ream-artifacts", pypi_ok=True)
    assert not _bare_installs("pip install ream-artifacts[server]", pypi_ok=True)
    assert _bare_installs("uv tool install ream", pypi_ok=True)
    assert _bare_installs("pipx install artifacts", pypi_ok=True)
    assert _bare_installs("uv tool install ream-artifacts ream", pypi_ok=True)


def _shipped_files():
    """(path, pypi_ok): what the service and tools ship is strict; the public docs may name PyPI."""
    for path in sorted((ROOT / "src").rglob("*")):
        if path.is_file() and path.suffix in {".py", ".html", ".txt", ".css", ".svg"}:
            yield path, False
    yield ROOT / "README.md", True
    yield ROOT / "AGENTS.md", False
    for path in sorted((ROOT / "docs").glob("*.md")):
        yield path, True


def test_no_shipped_text_teaches_a_bare_install():
    offenders = []
    for path, pypi_ok in _shipped_files():
        for line in _bare_installs(path.read_text(encoding="utf-8"), pypi_ok=pypi_ok):
            offenders.append(f"{path.relative_to(ROOT)}: {line}")
    assert not offenders, "install lines with no source:\n" + "\n".join(offenders)


def test_no_document_the_service_serves_teaches_a_bare_install(tmp_path, monkeypatch):
    """The text as it is rendered, for each wheel an image may hold (`ream-artifacts` now,
    `ream` and `artifacts` for images built before a rename): the onboarding document, the
    skill and the agent card name their source on every install line, and say the install name
    the wheel has."""
    import json

    from ream import onboarding

    monkeypatch.setenv("REAM_TOOLS_DIR", str(tmp_path))
    base = "https://x.example"
    for wheel, package in (
        ("ream_artifacts-0.1.0.post20261110120000-py3-none-any.whl", "ream-artifacts"),
        ("ream-0.1.0-py3-none-any.whl", "ream"),
        ("artifacts-0.1.0-py3-none-any.whl", "artifacts"),
    ):
        for old in tmp_path.glob("*.whl"):
            old.unlink()
        (tmp_path / wheel).write_bytes(b"PK")
        texts = {
            "agents.txt": onboarding.agents_txt(base),
            "skill.md": onboarding.skill_md(base),
            "agent.json": json.dumps(onboarding.agent_card()),
        }
        for name, text in texts.items():
            assert not _bare_installs(text), (wheel, name, _bare_installs(text))
        line = (
            "uv tool install --force --no-index --find-links "
            f"{base}/api/v1/tools/ {package}"
        )
        assert line in texts["agents.txt"], wheel
