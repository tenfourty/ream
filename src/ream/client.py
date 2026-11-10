"""`artifacts-publish`: so an agent can publish a page with one command,
anywhere the `artifacts` package is installed. Standard library only
(`urllib.request`, `json`, `argparse`) -- no third-party HTTP client to add
just to POST a page.

The key is read from a file and sent ONLY as `Authorization: Bearer …`. It is
never printed, never logged, and never appears in an exception message --
every error path below carries a status code and the server's own JSON
`detail`, never the key or the request that carried it.
"""

import argparse
import http.client
import ipaddress
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from ream import (
    clientlevel,
    csp,
    envnames,
    news,
    plugin_cli,
    selfupdate,
    settings,
    textvars,
    traps,
)
from ream.cli import prog_name

_DEFAULT_KEY_FILE = "~/.config/artifacts/key"

# Exactly what `keys.mint`/`artifacts-admin new-key` produce: "art_" plus a
# urlsafe-base64 token, never shorter than this. Anything else read from a key
# file is refused before it ever reaches a header -- see `_read_key`.
_KEY_SHAPE = re.compile(r"^art_[A-Za-z0-9_-]{20,}$")

_REQUEST_TIMEOUT = 30

# The environment variable a seat sets to say WHICH SESSION it is, when several
# seats on one machine share one key (homelab-ij8.32).
#
# The service cannot work this out and neither can this file. In Claude Code the
# real session id reaches a HOOK as `session_id` on stdin; the environment
# variable CLAUDE_CODE_SESSION_ID is NOT equivalent, because a forked subagent
# inherits its parent's and attribution taken from it would credit a parent for
# its children's work. So the caller declares it, and this only carries it.
#
# The shape worth using is "<seat>/<short id>" -- e.g. artifacts-seat/5ed2e1c5.
# Jeremy asked for "some sort of uuid", and a uuid alone is unique but unreadable
# on a page; a name alone is readable but not unique. Both costs nothing.
#
# NOT A CREDENTIAL. It is sent in the clear as an ordinary header, anyone holding
# the key can send any value, and nothing on the far side verifies it. Never put
# anything in here that would matter if it were read.
_SESSION_ENV = "REAM_SESSION"
_SESSION_HEADER = "X-Ream-Session"
# Sent beside it for good: a service that predates the rename reads only this one.
_SESSION_HEADER_OLD = "X-Artifacts-Session"

# Our own name on every request, never urllib's default. Measured through the
# Cloudflare edge on 23 Sep 2026, the morning the service went on the internet:
# Browser Integrity Check answers `Python-urllib/*` with 403 "error code: 1010"
# and lets any other agent string through. Without this, every agent outside
# the house using these commands was turned away before reaching the service.
_USER_AGENT = "artifacts-client/1"


def _add_client_level(request: urllib.request.Request) -> None:
    """Say which generation of the tools this is on every call (clientlevel.py), under both
    header names. A number, not a credential."""
    value = str(clientlevel.CLIENT_LEVEL)
    request.add_unredirected_header(clientlevel.CLIENT_HEADER, value)
    request.add_unredirected_header(
        clientlevel.old_name(clientlevel.CLIENT_HEADER), value
    )


def declared_session(explicit: str | None = None) -> str | None:
    """What this invocation calls itself, or None. `--session` beats the
    environment, and neither being set is a perfectly ordinary answer."""
    value = (explicit or envnames.get("SESSION") or "").strip()
    return value or None


def default_session() -> str | None:
    """A label read from the agent tool's own environment, for a session that
    declared none (runcove-5t3oj.25): "<office>/<first 8 of the Claude Code
    session id>". The office is quarterdeck's QD_OFFICE, else the artifacts-key
    seat with "_" as "-". Asked of every office on 22 Oct 2026
    (docs/agent/feedback/2026-10-22-review-scope-and-session-names.md): three
    said their pages had gone out unlabelled. None when there is no session id
    or no office, so the caller still warns."""
    ref = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()[:8]
    # A plug-in's `client_defaults(session_envs=[...])` names are asked first, then the
    # house's own (kept in the core until the house plug-in exists, design step 9).
    office = ""
    for name in plugin_cli.session_envs():
        # A value that is not a plain label is ignored and the next source is tried.
        office = plugin_cli.session_label(os.environ.get(name)) or ""
        if office:
            break
    if not office:
        office = os.environ.get("QD_OFFICE", "").strip()
    if not office:
        office = (envnames.get("SEAT") or "").replace("_", "-")
    if not ref or not office or "/" in office:
        return None
    return f"{office}/{ref}"


def session_label(explicit: str | None = None) -> str | None:
    """The label this process sends: the declared one, else the default.

    Only what is SENT. Which office a waiter or a comment list listens for is
    still `declared_session` alone: quarterdeck's office names ("artifacts",
    "quarterdeck") differ from labels offices already publish under
    ("artifacts-maintainer", "qd-maintainer"), so a default there would quietly
    stop an unlabelled waiter hearing its own pages' answers."""
    return declared_session(explicit) or default_session()


# Set once, by `main`, from `--session` or the environment. A module-level value
# rather than an argument threaded through eleven helpers: this is a one-shot
# process whose identity does not change halfway through, and the alternative was
# touching every call site for a header none of them cares about -- which is how
# one of them gets missed and a single route silently stops attributing.
_session: str | None = None


def use_session(value: str | None) -> None:
    # Process-wide identity, set once at startup -- see `_session` above.
    global _session
    _session = value


def warn_if_unlabelled(what: str) -> None:
    """One line on stderr when something is written with no session label
    (homelab-ij8.106). The first outside agent lost its label between Claude
    Code shell calls and got "no attribution, with no warning". stderr only, so
    no caller that parses stdout is affected; never refuses."""
    if _session is None:
        print(
            f"note: this {what} carries no session label, so "
            + textvars.words("{{owner_their}} record will not say ")
            + f"which session did it. Set {_SESSION_ENV} (in Claude Code, in "
            '~/.claude/settings.json under "env") or pass --session.',
            file=sys.stderr,
        )


class UsageError(Exception):
    """A problem with how the command was invoked or its key file -- exit 2."""


class Conflict(Exception):
    """Somebody else published between this caller's read and its write -- exit 1.

    NOT a UsageError: the command was invoked correctly and nothing about it was
    wrong; the world changed underneath it. Exit 2 would tell a calling script
    "you used this tool wrong", which is untrue and the wrong thing to branch
    on -- a wrapper retrying after a genuine conflict is sensible, a wrapper
    retrying after a usage error is a loop.
    """


class HTTPFailure(Exception):
    """The server answered with a non-2xx status, or a redirect was refused --
    exit 1. `detail` is whatever the server's JSON body carried under "detail"
    (a string, or a structured refusal like `{"refused": [...]}`); the server
    never echoes submitted content back, so this is always safe to print.

    THE TEXT MATTERS AS MUCH AS THE EXIT CODE. Until 22 Sep 2026 this stringified
    to a bare "HTTP 403" and the detail -- which the service writes carefully,
    naming the seat the key authenticates as and the grant it lacks -- was
    carried and never printed. The operator ran a cross-seat "what is stranded"
    check, got that line and no rows, and correctly observed that it reads as an
    empty queue: a refusal printed where rows would be is indistinguishable from
    "I looked and found nothing", which is the failure this house has already
    paid for once. So a 401 or 403 says REFUSED in words, says it is not an
    empty result, and carries the server's explanation with it. The status
    number stays in the string: callers grep for it."""

    def __init__(self, status: int, detail, body: dict | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.detail = detail
        # The whole JSON body when it was an object, for the few callers that
        # branch on a structured refusal (a 409 naming who holds a review
        # round). Never printed by __str__.
        self.body = body

    def __str__(self) -> str:
        head = f"HTTP {self.status}"
        if self.status in (401, 403):
            head = (
                f"REFUSED (HTTP {self.status}) -- this is NOT an empty result, "
                "it is a refusal to look"
            )
        return f"{head}: {self.detail}" if self.detail else head


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A 3xx is treated as a hard error, never followed. `urlopen`'s default
    handler happily replays every header -- Authorization included -- onto
    whatever host and scheme the Location line names; `add_unredirected_header`
    below (belt) already keeps this handler (braces) from ever mattering, but
    a redirect is refused outright regardless of whether it would have carried
    the key, since it is never something this client has any business
    following."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urlsplit(newurl).hostname or newurl
        raise HTTPFailure(code, f"refused a redirect to {host}")


# Built once: a fresh opener per request would still refuse redirects the same
# way, but there is no reason to rebuild it every call.
_opener = urllib.request.build_opener(_RefuseRedirects)


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_url_scheme(url: str) -> None:
    """https is always fine. http is fine ONLY to a loopback host (127.0.0.0/8,
    ::1, localhost) -- exactly what this project's own tests point at -- so a
    misconfigured REAM_API that ends up plain http:// to a real host can
    never send the key in cleartext, or at all, to that host."""
    parsed = urlsplit(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and _is_loopback_host(parsed.hostname or ""):
        return
    raise UsageError(
        f"refusing to use {parsed.scheme}:// for {parsed.hostname!r} -- "
        "only https, or http to a loopback host, is allowed"
    )


def _api_base() -> str:
    # REAM_API wins; else a plug-in's `client_defaults(api_url=...)`; else the house's
    # address, kept in the core until the house plug-in exists (design step 9).
    if not settings.is_set("api_url"):
        supplied = plugin_cli.default_api_url()
        if supplied:
            return supplied
    return settings.current().api_url


def _key_file_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env = envnames.get("KEY_FILE")
    if env:
        return Path(env)
    return Path(_DEFAULT_KEY_FILE).expanduser()


# Set by the key wrapper to the descriptor it opened. It carries the NUMBER,
# never the key, so it is not a secret and /proc/<pid>/environ tells a reader
# nothing they could use. This exists so one wrapper can serve a command it
# does not have to know the argument order of -- `artifacts-key -- <anything>`.
_KEY_FD_ENV = "REAM_KEY_FD"


def _resolve_key(args) -> str:
    """The key, from a descriptor or a file -- never quietly from both.

    Refusing the combination rather than preferring one: a caller that passed
    both meant something, and silently honouring the other is how a wrapper
    ends up reading the cleartext file it was written to stop using, with
    everything appearing to work. That is the whole failure ij8.34 is about,
    so it fails loudly instead.
    """
    env_fd = envnames.get("KEY_FD") or ""
    if args.key_fd is not None and args.key_file:
        raise UsageError(
            "--key-fd and --key-file are two answers to the same question; "
            "pass one. (A wrapper that sets both is probably still falling "
            "back to the file it meant to replace.)"
        )
    if env_fd and args.key_file:
        raise UsageError(
            f"${_KEY_FD_ENV} is set (a wrapper opened a key for this command) "
            "but --key-file was passed too. Pass one: honouring the file here "
            "would read the cleartext key the wrapper exists to stop using."
        )
    if args.key_fd is not None:
        return _read_key_fd(args.key_fd)
    if env_fd:
        try:
            number = int(env_fd)
        except ValueError:
            raise UsageError(
                f"${_KEY_FD_ENV} is {env_fd!r}, which is not a descriptor number"
            ) from None
        return _read_key_fd(number)
    return _read_key(_key_file_path(args.key_file))


# Checked against the key FILE's own age, so an ordinary command costs nothing:
# only a file older than this asks the service whether it is time to rotate.
_ROTATE_CHECK_AFTER_SECONDS = 7 * 24 * 3600


def _key_file_in_use(args) -> Path | None:
    """The key file this invocation read, or None when the key came from a
    descriptor (a vaulted house key the tools must never try to rewrite)."""
    if args.key_fd is not None or envnames.get("KEY_FD"):
        return None
    return _key_file_path(args.key_file)


def save_key_file(path: Path, key: str) -> None:
    """Owner-only from the first byte, then one atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".key-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(key + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def maybe_rotate(args, key: str) -> str:
    """Replace this machine's key if it is due (homelab-ij8.34, Jeremy's (3a)):
    keys from artifacts-connect swap themselves weekly, and one never swapped is
    refused after two weeks. Returns the key to use from here on. Never prints a
    key. A failure to rotate never fails the command the agent actually ran --
    the old key still works until the new one is first used."""
    path = _key_file_in_use(args)
    if path is None:
        return key
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return key
    if age < _ROTATE_CHECK_AFTER_SECONDS:
        return key
    base = _api_base()
    try:
        me = _request("GET", f"{base}/me", key, None)
        if not me.get("rotating"):
            return key
        fresh = _request("POST", f"{base}/keys/rotate", key, {})
        new_key = fresh.get("key")
        if not isinstance(new_key, str) or not new_key.startswith("art_"):
            return key
        save_key_file(path, new_key)
    except (HTTPFailure, UsageError, OSError) as exc:
        print(
            f"note: this machine's weekly key swap did not happen ({exc}); "
            "the current key still works, and it will try again",
            file=sys.stderr,
        )
        return key
    print(
        "note: this machine's key was replaced, as it is every week; the new one "
        f"is in {path} and was not shown",
        file=sys.stderr,
    )
    return new_key


def _read_key_fd(number: int) -> str:
    """The key from an already-open file descriptor, so it is never on disk.

    homelab-ij8.34. Four bearer tokens sat in cleartext in ~/.config/artifacts/,
    and mode 600 is real protection against another user and none at all
    against a stray read. The vaulted key lives in sops; a wrapper on the
    machine that holds the age recipient extracts ONE entry and hands it
    straight here:

        artifacts-publish --key-fd 3 page.html 3< <(sops -d --extract ... FILE)

    A DESCRIPTOR RATHER THAN A TEMPORARY FILE, which was my first proposal and
    the operator's improvement on it: with no file there is nothing to shred,
    no window in which the plaintext exists on disk, and no trap that has to
    fire to clean up. It beats an environment variable for the older reason --
    /proc/<pid>/environ and `ps e` are readable, and an exported variable
    outlives the command it was meant for.

    THE MODE CHECK HAS NO EQUIVALENT HERE and its absence is not an oversight:
    a descriptor has no permissions of its own, and whoever opened it decided
    who may read it. That is the wrapper's job and it is a smaller thing to get
    right than a file's lifetime. Everything else -- the shape check, never
    printing the value, never putting it in an exception -- is identical.
    """
    # 1 and 2 are where this process WRITES. Reading a key from a stream meant
    # for output is always a mistake, and a silent one: it would block, or read
    # nothing, and the shape check would then blame the key.
    if number in (1, 2):
        raise UsageError(
            f"--key-fd {number} is this process's own output stream; "
            "pass a descriptor you opened for reading (3 is conventional)"
        )
    if number < 0:
        raise UsageError("--key-fd must be a file descriptor number")
    try:
        # closefd=False: the descriptor belongs to the caller, which may have
        # its own plans for it. Reading it to the end is enough.
        with os.fdopen(number, encoding="utf-8", newline="", closefd=False) as handle:
            raw = handle.read()
    except OSError as exc:
        raise UsageError(
            f"cannot read --key-fd {number}: {exc.strerror}. Nothing was opened "
            "on that descriptor -- with bash, `3< <(...)` opens 3 for the "
            "command it is attached to."
        ) from None
    except UnicodeDecodeError as exc:
        raise UsageError(f"--key-fd {number} did not carry valid text") from exc
    content = raw.removesuffix("\n")
    if not _KEY_SHAPE.fullmatch(content):
        # Never the content. A truncated read and a wrong key look the same
        # from here, and saying which would mean showing it.
        raise UsageError(
            f"--key-fd {number} did not carry a valid agent key "
            f"({len(content)} characters read)"
        )
    return content


def _read_key(path: Path) -> str:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        # SAY WHERE A KEY COMES FROM NOW. The cleartext files under
        # ~/.config/artifacts/ are being shredded seat by seat as they move
        # into the vault (homelab-ij8.34), so from here on the commonest
        # reason this file is missing is that it is GONE ON PURPOSE -- and
        # "no such file" alone sends the reader looking for a lost key, or
        # minting a second one for an identity that already exists.
        #
        # ARTIFACTS_SEAT set on a bare command is the commonest way here: the
        # seat is read by the `artifacts-key` wrapper, never by this program,
        # so name the exact command the caller meant (homelab, 4 Nov 2026).
        seat = envnames.get("SEAT") or ""
        prog = Path(sys.argv[0]).name or "artifacts-publish"
        if seat:
            raise UsageError(
                f"cannot read key file {path}: {exc.strerror}. ARTIFACTS_SEAT "
                f"is {seat!r}, but only the `artifacts-key` wrapper reads it, "
                "not this command: run "
                f"`ARTIFACTS_SEAT={seat} artifacts-key -- {prog} …`."
            ) from None
        raise UsageError(
            f"cannot read key file {path}: {exc.strerror}. Where keys are "
            "vaulted, run the command through "
            "`ARTIFACTS_SEAT=<seat> artifacts-key -- …`; `artifacts-key "
            "--help` lists the seats. Elsewhere, point --key-file or "
            "--key-fd at your own store."
        ) from None
    if mode & 0o077:
        raise UsageError(
            f"key file {path} grants group or other permission -- chmod 600 it"
        )
    # `newline=""` disables universal-newline translation: a file ending in "\r\n"
    # must stay "\r\n" here, so that stripping exactly one trailing "\n" below
    # leaves the "\r" behind -- and the shape check catches it, rather than a
    # silent read turning it into a clean "\n" first.
    try:
        # `Path.read_text()` has no `newline=` parameter before Python 3.13 (this
        # project pins 3.12) -- `Path.open()` does, so read through it directly.
        with path.open(encoding="utf-8", newline="") as handle:
            raw = handle.read()
    except UnicodeDecodeError as exc:
        raise UsageError(f"key file {path} is not valid text") from exc
    content = raw.removesuffix("\n")
    if not _KEY_SHAPE.fullmatch(content):
        # Never the content, whatever it is -- a wrong key is exactly as likely
        # to be a real key with a typo as it is to be garbage.
        raise UsageError(f"key file {path} is not a valid agent key")
    return content


# A rolling deploy is a normal event here, not an outage: the front door answers
# 502/503/504 for about a second while the new copy of the service takes over.
_ROLLING_STATUSES = frozenset({502, 503, 504})
_ROLLING_ATTEMPTS = 4
_ROLLING_BACKOFF = 1.5


def _json_object(raw: bytes) -> dict | None:
    """An error body parsed, when it is a JSON object; None otherwise."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _detail(raw: bytes, headers=None) -> str:
    """What to SAY about an error body, which is not the same as showing it.

    An error from the service is JSON with a `detail` a person should read. An
    error from anything in front of it is an HTML page that was never addressed
    to an agent, and decoding it into a message puts bytes from the network --
    including any control characters in them -- straight into a terminal and a
    transcript. Say what it was, not what it contained.

    WHO answered matters as much (23 Sep 2026). With the service on the
    internet behind Cloudflare's bot protection, an agent can be stopped before
    its request ever reaches the service, and "the service answered" would send
    whoever reads it looking in the wrong place. Cloudflare marks a challenge
    with `cf-mitigated`, and signs its own pages with `server: cloudflare`.
    """
    headers = headers or {}
    if (headers.get("cf-mitigated") or "").lower() == "challenge":
        return (
            "Cloudflare's bot protection challenged this request before it reached "
            "the service -- nothing was looked up. An agent cannot pass a browser "
            + textvars.words(
                "challenge: ask {{owner_name}} to exempt agent calls to /api/v1/"
            )
        )
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        if (headers.get("server") or "").lower() == "cloudflare":
            return (
                f"Cloudflare answered with {len(raw)} bytes that are not JSON -- "
                "the request may never have reached the service"
            )
        return f"the service answered with {len(raw)} bytes that are not JSON"
    if isinstance(parsed, dict) and "detail" in parsed:
        return str(parsed["detail"])
    return f"the service answered with {len(raw)} bytes of JSON carrying no detail"


def _request(
    method: str,
    url: str,
    key: str,
    payload: dict | None,
    session: str | None = None,
    *,
    timeout: float | None = None,
    headers_out: dict | None = None,
    idempotency_key: str | None = None,
) -> dict:
    # `timeout`: a held /events request needs longer than the default 30 s.
    # `headers_out`: filled with the response headers, for the one caller that
    # reads one (the event cursor, homelab-ij8.106).
    # The process-wide declaration, unless a caller overrides it deliberately.
    session = session or _session
    _check_url_scheme(url)
    # UTF-8, not \u-escapes: an escaped non-ASCII character is six bytes, and a
    # 8 MiB page must still fit the ingress's 12m body limit as JSON.
    data = (
        json.dumps(payload, ensure_ascii=False).encode()
        if payload is not None
        else None
    )
    # `url` is always `{_api_base()}/...` -- an https address the operator configures
    # via REAM_API, never a scheme or host taken from page content or a caller
    # argument. bandit's file:// concern (S310) does not apply here.
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310
    request.add_header("User-Agent", _USER_AGENT)
    _add_client_level(request)
    # `add_unredirected_header`, not `add_header`: the latter is copied onto a
    # redirect's follow-up request by urllib's own default handler, to whatever
    # host and scheme the redirect names. `_RefuseRedirects` above already
    # refuses to follow any redirect at all -- this is the second, independent
    # reason the key can never leave on one.
    request.add_unredirected_header("Authorization", f"Bearer {key}")
    # `add_unredirected_header` for the same reason as the key above, though the
    # stake is far lower: this is a label, not a secret. Consistency here means
    # there is one rule about headers on this request rather than two.
    if session:
        request.add_unredirected_header(_SESSION_HEADER, session)
        request.add_unredirected_header(_SESSION_HEADER_OLD, session)
    if idempotency_key:
        # Set once on the request object the retry loop below re-sends, so a
        # retry after a 502 carries the SAME key and cannot post twice.
        request.add_unredirected_header("Idempotency-Key", idempotency_key)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    raw = _exchange(request, timeout or _REQUEST_TIMEOUT, headers_out)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # A 2xx that isn't JSON is not something to guess at or half-parse --
        # treat it exactly like any other failure to get a usable answer.
        raise HTTPFailure(0, "unexpected response") from None


def _exchange(
    request: urllib.request.Request, timeout: float, headers_out: dict | None
) -> bytes:
    """Send `request`, retrying through a rolling deploy, and return the body bytes.
    Raises HTTPFailure for anything but a 2xx."""
    if request.get_method().upper() not in {"GET", "HEAD"}:
        selfupdate.note_mutation()  # sent once is sent: no re-exec may repeat it
    for attempt in range(_ROLLING_ATTEMPTS):
        try:
            with _opener.open(request, timeout=timeout) as response:
                raw = response.read()
                seen = {
                    k.lower(): v for k, v in getattr(response, "headers", {}).items()
                }
                if headers_out is not None:
                    headers_out.update(seen)
            selfupdate.on_response(request.get_method(), seen, request.full_url)
            break
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            selfupdate.on_response(
                request.get_method(),
                {k.lower(): v for k, v in (exc.headers or {}).items()},
                request.full_url,
            )
            if exc.code in _ROLLING_STATUSES and attempt < _ROLLING_ATTEMPTS - 1:
                # The service updates itself on every push, and while the new
                # copy takes over the front door answers with its own error page
                # -- measured 19 Sep 2026 at about a second of 502s, each
                # returned in THIRTY MILLISECONDS. Without this, a publish that
                # lands in that second fails outright, and the agent has to be
                # told to try again by a human who happens to know there was a
                # deploy. Sleeping here is the whole point: retrying instantly
                # would spend every attempt inside the same second.
                time.sleep(_ROLLING_BACKOFF * (attempt + 1))
                continue
            raise HTTPFailure(
                exc.code, _detail(raw, exc.headers), body=_json_object(raw)
            ) from None
        except urllib.error.URLError as exc:
            raise HTTPFailure(0, str(exc.reason)) from None
        except (OSError, http.client.HTTPException) as exc:
            # urllib wraps only a failure to connect in URLError. A read that
            # times out, or a connection dropped once made, escapes as itself,
            # and every caller that retries (the answer waiter above all) knows
            # only HTTPFailure: on 22 Oct 2026 one read timeout killed it.
            raise HTTPFailure(0, type(exc).__name__) from None
    return raw


def _raw_request(
    method: str,
    url: str,
    key: str,
    data: bytes | None = None,
    content_type: str | None = None,
) -> urllib.request.Request:
    """A request carrying the key (and the declared session) like `_request`, for a
    body that is not JSON."""
    _check_url_scheme(url)
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 -- the operator's https API address, see _request
    request.add_header("User-Agent", _USER_AGENT)
    _add_client_level(request)
    request.add_unredirected_header("Authorization", f"Bearer {key}")
    if _session:
        request.add_unredirected_header(_SESSION_HEADER, _session)
        request.add_unredirected_header(_SESSION_HEADER_OLD, _session)
    if content_type:
        request.add_header("Content-Type", content_type)
    return request


def _upload_image(url: str, key: str, data: bytes) -> dict:
    """POST one picture to `/pages/<id>/comment-images` as multipart/form-data."""
    boundary = "----artifacts-" + uuid.uuid4().hex
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="image"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    request = _raw_request(
        "POST", url, key, body, f"multipart/form-data; boundary={boundary}"
    )
    raw = _exchange(request, _REQUEST_TIMEOUT, None)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPFailure(0, "unexpected response") from None


def _download(url: str, key: str) -> bytes:
    """GET a picture's bytes. The key rides the header only, never the URL."""
    return _exchange(_raw_request("GET", url, key), _REQUEST_TIMEOUT, None)


def _read_page(file_path: str) -> str:
    path = Path(file_path)
    if not path.is_file():
        raise UsageError(f"no such file: {file_path}")
    return path.read_text()


# A `.md` file is markdown: the service renders it into a page in the house
# look (runcove-5t3oj.33). No flag, by every office's wish: the extension is
# the declaration, and a flag is one more thing to forget.
_MARKDOWN_SUFFIXES = (".md", ".markdown")


def _is_markdown(file_path: str | None) -> bool:
    return bool(file_path) and Path(file_path).suffix.lower() in _MARKDOWN_SUFFIXES


def _format_of(file_path: str | None) -> dict:
    return {"format": "markdown"} if _is_markdown(file_path) else {}


def _folder_fields(folder: str | None, folder_title: str | None) -> dict:
    """The request fields for `--folder` / `--no-folder` (`""` takes a page out) and the
    display name of a new folder."""
    out: dict = {}
    if folder is not None:
        out["folder"] = folder
        if folder_title:
            out["folder_title"] = folder_title
    return out


def _move(
    ids: list[str], key: str, folder: str | None, folder_title: str | None = None
) -> dict:
    """`--move ID ... --folder SLUG` (or `--no-folder`, `folder` None): file several of this
    key's own pages in one request. The service answers `{moved, missing, folder}`.
    `folder_title` names the folder if this move makes it."""
    body: dict = {"ids": ids, "folder": folder}
    if folder_title and folder is not None:
        body["folder_title"] = folder_title
    return _request("POST", f"{_api_base()}/pages/move", key, body)


def _folder_action(args, key: str) -> int | None:
    """`--rename-folder`, `--archive-folder`, `--unarchive-folder`: act on one of this key's own
    folders (an old slug resolves). None when none of them was asked for."""
    chosen = [
        flag
        for flag, value in (
            ("--rename-folder", args.rename_folder),
            ("--archive-folder", args.archive_folder),
            ("--unarchive-folder", args.unarchive_folder),
        )
        if value
    ]
    if not chosen:
        if args.confirm:
            print("--confirm goes with --archive-folder SLUG", file=sys.stderr)
            return 2
        return None
    if len(chosen) > 1:
        print(f"{' and '.join(chosen)} cannot be used together", file=sys.stderr)
        return 2
    other = [
        flag
        for flag, value in (
            ("FILE", args.file),
            ("--update", args.update),
            ("--list", args.list),
            ("--folders", args.folders),
            ("--folder", args.folder is not None),
            ("--no-folder", args.no_folder),
            ("--move", args.move),
            ("--archive", args.archive),
            ("--unarchive", args.unarchive),
            ("--pin", args.pin),
            ("--unpin", args.unpin),
            ("--rename", args.rename),
            ("--fetch", args.fetch),
            ("--duplicate", args.duplicate),
            ("--touch", args.touch),
            ("--give", args.give),
            ("--title", args.title),
        )
        if value
    ]
    if other:
        print(
            f"{chosen[0]} goes with its own options only, not with " + ", ".join(other),
            file=sys.stderr,
        )
        return 2
    if args.rename_folder:
        if not args.to and not args.folder_title:
            print(
                "--rename-folder SLUG needs --to NEW-SLUG, --folder-title NAME, or both",
                file=sys.stderr,
            )
            return 2
        if args.confirm:
            print("--confirm goes with --archive-folder only", file=sys.stderr)
            return 2
        body = {}
        if args.to:
            body["slug"] = args.to
        if args.folder_title:
            body["title"] = args.folder_title
        slug = args.rename_folder
        result = _request(
            "POST", f"{_api_base()}/folders/{quote(slug, safe='')}/rename", key, body
        )
        print(json.dumps(result))
        if result.get("previous_slug"):
            print(
                f"renamed folder '{_printable(result['previous_slug'])}' to"
                f" '{_printable(result['slug'])}' (the old name still works)",
                file=sys.stderr,
            )
        else:
            print(
                f"folder '{_printable(result['slug'])}' is now titled"
                f" '{_printable(result['title'])}'",
                file=sys.stderr,
            )
        return 0
    if args.to or args.folder_title:
        print(f"{chosen[0]} takes no --to or --folder-title", file=sys.stderr)
        return 2
    archiving = bool(args.archive_folder)
    if args.confirm and not archiving:
        print("--confirm goes with --archive-folder only", file=sys.stderr)
        return 2
    slug = args.archive_folder or args.unarchive_folder
    result = _request(
        "POST",
        f"{_api_base()}/folders/{quote(slug, safe='')}/archive",
        key,
        {"archived": archiving, "confirm": bool(args.confirm)},
    )
    print(json.dumps(result))
    name = _printable(result["slug"])
    if "would_archive" in result:
        w = result["would_archive"]
        print(
            f"NOT archived yet: archiving folder '{name}' would archive {w['pages']} page(s)"
            f" and set aside {w['questions_set_aside']} open question(s) (the asking agents"
            " are told; they come back when the folder is unarchived). Run again with"
            " --confirm to do it.",
            file=sys.stderr,
        )
    elif archiving:
        print(
            f"archived folder '{name}': {result['pages']} page(s), "
            f"{result['questions_set_aside']} question(s) set aside",
            file=sys.stderr,
        )
    else:
        print(
            f"unarchived folder '{name}': {result['pages_restored']} page(s) restored, "
            f"{result['questions_reopened']} question(s) reopened",
            file=sys.stderr,
        )
    return 0


def _publish(
    file_path: str,
    title: str | None,
    key: str,
    folder: str | None = None,
    folder_title: str | None = None,
    refresh_every: int | None = None,
    comments: str | None = None,
) -> dict:
    body = _read_page(file_path)
    payload = {
        "title": title or "",
        "body": body,
        **_format_of(file_path),
        **_folder_fields(folder, folder_title),
    }
    if refresh_every:
        payload["refresh_every"] = refresh_every
    if comments:
        payload["comments"] = comments
    return _request("POST", f"{_api_base()}/pages", key, payload)


def _rendered(file_path: str, key: str, title: str | None = None) -> str:
    """The HTML a publish of this file would store: the file itself, or for a
    `.md` file the service's rendering of it (so `--check` and `--matches`
    look at what a reader would get)."""
    text = _read_page(file_path)
    if not _is_markdown(file_path):
        return text
    payload = {"body": text, **({"title": title} if title else {})}
    out = _request("POST", f"{_api_base()}/markdown", key, payload)
    return out["html"]


def _read_current(artifact_id: str, key: str) -> dict:
    return _request("GET", f"{_api_base()}/pages/{artifact_id}", key, None)


def _patch(
    file_path: str,
    artifact_id: str,
    expected_revision: str,
    key: str,
    title: str | None = None,
    note: str | None = None,
    folder: str | None = None,
    folder_title: str | None = None,
    refresh_every: int | None = None,
    comments: str | None = None,
) -> dict:
    """PATCH with `expected_revision`, and REFUSE on a conflict rather than
    retrying.

    This used to catch the 409, re-read the artifact's now-current revision,
    and retry once with THE SAME BODY. The server's expected_revision check is
    correct optimistic concurrency and the client defeated it: whoever patched
    second silently overwrote the version that landed in between, and the
    losing agent was never told its page had been replaced. Verified in source
    19 Sep 2026, still true 21 Sep, fixed here.

    A blind retry cannot be right, because the body being sent was composed
    against a page that no longer exists. Merging is not this tool's job -- it
    has no idea what the other writer meant. So it refuses, and says exactly
    how to recover, which is cheap now that the body can be fetched in one
    command:

        artifacts-publish --fetch <id> page.html   # what is there now
        ...re-apply your change...
        artifacts-publish page.html --update <id>

    This only bites where two writers share a page, which is where the service
    is heading -- a board an arc's lead and its workers all update.
    """
    body = _read_page(file_path)
    payload = {
        "body": body,
        "expected_revision": expected_revision,
        **_format_of(file_path),
    }
    if title is not None:
        payload["title"] = title
    if note:
        payload["note"] = note
    payload.update(_folder_fields(folder, folder_title))
    if refresh_every is not None:
        payload["refresh_every"] = refresh_every  # 0 clears it
    if comments:
        payload["comments"] = comments
    try:
        return _request("PATCH", f"{_api_base()}/pages/{artifact_id}", key, payload)
    except HTTPFailure as exc:
        if exc.status != 409:
            raise
        raise Conflict(
            f"{artifact_id} changed while you were writing it -- somebody else "
            "published a version after you last read it. Your update was NOT "
            "applied, and nothing was overwritten.\n"
            f"  artifacts-publish --fetch {artifact_id} page.html\n"
            "  ...re-apply your change to that file...\n"
            f"  artifacts-publish page.html --update {artifact_id}"
        ) from exc


def _update(
    file_path: str,
    artifact_id: str,
    key: str,
    title: str | None = None,
    *,
    note: str | None = None,
    if_revision: str | None = None,
    folder: str | None = None,
    folder_title: str | None = None,
    refresh_every: int | None = None,
    comments: str | None = None,
) -> dict:
    """A whole-page update. With `if_revision` (the hash or version number the
    caller last published or read), refuse if the page has moved on since: a
    generator that owns a page must not overwrite a hand edit made in between
    (7 Oct 2026). The read-then-PATCH gap is covered by `expected_revision`."""
    current = _read_current(artifact_id, key)
    if if_revision is not None:
        given = if_revision.strip()
        same = (
            int(given) == current.get("current_version")
            if given.isdigit()
            else given == current["revision"]
        )
        if not same:
            raise Conflict(
                f"{artifact_id} has changed since revision {given} -- it is at"
                f" version {current.get('current_version')} now. Your update was NOT"
                " applied, and nothing was overwritten.\n"
                f"  artifacts-publish --fetch {artifact_id} page.html\n"
                "  ...see what changed and re-apply your change...\n"
                f"  artifacts-publish page.html --update {artifact_id}"
            )
    return _patch(
        file_path,
        artifact_id,
        current["revision"],
        key,
        title,
        note,
        folder=folder,
        folder_title=folder_title,
        refresh_every=refresh_every,
        comments=comments,
    )


_INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_interval(text: str) -> int:
    """`--refresh-every` as seconds: a plain number of seconds, or a number with s, m, h or d
    (`30m`, `2h`, `1d`). The service holds the range (a minute to a year) and says so."""
    match = re.fullmatch(r"(\d+)([smhd]?)", text.strip())
    if not match:
        raise UsageError(
            f"--refresh-every {text!r} is not a duration: use seconds or a number with"
            " s, m, h or d, e.g. 30m, 2h, 1d"
        )
    return int(match.group(1)) * _INTERVAL_UNITS[match.group(2) or "s"]


def _since(stamp: str | None) -> str:
    """How long ago an ISO time was, in the coarsest honest unit."""
    if not stamp:
        return "unknown"
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return "unknown"
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - then).total_seconds()))
    for limit, size, word in ((90, 1, "s"), (5400, 60, "min"), (172800, 3600, "h")):
        if seconds < limit:
            return f"{seconds // size} {word} ago"
    return f"{seconds // 86400} d ago"


def _touch(args, key: str) -> int:
    """`--touch ID`: vouch for one of your own pages without sending it. Exit 0 only when the
    service recorded the check; a refusal (409 `--if-revision`, 404, 422) is exit 1, so a timer
    script that checks `$?` catches a rejection."""
    payload: dict = {"status": args.status or "ok"}
    if args.note:
        payload["note"] = args.note
    if args.if_revision:
        payload["if_revision"] = args.if_revision
    if args.no_refresh:
        payload["refresh_every"] = 0
    elif args.refresh_every:
        payload["refresh_every"] = parse_interval(args.refresh_every)
    try:
        done = _request("POST", f"{_api_base()}/pages/{args.touch}/touch", key, payload)
    except HTTPFailure as exc:
        if exc.status != 409:
            raise
        raise Conflict(
            f"{args.touch} has changed since revision {args.if_revision} -- it was NOT"
            " vouched for. Look at it before you vouch for it again:\n"
            f"  artifacts-publish --fetch {args.touch} page.html"
        ) from exc
    print(
        json.dumps(
            {
                field: done.get(field)
                for field in (
                    "status",
                    "checked_at",
                    "changed_at",
                    "revision",
                    "version",
                    "note",
                    "refresh_every",
                    "lapses_at",
                )
            }
        )
    )
    if done["status"] == "failed":
        note = f": {_printable(done['note'])}" if done.get("note") else ""
        print(f"marked failed{note}", file=sys.stderr)
    else:
        print(
            f"checked, still current (changed {_since(done.get('changed_at'))})",
            file=sys.stderr,
        )
    return 0


def _versions(artifact_id: str, key: str) -> list[dict]:
    return _request("GET", f"{_api_base()}/pages/{artifact_id}/versions", key, None)


# C0/C1 controls (ESC included), DEL, and the bidi overrides that reorder what
# a terminal shows. Stripped from server-sourced text before it is printed.
_UNPRINTABLE = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def _printable(value) -> str:
    return _UNPRINTABLE.sub("", str(value if value is not None else ""))


def _print_versions(rows: list[dict]) -> None:
    """One line per version, newest first, and never a body.

    homelab-ij8.30: publish returned a revision hash and nothing could list,
    show or roll back -- so the hash was a number the agent was told and could
    do nothing with. The `live` marker is the column that makes the rest
    actionable: without it you can read a history and still not know which one
    is on the wall.
    """
    if not rows:
        # Not silence. An empty result and a broken call print the same nothing.
        print("no versions -- which should be impossible for a page that exists")
        return
    for row in rows:
        mark = "*" if row.get("live") else " "
        # Every field is server-sourced and some are caller-written (session
        # labels, notes), so none reaches the terminal with a control or
        # escape character in it.
        who = _printable(row.get("session") or row.get("created_by"))
        when = _printable((row.get("created_at") or "")[:19].replace("T", " "))
        # A section update's note ("#rows: prepend"), last and only when set,
        # so the columns every existing caller splits on are unchanged.
        note = f"\t{_printable(row['note'])}" if row.get("note") else ""
        print(
            f"{mark} v{_printable(row['n'])}\t{_printable(row.get('revision'))}\t"
            f"{_printable(row.get('bytes', 0))}\t{when}\t{who}{note}"
        )
    print("* is the version on the wall now", file=sys.stderr)


def _fetch(artifact_id: str, key: str, version: int | None = None) -> str:
    """The page's own HTML, back to the agent that published it.

    `GET /pages/{id}/body` has existed since 19 Sep and nobody could find
    it. On 21 Sep a seat lost a page's source to a tmpfs scratchpad, guessed
    /source, /content, /html, /versions, /revisions and /history, got 404 from
    every one, and concluded a live page was permanently un-updatable. Its own
    diagnosis afterwards is the reason this flag exists rather than a longer
    paragraph in the skill: "the failure mode of guessing is indistinguishable
    from the failure mode of a missing capability -- every wrong guess returns
    exactly what a missing feature returns."

    A flag removes the guessing. A paragraph only helps whoever reads it first.
    """
    # `--at-version N` reads THAT version; without it, the live one. Rolling back
    # is then this plus an ordinary update -- no new verb, and the roll-back
    # lands as a new version rather than rewriting history.
    where = (
        f"/pages/{artifact_id}/versions/{version}"
        if version is not None
        else f"/pages/{artifact_id}/body"
    )
    got = _request("GET", f"{_api_base()}{where}", key, None)
    body = got.get("body")
    if not isinstance(body, str):
        raise UsageError("the service returned no body for that page")
    # The service re-runs its checks on every read-back, and this is where a
    # seat comes to find out what is wrong with a page it cannot see. Dropping
    # them here meant "no warnings" on a fetch proved nothing -- found on 21
    # Sep by trusting exactly that silence for a moment. Stderr, so a fetch to
    # stdout is still byte-for-byte the page.
    _print_warnings(got)
    return body


def _section_url(artifact_id: str, element_id: str) -> str:
    return (
        f"{_api_base()}/pages/{quote(artifact_id, safe='')}"
        f"/sections/{quote(element_id, safe='')}"
    )


def _fetch_section(artifact_id: str, element_id: str, key: str) -> dict:
    """One element of the LIVE page, with the revisions a section update can
    be pinned to. Edit what is live, never a local copy (6 Oct 2026)."""
    return _request("GET", _section_url(artifact_id, element_id), key, None)


def _section_op(args) -> tuple[str, str | None]:
    """Which operation, and the file holding its fragment. Exactly one."""
    chosen = [
        (op, getattr(args, op))
        for op in ("replace", "append", "prepend")
        if getattr(args, op)
    ]
    if args.remove:
        chosen.append(("remove", None))
    if len(chosen) != 1:
        raise UsageError(
            "--section needs exactly one of --replace FILE, --append FILE, "
            "--prepend FILE or --remove"
        )
    return chosen[0]


def _update_section(args, key: str) -> dict:
    op, file_path = _section_op(args)
    payload: dict = {"op": op, "notify": bool(args.notify)}
    if file_path is not None:
        payload["html"] = _read_page(file_path)
        payload.update(_format_of(file_path))
    if args.if_revision is not None:
        payload["if_revision"] = args.if_revision
    if args.if_section is not None:
        payload["if_section"] = args.if_section
    if args.note:
        payload["note"] = args.note
    try:
        return _request("PATCH", _section_url(args.update, args.section), key, payload)
    except HTTPFailure as exc:
        if exc.status != 409:
            raise
        raise Conflict(
            f"{exc.detail} -- your section update was NOT applied.\n"
            f"  artifacts-publish --fetch {args.update} --section {args.section}"
            " part.html\n"
            "  ...re-apply your change, then update with the new --if-section"
        ) from exc


def _matches(artifact_id: str, file_path: str | None, key: str) -> int:
    """Whether a local file is what the page is serving -- the question behind
    "is my republish going to change anything?".

    The warpgate seat, 24 Sep 2026, compared a file with `--fetch` piped
    through `jq -r`, got a mismatch from the newline jq adds, and could not
    compare against the revision either: the revision hashes the version
    number with the body, so no file alone can reproduce it. Exact bytes are
    the only honest comparison -- an identical republish mints no version, and
    one extra newline mints one -- so a difference that is only trailing
    whitespace is still "differs", and says so in words.
    """
    if not file_path:
        raise UsageError(
            "--matches needs a FILE: artifacts-publish --matches ID page.html"
        )
    if _is_markdown(file_path):
        # Rendered under the page's own name, which may not be its # heading:
        # otherwise a page published with --title always "differs".
        name = _read_current(artifact_id, key).get("title")
        local = _rendered(file_path, key, name)
    else:
        local = pathlib.Path(file_path).read_text(encoding="utf-8")
    live = _fetch(artifact_id, key)
    if local == live:
        print(f"{file_path} is the live version of {artifact_id}", file=sys.stderr)
        return 0
    if local.rstrip() == live.rstrip():
        print(
            f"{file_path} differs from {artifact_id} only in trailing whitespace "
            "-- publishing it would still mint a new version",
            file=sys.stderr,
        )
        return 3
    a, b = local.splitlines(), live.splitlines()
    first = next(
        (n for n, (x, y) in enumerate(zip(a, b, strict=False), 1) if x != y),
        min(len(a), len(b)) + 1,
    )
    print(
        f"{file_path} differs from the live version of {artifact_id}, first at "
        f"line {first} ({len(a)} lines here, {len(b)} live)",
        file=sys.stderr,
    )
    return 3


def _preview(file_path: str | None, out: str, width: int, key: str) -> int:
    """What a reader would see, before anyone can (homelab-ij8.107).

    Exit 0 with a PNG written; 3 when the service could not render (no browser
    there, or the render failed) -- never 0 without a picture, because "I
    previewed it" with nothing to look at is the claim this exists to stop.
    """
    if not file_path:
        raise UsageError(
            "--preview needs a FILE: artifacts-publish page.html --preview shot.png"
        )
    body = pathlib.Path(file_path).read_text(encoding="utf-8")
    got = _request(
        "POST",
        f"{_api_base()}/preview",
        key,
        {"body": body, "width": width, **_format_of(file_path)},
    )
    for error in got.get("errors") or []:
        print(f"script error: {error}", file=sys.stderr)
    _print_findings(got.get("findings"), "preview: ")
    if not got.get("rendered") or not got.get("png_base64"):
        why = "has no browser" if not got.get("renderer") else "could not render it"
        print(f"no preview: the service {why}", file=sys.stderr)
        return 3
    import base64

    pathlib.Path(out).write_bytes(base64.b64decode(got["png_base64"]))
    errors = got.get("errors")
    tail = (
        "no script errors" if errors == [] else f"{len(errors or [])} script error(s)"
    )
    print(
        f"{got['width']}x{got['height']} preview -> {out}; {tail}. Open it and look.",
        file=sys.stderr,
    )
    return 0


def _list(key: str, everyone: bool = False, folder: str | None = None) -> list[dict]:
    # `everyone`: every page this key may read, other agents' open pages
    # included (homelab-ij8.105), not only its own. `folder`: only the pages in
    # this key's own folder of that slug.
    if folder is not None:
        return _request(
            "GET",
            f"{_api_base()}/folders/{quote(folder, safe='')}/pages",
            key,
            None,
        )
    scope = "?scope=all" if everyone else ""
    return _request("GET", f"{_api_base()}/pages{scope}", key, None)


def _shelf(action: str, artifact_id: str, key: str, payload: dict) -> dict:
    """Archive, rename or pin -- the housekeeping a human does in the gallery.

    Jeremy, 20 Sep 2026: "ideally agents can take all the same actions a human
    can." These reach the same rows his controls do; the service scopes them to
    this key's own seat, so a page belonging to another agent answers 404 and
    nothing here has to know that.

    None of them send the body. That is the point of having them at all: an
    agent that published a page months ago, and no longer has the file, can
    still fix its name or take it off the wall.
    """
    return _request("POST", f"{_api_base()}/pages/{artifact_id}/{action}", key, payload)


def _inline_scripts(body: str) -> list[tuple[int, str]]:
    """Every inline <script> in the page, as (first line number, source).

    A script with a `src` is skipped: it is somebody else's file, the CSP will
    refuse it anyway, and `csp.check` already reports it as remote_script.
    """
    out: list[tuple[int, str]] = []
    for match in re.finditer(
        r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script\s*>",
        body,
        re.IGNORECASE | re.DOTALL,
    ):
        source = match.group(1)
        if source.strip():
            out.append((body.count("\n", 0, match.start(1)) + 1, source))
    return out


def _syntax_findings(body: str) -> list[str]:
    """Run `node --check` over each inline script. Empty when node is absent.

    From the cove seat's own note on homelab-ij8.29: "it catches a
    trailing-comma typo for nothing, in a validator that already parses the
    file." A page whose script does not parse renders as a page with no script
    at all -- the same silent half-failure the CSP shapes are about, reached by
    a different route.

    SKIPPED, NOT FAILED, where node is missing. A checker that refuses to run
    is not evidence of a problem, and saying so is the caller's job, not this
    function's -- see `_check_page`, which reports the skip out loud rather
    than letting a clean result mean two different things.
    """
    node = shutil.which("node")
    if node is None:
        return []
    findings: list[str] = []
    for line, source in _inline_scripts(body):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(source)
            path = handle.name
        try:
            done = subprocess.run(  # noqa: S603 -- node from which(), path we just wrote
                [node, "--check", path],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if done.returncode != 0:
                # node names OUR temporary file, which tells the author
                # nothing. The line it reports is relative to the script, so
                # it is offset back onto the page.
                detail = (done.stderr or "").strip().splitlines()
                first = next(
                    (
                        ln.strip()
                        for ln in detail
                        if "SyntaxError" in ln or "Error" in ln
                    ),
                    "does not parse",
                )
                findings.append(f"inline script at line {line}: {first}")
        finally:
            pathlib.Path(path).unlink(missing_ok=True)
    return findings


def check_page(body: str) -> tuple[list, list[str], bool]:
    """(findings, syntax findings, whether node ran). No network, no key.

    The findings are the serving policy's (`csp`) and the page-level traps
    (`traps`) in one list, sorted by line: they are one question to whoever
    runs this -- what is wrong with this file? -- and which module noticed is
    not their business (homelab-ij8.36).
    """
    findings = sorted(
        csp.check(body) + traps.check(body, client=True),
        key=lambda w: (w.line, w.shape),
    )
    return findings, _syntax_findings(body), shutil.which("node") is not None


def _check_page(file_path: str) -> int:
    """`--check`: the REFUSING half of the CSP advisory, run before publishing.

    homelab-ij8.29, first reported by the cove seat on 19 Sep: a page that
    violates the CSP does not error, it silently loses its styling or its
    images -- "the worst failure mode for something you publish and walk away
    from". That agent hand-rolled its own grep for `<form`, `src=`, `<link`,
    `@import` and any non-gallery URL, and observed that every agent would
    rediscover it.

    They will not now. The shapes here are the SAME table the service uses
    (`ream.csp`), derived from the real directives in the served policy,
    so this cannot drift from what the browser will actually do -- which a
    hand-rolled grep in each agent's own repo certainly would.

    Refuses where the server only warns, deliberately, and the reasoning is
    csp.py's own: the server cannot refuse a page that is 95 % right over a
    literal `<form` inside an escaped code sample, because the author cannot
    see the page and so cannot see what upset it. Here the author IS looking,
    at a file in front of them, and may simply publish anyway.

    Needs no key and makes no request: it reads a file. That matters for the
    case it exists for -- run it in CI, run it before you have a key, run it
    on a page you have not decided to publish yet.
    """
    if _is_markdown(file_path):
        raise UsageError(
            f"--check reads an HTML page; {file_path} is markdown, which the"
            " service renders. Publish it (the service runs the same checks on"
            " the rendered page and prints any warning), or look first with"
            f" --preview shot.png {file_path}."
        )
    body = _read_page(file_path)
    findings, syntax, node_ran = check_page(body)
    # THE SERVICE'S OWN SECRET SCAN, run here too (homelab-ij8.106). The first
    # outside agent's page passed --check and was then refused on publish with
    # a 422: this ran only the CSP half, never the scan that actually refuses.
    # Same module, same shapes file, so it cannot disagree with the service.
    from ream import scan  # stdlib-only; imported here to keep startup light

    secrets_found = scan.scan(body)
    for found in secrets_found:
        print(
            f"{file_path}:{found.line}: {found.shape} -- the service REFUSES a page "
            "carrying something shaped like a secret; remove or reword that line",
            file=sys.stderr,
        )
    for warning in findings:
        print(
            f"{file_path}:{warning.line}: {warning.shape} -- {warning.hint}",
            file=sys.stderr,
        )
    for problem in syntax:
        print(f"{file_path}: {problem}", file=sys.stderr)
    if not node_ran:
        # SAID OUT LOUD. A clean result that quietly skipped half the checks is
        # the shape this whole repo keeps meeting: a green signal that does not
        # mean what it reads as.
        print(
            f"{file_path}: note -- node is not installed here, so inline "
            "scripts were NOT syntax-checked",
            file=sys.stderr,
        )
    total = len(findings) + len(syntax) + len(secrets_found)
    if total == 0:
        print(
            f"{file_path}: nothing the serving policy refuses, nothing shaped like "
            "a secret, and no trap the templates warn about"
        )
        return 0
    thing = "problem" if total == 1 else "problems"
    print(
        f"{file_path}: {total} {thing} -- published as it stands, this page "
        "would half-render or read wrong, with nothing in any log you can see",
        file=sys.stderr,
    )
    return 1


# How long a publish waits for the service's render before saying nothing.
_RENDER_WAIT_SECONDS = 10


_FINDING_WORDS = {
    "blank": "looks blank",
    "missing_file": "missing file",
    "too_wide": "scrolls sideways",
    "unchecked": "not checked",
}


def _print_findings(findings, prefix: str) -> None:
    """The render check's findings other than script errors (those have their own
    lines), one per line on STDERR. `unchecked` says "not checked", never "fine"
    (runcove-5t3oj.56)."""
    for item in findings if isinstance(findings, list) else []:
        if not isinstance(item, dict) or item.get("kind") == "script_error":
            continue
        word = _FINDING_WORDS.get(str(item.get("kind")), str(item.get("kind")))
        detail = str(item.get("detail", ""))
        print(f"{prefix}render check, {word}: {detail}", file=sys.stderr)


def _report_render(artifact_id: str, key: str) -> None:
    """After a publish, wait briefly for the service to render the page, and
    say LOUDLY if its scripts threw (homelab-ij8.72): an error in a page the
    author cannot see is otherwise found by Jeremy. Silent when the render does
    not finish in time -- that is not the same as clean, so it claims nothing.
    Never fails the publish."""
    deadline = time.monotonic() + _RENDER_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            report = _request(
                "GET", f"{_api_base()}/pages/{artifact_id}/render", key, None
            )
        except HTTPFailure:
            return
        if report.get("renderer") is False:
            return
        if report.get("rendered"):
            errors = report.get("errors") or []
            _print_findings(report.get("findings"), "")
            if errors:
                print(
                    f"WARNING: rendered, the page's scripts threw {len(errors)} "
                    "error(s) -- a reader sees whatever they broke:",
                    file=sys.stderr,
                )
                for line in errors:
                    print(f"  {line}", file=sys.stderr)
            return
        time.sleep(1)


def _print_warnings(result: dict) -> None:
    """CSP advisories to STDERR, one line each, so stdout stays parseable JSON.

    The server sends these because the author cannot see its own rendered page
    and would otherwise learn nothing about a Google font or a hand-rolled
    form until Jeremy opened a half-rendered page. They are advisory: the
    publish succeeded. Printed field by field rather than passed through, for
    the same reason `_print_published` whitelists stdout.
    """
    warnings = result.get("warnings")
    if not isinstance(warnings, list):
        return
    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        shape, line, hint = (
            warning.get("shape"),
            warning.get("line"),
            warning.get("hint"),
        )
        message = warning.get("message")
        if isinstance(shape, str) and isinstance(message, str):
            # A fact about the whole page, not a place in it.
            print(
                f"warning: {shape}: {_printable(message)}"
                + (f" -- {_printable(hint)}" if isinstance(hint, str) else ""),
                file=sys.stderr,
            )
            continue
        if not isinstance(shape, str) or not isinstance(line, int):
            continue
        print(
            f"warning: {shape} at line {line}"
            + (f" -- {hint}" if isinstance(hint, str) else ""),
            file=sys.stderr,
        )


def _print_published(result: dict) -> None:
    """Whitelist, not passthrough: print only id/url/revision, exactly what the
    brief asks for, so a future field the server starts returning can never
    reach a caller's stdout (or a caller's own downstream parsing) unreviewed."""
    print(
        json.dumps(
            {
                field: result[field]
                # `page_url` is the address a person can follow; `url` is the
                # page's own origin, which 303s to the gallery, and was the one
                # field that looked pasteable and was not (homelab-ij8.42).
                # Both are here: `url` because callers already read it, and
                # `page_url` because it is the one to put in a message.
                for field in ("id", "page_url", "url", "revision", "title", "bytes")
                if field in result
            }
        )
    )
    # ON STDERR, not in the JSON: stdout is a contract every existing caller
    # already parses, and a new key there is a change to it. This is a note to
    # the person or agent running the command, which is where it belongs.
    # WHAT WAS ACTUALLY STORED, in words (homelab-ij8.43). The publisher cannot
    # see its own page, so a substitution that silently did nothing looks
    # exactly like one that worked -- the title and the byte count are the
    # cheapest thing that catches it.
    if result.get("title") and result.get("bytes"):
        print(
            f'stored "{result["title"]}", {result["bytes"]} bytes',
            file=sys.stderr,
        )
    # A folder, said in words on stderr: a first use is the one line that shows a typo.
    filed = result.get("folder")
    if filed:
        if filed.get("created"):
            print(f"created new folder '{_printable(filed['slug'])}'", file=sys.stderr)
        else:
            print(f"in folder '{_printable(filed['slug'])}'", file=sys.stderr)
    if result.get("unchanged"):
        print(
            "unchanged -- the body was identical, so no new version was minted. "
            "The page is exactly as it was.",
            file=sys.stderr,
        )
    _print_warnings(result)


def _print_shelf(result: dict) -> None:
    """Whitelisted like `_print_published`, and for the same reason. The fields
    differ because what changed differs: nothing was published, so a revision
    would only invite a caller to think one was."""
    print(
        json.dumps(
            {
                field: result[field]
                for field in (
                    "id",
                    "title",
                    "url",
                    "archived_at",
                    "pinned_at",
                    "agents_can_read",
                )
                if field in result
            }
        )
    )


def installed_stamp() -> str:
    """When the copy of the code that is RUNNING was put on disk.

    Not the package version: `artifacts` has been 0.1.0 since the first commit
    and will stay there, so a version string cannot distinguish a fresh install
    from a six-day-old one. The module file's mtime can, and it is the same
    answer for every way this gets run -- a `uv tool install` stamps it with the
    install, an editable checkout stamps it with the last edit.
    """
    try:
        when = time.localtime(Path(__file__).stat().st_mtime)
    except OSError:  # pragma: no cover -- the module we are executing from
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M", when)


# The line an unrecognised flag gets, on top of argparse's own usage.
#
# WHY. On 21 Sep a seat reached for `--fetch`, which had been committed the day
# before, and got argparse's bare `unrecognized arguments: --fetch`. The flag
# was real; the INSTALLED copy predated it, because `uv tool install` had not
# been re-run. Its own words: that message "reads exactly like 'this feature
# does not exist'" -- the identical failure the API's catch-all 404 was written
# to end a day earlier, one layer out. For a uv-installed CLI, committed and
# installed are different states, and the gap is invisible from the side that
# did the committing.
#
# So the age of the running code is stated at the only moment anybody is
# looking: when a flag they expected was refused.
_STALE_HINT = (
    "\nthis copy of {prog} was installed {stamp}. A flag added to the repo after"
    "\nthat is not in it -- committed and installed are different states. If you"
    "\nexpected this flag to exist, refresh the install:"
    "\n  uv tool install <path to the artifacts checkout> --force"
)

# A global flag placed AFTER a subcommand is refused by argparse with the same
# "unrecognized arguments" wording an absent flag gets, which sends the reader
# to reinstall a tool that is perfectly current. Caught by walking into it:
# `artifacts-decide ask <id> ... --session X` told me to refresh an install I
# had made four minutes earlier.
_GLOBAL_FLAGS = ("--session", "--key-file", "--version")


class _HelpFormatter(argparse.HelpFormatter):
    """`--help` with the owner's words filled in (`{{owner_name}}`, `{{owner_they}}`): the help
    text is written once and the house is in the settings. Filled BEFORE argparse wraps the
    lines, so the wrap is the same for a short name and a long one."""

    def _fill_text(self, text, width, indent):
        return super()._fill_text(textvars.words(text), width, indent)

    def _split_lines(self, text, width):
        return super()._split_lines(textvars.words(text), width)


class _Parser(argparse.ArgumentParser):
    """argparse, except that a refused flag says how old this build is."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", _HelpFormatter)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:  # type: ignore[override]
        # Only for the one message that is ambiguous with a missing feature.
        # A missing value or a bad choice is a real usage error and gets
        # argparse's wording unchanged -- adding an install hint to every one of
        # them would train the reader to skip it, which is how the hint stops
        # working on the day it is right.
        if "unrecognized arguments" in message or "invalid choice" in message:
            # The likelier explanation first, when it applies. A global flag
            # after a subcommand is a position error, not a missing feature,
            # and telling somebody to reinstall a current tool wastes the one
            # moment they are paying attention.
            misplaced = [f for f in _GLOBAL_FLAGS if f in message]
            if misplaced:
                message += (
                    f"\n{misplaced[0]} is a global flag: it goes BEFORE the "
                    f"command, not after.\n  {self.prog.split()[0]} "
                    f"{misplaced[0]} VALUE <command> ...\n"
                    f"or set $REAM_SESSION once for the whole session."
                )
            else:
                message += _STALE_HINT.format(
                    prog=self.prog.split()[0], stamp=installed_stamp()
                )
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=prog_name("artifacts-publish"),
        description=(
            "Publish a page to Ream (formerly artifacts), update it, list and "
            "fetch pages. Also run as `ream publish`; this older command name "
            "still works."
        ),
    )
    parser.add_argument(
        "file",
        nargs="?",
        metavar="FILE",
        help=(
            "the page: HTML, or markdown when it ends in .md, which the service"
            " renders into a page in the house look (tables, fenced code, task"
            " lists, linkable headings; raw HTML such as a decision slot passes"
            " through). A markdown page needs no --title: its first # heading"
            " names it. A .md fragment works with --section too."
        ),
    )
    # So the question can be asked BEFORE it becomes a surprise: a seat about to
    # rely on a recent flag can check what it is running in one command.
    parser.add_argument(
        "--version",
        action="version",
        version=f"artifacts-publish, installed {installed_stamp()}",
    )
    parser.add_argument(
        "--title", help="the page's name; for a .md file, overrides its # heading"
    )
    parser.add_argument("--update", metavar="ID")
    parser.add_argument("--list", action="store_true")
    parser.add_argument(
        "--folder",
        metavar="SLUG",
        help=(
            "your own folder (lowercase letters, digits, hyphens; up to 40): with a"
            " FILE it files the page there when it publishes or with --update; the"
            " folder is made on first use and the result says 'created new folder',"
            " so a typo shows. With --list it lists that folder's pages."
        ),
    )
    parser.add_argument(
        "--move",
        nargs="+",
        metavar="ID",
        help=(
            "file several of your own pages in one go: --move ID [ID ...] --folder SLUG"
            " (made on first use, and says so) or --move ID [ID ...] --no-folder to take"
            " them out; up to 200 ids. Prints which moved and which are missing (no such"
            " page, or not yours)."
        ),
    )
    parser.add_argument(
        "--folder-title",
        metavar="NAME",
        help=(
            "with --folder (publish, --update, or --move): the display name of a NEW"
            " folder (default the slug); with --rename-folder: the folder's new title"
        ),
    )
    parser.add_argument(
        "--rename-folder",
        metavar="SLUG",
        help=(
            "rename one of your folders: --rename-folder SLUG [--to NEW-SLUG]"
            " [--folder-title NAME], at least one of the two. The old slug keeps working"
            " for --folder, --list and every folder call; a slug already in use is refused"
            " (409). Title only: --rename-folder SLUG --folder-title NAME."
        ),
    )
    parser.add_argument(
        "--archive-folder",
        metavar="SLUG",
        help=(
            "archive a folder and every live page in it. Two steps: without --confirm it"
            " changes nothing and prints how many pages it would archive and how many"
            " open questions it would set aside (the asking agents are told)"
        ),
    )
    parser.add_argument(
        "--unarchive-folder",
        metavar="SLUG",
        help=(
            "unarchive a folder: restores the pages its archive archived (not ones you"
            " had archived yourself) and reopens their questions"
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="with --archive-folder: really do it",
    )
    parser.add_argument(
        "--no-folder",
        action="store_true",
        help="with FILE --update ID, or with --move: take the page(s) out of their folder",
    )
    parser.add_argument(
        "--folders",
        action="store_true",
        help="list your folders: slug, pages, questions waiting on {{owner_them}}, archived",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="with --list: every page you can read, other agents' open pages included",
    )
    parser.add_argument("--key-file")
    parser.add_argument(
        "--key-fd",
        type=int,
        metavar="N",
        help=(
            "read the key from an already-open descriptor instead of a file, "
            "so it never touches disk: `--key-fd 3 ... 3< <(sops -d ...)`"
        ),
    )
    parser.add_argument(
        "--check",
        metavar="FILE",
        help=(
            "refuse a page the serving policy would half-render, before "
            "publishing it. Reads a file; needs no key and makes no request."
        ),
    )
    parser.add_argument(
        "--preview",
        metavar="PNG",
        help=(
            "render FILE on the service WITHOUT publishing it and write what a "
            "reader sees to PNG (a phone, 390px wide, unless --width); prints "
            "the page's script errors. Look at the PNG before you publish."
        ),
    )
    parser.add_argument(
        "--width",
        type=int,
        default=390,
        metavar="PX",
        help="with --preview: the viewport width (320-1600; default 390, a phone)",
    )
    parser.add_argument(
        "--session",
        metavar="LABEL",
        help=(
            "which session this is, when several share one key; defaults to "
            f"${_SESSION_ENV}, else <team>/<session id> from Claude Code's "
            "QD_OFFICE or ARTIFACTS_SEAT. A label, never a secret."
        ),
    )
    # The gallery's housekeeping, for agents. Each takes the page id, and none
    # of them takes a FILE -- see `_shelf`.
    shelf = parser.add_argument_group("housekeeping (no FILE needed)")
    shelf.add_argument("--archive", metavar="ID", help="take a page off the wall")
    shelf.add_argument("--unarchive", metavar="ID", help="put it back")
    shelf.add_argument(
        "--rename", metavar="ID", help="rename a page; needs --title, sends no body"
    )
    shelf.add_argument(
        "--touch",
        metavar="ID",
        help=(
            "say a page of yours is checked and still current, without sending it:"
            " no new version, the wall does not reorder, nothing becomes unread. A timer"
            " that regenerates a page calls this on a round where nothing changed. Takes"
            " --status, --note, --if-revision, --refresh-every, --no-refresh. Exits"
            " non-zero if the service refuses (a script should check it)."
        ),
    )
    shelf.add_argument(
        "--status",
        choices=("ok", "failed"),
        help=(
            "with --touch: 'failed' says the job that keeps this page current failed;"
            " the wall flags it at once until a later ok check or a publish (default ok)"
        ),
    )
    shelf.add_argument(
        "--refresh-every",
        metavar="DURATION",
        help=(
            "how often you promise to check this page: 30m, 2h, 1d or seconds (a minute"
            " to a year). With FILE, FILE --update ID, or --touch ID. A page silent for"
            " 2.5 of these is flagged as stale. Never inferred; clear it with"
            " --no-refresh."
        ),
    )
    shelf.add_argument(
        "--no-refresh",
        action="store_true",
        help="with FILE --update ID or --touch ID: stop promising an interval (no flag)",
    )
    shelf.add_argument(
        "--comments",
        choices=("on", "off"),
        help=(
            "whether commenting starts on or off for a reader of the page. Will be required when"
            " FILE publishes a NEW page: pass it now in every script, because a NEW page"
            " without it will soon be refused. Off for a page a script redraws or a status"
            " board: no pill on selected words, and the reader can still turn commenting"
            " on for that page. With FILE --update ID it changes the setting; with"
            " --update ID alone it changes only the setting."
        ),
    )
    shelf.add_argument("--pin", metavar="ID", help="pin a page to the front")
    shelf.add_argument("--unpin", metavar="ID", help="unpin it")
    shelf.add_argument("--duplicate", metavar="ID", help="copy a page to a new address")
    shelf.add_argument(
        "--delete",
        metavar="ID",
        help="bin a page: recoverable for thirty days with --restore",
    )
    shelf.add_argument("--restore", metavar="ID", help="bring a binned page back")
    shelf.add_argument(
        "--deleted",
        action="store_true",
        help="list your binned pages, soonest to go first",
    )
    shelf.add_argument(
        "--give",
        metavar="ID",
        help="give one of your pages to another agent (needs --to). One-way and "
        "immediate: your next --update of it answers 404",
    )
    shelf.add_argument(
        "--to",
        metavar="AGENT",
        help="with --give: the agent receiving it, as the page list names it "
        "(homelab-ops; homelab_ops also works); with --rename-folder: the new slug",
    )
    shelf.add_argument(
        "--close-to-agents",
        metavar="ID",
        help="only {{owner_name}} and you can read this page (other agents can by default)",
    )
    shelf.add_argument(
        "--open-to-agents",
        metavar="ID",
        help="let every agent in {{owner_their}} space read it again",
    )
    shelf.add_argument(
        "--versions",
        metavar="ID",
        help="the page's history: which versions exist, when, and how big",
    )
    shelf.add_argument(
        # NOT `--version`: that is already the install stamp, and argparse
        # refuses the clash outright -- which is the good outcome. A quieter
        # library would have let one silently shadow the other.
        "--at-version",
        type=int,
        metavar="N",
        help="with --fetch, read THAT version instead of the live one",
    )
    shelf.add_argument(
        "--matches",
        metavar="ID",
        help="is FILE byte-for-byte the page's live version? exit 0 yes, 3 no "
        "(1 is a refusal, never an answer)",
    )
    shelf.add_argument(
        "--fetch",
        metavar="ID",
        help="write the page's current HTML back out, to FILE or stdout",
    )
    part = parser.add_argument_group(
        "one section of a page (with --update ID or --fetch ID)"
    )
    part.add_argument(
        "--section",
        metavar="EL",
        help="the id of ONE element on the live page; the change applies to it alone",
    )
    part.add_argument("--replace", metavar="FILE", help="FILE becomes its content")
    part.add_argument("--append", metavar="FILE", help="FILE becomes its last child")
    part.add_argument("--prepend", metavar="FILE", help="FILE becomes its first child")
    part.add_argument("--remove", action="store_true", help="remove the element")
    part.add_argument(
        "--if-revision",
        metavar="N",
        help="refuse if the page changed since this revision (hash or version);"
        " also on a whole-page --update",
    )
    part.add_argument(
        "--if-section",
        metavar="H",
        help="refuse if this section changed since --fetch printed this revision",
    )
    part.add_argument(
        "--notify",
        action="store_true",
        help="ask for {{owner_name}}'s eye: {{owner_their}} wall marks the tile (off by default)",
    )
    part.add_argument(
        "--note",
        metavar="TEXT",
        help="a line for the page's history; also on a whole-page --update",
    )
    return parser


# Flag -> (route, payload). One table rather than five branches in `main`, so a
# sixth action is a row here and cannot be added to the parser and forgotten in
# the dispatch.
_SHELF_ACTIONS = (
    ("archive", "archive", {"archived": True}),
    ("unarchive", "archive", {"archived": False}),
    ("pin", "pin", {"pinned": True}),
    ("unpin", "pin", {"pinned": False}),
    ("close_to_agents", "agents", {"can_read": False}),
    ("open_to_agents", "agents", {"can_read": True}),
)


def _touch_misuse(args) -> str | None:
    """The ways `--touch`, `--status`, `--refresh-every` and `--no-refresh` go wrong on the command
    line, as the sentence to print (exit 2), or None. One verb at a time, as `--move` is."""
    if args.refresh_every and args.no_refresh:
        return "--refresh-every and --no-refresh cannot be used together"
    if args.touch:
        other = [
            flag
            for flag, value in (
                ("FILE", args.file),
                ("--update", args.update),
                ("--list", args.list),
                ("--folders", args.folders),
                ("--folder", args.folder is not None),
                ("--no-folder", args.no_folder),
                ("--move", args.move),
                ("--archive", args.archive),
                ("--unarchive", args.unarchive),
                ("--pin", args.pin),
                ("--unpin", args.unpin),
                ("--rename", args.rename),
                ("--fetch", args.fetch),
                ("--duplicate", args.duplicate),
                ("--delete", args.delete),
                ("--restore", args.restore),
                ("--give", args.give),
                ("--section", args.section),
                ("--versions", args.versions),
                ("--preview", args.preview),
                ("--check", args.check),
                ("--comments", args.comments),
            )
            if value
        ]
        if other:
            return "--touch goes with its own options only, not with " + ", ".join(
                other
            )
        return None
    if args.status:
        return "--status goes with --touch ID"
    if args.refresh_every or args.no_refresh:
        flag = "--refresh-every" if args.refresh_every else "--no-refresh"
        if args.section:
            return (
                f"{flag} does not go with a section update; use a whole-page --update"
            )
        if args.no_refresh and not args.update:
            return "--no-refresh goes with FILE --update ID or --touch ID"
        if not args.file:
            return f"{flag} goes with FILE (publish or --update ID) or --touch ID"
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    use_session(session_label(args.session))
    # BEFORE the key is read, because this is the one thing here that needs
    # none. Half the value of a pre-publish check is being able to run it in
    # CI, or on a machine that has never been given a key, or on a page nobody
    # has decided to publish yet.
    if args.check and args.move:
        print("--move cannot be combined with --check", file=sys.stderr)
        return 2
    if args.check:
        try:
            return _check_page(args.check)
        except UsageError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    try:
        key = _resolve_key(args)
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    key = maybe_rotate(args, key)

    try:
        acted = _folder_action(args, key)
    except HTTPFailure as exc:
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
        print(f"{exc.status} {detail}", file=sys.stderr)
        return 1
    if acted is not None:
        return acted

    if args.move:
        if args.folder_title and args.no_folder:
            print("--folder-title goes with --folder, not --no-folder", file=sys.stderr)
            return 2
        if args.folder is not None and args.no_folder:
            print("--folder and --no-folder cannot be used together", file=sys.stderr)
            return 2
        if args.folder is None and not args.no_folder:
            print(
                "--move needs --folder SLUG (to file the pages) or --no-folder"
                " (to take them out)",
                file=sys.stderr,
            )
            return 2
        if args.folder == "":
            print(
                '--folder "" is empty: use --no-folder to take pages out',
                file=sys.stderr,
            )
            return 2
        other = [
            flag
            for flag, value in (
                ("FILE", args.file),
                ("--update", args.update),
                ("--list", args.list),
                ("--folders", args.folders),
                ("--archive-folder", args.archive_folder),
                ("--unarchive-folder", args.unarchive_folder),
                ("--rename-folder", args.rename_folder),
                ("--archive", args.archive),
                ("--unarchive", args.unarchive),
                ("--pin", args.pin),
                ("--unpin", args.unpin),
                ("--rename", args.rename),
                ("--fetch", args.fetch),
                ("--duplicate", args.duplicate),
                ("--touch", args.touch),
                ("--check", args.check),
                ("--preview", args.preview),
            )
            if value
        ]
        if other:
            print(
                "--move goes with --folder SLUG or --no-folder only, not with "
                + ", ".join(other),
                file=sys.stderr,
            )
            return 2
        try:
            result = _move(
                args.move,
                key,
                None if args.no_folder else args.folder,
                args.folder_title,
            )
        except HTTPFailure as exc:
            detail = (
                exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
            )
            print(f"{exc.status} {detail}", file=sys.stderr)
            return 1
        print(json.dumps({"moved": result["moved"], "missing": result["missing"]}))
        filed = result.get("folder")
        if filed and filed.get("created"):
            print(f"created new folder '{_printable(filed['slug'])}'", file=sys.stderr)
        elif filed:
            print(f"in folder '{_printable(filed['slug'])}'", file=sys.stderr)
        print(
            f"moved {len(result['moved'])}, missing {len(result['missing'])}"
            " (missing: no such page, or not yours)",
            file=sys.stderr,
        )
        return 0 if result["moved"] else 1
    misuse = _touch_misuse(args)
    if misuse:
        print(misuse, file=sys.stderr)
        return 2
    if args.folder is not None and not args.list and not args.file:
        print(
            "--folder goes with FILE (publish or --update ID) or with --list;"
            " to re-file a page without its file, publish it again with --update",
            file=sys.stderr,
        )
        return 2
    if args.no_folder and args.folder is not None:
        print("--folder and --no-folder cannot be used together", file=sys.stderr)
        return 2
    if (args.no_folder or args.folder == "") and not args.update:
        print(
            '--no-folder (or --folder "") takes a page out of its folder, so it goes'
            " with --update ID and FILE; a new page starts with no folder anyway",
            file=sys.stderr,
        )
        return 2
    if args.list and args.folder is not None and args.all:
        print(
            "--all cannot be combined with --folder: a folder is your own, and"
            " --all lists everyone's pages",
            file=sys.stderr,
        )
        return 2
    try:
        if args.folders:
            rows = _request("GET", f"{_api_base()}/folders", key, None)
            print(
                "# slug  title  pages  waiting  [archived]  (tab-separated)",
                file=sys.stderr,
            )
            for item in rows:
                print(
                    "\t".join(
                        (
                            _printable(item["slug"]),
                            _printable(item["title"]),
                            str(item["pages"]),
                            str(item["open_questions"]),
                            *(("archived",) if item.get("archived") else ()),
                        )
                    )
                )
            return 0
        if args.list:
            # The column names, on stderr so stdout stays exactly the rows every
            # caller splits on. Asked for by a seat on 24 Sep 2026 after reading
            # a date column as a version and a session label as an agent.
            # After the fetch, so a failure is the only thing on stderr.
            rows = _list(key, everyone=args.all, folder=args.folder)
            print(
                "# id  title  address  [updated  session  pinned,archived,closed]"
                "  (tab-separated; the bracketed columns appear only when set)",
                file=sys.stderr,
            )
            for item in rows:
                # A fourth column ONLY when there is something to say. Three
                # columns is what every existing caller splits on; an always-
                # present empty field would change the shape for everyone in
                # order to report the common case, which is nothing.
                shelf = ",".join(
                    word
                    for field, word in (
                        ("pinned_at", "pinned"),
                        ("archived_at", "archived"),
                    )
                    if item.get(field)
                )
                # Closed to other agents (homelab-ij8.105): said, because the
                # default is open and the exception is what a reader needs.
                if item.get("agents_can_read") is False:
                    shelf = ",".join(x for x in (shelf, "closed") if x)
                # WHEN it last moved, as a plain date, in the fourth column.
                # Without it, finding which of nine pages were stale took one
                # API call per page (reported 21 Sep). The service has always
                # returned `updated_at` here; only this printer ignored it.
                when = (item.get("updated_at") or "")[:10]
                # WHO published the version now live, and whether that is you.
                #
                # 22 Sep 2026: a key is shared by every session on its machine,
                # so a successful listing under a key says nothing about which
                # session published what. Jeremy attributed a page to the wrong
                # seat, another seat nearly rewrote it, and four of his answers
                # sat uncollected because a departed session and a live one were
                # indistinguishable. A 404 under a key is a strong negative; a
                # listing under a shared key is not a strong positive. This
                # column is what makes it one.
                #
                # Printed only when the service knows it: a page published
                # before sessions existed says nothing rather than claiming
                # nobody. "(you)" is computed here, from this process's own
                # declared session, and never sent by the service -- the service
                # has no business deciding who the reader is.
                who = item.get("last_session") or ""
                if who and _session and who == _session:
                    who = f"{who} (you)"
                # With --all, every row says whose page it is -- the session
                # label alone (e.g. claude/d7f3a6fc) does not name the machine.
                if args.all and item.get("agent"):
                    who = f"{who} by {item['agent']}" if who else f"by {item['agent']}"
                extra = "\t".join(x for x in (when, who, shelf) if x)
                suffix = f"\t{extra}" if extra else ""
                # The GALLERY address, which is the one a reader can open.
                # This column used to print the page's own origin, which 303s
                # -- a table of links none of which went anywhere useful.
                where = item.get("page_url") or item["url"]
                print(f"{item['id']}\t{item['title']}\t{where}{suffix}")
            return 0
        if args.touch:
            return _touch(args, key)
        for flag, route, payload in _SHELF_ACTIONS:
            artifact_id = getattr(args, flag)
            if artifact_id:
                _print_shelf(_shelf(route, artifact_id, key, payload))
                return 0
        # The bin, which the gallery has always had and agents did not
        # (homelab-ij8.93): two seats hand-rolled a DELETE with their key.
        if args.delete:
            print(
                json.dumps(
                    _request("DELETE", f"{_api_base()}/pages/{args.delete}", key, None)
                )
            )
            print(
                f"binned; recoverable for thirty days: artifacts-publish --restore {args.delete}",
                file=sys.stderr,
            )
            return 0
        if args.give:
            if not args.to:
                print("--give needs --to AGENT", file=sys.stderr)
                return 2
            moved = _request(
                "POST",
                f"{_api_base()}/pages/{args.give}/transfer",
                key,
                {"to": args.to},
            )
            print(json.dumps(moved))
            print(
                f"gave page {moved['id']} from {moved['from']} to {moved['to']}; "
                "you can no longer change it (--update of it now answers 404)",
                file=sys.stderr,
            )
            return 0
        if args.restore:
            _print_shelf(
                _request("POST", f"{_api_base()}/pages/{args.restore}/restore", key, {})
            )
            return 0
        if args.deleted:
            rows = _request("GET", f"{_api_base()}/pages/deleted", key, None)
            if not rows:
                print("nothing in the bin")
            for item in rows:
                gone = (item.get("deleted_at") or "")[:10]
                print(f"{item['id']}\t{item['title']}\tbinned {gone}")
            return 0
        if args.versions:
            _print_versions(_versions(args.versions, key))
            return 0
        if args.at_version is not None and not args.fetch:
            raise UsageError(
                "--at-version only means something with --fetch: "
                "artifacts-publish --fetch <id> --at-version 3 old.html"
            )
        if args.matches:
            return _matches(args.matches, args.file, key)
        if args.fetch and args.section:
            got = _fetch_section(args.fetch, args.section, key)
            if args.file:
                pathlib.Path(args.file).write_text(got["html"], encoding="utf-8")
            else:
                sys.stdout.write(got["html"])
            print(
                f"section_revision {got['section_revision']}\t"
                f"page_revision {got['page_revision']}\tversion {got['version']}",
                file=sys.stderr,
            )
            return 0
        if args.fetch:
            # To FILE when one is given, else stdout, so it composes either way:
            # `--fetch ID page.html` to edit, `--fetch ID | grep` to look. The
            # file is written whole and only on success -- a half-written one
            # would be the worst outcome for a command whose entire job is not
            # losing the source.
            html = _fetch(args.fetch, key, args.at_version)
            if args.file:
                pathlib.Path(args.file).write_text(html, encoding="utf-8")
                print(f"{len(html.encode())} bytes -> {args.file}", file=sys.stderr)
            else:
                sys.stdout.write(html)
            return 0
        if args.duplicate:
            # Its own branch rather than a row in _SHELF_ACTIONS: the others
            # change a page and hand back the same one, this makes a NEW page
            # and the caller wants its id and url, so it prints like a publish.
            _print_published(_shelf("duplicate", args.duplicate, key, {}))
            return 0
        if args.rename:
            if not args.title:
                print("--title is required with --rename", file=sys.stderr)
                return 2
            _print_shelf(_shelf("rename", args.rename, key, {"title": args.title}))
            return 0
        if args.preview:
            return _preview(args.file, args.preview, args.width, key)
        if args.update and args.section:
            warn_if_unlabelled("update")
            done = _update_section(args, key)
            print(
                json.dumps(
                    {
                        # json.dumps escapes control characters already; the
                        # strip makes the echo clean rather than merely inert.
                        k: _printable(done[k])
                        if isinstance(done.get(k), str)
                        else done.get(k)
                        for k in (
                            "page_revision",
                            "section_revision",
                            "version",
                            "unchanged",
                            "section",
                            "page_url",
                        )
                    }
                )
            )
            _print_warnings(done)
            return 0
        if args.section:
            raise UsageError("--section goes with --update ID or --fetch ID")
        if args.update and not args.file and args.comments:
            warn_if_unlabelled("update")
            done = _request(
                "POST",
                f"{_api_base()}/pages/{args.update}/comments-setting",
                key,
                {"comments": args.comments},
            )
            print(
                json.dumps({"id": done.get("id"), "comments": done.get("comments")}),
            )
            print(f"commenting now starts {args.comments}", file=sys.stderr)
            return 0
        if args.update:
            if not args.file:
                print("FILE is required with --update", file=sys.stderr)
                return 2
            warn_if_unlabelled("update")
            updated = _update(
                args.file,
                args.update,
                key,
                args.title,
                note=args.note,
                if_revision=args.if_revision,
                folder="" if args.no_folder else args.folder,
                folder_title=args.folder_title,
                refresh_every=(
                    0
                    if args.no_refresh
                    else parse_interval(args.refresh_every)
                    if args.refresh_every
                    else None
                ),
                comments=args.comments,
            )
            _print_published(updated)
            if not updated.get("unchanged"):
                _report_render(updated["id"], key)
            return 0
        if args.file and (args.title or _is_markdown(args.file)):
            # A markdown page may go without --title: its first `# heading`
            # names it, and the service refuses one that has neither.
            if not args.comments and clientlevel.COMMENTS_REQUIRED:
                print(
                    "--comments on|off is required for a new page: say whether"
                    " commenting starts on or off for a reader. Choose off for a page a"
                    " script redraws or a status board (the reader can still turn it on"
                    " for that page), on for anything else.",
                    file=sys.stderr,
                )
                return 2
            warn_if_unlabelled("publish")
            published = _publish(
                args.file,
                args.title,
                key,
                args.folder,
                args.folder_title,
                parse_interval(args.refresh_every) if args.refresh_every else None,
                comments=args.comments,
            )
            _print_published(published)
            _report_render(published["id"], key)
            return 0
        print(
            "usage: artifacts-publish FILE --title TITLE"
            " [--folder SLUG [--folder-title NAME]]"
            " | FILE --update ID [--title TITLE] [--folder SLUG | --no-folder]"
            " | --list [--folder SLUG] | --folders"
            " | --move ID [ID ...] (--folder SLUG [--folder-title NAME] | --no-folder)"
            " | --rename-folder SLUG [--to NEW] [--folder-title NAME]"
            " | --archive-folder SLUG [--confirm] | --unarchive-folder SLUG"
            " | --rename ID --title TITLE | --archive ID | --unarchive ID"
            " | --update ID --comments on|off"
            " | --pin ID | --unpin ID | --duplicate ID | --fetch ID [FILE]"
            " | --touch ID [--status ok|failed] [--note TEXT] [--if-revision REV]"
            " [--refresh-every 30m | --no-refresh]",
            file=sys.stderr,
        )
        return 2
    except HTTPFailure as exc:
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
        print(f"{exc.status} {detail}", file=sys.stderr)
        return 1
    except Conflict as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 -- last resort: `str(exc)` can carry
        # the key (e.g. http.client's "Invalid header value" embeds the whole
        # header). Only the exception's class name is ever safe to print.
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return 1


def run() -> None:
    # In run(), not main(): the console script only, so tests never touch
    # this machine's state file.
    plugin_cli.load_or_exit()
    news.announce()
    sys.exit(main())


if __name__ == "__main__":
    run()
