"""The Dockerfile's two image targets (decisions.md, 22 Nov 2026): `app` carries no browser,
`full` does and is the last stage, so a plain `docker build .` still renders in-process.

Read from the file, not from a build (no image is built here); CI's `image` job proves the
built images the same way, by running them.
"""

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"

BROWSER = re.compile(r"chrom|firefox|webkit", re.IGNORECASE)


def _instructions() -> list[tuple[str, str]]:
    """(keyword, rest) for each instruction: comments dropped, `\\` continuations joined."""
    joined: list[str] = []
    pending = ""
    for raw in DOCKERFILE.read_text().splitlines():
        if not pending and (not raw.strip() or raw.lstrip().startswith("#")):
            continue
        line = pending + raw.strip()
        if line.endswith("\\"):
            pending = line[:-1].rstrip() + " "
            continue
        pending = ""
        joined.append(line)
    assert not pending
    out = []
    for line in joined:
        keyword, _, rest = line.partition(" ")
        out.append((keyword.upper(), rest.strip()))
    return out


def _stages() -> dict[str, dict]:
    """name -> {"from": the FROM's image or stage, "body": [(keyword, rest)...]}, in file order."""
    stages: dict[str, dict] = {}
    current = None
    for keyword, rest in _instructions():
        if keyword == "FROM":
            match = re.fullmatch(r"(\S+)\s+AS\s+(\S+)", rest, re.IGNORECASE)
            assert match, f"a FROM with no stage name: {rest!r}"
            current = match.group(2)
            assert current not in stages, f"stage {current} is defined twice"
            stages[current] = {"from": match.group(1), "body": []}
        elif current is not None:
            stages[current]["body"].append((keyword, rest))
    return stages


def _chain(name: str) -> list[str]:
    """The stages `name` is built from, itself first, by FROM and by COPY --from."""
    stages = _stages()
    seen: list[str] = []
    todo = [name]
    while todo:
        stage = todo.pop()
        if stage in seen:
            continue
        seen.append(stage)
        parents = [stages[stage]["from"]]
        for keyword, rest in stages[stage]["body"]:
            match = re.search(r"--from=(\S+)", rest) if keyword == "COPY" else None
            if match:
                parents.append(match.group(1))
        todo.extend(p for p in parents if p in stages)
    return seen


def _text(stage_names: list[str]) -> list[str]:
    stages = _stages()
    lines = []
    for name in stage_names:
        lines.append(f"FROM {stages[name]['from']}")
        lines.extend(f"{k} {r}" for k, r in stages[name]["body"])
    return lines


def _tini(stage_names: list[str]) -> tuple[bool, bool]:
    lines = _text(stage_names)
    installed = any("apt-get install" in line and "tini" in line for line in lines)
    entry = any(
        line.startswith("ENTRYPOINT") and "/usr/bin/tini" in line for line in lines
    )
    return installed, entry


def test_the_app_stage_names_no_browser_anywhere_in_its_chain():
    chain = _chain("app")
    assert "base" in chain and "build" in chain and "full" not in chain
    for line in _text(chain):
        assert not BROWSER.search(line), f"a browser in the app chain: {line}"


def test_the_checker_sees_the_browser_in_the_full_stage():
    # The positive control: a pattern that matched nothing would let the test above pass.
    assert any(BROWSER.search(line) for line in _text(_chain("full")))


def test_full_exists_is_last_and_installs_chromium():
    stages = _stages()
    assert "full" in stages
    assert list(stages)[-1] == "full"
    installs = [
        rest
        for keyword, rest in stages["full"]["body"]
        if keyword == "RUN" and "apt-get install" in rest
    ]
    assert len(installs) == 1 and "chromium" in installs[0]
    assert "fonts-dejavu-core" in installs[0]


def test_the_chromium_layer_comes_before_the_copies_in_full():
    body = _stages()["full"]["body"]
    run = next(i for i, (k, r) in enumerate(body) if k == "RUN" and "chromium" in r)
    copies = [i for i, (k, _) in enumerate(body) if k == "COPY"]
    assert copies and run < min(copies)


def test_both_images_carry_tini_as_pid_1():
    for name in ("app", "full"):
        assert _tini(_chain(name)) == (True, True), name


def test_both_images_end_as_the_unprivileged_user():
    for name in ("app", "full"):
        users = [
            rest
            for stage in reversed(_chain(name))
            for keyword, rest in _stages()[stage]["body"]
            if keyword == "USER"
        ]
        assert users and users[-1] == "10001:10001", name
    # full switches to root for its install and must switch back after it.
    body = _stages()["full"]["body"]
    users = [rest for keyword, rest in body if keyword == "USER"]
    assert users == ["root", "10001:10001"]


def test_both_images_carry_the_same_virtualenv_and_wheels():
    stages = _stages()
    copies = {
        name: [r for k, r in stages[name]["body"] if k == "COPY"]
        for name in ("app", "full")
    }
    assert copies["app"] == copies["full"]
    assert any("/app/.venv" in c for c in copies["app"])
    assert any("/app/dist" in c for c in copies["app"])
