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
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime

from artifacts import news
from artifacts.client import (
    Conflict,
    HTTPFailure,
    UsageError,
    _api_base,
    _request,
    _resolve_key,
    declared_session,
    installed_stamp,
    maybe_rotate,
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
    where.append(
        f"asked by {asked_by}" if asked_by else "asked by an unlabelled session"
    )
    age = row.get("age_seconds")
    if isinstance(age, int):
        where.append(
            f"waiting {age // 3600}h" if age >= 3600 else f"waiting {age // 60}m"
        )
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
        lines.append(
            f"  SINCE ({outcome['created_at'][:16]}, {who}): {outcome['body']}"
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


def _new_answers(rows: list[dict]) -> list[dict]:
    """Answered and not yet acknowledged: what an agent must act on."""
    return [r for r in rows if r.get("answer") and not r.get("acknowledged_at")]


def _duration(text: str) -> float:
    """'55m', '90s', '2h', or plain seconds."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", text or "")
    if not match:
        raise UsageError(f"--timeout must look like 55m, 90s or 2h, got {text!r}")
    return float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]


def _threads(rows: list[dict]) -> list[dict]:
    """Comments grouped into threads, oldest first, each marked with whether the
    agent owes a reply: the last word is his, not the agent's (homelab-ij8.18)."""
    roots: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: r.get("created_at") or ""):
        root = row.get("thread_root") or row["id"]
        thread = roots.setdefault(root, {"id": root, "messages": []})
        thread["messages"].append(row)
    out = []
    for thread in roots.values():
        first, last = thread["messages"][0], thread["messages"][-1]
        thread["artifact_id"] = first.get("artifact_id")
        thread["quote"] = first.get("quote") or ""
        thread["kind"] = first.get("kind") or "comment"
        # A bare "looks good" -- the thread's only message, with no note of
        # its own -- is an acknowledgement, not a question: nothing is being
        # asked, so nothing is owed back (final review, Item 4c). `remove`
        # still needs a reply even bare: it is asking for an action. A
        # `looks_good` that DOES say something is a live thread like any
        # other, not an ack.
        is_bare_ack = (
            len(thread["messages"]) == 1
            and thread["kind"] == "looks_good"
            and not (last.get("body") or "").strip()
        )
        thread["needs_reply"] = last.get("author_kind") == "human" and not is_bare_ack
        out.append(thread)
    return out


def _fmt_thread(thread: dict) -> str:
    head = "NEEDS YOUR REPLY" if thread["needs_reply"] else "replied"
    lines = [
        f"{thread['id']}  comment thread on page {thread['artifact_id']}  [{head}]"
    ]
    if thread["quote"]:
        label = {"remove": "REMOVE THIS", "looks_good": "LOOKS GOOD"}.get(
            thread.get("kind"), "on"
        )
        lines.append(f'  {label}: "{thread["quote"]}"')
    for m in thread["messages"]:
        who = (
            "HE SAID" if m.get("author_kind") == "human" else f"{m.get('author')} said"
        )
        # `remove`/`looks_good` can carry no note at all -- "HE SAID: " with
        # nothing after the colon reads as a rendering bug, not as the
        # acknowledgement it is (final review, Item 4b).
        raw_body = m.get("body") or ""
        lines.append(f"  {who}: {raw_body if raw_body.strip() else '(no note)'}")
    if thread["needs_reply"]:
        lines.append(
            f'  reply: artifacts-decide reply {thread["id"]} "..."'
            f"   · done: artifacts-decide settle {thread['id']}"
        )
    return "\n".join(lines)


def _open_threads(key: str, base: str, page: str | None = None) -> list[dict]:
    url = f"{base}/artifacts/{page}/comments" if page else f"{base}/comments"
    return _threads(_request("GET", url, key, None))


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

    def owed() -> list[dict]:
        # His comments waiting for this agent's reply wake it too, unless it
        # asked for answers only (homelab-ij8.18: comments that reach the agent).
        if args.answers_only:
            return []
        return [t for t in _open_threads(key, base, page) if t["needs_reply"]]

    def report(answers: list[dict], threads: list[dict]) -> int:
        if answers:
            _print_rows(answers, empty="")
        if threads:
            if answers:
                print()
            print("\n\n".join(_fmt_thread(t) for t in threads))
        return 0

    # The cursor BEFORE the look: an answer landing between the two would
    # otherwise be missed until the timeout.
    headers: dict = {}
    _request("GET", f"{base}/events?since=999999999999", key, None, headers_out=headers)
    cursor = int(headers.get("x-artifacts-cursor", "0"))
    found, waiting_threads = current(), owed()
    if found or waiting_threads:
        return report(found, waiting_threads)
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
        kinds = {"decision_answered"}
        if not args.answers_only:
            kinds.add("comment_added")
        if any(
            r.get("kind") in kinds and (not page or r.get("artifact_id") == page)
            for r in rows
        ):
            found, waiting_threads = current(), owed()
            if found or waiting_threads:
                return report(found, waiting_threads)
    print("nothing yet: no answer before the timeout. Run it again.", file=sys.stderr)
    return 3


# Exit code per verdict for `wait --review`; 7 (new marks) and 3 (timeout) are
# handled inline.
_VERDICT_EXIT = {
    "approved": 0,
    "approved_with_notes": 4,
    "changes_requested": 5,
    "dismissed": 6,
}


def _refuse(text: str, code: int = 1) -> int:
    print(f"REFUSED: {text}", file=sys.stderr)
    return code


def _write_result_file(target: str, text: str) -> bool:
    """Same bytes as stdout, atomically, never over an existing file: a temp file
    beside the target (mode 0600), hard-linked into place. `os.link` fails if the
    target exists, which `os.rename` would not, so a racing writer is refused
    rather than clobbered. False when the target appeared first."""
    directory = os.path.dirname(os.path.abspath(target))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".verdict-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        try:
            os.link(tmp, target)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(tmp)


def _after(stamp: str | None, since: str | None) -> bool:
    if not stamp or not since:
        return bool(stamp)
    return datetime.fromisoformat(stamp) > datetime.fromisoformat(since)


def _wait_review(args, key: str, base: str) -> int:
    """Block until Jeremy gives a verdict on a review; exit by verdict.

    0 approved, 4 approved with notes, 5 changes requested, 6 dismissed, 7 new
    marks and no verdict yet, 3 timeout, 1 refused, 2 usage. `--require-approval`
    folds 0/4 to 0 and 5/6/7 to 1.

    STARTS BY LOOKING, then long-polls the events feed exactly as `_wait` does,
    so a verdict given earlier is printed at once and a later one wakes it."""
    review_id = args.review
    target = args.result_file
    if target and os.path.lexists(target):
        return _refuse(f"{target} exists; not overwriting it", 2)
    if target and not os.path.isdir(os.path.dirname(os.path.abspath(target))):
        return _refuse(f"the directory for {target} does not exist", 2)
    timeout = _duration(args.timeout)
    deadline = time.monotonic() + timeout

    def look() -> int | None:
        review = _request("GET", f"{base}/reviews/{review_id}", key, None)
        verdict = review.get("verdict")
        if verdict:
            out = (
                json.dumps(review["data"]) + "\n"
                if args.json
                else (review.get("message") or "")
            )
            sys.stdout.write(out)
            sys.stdout.flush()
            if target and not _write_result_file(target, out):
                return _refuse(f"{target} exists; not overwriting it", 2)
            code = _VERDICT_EXIT[verdict]
            if args.require_approval:
                code = 0 if code in (0, 4) else 1
            return code
        if args.verdict_only:
            return None
        # The baseline rides on the review itself, so it is never missing, even
        # for a review that has since been closed.
        since = review["created_at"]
        # A mark is new while it is an open human thread asked after the review
        # whose last word is still his -- `_threads`' own "needs your reply" rule
        # -- so replying clears it, and it still travels with the verdict.
        threads = _threads(
            _request(
                "GET", f"{base}/artifacts/{review['artifact_id']}/comments", key, None
            )
        )
        fresh = [
            t
            for t in threads
            if t["needs_reply"]
            and t["messages"][0].get("author_kind") == "human"
            and _after(t["messages"][0].get("created_at"), since)
        ]
        if not fresh:
            return None
        print("\n\n".join(_fmt_thread(t) for t in fresh))
        return 1 if args.require_approval else 7

    # THE CURSOR FIRST, then the look: a verdict landing between the two would
    # otherwise be missed until the timeout. Taken this way round, it is either
    # seen by the look or is an event after the cursor.
    headers: dict = {}
    _request("GET", f"{base}/events?since=999999999999", key, None, headers_out=headers)
    cursor = int(headers.get("x-artifacts-cursor", "0"))
    done = look()
    if done is not None:
        return done
    wait_for = f"{int(timeout)}s" if timeout < 60 else f"{int(timeout // 60)}m"
    print(
        f"waiting for Jeremy's verdict on {review_id} (up to {wait_for})...",
        file=sys.stderr,
        flush=True,
    )
    kinds = {"decision_answered"} | (set() if args.verdict_only else {"comment_added"})
    relook = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining < 1:
            break
        try:
            if not relook:
                hold = int(min(_HOLD, remaining))
                rows = _request(
                    "GET",
                    f"{base}/events?since={cursor}&wait={hold}&exclude_self=true",
                    key,
                    None,
                    timeout=hold + 30,
                )
                if not isinstance(rows, list):
                    rows = []
                for row in rows:
                    cursor = max(cursor, int(row.get("seq", cursor)))
                relook = any(r.get("kind") in kinds for r in rows)
            if relook:
                done = look()
                relook = False
                if done is not None:
                    return done
        except HTTPFailure as exc:
            if exc.status in (401, 403, 404):
                raise
            # A deploy or a dropped connection: ask again, and look again if the
            # failure was in the look, since its event is already consumed.
            time.sleep(min(5, max(0, deadline - time.monotonic())))
    print("nothing yet: no verdict before the timeout. Run it again.", file=sys.stderr)
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
        dest="cmd",
        required=True,
        metavar="COMMAND",
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

    ask_review = subs.add_parser(
        "ask-review",
        help="ask Jeremy to review one of your pages (a verdict, not a question)",
    )
    ask_review.add_argument("page", metavar="PAGE-ID")

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
        "--answered",
        action="store_true",
        help="also answers you have already acknowledged",
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
    waiting.add_argument(
        "--page", metavar="ARTIFACT-ID", help="only answers on this page"
    )
    waiting.add_argument(
        "--timeout", default="55m", help="how long to wait (default 55m)"
    )
    waiting.add_argument(
        "--answers-only", action="store_true", help="do not wake for his comments"
    )
    waiting.add_argument(
        "--review", metavar="REVIEW-ID", help="wait for the verdict on this review"
    )
    waiting.add_argument(
        "--json", action="store_true", help="with --review: print the data as one line"
    )
    waiting.add_argument(
        "--result-file",
        metavar="PATH",
        help="with --review: also write it here, never over a file",
    )
    waiting.add_argument(
        "--require-approval",
        action="store_true",
        help="with --review: exit 0 for approved (with or without notes), else 1",
    )
    waiting.add_argument(
        "--verdict-only",
        action="store_true",
        help="with --review: ignore new marks, wake only for the verdict",
    )

    threads = subs.add_parser(
        "comments",
        help="comment threads on your pages; NEEDS YOUR REPLY marks his last word",
    )
    threads.add_argument("--page", metavar="ARTIFACT-ID", help="only this page")
    reply = subs.add_parser(
        "reply", help="answer a comment thread on one of your pages"
    )
    reply.add_argument("thread", metavar="THREAD-ID")
    reply.add_argument("text", metavar="TEXT")
    settle = subs.add_parser("settle", help="mark a comment thread done")
    settle.add_argument("thread", metavar="THREAD-ID")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    use_session(declared_session(args.session))
    try:
        key = _resolve_key(args)
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    key = maybe_rotate(args, key)

    base = _api_base()
    if args.cmd == "wait" and not args.review:
        used = [
            flag
            for flag, on in (
                ("--json", args.json),
                ("--result-file", args.result_file),
                ("--require-approval", args.require_approval),
                ("--verdict-only", args.verdict_only),
            )
            if on
        ]
        if used:
            print(f"{', '.join(used)} only work with --review ID", file=sys.stderr)
            return 2

    if args.cmd in (
        "ask",
        "ask-review",
        "record",
        "outcome",
        "ack",
        "close",
        "reply",
        "settle",
    ):
        warn_if_unlabelled(args.cmd)
    try:
        if args.cmd == "ask":
            row = _ask(args, key)
            print(_fmt(row))
            return 0
        if args.cmd == "ask-review":
            row = _request("POST", f"{base}/artifacts/{args.page}/reviews", key, {})
            print(f"{row['id']}  review requested on {args.page}")
            print(f"  wait: artifacts-decide wait --review {row['id']}")
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
                    {
                        **r["decision"],
                        "agent": r.get("agent"),
                        "artifact_title": r.get("artifact_title"),
                        "collect_state": r.get("state"),
                        "last_read_at": r.get("last_read_at"),
                    }
                    for r in rows
                ]
            elif args.open_only:
                rows = _request(
                    "GET",
                    f"{base}/decisions?include_answered=false&scope={scope}",
                    key,
                    None,
                )
            else:
                # NEW ANSWERS FIRST (homelab-ij8.106): the first outside agent
                # ran `list`, saw only open questions, and missed three of his
                # answers and their notes. Answered-and-unacknowledged is what an
                # agent must act on, so it leads; acknowledged answers only with
                # --answered.
                rows = _request(
                    "GET",
                    f"{base}/decisions?include_answered=true&scope={scope}",
                    key,
                    None,
                )
                fresh = _new_answers(rows)
                still_open = [r for r in rows if not r.get("answer")]
                done = [r for r in rows if r.get("answer") and r.get("acknowledged_at")]
                rows = fresh + still_open + (done if args.answered else [])
            if args.stranded:
                empty = "no uncollected answers" + (
                    "" if args.all else " on this seat's pages"
                )
            elif args.open_only:
                empty = "nothing waiting on Jeremy" + (
                    "" if args.all else " from this seat"
                )
            else:
                empty = "no new answers, and nothing waiting on Jeremy" + (
                    "" if args.all else " from this seat"
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
            if args.review:
                try:
                    return _wait_review(args, key, base)
                except HTTPFailure as exc:
                    if exc.status in (401, 403, 404):
                        return _refuse(str(exc))
                    raise
            return _wait(args, key, base)
        if args.cmd == "comments":
            found = _open_threads(key, base, args.page)
            if not found:
                print(
                    "no open comment threads" + (" on that page" if args.page else "")
                )
                return 0
            found.sort(key=lambda t: not t["needs_reply"])
            print("\n\n".join(_fmt_thread(t) for t in found))
            return 0
        if args.cmd in ("reply", "settle"):
            mine = {t["id"]: t for t in _open_threads(key, base)}
            thread = mine.get(args.thread)
            if thread is None:
                raise UsageError(
                    f"no open comment thread {args.thread} on your pages "
                    "(artifacts-decide comments lists them)"
                )
            if args.cmd == "settle":
                _request("POST", f"{base}/comments/{args.thread}/resolve", key, {})
                print(f"settled {args.thread}")
                return 0
            _request(
                "POST",
                f"{base}/artifacts/{thread['artifact_id']}/comments",
                key,
                {"body": args.text, "thread_root": args.thread},
            )
            print(f"replied on {args.thread}; he sees it in the page's comments")
            return 0
        if args.cmd in ("ack", "close"):
            route = "acknowledge" if args.cmd == "ack" else "close"
            row = _request("POST", f"{base}/decisions/{args.decision}/{route}", key, {})
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
    news.announce()  # the console script only; see news.py
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
