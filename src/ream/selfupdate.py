"""The tools keep themselves current.

The server answers every /api/v1 call with `X-Ream-Client-Outdated: true` when the calling tool
is older than `clientlevel.MIN_CLIENT_LEVEL`. That is ALL the server says: it names no source,
no version and no ref. This module reacts, once per process:

* it runs `uv tool install --force --refresh <source>@main`, checks the installed copy is not
  older than this one, and re-runs the same command once, but only if this process has sent
  nothing but GET/HEAD and has not read stdin; otherwise (that would post twice) it says the next
  command is the new tool. uv runs in a minimal environment (no REAM_/ARTIFACTS_ variable, nothing
  named KEY, TOKEN, SECRET or PASSWORD); the attempt is claimed with an exclusive, no-symlink create;
* if uv is missing, the install fails or the installed copy is OLDER, it prints one line with
  the command to run by hand and carries on as it is.

SECURITY. The SOURCE is fixed on the client: `REAM_INSTALL_SOURCE` (or `ARTIFACTS_`) in the
tool's own environment, or the first line of `$XDG_CONFIG_HOME/ream/install-source`, or, with
neither, what uv's own receipt (`uv-receipt.toml` in `sys.prefix`) says the tool was installed
from: a `git+https` address (reinstalled at `main`), or the service's tools index, when uv
recorded `no-index` and its `find-links` addresses for this package (reinstalled with
`uv tool install --force --refresh --no-index --find-links <the same addresses> ream-artifacts`).
A path install, and a receipt with neither, give none: a bare name is never handed to uv
without `--no-index`, because an unrelated package called `ream` is on the public index.
THE INSTALL NAME is `ream-artifacts` (decisions.md, 10 Nov 2026); the package was `ream`, and
before that `artifacts`. A tool installed under an old name (its receipt says `ream` or
`artifacts`) updates from the same source and comes out as `ream-artifacts`: `--force` makes the
new tool's executables replace the old ones, and the old tool, which uv would otherwise go on
listing with the same executables, is then uninstalled (uv removes the executables its receipt
lists, which are the new tool's by then, so the new tool is installed once more to put them
back). See `_retire_old_tools`.
PLUG-INS. A tool installed with extra packages (`--with`, `--with-executables-from`) must not
come back as the package alone. The receipt's other requirements are passed back with the same
flags: a git (`https`, no login) address with its recorded ref, or a bare name only when the
receipt shows `no-index` and `find-links` (the command then carries them); `--with-executables-from` where the receipt shows
that package's console scripts. An extra whose source cannot be repeated safely (a local path or
editable directory, whose contents may have changed since; a url, a marker, an index, a
subdirectory, two sources) stops the update: the manual line names it. See
`receipt_extras`.
What the server sends (`X-Ream-Install-Source`) is never read here. The ref is a
constant, `main`: the reviewed, CI-gated branch, so a hostile or spoofed server cannot choose
which commit is installed (an unreviewed `ci/` branch, say); a tools-index install gets the
wheel its own index serves, and the index is the one the tool was installed from, never one
the server names. No downgrade: the installed copy's
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
import stat
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qsl, urlsplit

from ream import clientlevel, envnames

# Captured when this module is first imported, before `ream <verb>` rewrites sys.argv.
_ORIGINAL_ARGV = list(sys.argv)

_GUARD = "REAM_SELF_UPDATED"
REF = "main"
_SOURCE = re.compile(r"git\+https://[A-Za-z0-9._~/:-]+")
# A tools-index address: a plain web address with no login, query, fragment, space or quote.
_INDEX_URL = re.compile(r"https?://[A-Za-z0-9._~/:-]+")
_LOOPBACK = {"localhost", "127.0.0.1"}  # the only hosts allowed plain http
# What the tools index serves, and the name uv installs the tool under. The distribution name
# `ream` is an unrelated package on public PyPI, so the install name is `ream-artifacts`.
PACKAGE = "ream-artifacts"
# The names this package had before: a tool whose receipt says one of them is the same tool.
OLD_NAMES = ("ream", "artifacts")
# A receipt requirement with any of these keys has a source of its own (a git address, a path,
# a URL): it is not "the package, from an index".
_OWN_SOURCE = {"git", "path", "directory", "url", "editable", "index"}
# The extra packages a tool was installed with (`--with`), as the receipt records them.
_MAIN_NAMES = {PACKAGE, *OLD_NAMES}  # this package, under any of its names
# Console scripts that only a tool of ours has: how a tool directory under an old name is told
# from a stranger's package of the same name (`ream` is one on PyPI).
_OUR_SCRIPTS = {"artifacts-publish", "artifacts-decide", "artifacts-republish"}
_PKG_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_VERSION_SPEC = re.compile(
    r"[<>=!~][A-Za-z0-9.*+!<>=~, -]*"
)  # `>=1.2,<2`: no `@`, `;` or `:`
_REF = re.compile(r"[0-9A-Za-z][0-9A-Za-z._/-]{0,63}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BARE_KEYS = {"name", "specifier", "extras"}  # a name resolved from the index
_GIT_KEYS = {"name", "git", "rev", "tag", "branch"}
_LOCAL_SOURCES = {"path", "directory", "editable"}
_INSTALL_TIMEOUT = 300
_MIN_GAP = 3600  # seconds between two attempts on one machine

_done = False  # one decision per process
_mutated = False  # this process sent a change, or read stdin: it must never run twice

_ENV_KEEP = ("PATH", "HOME", "TMPDIR", "LANG", "REQUESTS_CA_BUNDLE")
_ENV_KEEP_PREFIX = ("XDG_", "UV_", "SSL_CERT_")
_ENV_SECRET = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)


def note_mutation() -> None:
    """A request that changes something went out, or stdin was read: a re-exec would repeat
    it, so this process never re-execs (it still installs)."""
    global _mutated
    _mutated = True


def _install_env() -> dict[str, str]:
    """The environment uv runs in: the few variables it needs, none of ours and none that
    looks like a credential."""
    env = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper.startswith(("REAM_", "ARTIFACTS_")) or _ENV_SECRET.search(upper):
            continue
        if upper in _ENV_KEEP or upper.startswith(_ENV_KEEP_PREFIX):
            env[name] = value
    env["GIT_TERMINAL_PROMPT"] = (
        "0"  # git never waits for a username on an unattended run
    )
    return env


def configured_source() -> str | tuple[str, ...] | None:
    """Where this client installs itself from: its own setting, never the server's. A
    `git+https://` address (the setting, the file or a git receipt), or, only from a receipt of
    a tools-index install, the tuple of that index's addresses."""
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


def _index_urls(options) -> tuple[str, ...] | None:
    """The addresses of a tools index from the receipt's `[tool.options]`, only when uv
    recorded `no-index = true` (so the public index cannot answer for the name `ream`) and
    every `find-links` entry is a plain web address: https, or http to this machine."""
    if not isinstance(options, dict) or options.get("no-index") is not True:
        return None
    links = options.get("find-links")
    if not isinstance(links, list) or not links:
        return None
    for link in links:
        if not isinstance(link, str) or not _INDEX_URL.fullmatch(link) or ".." in link:
            return None
        parts = urlsplit(link)
        if not parts.hostname:
            return None
        if parts.scheme != "https" and parts.hostname not in _LOOPBACK:
            return None
    return tuple(links)


def _receipt_source() -> str | tuple[str, ...] | None:
    """What uv recorded when it installed this tool (`uv-receipt.toml` beside `sys.prefix`):
    the git address of a git install (the ref is dropped; the tool always installs `main`), or,
    for this package (under its present name or an old one) installed with
    `--no-index --find-links`, those addresses. A path install, a receipt with neither, a
    missing or unreadable receipt gives None."""
    try:
        with open(Path(sys.prefix) / "uv-receipt.toml", "rb") as handle:
            tool = tomllib.load(handle)["tool"]
        for item in tool["requirements"]:
            if not _is_main(item.get("name")):
                continue
            git = item.get("git")
            if isinstance(git, str) and git.startswith(("https://", "git+https://")):
                return _clean(git)
            if not _OWN_SOURCE.intersection(item):
                return _index_urls(tool.get("options"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return None


@dataclass(frozen=True)
class Extras:
    """The extra packages a tool was installed with (`uv tool install --with ...`), read from
    uv's receipt. `args` are the options that install them again, `index` the receipt's
    `--find-links` addresses when a bare name among them needs them, `blocked` the names of
    extras whose source cannot be repeated safely (any one of them stops the update), `names`
    all of them."""

    args: tuple[str, ...] = ()
    index: tuple[str, ...] | None = None
    blocked: tuple[str, ...] = ()
    names: tuple[str, ...] = ()


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _is_main(name) -> bool:
    """True for a receipt requirement that is this package, under any of its names."""
    return isinstance(name, str) and _normal(name) in _MAIN_NAMES


class _Flag(NamedTuple):
    option: str  # `--with`
    value: str
    from_index: bool  # a name with no source of its own: only an index can answer it


def _git_extra(item: dict) -> str | None:
    """`git+https://host/path[@ref]` for an extra recorded as a git address, or None. Only
    https with no login; at most one of rev, tag or branch (in the address or as a key); no
    subdirectory, fragment or anything else uv might record."""
    git = item["git"]
    if not isinstance(git, str) or not git.startswith(("https://", "git+https://")):
        return None
    if "#" in git:
        return None
    address, _, query = git.removeprefix("git+").partition("?")
    if not _SOURCE.fullmatch(f"git+{address}") or ".." in address:
        return None
    refs = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key not in {"rev", "tag", "branch"}:
            return None
        refs.append(value)
    refs.extend(item[key] for key in ("rev", "tag", "branch") if key in item)
    if len(refs) > 1:
        return None
    if not refs:
        return f"git+{address}"
    ref = refs[0]
    if not isinstance(ref, str) or not _REF.fullmatch(ref) or ".." in ref:
        return None
    return f"git+{address}@{ref}"


def _extra_flag(item) -> _Flag | None:
    """The option that installs one receipt requirement again, or None when it cannot be
    repeated safely."""
    if not isinstance(item, dict):
        return None
    keys = set(item)
    if "git" in keys:
        spec = _git_extra(item) if keys <= _GIT_KEYS else None
        return _Flag("--with", spec, False) if spec else None
    if keys & _LOCAL_SOURCES:
        # What is at a local path now is not what was installed: building it again would run
        # code nobody chose, so a plug-in from a path or an editable directory is not repeated.
        return None
    if not keys <= _BARE_KEYS:
        return None  # a url, an index, a marker, a subdirectory: not repeated
    requirement = item["name"]
    extras = item.get("extras")
    if extras is not None:
        if not isinstance(extras, list) or not all(
            isinstance(extra, str) and _PKG_NAME.fullmatch(extra) for extra in extras
        ):
            return None
        if extras:
            requirement += f"[{','.join(extras)}]"
    specifier = item.get("specifier")
    if specifier is not None:
        if not isinstance(specifier, str) or not _VERSION_SPEC.fullmatch(specifier):
            return None
        requirement += specifier
    return _Flag("--with", requirement, True)


def receipt_extras() -> Extras:
    """The extra packages the receipt shows this tool was installed with, and how to install
    them again with the same flags: `--with <source>` for a git address (its ref kept); `--with <name>` for a name only when the receipt shows the
    tool's own `--no-index --find-links` (an index that is not the public one); and
    `--with-executables-from <name>` where the receipt shows that package's console scripts
    were installed. Anything else is `blocked`. A missing or unreadable receipt has none."""
    try:
        with open(Path(sys.prefix) / "uv-receipt.toml", "rb") as handle:
            tool = tomllib.load(handle)["tool"]
        requirements = tool["requirements"]
        if not isinstance(requirements, list):
            return Extras()
        scripts = tool.get("entrypoints")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return Extras()
    shown: dict[str, str] = {}  # normal name -> the spelling to print and pass
    sources: dict[
        str, list[tuple[str, str]]
    ] = {}  # (flag, value) of a source of its own
    named: dict[
        str, list[str]
    ] = {}  # requirements that are a name, maybe with a version
    bad: set[str] = set()
    unnamed = False
    for item in requirements:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not _PKG_NAME.fullmatch(name):
            unnamed = True
            continue
        key = _normal(name)
        if key in _MAIN_NAMES:
            continue
        shown.setdefault(key, name)
        flag = _extra_flag(item)
        if flag is None:
            bad.add(key)
        elif flag.from_index:
            named.setdefault(key, []).append(flag.value)
        elif (flag.option, flag.value) not in sources.setdefault(key, []):
            sources[key].append((flag.option, flag.value))
    args: list[str] = []
    wants_index: list[str] = []
    for key in shown:
        if key in bad:
            continue
        found = sources.get(key, [])
        # Two sources for one package, or a version beside a source, are not repeated; the
        # bare name that `--with-executables-from` adds beside a source asks nothing more.
        if len(found) > 1 or (
            found and any(_normal(s) != key for s in named.get(key, []))
        ):
            bad.add(key)
        elif found:
            args.extend(found[0])
        else:
            wants_index.append(key)
            for spec in dict.fromkeys(named[key]):
                args.extend(("--with", spec))
    index = None
    if wants_index:
        index = _index_urls(tool.get("options"))
        if index is None:
            bad.update(wants_index)
    made = {
        _normal(entry["from"])
        for entry in (scripts if isinstance(scripts, list) else [])
        if isinstance(entry, dict) and isinstance(entry.get("from"), str)
    }
    for key, name in shown.items():
        if key in made and key not in bad:
            args.extend(("--with-executables-from", name))
    blocked = [name for key, name in shown.items() if key in bad]
    if unnamed:
        blocked.append("an unnamed package")
    return Extras(tuple(args), index, tuple(blocked), tuple(shown.values()))


def installed_commit() -> str | None:
    """The commit uv installed this tool from, when its receipt says (`?rev=<sha>` on the git
    address, or a `rev` key); None for a path install or an unreadable receipt."""
    try:
        with open(Path(sys.prefix) / "uv-receipt.toml", "rb") as handle:
            requirements = tomllib.load(handle)["tool"]["requirements"]
        for item in requirements:
            if not _is_main(item.get("name")):
                continue
            git = item.get("git")
            if not isinstance(git, str):
                continue
            rev = item.get("rev")
            if not isinstance(rev, str):
                match = re.search(r"(?:^|&)rev=([^&#]+)", urlsplit(git).query)
                rev = match.group(1) if match else None
            if rev and re.fullmatch(r"[0-9A-Za-z._/-]{1,64}", rev):
                return rev
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return None


_AUTH_FAILURE = re.compile(
    r"could not read (username|password)|authentication failed|terminal prompts disabled"
    r"|\b401\b|invalid username or password|permission denied \(publickey",
    re.IGNORECASE,
)


def _auth_failure(result) -> bool:
    """True when the install's own output says it was refused for want of a login. The output
    is only matched, never printed: it may carry a credential."""
    text = (
        f"{getattr(result, 'stderr', '') or ''}\n{getattr(result, 'stdout', '') or ''}"
    )
    return bool(_AUTH_FAILURE.search(text))


def _disabled() -> bool:
    return (envnames.get("NO_SELF_UPDATE") or "").lower() in {"1", "true", "yes", "on"}


def command_for(
    source: str | tuple[str, ...], extras: Extras | None = None
) -> list[str]:
    """The install command: the package from `source`, beside the extras the tool was
    installed with (their `--with` options, from `receipt_extras`)."""
    extras = extras or Extras()
    command = ["uv", "tool", "install", "--force", "--refresh"]
    # A tools index: only ever beside `--no-index`, so the package name cannot be answered by
    # the public index. The same goes for a bare plug-in name the receipt says came from it.
    index = source if isinstance(source, tuple) else extras.index
    if index:
        command += ["--no-index", *(a for url in index for a in ("--find-links", url))]
    command += extras.args
    command.append(f"{source}@{REF}" if isinstance(source, str) else PACKAGE)
    return command


def _say(text: str) -> None:
    sys.stderr.write(f"ream: {text}\n")


def _stamp_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "ream" / "self-update-attempt"


def _attempt_allowed() -> bool:
    """True, and records the attempt, unless one was made in the last hour. The claim is one
    atomic exclusive create (no symlink followed), so two processes cannot both win."""
    path = _stamp_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False  # cannot record it, so cannot rate-limit it: do not install
    for _ in range(2):
        try:
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            pass
        except OSError:
            return False
        else:
            os.close(fd)
            return True
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue  # gone since: claim it
        except OSError:
            return False
        if not stat.S_ISREG(info.st_mode) or time.time() - info.st_mtime < _MIN_GAP:
            return False  # recent, or not a plain file (a symlink is never followed)
        try:
            os.unlink(path)  # stale: clear it, then race fairly for the new claim
        except FileNotFoundError:
            pass
        except OSError:
            return False
    return False


def _run(command: list[str], timeout: int = _INSTALL_TIMEOUT):
    return subprocess.run(  # noqa: S603 -- argument list from the client's own source
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_install_env(),
    )


def _tool_dir() -> Path | None:
    """Where uv keeps its tools (`uv tool dir`), or None when it cannot be told."""
    try:
        where = _run(["uv", "tool", "dir"], 30)
        text = where.stdout.strip()
        if where.returncode != 0 or not text:
            return None
        return Path(text)
    except (OSError, subprocess.SubprocessError):
        return None


def _installed_level() -> int | None:
    """The level of the copy `uv tool install` just wrote, asked of ITS Python (not this
    process's modules). None when it cannot be told."""
    where = _tool_dir()
    if where is None:
        return None
    # The tool's directory is named for the package: `ream-artifacts` now, which is where an
    # update from any old name puts it. A source that still builds the old name (a fork, say)
    # leaves it under the name this process lives in, so that is tried second.
    for name in dict.fromkeys([PACKAGE, Path(sys.prefix).name or PACKAGE]):
        python = where / name / "bin" / "python"
        try:
            answer = _run(
                [
                    str(python),
                    "-I",
                    "-c",
                    "from ream import clientlevel; print(clientlevel.CLIENT_LEVEL)",
                ],
                30,
            )
            if answer.returncode == 0:
                return int(answer.stdout.strip())
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return None


def old_tools(tool_dir: Path) -> list[str]:
    """The tools uv still has under an old name of this package (`ream`, `artifacts`), found
    from their receipts in uv's tool directory. Only ones that are ours count: the receipt
    names the package and lists one of our console scripts, because `ream` is also somebody
    else's package and a tool of that name is none of ours to remove."""
    found = []
    for name in OLD_NAMES:
        try:
            with open(tool_dir / name / "uv-receipt.toml", "rb") as handle:
                tool = tomllib.load(handle)["tool"]
            requirements = tool["requirements"]
            scripts = tool.get("entrypoints") or []
            ours = any(
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and _normal(item["name"]) == name
                for item in requirements
            ) and any(
                isinstance(entry, dict) and entry.get("name") in _OUR_SCRIPTS
                for entry in scripts
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        if ours:
            found.append(name)
    return found


def _retire_old_tools(command: list[str]) -> tuple[str | None, bool]:
    """After `command` installed `ream-artifacts`, uninstall the tool this machine still has
    under an old name, so two tools do not claim the same executables. `--force` already made
    the executables the new tool's; but `uv tool uninstall` removes the executables its receipt
    lists by path, whoever they belong to now (measured on uv 0.13.5), so the new tool is
    installed once more to put them back (the same command without `--refresh`: the wheel is
    in uv's cache). Returns `(note, usable)`: a line to say when something went wrong, and
    whether the new tool's commands are in place. A failed uninstall leaves the old tool
    listed but harmless (its executables are the new tool's)."""
    where = _tool_dir()
    stale = old_tools(where) if where is not None else []
    note = None
    removed = False
    for name in stale:
        try:
            gone = _run(["uv", "tool", "uninstall", name], 60)
        except (OSError, subprocess.SubprocessError):
            gone = None
        if gone is not None and gone.returncode == 0:
            removed = True
        elif note is None:
            note = (
                f"the new tool is installed, but the old one named {name} could not be "
                f"removed. Run by hand: uv tool uninstall {name} && {' '.join(command)}"
            )
    if removed:
        again = [part for part in command if part != "--refresh"]
        try:
            ok = _run(again).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if not ok:
            line = (
                "the old tool is removed but the new one's commands were not put back. "
                f"Run by hand: {' '.join(command)}"
            )
            return line, False
    return note, True


def _host(address: str) -> str | None:
    try:
        return (urlsplit(address.removeprefix("git+")).hostname or "").lower() or None
    except ValueError:
        return None


def _manual_line(headers, api_url: str | None, extras: Extras | None = None) -> str:
    """The line for a tool with no fixed source, or whose plug-ins cannot be installed
    again. The server's note is printed for a person to run once, and only when it names the
    very host this tool was talking to and the tool has no plug-ins (a reinstall from the
    note alone would drop them); nothing here executes it. The note is a git address, so what
    it installs is named by that repository, `ream-artifacts` now."""
    extras = extras or Extras()
    line = "this tool is out of date (the service asks for a newer one). "
    if extras.blocked:
        line += (
            f"It was installed with {', '.join(extras.blocked)}, whose source cannot be "
            "repeated safely, so it is not updating itself (that would drop them). "
        )
    line += (
        "Reinstall it with the install line in the service's onboarding document "
        "(/api/v1/agents.txt)"
    )
    if extras.names:
        line += (
            ", adding your --with and --with-executables-from options for "
            f"{', '.join(extras.names)} again"
        )
    line += "."
    note = (headers.get(clientlevel.SOURCE_HEADER.lower()) or "").strip()
    if not extras.names and note and _SOURCE.fullmatch(note) and ".." not in note:
        host = _host(note)
        if host and host == _host(api_url or ""):
            line += (
                " Run this once and later updates are automatic: "
                f"uv tool install --force {note}"
            )
    return line


def on_response(method: str, headers, api_url: str | None = None) -> None:
    """Called with every API response's headers (lower-case names). Does nothing unless the
    server says this tool is out of date, and then at most once per process."""
    global _done
    if _done:
        return
    if (headers.get(clientlevel.OUTDATED_HEADER.lower()) or "").lower() != "true":
        return
    _done = True
    source = configured_source()
    extras = receipt_extras()
    if source is None or extras.blocked:
        _say(_manual_line(headers, api_url, extras))
        return
    command = command_for(source, extras)
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
    result = None
    try:
        result = _run(command)
        ok = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        if result is not None and _auth_failure(result):
            _say(
                "the update failed: the source "
                f"{source if isinstance(source, str) else ' '.join(source)} needs a login "
                f"on this machine (a credential for that host). Log in, then run by "
                f"hand: {line}"
            )
        else:
            _say(f"the update failed. Run by hand: {line}")
        return
    level = _installed_level()
    if level is None or level < clientlevel.CLIENT_LEVEL:
        _say(
            "the installed copy is older than this one (or its level cannot be read), so "
            f"this command carries on as it is. Run by hand: {line}"
        )
        return
    note, usable = _retire_old_tools(command)
    if note:
        _say(note)
    if not usable:
        return
    if method.upper() not in {"GET", "HEAD"}:
        note_mutation()
    if _mutated:
        _say("updated; your next command uses the new tool")
        return
    program = _ORIGINAL_ARGV[0] if _ORIGINAL_ARGV else ""
    if program and not os.path.isabs(program):
        # Never from the working directory: a bare name is looked up on PATH only.
        program = (shutil.which(program) or "") if os.sep not in program else ""
    if not program:
        _say("updated; run the command again to use the new tool.")
        return
    os.environ[_GUARD] = "1"
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execv(program, _ORIGINAL_ARGV)  # noqa: S606 -- our own command, once
    except OSError:
        _say("updated; run the command again to use the new tool.")
