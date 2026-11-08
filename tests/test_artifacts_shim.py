"""The old import name: `artifacts.X` is `ream.X`, with the real spec and a runnable loader."""

import importlib
import subprocess
import sys

import ream


def test_identity_holds_and_the_real_spec_survives():
    mod = importlib.import_module("artifacts.envnames")
    real = importlib.import_module("ream.envnames")
    assert mod is real
    assert real.__spec__.name == "ream.envnames"
    assert importlib.import_module("artifacts") is ream


def test_python_dash_m_style_run_finds_code():
    # runpy asks the loader for code; it used to raise AttributeError (no get_code).
    # A fresh interpreter, as `python -m artifacts.x` is: in-process the module is
    # already imported and runpy would warn about that, whatever the shim does.
    code = (
        "import runpy; ns = runpy.run_module('artifacts.envnames', run_name='x');"
        "assert '__doc__' in ns"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-W", "error", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
