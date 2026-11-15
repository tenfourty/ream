"""`ream screenshot` (runcove-uap8t, slice 5): the screenshot route from the command line.

Against the real app over real HTTP (the `live` server of `tests/test_client.py`), with the
browser faked as in `tests/test_screenshot_api.py`; the retry rules against a scripted opener.
"""

import io
import json
import urllib.error

import pytest
from test_client import key_file, live  # noqa: F401
from test_screenshot_api import a_shot, browser, png  # noqa: F401

from ream import cli, client, keys, renderwire, shots, store

PAGE = "<!doctype html><html><head><title>B</title></head><body>one</body></html>"
FAKE_KEY = "art_" + "k" * 30


@pytest.fixture()
def page(db, key_file):
    first = store.publish(db, "ops-seat", "Board", PAGE)
    store.patch(db, "ops-seat", first.id, PAGE.replace("one", "two"), first.revision)
    return first.id


def run(path, *args) -> int:
    return client.screenshot_main(["--key-file", str(path), *args])


# --- against the service ----------------------------------------------------------------


def test_it_saves_the_png_and_prints_its_facts(
    live, key_file, page, browser, tmp_path, capsys
):
    path, _ = key_file
    out = tmp_path / "shot.png"
    browser.answer = a_shot(1280, 900)
    assert run(path, page, "--out", str(out)) == 0
    assert out.read_bytes() == png(1280, 900)
    said = capsys.readouterr()
    assert said.out == f"{out} 1280x900 v2\n"
    assert "warning" not in said.err
    assert browser.calls[-1][2] == renderwire.shot_request(1280, "light")
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_a_cut_picture_is_warned_about_on_stderr(
    live, key_file, page, browser, tmp_path, capsys
):
    path, _ = key_file
    browser.answer = a_shot(1280, 900, page_height=12000, truncated=True)
    assert run(path, page, "--out", str(tmp_path / "s.png")) == 0
    said = capsys.readouterr()
    assert said.out.endswith(" 1280x900 v2\n")
    assert "12000 px tall" in said.err and "--element" in said.err


def test_the_flags_reach_the_browser(live, key_file, page, browser, tmp_path, capsys):
    path, _ = key_file
    browser.answer = a_shot(390, 300)
    out = str(tmp_path / "s.png")
    args = ["--version", "1", "--width", "390", "--theme", "dark"]
    assert run(path, page, *args, "--section", "Intro Part", "--out", out) == 0
    assert capsys.readouterr().out == f"{out} 390x300 v1\n"
    _, html, request = browser.calls[-1]
    assert "one" in html and "two" not in html
    assert request == renderwire.shot_request(390, "dark", section="intro part")
    assert run(path, page, "--element", "results", "--out", out) == 0
    assert browser.calls[-1][2] == renderwire.shot_request(
        1280, "light", element="results"
    )


def test_ream_screenshot_runs_it(
    live, key_file, page, browser, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("sys.argv", ["ream"])  # `cli.main` rewrites it: restored after
    path, _ = key_file
    out = tmp_path / "s.png"
    with pytest.raises(SystemExit) as stop:
        cli.main(["screenshot", page, "--key-file", str(path), "--out", str(out)])
    assert stop.value.code == 0
    assert out.read_bytes() == png(1280, 900)
    assert "screenshot" in cli._help()


def test_another_agents_page_prints_the_refusal_and_writes_nothing(
    live, db, page, browser, tmp_path, capsys
):
    token, _ = keys.mint(db, agent="other")
    other = tmp_path / "other-key"
    other.write_text(token)
    other.chmod(0o600)
    out = tmp_path / "s.png"
    out.write_bytes(b"before")
    assert run(other, page, "--out", str(out)) == 1
    assert capsys.readouterr().err.strip() == "404 no such page"
    assert out.read_bytes() == b"before"
    assert browser.calls == []


def test_a_structured_refusal_prints_its_json(
    live, key_file, page, browser, tmp_path, capsys
):
    path, _ = key_file
    browser.answer = renderwire.Shot("no_match", matches=0)
    assert run(path, page, "--element", "nope", "--out", str(tmp_path / "s")) == 1
    status, _, detail = capsys.readouterr().err.strip().partition(" ")
    assert status == "422"
    assert json.loads(detail) == {
        "field": "element",
        "detail": "no part of the page is that element",
        "matches": 0,
    }


@pytest.mark.parametrize(
    "args",
    [
        ["--width", "800"],
        ["--theme", "sepia"],
        ["--element", "a", "--section", "b"],
        [],  # no --out
    ],
)
def test_bad_flags_are_usage_errors(tmp_path, args):
    out = [] if not args else ["--out", str(tmp_path / "s.png")]
    with pytest.raises(SystemExit) as stop:
        client.screenshot_main(["page", *args, *out])
    assert stop.value.code == 2


# --- retries, against a scripted opener ---------------------------------------------------


class _Script:
    """Answers each open with the next `(status, headers, body)`."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.timeouts = []

    def open(self, request, timeout=None):
        self.timeouts.append(timeout)
        status, headers, body = self.answers.pop(0)
        if status != 200:
            raise urllib.error.HTTPError(
                request.full_url, status, "no", headers, io.BytesIO(body)
            )

        class _R:
            def __init__(self):
                self.status = 200
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return body

        return _R()


OK = (
    200,
    {
        "x-ream-shot-version": "7",
        "x-ream-shot-width": "1280",
        "x-ream-shot-height": "3421",
        "x-ream-shot-truncated": "false",
        "x-ream-shot-page-height": "3421",
    },
    png(4, 4),
)
BUSY = (503, {"retry-after": "5"}, json.dumps({"detail": shots.BUSY}).encode())
UNAVAILABLE = (
    503,
    {},
    json.dumps(
        {"detail": {"renderer": True, "reason": "renderer_unavailable"}}
    ).encode(),
)


@pytest.fixture()
def scripted(monkeypatch, tmp_path):
    monkeypatch.setenv("REAM_API", "https://example.invalid/api/v1")
    slept = []
    monkeypatch.setattr(client.time, "sleep", slept.append)
    key = tmp_path / "key"
    key.write_text(FAKE_KEY)
    key.chmod(0o600)

    def use(*answers):
        opener = _Script(*answers)
        monkeypatch.setattr(client, "_opener", opener)
        return opener

    return key, slept, use


def test_a_busy_503_is_waited_out_as_retry_after_says(scripted, tmp_path, capsys):
    key, slept, use = scripted
    opener = use(BUSY, BUSY, OK)
    out = tmp_path / "shot.png"
    assert run(key, "abc", "--out", str(out)) == 0
    assert slept == [5, 5]
    assert opener.timeouts == [45, 45, 45]
    assert out.read_bytes() == png(4, 4)
    assert capsys.readouterr().out == f"{out} 1280x3421 v7\n"


def test_busy_is_tried_three_more_times_then_reported(scripted, tmp_path, capsys):
    key, slept, use = scripted
    opener = use(BUSY, BUSY, BUSY, BUSY)
    assert run(key, "abc", "--out", str(tmp_path / "s.png")) == 1
    assert len(opener.timeouts) == 4 and slept == [5, 5, 5]
    assert capsys.readouterr().err.strip().endswith(f"503 {shots.BUSY}")
    assert not (tmp_path / "s.png").exists()


def test_a_503_without_retry_after_is_not_retried(scripted, tmp_path, capsys):
    key, slept, use = scripted
    opener = use(UNAVAILABLE, OK)
    assert run(key, "abc", "--out", str(tmp_path / "s.png")) == 1
    assert len(opener.timeouts) == 1 and slept == []
    status, _, detail = capsys.readouterr().err.strip().partition(" ")
    assert status == "503"
    assert json.loads(detail)["reason"] == "renderer_unavailable"


def test_a_200_that_is_not_a_png_writes_nothing(scripted, tmp_path, capsys):
    key, _, use = scripted
    use((200, OK[1], b"<html>proxy page</html>"))
    assert run(key, "abc", "--out", str(tmp_path / "s.png")) == 1
    assert "not a PNG" in capsys.readouterr().err
    assert not (tmp_path / "s.png").exists()
