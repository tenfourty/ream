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

from ream import plugin_cli, selfupdate  # noqa: F401 -- first: it records the real argv

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

One command for Ream (formerly artifacts). Each verb runs the program of the same job and passes the rest of
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
{plugin_section}
The old `artifacts-*` commands keep working.
"""


def _help() -> str:
    rest = [v for v in DECIDE_VERBS if v not in ("ask", "wait")]
    lines = ", ".join(rest)
    # Plug-in verbs, when any is installed; with none the section is empty and the text is
    # exactly what it was before plug-ins.
    extra = plugin_cli.commands()
    section = (
        "\nFrom plug-ins:\n" + "".join(f"  {c.name:<14} {c.help}\n" for c in extra)
        if extra
        else ""
    )
    return _HELP.format(
        decide_rest=f"also (artifacts-decide): {lines}", plugin_section=section
    )


# The global flags of `artifacts-decide` that take a value; they may come before the verb.
_VALUE_FLAGS = ("--key-file", "--key-fd", "--session")


def prog_name(default: str) -> str:
    """The program name to show in usage and errors: `ream <verb>` (or `ream`) when run
    through this dispatcher, which sets sys.argv[0] so; otherwise the old `artifacts-*`
    command's own name."""
    first = sys.argv[0] if sys.argv else ""
    return first if first == "ream" or first.startswith("ream ") else default


def _find_verb(args: list[str]) -> int | None:
    """The verb is the first positional word. Only decide's own global flags may come
    before it; anything else in front (`ream --title list publish ...`) is no verb."""
    known = set(_OTHER) | set(DECIDE_VERBS) | {c.name for c in plugin_cli.commands()}
    index = 0
    while index < len(args):
        token = args[index]
        if token in _VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            if token.split("=", 1)[0] in _VALUE_FLAGS:
                index += 1
                continue
            return None
        return index if token in known else None
    return None


def _run_plugin_verb(command) -> int:
    """Run a plug-in's verb as a console script. A fault prints the exception's type and
    nothing else: its text may carry a value."""
    try:
        result = command.main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- a plug-in's fault, reported by type only
        sys.stderr.write(f"ream {command.name}: error: {type(exc).__name__}\n")
        return 1
    return result if isinstance(result, int) else 0


def main(argv: list[str] | None = None) -> None:
    # The plug-ins selected by REAM_PLUGINS register their verbs and client defaults first.
    plugin_cli.load_or_exit()
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
    plugin_verb = plugin_cli.command(verb)
    if plugin_verb is not None:
        sys.argv = [f"ream {verb}"] + args[:index] + args[index + 1 :]
        raise SystemExit(_run_plugin_verb(plugin_verb))
    before, after = args[:index], args[index + 1 :]
    if verb in DECIDE_VERBS:
        module_name, function_name = "ream.decide", "run"
        # decide keeps its verb as a sub-command, after its global flags.
        rest = before + [verb] + after
    else:
        module_name, function_name = _OTHER[verb]
        rest = before + after
    # The old programs show `prog_name(...)` in usage and errors; "ream <verb>" reads right there.
    # decide keeps its verb among its own arguments, so its usage reads `ream ask ...`.
    sys.argv = ["ream" if verb in DECIDE_VERBS else f"ream {verb}"] + rest
    function = getattr(importlib.import_module(module_name), function_name)
    result = function()
    raise SystemExit(result)
