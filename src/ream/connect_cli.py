"""artifacts-connect: join this machine to Jeremy's artifacts space.

homelab-ij8.102. Asks the service for access, prints a short code for Jeremy
to approve in his gallery, waits, and saves the key it is given to the file the
other tools read -- WITHOUT EVER PRINTING IT. The key never passes through a
terminal an agent is reading, so it never lands in a transcript, and he never
has to handle it at all.

Standard library only, like the rest of the tools: this runs through `uvx` on
a machine that has never seen this house.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from ream import client, plugin_cli, selfupdate, textvars
from ream.cli import prog_name

_WAIT_LIMIT_SECONDS = 16 * 60


def _post(url: str, body: dict) -> dict:
    client._check_url_scheme(url)
    request = urllib.request.Request(  # noqa: S310 -- https (or loopback http), checked above
        url, data=json.dumps(body).encode(), method="POST"
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", client._USER_AGENT)
    client._add_client_level(request)
    selfupdate.note_mutation()  # a POST: no re-exec may repeat it
    try:
        with client._opener.open(request, timeout=client._REQUEST_TIMEOUT) as response:
            raw = response.read()
            selfupdate.on_response(
                "POST", {k.lower(): v for k, v in response.headers.items()}, url
            )
            return json.loads(raw or b"{}")
    except urllib.error.HTTPError as exc:
        selfupdate.on_response(
            "POST", {k.lower(): v for k, v in (exc.headers or {}).items()}, url
        )
        raise client.HTTPFailure(
            exc.code, client._detail(exc.read(), exc.headers)
        ) from None
    except urllib.error.URLError as exc:
        raise client.HTTPFailure(0, str(exc.reason)) from None


def _save_key(path: Path, key: str) -> None:
    client.save_key_file(path, key)


def _start_move(args) -> int:
    """On the machine that already has a key: ask the service for a move token for THIS
    key's own agent (POST /agents/me/move). The token is a one-time credential for the
    new machine; it is written to --out (mode 600) when asked, else printed."""
    args.key_fd = (
        None  # this command has no --key-fd; _resolve_key reads the file or env
    )
    try:
        key = client._resolve_key(args)
        made = client._request("POST", f"{client._api_base()}/agents/me/move", key, {})
    except (client.HTTPFailure, client.UsageError) as exc:
        print(f"artifacts-connect: could not start a move: {exc}", file=sys.stderr)
        return 1
    token = made["move_token"]
    if args.out:
        client.save_key_file(Path(args.out), token)
        where = f"The move token is in {args.out}"
    else:
        print(token)
        where = "The move token is printed above"
    print(
        f"{where}. It works once, for {made['expires_in'] // 60} minutes. On the new "
        "machine run: ream connect --move-token-stdin  (token on stdin). This "
        f"machine's keys for {made['agent']} stop working once the new one is used.",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=prog_name("artifacts-connect"),
        description="connect this machine to {{owner_name}}'s Ream (formerly artifacts); {{owner_they}} approve{{owner_s}} a code. Also run as `ream connect`.",
        formatter_class=client._HelpFormatter,
    )
    parser.add_argument(
        "--name", help="what to call this machine (default: its hostname)"
    )
    parser.add_argument(
        "--key-file", help=f"where to save the key (default {client._DEFAULT_KEY_FILE})"
    )
    parser.add_argument(
        "--force", action="store_true", help="replace a key this machine already has"
    )
    parser.add_argument(
        "--start-move",
        action="store_true",
        help="run on the machine that already has a key: ask for a one-time move token "
        "so a NEW machine joins as this same agent (its pages, comments and decisions "
        "stay); this machine's keys stop working once the new one is first used",
    )
    parser.add_argument(
        "--out",
        help="with --start-move: write the move token to this file (mode 600) instead of printing it",
    )
    parser.add_argument(
        "--move-token-stdin",
        action="store_true",
        help="run on the NEW machine: read the move token from stdin and collect a key "
        "for the same agent, with no one to approve",
    )
    args = parser.parse_args(argv)
    if args.start_move:
        return _start_move(args)

    key_path = client._key_file_path(args.key_file)
    if key_path.exists() and not args.force:
        print(
            f"this machine already has a key at {key_path}. The other tools will use "
            "it. Pass --force to connect again and replace it.",
            file=sys.stderr,
        )
        return 1

    base = client._api_base()
    if args.move_token_stdin:
        token = sys.stdin.readline().strip()
        if not token:
            print("artifacts-connect: no move token on stdin", file=sys.stderr)
            return 2
        started = {"poll_token": token, "expires_in": 15 * 60, "interval": 5}
        print("Collecting a key for the agent that started this move...", flush=True)
    else:
        try:
            started = _post(
                f"{base}/connect", {"name": args.name or socket.gethostname()}
            )
        except (client.HTTPFailure, client.UsageError) as exc:
            print(
                f"artifacts-connect: could not ask for access: {exc}", file=sys.stderr
            )
            return 1
        print(
            textvars.words("Ask {{owner_name}} to approve this machine.\n\n")
            + f"    Code:  {started['code']}\n"
            + f"    Where: {started['approve_at']}\n\n"
            + textvars.words("Waiting for {{owner_them}} ")
            + f"(the code expires in {started['expires_in'] // 60} minutes)...",
            flush=True,
        )
    interval = max(2, int(started.get("interval", 5)))
    deadline = time.monotonic() + min(_WAIT_LIMIT_SECONDS, started["expires_in"] + 60)
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            answer = _post(
                f"{base}/connect/poll", {"poll_token": started["poll_token"]}
            )
        except client.HTTPFailure as exc:
            if exc.status in (0, 502, 503, 504):
                continue  # the network or a deploy blinked; keep waiting
            print(f"artifacts-connect: {exc}", file=sys.stderr)
            return 1
        status = answer.get("status")
        if status == "pending":
            continue
        if status == "approved" and answer.get("key"):
            _save_key(key_path, answer["key"])
            print(
                f"Connected as {answer.get('agent')}. The key is saved to {key_path} "
                "and was not shown; the other tools will find it there."
            )
            return 0
        if status == "denied":
            print(
                textvars.words(
                    "{{owner_name}} denied this request. Nothing was saved."
                ),
                file=sys.stderr,
            )
            return 1
        if status == "delivered":
            print(
                "this request's key was already collected by an earlier poll; it is "
                + textvars.words(
                    "not shown twice. Ask {{owner_name}} to revoke it and connect again."
                ),
                file=sys.stderr,
            )
            return 1
        print(
            "the code expired before it was approved. Run this again.", file=sys.stderr
        )
        return 1
    print("gave up waiting. Run this again for a fresh code.", file=sys.stderr)
    return 1


def run() -> None:
    plugin_cli.load_or_exit()
    sys.exit(main())
