"""The tools keep themselves current.

The server answers every /api/v1 call with `X-Ream-Client-Outdated: true` when the calling tool
is older than `clientlevel.MIN_CLIENT_LEVEL`, and names the version to move to in
`X-Ream-Client-Ref`. This module reacts, once per process:

* it runs `uv tool install --force <source>@<ref>` and, after a safe call (GET/HEAD), runs the
  same command again; after a call that changes something it does not repeat it (that would
  post twice), so the next call is the new tool;
* if uv is missing or the install fails, it prints one line with the command to run by hand.

SECURITY. The SOURCE is fixed on the client: `REAM_INSTALL_SOURCE` (or `ARTIFACTS_`) in the
tool's own environment, or the first line of `$XDG_CONFIG_HOME/ream/install-source`, written at
install time. Whatever the server sends as a source (`X-Ream-Install-Source`) is never read here
and never installed from. The server may name only a REF, and it must be a version tag or a
commit hash (`_REF`) before it is used. The command is an argument list, never a shell string.
No key is ever printed or passed.

`REAM_NO_SELF_UPDATE=1` turns the install off (the line is still printed). `REAM_SELF_UPDATED`
is the loop guard: set on the re-exec, so one command updates at most once.
Standard library only.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ream import clientlevel, envnames

# Captured when this module is first imported, before `ream <verb>` rewrites sys.argv.
_ORIGINAL_ARGV = list(sys.argv)

_GUARD = "REAM_SELF_UPDATED"
# A tag such as v1.2.3 or 2026.11.9, or a commit hash of 7 to 40 hex digits. Nothing else:
# no slash, no leading dash, no @, no space.
_REF = re.compile(r"(?:v?\d+(?:\.\d+){1,3}(?:-[0-9A-Za-z.]{1,20})?|[0-9a-f]{7,40})")
_SOURCE = re.compile(r"git\+https://[A-Za-z0-9._~/:-]+")
_INSTALL_TIMEOUT = 300

_done = False  # one decision per process


def valid_ref(ref: str | None) -> bool:
    return bool(ref) and _REF.fullmatch(ref or "") is not None


def configured_source() -> str | None:
    """Where this client installs itself from: its own setting, never the server's."""
    value = envnames.get("INSTALL_SOURCE")
    if not value:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        try:
            text = (Path(base) / "ream" / "install-source").read_text(encoding="utf-8")
            value = text.splitlines()[0].strip() if text.strip() else None
        except OSError:
            value = None
    if value and _SOURCE.fullmatch(value) and ".." not in value:
        return value
    return None


def _disabled() -> bool:
    return (envnames.get("NO_SELF_UPDATE") or "").lower() in {"1", "true", "yes", "on"}


def command_for(source: str, ref: str) -> list[str]:
    return ["uv", "tool", "install", "--force", f"{source}@{ref}"]


def _say(text: str) -> None:
    sys.stderr.write(f"ream: {text}\n")


def on_response(method: str, headers) -> None:
    """Called with every API response's headers (lower-case names). Does nothing unless the
    server says this tool is out of date, and then at most once per process."""
    global _done
    if _done:
        return
    if (headers.get(clientlevel.OUTDATED_HEADER.lower()) or "").lower() != "true":
        return
    _done = True
    ref = headers.get(clientlevel.REF_HEADER.lower())
    ref = ref.strip() if ref else None
    source = configured_source()
    if not valid_ref(ref) or source is None:
        _say(
            "this tool is out of date (the service asks for a newer one). Reinstall it with "
            "the install line in the service's onboarding document."
        )
        return
    command = command_for(source, ref)
    line = " ".join(command)
    if _disabled():
        _say(f"this tool is out of date. To update it run: {line}")
        return
    if os.environ.get(_GUARD):
        _say(f"this tool is still out of date after an update. Run by hand: {line}")
        return
    if shutil.which("uv") is None:
        _say(f"this tool is out of date and uv is not installed. Run by hand: {line}")
        return
    _say(f"this tool is out of date; updating to {ref} ...")
    try:
        done = subprocess.run(  # noqa: S603 -- fixed argv from the client's own source
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_INSTALL_TIMEOUT,
            check=False,
        )
        ok = done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        _say(f"the update failed. Run by hand: {line}")
        return
    if method.upper() in {"GET", "HEAD"} and _ORIGINAL_ARGV and _ORIGINAL_ARGV[0]:
        os.environ[_GUARD] = "1"
        sys.stdout.flush()
        sys.stderr.flush()
        program = _ORIGINAL_ARGV[0]
        if not Path(program).is_file():
            program = shutil.which(program) or program
        try:
            os.execv(program, _ORIGINAL_ARGV)  # noqa: S606 -- our own command, once
        except OSError:
            _say("updated; run the command again to use the new tool.")
        return
    _say("updated; the next call uses the new tool.")
