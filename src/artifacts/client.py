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
import re
import stat
import sys
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


class UsageError(Exception):
    """A problem with how the command was invoked or its key file -- exit 2."""


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


def _request(method: str, url: str, key: str, payload: dict | None) -> dict:
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
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with _opener.open(request, timeout=_REQUEST_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw).get("detail", raw.decode(errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = raw.decode(errors="replace")
        raise HTTPFailure(exc.code, detail) from None
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


def _patch_with_retry(
    file_path: str,
    artifact_id: str,
    expected_revision: str,
    key: str,
    title: str | None = None,
) -> dict:
    """PATCH with `expected_revision`; on a 409 (someone else patched first),
    re-read the artifact's now-current revision and retry exactly once, then
    let a second failure propagate. `title` renames the page in the same call;
    omitted, the page keeps the name it has."""
    body = _read_page(file_path)
    payload = {"body": body, "expected_revision": expected_revision}
    if title is not None:
        payload["title"] = title
    for attempt in range(2):
        try:
            return _request(
                "PATCH",
                f"{_api_base()}/artifacts/{artifact_id}",
                key,
                {**payload, "expected_revision": expected_revision},
            )
        except HTTPFailure as exc:
            if exc.status == 409 and attempt == 0:
                expected_revision = _read_current(artifact_id, key)["revision"]
                continue
            raise
    raise AssertionError("unreachable")  # pragma: no cover


def _update(
    file_path: str, artifact_id: str, key: str, title: str | None = None
) -> dict:
    expected_revision = _read_current(artifact_id, key)["revision"]
    return _patch_with_retry(file_path, artifact_id, expected_revision, key, title)


def _list(key: str) -> list[dict]:
    return _request("GET", f"{_api_base()}/artifacts", key, None)


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="artifacts-publish")
    parser.add_argument("file", nargs="?", metavar="FILE")
    parser.add_argument("--title")
    parser.add_argument("--update", metavar="ID")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--key-file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    try:
        key = _read_key(_key_file_path(args.key_file))
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        if args.list:
            for item in _list(key):
                print(f"{item['id']}\t{item['title']}\t{item['url']}")
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
            " | FILE --update ID [--title TITLE] | --list",
            file=sys.stderr,
        )
        return 2
    except HTTPFailure as exc:
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
        print(f"{exc.status} {detail}", file=sys.stderr)
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
