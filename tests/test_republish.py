"""One command a hook can call blindly, that cannot hurt its caller.

homelab-ij8.81. The operator's bd wrapper fires this after any mutating bd call,
for every seat on the machine, knowing nothing about any of them. Three
properties make that safe, and each has a test here because each is the kind of
thing that is true on the day it is written and quietly stops being true.
"""

from pathlib import Path

import pytest

from artifacts import republish


@pytest.fixture()
def logged(tmp_path, monkeypatch):
    log = tmp_path / "republish.log"
    monkeypatch.setenv("ARTIFACTS_REPUBLISH_LOG", str(log))
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
    monkeypatch.setenv("ARTIFACTS_PAGES_FILE", str(tmp_path / "absent.toml"))
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
    monkeypatch.setattr(republish, "RENDER_TIMEOUT", 1)
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


def test_an_unchanged_page_is_reported_as_minting_no_version(tmp_path, logged, monkeypatch):
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
