"""`artifacts-admin`: key tooling for the artifacts service.

A bearer token must never pass through a terminal, a transcript, a log, or a
network hop. `new-key` is built around that: it generates the token WHERE IT
WILL BE USED (an agent's own machine, or wherever the operator runs it right
before a `kubectl exec`), needs no database at all, and writes the token
straight to a file that only its owner can read. From there on, only the
token's SHA-256 ever travels anywhere -- typed into `register-key`, piped
over `kubectl exec`, or printed by `list-keys`. Neither `new-key`'s own
output nor any other command in this module ever prints the token itself.
"""

import argparse
import hashlib
import os
import re
import secrets
import sys

from artifacts import keys
from artifacts.db import Database

_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _check_agent(agent: str) -> None:
    if not _AGENT_NAME.fullmatch(agent):
        raise SystemExit(
            f"invalid agent name {agent!r}: must match {_AGENT_NAME.pattern!r}"
        )


def _database_url() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set")
    return dsn


def cmd_new_key(args: argparse.Namespace) -> None:
    _check_agent(args.agent)
    # Matches keys.mint()'s own format exactly -- an agent registered by hand
    # through this CLI must verify identically to one minted in-process.
    token = "art_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()

    path = args.out
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
        os.chmod(parent, 0o700)  # explicit: umask must never weaken this

    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(
            f"refusing to overwrite an existing key file: {path}"
        ) from None
    try:
        os.write(fd, token.encode())  # no trailing newline: the file IS the token
    finally:
        os.close(fd)

    print(f"agent: {args.agent}")
    print(f"path: {path}")
    print(f"sha256={digest}")
    print(
        "kubectl -n artifacts exec deploy/artifacts -- artifacts-admin "
        f"register-key --agent {args.agent} --sha256 {digest}"
    )


def cmd_register_key(args: argparse.Namespace) -> None:
    _check_agent(args.agent)
    if not _SHA256_HEX.fullmatch(args.sha256):
        raise SystemExit("sha256 must be exactly 64 lowercase hex characters")
    db = Database(_database_url())
    key_id = keys.register(db, args.agent, args.sha256)
    print(f"key_id={key_id} agent={args.agent}")


def cmd_revoke_key(args: argparse.Namespace) -> None:
    db = Database(_database_url())
    keys.revoke(db, args.key_id)


def cmd_list_keys(_args: argparse.Namespace) -> None:
    db = Database(_database_url())
    for row in keys.list_keys(db):
        print(f"{row.id}\t{row.agent}\t{row.created_at}\t{row.revoked_at or '-'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="artifacts-admin")
    sub = parser.add_subparsers(dest="command", required=True)

    new_key = sub.add_parser(
        "new-key", help="generate a token on this machine; needs no database"
    )
    new_key.add_argument("--agent", required=True)
    new_key.add_argument("--out", required=True)
    new_key.set_defaults(func=cmd_new_key)

    register_key = sub.add_parser(
        "register-key", help="register a token's SHA-256 (needs DATABASE_URL)"
    )
    register_key.add_argument("--agent", required=True)
    register_key.add_argument("--sha256", required=True)
    register_key.set_defaults(func=cmd_register_key)

    revoke_key = sub.add_parser("revoke-key")
    revoke_key.add_argument("key_id")
    revoke_key.set_defaults(func=cmd_revoke_key)

    list_keys_parser = sub.add_parser("list-keys")
    list_keys_parser.set_defaults(func=cmd_list_keys)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    args.func(args)


if __name__ == "__main__":
    main()
