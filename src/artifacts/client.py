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
from pathlib import Path
from urllib.parse import urlsplit

from artifacts import csp, traps

_DEFAULT_API = "https://artifacts.tenfourty.site/api/v1"
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
_SESSION_ENV = "ARTIFACTS_SESSION"
_SESSION_HEADER = "X-Artifacts-Session"

# Our own name on every request, never urllib's default. Measured through the
# Cloudflare edge on 23 Sep 2026, the morning the service went on the internet:
# Browser Integrity Check answers `Python-urllib/*` with 403 "error code: 1010"
# and lets any other agent string through. Without this, every agent outside
# the house using these commands was turned away before reaching the service.
_USER_AGENT = "artifacts-client/1"


def declared_session(explicit: str | None = None) -> str | None:
    """What this invocation calls itself, or None. `--session` beats the
    environment, and neither being set is a perfectly ordinary answer."""
    value = (explicit or os.environ.get(_SESSION_ENV, "") or "").strip()
    return value or None


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
            f"note: this {what} carries no session label, so his record will not say "
            f"which session did it. Set {_SESSION_ENV} (in Claude Code, in "
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

    def __init__(self, status: int, detail) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.detail = detail

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
    misconfigured ARTIFACTS_API that ends up plain http:// to a real host can
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
    return os.environ.get("ARTIFACTS_API", _DEFAULT_API)


def _key_file_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("ARTIFACTS_KEY_FILE")
    if env:
        return Path(env)
    return Path(_DEFAULT_KEY_FILE).expanduser()


# Set by the key wrapper to the descriptor it opened. It carries the NUMBER,
# never the key, so it is not a secret and /proc/<pid>/environ tells a reader
# nothing they could use. This exists so one wrapper can serve a command it
# does not have to know the argument order of -- `artifacts-key -- <anything>`.
_KEY_FD_ENV = "ARTIFACTS_KEY_FD"


def _resolve_key(args) -> str:
    """The key, from a descriptor or a file -- never quietly from both.

    Refusing the combination rather than preferring one: a caller that passed
    both meant something, and silently honouring the other is how a wrapper
    ends up reading the cleartext file it was written to stop using, with
    everything appearing to work. That is the whole failure ij8.34 is about,
    so it fails loudly instead.
    """
    env_fd = os.environ.get(_KEY_FD_ENV, "").strip()
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
        raise UsageError(
            f"cannot read key file {path}: {exc.strerror}. On baba the keys "
            "are vaulted: run the command through "
            "`ARTIFACTS_SEAT=<seat> artifacts-key -- …` (seats: baba_operator, "
            "homelab_ops, warpgate_arc, cove_seat). Elsewhere, point "
            "--key-file or --key-fd at your own store."
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
            "challenge: ask Jeremy to exempt agent calls to /api/v1/"
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
) -> dict:
    # `timeout`: a held /events request needs longer than the default 30 s.
    # `headers_out`: filled with the response headers, for the one caller that
    # reads one (the event cursor, homelab-ij8.106).
    # The process-wide declaration, unless a caller overrides it deliberately.
    session = session or _session
    _check_url_scheme(url)
    data = json.dumps(payload).encode() if payload is not None else None
    # `url` is always `{_api_base()}/...` -- an https address the operator configures
    # via ARTIFACTS_API, never a scheme or host taken from page content or a caller
    # argument. bandit's file:// concern (S310) does not apply here.
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310
    request.add_header("User-Agent", _USER_AGENT)
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
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for attempt in range(_ROLLING_ATTEMPTS):
        try:
            with _opener.open(request, timeout=timeout or _REQUEST_TIMEOUT) as response:
                raw = response.read()
                if headers_out is not None:
                    headers_out.update({k.lower(): v for k, v in response.headers.items()})
            break
        except urllib.error.HTTPError as exc:
            raw = exc.read()
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
            raise HTTPFailure(exc.code, _detail(raw, exc.headers)) from None
        except urllib.error.URLError as exc:
            raise HTTPFailure(0, str(exc.reason)) from None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # A 2xx that isn't JSON is not something to guess at or half-parse --
        # treat it exactly like any other failure to get a usable answer.
        raise HTTPFailure(0, "unexpected response") from None


def _read_page(file_path: str) -> str:
    path = Path(file_path)
    if not path.is_file():
        raise UsageError(f"no such file: {file_path}")
    return path.read_text()


def _publish(file_path: str, title: str, key: str) -> dict:
    body = _read_page(file_path)
    return _request(
        "POST", f"{_api_base()}/artifacts", key, {"title": title, "body": body}
    )


def _read_current(artifact_id: str, key: str) -> dict:
    return _request("GET", f"{_api_base()}/artifacts/{artifact_id}", key, None)


def _patch(
    file_path: str,
    artifact_id: str,
    expected_revision: str,
    key: str,
    title: str | None = None,
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
    payload = {"body": body, "expected_revision": expected_revision}
    if title is not None:
        payload["title"] = title
    try:
        return _request(
            "PATCH", f"{_api_base()}/artifacts/{artifact_id}", key, payload
        )
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
    file_path: str, artifact_id: str, key: str, title: str | None = None
) -> dict:
    expected_revision = _read_current(artifact_id, key)["revision"]
    return _patch(file_path, artifact_id, expected_revision, key, title)


def _versions(artifact_id: str, key: str) -> list[dict]:
    return _request("GET", f"{_api_base()}/artifacts/{artifact_id}/versions", key, None)


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
        who = row.get("session") or row.get("created_by") or ""
        when = (row.get("created_at") or "")[:19].replace("T", " ")
        print(
            f"{mark} v{row['n']}\t{row.get('revision','')}\t"
            f"{row.get('bytes', 0)}\t{when}\t{who}"
        )
    print("* is the version on the wall now", file=sys.stderr)


def _fetch(artifact_id: str, key: str, version: int | None = None) -> str:
    """The page's own HTML, back to the agent that published it.

    `GET /artifacts/{id}/body` has existed since 19 Sep and nobody could find
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
        f"/artifacts/{artifact_id}/versions/{version}"
        if version is not None
        else f"/artifacts/{artifact_id}/body"
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


def _list(key: str, everyone: bool = False) -> list[dict]:
    # `everyone`: every page this key may read, other agents' open pages
    # included (homelab-ij8.105), not only its own.
    scope = "?scope=all" if everyone else ""
    return _request("GET", f"{_api_base()}/artifacts{scope}", key, None)


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
    return _request(
        "POST", f"{_api_base()}/artifacts/{artifact_id}/{action}", key, payload
    )


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
        csp.check(body) + traps.check(body), key=lambda w: (w.line, w.shape)
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
    (`artifacts.csp`), derived from the real directives in the served policy,
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
    body = _read_page(file_path)
    findings, syntax, node_ran = check_page(body)
    # THE SERVICE'S OWN SECRET SCAN, run here too (homelab-ij8.106). The first
    # outside agent's page passed --check and was then refused on publish with
    # a 422: this ran only the CSP half, never the scan that actually refuses.
    # Same module, same shapes file, so it cannot disagree with the service.
    from artifacts import scan  # stdlib-only; imported here to keep startup light

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
                for field in ("id", "title", "url", "archived_at", "pinned_at", "agents_can_read")
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


class _Parser(argparse.ArgumentParser):
    """argparse, except that a refused flag says how old this build is."""

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
                    f"or set $ARTIFACTS_SESSION once for the whole session."
                )
            else:
                message += _STALE_HINT.format(
                    prog=self.prog.split()[0], stamp=installed_stamp()
                )
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="artifacts-publish")
    parser.add_argument("file", nargs="?", metavar="FILE")
    # So the question can be asked BEFORE it becomes a surprise: a seat about to
    # rely on a recent flag can check what it is running in one command.
    parser.add_argument(
        "--version",
        action="version",
        version=f"artifacts-publish, installed {installed_stamp()}",
    )
    parser.add_argument("--title")
    parser.add_argument("--update", metavar="ID")
    parser.add_argument("--list", action="store_true")
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
        "--session",
        metavar="LABEL",
        help=(
            "which session this is, when several share one key; defaults to "
            f"${_SESSION_ENV}. A label, never a secret."
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
    shelf.add_argument("--pin", metavar="ID", help="pin a page to the front")
    shelf.add_argument("--unpin", metavar="ID", help="unpin it")
    shelf.add_argument("--duplicate", metavar="ID", help="copy a page to a new address")
    shelf.add_argument(
        "--close-to-agents",
        metavar="ID",
        help="only Jeremy and you can read this page (other agents can by default)",
    )
    shelf.add_argument(
        "--open-to-agents", metavar="ID", help="let every agent in his space read it again"
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
        "--fetch",
        metavar="ID",
        help="write the page's current HTML back out, to FILE or stdout",
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    use_session(declared_session(args.session))
    # BEFORE the key is read, because this is the one thing here that needs
    # none. Half the value of a pre-publish check is being able to run it in
    # CI, or on a machine that has never been given a key, or on a page nobody
    # has decided to publish yet.
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

    try:
        if args.list:
            for item in _list(key, everyone=args.all):
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
        for flag, route, payload in _SHELF_ACTIONS:
            artifact_id = getattr(args, flag)
            if artifact_id:
                _print_shelf(_shelf(route, artifact_id, key, payload))
                return 0
        if args.versions:
            _print_versions(_versions(args.versions, key))
            return 0
        if args.at_version is not None and not args.fetch:
            raise UsageError(
                "--at-version only means something with --fetch: "
                "artifacts-publish --fetch <id> --at-version 3 old.html"
            )
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
        if args.update:
            if not args.file:
                print("FILE is required with --update", file=sys.stderr)
                return 2
            warn_if_unlabelled("update")
            _print_published(_update(args.file, args.update, key, args.title))
            return 0
        if args.file and args.title:
            warn_if_unlabelled("publish")
            _print_published(_publish(args.file, args.title, key))
            return 0
        print(
            "usage: artifacts-publish FILE --title TITLE"
            " | FILE --update ID [--title TITLE] | --list"
            " | --rename ID --title TITLE | --archive ID | --unarchive ID"
            " | --pin ID | --unpin ID | --duplicate ID | --fetch ID [FILE]",
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
    sys.exit(main())


if __name__ == "__main__":
    run()
