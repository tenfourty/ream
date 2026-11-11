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
    assert republish.render("exit 4") is None
    assert not published
    assert "exited 4" in logged.read_text()


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


# --- per-page timeout, refresh promise, touches, and "nothing to do" (runcove-5t3oj.125) ---

PAGE = "printf '<html>x</html>'"


@pytest.fixture()
def touches(monkeypatch):
    """Record every touch; `_read_current`/`_update` are stubbed by each test."""
    seen = []

    def post(page_id, key, **kw):
        seen.append({"id": page_id, **kw})
        return {"status": kw.get("status")}

    monkeypatch.setattr(republish.client, "_post_touch", post)
    return seen


def stub_service(monkeypatch, before="a", after="b"):
    updates = []
    monkeypatch.setattr(
        republish.client, "_read_current", lambda i, k: {"revision": before}
    )

    def update(f, i, k, **kw):
        updates.append(kw)
        return {"revision": after}

    monkeypatch.setattr(republish.client, "_update", update)
    return updates


def test_timeout_defaults_and_parses_the_clients_durations():
    assert republish.timeout_of({}) == republish.RENDER_TIMEOUT == 120
    assert republish.timeout_of({"timeout": "30m"}) == 1800
    assert republish.timeout_of({"timeout": 90}) == 90
    assert republish.timeout_of({"timeout": "1h"}) == 3600


@pytest.mark.parametrize("bad", ["soon", "2h", "0", 0, True, "-5", 7200])
def test_a_bad_timeout_is_refused(bad):
    with pytest.raises(ValueError):
        republish.timeout_of({"timeout": bad})


def test_a_page_with_a_bad_timeout_is_logged_and_skipped_not_fatal(
    tmp_path, logged, monkeypatch, touches
):
    updates = stub_service(monkeypatch)
    entry = {"id": "p", "render": PAGE, "timeout": "soon"}
    assert republish.republish_one(entry, "k") is False
    assert "bad setting" in logged.read_text()
    assert not updates and not touches


def test_the_pages_timeout_reaches_the_render(tmp_path, logged, monkeypatch, touches):
    stub_service(monkeypatch)
    seen = []
    monkeypatch.setattr(
        republish, "render", lambda c, k=None, t=None: seen.append(t) or None
    )
    republish.republish_one({"id": "p", "render": "x", "timeout": "10m"}, "k")
    assert seen == [600]


def test_a_slow_render_gets_its_longer_timeout(logged, monkeypatch):
    monkeypatch.setattr(republish, "RENDER_TIMEOUT", 0.2)
    assert republish.render("sleep 1; " + PAGE) is None  # default: times out
    assert republish.render("sleep 1; " + PAGE, None, 10) is not None


def test_a_published_page_carries_the_refresh_promise(logged, monkeypatch, touches):
    updates = stub_service(monkeypatch)
    entry = {"id": "p", "render": PAGE, "refresh_every": "2h"}
    assert republish.republish_one(entry, "k")
    assert updates == [{"refresh_every": 7200}]
    assert touches == []  # the update itself vouches for it


def test_no_refresh_every_sends_none(logged, monkeypatch, touches):
    updates = stub_service(monkeypatch)
    assert republish.republish_one({"id": "p", "render": PAGE}, "k")
    assert updates == [{}]


def test_a_bad_refresh_every_skips_the_page(logged, monkeypatch, touches):
    updates = stub_service(monkeypatch)
    entry = {"id": "p", "render": PAGE, "refresh_every": "often"}
    assert republish.republish_one(entry, "k") is False
    assert not updates and not touches
    assert "refresh_every" in logged.read_text()


def test_an_identical_revision_is_touched_ok_with_the_promise(
    logged, monkeypatch, touches
):
    stub_service(monkeypatch, before="same", after="same")
    entry = {"id": "p", "render": PAGE, "refresh_every": "30m"}
    assert republish.republish_one(entry, "k")
    assert touches == [{"id": "p", "status": "ok", "note": None, "refresh_every": 1800}]


def test_an_identical_revision_without_a_promise_is_touched_ok(
    logged, monkeypatch, touches
):
    stub_service(monkeypatch, before="same", after="same")
    assert republish.republish_one({"id": "p", "render": PAGE}, "k")
    assert touches[0]["status"] == "ok" and touches[0]["refresh_every"] is None


def test_exit_three_with_nothing_printed_is_skipped_and_touched_ok(
    logged, monkeypatch, touches
):
    updates = stub_service(monkeypatch)
    assert republish.RENDER_UNCHANGED == 3
    entry = {"id": "p", "render": "exit 3", "refresh_every": "1h"}
    assert republish.republish_one(entry, "k") is True
    assert not updates
    assert "skipped, nothing to do" in logged.read_text()
    assert touches == [{"id": "p", "status": "ok", "note": None, "refresh_every": 3600}]


def test_exit_three_that_printed_something_is_still_a_failure(logged):
    assert republish.render("echo oops; exit 3") is None
    assert republish.render("exit 3") is republish.SKIPPED


def test_empty_output_with_exit_zero_stays_a_failure(logged, monkeypatch, touches):
    updates = stub_service(monkeypatch)
    assert republish.republish_one({"id": "p", "render": "true"}, "k") is False
    assert not updates
    assert [t["status"] for t in touches] == ["failed"]


@pytest.mark.parametrize("cmd", ["exit 1", "echo nope"])
def test_a_failed_render_touches_failed(logged, monkeypatch, touches, cmd):
    updates = stub_service(monkeypatch)
    assert republish.republish_one({"id": "p", "render": cmd}, "k") is False
    assert not updates
    assert [(t["id"], t["status"]) for t in touches] == [("p", "failed")]


def test_a_timed_out_render_touches_failed(logged, monkeypatch, touches):
    stub_service(monkeypatch)
    monkeypatch.setattr(republish, "timeout_of", lambda e: 0.2)
    assert republish.republish_one({"id": "p", "render": "sleep 30"}, "k") is False
    assert [t["status"] for t in touches] == ["failed"]


def test_a_failing_touch_is_logged_never_raised(logged, monkeypatch):
    stub_service(monkeypatch, before="same", after="same")

    def boom(*a, **k):
        raise RuntimeError("touch refused")

    monkeypatch.setattr(republish.client, "_post_touch", boom)
    assert republish.republish_one({"id": "p", "render": PAGE}, "k") is True
    assert republish.republish_one({"id": "p", "render": "exit 1"}, "k") is False
    assert "touch (ok) failed" in logged.read_text()
    assert "touch (failed) failed" in logged.read_text()
