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
import hashlib
import json
import os
import random
import re
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from artifacts import images, news, pickup
from artifacts.client import (
    Conflict,
    HTTPFailure,
    UsageError,
    _api_base,
    _download,
    _request,
    _resolve_key,
    _upload_image,
    declared_session,
    installed_stamp,
    maybe_rotate,
    session_label,
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
    body = {
        "question": args.question,
        "options": options,
        "recommended": recommended,
    }
    # Only what was asked for goes out, so the call is unchanged for a question
    # that carries none of it.
    if args.context is not None:
        body["context"] = args.context
    if args.topic is not None:
        topic_id, _, title = args.topic.partition(":")
        body["topic"] = {"id": topic_id.strip()}
        if title.strip():
            body["topic"]["title"] = title.strip()
    if args.depends_on:
        body["depends_on"] = args.depends_on
    if args.replaces is not None:
        body["replaces"] = args.replaces
    if args.urgency is not None:
        body["urgency"] = args.urgency.replace("-", "_")
    if args.blocked_on is not None:
        body["blocked_on"] = args.blocked_on
    if args.needed_by is not None:
        body["needed_by"] = args.needed_by
    if args.for_office:
        body["for"] = args.for_office
    return _request(
        "POST", f"{_api_base()}/artifacts/{args.artifact}/decisions", key, body
    )


def _carries(row: dict) -> list[str]:
    """What a question carries, one compact line each, and only what is set:
    nothing at all for a plain question."""
    lines = []
    marks = []
    topic = row.get("topic")
    if topic:
        marks.append(f"topic {topic.get('id')} ({topic.get('title')})")
    urgency = row.get("urgency") or "when_you_can"
    if urgency == "blocking":
        marks.append(f"BLOCKING: {row.get('blocked_on')}")
    elif urgency == "by":
        marks.append("urgency by")
    if row.get("needed_by"):
        marks.append(f"needed by {row['needed_by']}")
    if marks:
        lines.append("  " + " · ".join(marks))
    links = []
    if row.get("depends_on"):
        links.append("depends on " + ", ".join(row["depends_on"]))
    if row.get("held_by"):
        links.append("HELD until answered: " + ", ".join(row["held_by"]))
    if row.get("replaces"):
        links.append(f"replaces {row['replaces']}")
    if row.get("replaced_by"):
        links.append(f"REPLACED by {row['replaced_by']}")
    if row.get("for"):
        links.append("also needed by " + ", ".join(row["for"]))
    if links:
        lines.append("  " + " · ".join(links))
    if row.get("context"):
        lines.append(f"  context: {row['context']}")
    return lines


def _set_aside_line(row: dict) -> str:
    """`SET ASIDE since 2026-11-02 13:40, page archived`: an open question whose page
    is archived or binned (2 Nov 2026). Said apart from "unanswered" because the two
    need opposite responses: this one cannot be answered until the page is back."""
    since = (row.get("set_aside_since") or "")[:16]
    by = f" by {row['set_aside_by']}" if row.get("set_aside_by") else ""
    return (
        f"  SET ASIDE since {since}, page {row.get('set_aside_why') or 'archived'}"
        f"{by} -- not waiting on him; it is open again if the page comes back"
    )


def _answered(row: dict) -> bool:
    """He has answered: a letter, or (a form) a submitted record. A form's `answer` stays
    null, so a test of `answer` alone calls a submitted form unanswered."""
    return bool(row.get("answer") or row.get("answer_record"))


def _speaker(person: str | None, source: str | None, relayer: str | None) -> str:
    """Whose answer it is, and, when a seat relayed it, which seat: `jeremy (relayed by
    ops-seat)`. The person is the speaker in a form and in a single answer alike; the
    relaying seat is only ever the relayer."""
    who = person or "?"
    if source in (None, "page") or not relayer:
        return who
    return f"{who} (relayed by {relayer})"


def _fmt_form(row: dict) -> list[str]:
    """A form's merged record: who and when, one `name: value` line per field (a relayed
    field says who relayed it from where), then the blanks, the stale fields with the
    version each was given on, a reopened form, and the ack hint."""
    rec = row["answer_record"]
    source = row.get("answer_source")
    by = _speaker(
        rec.get("answered_by") or row.get("answered_by"),
        source,
        row.get("recorded_by"),
    )
    how = "he submitted it" if source in (None, "page") else f"relayed from {source}"
    head = f"  FORM {rec.get('form', row.get('form_name', '?'))} "
    head += f"(version {rec.get('version', '?')}, {how}, by {by}, "
    head += f"{rec.get('answered_at', '?')})"
    lines = [head]
    sources = rec.get("sources") or {}
    for name, value in (rec.get("answers") or {}).items():
        if name in (rec.get("blank") or []):
            shown = "(blank)"
        elif isinstance(value, list):
            shown = ", ".join(str(v) for v in value)
        elif isinstance(value, bool):
            shown = "yes" if value else "no"
        else:
            shown = str(value)
        src = sources.get(name) or {}
        if src.get("source") not in (None, "page"):
            shown += f"   [relayed from {src['source']} by {src.get('by', '?')}]"
        lines.append(f"  {name}: {shown}")
    if rec.get("blank"):
        lines.append(f"  blank: {', '.join(rec['blank'])}")
    if rec.get("reopened"):
        lines.append("  REOPENED: a required field has no current answer")
    for item in rec.get("stale") or []:
        if not isinstance(item, dict):
            lines.append(f"  stale: {item}")
            continue
        why = "removed" if item.get("reason") == "removed" else "changed"
        text = item.get("text") or item.get("value")
        lines.append(
            f"  stale (answered on version {item.get('version', '?')}, question {why}"
            f" since): {item.get('name', '?')} = {text}"
            + (f"   [was: {item['label']}]" if item.get("label") else "")
        )
    if not row.get("acknowledged_at"):
        lines.append("  NOT YET ACKNOWLEDGED -- artifacts-decide ack " + row["id"])
    return lines


def _receipt_lines(row: dict) -> list[str]:
    """`RECEIPT <state>: <note>` for what the asking agent said it did with his answer."""
    rec = row.get("receipt")
    if not isinstance(rec, dict) or not rec.get("state"):
        return []
    state = str(rec["state"]).replace("_", "-")
    if rec.get("lapsed"):
        state += " (lapsed)"
    line = f"  RECEIPT {state}"
    if rec.get("note"):
        line += f": {rec['note']}"
    if rec.get("link"):
        line += f" <{rec['link']}>"
    return [line]


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
    lines.extend(_carries(row))
    lines.extend(_receipt_lines(row))

    if row.get("kind") == "form" and row.get("answer_record"):
        lines.extend(_fmt_form(row))
    elif row.get("answer"):
        chosen = row["answer"]
        source = row.get("answer_source") or "?"
        # Said plainly, because it is the difference between him and us. A tap
        # is 'page'; anything else was relayed by a seat, and the reader has to
        # be able to tell without knowing the vocabulary.
        how = "he tapped it" if source == "page" else f"relayed from {source}"
        by = _speaker(
            row.get("answered_by") or "jeremy", source, row.get("recorded_by")
        )
        lines.append(
            f"  ANSWER ({how}{f', by {by}' if source != 'page' else ''}): "
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
        if row.get("closed_at"):
            lines.append(f"  WITHDRAWN at {row['closed_at']}, never answered")
        elif row.get("set_aside"):
            # Parked, not dropped: not waiting on him, and not closed either.
            lines.append(_set_aside_line(row))
        else:
            lines.append("  unanswered")
        if row.get("reopened_at") and not row.get("set_aside"):
            lines.append(
                f"  REOPENED {row['reopened_at'][:16]}: the page came back, "
                "and this question with it"
            )
    # WHAT HAPPENED SINCE, under answered and unanswered alike. An outcome on an
    # OPEN question is the one with teeth: if the framing was corrected after it
    # was posed, Jeremy could answer the old question by accident.
    for outcome in row.get("outcomes") or []:
        who = outcome.get("session") or outcome.get("agent")
        lines.append(
            f"  SINCE ({outcome['created_at'][:16]}, {who}): {outcome['body']}"
        )
    # The thread under his note, so `answers` shows the conversation and not
    # only the letter and the note.
    for reply in row.get("replies") or []:
        who = (
            "HE REPLIED"
            if reply.get("author_kind") == "human"
            else f"{reply.get('author')} replied"
        )
        lines.append(
            f"  REPLY ({(reply.get('created_at') or '')[:16]}, {who}): "
            f"{reply.get('body') or ''}"
        )
    return "\n".join(lines)


def _get_decisions(url: str, key: str) -> list[dict]:
    """GET /decisions, following the pages. The service cuts a long list and says
    so in `X-Artifacts-Truncated`; a listing that stopped there without saying
    would read as complete, which for `--all --open-only` means "nothing else is
    waiting". So follow `X-Artifacts-Next` to the end, and if the service says
    it cut the list but names no way on, warn loudly on stderr rather than
    pass the partial list off as whole."""
    rows: list[dict] = []
    after: str | None = None
    while True:
        seen: dict = {}
        page_url = url + (f"&after={after}" if after else "")
        rows.extend(_request("GET", page_url, key, None, headers_out=seen) or [])
        if (seen.get("x-artifacts-truncated") or "").lower() != "true":
            return rows
        after = seen.get("x-artifacts-next")
        if not after:
            print(
                f"warning: the service cut this list at {len(rows)} rows and gave "
                "no way to get the rest; it is NOT complete",
                file=sys.stderr,
            )
            return rows


def _aside_section(row: dict) -> str | None:
    """The heading `list` puts before the first set-aside row."""
    return "SET ASIDE (not waiting on him; open again if the page comes back)"


def _print_rows(rows: list[dict], *, empty: str, section=None) -> int:
    if not rows:
        # Not silence. An empty result and a broken call look identical when
        # both print nothing, and this repo has already lost a shift to exactly
        # that (ij8.51: a wait client reported silence over answers it had been
        # handed). Say which one it is.
        print(empty)
        return 0
    if section is None:
        print("\n\n".join(_fmt(row) for row in rows))
        return 0
    blocks, heading_shown = [], False
    for row in rows:
        if row.get("set_aside") and not heading_shown:
            heading_shown = True
            blocks.append(section(row))
        blocks.append(_fmt(row))
    print("\n\n".join(blocks))
    return 0


def _new_answers(rows: list[dict]) -> list[dict]:
    """Answered and not yet acknowledged: what an agent must act on."""
    return [r for r in rows if _answered(r) and not r.get("acknowledged_at")]


# The two events that say a question was parked or came back (2 Nov 2026), and the
# name each goes by on a line and in --json.
_ASIDE_EVENTS = {"decision_set_aside": "set_aside", "decision_reopened": "reopened"}

# Exit code of a `wait --page` that has nothing left to wait on because every
# question it was waiting on is set aside. Distinct from 3 (timeout) and 4-8 (review).
_SET_ASIDE_EXIT = 9


def _aside_view(row: dict) -> dict:
    """An event row as the `--json` shape a waiter prints for it."""
    why = (row.get("summary") or "").split(": ", 1)[0]
    return {
        "event": _ASIDE_EVENTS[row["kind"]],
        "decision": row.get("ref_id"),
        "page": row.get("artifact_id"),
        "why": why,
        "by": row.get("actor"),
        "at": row.get("created_at"),
        "session": row.get("session"),
    }


def _aside_text(view: dict, question: str) -> str:
    """`SET ASIDE <id>  <question>` / `REOPENED ...`, two lines, for a terminal."""
    label = "SET ASIDE" if view["event"] == "set_aside" else "REOPENED"
    tail = (
        "not closed: it comes back open if the page does"
        if view["event"] == "set_aside"
        else "open again and unanswered, same id: re-arm your wait"
    )
    by = f" by {view['by']}" if view.get("by") else ""
    at = f" at {view['at'][:16]}" if view.get("at") else ""
    asked = view.get("session") or "an unlabelled session"
    return (
        f"{label} {view['decision']}  {question}\n"
        f"  page {view['page']} {view['why']}{by}{at}; asked by {asked}; {tail}"
    )


def _print_aside(views: list[tuple[dict, str]], args) -> None:
    for view, question in views:
        if args.json:
            print(json.dumps(view, separators=(",", ":")), flush=True)
        else:
            print(_aside_text(view, question), flush=True)


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
        thread["version_n"] = first.get("version_n")
        thread["anchor_state"] = first.get("anchor_state") or ""
        thread["anchor_note"] = first.get("anchor_note") or ""
        thread["anchor_current"] = first.get("anchor_current") or ""
        pin = first.get("anchor")
        thread["element"] = (
            pin if isinstance(pin, dict) and pin.get("kind") == "element" else None
        )
        thread["heading"] = (
            pin.get("heading") if isinstance(pin, dict) and pin.get("heading") else ""
        )
        anchor = first.get("anchor")
        thread["decision_id"] = (
            anchor.get("decision_id")
            if isinstance(anchor, dict) and anchor.get("kind") == "decision"
            else None
        )
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


def _where(thread: dict) -> str:
    """Where a pinned comment sits, as a parenthesis after its quote: the
    heading it was under, the version it was made on, and, if the page has
    moved on, whether the spot is edited or gone. Empty for a comment that
    says nothing of the sort (an older row, a whole-page comment)."""
    parts = []
    if thread.get("heading"):
        parts.append(f'under "{thread["heading"]}"')
    if thread.get("version_n") is not None:
        parts.append(f"v{thread['version_n']}")
    state = thread.get("anchor_state")
    if state == "outdated":
        parts.append("OUTDATED: this spot is gone from the current version")
    elif state == "edited":
        parts.append("text edited since")
    if thread.get("anchor_note"):
        parts.append(thread["anchor_note"])
    return f"  ({' · '.join(parts)})" if parts and state else ""


def _element_line(pin: dict) -> str:
    """What an element pin is on, in the page's own terms: the tag and id, the
    heading it sits under, a table cell's row and column, and the alt text or
    label of an image or chart. Every part is optional; none is invented."""
    tag = pin.get("tag") or "element"
    ident = f' id="{pin["id"]}"' if pin.get("id") else ""
    line = f"<{tag}{ident}>"
    if pin.get("row") or pin.get("column"):
        cell = [
            f'{name} "{pin[key]}"'
            for name, key in (("row", "row"), ("column", "column"))
            if pin.get(key)
        ]
        line += " " + ", ".join(cell)
    if pin.get("label"):
        line += f' "{pin["label"]}"'
    if pin.get("heading"):
        line += f' under "{pin["heading"]}"'
    return line


def _read_pictures(paths: list[str] | None) -> list[bytes]:
    """The pictures named by --image, read and checked with the service's own limits
    BEFORE anything is uploaded, so a bad third file costs no upload of the first two.
    Nothing is sent unless all of them pass."""
    paths = list(paths or [])
    if len(paths) > images.PER_COMMENT:
        raise UsageError(f"a comment takes at most {images.PER_COMMENT} pictures")
    found = []
    for path in paths:
        try:
            with open(path, "rb") as handle:
                data = handle.read(images.MAX_BYTES + 1)
        except OSError:
            raise UsageError(f"{path}: cannot read that file") from None
        try:
            images.inspect(data)
        except images.ImageRefused as exc:
            raise UsageError(f"{path}: {exc.detail}") from None
        found.append(data)
    return found


def _upload_pictures(
    page: str, base: str, key: str, pictures: list[bytes]
) -> list[str]:
    return [
        _upload_image(f"{base}/artifacts/{page}/comment-images", key, data)["image_id"]
        for data in pictures
    ]


_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
# The most pictures one `comments` call downloads; the rest are listed with their sizes.
_IMAGES_PER_CALL = 20


def _image_cache() -> str:
    root = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(root, "artifacts", "images")


def _private_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


_SERVER_ID = re.compile(r"[a-z2-7]{26}")


def _fetch_images(threads: list[dict], key: str, base: str) -> None:
    """Download every picture of the printed threads into the cache and set `local` on
    each: `<cache>/artifacts/images/<comment id>/<n>-<sha256 start>.<ext>`, directory 0700,
    files 0600. The name carries the sha256 of the bytes, so a repeat run finds the file
    and downloads nothing. At most `_IMAGES_PER_CALL` a call; no URL is ever printed."""
    budget = _IMAGES_PER_CALL
    for thread in threads:
        for message in thread["messages"]:
            for n, pic in enumerate(message.get("images") or [], start=1):
                digest = str(pic.get("sha256") or "")
                # Both ids come from the server and become a path and a URL: only the
                # service's own id shape (26 base32 characters) is ever used for either.
                if not (
                    _SERVER_ID.fullmatch(str(message.get("id") or ""))
                    and _SERVER_ID.fullmatch(str(pic.get("id") or ""))
                ):
                    print(
                        "warning: skipped a picture with an unexpected id",
                        file=sys.stderr,
                    )
                    continue
                ext = _EXTENSIONS.get(pic.get("content_type"), "bin")
                folder = os.path.join(_image_cache(), message["id"])
                target = os.path.join(folder, f"{n}-{digest[:16]}.{ext}")
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    continue
                if os.path.isfile(target):
                    pic["local"] = target
                    continue
                if budget <= 0:
                    continue
                budget -= 1
                data = _download(
                    f"{base}/comments/{message['id']}/images/{pic['id']}", key
                )
                if hashlib.sha256(data).hexdigest() != digest:
                    continue  # not the picture the listing named: keep nothing
                _private_dir(os.path.dirname(os.path.dirname(target)))
                _private_dir(folder)
                part = f"{target}.part"
                fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                os.replace(part, target)
                pic["local"] = target


def _picture_line(pic: dict) -> str:
    kind = (pic.get("content_type") or "image/?").split("/", 1)[-1]
    sized = (
        f"{kind} {pic.get('width')}x{pic.get('height')}, "
        f"{max(1, round((pic.get('byte_size') or 0) / 1024))} KB"
    )
    where = pic.get("local") or "(not downloaded)"
    return f"  image: {where}  ({sized})"


def _fmt_thread(thread: dict, *, on_page: bool = False) -> str:
    head = "NEEDS YOUR REPLY" if thread["needs_reply"] else "replied"
    lines = [
        f"{thread['id']}  comment thread on page {thread['artifact_id']}  [{head}]"
    ]
    if thread.get("reopened_by"):
        lines.append(
            f"  REOPENED by {thread['reopened_by']}: it was settled, and is open again"
        )
    if thread.get("decision_id"):
        lines.append(f"  under his answer on decision {thread['decision_id']}")
    if thread.get("element"):
        lines.append(f"  element: {_element_line(thread['element'])}")
    if thread["quote"]:
        label = {"remove": "REMOVE THIS", "looks_good": "LOOKS GOOD"}.get(
            thread.get("kind"), "on"
        )
        lines.append(f'  {label}: "{thread["quote"]}"{_where(thread)}')
        if thread.get("anchor_state") == "edited" and thread.get("anchor_current"):
            lines.append(f'  now: "{thread["anchor_current"]}"')
    for m in thread["messages"]:
        lines.extend(_picture_line(pic) for pic in m.get("images") or [])
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
    if thread["needs_reply"] and thread.get("decision_id"):
        lines.append(
            f'  reply: artifacts-decide reply-note {thread["decision_id"]} "..."'
            f"   · done: artifacts-decide settle {thread['id']}"
        )
    elif thread["needs_reply"]:
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

# THE SERVICE BEING GONE is a different failure from a blip. A deploy replaces
# the one pod (Recreate), and the ingress answers 502/503/504 for about a
# minute; a connection error (status 0) is the same thing from the other side.
# 26 Oct 2026: quarterdeck's waiter lost 9 of 19 holds to that gap by asking
# again every 5 s. So an outage backs off longer, with jitter so several
# waiters do not return together, and is bounded by TIME, not by tries: it
# keeps asking for `_UNAVAILABLE_LIMIT` seconds of unbroken unavailability and
# then says so and fails. The cursor only advances after a 200, so nothing is
# lost while it waits.
_UNAVAILABLE = (0, 502, 503, 504)
_UNAVAILABLE_PAUSE = 20.0
_UNAVAILABLE_LIMIT = 300.0
# The default for --timeout; ARTIFACTS_WAIT_TIMEOUT raises it for every run.
_DEFAULT_TIMEOUT = "55m"


class _Outage:
    """Tracks one unbroken stretch of the service being unavailable."""

    def __init__(self) -> None:
        self.since: float | None = None

    def ok(self) -> None:
        self.since = None

    def pause(self, exc: HTTPFailure, deadline: float) -> float | None:
        """Seconds to sleep before asking again, or None when the outage has
        outlasted `_UNAVAILABLE_LIMIT` and the wait should give up."""
        if exc.status not in _UNAVAILABLE:
            self.since = None
            return min(_RETRY_PAUSE, max(0.0, deadline - _mono()))
        now = _mono()
        if self.since is None:
            self.since = now
        if now - self.since >= _UNAVAILABLE_LIMIT:
            return None
        pause = _UNAVAILABLE_PAUSE * random.uniform(0.75, 1.25)  # noqa: S311
        return max(0.0, min(pause, deadline - now))


def _gave_up() -> int:
    minutes = _UNAVAILABLE_LIMIT / 60
    print(
        f"the service has been unavailable for {minutes:g} minutes (502/503/504 "
        "or no connection): giving up. No answer was lost; run this again"
        " when it is back.",
        file=sys.stderr,
    )
    return 1


def _timed_out(what: str, args) -> int:
    print(
        f"nothing yet: no {what} before the timeout. That is this command's"
        f" time limit ({args.timeout}), not a failure: run it again, or raise"
        " it with --timeout (or ARTIFACTS_WAIT_TIMEOUT; 0 or forever for no"
        " limit).",
        file=sys.stderr,
    )
    return 3


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


def _hears(session: str, office: str | None) -> bool:
    """Whether a waiter labelled `session` hears `office`'s answers: its label is
    the office or starts with `office/`, or it has no label and so hears every
    office. No office named: any waiter. (The service applies the same rule to
    the line under a question; `listening._reads_for`.)"""
    if office is None or session == "":
        return True
    return session == office or session.startswith(office + "/")


def _already_waiting(base: str, key: str, office: str | None) -> list[str]:
    """Labels of this office's sessions already holding a wait. Best effort: an
    older server without the route, or any failure, says nothing."""
    try:
        found = _request("GET", f"{base}/listening", key, None) or {}
    except HTTPFailure:
        return []
    return [
        w.get("session") or "(unlabelled)"
        for w in found.get("waiting", [])
        if _hears(w.get("session") or "", office)
    ]


def _listening(args, key: str, base: str) -> int:
    """Whether a session of this office is waiting for Jeremy's answer, and with
    --decision where his answer to that question goes. Exit 0 when one is
    waiting, 3 when none is (not a failure: the same 3 as a quiet `wait`), 1
    when the service refuses. Read-only; it does not count as a listener."""
    office = args.office.strip() or None if args.office is not None else None
    if args.office is None:
        session = declared_session(args.session)
        office = (session.split("/", 1)[0].strip() or None) if session else None
    url = f"{base}/listening"
    if args.decision:
        url += f"?decision={args.decision}"
    found = _request("GET", url, key, None) or {}
    mine = [
        w for w in found.get("waiting", []) if _hears(w.get("session") or "", office)
    ]
    if args.json:
        print(json.dumps({**found, "office": office, "office_waiting": bool(mine)}))
    else:
        whose = f"of {office} " if office else ""
        if mine:
            names = ", ".join(w.get("session") or "(unlabelled)" for w in mine)
            print(f"waiting now: {names}")
        else:
            print(f"no session {whose}is waiting for his answer")
        seen = [
            r["last_read_at"]
            for r in mine + found.get("recent", [])
            if _hears(r.get("session") or "", office) and r.get("last_read_at")
        ]
        if seen and not mine:
            print(f"last asked for events: {max(seen)}")
        if "decision" in found:
            print(found["decision"]["line"])
    waiting = (
        found["decision"]["state"] == "waiting_now"
        if "decision" in found
        else bool(mine)
    )
    return 0 if waiting else 3


_PICKUP_LAPSE = (
    "It shows only while you have checked for your events in the last "
    f"{pickup.PICKUP_FRESH // timedelta(hours=1)} hours; it returns when you check again."
)


def _pickup(args, key: str, base: str) -> int:
    """Set, clear or show this agent's declared pickup line."""
    # Words after the subcommand are one line, so quoting is optional.
    args.line = " ".join(args.line) if args.line else None
    if args.line is not None and args.clear:
        raise UsageError("give a line or --clear, not both")
    if args.clear:
        _request("DELETE", f"{base}/listening/pickup", key, None)
        print('pickup cleared; questions say "at its next check"')
        return 0
    if args.line is None:
        current = (_request("GET", f"{base}/listening", key, None) or {}).get("pickup")
        print(current if current else "no pickup line set")
        if current:
            print(_PICKUP_LAPSE, file=sys.stderr)  # stdout stays the bare line
        return 0
    kept = _request("PUT", f"{base}/listening/pickup", key, {"pickup": args.line})
    print(f"pickup set: {kept['pickup']}")
    print(_PICKUP_LAPSE)
    return 0


def _wait(args, key: str, base: str) -> int:
    """Block until Jeremy answers, print the answer(s), exit 0.

    homelab-ij8.106, from the first outside agent: it asked nine questions,
    ended its turn, and three answers sat unseen until he asked whether it had
    read them -- and every agent was left to hand-write its own listener.

    Exit codes a caller can branch on: 0 answers printed; 3 nothing by the
    timeout (ask again); 9 set aside (below); 1 refused (the key); 2 usage. Built
    to run as a background task that wakes its agent when it exits.

    SET ASIDE (2 Nov 2026). Archiving or binning a page parks its open questions
    (they are not closed). A waiter with `--page` whose every open question there
    is set aside prints the set-aside lines and exits 9; it does not come back by
    itself, so re-arm it when you read the `decision_reopened` event. An
    office-wide waiter (no `--page`) prints one SET ASIDE line per question, and
    one REOPENED line when it comes back, and keeps waiting: other pages' answers
    still have to reach it.

    REOPENED THREADS (5 Nov 2026). When he reopens a comment thread he had settled, the
    journal gets a `thread_reopened` event and this wakes and prints that thread under a
    `REOPENED by <who>` line, even if the last word in it is yours. (Not with
    --answers-only.) Your own reopen of your own thread is silent.

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
        # Another session of this office may already hold a wait. Say so and
        # carry on: two waiters are harmless, and refusing would strand an
        # office whose waiter has just died inside the service's grace window.
        others = _already_waiting(base, key, office)
        if others:
            print(
                f"note: {', '.join(others)} already waiting for {office}'s answers; "
                "waiting as well. Run `artifacts-decide listening` first to avoid "
                "a second wait.",
                file=sys.stderr,
            )
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
            if t["needs_reply"]
            and (
                _belongs(thread_session(t), office, key_agent)
                # A thread this office wrote in, on a page it does not own
                # (a reply under his note on another office's page): the
                # page's session is not this office's, its own message is.
                or any(
                    m.get("author_kind") == "agent"
                    and m.get("session")
                    and _belongs(m["session"], office, key_agent)
                    for m in t["messages"]
                )
            )
        ]

    next_due = [float("inf")]
    page_gone = False

    def look() -> tuple[list[dict], list[dict]]:
        """This office's news, less anything already shown within the hour;
        notes when the earliest held-back item falls due."""
        nonlocal page_gone
        try:
            answers, threads = current(), owed()
        except HTTPFailure as exc:
            # A BINNED page answers 404 like a missing one: nothing on it is
            # waiting. Noted, so the caller can say why instead of failing.
            if not page or exc.status != 404:
                raise
            page_gone = True
            return [], []
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

    def page_questions() -> list[dict]:
        """This office's questions and reviews on the waited page."""
        nonlocal page_gone
        try:
            rows = (
                _request("GET", f"{base}/artifacts/{page}/decisions", key, None) or []
            )
        except HTTPFailure as exc:
            # A BINNED page answers 404 like a missing one: nothing on it is
            # waiting, and the set-aside event already said why.
            if exc.status != 404:
                raise
            rows = []
            page_gone = True
        return [
            r
            for r in rows
            if r.get("kind", "question") in ("question", "review")
            and _belongs(r.get("session"), office, key_agent)
        ]

    if page:
        # Started on a page whose questions are all parked already: say so now,
        # rather than hold for a timeout on something that cannot be answered.
        mine = page_questions()
        parked = [r for r in mine if r.get("set_aside")]
        live = [
            r
            for r in mine
            if not r.get("set_aside") and not _answered(r) and not r.get("closed_at")
        ]
        if page_gone and not live:
            # A binned page lists nothing, so there is no row to print: say so
            # and exit as a parked page does, instead of waiting out the timeout.
            print(
                f"SET ASIDE: page {page} is binned or gone; nothing on it can "
                "be answered now",
                flush=True,
            )
            return _SET_ASIDE_EXIT
        if parked and not live:
            _print_aside(
                [
                    (
                        {
                            "event": "set_aside",
                            "decision": r["id"],
                            "page": page,
                            "why": r.get("set_aside_why"),
                            "by": r.get("set_aside_by"),
                            "at": r.get("set_aside_since"),
                            "session": r.get("session"),
                        },
                        r.get("question", ""),
                    )
                    for r in parked
                ],
                args,
            )
            return _SET_ASIDE_EXIT
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
        kinds.update(("comment_added", "thread_reopened"))
    outage = _Outage()
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
            pause = outage.pause(exc, deadline)
            if pause is None:
                return _gave_up()
            time.sleep(pause)
            continue
        outage.ok()
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
        aside = [
            r
            for r in rows
            if r.get("kind") in _ASIDE_EVENTS
            and (not page or r.get("artifact_id") == page)
            and _belongs(r.get("session"), office, key_agent)
        ]
        if aside:
            _print_aside(
                [
                    (
                        _aside_view(r),
                        (r.get("summary") or "").split(": ", 1)[-1],
                    )
                    for r in aside
                ],
                args,
            )
        if woke or time.monotonic() >= next_due[0]:
            found, waiting_threads = look()
            # A reopened thread is reported whether or not it is owed a reply and
            # whatever was shown within the hour: it is the news itself.
            reopened = {
                r["ref_id"]: r.get("actor") or "someone"
                for r in rows
                if r.get("kind") == "thread_reopened"
                and r.get("ref_id")
                and (not page or r.get("artifact_id") == page)
                and _belongs(r.get("session"), office, key_agent)
            }
            if reopened and not args.answers_only:
                have = {t["id"] for t in waiting_threads}
                for t in _open_threads(key, base, page, office):
                    if t["id"] in reopened:
                        t["reopened_by"] = reopened[t["id"]]
                        if t["id"] in have:
                            waiting_threads = [
                                t if w["id"] == t["id"] else w for w in waiting_threads
                            ]
                        else:
                            waiting_threads.append(t)
            if found or waiting_threads:
                return report(found, waiting_threads)
        if (
            page
            and any(r["kind"] == "decision_set_aside" for r in aside)
            and not any(
                not r.get("set_aside")
                and not r.get("answer")
                and not r.get("closed_at")
                for r in page_questions()
            )
        ):
            return _SET_ASIDE_EXIT
    return _timed_out("answer", args)


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
    marks and no verdict yet, 8 another session holds the round, 9 the page was
    archived or binned and the review set aside (no verdict is coming until it is
    back: re-arm on the `decision_reopened` event), 3 timeout, 1 refused, 2 usage. `--require-approval` folds 0/4 to 0 and 5/6/7/8 to 1.

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
    session = session_label(args.session)
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
    outage = _Outage()
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
                outage.ok()
                parked = [
                    r
                    for r in rows
                    if r.get("kind") == "decision_set_aside"
                    and r.get("ref_id") == review_id
                ]
                if parked:
                    _print_aside(
                        [
                            (
                                _aside_view(r),
                                (r.get("summary") or "").split(": ", 1)[-1],
                            )
                            for r in parked
                        ],
                        args,
                    )
                    return 1 if args.require_approval else _SET_ASIDE_EXIT
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
            pause = outage.pause(exc, deadline)
            if pause is None:
                return _gave_up()
            time.sleep(pause)
    return _timed_out("verdict", args)


def build_parser() -> argparse.ArgumentParser:
    parser = _StaleAwareParser(
        prog="artifacts-decide",
        description="ask Jeremy a lettered question on a page, and read the answer",
        epilog=(
            "A form published in a page (data-artifact-form) is answered in the gallery: "
            "`list`, `answers` and `wait` print its submitted record, one `name: value` "
            "line per field with blank and stale fields named. Read it by "
            "answer_record, never by `answer`, which stays empty for a form. A republish "
            "that changes a field's wording or options moves his old answer to the "
            "record's `stale` list; relay a form answer given elsewhere with "
            "`record ID --field NAME=VALUE ... --note`."
        ),
    )
    parser.add_argument("--key-file")
    # Same as artifacts-publish: a key from an open descriptor never touches
    # disk. See client._read_key_fd.
    parser.add_argument("--key-fd", type=int, metavar="N")
    # Which session raised the question, when several share one key. It rides on
    # the decision row, so a shared feed can be narrowed to your own -- see
    # artifacts.client._SESSION_ENV.
    parser.add_argument(
        "--session",
        metavar="LABEL",
        help=(
            "which session this is; defaults to $ARTIFACTS_SESSION, else "
            "<office>/<session id> from Claude Code's QD_OFFICE or "
            "ARTIFACTS_SEAT. Only a label you set chooses whose answers wait hears."
        ),
    )
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
    ask.add_argument(
        "--context",
        metavar="TEXT",
        help="what he needs to know to answer, plain text, at most 1000 characters",
    )
    ask.add_argument(
        "--topic",
        metavar="ID[:TITLE]",
        help="the subject this belongs to: a lowercase slug, plus a title the first"
        " time you use that slug",
    )
    ask.add_argument(
        "--depends-on",
        action="append",
        default=[],
        metavar="ID",
        help="repeatable (max 10); one of your own questions this waits on",
    )
    ask.add_argument(
        "--replaces",
        metavar="ID",
        help="one of your own still-open questions; asking this one closes it",
    )
    ask.add_argument(
        "--urgency",
        choices=["blocking", "by", "when-you-can"],
        help="blocking needs --blocked-on, by needs --needed-by; default when-you-can",
    )
    ask.add_argument(
        "--blocked-on",
        metavar="TEXT",
        help="with --urgency blocking: what has stopped, at most 200 characters",
    )
    ask.add_argument(
        "--needed-by",
        metavar="ISO8601",
        help="when you need the answer, with a timezone: 2026-10-22T15:00:00Z",
    )
    ask.add_argument(
        "--for",
        dest="for_office",
        action="append",
        default=[],
        metavar="OFFICE",
        help="repeatable (max 5); another office that also needs the answer",
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
        description=(
            "Lists decisions across every page of the scope, following the "
            "service's pages to the end. To read one page's questions, "
            "answered ones included, use: artifacts-decide answers PAGE. Questions "
            "on an archived or binned page are SET ASIDE, not closed: they are "
            "listed last in a SET ASIDE section and are not counted as waiting."
        ),
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
        description=(
            "Write down an answer he gave somewhere that is not the page. A question "
            "takes --letter. A FORM takes one --field NAME=VALUE per field he answered "
            "(repeat a name for a checkbox group, or give a,b; a lone checkbox is "
            "true or false): each field is stored with its source, never counts as his "
            "own submit, and never overwrites a value he submitted after --said-at. "
            "A field answered on an older wording of the form is stale: relay it again "
            "for the current wording."
        ),
    )
    record.add_argument("decision", metavar="DECISION-ID")
    record.add_argument("--letter", help="a question's lettered answer")
    record.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="a form field he answered (repeatable)",
    )
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
        help="append what this decision caused, or what changed under it "
        "(one-way; to answer his note use reply-note)",
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

    reply_note = subs.add_parser(
        "reply-note",
        help="reply under the note he left when answering a decision",
        description=(
            "Reply under the note Jeremy left with his answer, on any page you "
            "can read. He sees it under his note and can answer; the page's "
            "owner is told when you are another office, and you hear his answer "
            "through artifacts-decide wait --page. It never changes the "
            "decision: no answer, acknowledgement or closing. Use outcome to "
            "record what the decision caused; this is for talking back. "
            '"-" reads the text from stdin.'
        ),
    )
    reply_note.add_argument("decision", metavar="DECISION-ID")
    reply_note.add_argument("text", metavar="TEXT")
    reply_note.add_argument("--idempotency-key", metavar="KEY", help=_IDEMPOTENCY_HELP)

    ack = subs.add_parser(
        "ack", help="say you have seen an answer (the same as `receipt seen`)"
    )
    ack.add_argument("decision", metavar="DECISION-ID")
    ack.add_argument("--idempotency-key", metavar="KEY", help=_IDEMPOTENCY_HELP)

    receipt = subs.add_parser(
        "receipt",
        help="say what you did with his answer: seen, working, done, blocked, wont-do",
        description=(
            "Your own statement about his answer, shown to him and never verified. "
            "blocked and wont-do need --note; --link only with done; --for only with "
            "working (30m, 4h; at most 12h, default 4h). done needs his answer first "
            "and does not close the decision. `seen` is what `ack` does."
        ),
    )
    receipt.add_argument("decision", metavar="DECISION-ID")
    receipt.add_argument(
        "state", choices=["seen", "working", "done", "blocked", "wont-do"]
    )
    receipt.add_argument(
        "--note", default="", help="one line; needed for blocked, wont-do"
    )
    receipt.add_argument("--link", help="a URL for the work (done only)")
    receipt.add_argument("--for", dest="lifetime", help="how long working holds")
    receipt.add_argument("--idempotency-key", metavar="KEY", help=_IDEMPOTENCY_HELP)

    close = subs.add_parser("close", help="withdraw a question you no longer need")
    close.add_argument("decision", metavar="DECISION-ID")

    waiting = subs.add_parser(
        "wait",
        help="block until he answers, print the answer, exit 0 (3 if nothing by --timeout;"
        " 9 if --page's questions are all set aside because the page was archived or binned)",
        description=(
            "Block until he answers, print the answer. Exit codes: 0 an answer was"
            " printed; 3 nothing by --timeout (a time limit, not a failure: re-arm it);"
            " 9 the question was set aside because its page was archived or binned"
            " (unarchiving or restoring reopens it: re-arm when you see"
            " decision_reopened); 1 refused (the key); 2 usage."
        ),
    )
    waiting.add_argument(
        "--page", metavar="ARTIFACT-ID", help="only answers on this page"
    )
    waiting.add_argument(
        "--timeout",
        default=os.environ.get("ARTIFACTS_WAIT_TIMEOUT") or _DEFAULT_TIMEOUT,
        help="how long to wait before exiting 3, a time limit and not a failure"
        " (default 55m, or ARTIFACTS_WAIT_TIMEOUT; 0 or forever: no limit)",
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

    listen = subs.add_parser(
        "listening",
        help="is a session of your office waiting for his answer? exit 0 yes, 3 no",
        description=(
            "Asks the service whether a session of your office holds a wait for "
            "his answers right now (a held `artifacts-decide wait`, or any client "
            "holding GET /events?wait=...). Exit 0: one does. Exit 3: none does "
            "-- not a failure. Run it before arming a wait, so a restarted "
            "session does not start a second one: "
            "`artifacts-decide listening || artifacts-decide wait`. With "
            "--decision ID it also prints where his answer to that question "
            "goes, in the words his page uses. It sees only waits held through "
            "the service: an office that polls `list` leaves no trace, and which "
            "Slack office a question maps to is not known here. Your own agent's "
            "sessions only."
        ),
    )
    listen.add_argument(
        "--decision", metavar="DECISION-ID", help="also say where his answer goes"
    )
    listen.add_argument(
        "--office",
        metavar="NAME",
        help="the office to check (default: ARTIFACTS_SESSION before the first /;"
        " '' for any session of this key)",
    )
    listen.add_argument(
        "--json", action="store_true", help="the service's answer, as JSON"
    )

    pick = subs.add_parser(
        "pickup",
        help="say when your office acts on his answer; shown on his page as"
        ' "<agent> says: ..."',
        description=(
            "Declares one short line (at most 120 characters, one line, plain "
            "text) for when your office acts on his answer, e.g. `artifacts-"
            'decide pickup "within the hour"`. His page then reads "<agent> '
            'says: within the hour" under your open questions, in place of "acted '
            'on at its next check", whenever no session of yours is waiting and '
            "the Slack copy is not late. It is your word: the service does not check "
            "or promise it, so keep it true or clear it. It shows only while you have "
            f"checked for your events in the last {pickup.PICKUP_FRESH // timedelta(hours=1)} "
            "hours, and returns when you check again. One line per agent (key), "
            "replacing any earlier one. --clear removes it; with neither a line "
            'nor --clear, prints the current one. A line that starts with "-" '
            'goes after "--" (`pickup -- "- reading it now"`); a line with a '
            'space in it needs no "--".'
        ),
    )
    pick.add_argument(
        "line",
        nargs="*",
        metavar="LINE",
        help="the line to show (words are joined; put -- before a line that"
        " starts with a dash)",
    )
    pick.add_argument("--clear", action="store_true", help="remove the line")

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
        help="comment threads on your pages; NEEDS YOUR REPLY marks his last word."
        " A pinned comment says what it is pinned to (an `element:` line for a"
        " chart, image or table: tag, id, heading, a cell's row and column, an"
        " image's alt text; a quote otherwise), the version it was pinned on, and"
        " whether a republish left the spot current, edited (with a `now:` line"
        " for what stands there) or OUTDATED",
    )
    threads.add_argument("--page", metavar="ARTIFACT-ID", help="only this page")
    threads.add_argument(
        "--outdated",
        action="store_true",
        help="only the threads whose pinned spot a republish removed",
    )
    threads.add_argument(
        "--no-images",
        action="store_true",
        help="do not download the pictures on the comments; print their sizes only."
        " Otherwise each is fetched once into $XDG_CACHE_HOME/artifacts/images/"
        "<comment id>/ (at most 20 a call) and its path is printed",
    )
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
    reply.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="PATH",
        help="attach a picture (PNG, JPEG or WebP, 5 MB, 4000 px a side); repeat for"
        " up to 4. Checked here against the service's limits first, and all or"
        " nothing. Metadata (EXIF, GPS) is stripped by the service. Pictures are not"
        " checked for secrets: do not attach a screenshot of one",
    )
    reply.add_argument("--idempotency-key", metavar="KEY", help=_IDEMPOTENCY_HELP)
    comment = subs.add_parser(
        "comment",
        help="comment on any page you can read, whole, on a quoted passage or on an"
        " element, with up to 4 pictures",
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
        "if it is not found, or found more than once. The comment is pinned to it "
        "and to the heading it sits under, and is marked outdated if a republish "
        "removes the passage",
    )
    comment.add_argument(
        "--section",
        metavar="HEADING",
        help="with --quote: look only under the heading with this text, for a "
        "phrase that repeats",
    )
    comment.add_argument(
        "--element",
        metavar="ID",
        help="pin the comment to the element with this id (a chart, an image, a "
        "table, a section) instead of to a passage; not with --quote. The id "
        "must be on the current version exactly once. Its text, or an image's "
        "alt text, becomes the quote, and the comment is marked edited or "
        "outdated if a republish changes or removes it",
    )
    comment.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="PATH",
        help="attach a picture (PNG, JPEG or WebP, 5 MB, 4000 px a side); repeat for"
        " up to 4. Checked here against the service's limits first, and all or"
        " nothing. Metadata (EXIF, GPS) is stripped by the service. Pictures are not"
        " checked for secrets: do not attach a screenshot of one",
    )
    comment.add_argument("--idempotency-key", metavar="KEY", help=_IDEMPOTENCY_HELP)
    settle = subs.add_parser("settle", help="mark a comment thread done")
    settle.add_argument("thread", metavar="THREAD-ID")
    reopen = subs.add_parser(
        "reopen",
        help="put a settled comment thread back among the open ones (silent for"
        " your own page: only his reopen wakes you, as a thread_reopened event)",
    )
    reopen.add_argument("thread", metavar="THREAD-ID")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    use_session(session_label(args.session))
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
        "reply-note",
        "ack",
        "receipt",
        "close",
        "reply",
        "comment",
        "settle",
        "reopen",
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
            # WITHDRAWN ONES INCLUDED. A withdrawn question is part of its
            # page's record -- hiding it would mean a seat could be told "your
            # question is gone" by an event and then find no trace of it anywhere
            # (homelab-ij8.44). Archiving no longer withdraws (2 Nov 2026): it
            # sets questions aside, and `list` has the SET ASIDE section.
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
            if args.open_only:
                # Set aside is not waiting (the service returns them, flagged).
                rows = [r for r in rows if not r.get("set_aside")]
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
                rows = _get_decisions(
                    f"{base}/decisions?include_answered=false&scope={scope}", key
                )
            else:
                # NEW ANSWERS FIRST (homelab-ij8.106): the first outside agent
                # ran `list`, saw only open questions, and missed three of his
                # answers and their notes. Answered-and-unacknowledged is what an
                # agent must act on, so it leads; acknowledged answers only with
                # --answered.
                rows = _get_decisions(
                    f"{base}/decisions?include_answered=true&scope={scope}"
                    "&include_set_aside=true",
                    key,
                )
                fresh = _new_answers(rows)
                still_open = [
                    r for r in rows if not _answered(r) and not r.get("set_aside")
                ]
                # Their own section, after the open ones: parked, never counted as
                # waiting (2 Nov 2026).
                parked = [r for r in rows if not _answered(r) and r.get("set_aside")]
                done = [r for r in rows if _answered(r) and r.get("acknowledged_at")]
                rows = fresh + still_open + (done if args.answered else []) + parked
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
            return _print_rows(rows, empty=empty, section=_aside_section)
        if args.cmd == "record":
            if bool(args.letter) == bool(args.field):
                raise UsageError(
                    "give --letter (a question) or one or more --field NAME=VALUE (a form)"
                )
            payload = {"note": args.note, "source": args.source}
            if args.letter:
                payload["letter"] = args.letter.strip().lower()
            else:
                given: dict = {}
                for item in args.field:
                    name, sep, value = item.partition("=")
                    if not sep or not name:
                        raise UsageError(f"--field wants NAME=VALUE, not {item!r}")
                    if name in given:
                        prior = given[name]
                        given[name] = [
                            *(prior if isinstance(prior, list) else [prior]),
                            value,
                        ]
                    else:
                        given[name] = value
                payload["fields"] = given
            if args.said_at:
                payload["said_at"] = args.said_at
            row = _request(
                "POST", f"{base}/decisions/{args.decision}/record", key, payload
            )
            print(_fmt(row))
            skipped = (row.get("relayed") or {}).get("skipped")
            if skipped:
                print(
                    "  NOT STORED (he submitted a later answer on the page): "
                    + ", ".join(skipped)
                )
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
        if args.cmd == "reply-note":
            text = sys.stdin.read() if args.text == "-" else args.text
            if not text.strip():
                raise UsageError("nothing to say: the reply text is empty")
            written = _request(
                "POST",
                f"{base}/decisions/{args.decision}/replies",
                key,
                {"body": text},
                idempotency_key=args.idempotency_key or str(uuid.uuid4()),
            )
            page_id = written["artifact_id"]
            page = _request("GET", f"{base}/artifacts/{page_id}", key, None)
            owner = page.get("agent") or "unknown"
            label = page.get("last_session")
            print(f"replied {written['id']} under his answer on {args.decision}")
            print(f"  link: {page.get('page_url') or page_id}#note-{args.decision}")
            print(
                f"  page owner: {owner}"
                + (f" (last session {label})" if label else " (no session label)")
            )
            print(
                "  he sees it under his note; if you are another office, the owner "
                "is told through the service"
            )
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
        if args.cmd == "listening":
            return _listening(args, key, base)
        if args.cmd == "pickup":
            return _pickup(args, key, base)
        if args.cmd == "review-release":
            session = session_label(args.session)
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
            if args.outdated:
                found = [t for t in found if t["anchor_state"] == "outdated"]
            if not found:
                print(
                    "no open comment threads"
                    + (" with an outdated pin" if args.outdated else "")
                    + (" on that page" if args.page else "")
                )
                return 0
            found.sort(key=lambda t: not t["needs_reply"])
            if not args.no_images:
                _fetch_images(found, key, base)
            print("\n\n".join(_fmt_thread(t, on_page=bool(args.page)) for t in found))
            return 0
        if args.cmd == "comment":
            if args.section and not args.quote:
                raise UsageError("--section needs --quote")
            if args.element and (args.quote or args.section):
                raise UsageError("--element and --quote cannot be used together")
            text = sys.stdin.read() if args.text == "-" else args.text
            if not text.strip():
                raise UsageError("nothing to say: the comment text is empty")
            pictures = _read_pictures(args.image)
            page = _request("GET", f"{base}/artifacts/{args.page}", key, None)
            payload: dict = {"body": text}
            if pictures:
                payload["images"] = _upload_pictures(args.page, base, key, pictures)
            if args.element:
                payload["anchor"] = {"kind": "element", "id": args.element}
            elif args.quote:
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
        if args.cmd == "reopen":
            # No open-thread lookup first: a settled thread is exactly what this
            # is for, and the service answers 404 for one that is not yours.
            _request("POST", f"{base}/comments/{args.thread}/reopen", key, {})
            print(f"reopened {args.thread}")
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
            pictures = _read_pictures(getattr(args, "image", None))
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
                {
                    "body": args.text,
                    "thread_root": args.thread,
                    **(
                        {
                            "images": _upload_pictures(
                                thread["artifact_id"], base, key, pictures
                            )
                        }
                        if pictures
                        else {}
                    ),
                },
                idempotency_key=args.idempotency_key or str(uuid.uuid4()),
            )
            print(f"replied on {args.thread}; he sees it in the page's comments")
            return 0
        if args.cmd == "receipt":
            body: dict = {"state": args.state, "note": args.note}
            if args.link:
                body["link"] = args.link
            if args.lifetime:
                body["for"] = args.lifetime
            row = _request(
                "POST",
                f"{base}/decisions/{args.decision}/receipt",
                key,
                body,
                idempotency_key=args.idempotency_key or str(uuid.uuid4()),
            )
            note = f": {row['note']}" if row.get("note") else ""
            print(
                f"{args.decision}: receipt {str(row['state']).replace('_', '-')}{note}"
            )
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
