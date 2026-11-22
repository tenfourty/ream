"""The Dockerfile's two image targets (decisions.md, 22 Nov 2026): `app` carries no browser,
`full` does and is the last stage, so a plain `docker build .` still renders in-process.

Read from the file, not from a build (no image is built here); CI's `image` job proves the
built images the same way, by running them. The helpers take the Dockerfile's text, so the
rules below are also proved against small made-up Dockerfiles.
"""

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"

BROWSER = re.compile(r"chrom|firefox|webkit", re.IGNORECASE)


def _read() -> str:
    return DOCKERFILE.read_text()


def _instructions(text: str) -> list[tuple[str, str]]:
    """(keyword, rest) for each instruction: comment and blank lines dropped (inside a `\\`
    continuation too, as Docker drops them), continuations joined."""
    joined: list[str] = []
    pending = ""
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
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


def _parse(text: str) -> tuple[list[str], dict[str, dict]]:
    """(the global instructions before the first FROM, as lines; name -> {"from": the FROM's
    image or stage, "body": [(keyword, rest)...]} in file order)."""
    global_lines: list[str] = []
    stages: dict[str, dict] = {}
    current = None
    for keyword, rest in _instructions(text):
        if keyword == "FROM":
            match = re.fullmatch(r"(\S+)\s+AS\s+(\S+)", rest, re.IGNORECASE)
            assert match, f"a FROM with no stage name: {rest!r}"
            current = match.group(2)
            assert current not in stages, f"stage {current} is defined twice"
            stages[current] = {"from": match.group(1), "body": []}
        elif current is None:
            global_lines.append(f"{keyword} {rest}")
        else:
            stages[current]["body"].append((keyword, rest))
    return global_lines, stages


def _chain(stages: dict[str, dict], name: str, copies: bool) -> list[str]:
    """The stages `name` is built on, itself first: by FROM, and with `copies` also by
    COPY --from (what its files come from)."""
    seen: list[str] = []
    todo = [name]
    while todo:
        stage = todo.pop()
        if stage in seen:
            continue
        seen.append(stage)
        parents = [stages[stage]["from"]]
        if copies:
            for keyword, rest in stages[stage]["body"]:
                match = re.search(r"--from=(\S+)", rest) if keyword == "COPY" else None
                if match:
                    parents.append(match.group(1))
        todo.extend(p for p in parents if p in stages)
    return seen


def _text(stages: dict[str, dict], names: list[str]) -> list[str]:
    lines = []
    for name in names:
        lines.append(f"FROM {stages[name]['from']}")
        lines.extend(f"{k} {r}" for k, r in stages[name]["body"])
    return lines


def _browser_lines(text: str, name: str) -> list[str]:
    """Every instruction that names a browser in `name`'s whole chain (FROM and COPY --from
    sources, and the global ARGs). Fail-closed: a variable in a FROM of the chain is refused, since
    its value is not on the page."""
    global_lines, stages = _parse(text)
    chain = _chain(stages, name, copies=True)
    for stage in chain:
        assert "$" not in stages[stage]["from"], (
            f"a variable in the FROM of {stage}: {stages[stage]['from']}"
        )
    lines = global_lines + _text(stages, chain)
    return [line for line in lines if BROWSER.search(line)]


def _tini(text: str, name: str) -> tuple[bool, bool]:
    """(installed, is the entrypoint) over `name`'s FROM chain only: a stage whose files are
    merely copied in (`build`) does not make the image run tini."""
    _, stages = _parse(text)
    lines = _text(stages, _chain(stages, name, copies=False))
    installed = any("apt-get install" in line and "tini" in line for line in lines)
    entry = any(
        line.startswith("ENTRYPOINT") and "/usr/bin/tini" in line for line in lines
    )
    return installed, entry


def _users(text: str, name: str) -> list[str]:
    """The USER instructions of `name`'s FROM chain, base first, in the order they run."""
    _, stages = _parse(text)
    return [
        rest
        for stage in reversed(_chain(stages, name, copies=False))
        for keyword, rest in stages[stage]["body"]
        if keyword == "USER"
    ]


def test_the_app_stage_names_no_browser_anywhere_in_its_chain():
    text = _read()
    _, stages = _parse(text)
    chain = _chain(stages, "app", copies=True)
    assert "base" in chain and "build" in chain and "full" not in chain
    assert _browser_lines(text, "app") == []


def test_the_checker_sees_the_browser_in_the_full_stage():
    # The positive control: a pattern that matched nothing would let the test above pass.
    assert _browser_lines(_read(), "full")


def test_a_comment_inside_a_continuation_cannot_stand_for_the_browser_install():
    text = (
        "FROM x AS full\n"
        "RUN apt-get install -y --no-install-recommends tini \\\n"
        " # chromium\n"
        "\n"
        " && true\n"
    )
    _, stages = _parse(text)
    assert _text(stages, ["full"])[1] == (
        "RUN apt-get install -y --no-install-recommends tini && true"
    )
    assert _browser_lines(text, "full") == []


def test_a_global_arg_naming_a_browser_is_scanned():
    text = "ARG PKG=chromium\nFROM python:3.12 AS base\nFROM base AS app\n"
    assert _browser_lines(text, "app") == ["ARG PKG=chromium"]


def test_a_variable_in_a_from_of_the_app_chain_is_refused():
    text = "ARG IMG=python:3.12\nFROM ${IMG} AS base\nFROM base AS app\n"
    try:
        _browser_lines(text, "app")
    except AssertionError as refused:
        assert "variable in the FROM of base" in str(refused)
    else:
        raise AssertionError("a FROM with a variable was accepted")


def test_full_exists_is_last_and_installs_chromium():
    _, stages = _parse(_read())
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
    body = _parse(_read())[1]["full"]["body"]
    run = next(i for i, (k, r) in enumerate(body) if k == "RUN" and "chromium" in r)
    copies = [i for i, (k, _) in enumerate(body) if k == "COPY"]
    assert copies and run < min(copies)


def test_both_images_carry_tini_as_pid_1():
    for name in ("app", "full"):
        assert _tini(_read(), name) == (True, True), name


def test_a_tini_or_user_in_build_does_not_count_for_app():
    text = (
        "FROM python:3.12 AS build\n"
        "RUN apt-get install -y tini\n"
        "USER 10001:10001\n"
        'ENTRYPOINT ["/usr/bin/tini", "--"]\n'
        "FROM python:3.12 AS base\n"
        "FROM base AS app\n"
        "COPY --from=build /app /app\n"
    )
    assert _tini(text, "app") == (False, False)
    assert _users(text, "app") == []
    # ...and the same lines in the stage it is built on do count.
    on_base = text.replace("FROM base AS app", "FROM build AS app")
    assert _tini(on_base, "app") == (True, True)
    assert _users(on_base, "app") == ["10001:10001"]


def test_both_images_end_as_the_unprivileged_user():
    text = _read()
    for name in ("app", "full"):
        users = _users(text, name)
        assert users and users[-1] == "10001:10001", name
    # full switches to root for its install and must switch back after it.
    body = _parse(text)[1]["full"]["body"]
    assert [rest for keyword, rest in body if keyword == "USER"] == [
        "root",
        "10001:10001",
    ]


def test_both_images_carry_the_same_virtualenv_and_wheels():
    _, stages = _parse(_read())
    copies = {
        name: [r for k, r in stages[name]["body"] if k == "COPY"]
        for name in ("app", "full")
    }
    assert copies["app"] == copies["full"]
    assert any("/app/.venv" in c for c in copies["app"])
    assert any("/app/dist" in c for c in copies["app"])


def test_the_base_layer_is_refreshed_weekly_without_naming_a_browser():
    # tini is installed in base; its apt layer must change with the week's value too.
    _, stages = _parse(_read())
    body = stages["base"]["body"]
    assert any(k == "ARG" and r.startswith("IMAGE_REFRESH") for k, r in body)
    run = next(r for k, r in body if k == "RUN" and "tini" in r)
    assert "${IMAGE_REFRESH}" in run
