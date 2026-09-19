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
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

_DEFAULT_API = "https://artifacts.tenfourty.site/api/v1"
_DEFAULT_KEY_FILE = "~/.config/artifacts/key"


class UsageError(Exception):
    """A problem with how the command was invoked or its key file -- exit 2."""


class HTTPFailure(Exception):
    """The server answered with a non-2xx status -- exit 1. `detail` is
    whatever the server's JSON body carried under "detail" (a string, or a
    structured refusal like `{"refused": [...]}`); the server never echoes
    submitted content back, so this is always safe to print."""

    def __init__(self, status: int, detail) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.detail = detail


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
    return path.read_text().strip()


def _request(method: str, url: str, key: str, payload: dict | None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    # `url` is always `{_api_base()}/...` -- an https address the operator configures
    # via ARTIFACTS_API, never a scheme or host taken from page content or a caller
    # argument. bandit's file:// concern (S310) does not apply here.
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310
    request.add_header("Authorization", f"Bearer {key}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310
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
    return json.loads(raw) if raw else {}


def _publish(file_path: str, title: str, key: str) -> dict:
    body = Path(file_path).read_text()
    return _request("POST", f"{_api_base()}/artifacts", key, {"title": title, "body": body})


def _read_current(artifact_id: str, key: str) -> dict:
    return _request("GET", f"{_api_base()}/artifacts/{artifact_id}", key, None)


def _patch_with_retry(
    file_path: str, artifact_id: str, expected_revision: str, key: str
) -> dict:
    """PATCH with `expected_revision`; on a 409 (someone else patched first),
    re-read the artifact's now-current revision and retry exactly once, then
    let a second failure propagate."""
    body = Path(file_path).read_text()
    for attempt in range(2):
        try:
            return _request(
                "PATCH",
                f"{_api_base()}/artifacts/{artifact_id}",
                key,
                {"body": body, "expected_revision": expected_revision},
            )
        except HTTPFailure as exc:
            if exc.status == 409 and attempt == 0:
                expected_revision = _read_current(artifact_id, key)["revision"]
                continue
            raise
    raise AssertionError("unreachable")  # pragma: no cover


def _update(file_path: str, artifact_id: str, key: str) -> dict:
    expected_revision = _read_current(artifact_id, key)["revision"]
    return _patch_with_retry(file_path, artifact_id, expected_revision, key)


def _list(key: str) -> list[dict]:
    return _request("GET", f"{_api_base()}/artifacts", key, None)


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
            print(json.dumps(_update(args.file, args.update, key)))
            return 0
        if args.file and args.title:
            print(json.dumps(_publish(args.file, args.title, key)))
            return 0
        print(
            "usage: artifacts-publish FILE --title TITLE | FILE --update ID | --list",
            file=sys.stderr,
        )
        return 2
    except HTTPFailure as exc:
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
        print(f"{exc.status} {detail}", file=sys.stderr)
        return 1


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
