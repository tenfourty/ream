"""Redraw a seat's pages from the tracker, for a hook to call blindly.

homelab-ij8.81. Jeremy, 20 Sep 2026: every bead status change should republish
the seat's page, and can that be automated. It can — the operator's bd wrapper
already fronts every `bd` call on this machine and can fire a debounced
republish afterwards. What it needs is ONE command per seat that it can call
knowing nothing whatsoever about that seat:

    artifacts-republish

No arguments. Every seat's hook invocation is identical; the difference lives in
`~/.config/artifacts/pages.toml`, which the seat owns:

    [[page]]
    id     = "y5sx7wwujjy5dszde6tkqgvuf3"
    render = "python3 /home/tenfourty/repos/homelab/scripts/artifacts-status.py -"

`render` writes the page's HTML to stdout; this publishes it as a new version of
`id`. Several `[[page]]` blocks are fine. The key is never in the config — it
stays where every other tool here finds it.

THREE PROPERTIES, and they are the reason this is a command rather than a line
of shell in the wrapper:

  * IDEMPOTENT FOR REAL, not by convention. `revision` is a hash of the stored
    body, so republishing an unchanged page mints no new version — the service
    enforces it, this does not have to be careful. A debounced hook firing on a
    bead that did not change the page costs one request and creates nothing.
  * IT MUST NEVER FAIL ITS CALLER. Nobody should lose a bead update because a
    page did not redraw. Every failure is caught, logged, and reported in the
    exit code; nothing propagates.
  * IT NEVER PUBLISHES A PAGE IT COULD NOT SOURCE. A render that fails, times
    out, or prints nothing publishes NOTHING. A board that briefly goes empty
    because `bd` was mid-write is worse than one a minute stale: Jeremy reads it
    as current either way, and an empty board reads as "the work stopped".
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path

from ream import client, envnames

RENDER_TIMEOUT = 120


def config_path() -> Path:
    override = envnames.get("PAGES_FILE")
    if override:
        return Path(override)
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "artifacts"
        / "pages.toml"
    )


def log_path() -> Path:
    override = envnames.get("REPUBLISH_LOG")
    if override:
        return Path(override)
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "artifacts" / "republish.log"


def _log(message: str) -> None:
    """Best effort, always. A command whose whole promise is not to disturb its
    caller must not fall over because a log directory is unwritable."""
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}\n"
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass
    print(line.rstrip(), file=sys.stderr)


def pages(path: Path) -> list[dict]:
    """The configured pages, or [] when there is no config at all.

    No config is not an error: most seats will not have one, and a hook that
    fires on every bd call across the machine must be silent for them.
    """
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    out = []
    for entry in data.get("page", []):
        if not entry.get("id") or not entry.get("render"):
            _log(f"skipped a [[page]] in {path} with no id or no render command")
            continue
        out.append(entry)
    return out


def render(command: str, key: str | None = None) -> str | None:
    """The page's HTML, or None with the reason logged.

    None covers every way this can go wrong — non-zero exit, a timeout, nothing
    on stdout, or something that is plainly not a page — because every one of
    them has the same correct response: publish nothing.

    THE RENDER NEEDS THE KEY TOO, AND THE DESCRIPTOR IS READ-ONCE. A seat's
    render command is usually not a pure function of the tracker: this seat's
    status board asks the service what is still waiting on Jeremy and what he
    has answered, with the same key this command publishes under. `artifacts-key`
    hands that key over on a PIPE, which yields it exactly once — so whichever
    of the two halves reads $REAM_KEY_FD first wins, and the other gets an
    empty string. Publishing with an empty bearer token is a 401 that reads
    exactly like a revoked key; rendering without one is worse, because the
    board still renders — with "I could not check what is waiting on you" where
    his open questions belong, and "I could not list what you have settled"
    where his own words belong. Measured 22 Sep 2026: that is precisely the page
    a bare render produces.

    So the key is resolved ONCE by main and each render gets its own private
    pipe carrying a copy: never a command-line argument, never an environment
    variable holding the key itself, only a descriptor number — the same
    discipline `artifacts-key` follows.
    """
    child_env = dict(os.environ)
    read_fd: int | None = None
    if key:
        read_fd, write_fd = os.pipe()
        with os.fdopen(write_fd, "w", encoding="utf-8") as handle:
            handle.write(key + "\n")
        os.set_inheritable(read_fd, True)
        envnames.child_env(child_env, "KEY_FD", str(read_fd))
    else:
        # Never leave a descriptor number pointing at a pipe this process has
        # already drained: the renderer would read nothing and quietly publish
        # a page with its service-backed sections missing.
        envnames.child_env(child_env, "KEY_FD", None)

    try:
        done = subprocess.run(  # noqa: S602 -- the command is the seat's own config
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=RENDER_TIMEOUT,
            check=False,
            env=child_env,
            pass_fds=() if read_fd is None else (read_fd,),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"render failed to start: {command!r}: {exc}")
        return None
    finally:
        if read_fd is not None:
            os.close(read_fd)
    if done.returncode != 0:
        _log(
            f"render exited {done.returncode}: {command!r}: {done.stderr.strip()[:300]}"
        )
        return None
    body = done.stdout
    if not body.strip():
        _log(f"render printed nothing: {command!r}")
        return None
    # A render that prints a traceback, a usage message or a log line would
    # otherwise be published as the page. Cheap, and it only has to catch the
    # plausible mistakes rather than be a parser.
    if "<html" not in body[:4096].lower():
        _log(f"render did not print a page (no <html> near the top): {command!r}")
        return None
    return body


def republish_one(entry: dict, key: str) -> bool:
    body = render(entry["render"], key)
    if body is None:
        return False
    # _update reads from a path, so the rendered body goes to a temporary file
    # rather than reaching into the client's internals. TemporaryDirectory
    # rather than NamedTemporaryFile: the directory cleans itself up on every
    # path out of this function, including the ones that raise.
    with tempfile.TemporaryDirectory(prefix="artifacts-republish-") as workdir:
        page = Path(workdir) / "page.html"
        page.write_text(body, encoding="utf-8")
        try:
            before = client._read_current(entry["id"], key)["revision"]
            result = client._update(str(page), entry["id"], key)
        except Exception as exc:  # noqa: BLE001 -- nothing may propagate to the caller
            _log(f"{entry['id']}: publish failed: {type(exc).__name__}: {exc}")
            return False
    after = result.get("revision")
    if after == before:
        _log(f"{entry['id']}: unchanged, no new version")
    else:
        _log(f"{entry['id']}: republished as {after}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Republish this seat's pages on Ream (formerly artifacts) from their render commands."
    )
    parser.add_argument(
        "--config", help="override ~/.config/artifacts/pages.toml", default=None
    )
    # The same two the other commands take, for the same reason: this one read
    # the key FILE and nothing else until 22 Sep 2026, so the day the keys were
    # vaulted (21 Sep) it stopped working on this machine altogether — under
    # `artifacts-key`, with a perfectly good key on fd 3, it logged "cannot read
    # key file ~/.config/artifacts/key" and published nothing. It is called by a
    # hook that must never fail its caller, so the failure was silent, and this
    # seat's board simply went stale.
    parser.add_argument("--key-file", default=None)
    parser.add_argument("--key-fd", type=int, default=None)
    args = parser.parse_args(argv)

    path = Path(args.config) if args.config else config_path()
    try:
        entries = pages(path)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _log(f"could not read {path}: {exc}")
        return 1
    if not entries:
        return 0  # nothing configured: silent, so a machine-wide hook costs nothing

    try:
        key = client._resolve_key(args)
    except Exception as exc:  # noqa: BLE001
        _log(f"no usable key: {type(exc).__name__}: {exc}")
        return 1

    failed = sum(0 if republish_one(entry, key) else 1 for entry in entries)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
