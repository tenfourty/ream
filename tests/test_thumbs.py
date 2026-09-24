"""Rendering a page's picture, with the browser treated as untrusted.

homelab-ij8.22. Jeremy chose (b) — the renderer inside the service itself —
over a separate renderer, with the argument against in front of him. So the
execution of agent-written HTML happens in the service's own pod, and what
makes that acceptable is not care in the calling code but the constraints the
child runs under. Those constraints are what this file tests; everything else
about thumbnails is decoration by comparison.
"""

import subprocess
from pathlib import Path

import pytest

from artifacts import thumbs

PAGE = (
    "<!doctype html><html><head><title>T</title>"
    "<style>body{background:#fff;font:16px sans-serif}</style></head>"
    "<body><h1>Hello</h1><p>A real page.</p></body></html>"
)

needs_browser = pytest.mark.skipif(
    not thumbs.available(),
    reason="no headless browser in this environment; the unit tests below still run",
)


# --- the constraints, which do not need a browser to verify ------------------


def test_the_child_gets_no_credentials(monkeypatch):
    """THE ONE THAT MATTERS. An inherited environment is the likeliest way a
    render becomes an exfiltration: DATABASE_URL carries the database password,
    and the pod's environment is right there for the taking. The child's
    environment is built from nothing, and this asserts it stays that way."""
    seen = {}

    def capture(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(argv, 1)  # stop before doing any work

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:hunter2@db/artifacts")
    monkeypatch.setenv("ARTIFACTS_BROWSER", "/bin/true")
    monkeypatch.setattr(thumbs.subprocess, "run", capture)

    assert thumbs.render(PAGE) is None
    env = seen["env"]
    assert "DATABASE_URL" not in env
    assert not any("hunter2" in str(v) for v in env.values())
    assert set(env) <= {"HOME", "TMPDIR", "PATH"}, f"unexpected inherited vars: {env}"


def test_the_child_is_its_own_process_group(monkeypatch):
    """So a timeout kills the browser and everything it spawned, not just the
    parent. A renderer that leaves zombies behind fills the pod slowly."""
    seen = {}
    monkeypatch.setenv("ARTIFACTS_BROWSER", "/bin/true")
    monkeypatch.setattr(
        thumbs.subprocess,
        "run",
        lambda argv, **kw: seen.update(kw) or subprocess.CompletedProcess(argv, 0),
    )
    thumbs.render(PAGE)
    assert seen["start_new_session"] is True
    assert seen["timeout"] == thumbs.TIMEOUT_SECONDS


def test_no_browser_means_no_thumbnail_not_an_error(monkeypatch):
    """A deployment without a browser has a wall with gaps, not a broken
    service. Publishing must never depend on this."""
    monkeypatch.setenv("ARTIFACTS_BROWSER", "/nonexistent/browser")
    assert thumbs.available() is False
    assert thumbs.render(PAGE) is None


def test_a_timeout_produces_nothing_rather_than_raising(monkeypatch):
    monkeypatch.setenv("ARTIFACTS_BROWSER", "/bin/true")
    monkeypatch.setattr(
        thumbs.subprocess,
        "run",
        lambda argv, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 1)),
    )
    assert thumbs.render(PAGE) is None


def test_output_that_is_not_a_png_is_refused(monkeypatch, tmp_path):
    """A truncated or empty file would be served as a broken image, which looks
    to a reader like the PAGE is broken rather than the thumbnail."""
    monkeypatch.setenv("ARTIFACTS_BROWSER", "/bin/true")

    def write_rubbish(argv, **kw):
        shot = next(a for a in argv if str(a).startswith("--screenshot="))
        Path(shot.split("=", 1)[1]).write_bytes(b"not a png at all")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(thumbs.subprocess, "run", write_rubbish)
    assert thumbs.render(PAGE) is None


def test_the_flags_disable_the_network(monkeypatch):
    """The page's own CSP forbids external fetches, but a CSP is enforced by the
    component being treated as untrusted, so it is not the control relied on."""
    seen = {}
    monkeypatch.setenv("ARTIFACTS_BROWSER", "/bin/true")
    monkeypatch.setattr(
        thumbs.subprocess,
        "run",
        lambda argv, **kw: (
            seen.setdefault("argv", argv) or subprocess.CompletedProcess(argv, 0)
        ),
    )
    thumbs.render(PAGE)
    flags = " ".join(str(a) for a in seen["argv"])
    assert "NetworkService" in flags
    assert "--disable-background-networking" in flags
    assert "--disable-remote-fonts" in flags


# --- with a real browser, when there is one ----------------------------------


@needs_browser
def test_a_real_page_renders_to_a_real_png():
    png = thumbs.render(PAGE)
    assert png is not None
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 1000, "a PNG this small rendered nothing"


@needs_browser
def test_a_page_that_never_settles_still_returns(monkeypatch):
    """A page can loop. It must not hold a worker: the virtual time budget ends
    the render whatever the page is doing."""
    spinning = (
        "<!doctype html><html><body><script>"
        "while(false){} setInterval(function(){document.title=Date.now()},1);"
        "</script><p>busy</p></body></html>"
    )
    png = thumbs.render(spinning)
    assert png is None or png.startswith(b"\x89PNG\r\n\x1a\n")
