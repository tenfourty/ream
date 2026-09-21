"""`artifacts-decide`: pose a question to Jeremy, and read what he said back.

homelab-ij8.24. Asking him a lettered question is the single most important
thing this service does -- it is the reason it exists rather than being a
pastebin -- and until now it was the one thing with no command. Publishing had
`artifacts-publish` from the first day; decisions shipped the HTTP API and
nothing else, so every seat that wanted to ask him something hand-rolled a curl
with its key on the command line. Two of those were written in this very
session, by the seat that owns the service, which is about as clear a signal as
the gap can give.

Jeremy's standing criterion, 20 Sep 2026: "ideally agents can take all the same
actions a human can." His side of a decision has been a browser form since the
beginning. This is the other side of it.

The key is read exactly as `artifacts-publish` reads it -- same file, same
shape check, same `Authorization: Bearer` and nothing else. It never crosses a
command line and never appears in output, including in an error.

THE ONE ASYMMETRY IS DELIBERATE and is not fixed here. An agent may not ANSWER
as him. `record` exists instead: it stores words he gave somewhere else with
the source and the seat that relayed them, so the answer survives without being
disguised as a tap. The service refuses `--source page` for that reason, and so
this never offers it.
"""

import argparse
import sys

from artifacts.client import (
    Conflict,
    HTTPFailure,
    UsageError,
    _api_base,
    _key_file_path,
    _read_key,
    _request,
    declared_session,
    installed_stamp,
    use_session,
)
from artifacts.client import (
    _Parser as _StaleAwareParser,
)


def _options(pairs: list[str]) -> list[dict]:
    """`--option a=Do the simple thing` -> [{"letter": "a", "label": "..."}].

    Split on the FIRST `=` only, so a label may contain one. The letter is
    whatever precedes it, lowercased and stripped -- the service validates the
    set (unique, sequential from "a", non-empty labels) and returns one error
    naming the field, so this does not second-guess it. Duplicating that
    validation here would give two different messages for the same mistake
    depending on which layer caught it first.
    """
    options = []
    for pair in pairs:
        letter, sep, label = pair.partition("=")
        if not sep or not label.strip():
            raise UsageError(
                f"--option must be LETTER=LABEL, got {pair!r}\n"
                '  artifacts-decide ask <id> --question "..." '
                '--option a="the recommended one" --option b="the other"'
            )
        options.append({"letter": letter.strip().lower(), "label": label.strip()})
    return options


def _ask(args, key: str) -> dict:
    options = _options(args.option)
    recommended = args.recommend
    if recommended is None and options:
        # The first option IS the recommendation unless told otherwise, because
        # that is how Jeremy reads them. CLAUDE.md, his own house style:
        # "recommendation first as (a), because that is how he answers." A
        # command that made him hunt for which one we meant would be a command
        # that quietly drops the most useful half of every question.
        recommended = options[0]["letter"]
    return _request(
        "POST",
        f"{_api_base()}/artifacts/{args.artifact}/decisions",
        key,
        {
            "question": args.question,
            "options": options,
            "recommended": recommended,
        },
    )


def _fmt(row: dict) -> str:
    """One decision, as the reader needs it rather than as it is stored.

    Answered rows lead with the answer, because a caller running this is almost
    always asking "what did he say?" -- and with the NOTE in full, never
    truncated. Every note he has written so far has qualified the letter it came
    with, and "start with (b) but then (a) if we can" truncated to "start with
    (b)" is the failure this whole service is meant to prevent.
    """
    letters = {o["letter"]: o.get("label", "") for o in row.get("options") or []}
    lines = [f"{row['id']}  {row['question']}"]

    if row.get("answer"):
        chosen = row["answer"]
        source = row.get("answer_source") or "?"
        # Said plainly, because it is the difference between him and us. A tap
        # is 'page'; anything else was relayed by a seat, and the reader has to
        # be able to tell without knowing the vocabulary.
        how = "he tapped it" if source == "page" else f"relayed from {source}"
        by = row.get("recorded_by")
        lines.append(
            f"  ANSWER ({how}{f', by {by}' if by and source != 'page' else ''}): "
            f"({chosen}) {letters.get(chosen, '')}"
        )
        if row.get("answer_note"):
            lines.append(f"  HE SAID: {row['answer_note']}")
        when = row.get("said_at") or row.get("answered_at")
        if when:
            lines.append(f"  when: {when}")
        if not row.get("acknowledged_at"):
            lines.append("  NOT YET ACKNOWLEDGED -- artifacts-decide ack " + row["id"])
    else:
        for letter, label in letters.items():
            mark = " <- recommended" if letter == row.get("recommended") else ""
            lines.append(f"  ({letter}) {label}{mark}")
        # Withdrawn and still-waiting are completely different states and used
        # to print identically -- `close` answered with the word "unanswered",
        # which is true and reads as "nothing happened". Caught by running the
        # command against the live service rather than by reading it.
        lines.append(
            f"  WITHDRAWN at {row['closed_at']}, never answered"
            if row.get("closed_at")
            else "  unanswered"
        )
    return "\n".join(lines)


def _print_rows(rows: list[dict], *, empty: str) -> int:
    if not rows:
        # Not silence. An empty result and a broken call look identical when
        # both print nothing, and this repo has already lost a shift to exactly
        # that (ij8.51: a wait client reported silence over answers it had been
        # handed). Say which one it is.
        print(empty)
        return 0
    print("\n\n".join(_fmt(row) for row in rows))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = _StaleAwareParser(
        prog="artifacts-decide",
        description="ask Jeremy a lettered question on a page, and read the answer",
    )
    parser.add_argument("--key-file")
    # Which session raised the question, when several share one key. It rides on
    # the decision row, so a shared feed can be narrowed to your own -- see
    # artifacts.client._SESSION_ENV.
    parser.add_argument("--session", metavar="LABEL")
    parser.add_argument(
        "--version",
        action="version",
        version=f"artifacts-decide, installed {installed_stamp()}",
    )
    # `required`, so a bare `artifacts-decide` prints the list of verbs rather
    # than a traceback or a silent success.
    #
    # `parser_class` matters more than it looks: nearly every flag here lives on
    # a SUBcommand, and argparse builds subparsers as plain ArgumentParsers
    # unless told otherwise. Without this, `artifacts-decide ask --fetch` would
    # get argparse's bare "unrecognized arguments" -- the exact message that
    # cost a seat an hour on 21 Sep by reading as "this feature does not exist"
    # when the truth was an install six days old.
    subs = parser.add_subparsers(
        dest="cmd", required=True, metavar="COMMAND",
        parser_class=_StaleAwareParser,
    )

    ask = subs.add_parser("ask", help="pose a question on one of your pages")
    ask.add_argument("artifact", metavar="ARTIFACT-ID")
    ask.add_argument("--question", required=True)
    ask.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="LETTER=LABEL",
        help="repeatable; give them in the order he should read them",
    )
    ask.add_argument(
        "--recommend",
        metavar="LETTER",
        help="defaults to the first option -- his house style is (a) first",
    )

    answers = subs.add_parser(
        "answers", help="every decision on one page, answered ones included"
    )
    answers.add_argument("artifact", metavar="ARTIFACT-ID")
    answers.add_argument(
        "--open-only",
        action="store_true",
        help="just the ones still waiting on him",
    )

    listing = subs.add_parser(
        "list", help="your open decisions across every page you own"
    )
    listing.add_argument(
        "--answered", action="store_true", help="include ones he has answered"
    )

    record = subs.add_parser(
        "record",
        help="write down an answer he gave somewhere that is not the page",
    )
    record.add_argument("decision", metavar="DECISION-ID")
    record.add_argument("--letter", required=True)
    record.add_argument(
        "--note",
        required=True,
        help="his words, verbatim -- required, because the note usually decides it",
    )
    record.add_argument(
        "--source",
        required=True,
        choices=("terminal", "telegram", "in-person", "other"),
        help="where he said it; 'page' is refused -- that means he tapped it",
    )
    record.add_argument("--said-at", metavar="ISO8601", help="when HE said it")

    ack = subs.add_parser("ack", help="say you have seen an answer")
    ack.add_argument("decision", metavar="DECISION-ID")

    close = subs.add_parser("close", help="withdraw a question you no longer need")
    close.add_argument("decision", metavar="DECISION-ID")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    use_session(declared_session(args.session))
    try:
        key = _read_key(_key_file_path(args.key_file))
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    base = _api_base()
    try:
        if args.cmd == "ask":
            row = _ask(args, key)
            print(_fmt(row))
            return 0
        if args.cmd == "answers":
            rows = _request(
                "GET",
                f"{base}/artifacts/{args.artifact}/decisions"
                f"?include_answered={'false' if args.open_only else 'true'}",
                key,
                None,
            )
            return _print_rows(
                rows,
                empty=(
                    "no questions on that page"
                    + (" are still open" if args.open_only else " at all")
                ),
            )
        if args.cmd == "list":
            rows = _request(
                "GET",
                f"{base}/decisions?include_answered={'true' if args.answered else 'false'}",
                key,
                None,
            )
            return _print_rows(rows, empty="nothing waiting on Jeremy from this seat")
        if args.cmd == "record":
            payload = {
                "letter": args.letter.strip().lower(),
                "note": args.note,
                "source": args.source,
            }
            if args.said_at:
                payload["said_at"] = args.said_at
            row = _request(
                "POST", f"{base}/decisions/{args.decision}/record", key, payload
            )
            print(_fmt(row))
            return 0
        if args.cmd in ("ack", "close"):
            route = "acknowledge" if args.cmd == "ack" else "close"
            row = _request(
                "POST", f"{base}/decisions/{args.decision}/{route}", key, {}
            )
            print(_fmt(row))
            return 0
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Conflict as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except HTTPFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled command {args.cmd!r}")  # pragma: no cover


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
