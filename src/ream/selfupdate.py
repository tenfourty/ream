"""The tools keep themselves current.

The server answers every /api/v1 call with `X-Ream-Client-Outdated: true` when the calling tool
is older than `clientlevel.MIN_CLIENT_LEVEL`. That is ALL the server says: it names no source,
no version and no ref. This module reacts, once per process:

* it runs `uv tool install --force --refresh <source>@main`, checks the installed copy is not
  older than this one, and after a safe call (GET/HEAD) runs the same command again; after a
  call that changes something it does not repeat it (that would post twice), so the next call
  is the new tool;
* if uv is missing, the install fails or the installed copy is OLDER, it prints one line with
  the command to run by hand and carries on as it is.

SECURITY. The SOURCE is fixed on the client: `REAM_INSTALL_SOURCE` (or `ARTIFACTS_`) in the
tool's own environment, or the first line of `$XDG_CONFIG_HOME/ream/install-source`, or, with
neither, the `git` requirement in uv's own receipt (`uv-receipt.toml` in `sys.prefix`) when the
tool was installed from a git+https address; a path install gives none. What the server sends (`X-Ream-Install-Source`) is never read here. The ref is a
constant, `main`: the reviewed, CI-gated branch, so a hostile or spoofed server cannot choose
which commit is installed (an unreviewed `ci/` branch, say). No downgrade: the installed copy's
level must be at least this one's. At most one attempt an hour per machine (a stamp file), so a
server that always says "out of date" cannot cause an install loop. The command is an argument
list, never a shell string. No key is ever printed or passed.

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
import time
import tomllib
from pathlib import Path

from ream import clientlevel, envnames

# Captured when this module is first imported, before `ream <verb>` rewrites sys.argv.
_ORIGINAL_ARGV = list(sys.argv)

_GUARD = "REAM_SELF_UPDATED"
REF = "main"
_SOURCE = re.compile(r"git\+https://[A-Za-z0-9._~/:-]+")
_INSTALL_TIMEOUT = 300
_MIN_GAP = 3600  # seconds between two attempts on one machine

_done = False  # one decision per process


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
    if value:
        return None  # a setting that is wrong is not replaced by a guess
    return _receipt_source()


def _clean(address: str) -> str | None:
    """`git+https://host/path[@ref][#frag]` as `git+https://host/path`, or None."""
    address = address.split("#", 1)[0].split("?", 1)[0]
    if not address.startswith("git+"):
        address = "git+" + address
    scheme, _, rest = address.partition("://")
    rest = rest.rsplit("@", 1)[0] if "@" in rest.rsplit("/", 1)[-1] else rest
    address = f"{scheme}://{rest}"
    if _SOURCE.fullmatch(address) and ".." not in address:
        return address
    return None


def _receipt_source() -> str | None:
    """The git address uv recorded when it installed this tool (`uv-receipt.toml` beside
    `sys.prefix`): only its requirement is read. A path install, a missing or unreadable
    receipt gives None. The ref is dropped; the tool always installs `main`."""
    try:
        with open(Path(sys.prefix) / "uv-receipt.toml", "rb") as handle:
            receipt = tomllib.load(handle)
        requirements = receipt["tool"]["requirements"]
        for item in requirements:
            if item.get("name") not in {"ream", "artifacts"}:
                continue
            git = item.get("git")
            if isinstance(git, str) and git.startswith(("https://", "git+https://")):
                return _clean(git)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return None


def _disabled() -> bool:
    return (envnames.get("NO_SELF_UPDATE") or "").lower() in {"1", "true", "yes", "on"}


def command_for(source: str) -> list[str]:
    return ["uv", "tool", "install", "--force", "--refresh", f"{source}@{REF}"]


def _say(text: str) -> None:
    sys.stderr.write(f"ream: {text}\n")


def _stamp_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "ream" / "self-update-attempt"


def _attempt_allowed() -> bool:
    """True, and records the attempt, unless one was made in the last hour."""
    path = _stamp_path()
    try:
        if time.time() - path.stat().st_mtime < _MIN_GAP:
            return False
    except OSError:
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        return False  # cannot record it, so cannot rate-limit it: do not install
    return True


def _run(command: list[str], timeout: int = _INSTALL_TIMEOUT):
    return subprocess.run(  # noqa: S603 -- argument list from the client's own source
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _installed_level() -> int | None:
    """The level of the copy `uv tool install` just wrote, asked of ITS Python (not this
    process's modules). None when it cannot be told."""
    try:
        where = _run(["uv", "tool", "dir"], 30)
        if where.returncode != 0:
            return None
        python = Path(where.stdout.strip()) / "ream" / "bin" / "python"
        answer = _run(
            [
                str(python),
                "-I",
                "-c",
                "from ream import clientlevel; print(clientlevel.CLIENT_LEVEL)",
            ],
            30,
        )
        if answer.returncode != 0:
            return None
        return int(answer.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _manual_line(headers) -> str:
    """The line for a tool with no fixed source. The server's note is printed for a person to
    run once; nothing here executes it."""
    line = (
        "this tool is out of date (the service asks for a newer one). Reinstall it with "
        "the install line in the service's onboarding document."
    )
    note = (headers.get(clientlevel.SOURCE_HEADER.lower()) or "").strip()
    if note and _SOURCE.fullmatch(note) and ".." not in note:
        line += f" Run this once and later updates are automatic: uv tool install --force {note}"
    return line


def on_response(method: str, headers) -> None:
    """Called with every API response's headers (lower-case names). Does nothing unless the
    server says this tool is out of date, and then at most once per process."""
    global _done
    if _done:
        return
    if (headers.get(clientlevel.OUTDATED_HEADER.lower()) or "").lower() != "true":
        return
    _done = True
    source = configured_source()
    if source is None:
        _say(_manual_line(headers))
        return
    command = command_for(source)
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
    if not _attempt_allowed():
        _say(
            f"this tool is out of date; an update was tried within the hour. Run: {line}"
        )
        return
    _say("this tool is out of date; updating ...")
    try:
        ok = _run(command).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        _say(f"the update failed. Run by hand: {line}")
        return
    level = _installed_level()
    if level is None or level < clientlevel.CLIENT_LEVEL:
        _say(
            "the installed copy is older than this one (or its level cannot be read), so "
            f"this command carries on as it is. Run by hand: {line}"
        )
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
