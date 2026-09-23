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
import re
import sys
import time

from artifacts.client import (
    Conflict,
    HTTPFailure,
    UsageError,
    _api_base,
    _request,
    _resolve_key,
    declared_session,
    installed_stamp,
    use_session,
    warn_if_unlabelled,
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

    # WHOSE PAGE, AND WHICH SESSION ASKED. A seat name is not a session: one key
    # is shared by every session on its machine, so "the cove seat's questions"
    # and "that session's questions" are different sets, and on 22 Sep 2026 that
    # produced a wrong attribution and four answers nobody collected -- one of
    # them an explanation Jeremy asked for on 20 September and never received,
    # because the reader could not tell a departed asker from a stuck one.
    # Said plainly here, including when it is unknown, because unknown is the
    # case that needs a human to decide what to do.
    where = []
    if row.get("artifact_title") or row.get("agent"):
        title = row.get("artifact_title") or row.get("artifact_id") or "?"
        seat = row.get("agent")
        where.append(f'on "{title}"' + (f" [{seat}]" if seat else ""))
    asked_by = row.get("session")
    where.append(f"asked by {asked_by}" if asked_by else "asked by an unlabelled session")
    age = row.get("age_seconds")
    if isinstance(age, int):
        where.append(f"waiting {age // 3600}h" if age >= 3600 else f"waiting {age // 60}m")
    lines.append("  " + " · ".join(where))

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
            # Two states, opposite responses: an asker that never came back is
            # not the same as one that is reading its feed and still not acting.
            state = row.get("collect_state")
            if state == "no_reader":
                lines.append(
                    "  NOBODY HAS READ THIS SEAT'S FEED SINCE HE ANSWERED -- the asker "
                    "is not running; somebody else has to take this one"
                )
            elif state == "stuck":
                seen = (row.get("last_read_at") or "")[:16]
                lines.append(
                    f"  the asker HAS read its feed since he answered ({seen}) and has "
                    "still not taken it -- something is alive and not acting"
                )
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
    # WHAT HAPPENED SINCE, under answered and unanswered alike. An outcome on an
    # OPEN question is the one with teeth: if the framing was corrected after it
    # was posed, Jeremy could answer the old question by accident.
    for outcome in row.get("outcomes") or []:
        who = outcome.get("session") or outcome.get("agent")
        lines.append(f"  SINCE ({outcome['created_at'][:16]}, {who}): {outcome['body']}")
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


def _new_answers(rows: list[dict]) -> list[dict]:
    """Answered and not yet acknowledged: what an agent must act on."""
    return [r for r in rows if r.get("answer") and not r.get("acknowledged_at")]


def _duration(text: str) -> float:
    """'55m', '90s', '2h', or plain seconds."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", text or "")
    if not match:
        raise UsageError(f"--timeout must look like 55m, 90s or 2h, got {text!r}")
    return float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]


# How long each held request asks for. The service never holds a request that
# came through Cloudflare past 50 s (api.PUBLIC_MAX_WAIT); asking for exactly
# that keeps one code path inside the house and out.
_HOLD = 50


def _wait(args, key: str, base: str) -> int:
    """Block until Jeremy answers, print the answer(s), exit 0.

    homelab-ij8.106, from the first outside agent: it asked nine questions,
    ended its turn, and three answers sat unseen until he asked whether it had
    read them -- and every agent was left to hand-write its own listener.

    Exit codes a caller can branch on: 0 answers printed; 3 nothing by the
    timeout (ask again); 1 refused (the key); 2 usage. Built to run as a
    background task that wakes its agent when it exits.

    STARTS BY LOOKING, not by listening: an answer that landed before this ran
    is printed at once, so starting the waiter late never loses one."""
    timeout = _duration(args.timeout)
    deadline = time.monotonic() + timeout
    page = args.page

    def current() -> list[dict]:
        url = (
            f"{base}/artifacts/{page}/decisions?include_answered=true"
            if page
            else f"{base}/decisions?include_answered=true&scope=mine"
        )
        return _new_answers(_request("GET", url, key, None))

    found = current()
    if found:
        return _print_rows(found, empty="")
    headers: dict = {}
    _request("GET", f"{base}/events?since=999999999999", key, None, headers_out=headers)
    cursor = int(headers.get("x-artifacts-cursor", "0"))
    print(
        f"waiting for Jeremy's answer{f' on {page}' if page else ''} "
        f"(up to {int(timeout // 60)}m)...",
        file=sys.stderr,
        flush=True,
    )
    while time.monotonic() < deadline:
        hold = int(max(1, min(_HOLD, deadline - time.monotonic())))
        try:
            rows = _request(
                "GET",
                f"{base}/events?since={cursor}&wait={hold}&exclude_self=true",
                key,
                None,
                timeout=hold + 30,
            )
        except HTTPFailure as exc:
            if exc.status in (401, 403):
                raise
            # A deploy, a dropped connection, Cloudflare blinking: pause and ask
            # again from the same cursor, which loses nothing.
            time.sleep(5)
            continue
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            cursor = max(cursor, int(row.get("seq", cursor)))
        if any(
            r.get("kind") == "decision_answered" and (not page or r.get("artifact_id") == page)
            for r in rows
        ):
            found = current()
            if found:
                return _print_rows(found, empty="")
    print("nothing yet: no answer before the timeout. Run it again.", file=sys.stderr)
    return 3


def build_parser() -> argparse.ArgumentParser:
    parser = _StaleAwareParser(
        prog="artifacts-decide",
        description="ask Jeremy a lettered question on a page, and read the answer",
    )
    parser.add_argument("--key-file")
    # Same as artifacts-publish: a key from an open descriptor never touches
    # disk. See client._read_key_fd.
    parser.add_argument("--key-fd", type=int, metavar="N")
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
        "list",
        help="NEW ANSWERS first (answered, not yet acknowledged), then open questions",
    )
    listing.add_argument(
        "--answered", action="store_true", help="also answers you have already acknowledged"
    )
    listing.add_argument(
        "--open-only", action="store_true", help="only questions still waiting on him"
    )
    listing.add_argument(
        "--all",
        action="store_true",
        help="every seat's decisions, not just this key's (needs decisions:read-all)",
    )
    listing.add_argument(
        "--stranded",
        action="store_true",
        help="only answers nobody has collected yet -- who owes Jeremy a reply",
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

    outcome = subs.add_parser(
        "outcome",
        help="append what this decision caused, or what changed under it",
    )
    outcome.add_argument("decision", metavar="DECISION-ID")
    outcome.add_argument(
        "note",
        metavar="TEXT",
        help=(
            "what happened -- a PR, a closed bead, a correction to your own "
            "framing. Allowed while the question is still open, and that is "
            "the case that matters most: if the framing moved after he read "
            "it, he can answer the old question by accident."
        ),
    )

    ack = subs.add_parser("ack", help="say you have seen an answer")
    ack.add_argument("decision", metavar="DECISION-ID")

    close = subs.add_parser("close", help="withdraw a question you no longer need")
    close.add_argument("decision", metavar="DECISION-ID")

    waiting = subs.add_parser(
        "wait",
        help="block until he answers, print the answer, exit 0 (3 if nothing by --timeout)",
    )
    waiting.add_argument("--page", metavar="ARTIFACT-ID", help="only answers on this page")
    waiting.add_argument("--timeout", default="55m", help="how long to wait (default 55m)")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    use_session(declared_session(args.session))
    try:
        key = _resolve_key(args)
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    base = _api_base()
    if args.cmd in ("ask", "record", "outcome", "ack", "close"):
        warn_if_unlabelled(args.cmd)
    try:
        if args.cmd == "ask":
            row = _ask(args, key)
            print(_fmt(row))
            return 0
        if args.cmd == "answers":
            # WITHDRAWN ONES INCLUDED. A question withdrawn because its page
            # was archived is part of that page's record, and since 21 Sep the
            # service withdraws them rather than the agent -- so hiding them
            # here would mean a seat could be told "your question is gone" by
            # an event and then find no trace of it anywhere (homelab-ij8.44).
            # --open-only is the one case that excludes them, because a
            # withdrawn question is not waiting on anybody.
            keep = "false" if args.open_only else "true"
            rows = _request(
                "GET",
                f"{base}/artifacts/{args.artifact}/decisions"
                f"?include_answered={keep}&include_closed={keep}",
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
            # --stranded is "he answered and nobody picked it up", and it asks
            # the route that has answered that since 19 Sep: /decisions/
            # uncollected. It carries what a second spelling of the filter
            # could not -- `state`, which separates an asker that is GONE from
            # one that is STUCK, and those need opposite responses.
            #
            # 22 Sep 2026: I built that second spelling before finding this
            # route, and it returned strictly less. The comment on the route
            # itself had already said why that is a bad idea -- "two spellings
            # of the same privilege is how one of them ends up laxer than the
            # other" -- which is a reason to read the routes you are about to
            # add to.
            scope = "all" if args.all else "mine"
            if args.stranded:
                rows = _request(
                    "GET", f"{base}/decisions/uncollected?scope={scope}", key, None
                )
                rows = [
                    {**r["decision"], "agent": r.get("agent"),
                     "artifact_title": r.get("artifact_title"),
                     "collect_state": r.get("state"),
                     "last_read_at": r.get("last_read_at")}
                    for r in rows
                ]
            elif args.open_only:
                rows = _request(
                    "GET", f"{base}/decisions?include_answered=false&scope={scope}", key, None
                )
            else:
                # NEW ANSWERS FIRST (homelab-ij8.106): the first outside agent
                # ran `list`, saw only open questions, and missed three of his
                # answers and their notes. Answered-and-unacknowledged is what an
                # agent must act on, so it leads; acknowledged answers only with
                # --answered.
                rows = _request(
                    "GET", f"{base}/decisions?include_answered=true&scope={scope}", key, None
                )
                fresh = _new_answers(rows)
                still_open = [r for r in rows if not r.get("answer")]
                done = [r for r in rows if r.get("answer") and r.get("acknowledged_at")]
                rows = fresh + still_open + (done if args.answered else [])
            if args.stranded:
                empty = (
                    "no uncollected answers"
                    + ("" if args.all else " on this seat's pages")
                )
            elif args.open_only:
                empty = "nothing waiting on Jeremy" + ("" if args.all else " from this seat")
            else:
                empty = (
                    "no new answers, and nothing waiting on Jeremy"
                    + ("" if args.all else " from this seat")
                )
            return _print_rows(rows, empty=empty)
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
        if args.cmd == "outcome":
            written = _request(
                "POST",
                f"{base}/decisions/{args.decision}/outcome",
                key,
                {"body": args.note},
            )
            print(f"recorded on {written['decision_id']}: {written['body']}")
            return 0
        if args.cmd == "wait":
            return _wait(args, key, base)
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
