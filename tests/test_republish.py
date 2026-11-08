"""One command a hook can call blindly, that cannot hurt its caller.

homelab-ij8.81. The operator's bd wrapper fires this after any mutating bd call,
for every seat on the machine, knowing nothing about any of them. Three
properties make that safe, and each has a test here because each is the kind of
thing that is true on the day it is written and quietly stops being true.
"""

import os
from pathlib import Path

import pytest

from ream import republish


@pytest.fixture()
def logged(tmp_path, monkeypatch):
    log = tmp_path / "republish.log"
    monkeypatch.setenv("REAM_REPUBLISH_LOG", str(log))
    return log


def config(tmp_path, *entries) -> Path:
    path = tmp_path / "pages.toml"
    path.write_text(
        "".join(
            f'[[page]]\nid = "{e["id"]}"\nrender = """{e["render"]}"""\n\n'
            for e in entries
        ),
        encoding="utf-8",
    )
    return path


def test_no_config_is_silent_and_successful(tmp_path, logged, monkeypatch):
    """A machine-wide hook fires for every seat, and most have no config. That
    must cost nothing and say nothing, or the hook becomes noise people disable."""
    monkeypatch.setenv("REAM_PAGES_FILE", str(tmp_path / "absent.toml"))
    assert republish.main([]) == 0
    assert not logged.exists()


def test_a_render_that_fails_publishes_nothing(tmp_path, logged, monkeypatch):
    published = []
    monkeypatch.setattr(republish, "republish_one", lambda e, k: published.append(e))
    assert republish.render("exit 3") is None
    assert not published
    assert "exited 3" in logged.read_text()


def test_a_render_that_prints_nothing_publishes_nothing(tmp_path, logged):
    """An empty board reads as "the work stopped". Stale is better."""
    assert republish.render("true") is None
    assert "printed nothing" in logged.read_text()


def test_a_render_that_prints_something_that_is_not_a_page_publishes_nothing(
    tmp_path, logged
):
    """A traceback or a usage message on stdout would otherwise BE the page."""
    assert republish.render("echo 'Traceback (most recent call last):'") is None
    assert "did not print a page" in logged.read_text()


def test_a_render_that_hangs_does_not_hang_the_caller(tmp_path, logged, monkeypatch):
    monkeypatch.setattr(republish, "RENDER_TIMEOUT", 0.2)
    assert republish.render("sleep 30") is None
    assert "render" in logged.read_text().lower()


def test_a_real_page_is_returned(tmp_path):
    body = republish.render("printf '<!doctype html><html><body>hi</body></html>'")
    assert body is not None
    assert "<html" in body


def test_a_publish_failure_never_propagates(tmp_path, logged, monkeypatch):
    """The whole promise: nobody loses a bead update because a page did not
    redraw. A raising publish must come back as False, not as an exception."""

    def boom(*a, **k):
        raise RuntimeError("the service is down")

    monkeypatch.setattr(republish.client, "_read_current", boom)
    entry = {"id": "abc", "render": "printf '<html>x</html>'"}
    assert republish.republish_one(entry, "key") is False
    assert "publish failed" in logged.read_text()
    assert "the service is down" in logged.read_text()


def test_an_unchanged_page_is_reported_as_minting_no_version(
    tmp_path, logged, monkeypatch
):
    """`revision` is a hash of the stored body, so a debounced hook firing on a
    bead that did not change the page must create nothing. Asserted on the
    reported outcome rather than trusted."""
    monkeypatch.setattr(
        republish.client, "_read_current", lambda i, k: {"revision": "same"}
    )
    monkeypatch.setattr(
        republish.client, "_update", lambda f, i, k: {"revision": "same"}
    )
    entry = {"id": "abc", "render": "printf '<html>x</html>'"}
    assert republish.republish_one(entry, "key") is True
    assert "unchanged, no new version" in logged.read_text()


def test_a_changed_page_is_reported_as_a_new_version(tmp_path, logged, monkeypatch):
    monkeypatch.setattr(
        republish.client, "_read_current", lambda i, k: {"revision": "before"}
    )
    monkeypatch.setattr(
        republish.client, "_update", lambda f, i, k: {"revision": "after"}
    )
    entry = {"id": "abc", "render": "printf '<html>x</html>'"}
    assert republish.republish_one(entry, "key") is True
    assert "republished as after" in logged.read_text()


def test_the_body_actually_reaches_the_publisher(tmp_path, logged, monkeypatch):
    """The rendered HTML goes via a temporary file; prove the bytes survive it
    rather than assuming the plumbing is right."""
    seen = {}
    monkeypatch.setattr(
        republish.client, "_read_current", lambda i, k: {"revision": "r"}
    )

    def capture(file_path, artifact_id, key):
        seen["body"] = Path(file_path).read_text(encoding="utf-8")
        seen["id"] = artifact_id
        return {"revision": "r2"}

    monkeypatch.setattr(republish.client, "_update", capture)
    entry = {"id": "the-page", "render": "printf '<html><b>unique-marker</b></html>'"}
    assert republish.republish_one(entry, "key") is True
    assert "unique-marker" in seen["body"]
    assert seen["id"] == "the-page"


def test_a_malformed_page_entry_is_skipped_not_fatal(tmp_path, logged):
    path = tmp_path / "pages.toml"
    path.write_text('[[page]]\nid = "only-an-id"\n', encoding="utf-8")
    assert republish.pages(path) == []
    assert "no id or no render" in logged.read_text()


def test_several_pages_all_run_even_when_one_fails(tmp_path, logged, monkeypatch):
    """One broken page must not stop the others redrawing."""
    calls = []

    def one(entry, key):
        calls.append(entry["id"])
        return entry["id"] != "broken"

    monkeypatch.setattr(republish, "republish_one", one)
    monkeypatch.setattr(republish.client, "_read_key", lambda p: "key")
    monkeypatch.setattr(republish.client, "_key_file_path", lambda e: Path("/dev/null"))
    cfg = config(
        tmp_path,
        {"id": "first", "render": "true"},
        {"id": "broken", "render": "true"},
        {"id": "third", "render": "true"},
    )
    assert republish.main(["--config", str(cfg)]) == 1
    assert calls == ["first", "broken", "third"]


# --- the key reaches BOTH halves, and the descriptor is read-once ------------
#
# 22 Sep 2026 (ij8.81 follow-up). This command read the key FILE and nothing
# else, so the day the seat keys were vaulted it stopped working entirely: run
# under `artifacts-key`, with a good key on fd 3, it logged "cannot read key
# file" and published nothing — silently, because it must never fail its
# caller. And a render command usually needs the key itself, so handing the
# same read-once pipe to both halves gives one of them an empty string.


def _fd_with(key: str) -> int:
    """A read end holding `key`, the way the key wrapper hands one over."""
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "w", encoding="utf-8") as handle:
        handle.write(key + "\n")
    return read_fd


def test_the_render_can_read_the_key_even_though_the_parent_already_did(tmp_path):
    """The service-backed half of a board is only there if the render gets a key."""
    seen = tmp_path / "seen.txt"
    script = tmp_path / "render.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'printf %s "$(cat <&"$REAM_KEY_FD")" > {seen}\n'
        "printf '<html><body>page</body></html>\\n'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    body = republish.render(str(script), "art_the-one-key-4d1a7e2b9c")
    assert body is not None and "page" in body
    assert seen.read_text() == "art_the-one-key-4d1a7e2b9c"


def test_a_render_with_no_key_is_not_handed_a_drained_descriptor(tmp_path, monkeypatch):
    """A descriptor number left pointing at an already-read pipe is worse than
    none: the renderer reads nothing and the page publishes with its
    service-backed sections quietly missing."""
    monkeypatch.setenv("REAM_KEY_FD", "3")
    seen = tmp_path / "seen.txt"
    script = tmp_path / "render.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'printf %s "${{REAM_KEY_FD:-unset}}" > {seen}\n'
        "printf '<html><body>page</body></html>\\n'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    assert republish.render(str(script)) is not None
    assert seen.read_text() == "unset"


def test_the_key_comes_from_the_descriptor_when_there_is_no_key_file(
    tmp_path, logged, monkeypatch
):
    """Under `artifacts-key` there IS no key file — that is the point of it."""
    monkeypatch.setenv("REAM_KEY_FILE", str(tmp_path / "no-such-key"))
    monkeypatch.setenv("REAM_KEY_FD", str(_fd_with("art_from-the-descriptor-9f3b2c")))
    monkeypatch.setenv(
        "REAM_PAGES_FILE",
        str(config(tmp_path, {"id": "abc", "render": "echo '<html>x</html>'"})),
    )
    got: list[str] = []
    monkeypatch.setattr(republish, "republish_one", lambda e, k: got.append(k) is None)
    assert republish.main([]) == 0
    assert got == ["art_from-the-descriptor-9f3b2c"]
    assert not logged.exists()


def test_no_key_anywhere_is_logged_and_publishes_nothing(tmp_path, logged, monkeypatch):
    monkeypatch.delenv("REAM_KEY_FD", raising=False)
    monkeypatch.setenv("REAM_KEY_FILE", str(tmp_path / "no-such-key"))
    monkeypatch.setenv(
        "REAM_PAGES_FILE",
        str(config(tmp_path, {"id": "abc", "render": "echo '<html>x</html>'"})),
    )
    published: list[dict] = []
    monkeypatch.setattr(republish, "republish_one", lambda e, k: published.append(e))
    assert republish.main([]) == 1
    assert not published
    assert "no usable key" in logged.read_text()
