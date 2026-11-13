"""The browser runner lives in `ream.rendercore`, apart from the database layer.

`thumbs.py` keeps the store-facing parts and calls the runner; the runner itself
must load without `ream.store` or `ream.db`, so it can be used where neither is.
"""

import subprocess
import sys

from ream import rendercore, thumbs

_PROBE = (
    "import sys, ream.rendercore; "
    "print(','.join(sorted(m for m in sys.modules "
    "if m in ('ream.store', 'ream.db', 'artifacts.store', 'artifacts.db'))))"
)


def test_rendercore_imports_nothing_of_the_database_layer():
    done = subprocess.run(  # noqa: S603 -- fixed argv, our own interpreter
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "", f"rendercore pulled in: {done.stdout.strip()}"


def test_the_probe_would_see_the_database_layer():
    """The probe above is not vacuous: importing thumbs, which does use the
    store, is seen by the same check."""
    probe = _PROBE.replace("ream.rendercore", "ream.thumbs")
    done = subprocess.run(  # noqa: S603 -- fixed argv, our own interpreter
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "ream.store" in done.stdout.strip().split(",")


def test_thumbs_still_offers_the_runner_names():
    """Callers keep using `thumbs.X`; those names are the runner's own objects.
    (`available` is left out: the suite's own fixture replaces it on `thumbs`.)"""
    for name in (
        "browser_path",
        "console_errors",
        "Report",
        "RenderTooBig",
        "too_big_to_render",
        "WIDTH",
        "HEIGHT",
        "RENDER_MAX_BYTES",
        "TIMEOUT_SECONDS",
        "_RENDER_SLOT",
        "_run",
    ):
        assert getattr(thumbs, name) is getattr(rendercore, name), name
