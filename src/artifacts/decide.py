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
import uuid
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

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
    """'55m', '90s', '2h', or plain seconds; 'forever' is 0 (no limit)."""
    if (text or "").strip().lower() == "forever":
        return 0.0
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", text or "")
    if not match:
        raise UsageError(f"--timeout must look like 55m, 90s or 2h, got {text!r}")
    return float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]


def _office_of(label: str | None) -> str:
    return (label or "").split("/", 1)[0].strip()


def _threads(
    rows: list[dict], me: str | None = None, office: str | None = None
) -> list[dict]:
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
        # Another office's comment on this agent's page is owed an answer like
        # his; `me` is the key's own agent, so its own replies are not.
        # On a key several offices share the agent name is the same, so a
        # session label naming a different office counts as another's too.
        theirs = _office_of(last.get("session"))
        from_other_agent = (
            me is not None
            and last.get("author_kind") == "agent"
            and (
                last.get("author") != me or bool(office and theirs and theirs != office)
            )
        )
        thread["needs_reply"] = (
            last.get("author_kind") == "human" or from_other_agent
        ) and not is_bare_ack
        out.append(thread)
    return out


def _fmt_thread(thread: dict, *, on_page: bool = False) -> str:
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
            "HE SAID"
            if m.get("author_kind") == "human"
            else f"{m.get('author')} (agent"
            + (f", via session {m['session']}" if m.get("session") else "")
            + ") said"
        )
        # `remove`/`looks_good` can carry no note at all -- "HE SAID: " with
        # nothing after the colon reads as a rendering bug, not as the
        # acknowledgement it is (final review, Item 4b).
        raw_body = m.get("body") or ""
        lines.append(f"  {who}: {raw_body if raw_body.strip() else '(no note)'}")
    if thread["needs_reply"]:
        lines.append(
            f'  reply: artifacts-decide reply {thread["id"]} "..."'
            + (f" --page {thread['artifact_id']}" if on_page else "")
            + f"   · done: artifacts-decide settle {thread['id']}"
        )
    return "\n".join(lines)


def _open_threads(
    key: str, base: str, page: str | None = None, office: str | None = None
) -> list[dict]:
    url = f"{base}/artifacts/{page}/comments" if page else f"{base}/comments"
    rows = _request("GET", url, key, None)
    me = None
    if any(r.get("author_kind") == "agent" for r in rows):
        me = (_request("GET", f"{_api_base()}/me", key, None) or {}).get("agent")
    return _threads(rows, me, office)


# How long each held request asks for. The service never holds a request that
# came through Cloudflare past 50 s (api.PUBLIC_MAX_WAIT); asking for exactly
# that keeps one code path inside the house and out.
_HOLD = 50

# How long a waiter pauses after a failed request (a deploy, a dropped
# connection, Cloudflare blinking) before it asks again from the same cursor.
_RETRY_PAUSE = 5


# How often an answer of this office's own that is still unacknowledged is
# shown again (feedback, 6 Oct 2026). Every maintainer wanted it never lost
# and none wanted it re-waking on every re-arm: one missed ack used to turn the
# waiter into a busy loop. `list` keeps showing it until it is acknowledged.
_RESURFACE_EVERY = 3600.0


def _office(args) -> str | None:
    """Whose answers this waiter hears: `--office`, else the part of the
    session label before the first "/" ("cove-maintainer/ab12" is the
    cove-maintainer office). None means every office, as before 6 Oct 2026."""
    if args.all_offices:
        return None
    if args.office is not None:
        return args.office.strip() or None
    session = declared_session(args.session)
    if session is None:
        print(
            "no office: hearing every office's answers under this key. Set "
            "ARTIFACTS_SESSION=<office>/<ref> or pass --office NAME to hear only "
            "your own (--all-offices says this on purpose).",
            file=sys.stderr,
        )
        return None
    return session.split("/", 1)[0].strip() or None


def _belongs(session: str | None, office: str | None, key_agent: str | None) -> bool:
    """Whether something asked (or published) by `session` is this office's.

    A question with NO label -- asked before labels existed, or by a session
    that forgot -- goes to the key's own office: the one named like the key's
    agent. Every other office sharing the key leaves it alone, so an
    unlabelled question is heard once, by its owner, not by everyone."""
    if office is None:
        return True
    if not session:
        return office == key_agent
    return session == office or session.startswith(office + "/")


class _Surfaced:
    """When each of this office's items was last shown, kept on disk so a
    re-armed waiter does not show the same unacknowledged answer again within
    the hour. One small JSON file per office under
    ${XDG_STATE_HOME:-~/.local/state}/artifacts/. A file that cannot be read
    or written costs only the rate limit, never an answer."""

    def __init__(self, office: str) -> None:
        root = os.environ.get("XDG_STATE_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "state"
        )
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", office)
        self.path = os.path.join(root, "artifacts", f"wait-{safe}.json")
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.seen = {
                str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))
            }
        except (OSError, ValueError, AttributeError):
            self.seen = {}

    def due_at(self, mark: str) -> float:
        return self.seen.get(mark, float("-inf")) + _RESURFACE_EVERY

    def record(self, marks: list[str], now: float) -> None:
        for mark in marks:
            self.seen[mark] = now
        # Forget what nobody will ask about again, so the file stays small.
        self.seen = {k: v for k, v in self.seen.items() if now - v < 30 * 86400}
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.seen, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"could not record what was shown ({exc})", file=sys.stderr)


def _answer_mark(row: dict) -> str:
    # Per answer, not per question: a question answered again is news.
    return f"d:{row['id']}@{row.get('answered_at') or ''}"


def _thread_mark(thread: dict) -> str:
    # Per last message: his new reply in an old thread is news.
    return f"t:{thread['id']}@{thread['messages'][-1].get('id', '')}"


def _wait(args, key: str, base: str) -> int:
    """Block until Jeremy answers, print the answer(s), exit 0.

    homelab-ij8.106, from the first outside agent: it asked nine questions,
    ended its turn, and three answers sat unseen until he asked whether it had
    read them -- and every agent was left to hand-write its own listener.

    Exit codes a caller can branch on: 0 answers printed; 3 nothing by the
    timeout (ask again); 1 refused (the key); 2 usage. Built to run as a
    background task that wakes its agent when it exits.

    STARTS BY LOOKING, not by listening: an answer that landed before this ran
    is printed at once, so starting the waiter late never loses one.

    HEARS ONLY ITS OWN OFFICE (feedback, 6 Oct 2026). A key is shared by every
    office on a machine, and a waiter that woke for any unacknowledged answer
    under it looped on another office's answer for as long as that went
    unacknowledged. Now every look -- the first one, each wake, each re-look
    -- keeps only what this office asked, and an answer of its own already
    shown comes back at most hourly."""
    timeout = _duration(args.timeout)
    endless = timeout == 0
    deadline = float("inf") if endless else time.monotonic() + timeout
    page = args.page
    office = _office(args)
    key_agent = None
    surfaced = None
    if office is not None:
        key_agent = (_request("GET", f"{base}/me", key, None) or {}).get("agent")
        surfaced = _Surfaced(office)
    page_sessions: dict[str, str | None] | None = None

    def thread_session(thread: dict) -> str | None:
        # A comment is about the page it is on, so it is the office that
        # published the page whose waiter hears it.
        nonlocal page_sessions
        if page_sessions is None:
            listed = _request("GET", f"{base}/artifacts", key, None) or []
            page_sessions = {p["id"]: p.get("last_session") for p in listed}
        return page_sessions.get(thread.get("artifact_id"))

    def current() -> list[dict]:
        url = (
            f"{base}/artifacts/{page}/decisions?include_answered=true"
            if page
            else f"{base}/decisions?include_answered=true&scope=mine"
        )
        rows = _new_answers(_request("GET", url, key, None))
        return [r for r in rows if _belongs(r.get("session"), office, key_agent)]

    def owed() -> list[dict]:
        # His comments waiting for this agent's reply wake it too, unless it
        # asked for answers only (homelab-ij8.18: comments that reach the agent).
        if args.answers_only:
            return []
        return [
            t
            for t in _open_threads(key, base, page, office)
            if t["needs_reply"] and _belongs(thread_session(t), office, key_agent)
        ]

    next_due = [float("inf")]

    def look() -> tuple[list[dict], list[dict]]:
        """This office's news, less anything already shown within the hour;
        notes when the earliest held-back item falls due."""
        answers, threads = current(), owed()
        if surfaced is None:
            return answers, threads
        now = time.time()
        due = [float("inf")]

        def fresh(item: dict, mark: str) -> bool:
            at = surfaced.due_at(mark)
            if at <= now:
                return True
            due.append(at)
            return False

        answers = [a for a in answers if fresh(a, _answer_mark(a))]
        threads = [t for t in threads if fresh(t, _thread_mark(t))]
        next_due[0] = time.monotonic() + (min(due) - now)
        return answers, threads

    def report(answers: list[dict], threads: list[dict]) -> int:
        if surfaced is not None:
            surfaced.record(
                [_answer_mark(a) for a in answers] + [_thread_mark(t) for t in threads],
                time.time(),
            )
        if args.json:
            # One line per wake, so a unit can append it to a mailbox.
            print(
                json.dumps(
                    {
                        "office": office,
                        "page": page,
                        "answers": answers,
                        "threads": threads,
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
            return 0
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
    found, waiting_threads = look()
    if found or waiting_threads:
        return report(found, waiting_threads)
    whose = f" for {office}" if office else ""
    limit = "with no limit" if endless else f"up to {int(timeout // 60)}m"
    print(
        f"waiting for Jeremy's answer{whose}{f' on {page}' if page else ''} "
        f"({limit})...",
        file=sys.stderr,
        flush=True,
    )
    kinds = {"decision_answered"}
    if not args.answers_only:
        kinds.add("comment_added")
    while time.monotonic() < deadline:
        # Never hold past the moment a held-back answer of ours falls due, so
        # an endless waiter still shows it again on the hour.
        until = min(deadline, next_due[0]) - time.monotonic()
        hold = int(max(1, min(_HOLD, until)))
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
            time.sleep(_RETRY_PAUSE)
            continue
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            cursor = max(cursor, int(row.get("seq", cursor)))
        woke = any(
            r.get("kind") in kinds
            and (not page or r.get("artifact_id") == page)
            and _belongs(r.get("session"), office, key_agent)
            for r in rows
        )
        if woke or time.monotonic() >= next_due[0]:
            found, waiting_threads = look()
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


# THE LEASE (page review step 5). `wait --review` claims the round for its
# session so a second session waiting on the same review is told who has it
# instead of acting on the verdict twice. The service's lease is five minutes;
# this renews it when less than `_RENEW_BELOW` seconds are left, checked every
# loop, and never holds a request past the renewal point.
_RENEW_BELOW = 120

# Another session holds the round.
_CLAIMED_EXIT = 8

_NO_LABEL = "no session label: not claiming the round (set ARTIFACTS_SESSION)"

# The service's lease on an open round, for a server that does not say.
_LEASE_SECONDS = 300

# The two clocks a wait keeps. Monotonic time stops while the machine is
# suspended on Linux and wall time does not, so when they part by more than
# `_SUSPEND_GAP` the machine slept: the wait renews before its next poll and
# measures its deadline in wall time. Module names, so a test can move them.
_mono = time.monotonic
_wall = time.time
_SUSPEND_GAP = 60


# A retry inside one run always reuses its key; a fresh one per run by default.
_IDEMPOTENCY_HELP = (
    "reuse KEY (1-128 printable ASCII) so re-running this command after a"
    " timeout cannot post twice; default a fresh one each run"
)


def _claim_line(body: dict) -> str:
    """`CLAIMED by <session> until <HH:MM UTC>: ...`, from a 409's body."""
    who = body.get("claimed_by") or "another session"
    until = "?"
    try:
        until = (
            datetime.fromisoformat(body["claim_expires_at"])
            .astimezone(UTC)
            .strftime("%H:%M UTC")
        )
    except (KeyError, TypeError, ValueError):
        pass
    return f"CLAIMED by {who} until {until}: another session is acting on this round"


class _Lease:
    """This session's claim on a review round, renewed while the wait runs."""

    def __init__(self, base: str, key: str, review_id: str, session: str) -> None:
        self.url = f"{base}/reviews/{review_id}/claim"
        self.key = key
        self.session = session
        # Monotonic time at which to renew; 0 means now.
        self.renew_at = 0.0

    def due(self) -> bool:
        return _mono() >= self.renew_at

    def seconds_to_renewal(self) -> float:
        return self.renew_at - _mono()

    def claim(self) -> dict | None:
        """Claim or renew. The 409's body (who holds it, until when, which
        round) when another session holds it, else None. A 401/403/404 raises; any other failure leaves it due, so the
        next loop tries again."""
        headers: dict = {}
        asked = _mono()
        try:
            got = _request(
                "POST",
                self.url,
                self.key,
                {"session": self.session},
                headers_out=headers,
            )
        except HTTPFailure as exc:
            if exc.status == 409 and isinstance(exc.body, dict):
                return exc.body
            if exc.status in (401, 403, 404):
                raise
            self.renew_at = 0.0
            return None
        # The lease left, measured on the SERVER's clock (its Date header), so
        # a skewed clock here cannot renew late. Without one, the lease the
        # server named (or the known one) from the moment the claim was sent,
        # on this process's monotonic clock: never local wall time against
        # the server's.
        try:
            server_now = parsedate_to_datetime(headers["date"])
            left = (
                datetime.fromisoformat(got["claim_expires_at"]) - server_now
            ).total_seconds()
        except (KeyError, TypeError, ValueError):
            seconds = got.get("lease_seconds") if isinstance(got, dict) else None
            left = float(seconds) if seconds else float(_LEASE_SECONDS)
        self.renew_at = asked + max(0.0, left - _RENEW_BELOW)
        return None


def _wait_review(args, key: str, base: str) -> int:
    """Block until Jeremy gives a verdict on a review; exit by verdict.

    0 approved, 4 approved with notes, 5 changes requested, 6 dismissed, 7 new
    marks and no verdict yet, 8 another session holds the round, 3 timeout, 1
    refused, 2 usage. `--require-approval` folds 0/4 to 0 and 5/6/7/8 to 1.

    STARTS BY LOOKING, then long-polls the events feed exactly as `_wait` does,
    so a verdict given earlier is printed at once and a later one wakes it."""
    review_id = args.review
    target = args.result_file
    if target and os.path.lexists(target):
        return _refuse(f"{target} exists; not overwriting it", 2)
    if target and not os.path.isdir(os.path.dirname(os.path.abspath(target))):
        return _refuse(f"the directory for {target} does not exist", 2)
    timeout = _duration(args.timeout)
    # 0 / forever means no limit here too, as for a plain wait.
    deadline = float("inf") if timeout == 0 else _mono() + timeout
    wall_deadline = float("inf") if timeout == 0 else _wall() + timeout
    # Wall minus monotonic; it jumps when the machine was suspended.
    drift = _wall() - _mono()

    def claimed(body: dict) -> int:
        print(_claim_line(body), file=sys.stderr, flush=True)
        if args.json:
            # One line on stdout, like every other --json exit: who has it.
            print(
                json.dumps(
                    {
                        "claimed_by": body.get("claimed_by"),
                        "claim_expires_at": body.get("claim_expires_at"),
                        "round": body.get("round"),
                    }
                ),
                flush=True,
            )
        return 1 if args.require_approval else _CLAIMED_EXIT

    # CLAIM FIRST: the session that holds the round is the one that acts on its
    # verdict, including one given before this started.
    session = declared_session(args.session)
    lease = None
    if session is None:
        print(_NO_LABEL, file=sys.stderr, flush=True)
    else:
        lease = _Lease(base, key, review_id, session)
        try:
            held = lease.claim()
        except HTTPFailure as exc:
            if exc.status != 404:
                raise
            # Not yours or missing, OR a service older than claims. The review
            # itself says which: its own 404 ends the wait as a refusal.
            _request("GET", f"{base}/reviews/{review_id}", key, None)
            print(
                "the service does not take claims yet: not claiming the round",
                file=sys.stderr,
                flush=True,
            )
            lease = None
            held = None
        if held:
            return claimed(held)

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
        # From round 2 on, the round's own start: round-1 marks were answered
        # by the republish and are not new to this round.
        opened = review.get("round_opened_at")
        if opened and _after(opened, since):
            since = opened
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
            and t["messages"][-1].get("author_kind") == "human"
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
        now_drift = _wall() - _mono()
        if abs(now_drift - drift) > _SUSPEND_GAP:
            # The machine slept. The lease may have lapsed and the deadline is
            # counted in time a person lived through: renew (which re-checks
            # the claim) before the next poll, and re-measure the deadline.
            drift = now_drift
            deadline = _mono() + (wall_deadline - _wall())
            if lease is not None:
                lease.renew_at = 0.0
        if lease is not None and lease.due():
            held = lease.claim()
            if held:
                return claimed(held)
        remaining = deadline - _mono()
        if remaining < 1:
            break
        try:
            if not relook:
                hold = int(min(_HOLD, remaining))
                if lease is not None:
                    # Never held past the renewal point.
                    hold = max(1, min(hold, int(lease.seconds_to_renewal())))
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
            time.sleep(min(_RETRY_PAUSE, max(0, deadline - _mono())))
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
    mode = ask_review.add_mutually_exclusive_group()
    mode.add_argument(
        "--live",
        action="store_true",
        help="ask for his marks as he makes them",
    )
    mode.add_argument(
        "--hold",
        action="store_true",
        help="his marks stay hidden from you until his verdict",
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
        "--answered",
        action="store_true",
        help="also answers you have already acknowledged",
    )
    listing.add_argument(
        "--open-only", action="store_true", help="only questions still waiting on him"
    )
    listing.add_argument(
        "--json",
        action="store_true",
        help="the rows as JSON, each with its page (artifact_id) -- for a script",
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
    ack.add_argument("--idempotency-key", metavar="KEY", help=_IDEMPOTENCY_HELP)

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
        "--timeout",
        default="55m",
        help="how long to wait (default 55m; 0 or forever: no limit)",
    )
    waiting.add_argument(
        "--office",
        metavar="NAME",
        help="hear only this office's answers (default: ARTIFACTS_SESSION before"
        " the first /; '' for every office)",
    )
    waiting.add_argument(
        "--all-offices",
        action="store_true",
        help="hear every office's answers under this key, as before",
    )
    waiting.add_argument(
        "--answers-only", action="store_true", help="do not wake for his comments"
    )
    waiting.add_argument(
        "--review", metavar="REVIEW-ID", help="wait for the verdict on this review"
    )
    waiting.add_argument(
        "--json", action="store_true", help="print each wake as one JSON line"
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

    release = subs.add_parser(
        "review-release",
        help="hand a review round back: this session cannot act on it",
    )
    release.add_argument("review", metavar="REVIEW-ID")
    release.add_argument(
        "--reason", required=True, help="why -- he reads it on the review card"
    )

    threads = subs.add_parser(
        "comments",
        help="comment threads on your pages; NEEDS YOUR REPLY marks his last word",
    )
    threads.add_argument("--page", metavar="ARTIFACT-ID", help="only this page")
    reply = subs.add_parser(
        "reply",
        help="answer a comment thread: on one of your pages, or with --page on"
        " another office's page you can read",
    )
    reply.add_argument("thread", metavar="THREAD-ID")
    reply.add_argument("text", metavar="TEXT")
    reply.add_argument(
        "--page",
        metavar="ARTIFACT-ID",
        help="the page the thread is on; needed on another office's page",
    )
    reply.add_argument("--idempotency-key", metavar="KEY", help=_IDEMPOTENCY_HELP)
    comment = subs.add_parser(
        "comment",
        help="comment on any page you can read, whole or on a quoted passage",
        description=(
            "Comment on a page another office published (or your own), the way "
            "Jeremy does. The page's owner is told and answers in the thread. "
            "Two fixed rules: your comment is never his answer to a decision and "
            "never acknowledges or closes one, and it is shown as yours, with "
            "your agent and session label, never as if it were him. Only a plain "
            "comment is allowed on another office's page: 'remove' and 'looks "
            "good' stay his, so say your view in the text. Afterwards this prints "
            "the page link and whose page it is, so you know whom to tell."
        ),
    )
    comment.add_argument("page", metavar="PAGE-ID")
    comment.add_argument(
        "text", metavar="TEXT", help='what you want to say; "-" reads it from stdin'
    )
    comment.add_argument(
        "--quote",
        metavar="TEXT",
        help="the passage you mean, as it reads on the current version. Refused "
        "if it is not found, or found more than once",
    )
    comment.add_argument(
        "--section",
        metavar="HEADING",
        help="with --quote: look only under the heading with this text, for a "
        "phrase that repeats",
    )
    comment.add_argument("--idempotency-key", metavar="KEY", help=_IDEMPOTENCY_HELP)
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
        "comment",
        "settle",
    ):
        warn_if_unlabelled(args.cmd)
    try:
        if args.cmd == "ask":
            row = _ask(args, key)
            print(_fmt(row))
            return 0
        if args.cmd == "ask-review":
            body = (
                {"mode": "live"} if args.live else {"mode": "held"} if args.hold else {}
            )
            row = _request("POST", f"{base}/artifacts/{args.page}/reviews", key, body)
            # A server older than hold/live ignores the mode and names none; say
            # so rather than print "None" or let --live pass for granted.
            mode = row.get("review_mode") or "unknown, the server predates hold/live"
            print(f"{row['id']}  review requested on {args.page} (marks: {mode})")
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
            if args.json:
                print(json.dumps(rows))
                return 0
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
        if args.cmd == "review-release":
            session = declared_session(args.session)
            if session is None:
                raise UsageError(
                    "review-release needs a session label: pass --session or "
                    "set ARTIFACTS_SESSION"
                )
            try:
                _request(
                    "POST",
                    f"{base}/reviews/{args.review}/release",
                    key,
                    {"session": session, "reason": args.reason},
                )
            except HTTPFailure as exc:
                if exc.status == 409 and isinstance(exc.body, dict):
                    print(_claim_line(exc.body), file=sys.stderr)
                    return _CLAIMED_EXIT
                raise
            print(f"released {args.review}: {args.reason}")
            return 0
        if args.cmd == "comments":
            found = _open_threads(
                key, base, args.page, _office_of(declared_session(args.session))
            )
            if not found:
                print(
                    "no open comment threads" + (" on that page" if args.page else "")
                )
                return 0
            found.sort(key=lambda t: not t["needs_reply"])
            print("\n\n".join(_fmt_thread(t, on_page=bool(args.page)) for t in found))
            return 0
        if args.cmd == "comment":
            if args.section and not args.quote:
                raise UsageError("--section needs --quote")
            text = sys.stdin.read() if args.text == "-" else args.text
            if not text.strip():
                raise UsageError("nothing to say: the comment text is empty")
            page = _request("GET", f"{base}/artifacts/{args.page}", key, None)
            payload: dict = {"body": text}
            if args.quote:
                payload["anchor"] = {"kind": "text", "quote": args.quote}
                if args.section:
                    payload["anchor"]["section"] = args.section
            written = _request(
                "POST",
                f"{base}/artifacts/{args.page}/comments",
                key,
                payload,
                idempotency_key=args.idempotency_key or str(uuid.uuid4()),
            )
            owner = page.get("agent") or "unknown"
            label = page.get("last_session")
            print(f"commented {written['id']} on {args.page}")
            print(f"  link: {page.get('page_url') or args.page}#comments")
            print(
                f"  page owner: {owner}"
                + (f" (last session {label})" if label else " (no session label)")
            )
            print(
                "  they are told through the service; if they run no waiter, "
                "tell them yourself"
            )
            return 0
        if args.cmd in ("reply", "settle"):
            # Your own pages by default; another office's page by --page,
            # where every comment, his included, may be answered.
            page = getattr(args, "page", None)
            mine = {
                t["id"]: t
                for t in _open_threads(
                    key, base, page, _office_of(declared_session(args.session))
                )
            }
            thread = mine.get(args.thread)
            if thread is None:
                where = "on that page" if page else "on your pages"
                hint = (
                    "artifacts-decide comments --page <page-id> lists them"
                    if page
                    else "artifacts-decide comments lists them; on another"
                    " office's page add --page <page-id>"
                )
                raise UsageError(
                    f"no open comment thread {args.thread} {where} ({hint})"
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
                idempotency_key=args.idempotency_key or str(uuid.uuid4()),
            )
            print(f"replied on {args.thread}; he sees it in the page's comments")
            return 0
        if args.cmd in ("ack", "close"):
            route = "acknowledge" if args.cmd == "ack" else "close"
            row = _request(
                "POST",
                f"{base}/decisions/{args.decision}/{route}",
                key,
                {},
                idempotency_key=(args.idempotency_key or str(uuid.uuid4()))
                if route == "acknowledge"
                else None,
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
    news.announce()  # the console script only; see news.py
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
