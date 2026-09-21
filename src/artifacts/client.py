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
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

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
    never echoes submitted content back, so this is always safe to print."""

    def __init__(self, status: int, detail) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.detail = detail


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


def _read_key(path: Path) -> str:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise UsageError(f"cannot read key file {path}: {exc.strerror}") from None
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


def _detail(raw: bytes) -> str:
    """What to SAY about an error body, which is not the same as showing it.

    An error from the service is JSON with a `detail` a person should read. An
    error from anything in front of it is an HTML page that was never addressed
    to an agent, and decoding it into a message puts bytes from the network --
    including any control characters in them -- straight into a terminal and a
    transcript. Say what it was, not what it contained.
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
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
) -> dict:
    # The process-wide declaration, unless a caller overrides it deliberately.
    session = session or _session
    _check_url_scheme(url)
    data = json.dumps(payload).encode() if payload is not None else None
    # `url` is always `{_api_base()}/...` -- an https address the operator configures
    # via ARTIFACTS_API, never a scheme or host taken from page content or a caller
    # argument. bandit's file:// concern (S310) does not apply here.
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310
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
            with _opener.open(request, timeout=_REQUEST_TIMEOUT) as response:
                raw = response.read()
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
            raise HTTPFailure(exc.code, _detail(raw)) from None
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


def _fetch(artifact_id: str, key: str) -> str:
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
    got = _request("GET", f"{_api_base()}/artifacts/{artifact_id}/body", key, None)
    body = got.get("body")
    if not isinstance(body, str):
        raise UsageError("the service returned no body for that page")
    return body


def _list(key: str) -> list[dict]:
    return _request("GET", f"{_api_base()}/artifacts", key, None)


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
                for field in ("id", "url", "revision")
                if field in result
            }
        )
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
                for field in ("id", "title", "url", "archived_at", "pinned_at")
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
    parser.add_argument("--key-file")
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
)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    use_session(declared_session(args.session))
    try:
        key = _read_key(_key_file_path(args.key_file))
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        if args.list:
            for item in _list(key):
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
                # WHEN it last moved, as a plain date, in the fourth column.
                # Without it, finding which of nine pages were stale took one
                # API call per page (reported 21 Sep). The service has always
                # returned `updated_at` here; only this printer ignored it.
                when = (item.get("updated_at") or "")[:10]
                extra = "\t".join(x for x in (when, shelf) if x)
                suffix = f"\t{extra}" if extra else ""
                print(f"{item['id']}\t{item['title']}\t{item['url']}{suffix}")
            return 0
        for flag, route, payload in _SHELF_ACTIONS:
            artifact_id = getattr(args, flag)
            if artifact_id:
                _print_shelf(_shelf(route, artifact_id, key, payload))
                return 0
        if args.fetch:
            # To FILE when one is given, else stdout, so it composes either way:
            # `--fetch ID page.html` to edit, `--fetch ID | grep` to look. The
            # file is written whole and only on success -- a half-written one
            # would be the worst outcome for a command whose entire job is not
            # losing the source.
            html = _fetch(args.fetch, key)
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
            _print_published(_update(args.file, args.update, key, args.title))
            return 0
        if args.file and args.title:
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
