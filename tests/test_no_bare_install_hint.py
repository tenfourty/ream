"""No text the tools or the service ship may teach an install of `ream` that a public package
index can answer. The distribution name `ream` is also an unrelated package on PyPI, so a bare
`uv tool install ream` (or pip/pipx) installs a stranger's package. Every install line must
name its source: the service's tools index (`--no-index --find-links ...`) or a git address."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_LINE = re.compile(r"(?:uv tool install|pipx install|pip install)[^\n\"'`]*")
_NAMES = re.compile(r"(?<![\w./+-])(?:ream|artifacts)(?![\w./-])")


def _bare_installs(text: str) -> list[str]:
    found = []
    for match in _LINE.finditer(text):
        line = match.group(0)
        if not _NAMES.search(line):
            continue
        if "--no-index" in line or "git+" in line or "<path" in line:
            continue
        found.append(line.strip())
    return found


def test_the_detector_catches_a_bare_install_and_passes_a_sourced_one():
    assert _bare_installs("reinstall it (uv tool install ream --force, or ...)")
    assert _bare_installs("pipx install ream")
    assert not _bare_installs(
        "uv tool install --force --no-index --find-links https://x/api/v1/tools/ ream"
    )
    assert not _bare_installs(
        "uv tool install --force git+https://forge/runcove/artifacts"
    )
    assert not _bare_installs(
        "uv tool install <path to the artifacts checkout> --force"
    )


def test_no_shipped_text_teaches_a_bare_install():
    offenders = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        for line in _bare_installs(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(ROOT)}: {line}")
    assert not offenders, "install lines with no source:\n" + "\n".join(offenders)
