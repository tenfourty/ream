"""`ream`: one command with verbs, in front of the tools that already exist.

    ream publish page.html        # artifacts-publish
    ream ask ...                  # artifacts-decide ask ...
    ream wait ...                 # artifacts-decide wait ...
    ream admin ...                # artifacts-admin

Each verb hands the rest of the command line, untouched, to the existing program, so
`ream <verb> ARGS` behaves as the old command with the same ARGS. The `artifacts-*` commands
stay for good. Standard library only at import: a tool is loaded when its verb is used.
"""

import importlib
import sys

# verb -> (module, function). A function is called with no arguments, as a console script is,
# and reads sys.argv; its return value is the exit status.
_OTHER = {
    "publish": ("ream.client", "run"),
    "admin": ("ream.admin", "main"),
    "republish": ("ream.republish", "main"),
    "connect": ("ream.connect_cli", "run"),
    "beads-page": ("ream.beadpage", "run"),
    "bead-metrics": ("ream.beadmetrics", "main"),
}
# The verbs of `artifacts-decide`, kept as the command's own sub-commands.
DECIDE_VERBS = (
    "ack",
    "answers",
    "ask",
    "ask-review",
    "close",
    "comment",
    "comments",
    "list",
    "listening",
    "outcome",
    "pickup",
    "receipt",
    "record",
    "reopen",
    "reply",
    "reply-note",
    "review-release",
    "settle",
    "wait",
)

_HELP = """usage: ream [GLOBAL FLAGS] VERB [ARGS ...]

One command for the tools. Each verb runs the program of the same job and passes the rest of
the command line on unchanged, so `ream VERB --help` shows that program's own help.

  publish        publish or update a page             (artifacts-publish)
  ask            put a question on a page             (artifacts-decide ask)
  wait           wait for an answer                   (artifacts-decide wait)
  {decide_rest}
  admin          keys, scopes, renames, backups       (artifacts-admin)
  republish      republish a seat's pages             (artifacts-republish)
  connect        connect a machine                    (artifacts-connect)
  beads-page     a page of the tracker's beads        (artifacts-beads-page)
  bead-metrics   an arc's counts and velocity         (artifacts-bead-metrics)

The old `artifacts-*` commands keep working.
"""


def _help() -> str:
    rest = [v for v in DECIDE_VERBS if v not in ("ask", "wait")]
    lines = ", ".join(rest)
    return _HELP.format(decide_rest=f"also (artifacts-decide): {lines}")


def _find_verb(args: list[str]) -> int | None:
    known = set(_OTHER) | set(DECIDE_VERBS)
    for index, token in enumerate(args):
        if token in known:
            return index
    return None


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args == ["-h"] or args == ["--help"] or args == ["help"]:
        stream = sys.stdout if args else sys.stderr
        stream.write(_help())
        raise SystemExit(0 if args else 2)
    index = _find_verb(args)
    if index is None:
        sys.stderr.write(f"ream: no such verb in: {' '.join(args)}\n\n{_help()}")
        raise SystemExit(2)
    verb = args[index]
    before, after = args[:index], args[index + 1 :]
    if verb in DECIDE_VERBS:
        module_name, function_name = "ream.decide", "run"
        # decide keeps its verb as a sub-command, after its global flags.
        rest = before + [verb] + after
    else:
        module_name, function_name = _OTHER[verb]
        rest = before + after
    # Hint text in the old programs names `sys.argv[0]`; "ream <verb>" reads right there.
    sys.argv = [f"ream {verb}"] + rest
    function = getattr(importlib.import_module(module_name), function_name)
    result = function()
    raise SystemExit(result)
