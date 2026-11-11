"""The command-line side of the plug-in API (core-and-plugins design, 4.2 item 6, 7b part 4).

Standard library only (and `envnames`): the `ream` command and the other tools import it, and
a client machine has not installed the server stack.

Two things live here:

* the registry behind `api.cli(name, main, help)` and `api.client_defaults(api_url=...,
  session_envs=[...], news=[...])`. The server's `PluginAPI` and the CLI's `CliPluginAPI`
  both write to it, so a bad registration is refused the same way in either process;
* the loader the CLIs run (`load_or_exit`): the same selection as the server's
  (`REAM_PLUGINS` / `ARTIFACTS_PLUGINS`: unset = all, `none` = none, `a,b` = only those, an
  unknown name stops), handing each plug-in a `CliPluginAPI`, whose server registrations
  (`sink`, `answer_source`, `on_answer`, `scanner`, `mail_backend`) are accepted and ignored,
  so one `register` function serves both processes.

A plug-in's `register` runs in the command-line process on EVERY start of every tool, with the
caller's rights (it can read the key file and the environment): it must be cheap and free of side
effects (no network, no file writes, no subprocess); it only declares.

A fault here never carries a value or exception text: errors name the plug-in and, for an
exception, its type only.
"""

from __future__ import annotations

import inspect
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib import metadata
from urllib.parse import urlsplit

from ream import envnames

API_VERSION = (1, 3)
GROUP = "ream.plugins"
# The old group name, read for good beside the new one.
OLD_GROUP = "artifacts.plugins"

_log = logging.getLogger(__name__)

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_NEWS_ID = re.compile(r"^\d{4}-\d{2}-\d{2}[a-z]?$")
# An environment variable that holds a credential is never a session label.
_SECRETISH = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
MAX_HELP = 200
MAX_NEWS = 20
MAX_NEWS_TEXT = 2000
MAX_SESSION_ENVS = 8
# What a session label taken from a plug-in's environment variable must look like.
SESSION_LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
HINT = "set REAM_PLUGINS=none to load none, or name the ones you want"


class PluginError(Exception):
    """A plug-in could not be loaded or registered. The message names it, never a value."""


@dataclass(frozen=True)
class Command:
    plugin: str
    name: str
    main: Callable
    help: str


_commands: dict[str, Command] = {}
_api_url: tuple[str, str] | None = None  # (plug-in, url)
_session_envs: list[str] = []
_news: list[tuple[str, str, str]] = []  # (plug-in, id, text)
_loaded_for_cli = False


def reset() -> None:
    """Forget every registration (tests)."""
    global _api_url, _loaded_for_cli
    _commands.clear()
    _session_envs.clear()
    _news.clear()
    _api_url = None
    _loaded_for_cli = False


def _snapshot() -> tuple:
    return (dict(_commands), _api_url, list(_session_envs), list(_news))


def _restore(snap: tuple) -> None:
    global _api_url
    commands_, _api_url, envs, lines = snap
    _commands.clear()
    _commands.update(commands_)
    _session_envs[:] = envs
    _news[:] = lines


def reserved_verbs() -> set[str]:
    """The verbs the core owns: no plug-in may take one."""
    from ream import cli  # late: cli imports this module

    return set(cli._OTHER) | set(cli.DECIDE_VERBS) | {"help"}


def commands() -> list[Command]:
    """The plug-in verbs, in name order."""
    return [_commands[n] for n in sorted(_commands)]


def command(name: str) -> Command | None:
    return _commands.get(name)


def default_api_url() -> str | None:
    """The endpoint a plug-in supplies for a machine that sets none, or None."""
    return _api_url[1] if _api_url else None


def session_envs() -> list[str]:
    """Environment variables whose value is the office part of a default session label,
    asked in order before the core's own."""
    return list(_session_envs)


def news() -> list[tuple[str, str]]:
    """The plug-ins' news entries, `(id, text)` as `news.NEWS` has them."""
    return [(i, t) for _p, i, t in _news]


def news_by_plugin() -> list[tuple[str, str, str]]:
    """The plug-ins' news entries as `(plug-in, id, text)`, newest first: the CLI keeps what
    it has shown per plug-in."""
    return list(_news)


def session_label(value: str | None) -> str | None:
    """`value` (a plug-in's environment variable) as a session-label office, lower-cased, or
    None when it is not a plain label."""
    label = (value or "").strip().lower()
    return label if SESSION_LABEL.fullmatch(label) else None


def _news_id_ok(day: str) -> bool:
    """A real date, not later than today (UTC): a future id would hide the core's later
    entries behind it."""
    try:
        when = date.fromisoformat(day[:10])
    except ValueError:
        return False
    return when <= datetime.now(UTC).date()


def register_cli(plugin: str, name: str, main: Callable, help: str) -> None:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(f"plug-in {plugin!r}: cli name must match {_NAME.pattern}")
    if name in reserved_verbs():
        raise ValueError(f"plug-in {plugin!r}: cli name {name!r} is reserved")
    if name in _commands:
        raise ValueError(f"plug-in {plugin!r}: cli name {name!r} is already taken")
    if not callable(main):
        raise TypeError(f"plug-in {plugin!r}: cli {name!r} main is not callable")
    if (
        not isinstance(help, str)
        or not help.strip()
        or len(help) > MAX_HELP
        or not help.isprintable()
    ):
        raise ValueError(
            f"plug-in {plugin!r}: cli {name!r} help must be one printable line"
            f" of at most {MAX_HELP} characters"
        )
    _commands[name] = Command(plugin, name, main, help.strip())


def _checked_api_url(plugin: str, api_url: object) -> str:
    """`api_url` under the rules the request path enforces (https, or http to a loopback host
    only), with a valid port and no userinfo, query or fragment; a trailing "/" is dropped."""
    from ream import client  # late: client imports this module

    if not isinstance(api_url, str):
        raise ValueError(f"plug-in {plugin!r}: api_url must be a string")  # noqa: TRY004 -- one error type for every bad registration
    try:
        parts = urlsplit(api_url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise ValueError(f"plug-in {plugin!r}: api_url is not a valid URL") from None
    if (
        parts.scheme not in ("http", "https")
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or (port is not None and port == 0)
        or not api_url.isprintable()
    ):
        raise ValueError(f"plug-in {plugin!r}: api_url is not a plain http(s) URL")
    try:
        client._check_url_scheme(api_url)
    except client.UsageError:
        raise ValueError(
            f"plug-in {plugin!r}: api_url must be https, or http to a loopback host"
        ) from None
    return api_url.rstrip("/")


def register_client_defaults(
    plugin: str,
    api_url: str | None = None,
    session_envs: list[str] | tuple[str, ...] = (),
    news: list[tuple[str, str]] | tuple[tuple[str, str], ...] = (),
) -> None:
    """Validate everything, then record it: a refused call changes nothing. `api_url` is
    kept without a trailing "/"; each news id must be a real date not later than today (UTC)
    and each text one printable line."""
    global _api_url
    if api_url is not None:
        api_url = _checked_api_url(plugin, api_url)
        if _api_url is not None:
            raise ValueError(
                f"plug-in {plugin!r}: api_url is already supplied by plug-in"
                f" {_api_url[0]!r}"
            )
    envs = list(session_envs)
    if len(envs) > MAX_SESSION_ENVS:
        raise ValueError(f"plug-in {plugin!r}: too many session_envs")
    for env in envs:
        if (
            not isinstance(env, str)
            or not _ENV_NAME.fullmatch(env)
            or any(word in env for word in _SECRETISH)
        ):
            raise ValueError(f"plug-in {plugin!r}: a session_envs name is not allowed")
    lines = list(news)
    if len(lines) > MAX_NEWS:
        raise ValueError(f"plug-in {plugin!r}: too many news entries")
    for item in lines:
        if (
            not isinstance(item, (tuple, list))
            or len(item) != 2
            or not isinstance(item[0], str)
            or not _NEWS_ID.fullmatch(item[0])
            or not isinstance(item[1], str)
            or not _news_id_ok(item[0])
            or not item[1].strip()
            or len(item[1]) > MAX_NEWS_TEXT
            or not item[1].strip().isprintable()
        ):
            raise ValueError(
                f"plug-in {plugin!r}: news entries are (YYYY-MM-DD[a-z], text), the date"
                f" not later than today, text one printable line of at most"
                f" {MAX_NEWS_TEXT} characters"
            )
    if api_url is not None:
        _api_url = (plugin, api_url)
    for env in envs:
        if env not in _session_envs:
            _session_envs.append(env)
    _news.extend((plugin, str(i), str(t).strip()) for i, t in lines)
    _news.sort(key=lambda e: e[1], reverse=True)


def requires_major(register: Callable) -> int | None:
    module = inspect.getmodule(register)
    declared = getattr(module, "REQUIRES_API", None)
    if declared is None:
        return None
    return int(str(declared).split(".")[0])


def entry_points() -> list:
    """The installed `ream.plugins` and (for good) `artifacts.plugins` entry points. Where
    one name is in both groups the `ream.plugins` one wins."""
    found = {ep.name: ep for ep in metadata.entry_points(group=OLD_GROUP)}
    found.update({ep.name: ep for ep in metadata.entry_points(group=GROUP)})
    return list(found.values())


def register_all(
    eps: list,
    wanted: str,
    make_api: Callable[[str], object],
    log: logging.Logger,
    warn: Callable[[str], None] | None = None,
) -> list[str]:
    """Register every plug-in `wanted` (the REAM_PLUGINS value) selects, once, in name
    order, each with `make_api(name)`. Raises PluginError (naming the plug-in) and loads
    nothing further on a failure. Shared by the server and the CLIs. With `warn` (the CLIs, when
    the selection is the default), a plug-in that fails is skipped instead: its partial
    registrations are undone and `warn(message)` is called; the server never passes it."""
    wanted = wanted.strip()
    found = {ep.name: ep for ep in eps}
    if wanted.lower() == "none":
        names: list[str] = []
    elif wanted:
        names = [n.strip() for n in wanted.split(",") if n.strip()]
        for name in names:
            if name not in found:
                raise PluginError(
                    f"REAM_PLUGINS names plug-in {name!r}, which is not installed"
                )
        names = sorted(set(names))
    else:
        names = sorted(found)
    done: list[str] = []
    for name in names:
        snap = _snapshot() if warn else None
        try:
            register = found[name].load()
            major = requires_major(register)
            if major is not None and major != API_VERSION[0]:
                raise PluginError(
                    f"plug-in {name!r} requires plug-in API {major}, this core speaks"
                    f" {API_VERSION[0]}"
                )
            register(make_api(name))
        except (PluginError, Exception, SystemExit, KeyboardInterrupt) as exc:
            # SystemExit and KeyboardInterrupt too: a plug-in cannot end the tool from
            # inside `register`. Only the type is ever reported.
            if isinstance(exc, PluginError):
                error = exc
            else:
                error = PluginError(
                    f"plug-in {name!r} failed to register: {type(exc).__name__}"
                )
            if warn is None:
                if error is exc:
                    raise
                raise error from exc
            _restore(snap)
            warn(f"ream: skipped: {error}; {HINT}")
            continue
        done.append(name)
        log.info("plug-in %s registered", name)
    return done


class CliPluginAPI:
    """What a plug-in's `register` is handed in a command-line process. `cli` and
    `client_defaults` work; the server's registrations are accepted and ignored."""

    version = API_VERSION

    def __init__(self, plugin: str) -> None:
        self._plugin = plugin

    def cli(self, name: str, main: Callable, help: str) -> None:
        register_cli(self._plugin, name, main, help)

    def client_defaults(
        self,
        api_url: str | None = None,
        session_envs: list[str] | tuple[str, ...] = (),
        news: list[tuple[str, str]] | tuple[tuple[str, str], ...] = (),
    ) -> None:
        register_client_defaults(self._plugin, api_url, session_envs, news)

    def sink(self, *args, **kwargs) -> None:
        return None

    def answer_source(self, *args, **kwargs) -> None:
        return None

    def on_answer(self, *args, **kwargs) -> None:
        return None

    def scanner(self, *args, **kwargs) -> None:
        return None

    def mail_backend(self, *args, **kwargs) -> None:
        return None

    def schema(self, *args, **kwargs) -> None:
        return None

    def on_start(self, *args, **kwargs) -> None:
        return None

    def on_stop(self, *args, **kwargs) -> None:
        return None

    def read_side(self, *args, **kwargs) -> None:
        return None

    def text_vars(self, *args, **kwargs) -> None:
        return None

    def text_section(self, *args, **kwargs) -> None:
        return None

    def router(self, *args, **kwargs) -> None:
        return None


def load_for_cli() -> list[str]:
    """Register the selected plug-ins in this command-line process, once. Raises
    PluginError."""
    global _loaded_for_cli
    if _loaded_for_cli:
        return []
    # Set first: a failure stops the process, and a retry would double-register.
    _loaded_for_cli = True
    wanted = envnames.get("PLUGINS") or ""
    # The default selection (unset = all) skips a broken plug-in with a warning, so one bad
    # install does not stop every tool; naming plug-ins makes a failure stop.
    warn = None if wanted.strip() else lambda line: sys.stderr.write(f"{line}\n")
    return register_all(entry_points(), wanted, CliPluginAPI, _log, warn)


def load_or_exit() -> None:
    """`load_for_cli`, or exit 2 with the message (it names the plug-in, no value) and the
    hint; two settings that disagree (envnames.EnvConflict) exit 2 with their value-free
    message and no traceback."""
    try:
        load_for_cli()
    except PluginError as exc:
        sys.stderr.write(f"{exc}\n{HINT}\n")
        raise SystemExit(2) from None
    except envnames.EnvConflict as exc:
        sys.stderr.write(f"{exc}\n")
        raise SystemExit(2) from None
