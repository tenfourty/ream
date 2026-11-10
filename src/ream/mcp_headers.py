"""`ream mcp-headers`: the HTTP headers a Claude Code MCP server entry asks a helper for.

Claude Code's `headersHelper` runs a command and reads ONE JSON object of HTTP headers from its
stdout. This is that command for Ream, so a session sets up the same way on every machine:

    claude mcp add-json --scope local ream \\
      '{"type":"http","url":"<gallery>/mcp","headersHelper":"ream mcp-headers"}'

It prints exactly one line, `{"Authorization": "Bearer <key>", "X-Ream-Session": "<label>"}`, and
nothing else on stdout; the second header is left out unless `REAM_SESSION` (or `--session`) is
set, and the server then labels the session itself. The key is found the way every other verb
finds it (`--key-file`, `--key-fd`, `REAM_KEY_FILE`, `REAM_KEY_FD`, else
`~/.config/artifacts/key`), by the same functions, so the same refusals apply: a file other users
can read, or anything that is not shaped like a key.

What it never does: make a request (so it is fast, and there is nothing to retry or update from),
check for a newer tool, print the news, write the key anywhere, or put it on an argument list.
Every problem goes to stderr, in words that cannot contain the key, with a non-zero exit and an
empty stdout: Claude Code treats that as "no headers" and the server answers 401, which is the
honest outcome of a missing key.

Standard library only, like the rest of the tools.
"""

from __future__ import annotations

import json
import sys

from ream import client
from ream.cli import prog_name

# The longest label the server accepts (`api.session_label`: 120 characters, else 422 on
# every request). Checked here so a bad value stops the helper, not each call of the session.
_SESSION_MAX = 120


def build_parser() -> client._Parser:
    parser = client._Parser(
        prog=prog_name("ream mcp-headers"),
        description=(
            "Print the HTTP headers for Ream's MCP server (the Authorization header from "
            "this agent's key, and X-Ream-Session when a session is declared) as one JSON "
            "object on one line. It is a Claude Code `headersHelper`: it makes no request "
            "and prints nothing else to stdout. "
            'Set it up with: claude mcp add-json --scope local ream \'{"type":"http",'
            '"url":"<gallery>/mcp","headersHelper":"ream mcp-headers"}\''
        ),
    )
    parser.add_argument(
        "--key-file",
        help=f"the file holding the agent key (default {client._DEFAULT_KEY_FILE}, "
        "or $REAM_KEY_FILE)",
    )
    parser.add_argument(
        "--key-fd",
        type=int,
        metavar="N",
        help="read the key from an already-open descriptor instead of a file, so it "
        "never touches disk (or set $REAM_KEY_FD)",
    )
    parser.add_argument(
        "--session",
        help="the session label to send as X-Ream-Session, e.g. <agent>/<session ref> "
        "(default: $REAM_SESSION; none declared: the header is left out)",
    )
    return parser


def headers(args) -> dict[str, str]:
    """The headers to print. Raises `client.UsageError` for anything the caller must fix;
    its text never carries the key."""
    # The key first: with none, nothing else matters and nothing is printed.
    key = client._resolve_key(args)
    out = {"Authorization": f"Bearer {key}"}
    session = client.declared_session(args.session)
    if session:
        # A label, not a secret, but a bad one would make the server refuse every call of
        # the session (or the header not survive HTTP); say so once, here. The value itself
        # is not echoed.
        if (
            len(session) > _SESSION_MAX
            or not session.isascii()
            or not session.isprintable()
        ):
            raise client.UsageError(
                f"the session label must be printable ASCII of at most {_SESSION_MAX} "
                f"characters ({len(session)} read); fix --session or ${client._SESSION_ENV}"
            )
        out["X-Ream-Session"] = session
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    try:
        line = json.dumps(headers(args))
    except client.UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 -- last resort: only the class name is safe to
        # print, because an exception's text can carry the value being handled.
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return 1
    # The one write to stdout, and only once everything above has succeeded.
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return 0
